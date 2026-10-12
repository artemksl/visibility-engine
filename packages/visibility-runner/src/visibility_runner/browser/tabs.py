"""Create, identify and close the browser tabs used for measurements."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import errno
import hashlib
import inspect
import json
from pathlib import Path
import tempfile
import threading
from typing import Awaitable, Callable, Iterable, Mapping, Optional
from urllib.parse import quote
import uuid
import weakref

from ..errors import (
    BrowserRuntimeError as _CoreBrowserRuntimeError,
    ProfileIdentityMismatchError as _CoreProfileIdentityMismatchError,
)
from .profile_matching import (
    BrowserProfile,
    DEFAULT_BROWSER_CONTEXT_ID,
    VerifiedProfileTab,
    browser_context_id,
    browser_target_id,
)

try:  # Unix/macOS production path.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


DEFAULT_POPUP_LOCK_PATH = (
    Path(tempfile.gettempdir()) / "clinic-geo-profile-popup.lock"
)
_OWNED_PAGE_IDENTITY_ATTRIBUTE = "_visibility_runner_owned_tab_identity"
_OWNED_PAGE_CONTEXT_ATTRIBUTE = "_visibility_runner_owned_tab_context"
_OWNED_PAGE_TAB_ATTRIBUTE = "_visibility_runner_owned_tab_reference"
_OWNED_TAB_CLEANUP_TIMEOUT_SECONDS = 5.0
_PENDING_OWNED_TAB_OPERATIONS: set[asyncio.Future[object]] = set()


class _UnconfirmedOwnedTarget:
    """Registry placeholder for a CDP target whose closure was not proved."""


class OwnedTabError(_CoreBrowserRuntimeError):
    """An owned tab could not be opened or identified safely."""


class _OwnedTabOperationTimeout(OwnedTabError):
    """A browser operation outlived the owned-tab lifecycle deadline."""


class OwnedTabCleanupError(OwnedTabError):
    """One or more worker-owned tabs could not be confirmed closed."""

    def __init__(self, report: "OwnedTabCleanupReport"):
        self.report = report
        super().__init__(
            "could not confirm cleanup for owned tab(s): "
            + ", ".join(report.unconfirmed_tab_references)
        )


class SessionIdentityMismatch(
    OwnedTabError,
    _CoreProfileIdentityMismatchError,
):
    """A later turn no longer refers to the exact frozen browser session."""


@dataclass(frozen=True)
class TabSessionIdentity:
    profile_id: str
    chrome_directory: str
    provider: str
    browser_context_reference: str
    tab_reference: str
    browser_target_reference: str = ""
    conversation_reference: str = ""

    def with_conversation(self, reference: str) -> "TabSessionIdentity":
        return TabSessionIdentity(
            profile_id=self.profile_id,
            chrome_directory=self.chrome_directory,
            provider=self.provider,
            browser_context_reference=self.browser_context_reference,
            tab_reference=self.tab_reference,
            browser_target_reference=self.browser_target_reference,
            conversation_reference=reference,
        )


def require_same_tab_session(
    expected: TabSessionIdentity | Mapping[str, object],
    observed: TabSessionIdentity | Mapping[str, object],
    *,
    require_conversation: bool = False,
) -> None:
    """Fail if profile, raw directory, context, tab, or conversation changed."""

    def value(item: object, name: str) -> str:
        if isinstance(item, Mapping):
            return str(item.get(name) or "")
        return str(getattr(item, name, "") or "")

    keys = (
        "profile_id",
        "chrome_directory",
        "provider",
        "browser_context_reference",
        "tab_reference",
        "browser_target_reference",
    )
    for key in keys:
        if value(expected, key) != value(observed, key):
            raise SessionIdentityMismatch(f"browser session changed {key}")
    first_conversation = value(expected, "conversation_reference")
    second_conversation = value(observed, "conversation_reference")
    if (
        require_conversation
        and (not first_conversation or not second_conversation)
    ):
        raise SessionIdentityMismatch(
            "browser session lacks a durable conversation reference"
        )
    if first_conversation and first_conversation != second_conversation:
        raise SessionIdentityMismatch("browser conversation reference changed")


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


def _retain_browser_operation(task: asyncio.Future[object]) -> None:
    """Retain an abandoned operation and consume its eventual exception."""

    def finished(completed: asyncio.Future[object]) -> None:
        _PENDING_OWNED_TAB_OPERATIONS.discard(completed)
        try:
            completed.exception()
        except BaseException:
            pass

    if task.done():
        finished(task)
    elif task not in _PENDING_OWNED_TAB_OPERATIONS:
        _PENDING_OWNED_TAB_OPERATIONS.add(task)
        task.add_done_callback(finished)


async def _await_browser_task(
    task: asyncio.Future[object],
    *,
    deadline: float,
) -> object:
    """Enforce a deadline without awaiting cancellation-suppressing code."""

    loop = asyncio.get_running_loop()
    remaining = max(0.0, deadline - loop.time())
    if not task.done() and remaining:
        await asyncio.wait((task,), timeout=remaining)
    if not task.done() or loop.time() >= deadline:
        raise _OwnedTabOperationTimeout(
            "owned-tab browser operation exceeded its deadline"
        )
    return task.result()


async def _bounded_browser_call(
    call: Callable[[], object],
    *,
    deadline: float,
) -> object:
    if asyncio.get_running_loop().time() >= deadline:
        raise _OwnedTabOperationTimeout(
            "owned-tab browser operation exceeded its deadline"
        )
    task = asyncio.create_task(_maybe_await(call()))
    try:
        return await _await_browser_task(task, deadline=deadline)
    except BaseException:
        task.cancel()
        _retain_browser_operation(task)
        raise


async def _page_is_closed(page: object) -> bool:
    method = getattr(page, "is_closed", None)
    if callable(method):
        try:
            return bool(await _maybe_await(method()))
        except Exception:
            return False
    value = getattr(page, "closed", None)
    return bool(value) if isinstance(value, bool) else False


@dataclass
class OwnedTab:
    """One page that this worker proved it created."""

    page: object
    identity: TabSessionIdentity
    marker_url: str
    cleanup_confirmed: bool = False
    _on_cleanup_confirmed: Optional[Callable[["OwnedTab"], None]] = field(
        default=None, repr=False, compare=False,
    )
    _should_retain: Optional[Callable[["OwnedTab"], bool]] = field(
        default=None, repr=False, compare=False,
    )

    def _confirm_cleanup(self) -> None:
        self.cleanup_confirmed = True
        if self._on_cleanup_confirmed is not None:
            self._on_cleanup_confirmed(self)

    async def is_active(self) -> bool:
        if self.cleanup_confirmed:
            return False
        if await _page_is_closed(self.page):
            self._confirm_cleanup()
            return False
        return True

    async def close(self, *, force: bool = False) -> bool:
        if self.cleanup_confirmed:
            return True
        if await _page_is_closed(self.page):
            self._confirm_cleanup()
            return True
        if not force and self._should_retain is not None and self._should_retain(self):
            return False
        close = getattr(self.page, "close", None)
        if not callable(close):
            return False
        has_confirmation_check = callable(
            getattr(self.page, "is_closed", None)
        ) or isinstance(getattr(self.page, "closed", None), bool)
        try:
            await _maybe_await(close())
        except Exception:
            return False
        # Prefer the page's explicit closed state.  Small adapters without that
        # seam treat a successful close call as their acknowledgement.
        confirmed = (
            await _page_is_closed(self.page)
            if has_confirmation_check
            else True
        )
        if confirmed:
            self._confirm_cleanup()
        return self.cleanup_confirmed


@dataclass(frozen=True)
class OwnedTabCleanupReport:
    closed_tab_references: tuple[str, ...]
    unconfirmed_tab_references: tuple[str, ...]
    retained_tab_references: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.unconfirmed_tab_references

    def require_complete(self) -> None:
        if not self.complete:
            raise OwnedTabCleanupError(self)


def _bind_owned_page_identity(tab: OwnedTab) -> None:
    """Bind one registry-proved identity to the exact live Page object.

    The binding is runner-owned Python state, so cross-origin navigation
    cannot clear it as it can ``window.name``.  A different Page/CDP target
    does not inherit the binding and therefore cannot impersonate this tab.
    """

    page = tab.page
    existing = getattr(page, _OWNED_PAGE_IDENTITY_ATTRIBUTE, None)
    if existing is not None and existing != tab.identity:
        raise OwnedTabError(
            "provider Page already carries a different runner-owned identity"
        )
    context = getattr(page, "context", None)
    existing_context = getattr(page, _OWNED_PAGE_CONTEXT_ATTRIBUTE, context)
    if existing is not None and existing_context is not context:
        raise OwnedTabError(
            "provider Page changed browser context before registration"
        )
    try:
        setattr(page, _OWNED_PAGE_IDENTITY_ATTRIBUTE, tab.identity)
        setattr(page, _OWNED_PAGE_CONTEXT_ATTRIBUTE, context)
        setattr(page, _OWNED_PAGE_TAB_ATTRIBUTE, weakref.ref(tab))
    except Exception as exc:
        raise OwnedTabError(
            "provider Page cannot retain its runner-owned identity binding"
        ) from exc


async def retire_closed_owned_page(page: object) -> None:
    """Retire only an exact registry-owned Page with confirmed closed state."""

    reference = getattr(page, _OWNED_PAGE_TAB_ATTRIBUTE, None)
    tab = reference() if isinstance(reference, weakref.ReferenceType) else None
    if tab is not None and tab.page is page:
        await tab.is_active()


class OwnedTabRegistry:
    """Tracks only pages created through an owned-target handshake."""

    def __init__(
        self,
        *,
        on_cleanup_failure: Optional[
            Callable[[OwnedTabCleanupReport], object | Awaitable[object]]
        ] = None,
    ):
        self._tabs: dict[str, OwnedTab] = {}
        self._closed_tabs: dict[str, TabSessionIdentity] = {}
        self._guard = threading.Lock()
        self._on_cleanup_failure = on_cleanup_failure
        self.retention_predicate = None
        self.close_retained = False

    @property
    def tabs(self) -> tuple[OwnedTab, ...]:
        with self._guard:
            return tuple(self._tabs.values())

    def add(self, tab: OwnedTab) -> OwnedTab:
        reference = tab.identity.tab_reference
        with self._guard:
            if reference in self._tabs or reference in self._closed_tabs:
                raise OwnedTabError(
                    f"duplicate owned tab reference {reference!r}"
                )
            _bind_owned_page_identity(tab)
            tab._on_cleanup_confirmed = self._retire_confirmed
            tab._should_retain = lambda value: bool(
                self.retention_predicate is not None and self.retention_predicate(value))
            self._tabs[reference] = tab
        return tab

    def _retire_confirmed(self, tab: OwnedTab) -> None:
        reference = tab.identity.tab_reference
        with self._guard:
            if tab.cleanup_confirmed and self._tabs.get(reference) is tab:
                self._closed_tabs[reference] = tab.identity
                del self._tabs[reference]

    def page(self, tab_reference: str) -> object:
        with self._guard:
            return self._tabs[tab_reference].page

    def tab(self, tab_reference: str) -> OwnedTab:
        with self._guard:
            return self._tabs[tab_reference]

    def profile_website_tabs(
        self,
        profile_id: str,
        provider: str,
    ) -> tuple[OwnedTab, ...]:
        with self._guard:
            return tuple(
                tab
                for tab in self._tabs.values()
                if tab.identity.profile_id == profile_id
                and tab.identity.provider == provider
                and not tab.cleanup_confirmed
            )

    async def close_all(self) -> OwnedTabCleanupReport:
        tabs = self.tabs
        async def dispose(tab):
            if (not self.close_retained and tab._should_retain is not None
                    and tab._should_retain(tab) and await tab.is_active()):
                return "retained"
            return await tab.close(force=self.close_retained)
        outcomes = await asyncio.gather(
            *(dispose(tab) for tab in tabs),
            return_exceptions=True,
        )
        with self._guard:
            closed = list(self._closed_tabs)
        closed_references = set(closed)
        unconfirmed: list[str] = []
        retained: list[str] = []
        for tab, outcome in zip(tabs, outcomes):
            if outcome == "retained":
                retained.append(tab.identity.tab_reference)
            elif outcome is True:
                if tab.identity.tab_reference not in closed_references:
                    closed.append(tab.identity.tab_reference)
                    closed_references.add(tab.identity.tab_reference)
            else:
                unconfirmed.append(tab.identity.tab_reference)
        report = OwnedTabCleanupReport(
            closed_tab_references=tuple(closed),
            unconfirmed_tab_references=tuple(unconfirmed),
            retained_tab_references=tuple(retained),
        )
        if not report.complete and self._on_cleanup_failure is not None:
            await _maybe_await(self._on_cleanup_failure(report))
        with self._guard:
            for reference in closed:
                self._tabs.pop(reference, None)
                self._closed_tabs.pop(reference, None)
        return report

    async def record_unconfirmed_target(
        self,
        identity: TabSessionIdentity,
        marker_url: str,
    ) -> OwnedTabCleanupReport:
        """Retain an unclosed target, or an uncertain uniquely marked creation.

        A creation without a trustworthy reply has no safe target ID to close.
        Its profile/website, tab reference, and unique marker still identify
        the profile and website pair that must remain blocked; never guess a personal target.
        """

        self.add(
            OwnedTab(
                page=_UnconfirmedOwnedTarget(),
                identity=identity,
                marker_url=marker_url,
            )
        )
        report = OwnedTabCleanupReport(
            closed_tab_references=(),
            unconfirmed_tab_references=(identity.tab_reference,),
        )
        if self._on_cleanup_failure is not None:
            await _maybe_await(self._on_cleanup_failure(report))
        return report


def _acquire_popup_lock(path: Path) -> Optional[object]:
    """Try the stable lock inode once; never leave a waiting thread or fd."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise
    except BaseException:
        handle.close()
        raise
    return handle


def _release_popup_lock(handle: object) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


async def _finish_shielded_task(
    task: asyncio.Task[object],
    *,
    deadline: Optional[float] = None,
) -> object:
    """Reap an operation despite repeated cancellation, optionally bounded."""

    while True:
        try:
            if deadline is None:
                return await asyncio.shield(task)
            if task.done():
                return task.result()
            return await _await_browser_task(task, deadline=deadline)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def _get_popup_lock(path: Path, *, deadline: float) -> object:
    loop = asyncio.get_running_loop()
    while loop.time() < deadline:
        # These local nonblocking operations contain no await: cancellation
        # cannot discard an acquired handle between acquisition and handoff.
        handle = _acquire_popup_lock(path)
        if handle is not None:
            if loop.time() >= deadline:
                _release_popup_lock(handle)
                break
            return handle
        await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))
    raise _OwnedTabOperationTimeout("owned-tab popup lock exceeded its deadline")


def _profile_popup_lock_path(path: Path | str, chrome_directory: str) -> Path:
    """Coordinate an exact raw profile across aliases and worker processes."""

    base = Path(path).expanduser().resolve()
    profile_key = hashlib.sha256(chrome_directory.encode("utf-8")).hexdigest()
    return base.with_name(f"{base.name}.profile-{profile_key}")


async def _tab_creation_locks(
    anchor: VerifiedProfileTab,
    popup_lock_path: Path | str,
    *,
    deadline: float,
    background_target: bool,
) -> tuple[object, ...]:
    # An exact-profile anchor and unique marker need no other profile's lock.
    # Keep the historical global guard for the CDP createTarget path, which
    # also takes the profile guard to coordinate with anchor-based creation.
    paths = [_profile_popup_lock_path(
        popup_lock_path, anchor.profile.chrome_directory,
    )]
    if background_target:
        paths.insert(0, Path(popup_lock_path).expanduser().resolve())
    handles = []
    try:
        for path in paths:
            handles.append(await _get_popup_lock(path, deadline=deadline))
        return tuple(handles)
    except BaseException:
        _release_tab_creation_locks(handles)
        raise


def _release_tab_creation_locks(handles: Iterable[object]) -> None:
    for handle in reversed(tuple(handles)):
        _release_popup_lock(handle)


@asynccontextmanager
async def _opening_lock(lock: asyncio.Lock, *, deadline: float):
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise _OwnedTabOperationTimeout("owned-tab opening lock exceeded its deadline")
    try:
        await asyncio.wait_for(lock.acquire(), timeout=remaining)
    except asyncio.TimeoutError as exc:
        raise _OwnedTabOperationTimeout(
            "owned-tab opening lock exceeded its deadline"
        ) from exc
    try:
        yield
    finally:
        lock.release()


def _context_pages(anchor: VerifiedProfileTab) -> tuple[object, ...]:
    context = getattr(anchor.page, "context", None)
    return tuple(getattr(context, "pages", ()) or ())


def _profile_tab_browser(anchor: VerifiedProfileTab, browser: object = None) -> object:
    if browser is not None:
        return browser
    context = getattr(anchor.page, "context", None)
    return getattr(context, "browser", None)


async def _detach_cdp_session(session: object, *, deadline: float) -> None:
    detach = getattr(session, "detach", None)
    if callable(detach):
        try:
            await _bounded_browser_call(detach, deadline=deadline)
        except (Exception, asyncio.CancelledError):
            pass


async def _close_cdp_target(
    session: object,
    target_id: str,
    *,
    deadline: float,
) -> bool:
    send = getattr(session, "send", None)
    if not callable(send) or not target_id:
        return False
    try:
        response = await _bounded_browser_call(
            lambda: send("Target.closeTarget", {"targetId": target_id}),
            deadline=deadline,
        )
        if isinstance(response, Mapping) and response.get("success") is True:
            return True
    except (Exception, asyncio.CancelledError):
        pass
    try:
        response = await _bounded_browser_call(
            lambda: send("Target.getTargets", {}),
            deadline=deadline,
        )
    except (Exception, asyncio.CancelledError):
        return False
    if not isinstance(response, Mapping):
        return False
    targets = response.get("targetInfos")
    if not isinstance(targets, (list, tuple)):
        return False
    return not any(
        isinstance(target, Mapping) and target.get("targetId") == target_id
        for target in targets
    )


def _created_target_id(response: object) -> str:
    if not isinstance(response, Mapping):
        raise OwnedTabError(
            "Chrome returned an invalid background-target response"
        )
    target_value = response.get("targetId")
    if (
        not isinstance(target_value, str)
        or not target_value
        or target_value != target_value.strip()
    ):
        raise OwnedTabError(
            "Chrome did not identify the background worker-owned tab"
        )
    return target_value


def _defer_late_browser_cleanup(
    task: asyncio.Task[object],
    *,
    cdp_session: object = None,
) -> None:
    """Reap only the exact result of an unfinished session/target creation.

    This cannot submit a website prompt or clear blocked access. A late target
    reply authorizes closing only that returned target, never a guessed tab.
    """

    _retain_browser_operation(task)

    def finished(completed: asyncio.Future[object]) -> None:
        session = cdp_session
        target_id = ""
        try:
            value = completed.result()
            if session is None:
                session = value
            else:
                target_id = _created_target_id(value)
        except BaseException:
            pass
        if session is None:
            return

        async def cleanup() -> None:
            deadline = (
                asyncio.get_running_loop().time()
                + _OWNED_TAB_CLEANUP_TIMEOUT_SECONDS
            )
            if target_id:
                await _close_cdp_target(
                    session,
                    target_id,
                    deadline=deadline,
                )
            await _detach_cdp_session(session, deadline=deadline)

        _retain_browser_operation(asyncio.create_task(cleanup()))

    task.add_done_callback(finished)


async def _finalize_owned_tab_open(
    *,
    anchor: VerifiedProfileTab,
    provider: str,
    registry: OwnedTabRegistry,
    marker: str,
    tab_reference: str,
    handed_off: bool,
    cdp_session: object,
    session_task: Optional[asyncio.Task[object]],
    creation_requested: bool,
    creation_task: Optional[asyncio.Task[object]],
    created_target_id: str,
    process_lock: tuple[object, ...],
) -> Optional[OwnedTabCleanupReport]:
    """Finish cleanup, detachment, lock release, and blocked access reporting."""

    cleanup_unconfirmed = False
    deadline = (
        asyncio.get_running_loop().time()
        + _OWNED_TAB_CLEANUP_TIMEOUT_SECONDS
    )
    try:
        if cdp_session is None and session_task is not None:
            try:
                cdp_session = await _finish_shielded_task(
                    session_task,
                    deadline=deadline,
                )
            except _OwnedTabOperationTimeout:
                _defer_late_browser_cleanup(session_task)
            except BaseException:
                pass
        if creation_task is not None and not created_target_id:
            try:
                response = await _finish_shielded_task(
                    creation_task,
                    deadline=deadline,
                )
                created_target_id = _created_target_id(response)
            except _OwnedTabOperationTimeout:
                cleanup_unconfirmed = True
                _defer_late_browser_cleanup(
                    creation_task,
                    cdp_session=cdp_session,
                )
                # The continuation owns detachment so it can close the exact
                # target if Chrome eventually replies. The profile and website pair stays blocked.
                cdp_session = None
            except BaseException:
                pass
        # A transport error or malformed reply cannot prove that Chrome
        # rejected an invoked create request before allocating its target.
        # Retain the unique marker/profile and website pair without guessing an ID to close.
        if creation_requested and not created_target_id:
            cleanup_unconfirmed = True
        if not handed_off:
            for page in _context_pages(anchor):
                try:
                    matches = str(getattr(page, "url", "")) == marker
                except Exception:
                    matches = False
                if not matches:
                    continue
                close = getattr(page, "close", None)
                if callable(close):
                    try:
                        await _bounded_browser_call(close, deadline=deadline)
                    except (Exception, asyncio.CancelledError):
                        cleanup_unconfirmed = True
                else:
                    cleanup_unconfirmed = True
            if cdp_session is not None and created_target_id:
                target_closed = await _close_cdp_target(
                    cdp_session,
                    created_target_id,
                    deadline=deadline,
                )
                cleanup_unconfirmed = cleanup_unconfirmed or not target_closed
        if cdp_session is not None:
            await _detach_cdp_session(cdp_session, deadline=deadline)
    finally:
        # LOCK_UN and close are non-blocking local-descriptor operations. Do
        # not queue release behind threads blocked on other lock acquisitions.
        _release_tab_creation_locks(process_lock)
    if not cleanup_unconfirmed:
        return None
    return await registry.record_unconfirmed_target(
        TabSessionIdentity(
            profile_id=anchor.profile.id,
            chrome_directory=anchor.profile.chrome_directory,
            provider=provider,
            browser_context_reference=anchor.browser_context_id,
            tab_reference=tab_reference,
            browser_target_reference=(
                f"cdp-target:{created_target_id}" if created_target_id else ""
            ),
        ),
        marker,
    )


_ANCHOR_OPEN_SCRIPT = """(url) => {
  const child = window.open(url, "_blank");
  return child !== null;
}"""

_BACKGROUND_ANCHOR_OPEN_SCRIPT = """(url) => {
  const link = document.createElement('a');
  link.href = url;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  (document.body || document.documentElement).appendChild(link);
  try {
    return link.dispatchEvent(new MouseEvent('click', {
      view: window, bubbles: true, cancelable: true, button: 0,
      ctrlKey: true, metaKey: true, shiftKey: false, altKey: false
    }));
  } finally {
    link.remove();
  }
}"""


async def _open_background_anchor_tab(anchor: VerifiedProfileTab, marker: str) -> bool:
    """Request an inactive tab from the exact anchor's isolated CDP world.

    Chromium honors background-tab modifiers in isolated worlds. Main-world
    synthetic events are promoted to foreground tabs, so never fall back to
    page.evaluate or window.open. The enclosing creation task owns deadlines,
    late marker cleanup, and the profile lock through session detachment.
    """
    context = getattr(anchor.page, "context", None)
    create_session = getattr(context, "new_cdp_session", None)
    if not callable(create_session):
        raise OwnedTabError("verified profile cannot open an inactive worker tab")
    session = await _maybe_await(create_session(anchor.page))
    try:
        tree = await session.send("Page.getFrameTree")
        frame_id = tree.get("frameTree", {}).get("frame", {}).get("id")
        if not isinstance(frame_id, str) or not frame_id:
            raise OwnedTabError("verified profile main frame is unavailable")
        world = await session.send("Page.createIsolatedWorld", {
            "frameId": frame_id, "worldName": "visibility-runner-background-tab",
        })
        context_id = world.get("executionContextId")
        if type(context_id) is not int or context_id <= 0:
            raise OwnedTabError("verified profile isolated world is unavailable")
        result = await session.send("Runtime.evaluate", {
            "expression": "(" + _BACKGROUND_ANCHOR_OPEN_SCRIPT + ")(" + json.dumps(marker) + ")",
            "contextId": context_id, "returnByValue": True,
        })
        return not result.get("exceptionDetails") and result.get("result", {}).get("value") is True
    finally:
        await _maybe_await(session.detach())


async def _find_marker_page(
    anchor: VerifiedProfileTab,
    marker: str,
    *,
    deadline: float,
) -> object | None:
    loop = asyncio.get_running_loop()
    while loop.time() < deadline:
        for page in _context_pages(anchor):
            try:
                if str(getattr(page, "url", "")) == marker:
                    return page
            except Exception:
                continue
        await asyncio.sleep(0.05)
    return None


def _defer_profile_tab_cleanup(
    task: asyncio.Task[object],
    *,
    anchor: VerifiedProfileTab,
    marker: str,
) -> None:
    """Close only the unique marker if a late anchor-open eventually lands."""

    _retain_browser_operation(task)

    def finished(completed: asyncio.Future[object]) -> None:
        try:
            completed.result()
        except BaseException:
            pass

        async def cleanup() -> None:
            deadline = (
                asyncio.get_running_loop().time()
                + _OWNED_TAB_CLEANUP_TIMEOUT_SECONDS
            )
            page = await _find_marker_page(
                anchor,
                marker,
                deadline=deadline,
            )
            if page is None:
                return
            tab = OwnedTab(
                page=page,
                identity=TabSessionIdentity(
                    profile_id=anchor.profile.id,
                    chrome_directory=anchor.profile.chrome_directory,
                    provider="late-anchor-cleanup",
                    browser_context_reference=anchor.browser_context_id,
                    tab_reference="late-anchor-cleanup",
                ),
                marker_url=marker,
            )
            await tab.close()

        _retain_browser_operation(asyncio.create_task(cleanup()))

    task.add_done_callback(finished)


async def _finish_measurement_tab_creation(
    *,
    anchor: VerifiedProfileTab,
    provider: str,
    registry: OwnedTabRegistry,
    marker: str,
    tab_reference: str,
    handed_off: bool,
    creation_requested: bool,
    creation_task: Optional[asyncio.Task[object]],
    process_lock: tuple[object, ...],
) -> Optional[OwnedTabCleanupReport]:
    cleanup_unconfirmed = False
    deadline = (
        asyncio.get_running_loop().time()
        + _OWNED_TAB_CLEANUP_TIMEOUT_SECONDS
    )
    try:
        if creation_task is not None and not creation_task.done():
            try:
                await _finish_shielded_task(creation_task, deadline=deadline)
            except _OwnedTabOperationTimeout:
                cleanup_unconfirmed = True
                _defer_profile_tab_cleanup(
                    creation_task,
                    anchor=anchor,
                    marker=marker,
                )
            except BaseException:
                cleanup_unconfirmed = creation_requested
        if not handed_off and creation_requested:
            page = await _find_marker_page(
                anchor,
                marker,
                deadline=deadline,
            )
            if page is None:
                cleanup_unconfirmed = True
            else:
                tab = OwnedTab(
                    page=page,
                    identity=TabSessionIdentity(
                        profile_id=anchor.profile.id,
                        chrome_directory=anchor.profile.chrome_directory,
                        provider=provider,
                        browser_context_reference=anchor.browser_context_id,
                        tab_reference=tab_reference,
                    ),
                    marker_url=marker,
                )
                cleanup_unconfirmed = (
                    cleanup_unconfirmed or not await tab.close()
                )
    finally:
        _release_tab_creation_locks(process_lock)
    if not cleanup_unconfirmed:
        return None
    return await registry.record_unconfirmed_target(
        TabSessionIdentity(
            profile_id=anchor.profile.id,
            chrome_directory=anchor.profile.chrome_directory,
            provider=provider,
            browser_context_reference=anchor.browser_context_id,
            tab_reference=tab_reference,
        ),
        marker,
    )


async def open_measurement_tab(
    anchor: VerifiedProfileTab,
    *,
    provider: str,
    registry: OwnedTabRegistry,
    open_lock: asyncio.Lock,
    popup_lock_path: Path,
    timeout_seconds: float,
    background_only: bool = False,
    _deadline: Optional[float] = None,
) -> OwnedTab:
    """Open one exact-profile tab from its verified persistent anchor."""

    deadline = (
        asyncio.get_running_loop().time() + max(0.05, float(timeout_seconds))
        if _deadline is None else _deadline
    )
    token = uuid.uuid4().hex
    tab_reference = f"visibility-runner-owned-tab:{token}"
    marker = (
        "about:blank#visibility-runner-owned-tab="
        + quote(token, safe="")
    )
    process_lock = await _tab_creation_locks(
        anchor, popup_lock_path, deadline=deadline, background_target=False,
    )
    handed_off = False
    creation_requested = False
    creation_task = None
    current_task = asyncio.current_task()
    cancellation_count = current_task.cancelling() if current_task else 0
    try:
        async with _opening_lock(open_lock, deadline=deadline):
            evaluate = getattr(anchor.page, "evaluate", None)
            if not background_only and not callable(evaluate):
                raise OwnedTabError("verified profile tab cannot open a worker tab")
            creation_requested = True
            creation_task = asyncio.create_task(
                _open_background_anchor_tab(anchor, marker) if background_only
                else _maybe_await(evaluate(_ANCHOR_OPEN_SCRIPT, marker))
            )
            opened = await _await_browser_task(
                creation_task,
                deadline=deadline,
            )
            if opened is not True:
                creation_requested = False
                raise OwnedTabError("verified profile tab did not open a worker tab")
            page = await _find_marker_page(
                anchor,
                marker,
                deadline=deadline,
            )
            if page is None:
                raise OwnedTabError(
                    "verified profile tab worker tab was not observable"
                )
            actual_context_id = await _bounded_browser_call(
                lambda: browser_context_id(page),
                deadline=deadline,
            )
            if actual_context_id != anchor.browser_context_id:
                raise OwnedTabError(
                    "anchor-owned tab opened in the wrong Chrome context"
                )
            target_id = await _bounded_browser_call(
                lambda: browser_target_id(page),
                deadline=deadline,
            )
            if not target_id:
                raise OwnedTabError(
                    "Chrome did not identify the anchor-owned tab"
                )
            tab = registry.add(
                OwnedTab(
                    page=page,
                    identity=TabSessionIdentity(
                        profile_id=anchor.profile.id,
                        chrome_directory=anchor.profile.chrome_directory,
                        provider=provider,
                        browser_context_reference=anchor.browser_context_id,
                        tab_reference=tab_reference,
                        browser_target_reference=f"cdp-target:{target_id}",
                    ),
                    marker_url=marker,
                )
            )
            handed_off = True
            return tab
    finally:
        finalizer_task = asyncio.create_task(
            _finish_measurement_tab_creation(
                anchor=anchor,
                provider=provider,
                registry=registry,
                marker=marker,
                tab_reference=tab_reference,
                handed_off=handed_off,
                creation_requested=creation_requested,
                creation_task=creation_task,
                process_lock=process_lock,
            )
        )
        report = await _finish_shielded_task(finalizer_task)
        if report is not None:
            raise OwnedTabCleanupError(report)
        if (
            current_task is not None
            and current_task.cancelling() > cancellation_count
        ):
            raise asyncio.CancelledError


async def open_owned_tab(
    anchor: VerifiedProfileTab,
    *,
    provider: object,
    registry: OwnedTabRegistry,
    browser: object = None,
    open_lock: Optional[asyncio.Lock] = None,
    popup_lock_path: Path | str = DEFAULT_POPUP_LOCK_PATH,
    timeout_seconds: float = 20.0,
    _deadline: Optional[float] = None,
) -> OwnedTab:
    """Open and identify exactly one worker-owned tab in an anchor's profile."""

    loop = asyncio.get_running_loop()
    deadline = (
        loop.time() + max(0.05, float(timeout_seconds))
        if _deadline is None else _deadline
    )
    provider_value = getattr(provider, "value", provider)
    if (
        not isinstance(provider_value, str)
        or not provider_value
        or provider_value != provider_value.strip()
    ):
        raise OwnedTabError("provider must be non-empty exact text")
    token = uuid.uuid4().hex
    tab_reference = f"visibility-runner-owned-tab:{token}"
    marker = (
        "about:blank#visibility-runner-owned-tab="
        + quote(token, safe="")
    )
    process_lock = await _tab_creation_locks(
        anchor, popup_lock_path, deadline=deadline, background_target=True,
    )
    local_lock = open_lock or asyncio.Lock()
    handed_off = False
    cdp_session = None
    session_task = None
    creation_requested = False
    creation_task = None
    created_target_id = ""
    try:
        async with _opening_lock(local_lock, deadline=deadline):
            borrowed_browser = _profile_tab_browser(anchor, browser)
            new_cdp_session = getattr(
                borrowed_browser,
                "new_browser_cdp_session",
                None,
            )
            if not callable(new_cdp_session):
                raise OwnedTabError(
                    "borrowed Chrome connection cannot create a background "
                    "worker-owned tab"
                )
            try:
                session_task = asyncio.create_task(
                    _maybe_await(new_cdp_session())
                )
                cdp_session = await _await_browser_task(
                    session_task,
                    deadline=deadline,
                )
                send = getattr(cdp_session, "send", None)
                if not callable(send):
                    raise OwnedTabError(
                        "borrowed Chrome connection returned an invalid CDP "
                        "session"
                    )
                create_params: dict[str, object] = {
                    "url": marker,
                    "background": True,
                }
                if anchor.browser_context_id != DEFAULT_BROWSER_CONTEXT_ID:
                    create_params["browserContextId"] = (
                        anchor.browser_context_id
                    )
                creation_requested = True
                creation_task = asyncio.create_task(
                    _maybe_await(
                        send("Target.createTarget", create_params)
                    )
                )
                response = await _await_browser_task(
                    creation_task,
                    deadline=deadline,
                )
            except OwnedTabError:
                raise
            except Exception as exc:
                raise OwnedTabError(
                    "Chrome could not create a background worker-owned tab"
                ) from exc
            created_target_id = _created_target_id(response)
            while loop.time() < deadline:
                for page in _context_pages(anchor):
                    try:
                        page_url = str(getattr(page, "url", ""))
                    except Exception:
                        continue
                    if page_url != marker:
                        continue
                    actual_context_id = await _bounded_browser_call(
                        lambda: browser_context_id(page),
                        deadline=deadline,
                    )
                    if actual_context_id is None:
                        continue
                    if actual_context_id != anchor.browser_context_id:
                        # The finalizer closes only this unique marker and the
                        # exact target returned by Chrome, under its deadline.
                        raise OwnedTabError(
                            "owned tab opened in the wrong Chrome context: "
                            f"expected {anchor.browser_context_id!r}, got "
                            f"{actual_context_id!r}"
                        )
                    target_id = await _bounded_browser_call(
                        lambda: browser_target_id(page),
                        deadline=deadline,
                    )
                    if target_id is None:
                        continue
                    if target_id != created_target_id:
                        raise OwnedTabError(
                            "owned tab does not match the background CDP "
                            "target Chrome created"
                        )
                    tab = registry.add(
                        OwnedTab(
                            page=page,
                            identity=TabSessionIdentity(
                                profile_id=anchor.profile.id,
                                chrome_directory=(
                                    anchor.profile.chrome_directory
                                ),
                                provider=provider_value,
                                browser_context_reference=(
                                    anchor.browser_context_id
                                ),
                                tab_reference=tab_reference,
                                browser_target_reference=(
                                    f"cdp-target:{target_id}"
                                    if target_id
                                    else ""
                                ),
                            ),
                            marker_url=marker,
                        )
                    )
                    handed_off = True
                    return tab
                await asyncio.sleep(0.05)
        raise OwnedTabError(
            f"could not open an owned {provider_value} tab for profile "
            f"{anchor.profile.id!r}"
        )
    finally:
        current_task = asyncio.current_task()
        cancellation_count = (
            current_task.cancelling() if current_task is not None else 0
        )
        finalizer_task = asyncio.create_task(
            _finalize_owned_tab_open(
                anchor=anchor,
                provider=provider_value,
                registry=registry,
                marker=marker,
                tab_reference=tab_reference,
                handed_off=handed_off,
                cdp_session=cdp_session,
                session_task=session_task,
                creation_requested=creation_requested,
                creation_task=creation_task,
                created_target_id=created_target_id,
                process_lock=process_lock,
            )
        )
        report = await _finish_shielded_task(finalizer_task)
        if report is not None:
            assert isinstance(report, OwnedTabCleanupReport)
            raise OwnedTabCleanupError(report)
        if (
            current_task is not None
            and current_task.cancelling() > cancellation_count
        ):
            raise asyncio.CancelledError


class BrowserSession:
    """Page factory over selected anchors; never an owner of Chrome itself."""

    def __init__(
        self,
        *,
        browser: object,
        anchors: Iterable[VerifiedProfileTab],
        popup_lock_path: Path | str = DEFAULT_POPUP_LOCK_PATH,
        tab_open_timeout_seconds: float = 20.0,
        registry: Optional[OwnedTabRegistry] = None,
        tabs_per_provider: int = 1,
        background_only: bool = False,
    ):
        self.browser = browser
        if type(background_only) is not bool:
            raise OwnedTabError("background_only must be a boolean")
        self.background_only = background_only
        self.anchors = tuple(anchors)
        self._by_profile = {
            anchor.profile.id: anchor for anchor in self.anchors
        }
        if len(self._by_profile) != len(self.anchors):
            raise OwnedTabError("verified profile tabs contain a duplicate profile ID")
        self.popup_lock_path = Path(popup_lock_path).expanduser().resolve()
        self.tab_open_timeout_seconds = float(tab_open_timeout_seconds)
        self.registry = registry or OwnedTabRegistry()
        if isinstance(tabs_per_provider, bool) or not isinstance(
            tabs_per_provider, int
        ) or tabs_per_provider < 1:
            raise OwnedTabError("tabs_per_provider must be a positive integer")
        self.tabs_per_provider = int(tabs_per_provider)
        self._open_locks = {
            anchor.profile.id: asyncio.Lock() for anchor in self.anchors
        }
        self._profile_website_locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def open_owned_tab(
        self,
        profile_id: str,
        provider: object,
    ) -> OwnedTab:
        deadline = asyncio.get_running_loop().time() + max(
            0.05, self.tab_open_timeout_seconds,
        )
        try:
            anchor = self._by_profile[profile_id]
        except KeyError as exc:
            raise OwnedTabError(
                f"profile {profile_id!r} was not selected for this runtime"
            ) from exc
        provider_value = getattr(provider, "value", provider)
        if not isinstance(provider_value, str) or not provider_value:
            raise OwnedTabError("provider must be non-empty exact text")
        profile_website_key = (profile_id, provider_value)
        profile_website_lock = self._profile_website_locks.setdefault(profile_website_key, asyncio.Lock())
        async with _opening_lock(profile_website_lock, deadline=deadline):
            active_count = 0
            for tab in self.registry.profile_website_tabs(profile_id, provider_value):
                if isinstance(tab.page, _UnconfirmedOwnedTarget):
                    # A creation without a trustworthy reply keeps the whole
                    # profile/website pair blocked, whatever the tab budget.
                    raise OwnedTabError(
                        f"profile/provider lane {profile_id!r}/"
                        f"{provider_value!r} already owns an active tab"
                    )
                if await _bounded_browser_call(tab.is_active, deadline=deadline):
                    active_count += 1
            if active_count >= self.tabs_per_provider:
                raise OwnedTabError(
                    f"profile/provider lane {profile_id!r}/"
                    f"{provider_value!r} already owns an active tab"
                    if self.tabs_per_provider == 1
                    else (
                        f"profile/provider lane {profile_id!r}/"
                        f"{provider_value!r} already owns {active_count} "
                        f"active tabs (limit {self.tabs_per_provider})"
                    )
                )
            if (
                (len(self.anchors) > 1 or self.background_only)
                and anchor.browser_context_id
                != DEFAULT_BROWSER_CONTEXT_ID
            ):
                return await open_measurement_tab(
                    anchor,
                    provider=provider_value,
                    registry=self.registry,
                    open_lock=self._open_locks[profile_id],
                    popup_lock_path=self.popup_lock_path,
                    timeout_seconds=self.tab_open_timeout_seconds,
                    background_only=self.background_only,
                    _deadline=deadline,
                )
            return await open_owned_tab(
                anchor,
                provider=provider_value,
                registry=self.registry,
                browser=self.browser,
                open_lock=self._open_locks[profile_id],
                popup_lock_path=self.popup_lock_path,
                timeout_seconds=self.tab_open_timeout_seconds,
                _deadline=deadline,
            )

    async def open_page(self, profile_id: str, provider: object) -> object:
        """Return the raw page while retaining its ownership in the registry."""

        return (await self.open_owned_tab(profile_id, provider)).page

    def page_factory(
        self,
        profile_id: str,
        provider: object,
    ) -> Callable[[], Awaitable[object]]:
        """Bind an async zero-argument page factory for website drivers."""

        async def factory() -> object:
            return await self.open_page(profile_id, provider)

        return factory

    async def close(self) -> OwnedTabCleanupReport:
        """Close owned tabs only; persistent Chrome is deliberately untouched."""

        return await self.registry.close_all()


OwnedBrowserSession = BrowserSession


__all__ = [
    "BrowserSession",
    "DEFAULT_POPUP_LOCK_PATH",
    "OwnedBrowserSession",
    "OwnedTab",
    "OwnedTabCleanupError",
    "OwnedTabCleanupReport",
    "OwnedTabError",
    "OwnedTabRegistry",
    "SessionIdentityMismatch",
    "TabSessionIdentity",
    "open_owned_tab",
    "require_same_tab_session",
]
