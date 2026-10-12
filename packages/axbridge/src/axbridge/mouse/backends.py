"""Pointer drivers. Importing this module never opens a display or imports GUI code.

The macOS adapter deliberately isolates PyAutoGUI 0.9.41's private dispatcher:
its public move/drag wrappers sleep 10 ms, longer than our 8 ms sample interval.
"""

from __future__ import annotations

import importlib
import sys
from typing import Callable

from .models import BackendFailure, FailSafeTriggered, Point, Screen


class StockPyAutoGUIBackend:
    """Public-API diagnostic driver; actual timing depends on the host driver.

    macOS users must use ``MacOSPyAutoGUIBackend`` for nominal 125 Hz dispatch.
    State/release hooks allow other native backends to be used without guessing
    whether a user already holds a button.
    """

    name = "pyautogui-public"
    nominal_125hz_supported = False
    timing_limit = "Public PyAutoGUI driver delays are included in each dispatch."

    def __init__(self, gui=None, *, held_buttons: Callable | None = None,
                 emergency_release: Callable | None = None, edge_margin=2):
        self.gui = gui if gui is not None else importlib.import_module("pyautogui")
        self.gui.FAILSAFE = True
        self.edge_margin = edge_margin
        self._held_buttons = held_buttons
        self._emergency_release = emergency_release

    def position(self) -> Point:
        x, y = self.gui.position()
        return Point(int(x), int(y))

    def screen(self) -> Screen:
        width, height = self.gui.size()
        return Screen(int(width), int(height), edge_margin=self.edge_margin)

    def held_buttons(self) -> set[str]:
        if self._held_buttons is not None:
            return set(self._held_buttons())
        if sys.platform == "win32":
            import ctypes
            return {name for name, code in (("left", 1), ("right", 2), ("middle", 4))
                    if ctypes.windll.user32.GetAsyncKeyState(code) & 0x8000}
        platform = getattr(self.gui, "platformModule", None)
        display = getattr(platform, "_display", None)
        if sys.platform.startswith("linux") and display is not None:
            mask = display.screen().root.query_pointer().mask
            return {name for name, flag in (("left", 256), ("middle", 512), ("right", 1024))
                    if mask & flag}
        raise BackendFailure("The stock backend needs a native held-button state reader on this platform.")

    def check_fail_safe(self):
        # Do not weaken the global safety flag when another caller changes it.
        self.gui.FAILSAFE = True
        check = getattr(self.gui, "failSafeCheck", None) or getattr(self.gui, "_failSafeCheck", None)
        if check is None:
            raise BackendFailure("Unsupported PyAutoGUI fail-safe interface.")
        try:
            check()
        except Exception as error:
            exception_type = getattr(self.gui, "FailSafeException", ())
            if isinstance(error, exception_type):
                raise FailSafeTriggered("Pointer reached a PyAutoGUI fail-safe corner.") from error
            raise

    def move(self, point: Point):
        self._public(self.gui.moveTo, point.x, point.y, duration=0, _pause=False)

    def drag_move(self, point: Point, button: str):
        self._public(self.gui.dragTo, point.x, point.y, duration=0, button=button,
                     mouseDownUp=False, _pause=False)

    def _public(self, method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except Exception as error:
            if isinstance(error, getattr(self.gui, "FailSafeException", ())):
                raise FailSafeTriggered("Pointer reached a PyAutoGUI fail-safe corner.") from error
            raise

    def button_down(self, button: str, click_count=1):
        if click_count != 1:
            raise BackendFailure("The stock backend cannot explicitly set native double-click state.")
        self._public(self.gui.mouseDown, button=button, _pause=False)

    def button_up(self, button: str, click_count=1):
        self._public(self.gui.mouseUp, button=button, _pause=False)

    def wheel_step(self, sign: int):
        if sign not in (-1, 1):
            raise ValueError("A wheel step must be -1 or +1.")
        self._public(self.gui.scroll, sign, _pause=False)

    def release_owned_button(self, button: str):
        if self._emergency_release is not None:
            self._emergency_release(button)
            return
        # Native release only: public mouseUp itself checks the emergency corner.
        platform = getattr(self.gui, "platformModule", None)
        release = getattr(platform, "_mouseUp", None)
        if release is None:
            raise BackendFailure("No native emergency button-release adapter is available.")
        point = self.position()
        release(point.x, point.y, button)


class MacOSPyAutoGUIBackend(StockPyAutoGUIBackend):
    """Version-checked native adapter without the old driver's movement sleep."""

    name = "pyautogui-0.9.41-macos-native"
    nominal_125hz_supported = True
    timing_limit = "125 Hz is a dispatch objective; OS scheduling and delivery are measured separately."

    def __init__(self, gui=None, quartz=None, *, edge_margin=2):
        super().__init__(gui, edge_margin=edge_margin)
        if getattr(self.gui, "__version__", None) != "0.9.41":
            raise BackendFailure("The macOS mouse adapter supports pinned PyAutoGUI 0.9.41 only.")
        self.platform = getattr(self.gui, "platformModule", None)
        if not callable(getattr(self.platform, "_sendMouseEvent", None)):
            raise BackendFailure("The pinned macOS PyAutoGUI event dispatcher is unavailable.")
        self.quartz = quartz if quartz is not None else importlib.import_module("Quartz")
        q = self.quartz
        self._buttons = {
            "left": (q.kCGMouseButtonLeft, q.kCGEventLeftMouseDown,
                     q.kCGEventLeftMouseUp, q.kCGEventLeftMouseDragged),
            "right": (q.kCGMouseButtonRight, q.kCGEventRightMouseDown,
                      q.kCGEventRightMouseUp, q.kCGEventRightMouseDragged),
            "middle": (q.kCGMouseButtonCenter, q.kCGEventOtherMouseDown,
                       q.kCGEventOtherMouseUp, q.kCGEventOtherMouseDragged),
        }

    def held_buttons(self) -> set[str]:
        q = self.quartz
        return {name for name, values in self._buttons.items()
                if q.CGEventSourceButtonState(q.kCGEventSourceStateCombinedSessionState, values[0])}

    def move(self, point: Point):
        self.platform._sendMouseEvent(self.quartz.kCGEventMouseMoved, point.x, point.y, 0)

    def drag_move(self, point: Point, button: str):
        native_button, _, _, event = self._buttons[button]
        self.platform._sendMouseEvent(event, point.x, point.y, native_button)

    def _native_button(self, button: str, *, down: bool, click_count=1):
        q = self.quartz
        native_button, down_event, up_event, _ = self._buttons[button]
        point = self.position()
        event = q.CGEventCreateMouseEvent(None, down_event if down else up_event,
                                         (point.x, point.y), native_button)
        if event is None:
            raise BackendFailure("Quartz could not create a mouse event.")
        q.CGEventSetIntegerValueField(event, q.kCGMouseEventClickState, click_count)
        q.CGEventPost(q.kCGHIDEventTap, event)

    def button_down(self, button: str, click_count=1):
        if click_count == 1:
            self._public(self.gui.mouseDown, button=button, _pause=False)
        else:
            self._native_button(button, down=True, click_count=click_count)

    def button_up(self, button: str, click_count=1):
        if click_count == 1:
            self._public(self.gui.mouseUp, button=button, _pause=False)
        else:
            self._native_button(button, down=False, click_count=click_count)

    def wheel_step(self, sign: int):
        if sign not in (-1, 1):
            raise ValueError("A wheel step must be -1 or +1.")
        q = self.quartz
        event = q.CGEventCreateScrollWheelEvent(None, q.kCGScrollEventUnitLine, 1, sign)
        if event is None:
            raise BackendFailure("Quartz could not create a wheel event.")
        q.CGEventPost(q.kCGHIDEventTap, event)

    def release_owned_button(self, button: str):
        # The sole fail-safe bypass is an up event for a button owned by this
        # action. It neither changes FAILSAFE nor moves the pointer.
        self._native_button(button, down=False)


def default_backend(*, edge_margin=2):
    if sys.platform == "darwin":
        return MacOSPyAutoGUIBackend(edge_margin=edge_margin)
    return StockPyAutoGUIBackend(edge_margin=edge_margin)
