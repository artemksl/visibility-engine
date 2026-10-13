"""Offline cross-process checks for the engine's shared OS boundary.

These use temporary records and synthetic account IDs, never a saved Chrome
profile, provider request, credential lookup, or live browser.
"""
from __future__ import annotations

from pathlib import Path
import asyncio
import os
import shutil
import subprocess
import sys
import uuid

import pytest

from visibility_runner.execution.run_ownership import RunOwnership
from visibility_runner.providers.github_copilot import GitHubCopilotSource
from visibility_runner.providers import github_copilot
from visibility_runner.recovery import durable
from visibility_runner.browser import chrome


def _child(code: str, *arguments: str) -> str:
    completed = subprocess.run(
        [sys.executable, "-c", code, *arguments], text=True,
        capture_output=True, timeout=15, check=True,
    )
    return completed.stdout.strip()


@pytest.mark.native_process
def test_run_ownership_excludes_an_independent_process_and_releases(tmp_path):
    code = """
import sys
from visibility_runner.execution.run_ownership import RunOwnership
from visibility_runner.recovery.api import RecoveryBlocked
try:
    with RunOwnership(sys.argv[1]):
        print('acquired')
except RecoveryBlocked as error:
    print(str(error))
"""
    with RunOwnership(tmp_path):
        assert _child(code, str(tmp_path)) == "run_owned"
    assert _child(code, str(tmp_path)) == "acquired"


@pytest.mark.native_process
def test_sdk_account_lock_excludes_an_independent_process(tmp_path):
    # Invoking only the account lock cannot initialize the SDK or read a token.
    account = "platform-fixture-" + uuid.uuid4().hex
    source = GitHubCopilotSource(tmp_path / "parent", {})
    source._acquire_account_lock(account)
    code = """
import asyncio, sys
from visibility_runner.providers.github_copilot import GitHubCopilotSource, CopilotSourceError
source = GitHubCopilotSource(sys.argv[1], {})
try:
    source._acquire_account_lock(sys.argv[2])
    print('acquired')
except CopilotSourceError as error:
    print(error.code)
finally:
    asyncio.run(source.close())
"""
    try:
        assert _child(code, str(tmp_path / "child"), account) == "copilot_account_busy"
    finally:
        asyncio.run(source.close())
    assert _child(code, str(tmp_path / "child"), account) == "acquired"


def test_critical_exclusive_publication_preserves_existing_send_record(tmp_path):
    path = tmp_path / "effect.json"
    original = {"state": "send_started", "attempt_id": "original"}
    durable.publish_json(path, original, exclusive=True)
    saved = path.read_bytes()
    with pytest.raises(FileExistsError):
        durable.publish_json(path, {"state": "prepared", "attempt_id": "replacement"}, exclusive=True)
    assert path.read_bytes() == saved
    assert list(Path(tmp_path).iterdir()) == [path]


@pytest.mark.parametrize("launcher_kind", ["builtin", "command"])
def test_persistent_chrome_launch_stays_outside_worker_cleanup(monkeypatch, tmp_path, launcher_kind):
    binary = tmp_path / "chrome"
    binary.touch()
    request = chrome.ChromeLaunchRequest("http://127.0.0.1:9333", binary, tmp_path / "user-data")
    calls = []
    def settings(*, persistent):
        assert persistent is True
        return {"creationflags": 42}
    def launch(command, **kwargs):
        calls.append(kwargs)
        return type("Process", (), {"pid": 100})()
    monkeypatch.setattr(chrome, "popen_session_kwargs", settings)
    monkeypatch.setattr(chrome.subprocess, "Popen", launch)
    launcher = chrome.SubprocessChromeLauncher() if launcher_kind == "builtin" else chrome.CommandChromeLauncher(["custom-launcher"])
    assert launcher.launch(request).process_id == 100
    assert calls[0]["creationflags"] == 42


@pytest.mark.native_process
def test_node_publication_bridge_preserves_complete_payload_with_spaces(tmp_path):
    """Exercise Node/Python transport; native Win32 durability has OS tests."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("the optional GitHub SDK requires Node")
    directory = tmp_path / "owned state with spaces"
    directory.mkdir()
    temporary, target = directory / "record.tmp", directory / "record.json"
    payload = b'{"state":"send_started","attempt_id":"synthetic"}'
    temporary.write_bytes(payload)
    helper_root = Path(github_copilot.__file__).parent / "copilot_worker"
    env = dict(os.environ)
    env.update(VISIBILITY_PLATFORM_PYTHON=sys.executable,
               VISIBILITY_PLATFORM_BRIDGE=str(helper_root / "platform_bridge.py"))
    code = """
Object.defineProperty(process, 'platform', { value: 'win32' });
const { publishComplete } = await import(process.argv[1]);
publishComplete(process.argv[2], process.argv[3]);
"""
    subprocess.run(
        [node, "--input-type=module", "-e", code,
         (helper_root / "platform_io.mjs").as_uri(), str(temporary), str(target)],
        env=env, check=True, capture_output=True, timeout=20,
    )
    assert target.read_bytes() == payload
    assert not temporary.exists()
