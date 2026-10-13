"""Explicit credential references using offline stores and subprocess doubles."""
from __future__ import annotations

import asyncio
import codecs
import sys
from types import SimpleNamespace

import pytest

from platform_runtime import credentials


@pytest.mark.parametrize("reference,valid", [
    ("keychain:service:account", True),
    ("keychain:service:account:part", True),
    ("keychain::account", False),
    ("credential-manager:exact:target", True),
    ("credential-manager: target with spaces ", True),
    ("credential-manager:", False),
    ("credential-manager:target\r", False),
    ("credential-manager:target\n", False),
    ("credential-manager:target\x00", False),
    ("env:EXPLICIT_TOKEN", False),
    (None, False),
])
def test_store_reference_validation_is_os_independent(reference, valid):
    assert credentials.valid_store_reference(reference) is valid


def test_environment_lookup_reads_only_the_explicit_name(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ambient-must-not-be-used")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-must-not-be-used")
    monkeypatch.delenv("EXPLICIT_OFFLINE_TOKEN", raising=False)
    assert asyncio.run(credentials.resolve_reference("env:EXPLICIT_OFFLINE_TOKEN")) == ""
    monkeypatch.setenv("EXPLICIT_OFFLINE_TOKEN", "  fixture-token\n")
    assert asyncio.run(credentials.resolve_reference("env:EXPLICIT_OFFLINE_TOKEN")) == "fixture-token"


@pytest.mark.parametrize("platform,reference", [
    ("darwin", "credential-manager:explicit-target"),
    ("win32", "keychain:service:account"),
    ("win32", "credential-manager:"),
    ("win32", "credential-manager:target\nother"),
    ("win32", "credential-manager:target\x00other"),
    ("darwin", "keychain::account"),
    ("darwin", "env:BAD=NAME"),
    ("darwin", ""),
])
def test_invalid_or_wrong_platform_reference_never_accesses_a_store(monkeypatch, platform, reference):
    monkeypatch.setattr(credentials, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(credentials, "_windows_credential", lambda *_args: pytest.fail("store read"))
    async def forbid_process(*_args, **_kwargs):
        pytest.fail("store process")
    monkeypatch.setattr(credentials.asyncio, "create_subprocess_exec", forbid_process)
    with pytest.raises(credentials.CredentialLookupError) as refused:
        asyncio.run(credentials.resolve_reference(reference))
    assert refused.value.code == "reference_invalid"


class KeychainProcess:
    def __init__(self, *, output=b" fixture-token\n", returncode=0, error=None):
        self.output, self.returncode, self.error = output, returncode, error
        self.killed, self.waited = False, False

    async def communicate(self):
        if self.error is not None:
            raise self.error()
        return self.output, None

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waited = True


def keychain_double(monkeypatch, process):
    calls, deadlines = [], []
    monkeypatch.setattr(credentials, "sys", SimpleNamespace(platform="darwin"))
    async def launch(*args, **kwargs):
        calls.append((args, kwargs))
        return process
    original_wait_for = asyncio.wait_for
    async def wait_for(awaitable, deadline):
        deadlines.append(deadline)
        return await original_wait_for(awaitable, deadline)
    monkeypatch.setattr(credentials.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(credentials.asyncio, "wait_for", wait_for)
    return calls, deadlines


@pytest.mark.parametrize("returncode,expected", [(0, "fixture-token"), (1, "")])
def test_keychain_preserves_exact_command_and_deadline(monkeypatch, returncode, expected):
    process = KeychainProcess(returncode=returncode)
    calls, deadlines = keychain_double(monkeypatch, process)
    assert asyncio.run(credentials.resolve_reference("keychain:exact-service:exact:account")) == expected
    assert calls == [(("/usr/bin/security", "find-generic-password", "-s", "exact-service",
                      "-a", "exact:account", "-w"),
                     {"stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.DEVNULL})]
    assert deadlines == [15]
    assert not process.killed and not process.waited


@pytest.mark.parametrize("error", [asyncio.TimeoutError, asyncio.CancelledError])
def test_keychain_timeout_and_cancellation_kill_and_reap(monkeypatch, error):
    process = KeychainProcess(error=error)
    _calls, deadlines = keychain_double(monkeypatch, process)
    with pytest.raises(credentials.CredentialLookupError) as refused:
        asyncio.run(credentials.resolve_reference("keychain:service:account"))
    assert refused.value.code == "unavailable"
    assert process.killed and process.waited and deadlines == [15]


@pytest.mark.parametrize("blob", [
    b"fixture-token",
    codecs.BOM_UTF8 + b"fixture-token",
    "fixture-token".encode("utf-16-le"),
    codecs.BOM_UTF16_LE + "fixture-token".encode("utf-16-le"),
    codecs.BOM_UTF16_BE + "fixture-token".encode("utf-16-be"),
    "fixture-token",
])
def test_windows_reads_only_the_named_generic_item(monkeypatch, blob):
    monkeypatch.setattr(credentials, "sys", SimpleNamespace(platform="win32"))
    calls, deadlines = [], []
    def read(*args):
        calls.append(args)
        return {"CredentialBlob": blob}
    monkeypatch.setitem(sys.modules, "win32cred", SimpleNamespace(CRED_TYPE_GENERIC=42, CredRead=read))
    original_wait_for = asyncio.wait_for
    async def wait_for(awaitable, deadline):
        deadlines.append(deadline)
        return await original_wait_for(awaitable, deadline)
    monkeypatch.setattr(credentials.asyncio, "wait_for", wait_for)
    assert asyncio.run(credentials.resolve_reference("credential-manager:exact:target")) == "fixture-token"
    assert calls == [("exact:target", 42, 0)] and deadlines == [15]


@pytest.mark.parametrize("blob", [b"\xff", b"\x00a\x00b", b"a\x00b", b"token\x00value", "token\x00value", None])
def test_windows_ambiguous_or_malformed_blob_is_refused(blob):
    with pytest.raises(credentials.CredentialLookupError) as refused:
        credentials._decode_windows_blob(blob)
    assert refused.value.code == "invalid"


def test_windows_store_errors_are_redacted(monkeypatch):
    def read(*_args):
        raise OSError("fixture-secret-and-private-account")
    monkeypatch.setitem(sys.modules, "win32cred", SimpleNamespace(CRED_TYPE_GENERIC=1, CredRead=read))
    with pytest.raises(credentials.CredentialLookupError) as refused:
        credentials._windows_credential("private-target")
    assert str(refused.value) == "unavailable" and refused.value.__suppress_context__


@pytest.mark.parametrize("error", [asyncio.TimeoutError, asyncio.CancelledError])
def test_windows_lookup_timeout_and_cancellation_are_redacted(monkeypatch, error):
    monkeypatch.setattr(credentials, "sys", SimpleNamespace(platform="win32"))
    async def read_thread(function, target):
        assert function is credentials._windows_credential and target == "exact-target"
        raise error()
    monkeypatch.setattr(credentials.asyncio, "to_thread", read_thread)
    with pytest.raises(credentials.CredentialLookupError) as refused:
        asyncio.run(credentials.resolve_reference("credential-manager:exact-target"))
    assert refused.value.code == "unavailable"
