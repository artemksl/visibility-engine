"""Temporary local storage only: no profiles, browser or credential access."""
import hashlib
import os
import sys
import uuid

import pytest

from automation_core.contracts import ExpectedScope
from automation_core.storage.locking import resource_lock
from automation_core.storage.memory import AccountMemory
from automation_core.storage.private_files import atomic_json, private_directory, read_json
from platform_runtime.security import is_owned_private, protect_private


def test_private_json_is_complete_and_exclusive_publication_preserves_prior_record(tmp_path):
    directory = tmp_path / "state"
    private_directory(directory)
    target = directory / "record.json"
    atomic_json(target, {"state": "send_started"}, exclusive=True)
    assert is_owned_private(target)
    assert read_json(target) == {"state": "send_started"}
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        atomic_json(target, {"state": "prepared"}, exclusive=True)
    assert target.read_bytes() == before
    assert list(directory.iterdir()) == [target]


@pytest.mark.parametrize("payload", [b'{"a":1,"a":2}', b'{"a":NaN}', b''])
def test_private_json_rejects_ambiguous_or_invalid_records(tmp_path, payload):
    path = tmp_path / "record.json"
    path.write_bytes(payload)
    protect_private(path)
    with pytest.raises(ValueError):
        read_json(path)


def test_private_json_enforces_bound(tmp_path):
    path = tmp_path / "record.json"
    path.write_bytes(b'{"payload":"too large"}')
    protect_private(path)
    with pytest.raises(ValueError):
        read_json(path, limit=8)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows ACL denial is tested at its OS boundary")
def test_private_json_refuses_links_and_loose_permissions(tmp_path):
    path = tmp_path / "record.json"
    path.write_bytes(b'{"state":"synthetic"}')
    path.chmod(0o644)
    with pytest.raises(ValueError):
        read_json(path)
    path.chmod(0o600)
    linked = tmp_path / "link.json"
    linked.symlink_to(path)
    with pytest.raises(OSError):
        read_json(linked)


def test_account_store_initializes_and_reopens_under_same_owned_namespace(tmp_path):
    profile = uuid.uuid4().hex
    account = "42"
    scope = ExpectedScope(hashlib.sha256(account.encode()).hexdigest(), account, profile)
    with resource_lock(tmp_path / "state", profile, account) as lease:
        AccountMemory.initialize_store(lease.root, scope, lease)
        store = AccountMemory.open(lease.root, scope, lease)
        try:
            assert store.healthy
            assert is_owned_private(store.path)
            assert read_json(store.path.with_name("automation-store.json"))["state"] == "ready"
        finally:
            store.close()
