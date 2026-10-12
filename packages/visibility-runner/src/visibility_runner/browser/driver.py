"""Small lazy driver boundary. Importing this module starts no browser runtime."""
from __future__ import annotations

import importlib
from importlib import metadata
from pathlib import Path
import ipaddress
import math
import subprocess
import sys
from urllib.parse import urlsplit

from ..errors import BrowserRuntimeError


class DriverConfigurationError(BrowserRuntimeError):
    """The selected driver or browser does not match its frozen declaration."""


def load_driver(config, *, importer=None):
    """Resolve exactly the declared distribution; never silently fall back."""
    name = str(config.driver)
    if name not in {"playwright", "patchright"}:
        raise DriverConfigurationError(f"unsupported browser driver: {name}")
    expected = config.driver_version
    if expected is not None:
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError as exc:
            raise DriverConfigurationError(f"selected browser driver {name} is not installed") from exc
        if actual != expected:
            raise DriverConfigurationError(
                f"browser driver version mismatch: expected {name} {expected}, installed {actual}"
            )
    try:
        return (importer or importlib.import_module)(f"{name}.async_api")
    except ImportError as exc:
        raise DriverConfigurationError(f"selected browser driver {name} is not available") from exc


def verify_browser_version(config, health):
    """Verify read-only CDP health before attachment and again before adoption."""
    expected = config.expected_browser_version
    if expected is None:
        return
    raw = health.get("browser") if isinstance(health, dict) else getattr(health, "browser", None)
    reachable = health.get("reachable") if isinstance(health, dict) else getattr(health, "reachable", False)
    actual = str(raw or "").partition("/")[2]
    if not reachable or actual != expected:
        raise DriverConfigurationError(
            f"browser version mismatch: expected {expected}, observed {raw or 'unavailable'}"
        )


def driver_diagnostics(config):
    """Declared capabilities, not claims about provider acceptance."""
    return {
        "driver": str(config.driver),
        "driver_version": config.driver_version,
        "expected_browser_version": config.expected_browser_version,
        "browser_console": "unavailable" if str(config.driver) == "patchright" else "available",
    }


def verify_experiment_process(config):
    """Require the local endpoint's declared profile root in actual launch args.

    This read-only check is additional to configuration validation. The engine
    does not acquire process ownership or gain permission to stop this process.
    """
    if not config.driver_experiment:
        return None
    import psutil

    port = urlsplit(config.cdp_url).port
    listener_pid = _listener_pid(port)
    root = Path(config.user_data_dir).resolve()
    try:
        # Use a new Process each time: process_iter/create_time caches can
        # otherwise conceal PID reuse across the two attachment checks.
        info = psutil.Process(listener_pid).as_dict(attrs=["pid", "cmdline", "create_time"])
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        raise DriverConfigurationError("experiment requires one verifiable dedicated browser process") from exc
    created = info.get("create_time")
    if isinstance(created, bool) or not isinstance(created, (int, float)) or not math.isfinite(created) or created <= 0:
        raise DriverConfigurationError("experiment browser process creation time is unavailable")
    flags = {}
    args = info.get("cmdline") or []
    for index, arg in enumerate(args):
        if not arg.startswith("--"):
            continue
        key, equals, value = arg.partition("=")
        if not equals and index + 1 < len(args) and not args[index + 1].startswith("--"):
            value = args[index + 1]
        flags[key] = value
    # Port 0 is browser-assigned; listener PID proof binds its actual socket.
    if flags.get("--remote-debugging-port") not in {str(port), "0"}:
        raise DriverConfigurationError("experiment listener is not the declared browser process")
    actual_root = flags.get("--user-data-dir")
    if not actual_root or Path(actual_root).resolve() != root:
        raise DriverConfigurationError("experiment endpoint process uses a different profile root")
    return {"pid": info["pid"], "create_time": info["create_time"], "user_data_dir": str(root)}


def _listener_pid(port):
    """Resolve a unique loopback listener, failing closed if ownership is hidden."""
    import psutil
    try:
        sockets = psutil.net_connections(kind="tcp")
        relevant = [item for item in sockets if item.status == psutil.CONN_LISTEN and item.laddr.port == port]
        if any(not ipaddress.ip_address(item.laddr.ip).is_loopback for item in relevant):
            raise DriverConfigurationError("experiment endpoint is not loopback-only")
        pids = {item.pid for item in relevant}
    except (psutil.AccessDenied, PermissionError):
        # macOS denies the system socket table to ordinary users. lsof can
        # inspect this user's listener without privilege escalation.
        if sys.platform != "darwin":
            raise DriverConfigurationError("experiment listener ownership is unavailable") from None
        try:
            result = subprocess.run(
                ["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-F", "pn"],
                capture_output=True, text=True, timeout=2, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DriverConfigurationError("experiment listener ownership is unavailable") from exc
        if result.returncode != 0:
            raise DriverConfigurationError("experiment listener ownership is unavailable")
        pids, current = set(), None
        for line in result.stdout.splitlines():
            if line.startswith("p"):
                try:
                    current = int(line[1:])
                except ValueError as exc:
                    raise DriverConfigurationError("experiment listener ownership is malformed") from exc
            elif line.startswith("n"):
                address = line[1:].rsplit(":", 1)[0].strip("[]")
                try:
                    local = ipaddress.ip_address(address).is_loopback
                except ValueError:
                    local = False
                if not local:
                    raise DriverConfigurationError("experiment endpoint is not loopback-only")
                pids.add(current)
    if len(pids) != 1 or None in pids:
        raise DriverConfigurationError("experiment requires one verifiable loopback listener")
    return next(iter(pids))
