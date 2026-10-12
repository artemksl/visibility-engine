"""A local Chrome fixture using its own profile and explicitly recorded PID."""

import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import unquote

from .actions import Controller, Selector, preserve_motion_result, resolve
from .models import BridgeError
from .reader import MacReader


ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / ".runtime"
PROFILE = RUNTIME / "chrome-profile"
PAGE = ROOT / "demo" / "index.html"
STATE = RUNTIME / "demo-session.json"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def _is_demo_process(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    result = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                            capture_output=True, text=True, check=False)
    return result.returncode == 0 and str(CHROME) in result.stdout and f"--user-data-dir={PROFILE}" in result.stdout


def get_demo_state() -> dict:
    try:
        state = json.loads(STATE.read_text())
        if state.get("url") != PAGE.as_uri() or not _is_demo_process(state.get("pid")):
            raise ValueError("Demo process is not running")
        return state
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise BridgeError("No running demo session. Run 'python -m axbridge demo open' first.") from error


def open_demo() -> dict:
    if sys.platform != "darwin" or not CHROME.exists():
        raise BridgeError("The demo requires macOS and Google Chrome in /Applications.")
    if not PAGE.exists():
        raise BridgeError("Demo page is missing. Run from an editable checkout of this project.")
    try:
        return {**get_demo_state(), "already_running": True}
    except BridgeError:
        pass
    RUNTIME.mkdir(exist_ok=True)
    args = [str(CHROME), f"--user-data-dir={PROFILE}", "--no-first-run",
            "--no-default-browser-check", "--disable-sync", "--disable-background-networking",
            "--force-renderer-accessibility=complete", "--window-size=1100,850",
            "--window-position=80,80", f"--app={PAGE.as_uri()}"]
    with (RUNTIME / "chrome.log").open("a") as log:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=log, start_new_session=True)
    # Record ownership before waiting, so even a slow launch can be recovered.
    state = {"pid": process.pid, "url": PAGE.as_uri(), "profile": str(PROFILE)}
    STATE.write_text(json.dumps(state, indent=2) + "\n")
    from AppKit import NSRunningApplication
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BridgeError("Chrome did not stay running. See .runtime/chrome.log. "
                              "Run this command from a local Terminal outside the execution sandbox.")
        running = NSRunningApplication.runningApplicationWithProcessIdentifier_(process.pid)
        if running is not None and running.isFinishedLaunching():
            return {**state, "already_running": False}
        time.sleep(0.1)
    raise BridgeError(f"Chrome PID {process.pid} is still launching after 10 seconds. "
                      "Its session is saved; run 'python -m axbridge demo open' again shortly.")


def _assert_demo(snapshot):
    if "AX Bridge Demo" not in snapshot.window_title or unquote(snapshot.url or "") != unquote(PAGE.as_uri()):
        raise BridgeError("The demo window is no longer displaying the local test page; no action taken.")


class DemoReader(MacReader):
    def snapshot(self, *, timeout=None):
        snapshot = super().snapshot(timeout=timeout)
        _assert_demo(snapshot)
        return snapshot


def run_demo(name: str = "Ada", execute: bool = False, *, trace: list | None = None) -> dict:
    if not name.strip() or any(not 32 <= ord(char) <= 126 for char in name):
        raise BridgeError("Use a nonempty name containing printable ASCII characters.")
    state = get_demo_state()
    reader = DemoReader(pid=state["pid"], window="AX Bridge Demo")
    controller = Controller(reader)
    def perform(action, *args, **kwargs):
        before_motion = getattr(controller.mouse, "last_result", None)
        try:
            return action(*args, **kwargs)
        finally:
            motion = getattr(controller.mouse, "last_result", None)
            if trace is not None and motion is not None and motion is not before_motion:
                trace.append(motion.to_dict(include_trace=True))
    with preserve_motion_result(controller):
        before = reader.snapshot()
        field, button = Selector("textbox", "Name"), Selector("button", "Greet")
        # Validate both selectors before any input.
        resolve(before, field)
        resolve(before, button)
        actions = [perform(controller.type_text, field, name, execute=execute)]
        if execute:
            deadline = time.monotonic() + 5
            while resolve(reader.snapshot(), field).value != name:
                if time.monotonic() >= deadline:
                    raise BridgeError("The Name field did not contain the text just entered; demo stopped.")
                time.sleep(0.15)
        actions.append(perform(controller.click, button, execute=execute))
        expected = f"Hello, {name.strip()}"
        result = {"executed": execute, "pid": state["pid"], "actions": actions,
                  "expected": expected, "verified": False}
        if execute:
            after = controller.wait_for_text(expected)
            result.update(verified=True, node_count=len(after.nodes), captured_at=after.captured_at)
            (RUNTIME / "last-result.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
