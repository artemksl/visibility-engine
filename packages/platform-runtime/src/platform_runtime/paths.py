"""Machine-local paths; callers still freeze their explicit profile selection."""
import os
from pathlib import Path
import sys


def user_cache_dir():
    if sys.platform == "darwin":
        return Path.home() / "Library/Caches"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    return Path.home() / ".cache"


def chrome_user_data_dir():
    if sys.platform == "win32":
        return user_cache_dir() / "Google/Chrome/User Data"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Google/Chrome"
    return Path.home() / ".config/google-chrome"


def chrome_binary():
    candidates = []
    if sys.platform == "win32":
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(variable)
            if base:
                candidates.append(Path(base) / "Google/Chrome/Application/chrome.exe")
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe", 0,
                                        winreg.KEY_READ | view) as key:
                        candidates.append(Path(winreg.QueryValue(key, None)))
                except OSError:
                    continue
    elif sys.platform == "darwin":
        candidates = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                      Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        import shutil
        candidates = [Path(value) for name in ("google-chrome", "chromium", "chromium-browser")
                      if (value := shutil.which(name))]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError("Google Chrome executable was not found; configure its explicit path")
