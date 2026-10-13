"""Attach-only and launch-if-required persistent Chrome preflight.

Launchers are injected and have one operation: start Chrome.  There is
intentionally no stop, terminate, relaunch, or browser-close method in this
package because the persistent browser is owned outside measurement workers.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping, Optional, Protocol, Sequence, TYPE_CHECKING
from urllib.parse import urlsplit

from platform_runtime.processes import popen_session_kwargs

from ..errors import (
    BrowserRuntimeError as _CoreBrowserRuntimeError,
    BrowserUnavailableError as _CoreBrowserUnavailableError,
)
from .health import CDPHealthSnapshot, probe_cdp_health

if TYPE_CHECKING:
    from ..config import BrowserConfig


class ChromeRuntimeError(_CoreBrowserRuntimeError):
    """Persistent Chrome did not satisfy the explicit runtime policy."""


class ChromeAttachUnavailable(ChromeRuntimeError, _CoreBrowserUnavailableError):
    """Attach-only policy found no reachable CDP endpoint."""


class ChromeLaunchError(ChromeRuntimeError):
    """Chrome could not be started or did not expose CDP in time."""


@dataclass(frozen=True)
class ChromeLaunchRequest:
    cdp_url: str
    chrome_binary: Optional[Path]
    user_data_dir: Optional[Path]
    extra_arguments: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChromeLaunchResult:
    """Opaque launch acknowledgement; the process remains externally owned."""

    process_id: Optional[int] = None
    detail: str = ""


@dataclass(frozen=True)
class ChromeAvailability:
    health: CDPHealthSnapshot
    launched: bool
    launch_result: Optional[ChromeLaunchResult] = None


class ChromeLauncher(Protocol):
    def launch(
        self,
        request: ChromeLaunchRequest,
    ) -> ChromeLaunchResult | object:
        """Start persistent Chrome and return without assuming ownership."""


def _value(
    config: object,
    names: Sequence[str],
    default: Any = None,
) -> Any:
    if isinstance(config, Mapping):
        for name in names:
            if name in config:
                return config[name]
    else:
        for name in names:
            if hasattr(config, name):
                return getattr(config, name)
    return default


def _exact_policy(value: object) -> str:
    raw = getattr(value, "value", value)
    if not isinstance(raw, str):
        raise ChromeRuntimeError("launch_policy must be an explicit enum or string")
    aliases = {
        "attach_only": "attach_only",
        "attach-only": "attach_only",
        "launch_if_required": "launch_if_required",
        "launch-if-required": "launch_if_required",
    }
    try:
        return aliases[raw]
    except KeyError as exc:
        raise ChromeRuntimeError(
            "launch_policy must be attach_only or launch_if_required"
        ) from exc


def build_launch_request(config: "BrowserConfig | object") -> ChromeLaunchRequest:
    cdp_url = _value(config, ("cdp_url",))
    binary = _value(config, ("chrome_binary", "binary"))
    user_data = _value(
        config,
        ("user_data_dir", "chrome_user_data_dir"),
    )
    if not isinstance(cdp_url, str) or not cdp_url:
        raise ChromeLaunchError("BrowserConfig.cdp_url must be explicit")
    launcher_path = _value(config, ("launcher_path",), None)
    if launcher_path is None and (binary is None or user_data is None):
        raise ChromeLaunchError(
            "launch-if-required needs launcher_path or both chrome_binary "
            "and user_data_dir"
        )
    raw_arguments = _value(
        config,
        ("launch_arguments", "extra_arguments", "chrome_arguments"),
        (),
    )
    if not isinstance(raw_arguments, (tuple, list)):
        raise ChromeLaunchError("Chrome launch arguments must be a sequence")
    if any(not isinstance(argument, str) for argument in raw_arguments):
        raise ChromeLaunchError("Chrome launch arguments must contain only text")
    return ChromeLaunchRequest(
        cdp_url=cdp_url,
        chrome_binary=(
            Path(binary).expanduser().resolve()
            if binary is not None
            else None
        ),
        user_data_dir=(
            Path(user_data).expanduser().resolve()
            if user_data is not None
            else None
        ),
        extra_arguments=tuple(raw_arguments),
    )


class SubprocessChromeLauncher:
    """Minimal configurable launcher for a local persistent Chrome process."""

    def launch(self, request: ChromeLaunchRequest) -> ChromeLaunchResult:
        parsed = urlsplit(request.cdp_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ChromeLaunchError(
                "the built-in launcher supports only an explicit local CDP URL"
            )
        if parsed.port is None:
            raise ChromeLaunchError("CDP URL must include a remote-debugging port")
        if request.chrome_binary is None or request.user_data_dir is None:
            raise ChromeLaunchError(
                "the built-in launcher needs chrome_binary and user_data_dir"
            )
        if not request.chrome_binary.is_file():
            raise ChromeLaunchError(
                f"Chrome binary does not exist: {request.chrome_binary}"
            )
        request.user_data_dir.mkdir(parents=True, exist_ok=True)
        command = [
            str(request.chrome_binary),
            f"--remote-debugging-port={parsed.port}",
            f"--user-data-dir={request.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            *request.extra_arguments,
        ]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **popen_session_kwargs(persistent=True),
            )
        except OSError as exc:
            raise ChromeLaunchError(f"could not launch Chrome: {exc}") from exc
        # Do not retain a lifecycle operation for this process.  It belongs to
        # the persistent browser environment, not to the measurement worker.
        return ChromeLaunchResult(
            process_id=process.pid,
            detail="persistent Chrome launch requested",
        )


class CommandChromeLauncher:
    """Adapter for an explicitly configured launcher command."""

    def __init__(self, command: Sequence[str]):
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise ValueError("launcher command must contain non-empty text")
        self.command = tuple(command)

    def launch(self, request: ChromeLaunchRequest) -> ChromeLaunchResult:
        environment = dict(os.environ)
        environment.update({
            "VISIBILITY_RUNNER_CDP_URL": request.cdp_url,
            "VISIBILITY_RUNNER_CHROME_BINARY": (
                str(request.chrome_binary)
                if request.chrome_binary is not None
                else ""
            ),
            "VISIBILITY_RUNNER_CHROME_USER_DATA_DIR": (
                str(request.user_data_dir)
                if request.user_data_dir is not None
                else ""
            ),
        })
        try:
            process = subprocess.Popen(
                self.command,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **popen_session_kwargs(persistent=True),
            )
        except OSError as exc:
            raise ChromeLaunchError(
                f"configured Chrome launcher failed: {exc}"
            ) from exc
        return ChromeLaunchResult(
            process_id=process.pid,
            detail="configured persistent Chrome launcher started",
        )


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


def _coerce_health(value: object, *, cdp_url: str) -> CDPHealthSnapshot:
    if isinstance(value, CDPHealthSnapshot):
        return value
    if isinstance(value, Mapping):
        return CDPHealthSnapshot(
            checked_at=str(value.get("checked_at") or ""),
            endpoint=str(value.get("endpoint") or cdp_url.rstrip("/")),
            reachable=bool(value.get("reachable")),
            browser=(
                str(value["browser"]) if value.get("browser") is not None else None
            ),
            protocol_version=(
                str(value["protocol_version"])
                if value.get("protocol_version") is not None
                else None
            ),
            browser_session_id=(
                str(value["browser_session_id"])
                if value.get("browser_session_id") is not None
                else None
            ),
            error=str(value["error"]) if value.get("error") is not None else None,
        )
    raise ChromeRuntimeError("CDP health probe returned an unsupported value")


class ChromeController:
    """Apply an explicit launch policy and wait for a readable CDP endpoint."""

    def __init__(
        self,
        *,
        launcher: Optional[ChromeLauncher] = None,
        health_probe: object = probe_cdp_health,
        poll_seconds: float = 0.2,
    ):
        self.launcher = launcher
        self.health_probe = health_probe
        self.poll_seconds = max(0.01, float(poll_seconds))

    async def _health(
        self,
        cdp_url: str,
        timeout_seconds: float,
    ) -> CDPHealthSnapshot:
        probe = self.health_probe
        if probe is probe_cdp_health:
            value = await asyncio.to_thread(
                probe_cdp_health,
                cdp_url,
                timeout_seconds=timeout_seconds,
            )
        else:
            try:
                value = probe(cdp_url, timeout_seconds=timeout_seconds)
            except TypeError:
                value = probe(cdp_url)
            value = await _maybe_await(value)
        return _coerce_health(value, cdp_url=cdp_url)

    async def health(
        self,
        cdp_url: str,
        *,
        timeout_seconds: float = 0.75,
    ) -> CDPHealthSnapshot:
        """Expose the same injectable read-only probe used by preflight."""

        return await self._health(cdp_url, timeout_seconds)

    async def ensure_available(
        self,
        config: "BrowserConfig | object",
        *,
        startup_timeout_seconds: float = 20.0,
        health_timeout_seconds: float = 0.75,
    ) -> ChromeAvailability:
        cdp_url = _value(config, ("cdp_url",))
        if not isinstance(cdp_url, str) or not cdp_url:
            raise ChromeRuntimeError("BrowserConfig.cdp_url must be explicit")
        policy = _exact_policy(_value(config, ("launch_policy",), "attach_only"))
        before = await self._health(cdp_url, health_timeout_seconds)
        if before.reachable:
            return ChromeAvailability(health=before, launched=False)
        if _value(config, ("background_only",), True):
            raise ChromeAttachUnavailable(
                f"Background mode requires prepared Chrome at {cdp_url}; "
                "prepare the selected profiles before starting measurement"
            )
        if policy == "attach_only":
            raise ChromeAttachUnavailable(
                f"CDP endpoint {cdp_url} is unreachable under attach-only policy"
            )

        launcher = self.launcher
        if launcher is None:
            launcher_path = _value(config, ("launcher_path",), None)
            launcher = (
                CommandChromeLauncher((str(launcher_path),))
                if launcher_path is not None
                else SubprocessChromeLauncher()
            )
        request = build_launch_request(config)
        try:
            raw_result = await _maybe_await(launcher.launch(request))
        except ChromeRuntimeError:
            raise
        except Exception as exc:
            raise ChromeLaunchError(
                f"configured Chrome launcher raised {type(exc).__name__}: {exc}"
            ) from exc
        launch_result = (
            raw_result
            if isinstance(raw_result, ChromeLaunchResult)
            else ChromeLaunchResult(detail=str(raw_result or "launcher acknowledged"))
        )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(startup_timeout_seconds))
        last = before
        while True:
            last = await self._health(cdp_url, health_timeout_seconds)
            if last.reachable:
                return ChromeAvailability(
                    health=last,
                    launched=True,
                    launch_result=launch_result,
                )
            if loop.time() >= deadline:
                raise ChromeLaunchError(
                    f"Chrome launch did not expose CDP at {cdp_url}: "
                    f"{last.error or 'unreachable'}"
                )
            await asyncio.sleep(self.poll_seconds)


async def ensure_chrome_available(
    config: "BrowserConfig | object",
    *,
    launcher: Optional[ChromeLauncher] = None,
    startup_timeout_seconds: float = 20.0,
    health_timeout_seconds: float = 0.75,
) -> ChromeAvailability:
    return await ChromeController(launcher=launcher).ensure_available(
        config,
        startup_timeout_seconds=startup_timeout_seconds,
        health_timeout_seconds=health_timeout_seconds,
    )


__all__ = [
    "ChromeAttachUnavailable",
    "ChromeAvailability",
    "ChromeController",
    "ChromeLaunchError",
    "ChromeLaunchRequest",
    "ChromeLaunchResult",
    "ChromeLauncher",
    "ChromeRuntimeError",
    "CommandChromeLauncher",
    "SubprocessChromeLauncher",
    "build_launch_request",
    "ensure_chrome_available",
]
