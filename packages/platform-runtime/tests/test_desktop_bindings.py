"""Synthetic native trees and input drivers; no browser or desktop actions."""
from __future__ import annotations

import importlib.util
import ctypes
from pathlib import Path
import sys
from types import SimpleNamespace
import time

import pytest

pytest.importorskip("axbridge", reason="desktop bindings require setup --desktop-verification")

from axbridge.mouse import MotionConfig, Point
from axbridge.verification import CopilotVerificationAdapter, detect_verification
from platform_runtime import desktop, windows_desktop as windows


class Element:
    def __init__(self, identity, control, name="", *, value=None, enabled=True, hidden=False):
        self.identity, self.CurrentControlType, self.CurrentName = identity, control, name
        self.CurrentIsEnabled, self.CurrentIsOffscreen = enabled, hidden
        self.CurrentHasKeyboardFocus = False
        self.CurrentBoundingRectangle = SimpleNamespace(left=100, top=100, right=120, bottom=120)
        self.value, self.children, self.parent = value, [], None

    def add(self, child):
        self.children.append(child)
        child.parent = self
        return child

    def GetRuntimeId(self):
        return (42, self.identity)

    def FindAll(self, *_args):
        return SimpleNamespace(Length=len(self.children), GetElement=lambda index: self.children[index])

    def GetCurrentPropertyValue(self, kind):
        return self.value is not None and kind == ("Toggle" if self.CurrentControlType == "CheckBox" else "Value")

    def GetCurrentPattern(self, _kind):
        return SimpleNamespace(QueryInterface=lambda _interface: SimpleNamespace(
            CurrentValue=self.value, CurrentToggleState=self.value))


def native_case():
    root = Element(1, "Window", "Copilot - Chrome")
    address = root.add(Element(2, "Edit", "Address and search bar", value="https://copilot.com/chats/example"))
    page = root.add(Element(3, "Document", "Copilot"))
    context = page.add(Element(4, "Group", "Security challenge"))
    checkbox = context.add(Element(5, "CheckBox", "Verify you are human", value=0))
    foreground = [10]
    gui = SimpleNamespace(GetForegroundWindow=lambda: foreground[0],
        SetForegroundWindow=lambda hwnd: foreground.__setitem__(0, hwnd),
        GetWindowText=lambda _hwnd: "Copilot - Chrome")
    module = SimpleNamespace(TreeScope_Children=2,
        UIA_IsValuePatternAvailablePropertyId="Value", UIA_ValuePatternId="Value",
        IUIAutomationValuePattern=object, UIA_IsTogglePatternAvailablePropertyId="Toggle",
        UIA_TogglePatternId="Toggle", IUIAutomationTogglePattern=object)
    uia = SimpleNamespace(ElementFromHandle=lambda _hwnd: root,
        CompareElements=lambda first, second: first is second, CreateTrueCondition=lambda: True,
        ControlViewWalker=SimpleNamespace(GetParentElement=lambda element: element.parent),
        ElementFromPoint=lambda _point: checkbox)
    native = SimpleNamespace(gui=gui,
        processes=SimpleNamespace(GetWindowThreadProcessId=lambda _hwnd: (1, 7)),
        uia=uia, module=module, controls={control: control for control in windows._ROLES} | {"Edit": "Edit"})
    return SimpleNamespace(root=root, address=address, page=page, context=context,
        checkbox=checkbox, foreground=foreground, desktop=native)


def test_macos_factory_retains_existing_mouse_config_and_adapter(monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    config, calls = MotionConfig(guard_budget=.5, settle_limits=(.05, .85)), []
    mouse_factory = lambda **kwargs: calls.append(("mouse", kwargs)) or "mouse"
    adapter_factory = lambda **kwargs: calls.append(("adapter", kwargs)) or "adapter"
    mouse, adapter, close = desktop.verification_components(
        mouse_factory=mouse_factory, adapter_factory=adapter_factory, config=config)
    assert (mouse, adapter) == ("mouse", "adapter")
    assert calls == [("mouse", {"config": config}), ("adapter", {"mouse": "mouse"})]
    close()


def test_windows_factory_passes_same_shared_config_and_cleans_failed_creation(monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    closed, config = [], MotionConfig(guard_budget=.5, settle_limits=(.05, .85))
    native = SimpleNamespace(pointer=object(), close=lambda: closed.append(True))
    monkeypatch.setattr(windows, "WindowsDesktop", lambda: native)
    def mouse(**kwargs):
        assert kwargs == {"config": config, "backend": native.pointer}
        raise RuntimeError("synthetic native construction failure")
    with pytest.raises(RuntimeError, match="synthetic"):
        desktop.verification_components(mouse_factory=mouse, adapter_factory=object, config=config)
    assert closed == [True]


def test_windows_normalization_uses_shared_challenge_recognition_and_exact_bound_hit():
    case = native_case()
    reader = windows.WindowsReader(pid=7, timeout=2,
        expected_url="https://copilot.com/chats/example", desktop=case.desktop)
    snapshot = reader.snapshot(timeout=2)
    detection = detect_verification(snapshot)
    assert detection.status == "ready"
    assert snapshot.url == case.address.value
    bound = reader.bind_target(detection.checkbox)
    bound.validate(Point(110, 110), deadline=time.monotonic() + 1)
    case.desktop.uia.ElementFromPoint = lambda _point: case.address
    with pytest.raises(Exception, match="covered"):
        bound.validate(Point(110, 110), deadline=time.monotonic() + 1)


def test_windows_dialog_context_maps_to_the_same_shared_verification_role():
    case = native_case()
    case.context.CurrentControlType = "Window"
    reader = windows.WindowsReader(pid=7, timeout=2,
        expected_url=case.address.value, desktop=case.desktop)
    detection = detect_verification(reader.snapshot(timeout=2))
    assert detection.status == "ready" and detection.context.role == "AXDialog"
    assert reader._attribute_reader(reader._reference(detection.context), "AXRole") == "AXDialog"


@pytest.mark.parametrize("change", ["name", "checked", "disabled", "hidden", "url", "foreground"])
def test_shared_windows_attempt_never_presses_changed_or_unsupported_target(change):
    case, clicks = native_case(), []
    if change == "checked":
        case.checkbox.value = 1
    elif change == "disabled":
        case.checkbox.CurrentIsEnabled = False
    elif change == "hidden":
        case.checkbox.CurrentIsOffscreen = True
    elif change == "name":
        case.checkbox.CurrentName = "Continue"
    elif change == "url":
        case.address.value = "https://copilot.com/chats/different"
    mouse = SimpleNamespace(click=lambda *_args, **_kwargs: clicks.append(True), last_result=None)
    adapter = windows.WindowsDesktop.verification_adapter(case.desktop,
        mouse=mouse, factory=CopilotVerificationAdapter)
    result = adapter.attempt(expected_pid=7, expected_url="https://copilot.com/chats/example",
        foreground_check=lambda **_kwargs: change != "foreground", deadline=time.monotonic() + 2)
    assert not result["click_attempted"]
    assert clicks == []


def test_shared_windows_attempt_dispatches_exactly_one_guarded_click():
    case, clicks = native_case(), []
    motion = SimpleNamespace(cleanup_succeeded=True, to_dict=lambda: {"cleanup_succeeded": True})
    def click(bounds, **kwargs):
        assert bounds == (100., 100., 20., 20.)
        assert kwargs["button"] == "left" and kwargs["count"] == 1
        kwargs["guard"].validate(Point(110, 110), deadline=time.monotonic() + 1)
        clicks.append(True)
        return motion
    adapter = windows.WindowsDesktop.verification_adapter(case.desktop,
        mouse=SimpleNamespace(click=click, last_result=None), factory=CopilotVerificationAdapter)
    result = adapter.attempt(expected_pid=7, expected_url="https://copilot.com/chats/example",
        foreground_check=lambda **_kwargs: True, deadline=time.monotonic() + 2)
    assert result["status"] == "clicked" and result["click_attempted"] is True
    assert clicks == [True]


def test_uia_partial_capture_and_unknown_toggle_are_not_clearance():
    case = native_case()
    case.checkbox.value = None
    reader = windows.WindowsReader(pid=7, timeout=2,
        expected_url=case.address.value, desktop=case.desktop)
    assert detect_verification(reader.snapshot(timeout=2)).status == "unsupported"
    case.root.FindAll = lambda *_args: (_ for _ in ()).throw(RuntimeError("missing native branch"))
    with pytest.raises(windows._NativeError, match="unavailable"):
        reader.snapshot(timeout=2)


def test_focus_return_never_overrides_user_switch_or_reused_window(monkeypatch):
    current, calls = [20], []
    pids = {20: 5, 10: 7, 30: 11}
    gui = SimpleNamespace(GetForegroundWindow=lambda: current[0], IsWindow=lambda hwnd: hwnd in pids,
        SetForegroundWindow=lambda hwnd: calls.append(hwnd))
    processes = SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (1, pids[hwnd]))
    monkeypatch.setattr(windows, "_native_modules", lambda: (None, gui, processes))
    restore = windows.capture_focus_return(7)
    current[0] = 30
    restore()
    assert calls == []
    current[0] = 10
    pids[20] = 99
    restore()
    assert calls == []
    pids[20] = 5
    restore()
    assert calls == [20]


def test_windows_native_pointer_dispatch_and_failed_release_are_explicit():
    calls, success = [], [1]
    def send(count, pointer, size):
        assert count == 1 and size == ctypes.sizeof(driver.Input)
        event = ctypes.cast(pointer, ctypes.POINTER(driver.Input)).contents
        calls.append(event.mi.dwFlags)
        return success[0]
    api = SimpleNamespace(SendInput=send, GetAsyncKeyState=lambda _code: 0,
        GetCursorPos=lambda _point: True, SetCursorPos=lambda *_args: True,
        GetSystemMetrics=lambda _index: 1920)
    driver = windows.WindowsPointer(api)
    driver.button_down("left")
    driver.release_owned_button("left")
    assert calls == [0x0002, 0x0004]
    success[0] = 0
    with pytest.raises(Exception, match="did not confirm"):
        driver.release_owned_button("left")


def test_windows_native_session_initializes_mta_first_and_releases_owned_resources(monkeypatch):
    calls = []
    def initialize(reserved, mode):
        assert reserved is None and mode == 0
        calls.append("mta")
    def uninitialize():
        calls.append("uninitialize")
    ole = SimpleNamespace(CoInitializeEx=initialize, CoUninitialize=uninitialize)
    monkeypatch.setattr(ctypes, "OleDLL", lambda _name: ole, raising=False)
    def dpi(context):
        if isinstance(context, ctypes.c_void_p):
            calls.append("physical-coordinates")
            return 41
        assert context == 41
        calls.append("restore-dpi")
    pointer = SimpleNamespace(user32=SimpleNamespace(SetThreadDpiAwarenessContext=dpi))
    monkeypatch.setattr(windows, "WindowsPointer", lambda: pointer)
    module = SimpleNamespace(CUIAutomation8=object(), IUIAutomation2=object())
    for index, name in enumerate(("CheckBox", "Group", "Pane", "Window", "Document", "Custom", "Edit")):
        setattr(module, "UIA_" + name + "ControlTypeId", index)
    def initialize_uia():
        assert calls[0] == "mta"
        return SimpleNamespace(ui_automation_client=module)
    client = SimpleNamespace(CreateObject=lambda _class, **_kwargs: object())
    monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "pywinauto.uia_defines", SimpleNamespace(IUIA=initialize_uia))
    monkeypatch.setitem(sys.modules, "comtypes", SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, "comtypes.client", client)
    monkeypatch.setattr(windows, "_native_modules", lambda: (None, None, None))
    native = windows.WindowsDesktop()
    assert ole.CoInitializeEx.argtypes == [ctypes.c_void_p, windows.wintypes.DWORD]
    assert ole.CoUninitialize.argtypes == [] and ole.CoUninitialize.restype is None
    native.close()
    native.close()
    assert calls == ["mta", "physical-coordinates", "restore-dpi", "uninitialize"]


def test_bootstrap_installs_all_local_packages_without_mac_only_windows_dependencies():
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("visibility_checkout_setup", root / "setup.py")
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    assert setup.venv_python(root, platform="win32") == root / ".venv/Scripts/python.exe"
    assert setup.venv_python(root, platform="darwin") == root / ".venv/bin/python"
    args = setup.installation_arguments(root, tests=True, desktop=True)
    assert str(root / "packages/platform-runtime") + "[desktop]" in args
    assert str(root / "packages/axbridge") in args
    assert str(root / "panel") + "[test]" in args
    assert len(args) == 5 and "-e" not in args
