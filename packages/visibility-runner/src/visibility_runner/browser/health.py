"""Read-only CDP health and persistent-browser session change detection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
import json
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit
import urllib.request


class BrowserTransportScope(str, Enum):
    PAGE_TARGET_CLOSED = "page_target_closed"
    PAGE_OR_CONTEXT_ERROR = "page_or_context_error"
    SHARED_BROWSER_SESSION_REPLACED = "shared_browser_session_replaced"
    WORKER_CLIENT_DISCONNECTED = (
        "worker_cdp_client_disconnected_shared_browser_reachable"
    )
    SHARED_CDP_UNREACHABLE = "shared_cdp_endpoint_unreachable"


class CDPSessionChangedError(RuntimeError):
    """The persistent Chrome endpoint now represents a different session."""


@dataclass(frozen=True)
class CDPHealthSnapshot:
    checked_at: str
    endpoint: str
    reachable: bool
    browser: Optional[str]
    protocol_version: Optional[str]
    browser_session_id: Optional[str]
    error: Optional[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    # Small mapping conveniences preserve compatibility with existing
    # diagnostic callers without making the typed contract mutable.
    def __getitem__(self, key: str) -> object:
        return self.to_dict()[key]

    def get(self, key: str, default: object = None) -> object:
        return self.to_dict().get(key, default)


def probe_cdp_health(
    cdp_url: str,
    *,
    timeout_seconds: float = 0.75,
) -> CDPHealthSnapshot:
    """Fingerprint one CDP browser process without changing browser state."""

    if not isinstance(cdp_url, str) or not cdp_url:
        raise ValueError("cdp_url must be explicit non-empty text")
    endpoint = cdp_url.rstrip("/")
    checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        with urllib.request.urlopen(
            endpoint + "/json/version",
            timeout=max(0.05, float(timeout_seconds)),
        ) as response:
            payload = json.load(response)
        if not isinstance(payload, Mapping):
            raise ValueError("CDP version endpoint returned a non-object payload")
        websocket_url = payload.get("webSocketDebuggerUrl")
        websocket_path = (
            urlsplit(websocket_url).path.rstrip("/")
            if isinstance(websocket_url, str) and websocket_url
            else ""
        )
        session_id = (
            websocket_path.rsplit("/", 1)[-1] if websocket_path else None
        )
        browser = payload.get("Browser")
        protocol = payload.get("Protocol-Version")
        return CDPHealthSnapshot(
            checked_at=checked_at,
            endpoint=endpoint,
            reachable=True,
            browser=str(browser) if browser else None,
            protocol_version=str(protocol) if protocol else None,
            browser_session_id=session_id,
            error=None,
        )
    except Exception as exc:
        return CDPHealthSnapshot(
            checked_at=checked_at,
            endpoint=endpoint,
            reachable=False,
            browser=None,
            protocol_version=None,
            browser_session_id=None,
            error=f"{type(exc).__name__}: {exc}",
        )


def cdp_endpoint_health(
    cdp_url: str,
    *,
    timeout_seconds: float = 0.75,
) -> dict[str, object]:
    """Compatibility mapping form of :func:`probe_cdp_health`."""

    return probe_cdp_health(
        cdp_url,
        timeout_seconds=timeout_seconds,
    ).to_dict()


def _health_value(value: object, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def cdp_session_changed(before: object, after: object) -> Optional[bool]:
    """Compare health fingerprints, returning ``None`` when unknowable."""

    first = _health_value(before, "browser_session_id")
    second = _health_value(after, "browser_session_id")
    if not isinstance(first, str) or not first:
        return None
    if not isinstance(second, str) or not second:
        return None
    return first != second


def require_same_cdp_session(before: object, after: object) -> None:
    changed = cdp_session_changed(before, after)
    if changed is True:
        raise CDPSessionChangedError(
            "persistent Chrome CDP browser session changed during the operation"
        )


@dataclass(frozen=True)
class CDPSessionGuard:
    """Frozen pre-operation health fingerprint for later verification."""

    before: CDPHealthSnapshot

    @classmethod
    def capture(
        cls,
        cdp_url: str,
        *,
        timeout_seconds: float = 0.75,
    ) -> "CDPSessionGuard":
        return cls(
            before=probe_cdp_health(
                cdp_url,
                timeout_seconds=timeout_seconds,
            )
        )

    def check(
        self,
        *,
        timeout_seconds: float = 0.75,
    ) -> CDPHealthSnapshot:
        after = probe_cdp_health(
            self.before.endpoint,
            timeout_seconds=timeout_seconds,
        )
        require_same_cdp_session(self.before, after)
        return after


def classify_browser_transport(
    *,
    client_connected: bool,
    page_closed: bool,
    before: object = None,
    after: object = None,
) -> str:
    """Conservatively scope a Playwright target/transport failure."""

    if client_connected:
        return (
            BrowserTransportScope.PAGE_TARGET_CLOSED.value
            if page_closed
            else BrowserTransportScope.PAGE_OR_CONTEXT_ERROR.value
        )
    if bool(_health_value(after, "reachable")):
        if cdp_session_changed(before, after) is True:
            return BrowserTransportScope.SHARED_BROWSER_SESSION_REPLACED.value
        return BrowserTransportScope.WORKER_CLIENT_DISCONNECTED.value
    return BrowserTransportScope.SHARED_CDP_UNREACHABLE.value


__all__ = [
    "BrowserTransportScope",
    "CDPHealthSnapshot",
    "CDPSessionChangedError",
    "CDPSessionGuard",
    "cdp_endpoint_health",
    "cdp_session_changed",
    "classify_browser_transport",
    "probe_cdp_health",
    "require_same_cdp_session",
]
