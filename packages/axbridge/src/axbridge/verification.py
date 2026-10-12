"""Bounded Copilot checkbox assistance for a serialized desktop action queue.

This module never selects or navigates a window. The browser owner must select
its exact owned tab and retain the desktop lease for the entire call. Only that
tab's explicit process PID may be activated. Construction is display independent;
native dependencies are loaded only
when an attempt creates its reader or dispatches mouse input.
"""

from dataclasses import dataclass
import math
import re
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from .models import BridgeError, Node, Snapshot
from .mouse import HumanMouse
from .mouse.models import Cancelled, TargetChanged, TimingOverrun
from .reader import MacReader, _ValidationGate


_LABELS = frozenset({"verify you are human", "verify that you are human"})
_CONTEXTS = frozenset({
    "widget containing a cloudflare security challenge", "security challenge",
    "security verification", "human verification", "verification required",
    "verify you are human", "verify that you are human",
})
_CONTEXT_ROLES = frozenset({"AXGroup", "AXDialog", "AXWebArea"})
_UNCHECKED = frozenset({"0", "false", "unchecked", "off"})
_COPILOT_HOSTS = frozenset({"copilot.com", "copilot.microsoft.com"})


class VerificationCleanupError(BridgeError):
    """The queue must retain its desktop lease until owned input is released."""

    def __init__(self, message, *, result):
        super().__init__(message)
        self.result = result


def _normal(value):
    return " ".join(str("" if value is None else value).split()).casefold()


def _copilot_page_url(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in _COPILOT_HOSTS
                and parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except (ValueError, TypeError, AttributeError):
        return False


def _page_reference(url):
    """Normalize tracking parameters only on known Copilot conversation paths."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme == "https" and parsed.hostname in _COPILOT_HOSTS
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and re.fullmatch(r"/(?:chats|chat/conversation)/[A-Za-z0-9_-]{1,200}", parsed.path)):
            return urlunsplit(("https", parsed.hostname, parsed.path, "", ""))
    except (ValueError, TypeError, AttributeError):
        pass
    return url


def _activate_pid(pid):
    from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is None or not app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps):
        raise BridgeError("The exact requested Chrome process could not be activated.")


@dataclass(frozen=True)
class VerificationDetection:
    status: str
    reason: str
    checkbox: Node | None = None
    context: Node | None = None


def detect_verification(snapshot: Snapshot) -> VerificationDetection:
    """Recognize only an exposed, unchecked checkbox in a known AX context.

    Text mentions, generic buttons, incomplete snapshots, duplicate candidates,
    disabled controls and unknown checkbox state cannot authorize a click.
    """
    if snapshot.truncated or snapshot.warnings:
        return VerificationDetection("unsupported", "Accessibility snapshot is incomplete.")
    by_id = {node.id: node for node in snapshot.nodes}
    if len(by_id) != len(snapshot.nodes):
        return VerificationDetection("unsupported", "Accessibility node identities are ambiguous.")
    contexts = {node.id: node for node in snapshot.nodes
                if node.role in _CONTEXT_ROLES and _normal(node.name) in _CONTEXTS
                and node.bounds is not None}
    matches = []
    for node in snapshot.nodes:
        if node.role != "AXCheckBox" or _normal(node.name) not in _LABELS:
            continue
        parent_id, seen = node.parent_id, {node.id}
        while parent_id is not None and parent_id not in seen:
            seen.add(parent_id)
            if parent_id in contexts:
                matches.append((node, contexts[parent_id]))
                break
            parent = by_id.get(parent_id)
            parent_id = parent.parent_id if parent is not None else None
    if len(matches) > 1:
        return VerificationDetection("unsupported", "More than one verification checkbox is exposed.")
    if not matches:
        if contexts:
            return VerificationDetection("unsupported", "The challenge has no supported accessible checkbox.")
        return VerificationDetection("clear", "No known verification context is exposed.")
    node, context = matches[0]
    if node.enabled is not True or node.bounds is None:
        return VerificationDetection("unsupported", "Verification checkbox is hidden or disabled.")
    if _normal(node.value) not in _UNCHECKED:
        return VerificationDetection("unsupported", "Verification checkbox is checked or its state is unknown.")
    return VerificationDetection("ready", "Known unchecked verification checkbox found.", node, context)


class _AttemptGate:
    def __init__(self, deadline, cancel, foreground_check):
        self.deadline, self.cancel, self.foreground_check = deadline, cancel, foreground_check

    def cancelled(self):
        if self.cancel is None:
            return False
        return bool(self.cancel.is_set() if hasattr(self.cancel, "is_set") else self.cancel())

    def check(self, deadline=None):
        if self.cancelled():
            raise Cancelled("Verification action was cancelled.")
        limit = min(self.deadline, deadline) if deadline is not None else self.deadline
        remaining = limit - time.monotonic()
        if remaining <= 0:
            raise TimingOverrun("Verification action deadline expired.")
        return remaining

    def focused(self, deadline=None):
        self.check(deadline)
        limit = min(self.deadline, deadline) if deadline is not None else self.deadline
        if self.foreground_check(deadline=limit) is not True:
            raise TargetChanged("The exact owned browser tab is no longer in the foreground.")
        self.check(deadline)

    def stop_mouse(self):
        return self.cancelled() or time.monotonic() >= self.deadline


class _CheckboxGuard:
    """Revalidate native identity, challenge ancestry and unchecked state."""

    def __init__(self, reader, detection, gate):
        self.reader, self.gate = reader, gate
        self.native = reader.bind_target(detection.checkbox)
        self.target = reader._reference(detection.checkbox)
        self.context = reader._reference(detection.context)
        self.context_role, self.context_name = detection.context.role, detection.context.name

    def validate(self, point, *, deadline):
        deadline = min(deadline, self.gate.deadline)
        self.gate.focused(deadline)
        native = _ValidationGate(self.reader, deadline)
        try:
            if not native.ancestors_contain(self.target, self.context):
                raise TargetChanged("The checkbox left its verification context.")
            if native.read(self.context, "AXRole", required=True) != self.context_role:
                raise TargetChanged("The verification context changed.")
            name = native.read(self.context, "AXTitle", "") or native.read(self.context, "AXDescription", "")
            if name != self.context_name or native.read(self.context, "AXHidden", False):
                raise TargetChanged("The verification context changed or became hidden.")
            if _normal(native.read(self.target, "AXValue", required=True)) not in _UNCHECKED:
                raise TargetChanged("The verification checkbox is no longer unchecked.")
        finally:
            native.close()
        self.gate.check(deadline)
        self.native.validate(point, deadline=deadline)
        self.gate.check(deadline)


class CopilotVerificationAdapter:
    """One reusable mouse session; at most one checkbox press per invocation.

    ``foreground_check(*, deadline) -> bool`` must synchronously verify the exact
    owned CDP target is focused and visible, with its expected URL/profile/context.
    The caller must bound that check by the supplied monotonic deadline. A native
    title or URL alone is insufficient because parallel tabs can share both.

    An external queue must serialize all desktop users, including other process
    runs. This adapter's lock only prevents accidental concurrent use of itself.
    """

    def __init__(self, *, mouse=None, reader_factory=None, activate_pid=None):
        self.mouse = mouse if mouse is not None else HumanMouse()
        self.reader_factory = reader_factory if reader_factory is not None else MacReader
        self.activate_pid = activate_pid if activate_pid is not None else _activate_pid
        self._lock = threading.Lock()

    def attempt(self, *, expected_pid: int, expected_url: str,
                foreground_check, deadline: float, expected_window_title: str | None = None,
                cancel=None, page_url_check=None) -> dict:
        started = time.monotonic()
        result = {"status": "unsupported", "reason": "", "click_attempted": False,
                  "motion": None, "elapsed_seconds": 0.0}
        if not self._lock.acquire(blocking=False):
            result["reason"] = "Another verification action is using this adapter."
            return result
        mouse_started = False
        previous_motion = None
        try:
            if type(expected_pid) is not int or expected_pid <= 0:
                raise BridgeError("An explicit positive Chrome PID is required.")
            if not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
                raise BridgeError("A finite monotonic action deadline is required.")
            if not callable(foreground_check):
                raise BridgeError("An exact owned-tab foreground check is required.")
            if page_url_check is not None and not callable(page_url_check):
                raise BridgeError("The owned-page URL check must be callable.")
            if cancel is not None and not callable(cancel) and not callable(getattr(cancel, "is_set", None)):
                raise BridgeError("Cancellation requires a callable or an event with is_set().")
            try:
                parsed = urlsplit(expected_url)
                supported = (parsed.scheme == "https" and parsed.hostname in _COPILOT_HOSTS
                             and parsed.port in (None, 443) and not parsed.username and not parsed.password)
            except (ValueError, TypeError, AttributeError):
                supported = False
            if not supported:
                raise BridgeError("An exact HTTPS Copilot page URL is required.")
            gate = _AttemptGate(deadline, cancel, foreground_check)
            reader = self.reader_factory(pid=expected_pid, timeout=min(8.0, gate.check()))
            gate.check()  # Native dependency startup may have consumed the budget.
            self.activate_pid(expected_pid)
            gate.focused()

            def observe():
                gate.focused()
                snapshot = reader.snapshot(timeout=min(8.0, gate.check()))
                gate.check()
                page_matches = _copilot_page_url(snapshot.url) and (
                    _page_reference(snapshot.url) == _page_reference(expected_url)
                    if page_url_check is None else page_url_check(snapshot.url, deadline=deadline) is True)
                gate.check()
                if snapshot.app_pid != expected_pid or not page_matches:
                    raise TargetChanged("The accessibility page does not match the owned browser target.")
                if expected_window_title is not None and snapshot.window_title != expected_window_title:
                    raise TargetChanged("The accessibility window title changed.")
                if not reader.is_frontmost():
                    raise TargetChanged("The owned Chrome window is not in the foreground.")
                gate.focused()
                return detect_verification(snapshot)

            detection = observe()
            if detection.status == "ready":
                # A fresh capture binds references only after the caller's focus
                # proof; no coordinates from the first observation survive.
                detection = observe()
            if detection.status != "ready":
                result.update(status="already_clear" if detection.status == "clear" else "unsupported",
                              reason=detection.reason)
                return result
            guard = _CheckboxGuard(reader, detection, gate)
            gate.check()
            mouse_started = True
            previous_motion = getattr(self.mouse, "last_result", None)
            motion = self.mouse.click(detection.checkbox.bounds, button="left", count=1,
                                      guard=guard, cancel=gate.stop_mouse)
            if motion.cleanup_succeeded is False:
                raise VerificationCleanupError("Verification mouse-button cleanup is unconfirmed; quarantine the desktop queue.", result=motion)
            result.update(status="clicked", reason="One verification checkbox click was dispatched; page clearance must be confirmed by the caller.",
                          click_attempted=True, motion=motion.to_dict())
            return result
        except Exception as error:
            motion = getattr(error, "result", None)
            if motion is None and mouse_started:
                current_motion = getattr(self.mouse, "last_result", None)
                if current_motion is not previous_motion:
                    motion = current_motion
            if motion is not None and getattr(motion, "cleanup_succeeded", None) is False:
                if isinstance(error, VerificationCleanupError):
                    raise
                raise VerificationCleanupError("Verification mouse-button cleanup failed; quarantine the desktop queue.", result=motion) from error
            attempted = bool(motion is not None and getattr(motion, "down_attempted", 0))
            result.update(status="clicked" if attempted else "unsupported",
                          reason=f"{type(error).__name__}: {error}", click_attempted=attempted,
                          motion=motion.to_dict() if motion is not None else None)
            return result
        finally:
            result["elapsed_seconds"] = round(time.monotonic() - started, 6)
            self._lock.release()
