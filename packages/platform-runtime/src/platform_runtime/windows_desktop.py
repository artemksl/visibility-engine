"""Windows UI Automation and SendInput transports for the shared AX contract.

There is no provider policy here: axbridge still decides supported challenge
labels, unchecked state, ancestry, a single press, motion and cleanup. UIA reads
are bounded and fail closed; inaccessible/ambiguous controls are never replaced
by guessed coordinates, key presses, OCR or a different verification strategy.
"""
from __future__ import annotations

from collections import deque
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import math
import threading
import time
from types import SimpleNamespace


def _native_modules():
    import win32api
    import win32gui
    import win32process
    return win32api, win32gui, win32process


def capture_focus_return(expected_pid):
    _, gui, processes = _native_modules()
    previous = gui.GetForegroundWindow()
    previous_pid = processes.GetWindowThreadProcessId(previous)[1] if previous else None

    def restore():
        current = gui.GetForegroundWindow()
        if (not previous or previous_pid == expected_pid or not current
                or processes.GetWindowThreadProcessId(current)[1] != expected_pid
                or not gui.IsWindow(previous)
                or processes.GetWindowThreadProcessId(previous)[1] != previous_pid):
            return
        # Respect foreground restrictions. Never emulate Alt/Tab or attach
        # another thread's input queue to force focus past the user's choice.
        gui.SetForegroundWindow(previous)
    return restore


class WindowsPointer:
    """Physical pointer driver used by the unchanged HumanMouse executor."""

    name = "windows-sendinput"
    nominal_125hz_supported = True
    timing_limit = "Native dispatch timing depends on Windows scheduling."

    def __init__(self, user32=None):
        self.user32 = user32 or ctypes.WinDLL("user32", use_last_error=True)
        self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user32.GetAsyncKeyState.restype = ctypes.c_short
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.user32.GetCursorPos.restype = wintypes.BOOL
        self.user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self.user32.SetCursorPos.restype = wintypes.BOOL
        self.user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        self.user32.GetSystemMetrics.restype = ctypes.c_int
        # INPUT's union must include its largest member (MOUSEINPUT), rather
        # than the smaller ad-hoc structure that Windows rejects on 64-bit.
        class MouseInput(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]
        class InputUnion(ctypes.Union):
            _fields_ = [("mi", MouseInput)]
        class Input(ctypes.Structure):
            _anonymous_ = ("data",)
            _fields_ = [("type", wintypes.DWORD), ("data", InputUnion)]
        self.Input, self.MouseInput = Input, MouseInput
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        self.user32.SendInput.restype = wintypes.UINT

    def position(self):
        from axbridge.mouse import BackendFailure, Point
        point = wintypes.POINT()
        if not self.user32.GetCursorPos(ctypes.byref(point)):
            raise BackendFailure("Windows could not read the physical pointer.")
        return Point(point.x, point.y)

    def screen(self):
        from axbridge.mouse import Screen
        return Screen(int(self.user32.GetSystemMetrics(0)), int(self.user32.GetSystemMetrics(1)))

    def held_buttons(self):
        return {name for name, code in (("left", 1), ("right", 2), ("middle", 4))
                if self.user32.GetAsyncKeyState(code) & 0x8000}

    def check_fail_safe(self):
        from axbridge.mouse import FailSafeTriggered
        point, screen = self.position(), self.screen()
        if point.x in (0, screen.width - 1) and point.y in (0, screen.height - 1):
            raise FailSafeTriggered("Pointer reached a primary-display fail-safe corner.")

    def move(self, point):
        from axbridge.mouse import BackendFailure
        if not self.user32.SetCursorPos(round(point.x), round(point.y)):
            raise BackendFailure("Windows could not move the physical pointer.")

    def drag_move(self, point, button):
        self.move(point)

    def _send(self, flags, data=0):
        from axbridge.mouse import BackendFailure
        event = self.Input(type=0, mi=self.MouseInput(mouseData=data, dwFlags=flags))
        if self.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) != 1:
            raise BackendFailure("Windows did not confirm native mouse dispatch (desktop or integrity level).")

    def button_down(self, button, click_count=1):
        from axbridge.mouse import BackendFailure
        if click_count != 1:
            raise BackendFailure("Verification permits a single native press only.")
        self._send({"left": 0x0002, "right": 0x0008, "middle": 0x0020}[button])

    def button_up(self, button, click_count=1):
        self._send({"left": 0x0004, "right": 0x0010, "middle": 0x0040}[button])

    def release_owned_button(self, button):
        self.button_up(button)

    def wheel_step(self, sign):
        if sign not in (-1, 1):
            raise ValueError("A wheel step must be -1 or +1.")
        self._send(0x0800, sign * 120 & 0xFFFFFFFF)


class _NativeError(RuntimeError):
    pass


class _Element:
    """Retained UIA reference; no locator re-query or cached attributes."""
    def __init__(self, reader, native):
        self.reader, self.native = reader, native

    def setTimeout(self, seconds):
        self.reader.deadline = time.monotonic() + (seconds or min(self.reader.timeout, .5))
        self.reader._native_timeout(seconds or min(self.reader.timeout, .5))

    def __eq__(self, other):
        return isinstance(other, _Element) and bool(self.reader._call(
            lambda: self.reader.uia.CompareElements(self.native, other.native)))


_ROLES = {"CheckBox": "AXCheckBox", "Group": "AXGroup", "Pane": "AXGroup",
          "Window": "AXWindow", "Document": "AXWebArea", "Custom": "AXGroup"}


class WindowsReader:
    """Translate current UIA observations into axbridge's existing contract."""
    def __init__(self, *, pid, timeout, expected_url, desktop, max_nodes=1500, max_depth=40):
        self.pid, self.timeout, self.expected_url = pid, timeout, expected_url
        self.desktop, self.uia, self.module = desktop, desktop.uia, desktop.module
        self.max_nodes, self.max_depth = max_nodes, max_depth
        self.deadline = time.monotonic() + timeout
        self.ax = SimpleNamespace(errors=SimpleNamespace(AXError=_NativeError))
        self.refs = {}
        self.hwnd = self._select_window()
        self.window = _Element(self, self._call(lambda: self.uia.ElementFromHandle(self.hwnd)))
        self.page = None
        self.url = None

    def _native_timeout(self, seconds):
        milliseconds = max(1, int(min(seconds, max(0, self.deadline - time.monotonic())) * 1000))
        self.uia.ConnectionTimeout = milliseconds
        self.uia.TransactionTimeout = milliseconds

    def _call(self, read):
        from axbridge.mouse import TimingOverrun
        if time.monotonic() >= self.deadline:
            raise TimingOverrun("Windows accessibility read deadline expired.")
        self._native_timeout(min(.5, self.deadline - time.monotonic()))
        try:
            value = read()
        except Exception as error:
            raise _NativeError("The bound UI Automation reference is unavailable.") from error
        if time.monotonic() >= self.deadline:
            raise TimingOverrun("Windows accessibility read returned after its deadline.")
        return value

    def _id(self, element):
        value = tuple(self._call(element.native.GetRuntimeId))
        if not value:
            raise _NativeError("UI Automation did not expose a stable element identity.")
        return "uia:" + ":".join(map(str, value))

    def _children(self, element):
        # pywinauto's convenience children() drops failed COM reads. Retain raw
        # failures here so an incomplete capture can never look like clearance.
        values = self._call(lambda: element.native.FindAll(
            self.module.TreeScope_Children, self.uia.CreateTrueCondition()))
        return [_Element(self, self._call(lambda i=i: values.GetElement(i)))
                for i in range(self._call(lambda: values.Length))]

    def _control(self, element):
        value = self._call(lambda: element.native.CurrentControlType)
        return self.desktop.controls.get(value, "Unknown")

    def _role(self, element, control=None):
        control = control or self._control(element)
        if control == "Window" and element != self.window:
            return "AXDialog"
        return _ROLES.get(control, "AX" + control)

    def _pattern_value(self, element, pattern):
        # An absent pattern is a supported absence; an exposed pattern that
        # fails to read is an incomplete observation, not an unchecked value.
        available = getattr(self.module, "UIA_Is" + pattern + "PatternAvailablePropertyId")
        if not self._call(lambda: element.native.GetCurrentPropertyValue(available)):
            return None
        interface = getattr(self.module, "IUIAutomation" + pattern + "Pattern")
        pattern_id = getattr(self.module, "UIA_" + pattern + "PatternId")
        native = self._call(lambda: element.native.GetCurrentPattern(pattern_id).QueryInterface(interface))
        attribute = "CurrentToggleState" if pattern == "Toggle" else "CurrentValue"
        return self._call(lambda: getattr(native, attribute))

    @staticmethod
    def _copilot_url(value):
        if not isinstance(value, str):
            return None
        candidate = value.strip()
        if candidate.startswith(("copilot.com", "copilot.microsoft.com")):
            candidate = "https://" + candidate
        from axbridge.verification import _copilot_page_url
        return candidate if _copilot_page_url(candidate) else None

    def _read_window_url(self, root):
        queue, values, seen = deque([(root, False, 0)]), {}, set()
        while queue:
            element, in_document, depth = queue.popleft()
            if len(seen) >= self.max_nodes or depth > self.max_depth:
                raise _NativeError("The Chrome accessibility chrome is incomplete.")
            identity = self._id(element)
            if identity in seen:
                raise _NativeError("Chrome accessibility node identities are ambiguous.")
            seen.add(identity)
            control = self._control(element)
            # Chrome's omnibox is outside the web document. Do not mistake a
            # draft containing a URL for the current native page reference.
            if control == "Edit" and not in_document:
                value = self._copilot_url(self._pattern_value(element, "Value"))
                if value:
                    values[value] = element
            if control == "Document":
                value = self._copilot_url(self._pattern_value(element, "Value"))
                if value:
                    values[value] = element
                continue
            queue.extend((child, in_document, depth + 1) for child in self._children(element))
        if len(values) != 1:
            raise _NativeError("The exact Copilot URL is not uniquely exposed by native accessibility.")
        value, self.url_source = next(iter(values.items()))
        return value

    def _select_window(self):
        from axbridge.models import BridgeError
        gui, processes = self.desktop.gui, self.desktop.processes
        current = gui.GetForegroundWindow()
        if current and processes.GetWindowThreadProcessId(current)[1] == self.pid:
            return current
        windows = []
        def collect(hwnd, _):
            if (gui.IsWindowVisible(hwnd) and gui.GetClassName(hwnd).startswith("Chrome_WidgetWin_")
                    and processes.GetWindowThreadProcessId(hwnd)[1] == self.pid):
                windows.append(hwnd)
        gui.EnumWindows(collect, None)
        if len(windows) == 1:
            return windows[0]
        from axbridge.verification import _page_reference
        matches = []
        for hwnd in windows:
            root = _Element(self, self._call(lambda: self.uia.ElementFromHandle(hwnd)))
            try:
                url = self._read_window_url(root)
            except _NativeError:
                continue
            if _page_reference(url) == _page_reference(self.expected_url):
                matches.append(hwnd)
        if len(matches) != 1:
            raise BridgeError("The owned Chrome window cannot be uniquely identified for native activation.")
        return matches[0]

    def activate(self, pid):
        from axbridge.models import BridgeError
        if pid != self.pid or self.desktop.processes.GetWindowThreadProcessId(self.hwnd)[1] != pid:
            raise BridgeError("The retained Chrome window process changed.")
        self.desktop.gui.SetForegroundWindow(self.hwnd)
        if not self.is_frontmost():
            raise BridgeError("Windows refused foreground activation of the exact owned Chrome window.")

    def is_frontmost(self):
        return (self.desktop.gui.GetForegroundWindow() == self.hwnd
            and self.desktop.processes.GetWindowThreadProcessId(self.hwnd)[1] == self.pid)

    def snapshot(self, *, timeout):
        from axbridge.models import Node, Snapshot
        self.deadline = time.monotonic() + timeout
        self.url = self._read_window_url(self.window)
        self.refs, nodes, queue = {}, [], deque([(self.window, None, 0)])
        self.page = None
        while queue:
            element, parent_id, depth = queue.popleft()
            if len(nodes) >= self.max_nodes or depth > self.max_depth:
                raise _NativeError("The UI Automation capture is incomplete.")
            identity, control = self._id(element), self._control(element)
            if identity in self.refs:
                raise _NativeError("The UI Automation capture has ambiguous node identities.")
            self.refs[identity] = element
            if control == "Document" and self.page is None:
                self.page = element
            role = self._role(element, control)
            name = self._call(lambda: element.native.CurrentName) or ""
            value = self._pattern_value(element, "Toggle") if control == "CheckBox" else None
            bounds = self._bounds(element)
            hidden = self._call(lambda: element.native.CurrentIsOffscreen)
            nodes.append(Node(identity, role, str(name), None if value is None else str(value),
                bool(self._call(lambda: element.native.CurrentIsEnabled)),
                bool(self._call(lambda: element.native.CurrentHasKeyboardFocus)),
                None if hidden else bounds, parent_id))
            queue.extend((child, identity, depth + 1) for child in self._children(element))
        if self.page is None:
            raise _NativeError("The foreground Chrome window has no native web document.")
        return Snapshot(self.pid, self.desktop.gui.GetWindowText(self.hwnd),
            datetime.now(timezone.utc).isoformat(), nodes, self.url)

    def _bounds(self, element):
        bounds = self._call(lambda: element.native.CurrentBoundingRectangle)
        value = tuple(float(v) for v in (bounds.left, bounds.top,
            bounds.right - bounds.left, bounds.bottom - bounds.top))
        return value if all(math.isfinite(v) for v in value) and min(value[2:]) > 0 else None

    def _reference(self, node):
        return self.refs[node.id]

    def _attribute_reader(self, element, attribute):
        if attribute == "AXParent":
            value = self._call(lambda: self.uia.ControlViewWalker.GetParentElement(element.native))
            return _Element(self, value) if value else None
        if attribute == "AXRole":
            return self._role(element)
        if attribute in {"AXTitle", "AXDescription"}:
            return self._call(lambda: element.native.CurrentName) or ""
        if attribute == "AXHidden":
            return bool(self._call(lambda: element.native.CurrentIsOffscreen))
        if attribute == "AXValue":
            value = self._pattern_value(element, "Toggle")
            return None if value is None else str(value)
        raise AttributeError(attribute)

    def bind_target(self, node):
        return _BoundTarget(self, node)


class _BoundTarget:
    def __init__(self, reader, node):
        self.reader, self.node, self.target = reader, node, reader._reference(node)
        self.page, self.url = reader.page, reader.url
        self.url_source = reader.url_source

    def validate(self, point, *, deadline):
        from axbridge.mouse import TargetChanged, TargetNotExposed
        from axbridge.reader import _ValidationGate
        reader = self.reader
        reader.deadline = deadline
        gate = _ValidationGate(reader, deadline)
        try:
            gate.check()
            current_url = reader._copilot_url(reader._pattern_value(self.url_source, "Value"))
            if (not reader.is_frontmost() or current_url != self.url
                    or not gate.ancestors_contain(self.url_source, reader.window)):
                raise TargetChanged("The bound Chrome foreground window or URL changed.")
            if (not gate.ancestors_contain(self.target, self.page)
                    or not gate.ancestors_contain(self.page, reader.window)):
                raise TargetChanged("The bound checkbox left its exact native web document.")
            if (gate.read(self.target, "AXRole", required=True) != self.node.role
                    or gate.read(self.target, "AXTitle", "") != self.node.name):
                raise TargetChanged("The retained native checkbox role or name changed.")
            bounds = reader._bounds(self.target)
            if bounds != self.node.bounds:
                raise TargetChanged("The bound checkbox moved or resized.")
            x, y = point.as_tuple()
            if not bounds or not (bounds[0] <= x < bounds[0] + bounds[2]
                    and bounds[1] <= y < bounds[1] + bounds[3]):
                raise TargetNotExposed("The physical pointer is outside the bound checkbox.")
            if (not reader._call(lambda: self.target.native.CurrentIsEnabled)
                    or reader._call(lambda: self.target.native.CurrentIsOffscreen)):
                raise TargetNotExposed("The bound checkbox is hidden or disabled.")
            hit = _Element(reader, reader._call(lambda: reader.uia.ElementFromPoint(wintypes.POINT(int(x), int(y)))))
            if not gate.ancestors_contain(hit, self.target):
                raise TargetNotExposed("The retained checkbox is covered at the physical pointer.")
            gate.check()
        finally:
            gate.close()
        gate.check()


class WindowsDesktop:
    """Thread-affine native resources behind the portable component factory."""
    def __init__(self):
        self.thread_id = threading.get_ident()
        self.ole32 = ctypes.OleDLL("ole32")
        self.ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        self.ole32.CoUninitialize.argtypes = []
        self.ole32.CoUninitialize.restype = None
        self.ole32.CoInitializeEx(None, 0)  # UIA clients run in an MTA thread.
        self.previous_dpi, self.pointer, self.uia = None, None, None
        try:
            from pywinauto.uia_defines import IUIA
            import comtypes.client
            self.api, self.gui, self.processes = _native_modules()
            self.pointer = WindowsPointer()
            self.pointer.user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            self.pointer.user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            self.previous_dpi = self.pointer.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            if not self.previous_dpi:
                raise RuntimeError("Windows could not establish physical desktop coordinates.")
            self.module = IUIA().ui_automation_client
            self.uia = comtypes.client.CreateObject(self.module.CUIAutomation8,
                interface=self.module.IUIAutomation2)
            self.controls = {getattr(self.module, "UIA_" + name + "ControlTypeId"): name
                for name in ("CheckBox", "Group", "Pane", "Window", "Document", "Custom", "Edit")}
        except BaseException:
            self.close()
            raise

    def verification_adapter(self, *, mouse, factory):
        desktop, context = self, threading.local()
        def reader_factory(*, pid, timeout):
            reader = WindowsReader(pid=pid, timeout=timeout,
                expected_url=context.expected_url, desktop=desktop)
            context.reader = reader
            return reader
        shared = factory(mouse=mouse, reader_factory=reader_factory,
            activate_pid=lambda pid: context.reader.activate(pid))
        class BoundAdapter:
            def attempt(self, **kwargs):
                context.expected_url = kwargs["expected_url"]
                try:
                    return shared.attempt(**kwargs)
                finally:
                    if hasattr(context, "reader"):
                        del context.reader
        return BoundAdapter()

    def close(self):
        if threading.get_ident() != self.thread_id:
            raise RuntimeError("Native Windows desktop cleanup must run on its owning thread.")
        if self.previous_dpi:
            self.pointer.user32.SetThreadDpiAwarenessContext(self.previous_dpi)
            self.previous_dpi = None
        self.uia = None
        if self.ole32 is not None:
            self.ole32.CoUninitialize()
            self.ole32 = None
