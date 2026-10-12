"""Attach Playwright to saved Octo profiles without taking over their lifecycle.

Local API contract: https://github.com/octobrowser/documentation/blob/main/api/local-client.md
The desktop app owns persistence. Neither CDP disconnect nor disappearance from
the active list proves that Octo finished its cloud synchronization.
"""

from dataclasses import dataclass, field
import ipaddress
import json
import math
import re
import time
import os
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from automation_core.storage.models import BudgetStopped, NeedsAttention, check_budget, timeout_seconds


_MAX_RESPONSE = 256 * 1024
_STOP_SECONDS = 10.0
_START_SERVER_SECONDS = 60
_START_HTTP_SECONDS = 65.0
_UUID = re.compile(r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\Z")


class OctoProfileNotFound(NeedsAttention):
    """An explicitly rejected UUID, identified only by Octo's known code."""

    code = "profiles.not_found"

    def __init__(self):
        super().__init__("The requested Octo profile UUID was not found; no retry or stop was attempted.")


class OctoDebugEndpointUnavailable(NeedsAttention):
    code = "profiles.debug_endpoint_unavailable"

    def __init__(self):
        super().__init__("This running Octo profile has no debugging endpoint; no start, stop or retry was attempted.")


def normalize_profile_id(value: str) -> str:
    """Use one canonical key for both Octo UUID spellings and local locks."""
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise NeedsAttention("Octo profile ID must be a complete hexadecimal UUID.")
    return value.replace("-", "").lower()


def _validated_timeout(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NeedsAttention("Octo timeout must be between zero and 120 seconds.")
    try:
        valid = math.isfinite(value) and 0 < value <= 120
    except OverflowError:
        valid = False
    if not valid:
        raise NeedsAttention("Octo timeout must be between zero and 120 seconds.")
    return float(value)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _loopback_url(value: str, *, websocket: bool = False) -> str:
    """Reject remote endpoints, URL credentials, odd encodings, and redirects."""
    if not isinstance(value, str) or not value or any(ord(c) <= 32 for c in value):
        raise NeedsAttention("Octo supplied an invalid local connection address.")
    try:
        parts = urlsplit(value)
        schemes = {"ws", "wss"} if websocket else {"http", "https"}
        if (parts.scheme not in schemes or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment
                or not parts.hostname or parts.port is None):
            raise ValueError
        host = parts.hostname
        if host == "localhost":
            host = "127.0.0.1"  # No DNS resolution or proxy for local control.
        address = ipaddress.ip_address(host)
        if not address.is_loopback or not 1 <= parts.port <= 65535:
            raise ValueError
        if websocket:
            if not re.fullmatch(r"/devtools/browser/[A-Za-z0-9_-]+", parts.path):
                raise ValueError
        elif parts.path not in ("", "/"):
            raise ValueError
        authority = f"[{address}]:{parts.port}" if address.version == 6 else f"{address}:{parts.port}"
        return urlunsplit((parts.scheme, authority, parts.path if websocket else "", "", ""))
    except (ValueError, TypeError):
        raise NeedsAttention("Octo connections must use a valid loopback address without credentials.") from None


def _port(value) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise NeedsAttention("Octo returned an invalid debugging port.")
    if isinstance(value, str) and not re.fullmatch(r"[0-9]{1,5}", value):
        raise NeedsAttention("Octo returned an invalid debugging port.")
    port = int(value)
    if not 1 <= port <= 65535:
        raise NeedsAttention("Octo returned an invalid debugging port.")
    return port


def _profile(value) -> dict:
    if not isinstance(value, dict):
        raise NeedsAttention("Octo returned an invalid profile record.")
    profile_id = normalize_profile_id(value.get("uuid"))
    if type(value.get("one_time")) is not bool:
        raise NeedsAttention("Octo did not confirm whether the profile is saved.")
    state = value.get("state")
    if not isinstance(state, str) or not re.fullmatch(r"[A-Z_]{1,32}", state):
        raise NeedsAttention("Octo returned an invalid profile state.")
    result = {"uuid": profile_id, "one_time": value["one_time"], "state": state}
    if "headless" in value:
        if type(value["headless"]) is not bool:
            raise NeedsAttention("Octo returned an invalid profile mode.")
        result["headless"] = value["headless"]
    result["debug_port"] = _port(value.get("debug_port"))
    endpoint = value.get("ws_endpoint")
    if endpoint not in (None, ""):
        endpoint = _loopback_url(endpoint, websocket=True)
        endpoint_port = urlsplit(endpoint).port
        if result["debug_port"] is not None and endpoint_port != result["debug_port"]:
            raise NeedsAttention("Octo returned conflicting connection addresses.")
        result["debug_port"] = endpoint_port
    result["ws_endpoint"] = endpoint or None
    for name in ("start_time", "browser_pid"):
        if name in value:
            number = value[name]
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number < 0:
                raise NeedsAttention("Octo returned invalid profile identity metadata.")
            result[name] = number
    return result


@dataclass(repr=False)
class OctoSession:
    context: object
    started_by_us: bool
    profile_id: str
    _client: "OctoClient" = field(repr=False)
    _browser: object = field(repr=False)
    _profile: dict = field(repr=False)
    _stop_result: str | None = field(default=None, repr=False)
    _owner_pid: int = field(default_factory=os.getpid, repr=False)
    _owner_thread: int = field(default_factory=threading.get_ident, repr=False)

    def assert_owned(self) -> None:
        if self._owner_pid != os.getpid() or self._owner_thread != threading.get_ident():
            raise BudgetStopped("ownership_lost")

    def release(self, stop: bool = False, *, budget=None, effects=None) -> str:
        """Never close the browser/context; optionally stop only our own launch.

        Exiting the caller's Playwright driver disconnects CDP. A failed stop is
        intentionally not retried; the caller must review Octo's visible state.
        """
        self.assert_owned()
        if self._stop_result is not None:
            return self._stop_result
        if not stop:
            return "left_open"
        if not self.started_by_us:
            return "left_open_borrowed"
        self._stop_result = "stop_unconfirmed"
        deadline = time.monotonic() + _STOP_SECONDS
        saving_intent = False
        try:
            active = self._client.active(timeout=timeout_seconds(budget, min(self._client.timeout, _STOP_SECONDS)))
            match = next((item for item in active if item["uuid"] == self.profile_id), None)
            if match is None:
                self._stop_result = "stopped_sync_unverified"
                return self._stop_result
            # A manually restarted instance of the same profile is borrowed.
            if (match["one_time"]
                    or any(self._profile.get(key) is None or match.get(key) is None
                           for key in ("start_time", "browser_pid"))
                    or any(
                match.get(key) != self._profile.get(key)
                for key in ("debug_port", "start_time", "browser_pid")
            )):
                return self._stop_result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._stop_result
            request_timeout = timeout_seconds(budget, remaining)
            if effects is not None:
                saving_intent = True
                effects("stop_pending", self._profile)
                saving_intent = False
                request_timeout = timeout_seconds(budget, min(request_timeout, deadline - time.monotonic()))
            response = self._client._request(
                "/api/profiles/stop", {"uuid": self.profile_id}, timeout=request_timeout
            )
            if not isinstance(response, dict) or set(response) != {"msg"} or not isinstance(response["msg"], str):
                return self._stop_result
            for _ in range(40):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                active = self._client.active(timeout=timeout_seconds(budget, remaining))
                if not any(item["uuid"] == self.profile_id for item in active):
                    self._stop_result = "stopped_sync_unverified"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(timeout_seconds(budget, min(0.25, remaining)))
        except NeedsAttention:
            if saving_intent:
                raise
        if effects is not None:
            effects("stop_observed" if self._stop_result == "stopped_sync_unverified" else "stop_uncertain", self._profile)
        return self._stop_result


class OctoClient:
    def __init__(self, base_url: str = "http://127.0.0.1:58888", timeout: float = 15.0):
        self.base_url = _loopback_url(base_url)
        self.timeout = _validated_timeout(timeout)
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        self.started_profile_ids: set[str] = set()
        self._uncertain_profiles: set[str] = set()
        self._debug_reconnect_attempted: set[str] = set()

    def _request(self, path: str, body: dict | None = None, *, timeout: float | None = None, origin: str | None = None,
                 budget=None):
        request_timeout = timeout_seconds(budget, self.timeout if timeout is None else _validated_timeout(timeout))
        request = Request(
            (origin or self.base_url) + path,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="GET" if body is None else "POST",
        )
        try:
            with self._opener.open(request, timeout=request_timeout) as response:
                if response.status != 200:
                    raise NeedsAttention("Octo local API returned an unexpected status.")
                raw = response.read(_MAX_RESPONSE + 1)
                if len(raw) > _MAX_RESPONSE:
                    raise NeedsAttention("Octo local API response exceeded the size limit.")
                return json.loads(raw.decode("utf-8"))
        except HTTPError as error:
            status = error.code
            not_found = False
            try:
                # Only recognize the documented start rejection. Raw server
                # messages can contain proxy credentials and are never echoed.
                if path == "/api/profiles/start" and status in (400, 404):
                    raw = error.read(_MAX_RESPONSE + 1)
                    if len(raw) <= _MAX_RESPONSE:
                        value = json.loads(raw.decode("utf-8"))
                        not_found = isinstance(value, dict) and value.get("code") == OctoProfileNotFound.code
            except (OSError, ValueError, TypeError, RecursionError):
                pass
            finally:
                error.close()
            if not_found:
                raise OctoProfileNotFound() from None
            raise NeedsAttention(f"Octo local API request failed (HTTP {status}).") from None
        except (URLError, OSError, ValueError, RecursionError):
            raise NeedsAttention("Octo local API was unavailable or returned invalid JSON.") from None

    def active(self, *, timeout: float | None = None, budget=None) -> list[dict]:
        """Return validated local status only, dropping arbitrary response fields."""
        value = self._request("/api/profiles/active", timeout=timeout,
                              **({} if budget is None else {"budget": budget}))
        if not isinstance(value, list) or len(value) > 1000:
            raise NeedsAttention("Octo returned an invalid active-profile list.")
        result = [_profile(item) for item in value]
        if len({item["uuid"] for item in result}) != len(result):
            raise NeedsAttention("Octo returned duplicate profile identities.")
        check_budget(budget)
        return result

    def stop_for_route(self, profile, *, budget, before_stop):
        """One normal stop, called with a durable intent and the profile lease."""
        current = next((p for p in self.active(budget=budget) if p['uuid'] == profile['uuid']), None)
        if current != profile or profile['one_time'] or profile['state'] != 'STARTED':
            raise NeedsAttention('The selected profile instance changed before its route update.')
        before_stop()
        self._request('/api/profiles/stop', {'uuid': profile['uuid']}, budget=budget)
        deadline = time.monotonic() + min(_STOP_SECONDS, check_budget(budget))
        while time.monotonic() < deadline:
            if not any(p['uuid'] == profile['uuid'] for p in self.active(budget=budget)):
                return
            time.sleep(timeout_seconds(budget, min(.25, max(.001, deadline - time.monotonic()))))
        raise NeedsAttention('The profile stop is unconfirmed; no stop was repeated.')

    def _endpoint(self, profile: dict, *, budget=None) -> str:
        check_budget(budget)
        if profile["ws_endpoint"]:
            return profile["ws_endpoint"]
        if profile["debug_port"] is None:
            raise OctoDebugEndpointUnavailable()
        # Resolve HTTP discovery ourselves so redirects, proxies, and remote
        # webSocketDebuggerUrl values cannot redirect the CDP connection.
        origin = f"http://127.0.0.1:{profile['debug_port']}"
        kwargs = {} if budget is None else {"timeout": timeout_seconds(budget, self.timeout)}
        value = self._request("/json/version", origin=origin, **kwargs)
        if not isinstance(value, dict):
            raise NeedsAttention("Octo debugging discovery returned invalid metadata.")
        endpoint = _loopback_url(value.get("webSocketDebuggerUrl"), websocket=True)
        if urlsplit(endpoint).port != profile["debug_port"]:
            raise NeedsAttention("Octo debugging discovery returned a different browser port.")
        return endpoint

    def _stop_for_debug_reconnect(self, profile, *, budget, effects):
        """One explicitly admitted normal stop, with intent before transport.

        The application owns the account/profile lease, admission and durable
        one-stop allowance. This adapter preserves its supplied deadline and
        never changes profile settings or imports cookies. Local disappearance
        proves only the stop observation, not cloud synchronization.
        """
        profile_id = profile["uuid"]
        if (budget is None or not callable(effects) or profile["one_time"]
                or profile["state"] != "STARTED" or profile["debug_port"] is not None
                or profile["ws_endpoint"] is not None
                or any(profile.get(key) is None for key in ("start_time", "browser_pid"))
                or profile_id in self._debug_reconnect_attempted):
            raise NeedsAttention("The selected profile is not eligible for an admitted debugging reconnect.")
        check_budget(budget)
        current = next((item for item in self.active(budget=budget)
                        if item["uuid"] == profile_id), None)
        if current != profile:
            raise NeedsAttention("The selected profile instance changed before its debugging reconnect.")
        deadline = time.monotonic() + min(_STOP_SECONDS, check_budget(budget))
        # Persist first. The callback rechecks ownership and the saved allowance;
        # a denied or failed write cannot authorize a stop request.
        effects("stop_pending", profile)
        self._debug_reconnect_attempted.add(profile_id)
        self._uncertain_profiles.add(profile_id)
        remaining = deadline - time.monotonic()
        if remaining < .001:
            raise BudgetStopped()
        response = self._request("/api/profiles/stop", {"uuid": profile_id},
            timeout=timeout_seconds(budget, remaining))
        if (type(response) is not dict or set(response) != {"msg"}
                or not isinstance(response["msg"], str)):
            raise NeedsAttention("The normal debugging reconnect stop was not acknowledged; no retry was attempted.")
        for _ in range(40):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            active = self.active(timeout=timeout_seconds(budget, remaining), budget=budget)
            match = next((item for item in active if item["uuid"] == profile_id), None)
            if match is None:
                effects("stop_observed", profile)
                self._uncertain_profiles.discard(profile_id)
                return
            if any(match.get(key) != profile.get(key)
                   for key in ("start_time", "browser_pid", "debug_port")):
                raise NeedsAttention("The selected profile instance changed while its stop was pending.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(timeout_seconds(budget, min(.25, remaining)))
        raise NeedsAttention("The normal debugging reconnect stop remains unconfirmed; no restart was attempted.")

    def open_session(self, playwright, profile_id: str, *, budget=None,
                     allow_start: bool = True, effects=None,
                     allow_debug_reconnect: bool = False) -> OctoSession:
        profile_id = normalize_profile_id(profile_id)
        check_budget(budget)
        if profile_id in self._uncertain_profiles and allow_start:
            raise NeedsAttention("An earlier Octo open attempt is uncertain; review the profile before retrying.")
        profiles = self.active(**({} if budget is None else {"timeout": timeout_seconds(budget, self.timeout)}))
        profile = next((item for item in profiles if item["uuid"] == profile_id), None)
        started = False
        if (allow_debug_reconnect is True and profile is not None
                and profile["debug_port"] is None and profile["ws_endpoint"] is None):
            if allow_start is not True:
                raise NeedsAttention("An unresolved start prevents an admitted debugging reconnect.")
            self._stop_for_debug_reconnect(profile, budget=budget, effects=effects)
            profile = None
        if profile is None:
            if not allow_start:
                raise NeedsAttention("An earlier Octo open attempt is unresolved and the saved profile is not active; no new start was attempted.")
            request_timeout = timeout_seconds(budget, _START_HTTP_SECONDS)
            check_budget(budget)
            if effects is not None:
                effects("start_pending", None)
            request_timeout = timeout_seconds(budget, request_timeout)
            # Set before sending: a timeout may occur after Octo has started it.
            self._uncertain_profiles.add(profile_id)
            try:
                response = self._request("/api/profiles/start", {
                    "uuid": profile_id, "headless": False,
                    "debug_port": True, "only_local": True,
                    "timeout": _START_SERVER_SECONDS if budget is None else min(_START_SERVER_SECONDS, max(1, int(request_timeout))),
                }, timeout=request_timeout)
                if not isinstance(response, dict) or normalize_profile_id(response.get("uuid")) != profile_id:
                    raise NeedsAttention("Octo did not confirm the requested profile identity.")
                self.started_profile_ids.add(profile_id)
                started = True
                profile = _profile(response)
                if effects is not None:
                    effects("start_observed", profile)
            except BudgetStopped:
                raise
            except OctoProfileNotFound:
                # Keep the allowlisted diagnostic while preserving the same
                # no-retry guard as uncertain starts.
                raise
            except NeedsAttention:
                raise NeedsAttention("Octo start could not be confirmed safely. No retry or stop was attempted; review Octo before continuing.") from None
        try:
            check_budget(budget)
            if profile["one_time"]:
                raise NeedsAttention("A saved Octo profile is required; one-time profiles are not supported.")
            if profile["state"] != "STARTED":
                raise NeedsAttention("This Octo profile is transitioning; review its state before connecting.")
            endpoint = self._endpoint(profile, **({} if budget is None else {"budget": budget}))
            profile = {**profile, "ws_endpoint": endpoint}
            browser = playwright.chromium.connect_over_cdp(endpoint, timeout=timeout_seconds(budget, self.timeout) * 1000)
            contexts = browser.contexts
            if len(contexts) != 1:
                raise NeedsAttention("Octo must expose exactly one existing browser context.")
            check_budget(budget)
        except NeedsAttention:
            raise
        except Exception:
            raise NeedsAttention("Could not attach to the Octo browser. The profile was left running; no retry or stop was attempted.") from None
        self._uncertain_profiles.discard(profile_id)
        return OctoSession(contexts[0], started, profile_id, self, browser, profile)
