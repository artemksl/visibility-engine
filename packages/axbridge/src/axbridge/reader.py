"""The only module that depends on atomacos and macOS Accessibility."""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata
import math
import platform
import sys
import time

from .models import BridgeError, Node, Snapshot


ACCESS_HELP = (
    "macOS Accessibility access is disabled for this process. Run "
    "'python -m axbridge doctor --request-access', then enable the app named by "
    "macOS in System Settings > Privacy & Security > Accessibility. "
    "If using Terminal, grant Terminal access and run the command there again."
)


def doctor(request_access: bool = False) -> dict:
    report = {"python": sys.version.split()[0], "platform": platform.platform()}
    for package in ("atomacos", "PyAutoGUI", "pyobjc-core"):
        try:
            report[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            report[package] = "not installed"
    if sys.platform != "darwin":
        return {**report, "ready": False, "help": "Live automation requires macOS."}
    try:
        from ApplicationServices import (
            AXIsProcessTrusted, AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt,
        )
        if request_access:
            AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
        import atomacos  # noqa: F401 -- explicitly check compatibility
        import pyautogui
        trusted = bool(AXIsProcessTrusted())
        size = tuple(pyautogui.size())
        report.update(accessibility_trusted=trusted, primary_screen=list(size))
        report["ready"] = trusted and min(size) > 0
        if not trusted:
            report["help"] = ACCESS_HELP
        elif min(size) <= 0:
            report["help"] = "No GUI display is accessible. Run from a local, unsandboxed Terminal session."
    except Exception as error:
        report.update(ready=False, error=f"{type(error).__name__}: {error}")
    return report


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _bounds(position, size):
    if position is None or size is None:
        return None
    try:
        x, y = position
        width, height = size
        values = tuple(float(v) for v in (x, y, width, height))
        if all(math.isfinite(v) for v in values) and width > 0 and height > 0:
            return values
    except (TypeError, ValueError):
        pass
    return None


def _native_attribute(element, attribute):
    """Read one attribute without atomacos' repeated attribute-name enumeration."""
    from atomacos._macos import PAXUIElementCopyAttributeValue
    return element.converter.convert_value(PAXUIElementCopyAttributeValue(element.ref, attribute))


class _ValidationGate:
    """A single pre-press budget; late native replies never authorize input."""

    def __init__(self, reader, deadline):
        self.reader, self.deadline = reader, deadline
        self.timed_refs = {}

    def check(self):
        from .mouse.models import TimingOverrun
        remaining = self.deadline - time.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise TimingOverrun("The accessibility validation deadline expired; input was stopped.")
        return remaining

    def limit(self, element, *, restore_timeout=0):
        remaining = self.check()
        # Inspect the class, not the AX object's dynamic attribute getter.
        timeout = getattr(type(element), "setTimeout", None)
        if timeout is not None:
            self.timed_refs[id(element)] = (element, restore_timeout)
            try:
                timeout(element, remaining)
            except self.reader.ax.errors.AXError as error:
                from .mouse.models import TargetChanged
                raise TargetChanged("The bound accessibility reference is unavailable.") from error
            self.check()

    def read(self, element, attribute, default=None, *, required=False):
        from .mouse.models import TargetChanged
        self.limit(element)
        errors = self.reader.ax.errors
        missing = tuple(error for error in (
            AttributeError, getattr(errors, "AXErrorNoValue", None),
            getattr(errors, "AXErrorAttributeUnsupported", None),
        ) if isinstance(error, type))
        try:
            read = getattr(self.reader, "_attribute_reader", getattr)
            value = read(element, attribute)
        except missing as error:
            if required:
                raise TargetChanged(f"The bound target no longer exposes {attribute}.") from error
            value = default
        except errors.AXError as error:
            raise TargetChanged(f"The bound accessibility reference is unavailable ({attribute}).") from error
        self.check()
        if value is None:
            if required:
                raise TargetChanged(f"The bound target no longer exposes {attribute}.")
            return default
        return value

    def ancestors_contain(self, element, expected):
        for _ in range(self.reader.max_depth + 1):
            self.check()
            if element is None:
                return False
            if element == expected:
                return True
            element = self.read(element, "AXParent")
        return False

    def close(self):
        # A gate must not leave its shrinking timeout on references used by later
        # snapshots. Resetting to zero restores the configured system-wide value.
        for element, restore_timeout in self.timed_refs.values():
            try:
                type(element).setTimeout(element, restore_timeout)
            except self.reader.ax.errors.AXError:
                pass  # A stale reference is already rejected by the live reads.


@dataclass(frozen=True)
class _BoundTarget:
    reader: object
    target: object
    window: object
    page: object
    role: str
    name: str
    bounds: tuple
    url: str | None
    enabled: bool | None

    def validate(self, point, *, deadline):
        from .mouse.models import TargetChanged, TargetNotExposed
        gate = _ValidationGate(self.reader, deadline)
        try:
            gate.check()
            if self.reader._frontmost_pid() != self.reader.pid:
                raise TargetChanged("The target application is not in the foreground; input was stopped.")
            if gate.read(self.reader.app, "AXFocusedWindow", required=True) != self.window:
                raise TargetChanged("The selected application window changed; input was stopped.")
            if gate.read(self.page, "AXRole", required=True) != "AXWebArea":
                raise TargetChanged("The bound page is no longer available.")
            if (str(gate.read(self.page, "AXURL", "")) or None) != self.url:
                raise TargetChanged("The page navigated while moving to the target.")
            if not gate.ancestors_contain(self.target, self.page):
                raise TargetChanged("The target no longer belongs to the bound page.")
            if not gate.ancestors_contain(self.page, self.window):
                raise TargetChanged("The page no longer belongs to the selected window.")
            if gate.read(self.target, "AXRole", required=True) != self.role:
                raise TargetChanged("The bound target role changed.")
            name = ""
            for attribute in ("AXTitle", "AXDescription"):
                name = _text(gate.read(self.target, attribute))
                if name:
                    break
            if not name:
                label = gate.read(self.target, "AXTitleUIElement")
                if label is not None:
                    for attribute in ("AXValue", "AXTitle", "AXDescription"):
                        name = _text(gate.read(label, attribute))
                        if name:
                            break
            if not name and self.role == "AXStaticText":
                name = _text(gate.read(self.target, "AXValue"))
            if name != self.name:
                raise TargetChanged("The bound target name changed.")
            enabled = gate.read(self.target, "AXEnabled", required=self.enabled is not None)
            if (enabled is not None and not enabled) or gate.read(self.target, "AXHidden", False):
                raise TargetNotExposed("The bound target is disabled or hidden.")
            actual = _bounds(gate.read(self.target, "AXPosition", required=True),
                             gate.read(self.target, "AXSize", required=True))
            if actual != self.bounds:
                raise TargetChanged("The target moved or resized while the pointer was moving.")
            x, y = point.as_tuple() if hasattr(point, "as_tuple") else point
            left, top, width, height = self.bounds
            if not (left <= x < left + width and top <= y < top + height):
                raise TargetNotExposed("The actual pointer is outside the bound target.")
            system = self.reader.ax.NativeUIElement.getSystemObject()
            # A system object's timeout sets the global AX timeout. Restore the
            # reader's original bound rather than resetting it to macOS' default.
            gate.limit(system, restore_timeout=min(self.reader.timeout, 0.5))
            try:
                hit = system.getElementAtPosition((x, y))
            except self.reader.ax.errors.AXError as error:
                raise TargetNotExposed("Could not verify the target at the actual pointer position.") from error
            gate.check()
            if not gate.ancestors_contain(hit, self.target):
                raise TargetNotExposed("The target is not exposed at the pointer; it may be covered.")
            gate.check()
        finally:
            gate.close()
        # Cleanup also consumes time. A late response or cleanup cannot open the gate.
        gate.check()


class MacReader:
    def __init__(self, *, pid: int | None = None, bundle_id: str = "com.google.Chrome",
                 window: str | None = None, max_nodes: int = 1500,
                 max_depth: int = 40, timeout: float = 8.0):
        if sys.platform != "darwin":
            raise BridgeError("Live automation requires macOS.")
        if max_nodes < 1 or max_depth < 1 or not math.isfinite(timeout) or timeout <= 0:
            raise BridgeError("Reader limits must be positive and finite.")
        try:
            import atomacos
            from ApplicationServices import AXIsProcessTrusted
            from AppKit import NSRunningApplication
        except ImportError as error:
            raise BridgeError("Install the project dependencies first: python -m pip install -e .") from error
        if not AXIsProcessTrusted():
            raise BridgeError(ACCESS_HELP)
        self.ax = atomacos
        self._attribute_reader = _native_attribute
        self.window_filter = window
        self.max_nodes, self.max_depth, self.timeout = max_nodes, max_depth, timeout
        self._refs = {}
        self._nodes = {}
        self._window = None
        self._errors = []
        self._generation = 0
        if pid is None:
            apps = list(NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id))
            if not apps:
                raise BridgeError(f"No running application with bundle ID {bundle_id!r}.")
            if len(apps) > 1:
                raise BridgeError(f"Multiple applications with bundle ID {bundle_id!r} are running. "
                                  "Select one with --pid.")
            pid = int(apps[0].processIdentifier())
        if pid <= 0 or NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) is None:
            raise BridgeError(f"Application PID {pid} is not running.")
        self.pid = pid
        self.app = atomacos.getAppRefByPid(pid)
        # Bound each AX request as well as the overall traversal.
        atomacos.NativeUIElement.setSystemWideTimeout(min(timeout, 0.5))

    def _read(self, element, attribute, default=None):
        deadline = getattr(self, "_deadline", None)
        if deadline is not None and time.monotonic() >= deadline:
            self._errors.append("Accessibility read deadline reached.")
            return default
        try:
            result = getattr(element, attribute)
            return default if result is None else result
        except (AttributeError, self.ax.errors.AXErrorNoValue):
            return default
        except self.ax.errors.AXError as error:
            self._errors.append(f"{attribute}: {type(error).__name__}")
            return default

    def _children(self, element):
        children = self._read(element, "AXChildren", [])
        return children if isinstance(children, (list, tuple)) else []

    def _select_window(self):
        # Chrome can expose its selected window while AXWindows is empty,
        # especially when another application is in the foreground.
        # The default scope is that selected window, so no enumeration is needed.
        if not self.window_filter:
            focused = self._read(self.app, "AXFocusedWindow")
            if focused is not None:
                return focused
        windows = list(self._read(self.app, "AXWindows", []))
        if self.window_filter:
            windows = [w for w in windows if self.window_filter in _text(self._read(w, "AXTitle"))]
        if len(windows) == 1:
            return windows[0]
        if not windows:
            raise BridgeError("No matching application window. Open a page or use --window with part of its title.")
        if not self.window_filter:
            focused = self._read(self.app, "AXFocusedWindow")
            if focused is not None and any(w == focused for w in windows):
                return focused
        raise BridgeError("More than one application window matches. Use a more specific --window title.")

    def _web_area(self, window, deadline):
        queue = deque([(window, 0)])
        areas = []
        count = 0
        limited = False
        while queue and count < self.max_nodes and time.monotonic() < deadline:
            element, depth = queue.popleft()
            count += 1
            if self._read(element, "AXRole") == "AXWebArea":
                areas.append(element)
                continue  # Nested iframe web areas belong to this page.
            children = self._children(element)
            if depth >= self.max_depth:
                limited |= bool(children)
            else:
                queue.extend((child, depth + 1) for child in children)
        if queue or limited:
            raise BridgeError("Window traversal limit reached before identifying a unique page. Increase reader limits.")
        if len(areas) != 1:
            raise BridgeError(
                f"Expected one active AXWebArea, found {len(areas)}. Select a window exposing one web page. "
                "For Chrome, enable accessibility at chrome://accessibility or launch the isolated demo "
                "with 'python -m axbridge demo open'. For a profile browser, select its running browser "
                "PID rather than its profile manager."
            )
        return areas[0]

    def _name(self, element, role, value):
        for attribute in ("AXTitle", "AXDescription"):
            candidate = _text(self._read(element, attribute))
            if candidate:
                return candidate
        label = self._read(element, "AXTitleUIElement")
        if label is not None:
            for attribute in ("AXValue", "AXTitle", "AXDescription"):
                candidate = _text(self._read(label, attribute))
                if candidate:
                    return candidate
        return _text(value) if role == "AXStaticText" else ""

    def snapshot(self, *, timeout: float | None = None) -> Snapshot:
        budget = self.timeout if timeout is None else min(timeout, self.timeout)
        if not math.isfinite(budget) or budget <= 0:
            raise BridgeError("Snapshot timeout must be finite and positive.")
        self._deadline = time.monotonic() + budget
        try:
            return self._capture(self._deadline)
        finally:
            self._deadline = None

    def _capture(self, deadline) -> Snapshot:
        self._refs, self._nodes, self._errors = {}, {}, []
        self._generation += 1
        self._window = self._select_window()
        area = self._web_area(self._window, deadline)
        self._area = area
        snapshot = Snapshot(
            app_pid=self.pid, window_title=_text(self._read(self._window, "AXTitle")),
            captured_at=datetime.now(timezone.utc).isoformat(), nodes=[],
            url=str(self._read(area, "AXURL", "")) or None,
        )
        self._page_url = snapshot.url
        queue = deque([(area, None, 0)])
        while queue and len(snapshot.nodes) < self.max_nodes and time.monotonic() < deadline:
            element, parent_id, depth = queue.popleft()
            role = _text(self._read(element, "AXRole"))
            if not role:
                self._errors.append("Element role unavailable; page may have changed during inspection.")
                continue
            value = self._read(element, "AXValue")
            value = str(value) if isinstance(value, (str, int, float, bool)) else None
            node_id = f"s{self._generation}:n{len(snapshot.nodes)}"
            enabled = self._read(element, "AXEnabled")
            focused = self._read(element, "AXFocused")
            hidden = self._read(element, "AXHidden", False)
            node = Node(
                id=node_id, role=role, name=self._name(element, role, value), value=value,
                enabled=bool(enabled) if enabled is not None else None,
                focused=bool(focused) if focused is not None else None,
                bounds=None if hidden else _bounds(self._read(element, "AXPosition"), self._read(element, "AXSize")),
                parent_id=parent_id,
            )
            snapshot.nodes.append(node)
            self._refs[node.id], self._nodes[node.id] = element, node
            children = self._children(element)
            if depth >= self.max_depth:
                snapshot.truncated |= bool(children)
            else:
                queue.extend((child, node.id, depth + 1) for child in children)
        snapshot.truncated |= bool(queue) or bool(self._errors)
        if snapshot.truncated:
            snapshot.warnings.append("Snapshot is incomplete; actions will be refused. Raise limits or inspect a stable page.")
        snapshot.warnings.extend(sorted(set(self._errors))[:10])
        return snapshot

    def activate(self):
        if self._window is None:
            self._window = self._select_window()
        self.app.activate()
        try:
            self._window.Raise()
        except (AttributeError, self.ax.errors.AXError) as error:
            raise BridgeError("Could not raise the selected application window.") from error
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.is_frontmost():
                return
            time.sleep(0.05)
        raise BridgeError("The target application did not become the foreground window.")

    def is_frontmost(self) -> bool:
        if self._frontmost_pid() != self.pid:
            return False
        focused = self._read(self.app, "AXFocusedWindow")
        return self._window is not None and focused is not None and focused == self._window

    @staticmethod
    def _frontmost_pid():
        from AppKit import NSWorkspace
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(front.processIdentifier()) if front is not None else None

    def _reference(self, node):
        # IDs are valid only within the latest snapshot.
        return self._refs.get(node.id) if self._nodes.get(node.id) is node else None

    def bind_target(self, node: Node):
        """Bind latest-snapshot identity once; later snapshots do not invalidate it."""
        from .mouse.models import InvalidTarget, TargetChanged
        target = self._reference(node)
        if target is None:
            raise TargetChanged("Cannot bind a stale or copied snapshot node.")
        if node.bounds is None or node.enabled is False:
            raise InvalidTarget("Cannot bind a target without enabled, visible bounds.")
        return _BoundTarget(self, target, self._window, self._area,
                            node.role, node.name, node.bounds, self._page_url, node.enabled)

    def hit_test(self, node: Node, x: float, y: float) -> bool:
        target = self._reference(node)
        if target is None:
            return False
        try:
            hit = self.ax.NativeUIElement.getSystemObject().getElementAtPosition((x, y))
            for _ in range(self.max_depth + 1):
                if hit is None:
                    return False
                if hit == target:
                    return True
                hit = self._read(hit, "AXParent")
        except (AttributeError, self.ax.errors.AXError):
            return False
        return False

    def is_focused(self, node: Node) -> bool:
        target = self._reference(node)
        focused = self._read(self.app, "AXFocusedUIElement")
        return target is not None and focused is not None and focused == target
