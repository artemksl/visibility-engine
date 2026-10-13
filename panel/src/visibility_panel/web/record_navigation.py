"""Human inspection links, separate from runner-owned measurement automation.

This module never attaches to Chrome, reads a page, sends a prompt, or restarts
the browser. It opens one verified URL in one explicitly selected profile.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from platform_runtime.paths import chrome_binary, chrome_user_data_dir
from platform_runtime.processes import popen_session_kwargs, process_is_current_user
from platform_runtime.storage import publish_complete, sync_file

from visibility_panel.integrity import canonical_sha256
from visibility_panel.web.config import selected_snapshot_profile_rows


PLATFORM_URLS = {
    "chatgpt": "https://chatgpt.com/",
    "gemini": "https://gemini.google.com/app",
    "copilot": "https://copilot.com/",
    "google_ai_overview": "https://www.google.com/",
}
_CONVERSATION_REF = re.compile(r"conversations/[0-9a-f]{64}\Z")


class RecordNavigationError(ValueError):
    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


def latest_conversation_reference(item: dict) -> str | None:
    """The engine publishes correlation links in chronological ledger order."""
    return next((value for value in reversed(item.get("evidence_links") or ())
                 if isinstance(value, str) and _CONVERSATION_REF.fullmatch(value)), None)


def verified_profile_rows(runtime: dict, root: Path) -> dict[str, dict]:
    """Require frozen, explicit profile bindings and today's same ID/directory."""
    try:
        profiles = runtime["profiles"]
        selected = profiles["selected_profile_ids"]
        current = selected_snapshot_profile_rows(selected, root)
        frozen = profiles["selected_profiles"]
        manifest = Path(profiles["manifest_path"])
        if manifest.is_symlink():
            raise ValueError("profile manifest is a symlink")
        recorded = json.loads(manifest.read_text(encoding="utf-8"))
        if (recorded != frozen or canonical_sha256(recorded) != profiles["manifest_sha256"]
                or [row["id"] for row in frozen] != selected
                or [(row["id"], row["chrome_directory"]) for row in frozen]
                != [(row["id"], row["chrome_directory"]) for row in current]):
            raise ValueError("profile binding changed")
        for row in frozen:
            directory = row["chrome_directory"]
            if (not isinstance(directory, str) or not directory or directory in {".", ".."}
                    or "/" in directory or "\\" in directory or "\0" in directory):
                raise ValueError("invalid profile directory")
        return {row["id"]: row for row in frozen}
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise RecordNavigationError("profile_unavailable", "The saved selected profile cannot be verified.") from exc


def conversation_url(link: dict, platform: str) -> str:
    """Accept only engine-resolved chat URLs on the record's actual provider."""
    value = link.get("url")
    try:
        if link.get("kind") != "conversation" or not isinstance(value, str) or value != value.strip():
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.query or parsed.fragment):
            raise ValueError
        chatgpt = platform == "chatgpt" and parsed.hostname == "chatgpt.com" and re.fullmatch(
            r"/c/[A-Za-z0-9_-]{1,200}|/g/[A-Za-z0-9_-]{1,200}/c/[A-Za-z0-9_-]{1,200}", parsed.path)
        gemini = platform in {"gemini", "google_ai_overview"} and parsed.hostname == "gemini.google.com" and re.fullmatch(
            r"/app/[A-Za-z0-9_-]{1,200}", parsed.path)
        copilot = platform == "copilot" and parsed.hostname in {"copilot.com", "copilot.microsoft.com"} and re.fullmatch(
            r"/chats/[A-Za-z0-9_-]{1,200}", parsed.path)
        if not (chatgpt or gemini or copilot):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise RecordNavigationError("conversation_unavailable", "The saved chat link cannot be verified. Refresh the results and try again.") from exc
    return value


def _browser_paths(root: Path, runtime: dict) -> tuple[Path, Path]:
    # Read only the two non-secret launcher settings; never source shell code.
    values = {key: os.environ[key] for key in ("BROWSER_CMD", "CHROME_USER_DATA_DIR") if key in os.environ}
    env_file = root / "config" / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and key.strip() in {"BROWSER_CMD", "CHROME_USER_DATA_DIR"}:
                value = value.strip()
                if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key.strip()] = value
    # The authorized measurement shell launcher sources .env over inherited
    # values. Mirror that precedence so a directory name cannot select an
    # account in a different Chrome user-data root.
    browser = runtime.get("browser") or {}
    try:
        binary = values.get("BROWSER_CMD") or str(chrome_binary())
        user_data = browser.get("user_data_dir") or browser.get("chrome_user_data_dir") or values.get("CHROME_USER_DATA_DIR") or str(chrome_user_data_dir())
    except (OSError, RuntimeError) as exc:
        raise RecordNavigationError("browser_unavailable", "Chrome's configured location cannot be verified.") from exc
    paths = tuple(Path(str(value).replace("${HOME}", str(Path.home())).replace("$HOME", str(Path.home()))).expanduser() for value in (binary, user_data))
    if any(not path.is_absolute() for path in paths):
        raise RecordNavigationError("browser_unavailable", "Chrome's configured location cannot be verified.")
    for explicit, actual in ((browser.get("chrome_binary"), paths[0]),
                             (browser.get("user_data_dir") or browser.get("chrome_user_data_dir"), paths[1])):
        if explicit and Path(str(explicit)).expanduser().resolve() != actual.resolve():
            raise RecordNavigationError("browser_unavailable", "The saved and current Chrome locations do not match.")
    return paths


def _chrome_running(binary: Path, user_data: Path) -> bool:
    """Inspect exact argv without attaching to Chrome or reading any page."""
    try:
        import psutil
        for process in psutil.process_iter(attrs=["name", "exe", "cmdline"], ad_value=None):
            row = process.info
            executable = row.get("exe")
            if row.get("name") != binary.name and executable != str(binary):
                continue
            if not process_is_current_user(process):
                continue
            if not executable or not row.get("cmdline"):
                raise ValueError("Chrome process identity is unreadable")
            args = row["cmdline"]
            directories = []
            for index, arg in enumerate(args):
                if arg.startswith("--user-data-dir="):
                    directories.append(arg.split("=", 1)[1])
                elif arg == "--user-data-dir":
                    directories.append(args[index + 1])
            if len(directories) > 1:
                raise ValueError("ambiguous Chrome directory")
            directory = Path(directories[0]).expanduser() if directories else chrome_user_data_dir()
            if directory.resolve() == user_data.resolve():
                if Path(executable).resolve() != binary.resolve():
                    raise ValueError("another Chrome executable owns the selected directory")
                return True
        return False
    except Exception as exc:
        raise RecordNavigationError("browser_unavailable", "Chrome's running profile location cannot be verified.") from exc


def _prepare_cold_start(user_data: Path, directory: str) -> None:
    """Limit Chrome's automatic startup restore to the selected profile only."""
    state_path = user_data / "Local State"
    temporary = None
    try:
        if state_path.is_symlink() or not state_path.is_file():
            raise ValueError("Chrome Local State is unavailable")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        profile = state.get("profile")
        if (not isinstance(profile, dict) or not isinstance(profile.get("last_active_profiles"), list)
                or not all(isinstance(value, str) for value in profile["last_active_profiles"])
                or not isinstance(profile.get("last_used"), str)):
            raise ValueError("Chrome restoration state is unavailable")
        profile["last_active_profiles"] = [directory]
        profile["last_used"] = directory
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=user_data,
                                         prefix=".record-inspection-", delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, state_path.stat().st_mode & 0o777)
            json.dump(state, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            sync_file(stream.fileno())
        publish_complete(temporary, state_path)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise RecordNavigationError("browser_unavailable", "Chrome's saved startup profiles cannot be verified. Open the selected profile manually and try again.") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def launch_inspection(*, root: Path, runtime: dict, profile: dict, url: str) -> None:
    """Caller holds the exclusive lifecycle lease; no automation is performed."""
    binary, user_data = _browser_paths(root, runtime)
    profile_path = user_data / profile["chrome_directory"]
    if (not binary.is_file() or not os.access(binary, os.X_OK) or not profile_path.is_dir()
            or profile_path.is_symlink() or profile_path.resolve().parent != user_data.resolve()):
        raise RecordNavigationError("browser_unavailable", "Chrome or the saved profile directory is unavailable on this computer.")
    if not _chrome_running(binary, user_data):
        _prepare_cold_start(user_data, profile["chrome_directory"])
    try:
        process = subprocess.Popen(
            [str(binary), f"--user-data-dir={user_data}", f"--profile-directory={profile['chrome_directory']}",
             "--new-window", url],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **popen_session_kwargs(persistent=True),
        )
        # Existing Chrome hands the URL to its process and exits. A newly
        # opened Chrome stays running; neither case needs a browser connection.
        try:
            code = process.wait(timeout=0.3)
        except subprocess.TimeoutExpired:
            return
        if code != 0:
            raise OSError("Chrome refused navigation")
    except OSError as exc:
        raise RecordNavigationError("browser_open_failed", "Chrome could not open this record. Check that the selected profile is available and try again.") from exc
