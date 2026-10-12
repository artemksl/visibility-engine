"""Resolve accessible controls, perform deliberate input, and verify results."""

from dataclasses import dataclass
from contextlib import contextmanager
import math
import time

from .models import BridgeError, Node, Snapshot


@dataclass(frozen=True)
class Selector:
    role: str
    name: str
    parent_name: str | None = None


_ROLE_ALIASES = {
    "button": {"AXButton"},
    "textbox": {"AXTextField", "AXTextArea"},
    "textfield": {"AXTextField"},
    "textarea": {"AXTextArea"},
}


@contextmanager
def preserve_motion_result(controller):
    """Keep evidence of input when subsequent reading or verification fails."""
    try:
        yield
    except BridgeError as error:
        if getattr(error, "result", None) is None:
            result = getattr(controller.mouse, "last_result", None)
            if result is not None:
                error.result = result
        raise


def resolve(snapshot: Snapshot, selector: Selector) -> Node:
    """Require exactly one exact-name match, optionally within an ancestor."""
    roles = _ROLE_ALIASES.get(selector.role.lower(), {selector.role})
    by_id = {node.id: node for node in snapshot.nodes}

    def in_parent(node: Node) -> bool:
        if selector.parent_name is None:
            return True
        seen = {node.id}
        parent_id = node.parent_id
        while parent_id is not None and parent_id not in seen:
            seen.add(parent_id)
            parent = by_id.get(parent_id)
            if parent is None:
                break
            if parent.name == selector.parent_name:
                return True
            parent_id = parent.parent_id
        return False

    matches = [
        node for node in snapshot.nodes
        if node.role in roles and node.name == selector.name and in_parent(node)
    ]
    description = f"{selector.role} named {selector.name!r}"
    if selector.parent_name is not None:
        description += f" within {selector.parent_name!r}"
    if not matches:
        raise BridgeError(f"No element matches {description}.")
    if len(matches) > 1:
        raise BridgeError(
            f"Ambiguous selector: {len(matches)} elements match {description}; "
            "choose a specific parent name."
        )
    if snapshot.truncated:
        raise BridgeError(
            "The accessibility snapshot was truncated; uniqueness cannot be "
            "verified. Increase the reader limits before acting."
        )
    return matches[0]


def _plan(node: Node, action: str) -> dict:
    if node.enabled is False:
        raise BridgeError(f"Element {node.name!r} is disabled.")
    if node.bounds is None or len(node.bounds) != 4:
        raise BridgeError(f"Element {node.name!r} has no usable screen bounds.")
    try:
        x, y, width, height = node.bounds
        valid = all(math.isfinite(value) for value in node.bounds)
        valid = valid and width > 0 and height > 0
        center = (x + width / 2, y + height / 2)
        valid = valid and all(math.isfinite(value) for value in center)
    except (TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        raise BridgeError(f"Element {node.name!r} has invalid screen bounds.")
    return {
        "action": action,
        "target": node.name,
        "role": node.role,
        "id": node.id,
        "target_bounds": list(node.bounds),
        "behavior": "human_mouse",
        "executed": False,
    }


class Controller:
    """Keep desktop dependencies lazy; fake readers and input drivers are supported."""

    def __init__(self, reader, gui=None, mouse=None):
        self.reader = reader
        self.gui = gui
        self.mouse = mouse

    def _mouse(self):
        if self.mouse is None:
            from .mouse import HumanMouse
            self.mouse = HumanMouse()
        return self.mouse

    def _driver(self):
        if self.gui is None:
            try:
                import pyautogui
            except ImportError as exc:
                raise BridgeError(
                    "PyAutoGUI is unavailable. Install the project's dependencies first."
                ) from exc
            self.gui = pyautogui
        self.gui.FAILSAFE = True
        return self.gui

    def _input(self, function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception as exc:
            fail_safe = getattr(self.gui, "FailSafeException", None)
            if isinstance(fail_safe, type) and isinstance(exc, fail_safe):
                raise BridgeError(
                    "PyAutoGUI fail-safe stopped the action. Move the pointer "
                    "away from a screen corner before trying again."
                ) from exc
            raise

    def _require_frontmost(self):
        if not self.reader.is_frontmost():
            raise BridgeError("The target application is not in the foreground; input was stopped.")

    @staticmethod
    def _finish(plan, result):
        plan.update(executed=True, motion=result.to_dict())
        if result.click_point is not None:
            plan["coordinates"] = list(result.click_point.as_tuple())
        return plan

    def _prepare(self, selector, action, execute):
        node = resolve(self.reader.snapshot(), selector)
        plan = _plan(node, action)
        if not execute:
            return node, plan
        self.reader.activate()
        # Activation can move windows, navigate pages, or change accessibility IDs.
        node = resolve(self.reader.snapshot(), selector)
        plan = _plan(node, action)
        self._require_frontmost()
        return node, plan

    def click(self, selector: Selector, execute: bool = False, *,
              button: str = "left", count: int = 1) -> dict:
        if button not in ("left", "right", "middle") or type(count) is not int or count not in (1, 2):
            raise BridgeError("Click requires left/right/middle and a count of 1 or 2.")
        node, plan = self._prepare(selector, "click", execute)
        plan.update(button=button, count=count)
        if not execute:
            return plan
        guard = self.reader.bind_target(node)
        result = self._mouse().click(node.bounds, button=button, count=count, guard=guard)
        return self._finish(plan, result)

    def move(self, selector: Selector, execute: bool = False) -> dict:
        node, plan = self._prepare(selector, "move", execute)
        if not execute:
            return plan
        return self._finish(plan, self._mouse().move_to(node.bounds))

    def drag(self, source: Selector, destination: Selector, execute: bool = False, *,
             button: str = "left") -> dict:
        if button not in ("left", "right", "middle"):
            raise BridgeError("Drag button must be left, right, or middle.")
        def resolve_pair():
            observed = self.reader.snapshot()
            first, last = resolve(observed, source), resolve(observed, destination)
            plan = _plan(first, "drag")
            plan.update(destination=_plan(last, "move"), button=button)
            return first, last, plan
        first, last, plan = resolve_pair()
        if not execute:
            return plan
        self.reader.activate()
        first, last, plan = resolve_pair()
        self._require_frontmost()
        source_guard = self.reader.bind_target(first)
        destination_guard = self.reader.bind_target(last)
        result = self._mouse().drag(first.bounds, last.bounds, button=button,
                                    source_guard=source_guard, destination_guard=destination_guard)
        return self._finish(plan, result)

    def scroll(self, notches: int, over: Selector | None = None, execute: bool = False) -> dict:
        if type(notches) is not int:
            raise BridgeError("Scroll notches must be an integer.")
        node = None
        if over is not None:
            node, plan = self._prepare(over, "scroll", execute)
        else:
            plan = {"action": "scroll", "behavior": "human_mouse", "executed": False}
            if execute:
                self.reader.activate()
                self._require_frontmost()
        plan["notches"] = notches
        if not execute:
            return plan
        guard = self.reader.bind_target(node) if node is not None else None
        result = self._mouse().scroll(notches, over=node.bounds if node is not None else None, guard=guard)
        return self._finish(plan, result)

    def random_move(self, *, region=None, distance=(20.0, 120.0), execute: bool = False) -> dict:
        from .mouse.models import Rect
        if region is not None:
            region = region if isinstance(region, Rect) else Rect(*region)
        try:
            low, high = distance
            valid = all(math.isfinite(n) for n in (low, high)) and 0 <= low <= high and high > 0
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise BridgeError("Wander distance requires 0 <= minimum <= maximum and a positive maximum.")
        plan = {"action": "random_move", "behavior": "human_mouse", "executed": False,
                "region": list(region.as_tuple()) if region is not None else None,
                "distance": [low, high]}
        if not execute:
            return plan
        return self._finish(plan, self._mouse().random_move(region=region, distance=distance))

    def idle(self, *, region=None, wander_probability=0.2, execute: bool = False) -> dict:
        from .mouse.models import Rect
        if region is not None:
            region = region if isinstance(region, Rect) else Rect(*region)
        if not math.isfinite(wander_probability) or not 0 <= wander_probability <= 1:
            raise BridgeError("Wander probability must be between zero and one.")
        plan = {"action": "idle", "behavior": "human_mouse", "executed": False,
                "region": list(region.as_tuple()) if region is not None else None,
                "wander_probability": wander_probability}
        if not execute:
            return plan
        return self._finish(plan, self._mouse().idle(region=region, wander_probability=wander_probability))

    def type_text(self, selector: Selector, text: str, execute: bool = False) -> dict:
        if not isinstance(text, str) or any(not 32 <= ord(char) <= 126 for char in text):
            raise BridgeError("The first prototype accepts printable ASCII text only.")
        roles = _ROLE_ALIASES.get(selector.role.lower(), {selector.role})
        if not roles.issubset({"AXTextField", "AXTextArea"}):
            raise BridgeError("Typing requires a text field or text area selector.")
        plan = self.click(selector, execute=execute)
        plan.update(action="type_text", text=text)
        if not execute:
            return plan

        with preserve_motion_result(self):
            # Clicking can rerender the page. Only type into a freshly resolved control.
            node = resolve(self.reader.snapshot(), selector)
            clicked = plan
            plan = _plan(node, "type_text")
            plan.update({key: clicked[key] for key in ("motion", "coordinates") if key in clicked})
            plan["text"] = text
            self._require_frontmost()
            if not self.reader.is_focused(node):
                raise BridgeError(f"Element {node.name!r} is not focused; typing was stopped.")
            gui = self._driver()
            self._input(gui.hotkey, "command", "a")
            self._require_frontmost()
            if not self.reader.is_focused(node):
                raise BridgeError("The target lost focus; typing was stopped.")
            if text:
                self._input(gui.write, text, interval=0.04)
            else:
                self._input(gui.press, "backspace")
            plan["executed"] = True
            return plan

    def wait_for_text(
        self, text: str, timeout: float = 5, poll_interval: float = 0.15
    ) -> Snapshot:
        if not math.isfinite(timeout) or timeout < 0:
            raise BridgeError("Timeout must be a finite non-negative number.")
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise BridgeError("Poll interval must be a finite positive number.")
        def timed_out():
            return BridgeError(f"Timed out after {timeout:g}s waiting for text {text!r}.")

        def matches(snapshot):
            return any(text == node.name or text == node.value for node in snapshot.nodes)

        if timeout == 0:
            snapshot = self.reader.snapshot()
            if matches(snapshot):
                return snapshot
            raise timed_out()

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise timed_out()
            try:
                snapshot = self.reader.snapshot(timeout=remaining)
            except BridgeError:
                if time.monotonic() >= deadline:
                    raise timed_out() from None
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise timed_out()
            if matches(snapshot):
                return snapshot
            time.sleep(min(poll_interval, remaining))
