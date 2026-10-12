"""Explicit, lazily attached persistent-browser runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import importlib
import inspect
from typing import Awaitable, Callable, Mapping, Optional

from ..config import ProfileConfig, RuntimeConfig
from ..errors import BrowserRuntimeError as _CoreBrowserRuntimeError
from .chrome import (
    ChromeAvailability,
    ChromeController,
    ChromeLauncher,
)
from .health import (
    CDPHealthSnapshot,
    CDPSessionChangedError,
    require_same_cdp_session,
)
from .driver import driver_diagnostics, load_driver, verify_browser_version, verify_experiment_process
from .profile_access import DEFAULT_PROFILE_LEASE_ROOT, ProfileReservationManager
from .profile_matching import (
    BrowserProfile,
    ProfileManifest,
    browser_context_id,
    browser_target_id,
    read_selected_profiles,
    match_profile_tabs,
)
from .tabs import (
    BrowserSession,
    OwnedTab,
    OwnedTabCleanupReport,
    OwnedTabRegistry,
    TabSessionIdentity,
)


class BrowserRuntimeAttachError(_CoreBrowserRuntimeError):
    """The explicit CDP attach operation could not complete safely."""


class BrowserRuntimeNotAttached(BrowserRuntimeAttachError):
    """A website requested a page before ``BrowserRuntime.attach()``."""


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


def _attribute(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _quarantine_unconfirmed_owned_tabs(lease_manager, manifest, registry, report) -> None:
    """Keep each cleanup marker bound to that profile's exact owned targets."""
    by_lane: dict[tuple[str, str], set[str]] = {}
    for tab in registry.tabs:
        identity = tab.identity
        if identity.tab_reference in report.unconfirmed_tab_references:
            by_lane.setdefault((identity.provider, identity.profile_id), set()).add("tab:" + identity.tab_reference)
    for (provider, profile_id), references in by_lane.items():
        lease_manager.quarantine(
            (manifest.profile(profile_id),), owner="visibility-runner-owned-tab-cleanup", provider=provider,
            resource_references=tuple(sorted(references)),
            detail="worker could not confirm closure of owned provider tabs",
        )


def _retain_runtime_call(task: asyncio.Future, pending: set) -> None:
    pending.add(task)
    def finished(done):
        pending.discard(done)
        try:
            done.exception()
        except BaseException:
            pass
    task.add_done_callback(finished)


async def _bounded_runtime_call(call, *, timeout_seconds: float, pending: set,
                                message: str, late_cleanup=None, on_late_result=None):
    """Bound the caller without waiting forever for cancellation acknowledgement."""
    async def invoke():
        return await _maybe_await(call())
    task = asyncio.create_task(invoke())
    try:
        done, _ = await asyncio.wait((task,), timeout=max(0.0, float(timeout_seconds)))
        if not done:
            raise BrowserRuntimeAttachError(message)
        return task.result()
    except BaseException:
        if not task.done():
            task.cancel()
            _retain_runtime_call(task, pending)
        if late_cleanup is not None or on_late_result is not None:
            def settle_late(done):
                if done.cancelled():
                    return
                try:
                    result = done.result()
                except BaseException:
                    return
                if on_late_result is not None:
                    on_late_result(result)
                if late_cleanup is not None:
                    cleanup = asyncio.create_task(late_cleanup(result))
                    _retain_runtime_call(cleanup, pending)
            task.add_done_callback(settle_late)
        raise


async def _start_playwright(module: object, *, register_stop=None) -> tuple[object, Callable[[], Awaitable[object]]]:
    factory = getattr(module, "async_playwright", None)
    if not callable(factory):
        raise BrowserRuntimeAttachError("playwright.async_api does not expose async_playwright")
    manager = factory()
    playwright = None
    async def stop():
        method = getattr(playwright, "stop", None)
        if callable(method):
            return await _maybe_await(method())
        method = getattr(manager, "stop", None)
        if callable(method):
            return await _maybe_await(method())
        exit_method = getattr(manager, "__aexit__", None)
        if callable(exit_method):
            return await _maybe_await(exit_method(None, None, None))
        raise BrowserRuntimeAttachError("owned Playwright manager has no cleanup operation")
    if register_stop is not None:
        register_stop(stop)
    start = getattr(manager, "start", None)
    if callable(start):
        playwright = await _maybe_await(start())
        return playwright, stop
    enter = getattr(manager, "__aenter__", None)
    exit_method = getattr(manager, "__aexit__", None)
    if not callable(enter) or not callable(exit_method):
        raise BrowserRuntimeAttachError("async_playwright manager has no supported start operation")
    playwright = await _maybe_await(enter())
    return playwright, stop


@dataclass
class RuntimeAttachment:
    """A live driver connection plus worker-owned tabs.

    ``browser`` is a borrowed persistent Chrome reference.  ``close()`` stops
    the local Playwright transport after closing owned tabs; it never calls
    ``browser.close()``.
    """

    config: RuntimeConfig
    manifest: ProfileManifest
    availability: ChromeAvailability
    attached_health: CDPHealthSnapshot
    browser: object
    session: BrowserSession
    lease_manager: ProfileReservationManager
    _stop_playwright: Callable[[], Awaitable[object]]
    closed: bool = False
    _cleanup_report: Optional[OwnedTabCleanupReport] = None
    _pending_calls: set = field(default_factory=set, repr=False)
    _transport_stopped: bool = False

    @property
    def driver_capabilities(self) -> dict[str, object]:
        """Explicit diagnostics availability for this selected connection."""
        return {**driver_diagnostics(self.config.browser), "observed_browser": self.attached_health.browser}

    @property
    def selected_profiles(self) -> tuple[BrowserProfile, ...]:
        return self.manifest.selected_profiles

    async def open_page(self, profile_id: str, provider: object) -> object:
        if self.closed:
            raise BrowserRuntimeNotAttached("browser attachment is closed")
        return await self.session.open_page(profile_id, provider)

    def page_factory(
        self,
        profile_id: str,
        provider: object,
    ) -> Callable[[], Awaitable[object]]:
        if self.closed:
            raise BrowserRuntimeNotAttached("browser attachment is closed")
        return self.session.page_factory(profile_id, provider)

    async def provider_page_factory(
        self,
        platform: str,
        profile_id: str,
        _observation_id: str,
        fresh: bool,
        identity: object = None,
    ) -> object:
        """Adapter matching the website drivers' five-argument PageFactory.

        A restore returns only the exact still-owned tab reference.  It never
        opens a replacement page for a durable same-chat identity.
        """

        if self.closed:
            raise BrowserRuntimeNotAttached("browser attachment is closed")
        profile = self.manifest.profile(profile_id)
        if fresh:
            return await self.open_page(profile_id, platform)
        if identity is None:
            return None

        def identity_value(name: str) -> str:
            if isinstance(identity, Mapping):
                return str(identity.get(name) or "")
            return str(getattr(identity, name, "") or "")

        if (
            identity_value("profile_id") != profile_id
            or identity_value("chrome_directory")
            != profile.chrome_directory
        ):
            return None
        reference = identity_value("tab_reference")
        if not reference:
            return None
        try:
            tab = self.session.registry.tab(reference)
        except KeyError:
            tab = await self._adopt_durable_owned_tab(
                platform=platform,
                profile=profile,
                reference=reference,
                expected_context=identity_value(
                    "browser_context_reference"
                ),
                expected_target=identity_value(
                    "browser_target_reference"
                ),
            )
            if tab is None:
                return None
        if (
            tab.cleanup_confirmed
            or tab.identity.profile_id != profile_id
            or tab.identity.chrome_directory != profile.chrome_directory
            or tab.identity.provider != platform
            or identity_value("browser_context_reference")
            != tab.identity.browser_context_reference
            or identity_value("browser_target_reference")
            != tab.identity.browser_target_reference
        ):
            return None
        return tab.page

    async def _adopt_durable_owned_tab(
        self,
        *,
        platform: str,
        profile: BrowserProfile,
        reference: str,
        expected_context: str,
        expected_target: str,
    ) -> Optional[OwnedTab]:
        """Recover only a uniquely tagged tab left by an interrupted worker."""

        if not reference.startswith("visibility-runner-owned-tab:"):
            return None
        if platform == "google_ai_overview" and not expected_target:
            return None
        anchor = next(
            (
                value
                for value in self.session.anchors
                if value.profile.id == profile.id
            ),
            None,
        )
        if (
            anchor is None
            or not expected_context
            or anchor.browser_context_id != expected_context
        ):
            return None
        context = getattr(anchor.page, "context", None)
        matches: list[object] = []
        for page in tuple(getattr(context, "pages", ()) or ()):
            is_closed = getattr(page, "is_closed", None)
            if callable(is_closed) and bool(await _maybe_await(is_closed())):
                continue
            if expected_target:
                observed_target = await browser_target_id(page)
                if (
                    not observed_target
                    or f"cdp-target:{observed_target}" != expected_target
                ):
                    continue
            else:
                evaluate = getattr(page, "evaluate", None)
                if not callable(evaluate):
                    continue
                try:
                    observed_reference = str(
                        await _maybe_await(
                            evaluate("() => window.name || ''")
                        )
                        or ""
                    )
                except Exception:
                    continue
                if observed_reference != reference:
                    continue
            if await browser_context_id(page) != expected_context:
                return None
            matches.append(page)
        if len(matches) != 1:
            return None
        page = matches[0]
        adopted = OwnedTab(
            page=page,
            identity=TabSessionIdentity(
                profile_id=profile.id,
                chrome_directory=profile.chrome_directory,
                provider=platform,
                browser_context_reference=expected_context,
                tab_reference=reference,
                browser_target_reference=expected_target,
                conversation_reference=str(
                    getattr(page, "url", "") or ""
                ),
            ),
            marker_url=f"adopted:{reference}",
        )
        return self.session.registry.add(adopted)

    @property
    def provider_factory(self) -> Callable[..., Awaitable[object]]:
        return self.provider_page_factory

    async def close(self) -> OwnedTabCleanupReport:
        if self.closed:
            if self._cleanup_report is None or self._pending_calls or not self._transport_stopped:
                raise BrowserRuntimeAttachError("owned browser cleanup remains unconfirmed")
            report = self._cleanup_report
            report.require_complete()
            return report
        self.closed = True
        report: Optional[OwnedTabCleanupReport] = None
        stop_error: Optional[BaseException] = None
        try:
            def remember_report(value):
                if isinstance(value, OwnedTabCleanupReport):
                    self._cleanup_report = value
            report = await _bounded_runtime_call(self.session.close,
                timeout_seconds=self.config.cleanup_timeout_seconds, pending=self._pending_calls,
                message="owned-tab cleanup deadline exceeded", on_late_result=remember_report)
            self._cleanup_report = report
        finally:
            try:
                await _bounded_runtime_call(self._stop_playwright,
                    timeout_seconds=self.config.cleanup_timeout_seconds, pending=self._pending_calls,
                    message="local Playwright cleanup deadline exceeded",
                    on_late_result=lambda value: setattr(self, "_transport_stopped", True))
                self._transport_stopped = True
            except BaseException as exc:
                stop_error = exc
        if stop_error is not None:
            raise BrowserRuntimeAttachError(
                "owned tabs settled, but the local Playwright transport "
                "could not be stopped"
            ) from stop_error
        assert report is not None
        self._cleanup_report = report
        report.require_complete()
        return report

    async def __aenter__(self) -> "RuntimeAttachment":
        if self.closed:
            raise BrowserRuntimeNotAttached("browser attachment is closed")
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        await self.close()


class BrowserRuntime:
    """Lazily attach browser infrastructure from one explicit RuntimeConfig."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        launcher: Optional[ChromeLauncher] = None,
        chrome_controller: Optional[ChromeController] = None,
    ):
        if not isinstance(config, RuntimeConfig):
            raise TypeError("BrowserRuntime requires an explicit RuntimeConfig")
        if not isinstance(config.profiles, ProfileConfig):
            raise TypeError("RuntimeConfig.profiles must be a ProfileConfig")
        self.config = config
        self.chrome_controller = chrome_controller or ChromeController(
            launcher=launcher
        )
        self._attachment: Optional[RuntimeAttachment] = None
        self._attach_lock = asyncio.Lock()
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._cancel_cleanup_task: Optional[asyncio.Task[object]] = None
        self._attach_task: Optional[asyncio.Task] = None
        self._pending_calls: set = set()
        self._cleanup_callbacks: set = set()
        self._cancel_requested = False
        self._desktop_cleanup: Optional[Callable[[], Awaitable[bool]]] = None
        self._desktop_cleanup_task: Optional[asyncio.Task] = None
        self._desktop_cleanup_confirmed = False

    def set_desktop_cleanup(self, callback: Callable[[], Awaitable[bool]]) -> None:
        """Require desktop actions to drain before any owned tab can close.

        The callback must permanently stop accepting actions and return exactly
        True only once no native action can still use the owned tabs.
        """
        if not callable(callback):
            raise TypeError("desktop cleanup must be callable")
        if self._cancel_requested or self._desktop_cleanup_task is not None:
            raise BrowserRuntimeAttachError("desktop cleanup registration is too late")
        if self._desktop_cleanup is not None and self._desktop_cleanup is not callback:
            raise BrowserRuntimeAttachError("desktop cleanup is already registered")
        self._desktop_cleanup = callback

    async def _before_owned_tab_cleanup(self) -> None:
        callback = self._desktop_cleanup
        if callback is None or self._desktop_cleanup_confirmed:
            return
        task = self._desktop_cleanup_task
        retry = task is not None and task.done() and (
            task.cancelled() or task.exception() is not None or task.result() is not True
        )
        if task is None or retry:
            async def drain():
                return await _maybe_await(callback())
            task = asyncio.create_task(drain())
            self._desktop_cleanup_task = task
            _retain_runtime_call(task, self._pending_calls)
        # Waiting must not cancel a native drain: cancelling an asyncio waiter
        # cannot stop a mouse operation already executing in another thread.
        done, _ = await asyncio.wait(
            (task,), timeout=self.config.cleanup_timeout_seconds,
        )
        if not done:
            raise BrowserRuntimeAttachError("desktop action cleanup deadline exceeded")
        try:
            confirmed = task.result()
        except BaseException as exc:
            raise BrowserRuntimeAttachError("desktop action cleanup remains unconfirmed") from exc
        if confirmed is not True:
            raise BrowserRuntimeAttachError("desktop action cleanup remains unconfirmed")
        self._desktop_cleanup_confirmed = True

    @property
    def attached(self) -> bool:
        return (
            self._attachment is not None
            and not self._attachment.closed
        )

    @property
    def attachment(self) -> RuntimeAttachment:
        if not self.attached:
            raise BrowserRuntimeNotAttached(
                "call and await BrowserRuntime.attach() before browser work"
            )
        assert self._attachment is not None
        return self._attachment

    @property
    def session(self) -> BrowserSession:
        return self.attachment.session

    async def _stop_owned_transport(self, stop) -> None:
        self._cleanup_callbacks.add(stop)
        def confirmed(value):
            self._cleanup_callbacks.discard(stop)
            if self._attachment is not None and self._attachment.closed:
                self._attachment._transport_stopped = True
        await _bounded_runtime_call(stop, timeout_seconds=self.config.cleanup_timeout_seconds,
            pending=self._pending_calls, message="local Playwright cleanup deadline exceeded",
            on_late_result=confirmed)
        confirmed(None)

    @property
    def cleanup_pending(self) -> bool:
        return bool(
            self._pending_calls
            or (self._cleanup_callbacks and not self.attached)
            or (self._desktop_cleanup_task is not None and not self._desktop_cleanup_confirmed)
        )

    async def attach(self) -> RuntimeAttachment:
        """Explicitly verify identities, load the selected driver, and attach."""

        async with self._attach_lock:
            if self._cancel_requested:
                raise asyncio.CancelledError()
            if self.attached:
                return self.attachment
            if self.cleanup_pending:
                raise BrowserRuntimeAttachError("previous local attachment cleanup remains unresolved")

            # Manifest integrity and exact selection are checked before Chrome
            # preflight or importing Playwright.
            manifest = read_selected_profiles(self.config.profiles)
            browser_config = self.config.browser
            self._event_loop = asyncio.get_running_loop()
            self._attach_task = asyncio.current_task()
            # Resolve exact driver metadata before browser preflight. Keep the
            # importer injectable for bounded lifecycle fixtures.
            try:
                playwright_module = load_driver(browser_config, importer=importlib.import_module)
            except BaseException:
                self._attach_task = None
                raise
            try:
                availability = await _bounded_runtime_call(lambda: self.chrome_controller.ensure_available(
                    browser_config, startup_timeout_seconds=browser_config.launch_timeout_seconds,
                    health_timeout_seconds=browser_config.health_timeout_seconds),
                    timeout_seconds=browser_config.launch_timeout_seconds + browser_config.health_timeout_seconds,
                    pending=self._pending_calls, message="browser preflight deadline exceeded")
                verify_browser_version(browser_config, availability.health)
                experiment_process = await _bounded_runtime_call(
                    lambda: asyncio.to_thread(verify_experiment_process, browser_config),
                    timeout_seconds=browser_config.health_timeout_seconds,
                    pending=self._pending_calls, message="experiment process verification deadline exceeded")
            except BaseException:
                self._attach_task = None
                raise

            stop_playwright: Optional[Callable[[], Awaitable[object]]] = None
            try:
                playwright, stop_playwright = await _bounded_runtime_call(
                    lambda: _start_playwright(playwright_module, register_stop=self._cleanup_callbacks.add),
                    timeout_seconds=browser_config.connect_timeout_seconds, pending=self._pending_calls,
                    message="Playwright startup deadline exceeded",
                    late_cleanup=lambda pair: self._stop_owned_transport(pair[1]))
                chromium = getattr(playwright, "chromium", None)
                connect = getattr(chromium, "connect_over_cdp", None)
                if not callable(connect):
                    raise BrowserRuntimeAttachError(
                        "Playwright Chromium has no CDP attach operation"
                    )
                timeout_ms = int(
                    browser_config.connect_timeout_seconds * 1000
                )
                # Inspect the callable contract rather than retry a TypeError
                # raised after a CDP connection may already have started.
                try:
                    inspect.signature(connect).bind(browser_config.cdp_url, timeout=timeout_ms)
                    connect_kwargs = {"timeout": timeout_ms}
                except TypeError:
                    connect_kwargs = {}
                except (ValueError, AttributeError):
                    connect_kwargs = {"timeout": timeout_ms}
                browser = await _bounded_runtime_call(lambda: connect(browser_config.cdp_url, **connect_kwargs),
                    timeout_seconds=browser_config.connect_timeout_seconds, pending=self._pending_calls,
                    message="CDP attachment deadline exceeded")
                attached_health = await _bounded_runtime_call(lambda: self.chrome_controller.health(
                    browser_config.cdp_url, timeout_seconds=browser_config.health_timeout_seconds),
                    timeout_seconds=browser_config.health_timeout_seconds, pending=self._pending_calls,
                    message="attached browser health deadline exceeded")
                require_same_cdp_session(
                    availability.health,
                    attached_health,
                )
                verify_browser_version(browser_config, attached_health)
                if browser_config.driver_experiment:
                    after_process = await _bounded_runtime_call(
                        lambda: asyncio.to_thread(verify_experiment_process, browser_config),
                        timeout_seconds=browser_config.health_timeout_seconds,
                        pending=self._pending_calls, message="experiment process verification deadline exceeded")
                    if experiment_process != after_process:
                        raise BrowserRuntimeAttachError("experiment browser process changed during attachment")
                if not attached_health.reachable:
                    raise BrowserRuntimeAttachError(
                        "CDP became unreachable during browser attachment"
                    )

                anchors = await _bounded_runtime_call(lambda: match_profile_tabs(
                    browser, manifest.selected_profiles, wait_seconds=browser_config.connect_timeout_seconds),
                    timeout_seconds=browser_config.connect_timeout_seconds, pending=self._pending_calls,
                    message="profile attachment identity deadline exceeded")
                lock_root = (
                    self.config.profiles.lease_lock_root
                    or DEFAULT_PROFILE_LEASE_ROOT
                )
                namespace = str(
                    browser_config.user_data_dir
                    or "default-chrome-user-data"
                )
                lease_manager = ProfileReservationManager(
                    lock_root,
                    namespace=namespace,
                )
                registry: OwnedTabRegistry

                async def block_unconfirmed_profile_access(
                    report: OwnedTabCleanupReport,
                ) -> None:
                    _quarantine_unconfirmed_owned_tabs(lease_manager, manifest, registry, report)

                registry = OwnedTabRegistry(
                    on_cleanup_failure=block_unconfirmed_profile_access
                )
                session = BrowserSession(
                    browser=browser,
                    anchors=anchors,
                    tab_open_timeout_seconds=(
                        browser_config.connect_timeout_seconds
                    ),
                    registry=registry,
                    tabs_per_provider=self.config.tabs_per_provider,
                    background_only=browser_config.background_only,
                )
                attachment = RuntimeAttachment(
                    config=self.config,
                    manifest=manifest,
                    availability=availability,
                    attached_health=attached_health,
                    browser=browser,
                    session=session,
                    lease_manager=lease_manager,
                    _stop_playwright=lambda: self._stop_owned_transport(stop_playwright),
                    _pending_calls=self._pending_calls,
                )
                self._attachment = attachment
                self._event_loop = asyncio.get_running_loop()
                return attachment
            except BaseException:
                for cleanup in tuple(self._cleanup_callbacks):
                    try:
                        await self._stop_owned_transport(cleanup)
                    except BaseException:
                        pass
                raise
            finally:
                self._attach_task = None

    async def open_page(self, profile_id: str, provider: object) -> object:
        """Open a page only after a prior explicit ``attach()`` call."""

        return await self.attachment.open_page(profile_id, provider)

    def page_factory(
        self,
        profile_id: str,
        provider: object,
    ) -> Callable[[], Awaitable[object]]:
        """Return the website page-factory seam after explicit attachment."""

        return self.attachment.page_factory(profile_id, provider)

    async def provider_page_factory(
        self,
        platform: str,
        profile_id: str,
        observation_id: str,
        fresh: bool,
        identity: object = None,
    ) -> object:
        return await self.attachment.provider_page_factory(
            platform,
            profile_id,
            observation_id,
            fresh,
            identity,
        )

    @property
    def provider_factory(self) -> Callable[..., Awaitable[object]]:
        """Five-argument page factory accepted by website drivers."""

        # Accessing the adapter itself is harmless, but invocation still
        # requires a prior explicit attach through ``self.attachment``.
        return self.provider_page_factory

    async def close(self) -> OwnedTabCleanupReport:
        await self._before_owned_tab_cleanup()
        if self._attachment is None:
            for cleanup in tuple(self._cleanup_callbacks):
                await self._stop_owned_transport(cleanup)
            if self._pending_calls:
                raise BrowserRuntimeAttachError("local attachment operations remain unresolved")
            return OwnedTabCleanupReport((), ())
        if self._attachment.closed and self._cleanup_callbacks:
            for cleanup in tuple(self._cleanup_callbacks):
                await self._stop_owned_transport(cleanup)
            self._attachment._transport_stopped = True
        task = self._cancel_cleanup_task
        if task is not None:
            try:
                await _bounded_runtime_call(lambda: task, timeout_seconds=self.config.cleanup_timeout_seconds,
                    pending=self._pending_calls, message="cancelled owned-tab cleanup deadline exceeded")
            except BaseException:
                # RuntimeAttachment.close() repeats cleanup and applies the
                # authoritative blocked access/confirmation behavior.
                pass
        try:
            return await self._attachment.close()
        finally:
            self._event_loop = None
            self._cancel_cleanup_task = None

    def request_cancel(self) -> None:
        """Promptly close only worker-owned tabs on the attachment loop."""

        self._cancel_requested = True
        attachment = self._attachment
        loop = self._event_loop
        task = self._attach_task
        if task is not None and not task.done() and loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(task.cancel)
        if (
            attachment is None
            or attachment.closed
            or loop is None
            or loop.is_closed()
        ):
            return

        def schedule_cleanup() -> None:
            current = self._cancel_cleanup_task
            if current is None or current.done():
                async def close_after_desktop_cleanup():
                    await self._before_owned_tab_cleanup()
                    return await _bounded_runtime_call(attachment.session.close,
                        timeout_seconds=self.config.cleanup_timeout_seconds, pending=self._pending_calls,
                        message="cancelled owned-tab cleanup deadline exceeded")
                self._cancel_cleanup_task = asyncio.create_task(close_after_desktop_cleanup())
                if self._desktop_cleanup is not None:
                    _retain_runtime_call(self._cancel_cleanup_task, self._pending_calls)

        loop.call_soon_threadsafe(schedule_cleanup)


__all__ = [
    "BrowserRuntime",
    "BrowserRuntimeAttachError",
    "BrowserRuntimeNotAttached",
    "RuntimeAttachment",
]
