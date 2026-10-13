"""OS identity and signalling; admission and recovery policy stay with callers."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from contextlib import contextmanager

_windows_boot = None
_boot_guard = threading.Lock()


def boot_identity():
    global _windows_boot
    try:
        if sys.platform.startswith("linux"):
            return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
        if sys.platform == "darwin":
            result = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
                                    capture_output=True, text=True, timeout=2)
            value = result.stdout.strip()
            return "boot-session:" + value if result.returncode == 0 and value else None
        if sys.platform == "win32":
            with _boot_guard:
                if _windows_boot is None:
                    # WMI's stored boot timestamp is stable across wall-clock
                    # adjustments. Never infer a boot identifier from a PID or
                    # subtract uptime from the current clock.
                    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                               "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')"]
                    result = subprocess.run(command, capture_output=True, text=True, timeout=5,
                                            creationflags=subprocess.CREATE_NO_WINDOW)
                    value = result.stdout.strip()
                    from datetime import datetime
                    if result.returncode == 0 and value:
                        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                        if parsed.tzinfo is not None:
                            _windows_boot = "windows-boot:" + value
                return _windows_boot
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return None
    return None


def current_user_key():
    if sys.platform != "win32":
        return str(os.getuid())
    import win32api
    import win32con
    import win32security
    from .native_errors import windows_errors
    with windows_errors():
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
            return win32security.ConvertSidToStringSid(sid)
        finally:
            token.Close()


def process_is_current_user(process):
    if sys.platform != "win32":
        return process.uids().real == os.getuid()
    import psutil
    return process.username().casefold() == psutil.Process(os.getpid()).username().casefold()


def popen_session_kwargs(*, persistent=False):
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        if persistent:
            flags |= subprocess.CREATE_BREAKAWAY_FROM_JOB
        return {"creationflags": flags}
    return {"start_new_session": True}


@contextmanager
def _process_errors():
    import psutil
    try:
        yield
    except psutil.NoSuchProcess:
        raise ProcessLookupError("process is no longer running") from None
    except psutil.AccessDenied:
        raise PermissionError("process identity or signalling is inaccessible") from None
    except psutil.Error as exc:
        raise OSError(str(exc)) from None


def _verified_process(pid, *, expected_created=None, expected_executable=None):
    import psutil
    with _process_errors():
        process = pid if isinstance(pid, psutil.Process) else psutil.Process(pid)
        if (expected_created is not None and process.create_time() != expected_created
                or expected_executable is not None and os.path.normcase(process.exe()) != os.path.normcase(expected_executable)):
            raise ProcessLookupError("process identity changed before signalling")
        if not process.is_running():
            raise ProcessLookupError("process is no longer running")
    return process


def signal_process(pid, *, force=False, expected_created=None, expected_executable=None):
    if sys.platform != "win32":
        os.kill(getattr(pid, "pid", pid), signal.SIGKILL if force else signal.SIGTERM)
        return
    with _process_errors():
        process = _verified_process(pid, expected_created=expected_created, expected_executable=expected_executable)
        (process.kill if force else process.terminate)()


def terminate_process_tree(pid, *, force=False, expected_created=None, expected_executable=None):
    if sys.platform != "win32":
        os.killpg(getattr(pid, "pid", pid), signal.SIGKILL if force else signal.SIGTERM)
        return
    import psutil
    with _process_errors():
        parent = _verified_process(pid, expected_created=expected_created, expected_executable=expected_executable)
        # Captured Process objects detect PID reuse before signalling.
        descendants = parent.children(recursive=True)
        for process in reversed(descendants):
            try:
                (process.kill if force else process.terminate)()
            except psutil.NoSuchProcess:
                pass
        (parent.kill if force else parent.terminate)()


def isolated_environment_paths(home, cache, temporary):
    values = {"HOME": str(home), "XDG_CONFIG_HOME": str(Path(home) / ".config"),
              "XDG_CACHE_HOME": str(cache), "TMPDIR": str(temporary)}
    if sys.platform == "win32":
        values.update(USERPROFILE=str(home), TEMP=str(temporary), TMP=str(temporary))
    return values


class OwnedProcessGroup:
    """OS child containment; closing the lease reaps a dead parent's children."""
    def __init__(self, process, *, allow_breakaway=False):
        self.handle = None
        if sys.platform != "win32":
            return
        import win32api
        import win32con
        import win32job
        from .native_errors import os_error
        job = None
        try:
            job = win32job.CreateJobObject(None, None)
            limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            flags = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if allow_breakaway:
                flags |= win32job.JOB_OBJECT_LIMIT_BREAKAWAY_OK
            limits["BasicLimitInformation"]["LimitFlags"] = flags
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
            # Popen already owns the exact native process handle. Reopening
            # by PID would add an avoidable process-reuse race.
            native = process
            transport = getattr(process, "_transport", None)
            if transport is not None:
                native = transport.get_extra_info("subprocess") or process
            handle = getattr(native, "_handle", None)
            opened = handle is None
            if opened:
                target = _verified_process(getattr(process, "pid", process))
                handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE,
                                              False, target.pid)
            try:
                win32job.AssignProcessToJobObject(job, handle)
            except BaseException as failure:
                if opened:
                    try:
                        handle.Close()
                    except Exception as cleanup:
                        failure.add_note(f"Owned process handle cleanup failed: {os_error(cleanup)}")
                raise
            else:
                if opened:
                    handle.Close()
            self.handle = job
        except BaseException as exc:
            if job is not None:
                try:
                    job.Close()
                except Exception as cleanup:
                    exc.add_note(f"Owned job cleanup failed: {os_error(cleanup)}")
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, OSError):
                raise
            raise os_error(exc) from exc

    def close(self):
        if self.handle is not None:
            handle, self.handle = self.handle, None
            try:
                handle.Close()
            except Exception as exc:
                from .native_errors import os_error
                raise os_error(exc) from exc


def own_process_group(process, *, allow_breakaway=False):
    return OwnedProcessGroup(process, allow_breakaway=allow_breakaway)
