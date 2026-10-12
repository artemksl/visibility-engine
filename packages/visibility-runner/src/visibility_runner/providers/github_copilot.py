"""Owned JSON-lines transport to the pinned official GitHub Copilot SDK.

No SDK process, credential lookup or network request happens on import. The
caller must persist its observation intent before ``send_once``. The worker has
an additional durable send fence; neither layer treats a request ID as provider
idempotency. A timed-out command is always ambiguous, never implicitly retried.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import uuid
from typing import Any, Mapping

from ..config import GITHUB_COPILOT_LEGACY_REQUEST_POLICY, GITHUB_COPILOT_REQUEST_POLICIES

SDK_VERSION = "1.0.13"
RUNTIME_VERSION = "1.0.83"
PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
_METHODS = {"github_copilot_web_v1", "github_copilot_model_only_v1"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")


def _capability_detail(value: object) -> dict[str, Any] | None:
    """Allow only the content-free capability diagnosis across stdio."""
    if not isinstance(value, Mapping):
        return None
    stage, reason = value.get("stage"), value.get("reason")
    if not isinstance(stage, str) or not isinstance(reason, str) or stage not in {"runtime_tool_catalog", "session_tools"} or reason not in {
        "missing_required_tools", "unexpected_tools", "metadata_malformed", "metadata_rpc_failed"
    }:
        return None
    result = {"stage": stage, "reason": reason}
    key = {"missing_required_tools": "missing_tools", "unexpected_tools": "unexpected_tools"}.get(reason)
    if key is not None:
        names = value.get(key)
        if not isinstance(names, list) or not names or len(names) > 100:
            return None
        if any(not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", name)
               or re.match(r"(?:gh[pousr]_|github_pat_)", name) for name in names):
            return None
        if key == "missing_tools" and any(name not in {"web_search", "web_fetch"} for name in names):
            return None
        result[key] = list(dict.fromkeys(names))
    return result


class CopilotSourceError(RuntimeError):
    """A redacted transport/admission error with explicit send certainty."""

    def __init__(self, code: str, *, submitted: bool | None = False,
                 retry_after_seconds: float | None = None,
                 capability_detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.submitted = submitted
        self.retry_after_seconds = retry_after_seconds
        self.capability_detail = _capability_detail(capability_detail) if code == "copilot_source_capability_unavailable" else None


async def _credential(reference: str) -> str:
    """Resolve only the explicitly named credential. Never read dotenv or gh."""
    if reference.startswith("env:"):
        key = reference.removeprefix("env:")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise CopilotSourceError("copilot_credential_ref_invalid")
        token = os.environ.get(key, "").strip()
    elif reference.startswith("keychain:"):
        parts = reference.split(":", 2)
        if sys.platform != "darwin" or len(parts) != 3 or not all(parts[1:]):
            raise CopilotSourceError("copilot_credential_ref_invalid")
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/security", "find-generic-password", "-s", parts[1],
            "-a", parts[2], "-w", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 15)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise CopilotSourceError("copilot_credential_unavailable") from None
        token = output.decode("utf-8").strip() if process.returncode == 0 else ""
    else:
        raise CopilotSourceError("copilot_credential_ref_invalid")
    if not token:
        raise CopilotSourceError("copilot_credential_unavailable")
    if len(token) > 16384 or "\n" in token:
        raise CopilotSourceError("copilot_credential_invalid")
    return token


class GitHubCopilotSource:
    """One explicitly bound account's SDK worker; async context-manager capable.

    ``state_directory`` is durable and private, and must be reused to inspect
    sessions after a crash. It may not be a temporary directory removed on exit.
    Node dependencies are installed separately using the shipped lockfile; this
    class never downloads or installs a runtime while executing a measurement.
    """

    def __init__(self, state_directory: str | Path, config: Mapping[str, Any]):
        self.state_directory = Path(state_directory).expanduser().resolve()
        self.config = dict(config)
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._write_lock = asyncio.Lock()
        self._initialize_lock = asyncio.Lock()
        self._source_lock = asyncio.Lock()
        self._preflight: dict[str, Any] | None = None
        self._closed = False
        self._account_lock: Any = None

    async def __aenter__(self) -> "GitHubCopilotSource":
        await self.initialize()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def initialize(self, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
        async with self._initialize_lock:
            if self._closed:
                raise CopilotSourceError("copilot_source_closed")
            if self._preflight is not None:
                if config is not None and any(self.config.get(key) != value for key, value in config.items()):
                    raise CopilotSourceError("copilot_source_config_changed")
                return dict(self._preflight)
            if config is not None:
                self.config.update(config)
            account = str(self.config.get("github_account_id", "")).strip()
            account_ref = str(self.config.get("account_ref", "")).strip()
            method = self.config.get("source_method", self.config.get("method"))
            model = self.config.get("model", self.config.get("model_policy", "auto"))
            request_policy_version = self.config.get("request_policy_version", GITHUB_COPILOT_LEGACY_REQUEST_POLICY)
            if not account or not account_ref:
                raise CopilotSourceError("copilot_account_required")
            if method not in _METHODS or model != "auto":
                raise CopilotSourceError("copilot_source_config_invalid")
            if request_policy_version not in GITHUB_COPILOT_REQUEST_POLICIES:
                raise CopilotSourceError("copilot_request_policy_unsupported")
            if self.config.get("sdk_version", SDK_VERSION) != SDK_VERSION or self.config.get("runtime_version", RUNTIME_VERSION) != RUNTIME_VERSION:
                raise CopilotSourceError("copilot_version_mismatch")
            token = await _credential(str(self.config.get("credential_ref", "")))
            worker = Path(self.config.get("worker_path") or Path(__file__).parent / "copilot_worker" / "worker.mjs").resolve()
            if not worker.is_file():
                raise CopilotSourceError("copilot_worker_unavailable")
            self.state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.state_directory.chmod(0o700)
            # Deliberately exclude HOME, GitHub/Copilot credentials, NODE_OPTIONS,
            # SDK overrides, npm settings and ambient plugin/config variables.
            env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT") if key in os.environ}
            env["HOME"] = str(self.state_directory / "home")
            env["XDG_CONFIG_HOME"] = str(self.state_directory / "home" / ".config")
            env["XDG_CACHE_HOME"] = str(self.state_directory / "cache")
            env["TMPDIR"] = str(self.state_directory / "tmp")
            # The pinned in-process SDK runtime inherits its Node worker's
            # ambient environment; CopilotClient.env does not reach FFI.
            env["COPILOT_ENABLE_BUILTIN_GITHUB_MCP"] = "true" if method == "github_copilot_web_v1" else "false"
            for directory in ("home", "cache", "tmp", "workspace"):
                (self.state_directory / directory).mkdir(mode=0o700, exist_ok=True)
            try:
                self._process = await asyncio.create_subprocess_exec(
                    str(self.config.get("node_binary") or "node"), str(worker),
                    cwd=self.state_directory / "workspace", env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=MAX_MESSAGE_BYTES + 1,
                    start_new_session=True,
                )
                self._reader = asyncio.create_task(self._read_responses())
                self._stderr = asyncio.create_task(self._discard_stderr())
                preflight = await self._command("initialize", {
                    "state_directory": str(self.state_directory),
                    "github_account_id": account, "account_ref": account_ref,
                    "source_method": method, "model": model,
                    "request_policy_version": request_policy_version,
                    "tool_call_limit": int(self.config.get("tool_call_limit", 10)),
                    "sdk_version": SDK_VERSION, "runtime_version": RUNTIME_VERSION,
                    "token": token,
                }, timeout_seconds=45)
                if preflight.get("request_policy_version", GITHUB_COPILOT_LEGACY_REQUEST_POLICY) != request_policy_version:
                    raise CopilotSourceError("copilot_request_policy_mismatch")
                self._acquire_account_lock(str(preflight.get("github_account_id", "")))
                self._preflight = preflight
            except OSError:
                await self.close()
                raise CopilotSourceError("copilot_worker_unavailable") from None
            except BaseException:
                await self.close()
                raise
            finally:
                token = ""
            return dict(self._preflight)

    def _acquire_account_lock(self, canonical_account_id: str) -> None:
        """Prevent another runner process/run from sending as this same account."""
        import fcntl
        if not canonical_account_id:
            raise CopilotSourceError("copilot_account_identity_unproven")
        directory = Path(tempfile.gettempdir()) / f"visibility-runner-copilot-locks-{os.getuid()}"
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_uid != os.getuid():
            raise CopilotSourceError("copilot_account_lock_invalid")
        directory.chmod(0o700)
        key = hashlib.sha256(canonical_account_id.encode("utf-8")).hexdigest()
        fd = os.open(directory / f"{key}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        handle = os.fdopen(fd, "r+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise CopilotSourceError("copilot_account_busy") from None
        self._account_lock = handle

    @staticmethod
    def _valid_id(value: str) -> str:
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise CopilotSourceError("copilot_identifier_invalid")
        return value

    async def create_session(self, session_id: str) -> dict[str, Any]:
        await self.initialize()
        return await self._command("create_session", {"session_id": self._valid_id(session_id)}, timeout_seconds=45)

    async def send_once(self, session_id: str, prompt: str, request_id: str,
                        timeout_seconds: float = 240) -> dict[str, Any]:
        await self.initialize()
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > 512 * 1024:
            raise CopilotSourceError("copilot_prompt_invalid")
        if not 0 < timeout_seconds <= 3600:
            raise CopilotSourceError("copilot_deadline_invalid")
        async with self._source_lock:
            # The outer deadline includes pipe drain and SDK dispatch. The worker
            # leaves an uncertain request inspectable when its own deadline fires.
            return await self._command("send_once", {
                "session_id": self._valid_id(session_id), "prompt": prompt,
                "request_id": self._valid_id(request_id),
                "timeout_ms": max(1, int(timeout_seconds * 1000)),
            }, timeout_seconds=timeout_seconds, submitted=None)

    async def inspect_session(self, session_id: str, request_id: str) -> dict[str, Any]:
        await self.initialize()
        return await self._command("inspect_session", {
            "session_id": self._valid_id(session_id), "request_id": self._valid_id(request_id),
        }, timeout_seconds=30, submitted=None)

    async def cancel(self, session_id: str) -> dict[str, Any]:
        if self._process is None:
            return {"status": "ambiguous", "code": "copilot_worker_not_running"}
        return await self._command("cancel", {"session_id": self._valid_id(session_id)}, timeout_seconds=10, submitted=None)

    async def _command(self, command: str, params: Mapping[str, Any], *,
                       timeout_seconds: float, submitted: bool | None = False) -> dict[str, Any]:
        if not self._process or self._process.returncode is not None or not self._process.stdin:
            raise CopilotSourceError("copilot_worker_exited", submitted=submitted)
        if len(self._pending) >= 8:
            raise CopilotSourceError("copilot_command_queue_full", submitted=False)
        command_id = uuid.uuid4().hex
        payload = json.dumps({"protocol": PROTOCOL_VERSION, "id": command_id,
                              "command": command, "params": params}, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(payload) > MAX_MESSAGE_BYTES:
            raise CopilotSourceError("copilot_message_too_large", submitted=False)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[command_id] = future
        async def dispatch() -> dict[str, Any]:
            async with self._write_lock:
                assert self._process is not None and self._process.stdin is not None
                self._process.stdin.write(payload)
                await self._process.stdin.drain()
            return await asyncio.shield(future)
        try:
            return await asyncio.wait_for(dispatch(), timeout_seconds)
        except asyncio.TimeoutError:
            raise CopilotSourceError("copilot_command_timeout", submitted=submitted) from None
        except (BrokenPipeError, ConnectionError):
            raise CopilotSourceError("copilot_worker_exited", submitted=submitted) from None
        finally:
            self._pending.pop(command_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # Retrieve any exception set while timeout/cancellation won the
                # scheduling race, avoiding unhandled-Future warnings.
                future.exception()

    async def _read_responses(self) -> None:
        assert self._process and self._process.stdout
        failure = "copilot_worker_exited"
        try:
            while raw := await self._process.stdout.readline():
                if len(raw) > MAX_MESSAGE_BYTES:
                    raise ValueError("oversize")
                message = json.loads(raw)
                if not isinstance(message, dict) or message.get("protocol") != PROTOCOL_VERSION:
                    raise ValueError("protocol")
                if not isinstance(message.get("id"), str):
                    raise ValueError("id")
                future = self._pending.get(message["id"])
                if future is None or future.done():
                    continue  # Late result remains in the worker's durable journal.
                if "error" in message:
                    error = message["error"]
                    if not isinstance(error, dict):
                        raise ValueError("error")
                    code = str(error.get("code", "copilot_worker_error"))
                    if not re.fullmatch(r"[a-z0-9_]{1,100}", code):
                        code = "copilot_worker_error"
                    future.set_exception(CopilotSourceError(code, submitted=error.get("submitted"),
                        retry_after_seconds=error.get("retry_after_seconds"), capability_detail=error.get("detail")))
                elif isinstance(message.get("result"), dict):
                    future.set_result(message["result"])
                else:
                    raise ValueError("result")
        except (ValueError, UnicodeError, asyncio.LimitOverrunError):
            failure = "copilot_worker_protocol_invalid"
        except asyncio.CancelledError:
            return
        finally:
            for future in tuple(self._pending.values()):
                if not future.done():
                    future.set_exception(CopilotSourceError(failure, submitted=None))

    async def _discard_stderr(self) -> None:
        # SDK errors can contain headers/credentials. Retain structured, redacted
        # protocol codes only, never raw child stderr (and drain it without growth).
        assert self._process and self._process.stderr
        while await self._process.stderr.read(8192):
            pass

    async def close(self) -> None:
        self._closed = True
        process = self._process
        if process and process.returncode is None:
            with contextlib.suppress(Exception):
                await self._command("close", {}, timeout_seconds=5, submitted=None)
            if process.stdin:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                pass
        # The SDK runtime is a descendant in the worker's new process group.
        # Always reap that owned group, even if the worker itself already exited.
        if process:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
        for task in (self._reader, self._stderr):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._process = None
        if self._account_lock is not None:
            self._account_lock.close()
            self._account_lock = None
