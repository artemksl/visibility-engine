"""Offline OS primitives: real local locks and strict Win32 API doubles."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from platform_runtime import file_locks, processes, storage


def test_independent_processes_observe_real_shared_and_exclusive_leases(tmp_path):
    target = tmp_path / "lease.lock"
    fd = os.open(target, os.O_RDWR | os.O_CREAT, 0o600)
    source = Path(file_locks.__file__).resolve().parents[1]
    script = """import os, sys
from platform_runtime import file_locks
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    file_locks.flock(fd, int(sys.argv[2]) | file_locks.LOCK_NB)
except BlockingIOError:
    sys.exit(42)
else:
    file_locks.flock(fd, file_locks.LOCK_UN)
finally:
    os.close(fd)
"""
    environment = dict(os.environ, PYTHONPATH=str(source) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    def acquire(mode):
        return subprocess.run([sys.executable, "-c", script, str(target), str(mode)],
            env=environment, timeout=10, capture_output=True, text=True)
    try:
        file_locks.flock(fd, file_locks.LOCK_SH)
        assert acquire(file_locks.LOCK_SH).returncode == 0
        blocked = acquire(file_locks.LOCK_EX)
        assert blocked.returncode == 42, blocked.stderr
        file_locks.flock(fd, file_locks.LOCK_EX)
        blocked = acquire(file_locks.LOCK_SH)
        assert blocked.returncode == 42, blocked.stderr
        file_locks.flock(fd, file_locks.LOCK_UN)
        free = acquire(file_locks.LOCK_EX)
        assert free.returncode == 0, free.stderr
    finally:
        os.close(fd)


class WinError(Exception):
    def __init__(self, code):
        super().__init__(code, "synthetic Win32 refusal")
        self.winerror = code


@pytest.fixture
def win_locks(monkeypatch, tmp_path):
    class Overlapped:
        pass
    calls, responses, unlock_responses, handle = [], [], [], [501]
    def lock(native, flags, low, high, overlap):
        assert native == handle[0]
        assert flags in {0, 1, 2, 3}
        assert (low, high) == (0xFFFFFFFF, 0xFFFFFFFF)
        assert isinstance(overlap, Overlapped)
        calls.append(("lock", native, flags, overlap))
        if responses:
            response = responses.pop(0)
            if response is not None:
                raise WinError(response)
    def unlock(native, low, high, overlap):
        assert native == handle[0] and (low, high) == (0xFFFFFFFF, 0xFFFFFFFF)
        assert isinstance(overlap, Overlapped)
        calls.append(("unlock", native, overlap))
        if unlock_responses:
            response = unlock_responses.pop(0)
            if response is not None:
                raise WinError(response)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda _fd: handle[0]))
    monkeypatch.setitem(sys.modules, "pywintypes", SimpleNamespace(error=WinError, OVERLAPPED=Overlapped))
    monkeypatch.setitem(sys.modules, "win32file", SimpleNamespace(LockFileEx=lock, UnlockFileEx=unlock))
    file_locks._held.clear()
    fd = os.open(tmp_path / "windows-fake.lock", os.O_CREAT | os.O_RDWR, 0o600)
    yield SimpleNamespace(fd=fd, calls=calls, responses=responses,
        unlock_responses=unlock_responses, handle=handle)
    file_locks._held.clear()
    os.close(fd)


def test_win32_lock_uses_full_range_and_propagates_unknown_errors(win_locks):
    case = win_locks
    file_locks._windows_flock(case.fd, file_locks.LOCK_EX | file_locks.LOCK_NB)
    assert case.calls[0][2] == 3
    file_locks._windows_flock(case.fd, file_locks.LOCK_UN)
    assert [call[0] for call in case.calls] == ["lock", "unlock"]
    case.responses[:] = [9876]
    with pytest.raises(OSError) as refusal:
        file_locks._windows_flock(case.fd, file_locks.LOCK_SH | file_locks.LOCK_NB)
    assert refusal.value.winerror == 9876 and refusal.value.errno == errno.EIO
    assert case.fd not in file_locks._held


def test_win32_contention_does_not_authorize_an_unlocked_caller(win_locks):
    case = win_locks
    case.responses[:] = [33]
    with pytest.raises(BlockingIOError) as failure:
        file_locks._windows_flock(case.fd, file_locks.LOCK_EX | file_locks.LOCK_NB)
    assert failure.value.winerror == 33
    assert case.fd not in file_locks._held


def test_windows_native_access_denial_is_an_os_error_for_existing_callers(win_locks):
    case = win_locks
    case.responses[:] = [5]
    with pytest.raises(PermissionError) as acquire:
        file_locks._windows_flock(case.fd, file_locks.LOCK_EX | file_locks.LOCK_NB)
    assert acquire.value.winerror == 5 and case.fd not in file_locks._held
    file_locks._windows_flock(case.fd, file_locks.LOCK_EX | file_locks.LOCK_NB)
    original = file_locks._held[case.fd]
    case.unlock_responses[:] = [5]
    with pytest.raises(PermissionError) as release:
        file_locks._windows_flock(case.fd, file_locks.LOCK_UN)
    assert release.value.winerror == 5 and file_locks._held[case.fd] == original


def test_failed_promotion_restores_original_shared_lease_before_refusal(win_locks):
    case = win_locks
    file_locks._windows_flock(case.fd, file_locks.LOCK_SH | file_locks.LOCK_NB)
    retained = file_locks._held[case.fd]
    case.responses[:] = [33, None]
    with pytest.raises(BlockingIOError):
        file_locks._windows_flock(case.fd, file_locks.LOCK_EX | file_locks.LOCK_NB)
    assert file_locks._held[case.fd] == retained
    assert [call[0] for call in case.calls] == ["lock", "unlock", "lock", "lock"]
    assert case.calls[-1][2:] == (0, retained[2])


def test_failed_promotion_restoration_is_reported_without_inventing_a_lease(win_locks):
    case = win_locks
    file_locks._windows_flock(case.fd, file_locks.LOCK_SH)
    case.responses[:] = [33, 5]
    with pytest.raises(PermissionError) as failure:
        file_locks._windows_flock(case.fd, file_locks.LOCK_EX | file_locks.LOCK_NB)
    assert failure.value.winerror == 5
    assert case.fd not in file_locks._held


def test_reused_descriptor_never_unlocks_a_previous_native_handle(win_locks):
    case = win_locks
    file_locks._windows_flock(case.fd, file_locks.LOCK_EX)
    case.handle[0] += 1
    file_locks._windows_flock(case.fd, file_locks.LOCK_SH)
    assert [call[0] for call in case.calls] == ["lock", "lock"]
    assert file_locks._held[case.fd][0][0] == case.handle[0]


def test_same_descriptor_handle_and_inode_cache_requires_real_lock_after_not_locked(win_locks):
    case = win_locks
    file_locks._windows_flock(case.fd, file_locks.LOCK_SH)
    old = file_locks._held[case.fd]
    # The OS can reuse every identifier after close/reopen. Native refusal,
    # rather than the cache signature, determines whether the lease exists.
    case.unlock_responses[:] = [158]
    file_locks._windows_flock(case.fd, file_locks.LOCK_SH | file_locks.LOCK_NB)
    current = file_locks._held[case.fd]
    assert current[0] == old[0] and current[2] is not old[2]
    assert [call[0] for call in case.calls] == ["lock", "unlock", "lock"]
    assert case.calls[-1][2] == 1


@pytest.mark.parametrize("exclusive,durable,flags", [(False, True, 9), (True, True, 8), (True, False, 0)])
def test_windows_publication_uses_write_through_and_no_overwrite_flags(monkeypatch, tmp_path, exclusive, durable, flags):
    source, destination = tmp_path / "complete.tmp", tmp_path / "published.json"
    source.write_text("complete")
    calls = []
    def move(first, second, native_flags):
        assert (first, second, native_flags) == (str(source), str(destination), flags)
        calls.append(native_flags)
    monkeypatch.setitem(sys.modules, "win32file", SimpleNamespace(MoveFileEx=move))
    monkeypatch.setattr(storage.sys, "platform", "win32")
    storage.publish_complete(source, destination, exclusive=exclusive, durable=durable)
    assert calls == [flags]


@pytest.mark.parametrize("error,kind", [(80, FileExistsError), (183, FileExistsError),
    (5, PermissionError), (9876, OSError)])
def test_windows_publish_errors_preserve_existing_evidence(monkeypatch, tmp_path, error, kind):
    source, destination = tmp_path / "complete.tmp", tmp_path / "published.json"
    source.write_text("new")
    destination.write_text("original")
    def move(*_args):
        raise WinError(error)
    monkeypatch.setitem(sys.modules, "win32file", SimpleNamespace(MoveFileEx=move))
    with pytest.raises(kind) as failure:
        storage._windows_move(source, destination, replace=False, durable=True)
    assert failure.value.winerror == error and failure.value.filename == str(destination)
    assert destination.read_text() == "original" and source.read_text() == "new"


def test_windows_cross_volume_publication_never_uses_copy_delete(monkeypatch, tmp_path):
    source, destination = tmp_path / "complete.tmp", tmp_path / "published.json"
    source.write_text("complete")
    original_stat = storage.os.stat
    def fake_stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        return SimpleNamespace(st_dev=result.st_dev + 1) if Path(path) == source else result
    monkeypatch.setattr(storage.os, "stat", fake_stat)
    monkeypatch.setitem(sys.modules, "win32file", SimpleNamespace(MoveFileEx=lambda *_args: pytest.fail("unsafe move")))
    with pytest.raises(OSError) as refusal:
        storage._windows_move(source, destination, replace=False, durable=True)
    assert refusal.value.errno == errno.EXDEV


def test_windows_removal_requires_write_through_rename_before_deletion(monkeypatch, tmp_path):
    target, calls = tmp_path / "owner.json", []
    target.write_text("owner")
    def move(source, destination, flags):
        assert source == str(target) and flags == 8
        assert Path(destination).parent == target.parent
        calls.append(destination)
        os.rename(source, destination)
    monkeypatch.setitem(sys.modules, "win32file", SimpleNamespace(MoveFileEx=move))
    monkeypatch.setattr(storage.sys, "platform", "win32")
    storage.remove_complete(target)
    assert len(calls) == 1 and not target.exists() and not Path(calls[0]).exists()


def test_windows_failed_removal_leaves_authoritative_name_and_propagates(monkeypatch, tmp_path):
    target = tmp_path / "owner.json"
    target.write_text("owner")
    def fail(*_args):
        raise WinError(5)
    monkeypatch.setitem(sys.modules, "win32file", SimpleNamespace(MoveFileEx=fail))
    monkeypatch.setattr(storage.sys, "platform", "win32")
    with pytest.raises(PermissionError) as failure:
        storage.remove_complete(target)
    assert failure.value.winerror == 5
    assert target.read_text() == "owner"
    with pytest.raises(OSError) as refusal:
        storage.sync_directory(tmp_path)
    assert refusal.value.errno == errno.ENOTSUP


class FakeProcess:
    all = {}
    calls = []
    def __new__(cls, pid):
        if pid not in cls.all:
            raise FakeNoSuchProcess(pid)
        return cls.all[pid]
    def create_time(self):
        return self.created
    def exe(self):
        return self.executable
    def is_running(self):
        return self.running
    def children(self, *, recursive):
        assert recursive is True
        self.calls.append((self.pid, "children"))
        return self.descendants
    def terminate(self):
        self.calls.append((self.pid, "terminate"))
        if self.gone:
            raise FakeNoSuchProcess(self.pid)
    def kill(self):
        self.calls.append((self.pid, "kill"))
        if self.gone:
            raise FakeNoSuchProcess(self.pid)


class FakePsutilError(Exception):
    pass


class FakeNoSuchProcess(FakePsutilError):
    pass


class FakeAccessDenied(FakePsutilError):
    pass


@pytest.fixture
def win_processes(monkeypatch):
    FakeProcess.all, FakeProcess.calls = {}, []
    for pid in (100, 101, 102):
        instance = object.__new__(FakeProcess)
        instance.pid, instance.created = pid, 1234.5
        instance.executable, instance.running, instance.gone = "C:/Python/python.exe", True, False
        instance.descendants = []
        FakeProcess.all[pid] = instance
    FakeProcess.all[100].descendants = [FakeProcess.all[101], FakeProcess.all[102]]
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(Process=FakeProcess,
        NoSuchProcess=FakeNoSuchProcess, AccessDenied=FakeAccessDenied, Error=FakePsutilError))
    monkeypatch.setattr(processes.sys, "platform", "win32")
    return FakeProcess


@pytest.mark.parametrize("created,executable", [(1, "C:/Python/python.exe"), (1234.5, "C:/Other/python.exe")])
def test_windows_signalling_rejects_changed_process_identity(win_processes, created, executable):
    with pytest.raises(ProcessLookupError, match="identity changed"):
        processes.terminate_process_tree(100, expected_created=created, expected_executable=executable)
    assert win_processes.calls == []


@pytest.mark.parametrize("force,action", [(False, "terminate"), (True, "kill")])
def test_windows_tree_cleanup_enumerates_before_child_first_termination(win_processes, force, action):
    win_processes.all[102].gone = True
    processes.terminate_process_tree(100, force=force, expected_created=1234.5,
        expected_executable="C:/Python/python.exe")
    assert win_processes.calls == [(100, "children"), (102, action), (101, action), (100, action)]


def test_windows_single_process_signal_preserves_force_and_identity(win_processes):
    processes.signal_process(100, force=True, expected_created=1234.5,
        expected_executable="C:/Python/python.exe")
    assert win_processes.calls == [(100, "kill")]


@pytest.mark.parametrize("assign_fails", [False, True])
@pytest.mark.parametrize("borrowed", [False, True, "asyncio"])
def test_windows_job_object_contains_children_and_releases_handles_on_failure(monkeypatch, win_processes, assign_fails, borrowed):
    calls = []
    class Handle:
        def __init__(self, name):
            self.name = name
        def Close(self):
            calls.append((self.name, "close"))
    job, process_handle = Handle("job"), Handle("process")
    def assign(actual_job, actual_process):
        assert actual_job is job and actual_process is process_handle
        calls.append(("job", "assign"))
        if assign_fails:
            raise WinError(5)
    def limits(actual_job, kind, value):
        assert actual_job is job and kind == "limits"
        assert value["BasicLimitInformation"]["LimitFlags"] == 0x2000 | 0x0800
        calls.append(("job", "limits"))
    def open_process(access, inherit, pid):
        assert (access, inherit, pid) == (0x0100 | 0x0001, False, 100)
        return process_handle
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace(OpenProcess=open_process))
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(PROCESS_SET_QUOTA=0x0100, PROCESS_TERMINATE=0x0001))
    monkeypatch.setitem(sys.modules, "win32job", SimpleNamespace(CreateJobObject=lambda *_args: job,
        QueryInformationJobObject=lambda *_args: {"BasicLimitInformation": {}},
        JobObjectExtendedLimitInformation="limits", JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=0x2000,
        JOB_OBJECT_LIMIT_BREAKAWAY_OK=0x0800, SetInformationJobObject=limits, AssignProcessToJobObject=assign))
    monkeypatch.setattr(processes.sys, "platform", "win32")
    process = SimpleNamespace(pid=100, _handle=process_handle) if borrowed else win_processes.all[100]
    if borrowed == "asyncio":
        def get_extra_info(key):
            assert key == "subprocess"
            return SimpleNamespace(pid=100, _handle=process_handle)
        process = SimpleNamespace(pid=100, _transport=SimpleNamespace(get_extra_info=get_extra_info))
    if assign_fails:
        with pytest.raises(PermissionError) as failure:
            processes.OwnedProcessGroup(process, allow_breakaway=True)
        assert failure.value.winerror == 5
    else:
        group = processes.OwnedProcessGroup(process, allow_breakaway=True)
        group.close()
        group.close()
    expected = [("job", "limits"), ("job", "assign")]
    if not borrowed:
        expected.append(("process", "close"))
    assert calls == [*expected, ("job", "close")]


def test_windows_job_creation_error_uses_the_existing_os_error_contract(monkeypatch):
    def create(*_args):
        raise WinError(5)
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "win32job", SimpleNamespace(CreateJobObject=create))
    monkeypatch.setattr(processes.sys, "platform", "win32")
    with pytest.raises(PermissionError) as failure:
        processes.OwnedProcessGroup(SimpleNamespace(pid=100))
    assert failure.value.winerror == 5


@pytest.mark.parametrize("failed_cleanup", ["job", "process"])
def test_windows_job_cleanup_reports_close_failure_without_masking_assignment(monkeypatch, failed_cleanup):
    calls = []
    def close():
        calls.append("job-close")
        if failed_cleanup == "job":
            raise WinError(6)
    def process_close():
        calls.append("process-close")
        raise WinError(6)
    def assign(*_args):
        raise WinError(5)
    job = SimpleNamespace(Close=close)
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace(
        OpenProcess=lambda *_args: SimpleNamespace(Close=process_close)))
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(PROCESS_SET_QUOTA=0x0100, PROCESS_TERMINATE=1))
    monkeypatch.setattr(processes, "_verified_process", lambda pid: SimpleNamespace(pid=pid))
    monkeypatch.setitem(sys.modules, "win32job", SimpleNamespace(CreateJobObject=lambda *_args: job,
        QueryInformationJobObject=lambda *_args: {"BasicLimitInformation": {}},
        JobObjectExtendedLimitInformation="limits", JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=0x2000,
        SetInformationJobObject=lambda *_args: None, AssignProcessToJobObject=assign))
    monkeypatch.setattr(processes.sys, "platform", "win32")
    process = SimpleNamespace(pid=100)
    if failed_cleanup == "job":
        process._handle = object()
    with pytest.raises(PermissionError) as failure:
        processes.OwnedProcessGroup(process)
    assert failure.value.winerror == 5
    assert calls == (["job-close"] if failed_cleanup == "job" else ["process-close", "job-close"])
    assert "cleanup failed" in failure.value.__cause__.__notes__[0].lower()


def test_windows_explicit_job_close_failure_is_normalized_without_double_close():
    calls = []
    def close():
        calls.append(True)
        raise WinError(6)
    group = object.__new__(processes.OwnedProcessGroup)
    group.handle = SimpleNamespace(Close=close)
    with pytest.raises(OSError) as failure:
        group.close()
    assert failure.value.winerror == 6
    group.close()
    assert calls == [True]
