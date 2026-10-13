#!/usr/bin/env python3
"""Windows Chrome launcher; macOS delegates to its existing launcher unchanged.

Only OS lifecycle mechanics live here. The caller freezes the selected manifest
and holds the runner's restart barriers; Chrome opens local identity anchors,
never provider pages or prompts.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlsplit
from urllib.request import urlopen

from platform_runtime.paths import chrome_binary, chrome_user_data_dir
from platform_runtime.processes import popen_session_kwargs, process_is_current_user, terminate_process_tree
from platform_runtime.storage import publish_complete, sync_file
from visibility_panel.browser.browser_lifecycle import (
    LOCK_HELD_ENV, exclusive_browser_lifecycle,
)
from visibility_panel.execution.chrome_clean_start import prepare


ROOT = Path(__file__).resolve().parents[1]
BACKGROUND_FLAGS = [
    "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding", "--disable-features=CalculateNativeWinOcclusion",
    "--force-renderer-accessibility=complete",
]


def loopback_address(value: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.port is None or not 1 <= parsed.port <= 65535
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("--cdp-url must be an HTTP loopback URL with an explicit port") from None
    return "127.0.0.1", parsed.port


def launcher_values() -> dict[str, str]:
    """Read only launcher paths; never execute shell code or expose credentials."""
    allowed = {"BROWSER_CMD", "CHROME_USER_DATA_DIR", "PROFILE_ANCHOR_URL"}
    values = {key: value for key, value in os.environ.items() if key in allowed}
    env_file = ROOT / "config" / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.strip().removeprefix("export ").partition("=")
            if separator and key.strip() in allowed:
                value = value.strip()
                if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key.strip()] = value
    return values


def selected_profiles(manifest: Path, user_data: Path) -> list[dict]:
    rows = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("An explicit nonempty selected-profile manifest is required")
    ids, directories = set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Selected profile is invalid")
        profile_id, directory = row.get("id"), row.get("chrome_directory")
        if (not isinstance(profile_id, str) or not profile_id.strip() or profile_id in ids
            or not isinstance(directory, str) or not directory.strip() or directory in {".", ".."}
            or any(character in directory for character in ("/", "\\", "\0", ":"))
            or directory in directories):
            raise ValueError("Selected profile IDs and exact Chrome directories must be unique")
        profile = user_data / directory
        if (not profile.is_dir() or profile.is_symlink()
            or profile.resolve().parent != user_data.resolve()):
            raise ValueError(f"Selected saved Chrome profile is unavailable: {profile_id}")
        ids.add(profile_id)
        directories.add(directory)
    return rows


def chrome_processes(binary: Path):
    """Return exact executable/current-user processes, retaining psutil identities."""
    import psutil
    result = []
    for process in psutil.process_iter(["name", "exe"], ad_value=None):
        row = process.info
        if str(row.get("name") or "").casefold() != binary.name.casefold():
            continue
        try:
            if not process_is_current_user(process):
                continue
            if not row.get("exe"):
                raise ValueError("Chrome process executable is unreadable")
            if Path(row["exe"]).resolve() != binary.resolve():
                continue
            # Capture creation time now; psutil guards later signals from PID reuse.
            process.create_time()
            if not process.is_running():
                continue
            result.append(process)
        except psutil.NoSuchProcess:
            continue
    return result


def close_chrome_windows(processes) -> None:
    """Request Chrome's normal exit using only windows of verified Chrome PIDs."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    verified = {process.pid: process for process in processes}
    failures = []

    @callback_type
    def close(window, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
        process = verified.get(pid.value)
        try:
            if process is not None and process.is_running() and process_is_current_user(process):
                if not user32.PostMessageW(window, 0x0010, 0, 0):  # WM_CLOSE
                    raise OSError(ctypes.get_last_error(), "Chrome close request failed")
        except Exception as exc:
            failures.append(exc)
            return False
        return True

    completed = user32.EnumWindows(close, 0)
    if failures:
        raise failures[0]
    if not completed:
        raise OSError(ctypes.get_last_error(), "Chrome window enumeration failed")


def stop_chrome(binary: Path, *, force: bool) -> None:
    processes = chrome_processes(binary)
    if processes:
        close_chrome_windows(processes)
    for force_kill, duration in ((None, 10), (False, 5), (True, 5)):
        if force_kill is not None:
            if not force:
                break
            for process in chrome_processes(binary):
                # Guard again immediately before process-tree signaling.
                if process.is_running() and process_is_current_user(process):
                    terminate_process_tree(process, force=force_kill)
        deadline = time.monotonic() + duration
        while chrome_processes(binary) and time.monotonic() < deadline:
            time.sleep(0.5)
        if not chrome_processes(binary):
            return
    if chrome_processes(binary):
        raise RuntimeError("Chrome is still running; quit it manually and retry")


def limit_restoration(user_data: Path, first: str) -> None:
    path = user_data / "Local State"
    if not path.exists():
        return
    if path.is_symlink():
        raise ValueError("Chrome Local State is unavailable")
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("profile", {}), dict):
        raise ValueError("Chrome restoration state is invalid")
    profile = state.setdefault("profile", {})
    profile["last_active_profiles"], profile["last_used"] = [first], first
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=user_data,
                                         prefix=".logged-in-launch-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream, separators=(",", ":"))
            stream.flush()
            sync_file(stream.fileno())
        publish_complete(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def anchor_url(base: str, directory: str) -> str:
    if "://" not in base and not base.startswith(("about:", "data:")):
        base = Path(base).resolve().as_uri()
    return f"{base.split('#', 1)[0]}#citeup-profile={quote(directory, safe='')}"


def open_profile(binary: Path, user_data: Path, directory: str, anchor: str,
                 address: tuple[str, int] | None) -> None:
    command = [str(binary), f"--user-data-dir={user_data}", f"--profile-directory={directory}",
               "--no-first-run", "--no-default-browser-check", "--disable-popup-blocking", *BACKGROUND_FLAGS]
    if address is not None:
        command.extend([f"--remote-debugging-address={address[0]}", f"--remote-debugging-port={address[1]}"])
    command.extend(["--new-window", anchor_url(anchor, directory)])
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 7  # SW_SHOWMINNOACTIVE: request a minimized launch without activation.
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, startupinfo=startup, **popen_session_kwargs(persistent=True))


def wait_debug(address: tuple[str, int]) -> None:
    endpoint = f"http://{address[0]}:{address[1]}/json/version"
    for _ in range(30):
        try:
            with urlopen(endpoint, timeout=2):
                return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("Timed out waiting for Chrome's debug port")


def launch_windows(arguments: argparse.Namespace) -> None:
    address = loopback_address(arguments.cdp_url)
    values = launcher_values()
    binary = Path(os.path.expandvars(values.get("BROWSER_CMD") or str(chrome_binary()))).expanduser()
    user_data = Path(os.path.expandvars(arguments.user_data_dir or values.get("CHROME_USER_DATA_DIR")
                                       or str(chrome_user_data_dir()))).expanduser()
    if not binary.is_absolute() or not binary.is_file() or not user_data.is_absolute():
        raise ValueError("Chrome and its saved user-data directory must be available absolute paths")
    rows = selected_profiles(Path(arguments.profiles_path), user_data)
    directories = [row["chrome_directory"] for row in rows]
    if arguments.clean_start:
        prepare(user_data, directories, validate_only=True)
    anchor = values.get("PROFILE_ANCHOR_URL") or str(ROOT / "config" / "profile_anchor.html")
    environment = {"CHROME_USER_DATA_DIR": str(user_data), "CDP_URL": arguments.cdp_url}
    guard = (nullcontext() if os.environ.get(LOCK_HELD_ENV) == "1"
             else exclusive_browser_lifecycle(environment=environment))
    with guard:
        stop_chrome(binary, force=arguments.force_close)
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            (user_data / name).unlink(missing_ok=True)
        if arguments.clean_start:
            prepare(user_data, directories)
        limit_restoration(user_data, directories[0])
        open_profile(binary, user_data, directories[0], anchor, address)
        wait_debug(address)
        for directory in directories[1:]:
            open_profile(binary, user_data, directory, anchor, None)
            time.sleep(0.6)
    print(f"Selected Chrome identity windows ready: {len(rows)}. Debug: http://{address[0]}:{address[1]}")


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if sys.platform == "darwin":
        return subprocess.run(["/bin/bash", str(ROOT / "config" / "launch_logged_in.sh"), *arguments], check=False).returncode
    if sys.platform != "win32":
        print("Saved-profile launching supports macOS and Windows", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles-path", required=True)
    parser.add_argument("--cdp-url", required=True)
    parser.add_argument("--user-data-dir")
    parser.add_argument("--clean-start", action="store_true")
    parser.add_argument("--force-close", action="store_true")
    try:
        launch_windows(parser.parse_args(arguments))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Chrome startup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
