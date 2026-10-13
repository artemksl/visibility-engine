"""Native desktop bindings only; provider and motion policy remain shared.

Imports are display independent. Ordinary browser work never loads a native
desktop dependency. The macOS factories are the existing axbridge factories.
"""
from __future__ import annotations

import sys


def verification_components(*, mouse_factory, adapter_factory, config):
    """Supply native transports to the same verification/motion algorithms."""
    if sys.platform == "win32":
        from .windows_desktop import WindowsDesktop
        desktop = WindowsDesktop()
        try:
            mouse = mouse_factory(config=config, backend=desktop.pointer)
            adapter = desktop.verification_adapter(mouse=mouse, factory=adapter_factory)
        except BaseException:
            desktop.close()
            raise
        return mouse, adapter, desktop.close
    mouse = mouse_factory(config=config)
    return mouse, adapter_factory(mouse=mouse), lambda: None


def capture_focus_return(expected_pid, *, mac_factory):
    if sys.platform == "win32":
        from .windows_desktop import capture_focus_return as capture_windows_focus_return
        return capture_windows_focus_return(expected_pid)
    return mac_factory(expected_pid)
