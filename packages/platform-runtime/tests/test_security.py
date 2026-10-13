"""Native private paths plus strict Windows ownership/ACL observations."""
import os
import sys
from types import SimpleNamespace

import pytest

from platform_runtime import processes, security


class NativeError(Exception):
    winerror = 5


class ACL:
    def __init__(self, entries):
        self.entries = entries

    def GetAceCount(self):
        return len(self.entries)

    def GetAce(self, index):
        return self.entries[index]


@pytest.mark.parametrize("owner,entries,private", [
    ("current", [((0, 0), 1, "current")], True),
    ("other", [((0, 0), 1, "current")], False),
    ("current", None, False),
    ("current", [((0, 0), 1, "everyone")], False),
    ("current", [((0, 0), 1, "S-1-5-18"), ((0, 0), 1, "current")], True),
    ("current", [((5, 0), 1, "current")], False),
])
def test_windows_private_proof_requires_exact_owner_and_closed_acl(monkeypatch, tmp_path, owner, entries, private):
    path = tmp_path / "record"
    path.write_bytes(b"synthetic")
    descriptor = SimpleNamespace(GetSecurityDescriptorOwner=lambda: owner,
                                 GetSecurityDescriptorDacl=lambda: None if entries is None else ACL(entries))
    native = SimpleNamespace(SE_FILE_OBJECT=1, OWNER_SECURITY_INFORMATION=1,
        DACL_SECURITY_INFORMATION=4, ACCESS_ALLOWED_ACE_TYPE=0, ACCESS_DENIED_ACE_TYPE=1,
        GetNamedSecurityInfo=lambda name, kind, flags: descriptor,
        GetSecurityInfo=lambda handle, kind, flags: descriptor,
        ConvertSidToStringSid=lambda sid: sid)
    monkeypatch.setitem(sys.modules, "pywintypes", SimpleNamespace(error=NativeError))
    monkeypatch.setitem(sys.modules, "win32security", native)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: fd + 100))
    monkeypatch.setattr(security, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(processes, "current_user_key", lambda: "current")
    fd = os.open(path, os.O_RDONLY)
    try:
        assert security.is_owned_private(path, fd=fd) is private
    finally:
        os.close(fd)


@pytest.mark.parametrize("operation", ["read", "protect", "open"])
def test_windows_private_native_refusal_retains_shared_os_error_contract(monkeypatch, tmp_path, operation):
    path = tmp_path / "private"
    path.write_bytes(b"synthetic")
    def refuse(*_args):
        raise NativeError("synthetic refusal")
    monkeypatch.setattr(security, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setitem(sys.modules, "pywintypes", SimpleNamespace(error=NativeError))
    monkeypatch.setitem(sys.modules, "win32security", SimpleNamespace(
        OWNER_SECURITY_INFORMATION=1, DACL_SECURITY_INFORMATION=4, SE_FILE_OBJECT=1,
        GetNamedSecurityInfo=refuse))
    monkeypatch.setitem(sys.modules, "win32file", SimpleNamespace(CreateFile=refuse))
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(
        GENERIC_READ=1, OPEN_EXISTING=3, FILE_FLAG_OPEN_REPARSE_POINT=4,
        FILE_FLAG_BACKUP_SEMANTICS=8, FILE_SHARE_READ=1, FILE_SHARE_WRITE=2, FILE_SHARE_DELETE=4))
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace())
    with pytest.raises(PermissionError) as failure:
        if operation == "read":
            security.is_owned_private(path)
        elif operation == "protect":
            security.protect_private(path)
        else:
            security.open_no_follow(path, os.O_RDONLY)
    assert failure.value.winerror == 5 and failure.value.filename == str(path)


def test_windows_user_identity_refusal_closes_token_and_remains_an_os_error(monkeypatch):
    closed = []
    token = SimpleNamespace(Close=lambda: closed.append(True))
    def refuse(*_args):
        raise NativeError("synthetic refusal")
    monkeypatch.setattr(processes, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setitem(sys.modules, "pywintypes", SimpleNamespace(error=NativeError))
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace(GetCurrentProcess=lambda: "process"))
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(TOKEN_QUERY=8))
    monkeypatch.setitem(sys.modules, "win32security", SimpleNamespace(
        OpenProcessToken=lambda *_args: token, TokenUser=1, GetTokenInformation=refuse))
    with pytest.raises(PermissionError) as failure:
        processes.current_user_key()
    assert failure.value.winerror == 5 and closed == [True]


def test_opened_private_identity_cannot_be_replaced_by_a_different_file(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    security.protect_private(first)
    security.protect_private(second)
    fd = os.open(first, os.O_RDONLY)
    try:
        assert security.is_owned_private(first, fd=fd)
        assert not security.is_owned_private(second, fd=fd)
    finally:
        os.close(fd)
