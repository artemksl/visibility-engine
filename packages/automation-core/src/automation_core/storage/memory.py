"""Account-scoped, explicitly provisioned transactional workflow storage.

Every public transition is short and synchronous. Browser/provider calls and
recovery selection belong to callers, never to this persistence boundary.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
from uuid import NAMESPACE_URL, uuid4, uuid5

from automation_core import contracts as c
from automation_core.storage import persistence as codec
from automation_core.storage.legacy_state import LegacyStateGuard,LegacyStateBlocked
from automation_core.adapters.octo import normalize_profile_id
from automation_core.storage.locking import account_lock_key
from automation_core.storage.models import NeedsAttention
from automation_core.storage.private_files import _save


def _audit_snapshot(method):
    """Reuse source proofs only inside one read transaction, never at dispatch."""
    @wraps(method)
    def audit(self, *args, **kwargs):
        self._require()
        if self._connection.in_transaction or getattr(self, '_source_audit', None) is not None:
            raise StorageFailure()
        self._connection.execute('BEGIN')
        self._source_audit = {}
        try:
            return method(self, *args, **kwargs)
        finally:
            self._source_audit = None
            self._connection.execute('ROLLBACK')
    return audit


class StorageFailure(RuntimeError):
    """Sanitized I/O/integrity failure; no further input is authorized."""
    def __init__(self, message="Account storage is unavailable."):
        self.stop_reason = c.StopReason.STORAGE_UNAVAILABLE
        super().__init__("Account storage is unavailable.")


class TransitionDenied(RuntimeError):
    def __init__(self, stop_reason):
        self.stop_reason = c.StopReason(stop_reason)
        super().__init__(self.stop_reason.value)


@dataclass(frozen=True)
class OperationRecord:
    intent: c.OperationIntent
    current_phase: str
    current_attempt_id: str | None
    current_result_event_id: str | None
    aggregate_outcome: c.Outcome | None
    aggregate_result: c.StoredActionResult | None
    timing_scope_key: str
    limits: object
    attempts_used: int
    effect_intents_used: int
    recovery_used: int
    max_attempts: int
    max_effect_intents: int
    max_recovery: int
    unresolved_attempt_ids: tuple[str, ...]

    @property
    def operation_id(self): return self.intent.operation_id
    @property
    def workflow_id(self): return self.intent.workflow_id
    @property
    def attempt_id(self): return self.current_attempt_id
    @property
    def pending(self): return bool(self.unresolved_attempt_ids)
    @property
    def outcome(self): return self.aggregate_outcome


@dataclass(frozen=True)
class LegacyOperationRecord:
    operation_id: str
    workflow_id: str
    evidence: c.LegacyEvidence
    pending: bool
    current_phase: str = "legacy"
    current_attempt_id: None = None
    current_result_event_id: None = None
    aggregate_outcome: c.Outcome = c.Outcome.UNKNOWN
    legacy: bool = True


_SCHEMA = """
CREATE TABLE workflows (
 workflow_id TEXT PRIMARY KEY, definition_id TEXT NOT NULL, definition_version INTEGER NOT NULL CHECK(definition_version>=0),
 account_key TEXT NOT NULL, scope_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('running','suspended','finished','legacy_blocked')),
 checkpoint_json TEXT NOT NULL, time_scopes_json TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=0),
 created_utc_us INTEGER NOT NULL, updated_utc_us INTEGER NOT NULL);
CREATE TABLE operations (
 operation_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
 step_id TEXT NOT NULL, occurrence_id TEXT NOT NULL, intent_json TEXT NOT NULL, policy_json TEXT NOT NULL,
 root_operation_id TEXT NOT NULL REFERENCES operations(operation_id) DEFERRABLE INITIALLY DEFERRED,
 corrective_parent_id TEXT REFERENCES operations(operation_id),
 aggregate_outcome TEXT CHECK(aggregate_outcome IN ('confirmed','rejected','unknown','not_run')), aggregate_result_json TEXT,
 current_attempt_id TEXT, current_phase TEXT NOT NULL CHECK(current_phase IN ('reserved','intent','completed','legacy')),
 current_outcome TEXT CHECK(current_outcome IN ('confirmed','rejected','unknown','not_run')),
 current_result_event_id TEXT REFERENCES operation_events(event_id), timing_json TEXT NOT NULL,
 unresolved_attempts INTEGER NOT NULL CHECK(unresolved_attempts>=0), attempts_used INTEGER NOT NULL CHECK(attempts_used>=0),
 effect_intents_used INTEGER NOT NULL CHECK(effect_intents_used>=0), recovery_used INTEGER NOT NULL CHECK(recovery_used>=0),
 max_attempts INTEGER NOT NULL CHECK(max_attempts>=0), max_effect_intents INTEGER NOT NULL CHECK(max_effect_intents>=0),
 max_recovery INTEGER NOT NULL CHECK(max_recovery>=0), UNIQUE(workflow_id,step_id,occurrence_id), UNIQUE(workflow_id,operation_id),
 CHECK(attempts_used<=max_attempts AND effect_intents_used<=max_effect_intents AND recovery_used<=max_recovery));
CREATE TABLE operation_events (
 sequence INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, workflow_id TEXT REFERENCES workflows(workflow_id),
 operation_id TEXT, attempt_id TEXT, run_id TEXT NOT NULL, kind TEXT NOT NULL, workflow_revision INTEGER,
 utc_us INTEGER NOT NULL, payload_json TEXT NOT NULL,
 FOREIGN KEY(workflow_id,operation_id) REFERENCES operations(workflow_id,operation_id),
 CHECK(operation_id IS NULL OR workflow_id IS NOT NULL), CHECK(attempt_id IS NULL OR operation_id IS NOT NULL));
CREATE TABLE facts (
 account_key TEXT NOT NULL, scope_key TEXT NOT NULL, fact_key TEXT NOT NULL, value_schema TEXT NOT NULL,
 value_json TEXT NOT NULL, verified_utc_us INTEGER NOT NULL, source_operation_id TEXT REFERENCES operations(operation_id),
 evidence_event_id TEXT NOT NULL REFERENCES operation_events(event_id), provenance_json TEXT NOT NULL,
 PRIMARY KEY(account_key,scope_key,fact_key));
CREATE UNIQUE INDEX store_genesis ON operation_events(kind) WHERE kind='store_initialized';
CREATE UNIQUE INDEX attempt_admission ON operation_events(attempt_id) WHERE kind='admission';
CREATE UNIQUE INDEX attempt_intent ON operation_events(attempt_id) WHERE kind='effect_intent';
CREATE UNIQUE INDEX attempt_completion ON operation_events(attempt_id) WHERE kind='completion';
CREATE INDEX operation_history ON operation_events(operation_id,sequence);
CREATE INDEX pending_operations ON operations(workflow_id,unresolved_attempts);
CREATE TRIGGER events_no_update BEFORE UPDATE ON operation_events BEGIN SELECT RAISE(ABORT,'immutable_event'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON operation_events BEGIN SELECT RAISE(ABORT,'immutable_event'); END;
CREATE TRIGGER operations_identity BEFORE UPDATE ON operations WHEN
 NEW.operation_id!=OLD.operation_id OR NEW.workflow_id!=OLD.workflow_id OR NEW.step_id!=OLD.step_id OR
 NEW.occurrence_id!=OLD.occurrence_id OR NEW.intent_json!=OLD.intent_json OR NEW.policy_json!=OLD.policy_json OR
 NEW.root_operation_id!=OLD.root_operation_id OR NEW.corrective_parent_id IS NOT OLD.corrective_parent_id
 BEGIN SELECT RAISE(ABORT,'immutable_intent'); END;
CREATE TRIGGER operations_counts BEFORE UPDATE ON operations WHEN
 NEW.attempts_used<OLD.attempts_used OR NEW.effect_intents_used<OLD.effect_intents_used OR NEW.recovery_used<OLD.recovery_used OR
 NEW.max_attempts>OLD.max_attempts OR NEW.max_effect_intents>OLD.max_effect_intents OR NEW.max_recovery>OLD.max_recovery
 BEGIN SELECT RAISE(ABORT,'immutable_allowance'); END;
CREATE TRIGGER operations_no_delete BEFORE DELETE ON operations BEGIN SELECT RAISE(ABORT,'immutable_operation'); END;
CREATE TRIGGER workflows_no_delete BEFORE DELETE ON workflows BEGIN SELECT RAISE(ABORT,'immutable_workflow'); END;
"""
_TABLES = {"workflows", "operations", "operation_events", "facts"}
_EVENT_KINDS = {"store_initialized", "workflow_created", "scope_activated", "scope_checkpointed", "admission", "effect_intent", "receipt", "observation", "completion", "resolution", "result_consumed", "recovery_admitted", "recovery_verified", "resumed", "suspended", "finished", "legacy_imported", "legacy_resolved", "deferred_policy_wait", "deferred_policy_value", "deferred_policy_release"}
_EVENT_KINDS.add('resource_preflight_retry_admitted')
_EVENT_KINDS.add('known_path_retry_admitted')
_EVENT_KINDS.add('preparation_restore_admitted')
_EVENT_KINDS.add('undispatched_restore_admitted')
_ATTEMPT_KINDS = {"admission", "effect_intent", "receipt", "completion", "resolution"}
_MARKER_FIELDS = {"version", "state", "schema_version", "account_key", "store_uuid", "database"}


def _value(value):
    return getattr(value, "value", value)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _pack(**values):
    return _json({key: codec.encode(value) for key, value in values.items()})


_UNPACK_CACHE = OrderedDict()
_UNPACK_CACHE_LOCK = threading.RLock()
_UNPACK_CACHE_BYTES = 0
_UNPACK_CACHE_MAX_ENTRIES = 2048
_UNPACK_CACHE_MAX_BYTES = 16 * 1_048_576
_UNPACK_CACHE_MAX_ITEM_BYTES = 1_048_576


def reset_unpack_reuse():
    """Clear pure decoded envelopes when tests replace fixed schema/decoder code."""
    global _UNPACK_CACHE_BYTES
    with _UNPACK_CACHE_LOCK:
        _UNPACK_CACHE.clear()
        _UNPACK_CACHE_BYTES = 0


def _unpack_immutable(value):
    if value is None or type(value) in (bool, int, float, str) or isinstance(value, Enum):
        return True
    if type(value) is tuple:
        return all(_unpack_immutable(item) for item in value)
    return (is_dataclass(value) and value.__dataclass_params__.frozen
        and all(_unpack_immutable(getattr(value, member.name)) for member in fields(value)))


def _unpack(raw):
    # Only identical wire decoding is reused. SQL reads and every source,
    # history, pending, identity and timing audit still run on each caller.
    global _UNPACK_CACHE_BYTES
    if type(raw) is str:
        with _UNPACK_CACHE_LOCK:
            cached = _UNPACK_CACHE.get(raw)
            if cached is not None:
                _UNPACK_CACHE.move_to_end(raw)
                return dict(cached[0])
    value = json.loads(raw)
    if type(value) is not dict:
        raise ValueError("invalid envelope")
    decoded = {key: codec.decode(item) for key, item in value.items()}
    if (type(raw) is str and len(raw) <= _UNPACK_CACHE_MAX_ITEM_BYTES
            and all(_unpack_immutable(item) for item in decoded.values())):
        size = len(raw.encode('utf-8'))
        if size <= _UNPACK_CACHE_MAX_ITEM_BYTES:
            with _UNPACK_CACHE_LOCK:
                if raw not in _UNPACK_CACHE:
                    _UNPACK_CACHE[raw] = (tuple(decoded.items()), size)
                    _UNPACK_CACHE_BYTES += size
                    while (len(_UNPACK_CACHE) > _UNPACK_CACHE_MAX_ENTRIES
                            or _UNPACK_CACHE_BYTES > _UNPACK_CACHE_MAX_BYTES):
                        _, (_, removed_size) = _UNPACK_CACHE.popitem(last=False)
                        _UNPACK_CACHE_BYTES -= removed_size
    return decoded


def _id(value):
    if not isinstance(value, str) or not value or len(value)>256 or any(ord(ch)<33 or ord(ch)>126 for ch in value):
        raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
    return value





def _payload_refs(value, prefix=""):
    if type(value) is c.OutputRef:
        return ((prefix, value),)
    if type(value) is tuple:
        return tuple(item for entry in value for item in _payload_refs(entry, prefix))
    if is_dataclass(value):
        return tuple(item for member in fields(value)
                     for item in _payload_refs(getattr(value, member.name), prefix or member.name))
    return ()


class AccountMemory:
    recovery_namespace = 'automation.known_path_recovery'
    completion_input_types = (c.BlockCompletionInputs,)
    def __init__(self, *args, **kwargs):
        raise TypeError("Use explicit initialize_store and AccountMemory.open.")

    @staticmethod
    def _paths(state_dir, scope, ownership):
        ownership.assert_owned()
        root = Path(state_dir).resolve()
        uid = scope.expected_account_id
        try:
            account_lock = account_lock_key(uid)
        except NeedsAttention:
            raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH) from None
        if (scope.account_key != hashlib.sha256(uid.encode()).hexdigest()
                or normalize_profile_id(scope.profile_id) != scope.profile_id
                or root != Path(ownership.root).resolve()
                or not {account_lock,"profile:"+scope.profile_id}.issubset(ownership.keys)):
            raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
        codec.dumps(scope)
        folder = root / "accounts" / scope.account_key
        if not folder.resolve().is_relative_to(root):
            raise StorageFailure()
        return root, folder, folder / "automation.sqlite3", folder / "automation-store.json"

    @staticmethod
    def _private_file(path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise StorageFailure()

    @staticmethod
    def _connect(path):
        # Panel snapshots briefly hold a read lock in DELETE-journal mode.
        # Let SQLite finish the same transaction; never replay browser work.
        connection = sqlite3.connect(path.as_uri()+"?mode=rw", uri=True, timeout=1.,
                                     autocommit=True, check_same_thread=True)
        try:
            connection.row_factory = sqlite3.Row
            for setting, value in (("foreign_keys","ON"),("journal_mode","DELETE"),("synchronous","EXTRA"),("busy_timeout","1000")):
                connection.execute(f"PRAGMA {setting}={value}")
            for setting, expected in (("foreign_keys",1),("journal_mode","delete"),("synchronous",3),("busy_timeout",1000)):
                if connection.execute(f"PRAGMA {setting}").fetchone()[0] != expected:
                    raise StorageFailure()
            return connection
        except BaseException:
            connection.close()
            raise

    @classmethod
    def initialize_store(cls, state_dir, scope, ownership):
        connection = None
        try:
            root, folder, path, marker_path = cls._paths(state_dir, scope, ownership)
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            folder.chmod(0o700)
            if marker_path.exists() or path.exists() or (folder/"verification.json").exists() or tuple(folder.glob("*.sqlite*")):
                raise TransitionDenied(c.StopReason.LEGACY_STATE_BLOCKED)
            try: LegacyStateGuard(root,scope,ownership).check()
            except LegacyStateBlocked:
                raise TransitionDenied(c.StopReason.LEGACY_STATE_BLOCKED) from None
            marker = dict(version=1,state="initializing",schema_version=2,account_key=scope.account_key,
                          store_uuid=uuid4().hex,database=path.name)
            _save(marker_path,marker)
            descriptor = os.open(path,os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o600)
            os.close(descriptor)
            connection = cls._connect(path)
            connection.execute("BEGIN IMMEDIATE")
            # executescript performs no implicit COMMIT under autocommit=True.
            connection.executescript(_SCHEMA)
            connection.execute("PRAGMA user_version=2")
            connection.execute("INSERT INTO operation_events(event_id,run_id,kind,utc_us,payload_json) VALUES (?,?,?,?,?)",
                               (uuid4().hex,"provision","store_initialized",0,
                                _json(dict(schema_version=2,account_key=scope.account_key,store_uuid=marker["store_uuid"]))))
            connection.execute("COMMIT")
            connection.close(); connection = None
            from automation_core.storage.legacy_state import _validate_migration_candidate
            _validate_migration_candidate(path,root,scope,ownership,marker["store_uuid"])
            descriptor=os.open(path,os.O_RDONLY)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
            _save(marker_path,dict(marker,state="ready"))
        except TransitionDenied:
            raise
        except Exception:
            raise StorageFailure() from None
        finally:
            if connection is not None: connection.close()

    @classmethod
    def open(cls, state_dir, scope, ownership):
        self=object.__new__(cls)
        self._connection=None
        self.healthy=True
        self._definitions={}
        self._fault_hook=None
        self._owns_transaction_journal=False
        self.run_id=uuid4().hex
        self._run_bound=False
        self._thread=threading.get_ident()
        self.ownership=ownership
        self.scope=scope
        self.account_key=scope.account_key
        try:
            self.root,folder,self.path,marker_path=cls._paths(state_dir,scope,ownership)
            self._private_file(marker_path); self._private_file(self.path)
            marker=json.loads(marker_path.read_text())
            if (set(marker)!=_MARKER_FIELDS or marker["version"]!=1 or marker["schema_version"]!=2
                    or marker["state"]!="ready" or marker["account_key"]!=scope.account_key or marker["database"]!=self.path.name):
                raise StorageFailure()
            self.store_uuid=_id(marker["store_uuid"])
            self._connection=self._connect(self.path)
            try: self._validate_database()
            except Exception: raise StorageFailure() from None
            return self
        except TransitionDenied:
            if self._connection is not None: self._connection.close()
            self.healthy=False
            raise
        except Exception:
            if self._connection is not None: self._connection.close()
            self.healthy=False
            raise StorageFailure() from None

    def _require(self):
        if not self.healthy or self._connection is None:
            raise StorageFailure()
        if threading.get_ident()!=self._thread:
            raise TransitionDenied(c.StopReason.OWNERSHIP_LOST)
        try: self.ownership.assert_owned()
        except Exception: raise TransitionDenied(c.StopReason.OWNERSHIP_LOST) from None

    def bind_run(self,run_id):
        """Bind once; deriving the same run grants no fresh storage permission."""
        _id(run_id)
        if threading.get_ident()!=self._thread:
            raise TransitionDenied(c.StopReason.OWNERSHIP_LOST)
        if self._run_bound and self.run_id==run_id:
            return
        self._require()
        if self._run_bound:
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        self.run_id=run_id
        self._run_bound=True

    def _legacy_guard(self):
        self._require()
        journal = (self.path.with_name(self.path.name + "-journal")
            if self._owns_transaction_journal and self._connection.in_transaction else None)
        try: LegacyStateGuard(self.root,self.scope,self.ownership,self._resolved_legacy_sources(),
            owned_transaction_journal=journal).check()
        except LegacyStateBlocked: raise TransitionDenied(c.StopReason.LEGACY_STATE_BLOCKED) from None

    def _read(self,sql,args=()):
        self._require()
        try: return self._connection.execute(sql,args).fetchall()
        except (sqlite3.Error,OSError):
            self.healthy=False
            raise StorageFailure() from None

    def _commit(self):
        self._connection.execute("COMMIT")

    @contextmanager
    def _transaction(self, *, deferred_policy_guard=None):
        self._require()
        if getattr(self, '_source_audit', None) is not None:
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        self._validate_transaction_guard(deferred_policy_guard)
        journal = self.path.with_name(self.path.name + "-journal")
        # A companion present before this owned transaction is historical state,
        # never evidence that this connection created it. DELETE mode is pinned
        # when the validated store is opened.
        if journal.exists() or journal.is_symlink():
            raise TransitionDenied(c.StopReason.LEGACY_STATE_BLOCKED)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._owns_transaction_journal=True
            yield
            if self._fault_hook: self._fault_hook("before_commit")
            if deferred_policy_guard is not None:
                deferred_policy_guard.verify(self)
            self._commit()
            if self._fault_hook: self._fault_hook("after_commit")
        except TransitionDenied:
            if self._connection.in_transaction: self._connection.execute("ROLLBACK")
            raise
        except BaseException as error:
            from automation_core.diagnostics.live_faults import observe_storage_failure
            observe_storage_failure(self, error)
            self.healthy=False
            try:
                if self._connection.in_transaction: self._connection.execute("ROLLBACK")
            except sqlite3.Error: pass
            if isinstance(error,(KeyboardInterrupt,SystemExit)): raise
            raise StorageFailure() from None
        finally:
            self._owns_transaction_journal=False

    def close(self):
        try:
            if self._connection is not None: self._connection.close()
        except sqlite3.Error: raise StorageFailure() from None
        finally:
            self._connection=None
            self.healthy=False

    def reload(self):
        """Revalidate durable state without replacing the shared service object.

        The caller must first settle input and observation callbacks. Reload does
        not authorize replay or restore any unacknowledged in-memory transition.
        """
        if threading.get_ident()!=self._thread:
            raise TransitionDenied(c.StopReason.OWNERSHIP_LOST)
        try: self.ownership.assert_owned()
        except Exception: raise TransitionDenied(c.StopReason.OWNERSHIP_LOST) from None
        replacement=None
        try:
            replacement=type(self).open(self.root,self.scope,self.ownership)
            if replacement.store_uuid!=self.store_uuid:
                raise StorageFailure()
            previous=self._connection
            if previous is not None:
                previous.close()
            self._connection=replacement._connection
            replacement._connection=None
            replacement.healthy=False
            self._fault_hook=None
            self.healthy=True
            return self
        except BaseException:
            self.healthy=False
            if replacement is not None: replacement.close()
            raise

    def _typed(self,value,expected=None):
        try:
            encoded=codec.dumps(value)
            if expected is not None and type(value) is not expected: raise ValueError()
            return encoded
        except Exception:
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH) from None

    def _decode(self,raw,expected=None):
        try: return codec.loads(raw,expected)
        except Exception:
            self.healthy=False
            raise StorageFailure() from None

    @codec.reuse_immutable_values
    @_audit_snapshot
    def _validate_database(self):
        global _EXPECTED_SCHEMA
        if "_EXPECTED_SCHEMA" not in globals():
            temporary=sqlite3.connect(":memory:",autocommit=True)
            try:
                temporary.executescript(_SCHEMA)
                _EXPECTED_SCHEMA={row[0]:" ".join(row[1].split()) for row in temporary.execute(
                    "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")}
            finally: temporary.close()
        actual={row[0]:" ".join(row[1].split()) for row in self._connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")}
        if actual!=_EXPECTED_SCHEMA or self._connection.execute("PRAGMA user_version").fetchone()[0]!=2:
            raise StorageFailure()
        if self._connection.execute("PRAGMA integrity_check").fetchone()[0]!="ok" or self._connection.execute("PRAGMA foreign_key_check").fetchall():
            raise StorageFailure()
        genesis=self._read("SELECT * FROM operation_events WHERE kind='store_initialized'")
        if len(genesis)!=1 or genesis[0]["workflow_id"] is not None or genesis[0]["operation_id"] is not None:
            raise StorageFailure()
        if json.loads(genesis[0]["payload_json"])!=dict(schema_version=2,account_key=self.account_key,store_uuid=self.store_uuid):
            raise StorageFailure()
        # Check the store at entry; validate the selected workflow, referenced
        # outputs and journal-derived pending effects when they are consumed.
        # Rewalking every historical workflow on each attachment made startup
        # grow with prior runs and could exhaust a new run before browser input.
        self.pending_for(self.scope)

    def _checkpoint_row(self,row):
        from automation_core.workflow.timing import TimeCheckpoint
        cp=self._decode(row["checkpoint_json"],c.WorkflowCheckpoint)
        at=self._decode(row["time_scopes_json"],TimeCheckpoint)
        if (cp.workflow_id!=row["workflow_id"] or cp.definition_id!=row["definition_id"] or cp.definition_version!=row["definition_version"]
                or cp.revision!=row["revision"] or cp.status!=row["status"] or cp.scope.account_key!=self.account_key
                or codec.dumps(cp.scope)!=row["scope_json"] or cp.time_scopes!=at.scopes or cp.utc_high_water_us!=at.utc_high_water_us):
            self.healthy=False
            raise StorageFailure()
        try: self._validate_scope_graph(cp,cp.time_scopes)
        except ValueError:
            self.healthy=False
            raise StorageFailure() from None
        return cp

    @codec.reuse_immutable_values
    def load_checkpoint(self,workflow_id):
        rows=self._read("SELECT * FROM workflows WHERE workflow_id=?",(_id(workflow_id),))
        if not rows: raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
        return self._checkpoint_row(rows[0])

    @codec.reuse_immutable_values
    def load_time_checkpoint(self,workflow_id):
        from automation_core.workflow.timing import TimeCheckpoint
        rows=self._read("SELECT time_scopes_json FROM workflows WHERE workflow_id=?",(_id(workflow_id),))
        if not rows: raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
        return self._decode(rows[0][0],TimeCheckpoint)

    def _definition(self,cp):
        definition=self._definitions.get(cp.workflow_id)
        if definition is None or definition.version!=cp.definition_version or definition.definition_id!=cp.definition_id:
            raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
        return definition

    def _saved_definition(self, cp):
        events = self._read("SELECT payload_json FROM operation_events WHERE workflow_id=? AND kind='workflow_created'", (cp.workflow_id,))
        if len(events) != 1:
            raise StorageFailure()
        record = _unpack(events[0][0]).get("definition")
        if (type(record) is not c.WorkflowDefinitionRecord or record.definition_id != cp.definition_id
                or record.version != cp.definition_version):
            raise StorageFailure()
        return record

    def workflow_exists(self, workflow_id):
        return bool(self._read('SELECT 1 FROM workflows WHERE workflow_id=?', (_id(workflow_id),)))

    @codec.reuse_immutable_values
    def workflow_inputs(self, workflow_id):
        cp = self.load_checkpoint(workflow_id)
        events = self._read("SELECT payload_json FROM operation_events WHERE workflow_id=? AND kind='workflow_created'", (cp.workflow_id,))
        if len(events) != 1:
            raise StorageFailure()
        return _unpack(events[0][0]).get("workflow_inputs")

    @codec.reuse_immutable_values
    def preflight_workflow(self, definition, workflow_id, scope=None, *, create=False, workflow_inputs=None):
        """Read-only definition/input compatibility gate before session effects."""
        scope = self.scope if scope is None else scope
        record = c.definition_record(definition)
        self._typed(record, c.WorkflowDefinitionRecord)
        self._validate_workflow_inputs(workflow_inputs, workflow_id, scope, definition)
        rows = self._read("SELECT * FROM workflows WHERE workflow_id=?", (_id(workflow_id),))
        if not rows:
            if not create or scope != self.scope:
                raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
            self._legacy_guard()
            if self.pending_for_request(scope, workflow_id, record, workflow_inputs):
                raise TransitionDenied(c.StopReason.NEEDS_INPUT)
            return None
        cp = self._checkpoint_row(rows[0])
        if cp.stop_reason is c.StopReason.CANCELLED and self._read(
                "SELECT 1 FROM operation_events WHERE workflow_id=? AND kind='unpublished_upload_retired'", (workflow_id,)):
            raise TransitionDenied(c.StopReason.CANCELLED)
        if (cp.scope != scope or scope != self.scope or self._saved_definition(cp) != record
                or self.workflow_inputs(workflow_id) != workflow_inputs):
            raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
        self._validate_position(cp, cp.main, definition=record)
        if tuple(sorted(cp.pending_operation_ids)) != self._pending_ids(cp.workflow_id):
            raise StorageFailure()
        if isinstance(cp.main, c.Finished) and record.blocks:
            self._validate_final_completion(cp)
        self._legacy_guard()
        if any(pending.workflow_id != cp.workflow_id for pending in self.blocking_pending_for(scope, workflow_id)):
            if not self._can_resume_related_recovery(workflow_id):
                raise TransitionDenied(c.StopReason.NEEDS_INPUT)
        return cp

    def _step(self,cp,step_id):
        matches=[step for step in self._definition(cp).steps if step.step_id==step_id]
        if len(matches)!=1: raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
        return matches[0]

    def _event_row(self,event_id):
        rows=self._read("SELECT * FROM operation_events WHERE event_id=?",(_id(event_id),))
        if not rows: raise StorageFailure()
        return rows[0]

    def _event(self,kind,cp,at,*,operation_id=None,attempt_id=None,event_id=None,**payload):
        event_id=event_id or uuid4().hex
        self._connection.execute("INSERT INTO operation_events(event_id,workflow_id,operation_id,attempt_id,run_id,kind,workflow_revision,utc_us,payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (event_id,cp.workflow_id,operation_id,attempt_id,self.run_id,kind,
             cp.revision if kind=="workflow_created" else cp.revision+1,at.sampled_utc_us,_pack(**payload)))
        return event_id

    def _cas(self,workflow_id,expected_revision):
        cp=self.load_checkpoint(workflow_id)
        self._definition(cp)
        if type(expected_revision) is not int or cp.revision!=expected_revision:
            raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
        if cp.status=="legacy_blocked": raise TransitionDenied(c.StopReason.LEGACY_STATE_BLOCKED)
        return cp

    def _merge_time(self,cp,at):
        from automation_core.workflow.timing import TimeCheckpoint,validate_scope_tightening,validate_limits_tightening
        self._typed(at,TimeCheckpoint)
        scopes={saved.scope_key.token():saved for saved in cp.time_scopes}
        if len({s.scope_key.token() for s in at.scopes})!=len(at.scopes): raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        try:
            for saved in at.scopes:
                if saved.scope_key.account_key!=cp.scope.account_key or saved.scope_key.workflow_operation_id!=cp.workflow_id:
                    raise ValueError()
                key=saved.scope_key.token()
                if key in scopes: validate_scope_tightening(scopes[key],saved)
                scopes[key]=saved
            self._validate_scope_graph(cp,tuple(scopes.values()))
            if len({opid for opid,_ in at.action_limits})!=len(at.action_limits): raise ValueError()
            for opid,limits in at.action_limits:
                rows=self._read("SELECT workflow_id,timing_json FROM operations WHERE operation_id=?",(opid,))
                if rows:
                    if rows[0]["workflow_id"]!=cp.workflow_id: raise ValueError()
                    timing=_unpack(rows[0]["timing_json"])
                    validate_limits_tightening(timing["limits"],limits)
                    self._connection.execute("UPDATE operations SET timing_json=? WHERE operation_id=?",(_pack(scope_key=timing["scope_key"],limits=limits),opid))
        except ValueError:
            raise TransitionDenied(c.StopReason.CLOCK_UNTRUSTED) from None
        merged=replace(at,scopes=tuple(scopes.values()),utc_high_water_us=max(cp.utc_high_water_us,at.utc_high_water_us,at.sampled_utc_us))
        return merged

    @staticmethod
    def _validate_scope_graph(cp,saved_scopes):
        from automation_core.workflow.timing import validate_limits_tightening
        scopes={saved.scope_key.token():saved for saved in saved_scopes}
        if len(scopes)!=len(saved_scopes): raise ValueError()
        for saved in scopes.values():
            if saved.scope_key.account_key!=cp.scope.account_key or saved.scope_key.workflow_operation_id!=cp.workflow_id:
                raise ValueError()
            visited={saved.scope_key.token()}
            parent=saved.parent_scope_key
            if parent is not None:
                if parent not in scopes: raise ValueError()
                validate_limits_tightening(scopes[parent].limits,saved.limits)
            while parent is not None:
                if parent in visited or parent not in scopes: raise ValueError()
                visited.add(parent)
                parent=scopes[parent].parent_scope_key

    def _save(self,cp,at):
        merged=self._merge_time(cp,at)
        saved=replace(cp,revision=cp.revision+1,time_scopes=merged.scopes,utc_high_water_us=merged.utc_high_water_us,
                      pending_operation_ids=self._pending_ids(cp.workflow_id))
        changed=self._connection.execute("UPDATE workflows SET checkpoint_json=?,time_scopes_json=?,status=?,revision=?,updated_utc_us=? WHERE workflow_id=? AND revision=?",
            (codec.dumps(saved),codec.dumps(merged),saved.status,saved.revision,at.sampled_utc_us,saved.workflow_id,cp.revision))
        if changed.rowcount!=1: raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
        return saved

    @codec.reuse_immutable_values
    def open_or_resume_workflow(self,definition,workflow_id,scope,*,create=False,at,workflow_inputs=None,
                                recovery_policy_version=1, continuation_link=None):
        self._typed(scope,c.ExpectedScope)
        if type(recovery_policy_version) is not int or recovery_policy_version not in (1, 2):
            raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
        record=c.definition_record(definition)
        self._typed(record,c.WorkflowDefinitionRecord)
        self._validate_workflow_inputs(workflow_inputs, workflow_id, scope, definition)
        if scope!=self.scope or definition.version<=0 or not definition.steps or len({s.step_id for s in definition.steps})!=len(definition.steps):
            raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
        for step in definition.steps:
            if any(name not in {s.step_id for s in definition.steps} for name in step.normal_next):
                raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
        workflow_id=_id(workflow_id)
        rows=self._read("SELECT * FROM workflows WHERE workflow_id=?",(workflow_id,))
        if rows:
            cp=self._checkpoint_row(rows[0])
            events=self._read("SELECT payload_json FROM operation_events WHERE workflow_id=? AND kind='workflow_created'",(workflow_id,))
            if (cp.scope!=scope or cp.definition_id!=definition.definition_id or cp.definition_version!=definition.version
                    or len(events)!=1 or _unpack(events[0][0]).get("definition")!=record
                    or _unpack(events[0][0]).get("workflow_inputs") != workflow_inputs):
                raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
            self._definitions[workflow_id]=definition
            self._validate_position(cp,cp.main,definition)
            return cp
        if not create: raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
        self._validate_new_workflow(definition, workflow_id, continuation_link)
        self._legacy_guard()
        pending = self.pending_for_request(scope, workflow_id, record, workflow_inputs)
        if pending:
            raise TransitionDenied(c.StopReason.NEEDS_INPUT)
        with self._transaction():
            cp=c.WorkflowCheckpoint(workflow_id=workflow_id,definition_id=definition.definition_id,definition_version=definition.version,
                revision=0,scope=scope,status="running",main=c.Ready(definition.entry_step_id,"0"),
                recovery_policy_version=recovery_policy_version)
            merged=self._merge_time(cp,at)
            cp=replace(cp,time_scopes=merged.scopes,utc_high_water_us=merged.utc_high_water_us)
            self._connection.execute("INSERT INTO workflows VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (workflow_id,definition.definition_id,definition.version,self.account_key,codec.dumps(scope),cp.status,codec.dumps(cp),codec.dumps(merged),0,at.sampled_utc_us,at.sampled_utc_us))
            initial = {"definition": record}
            if workflow_inputs is not None:
                initial["workflow_inputs"] = workflow_inputs
            initial.update(self._continuation_fields(definition, workflow_id, continuation_link))
            self._event("workflow_created",cp,at,**initial)
            self._validate_continuation(continuation_link, cp)
        self._definitions[workflow_id]=definition
        return cp

    def _commit_continuation(self, request, prepare, event):
        """The component prepares its immutable values; the store owns the commit."""
        with self._transaction():
            prepared = prepare(self, request)
            cp, at = prepared.checkpoint, prepared.at
            if at is not None:
                merged = self._merge_time(cp, at)
                self._connection.execute('INSERT INTO workflows VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (cp.workflow_id, cp.definition_id, cp.definition_version, self.account_key,
                        codec.dumps(cp.scope), cp.status, codec.dumps(cp), codec.dumps(merged),
                        cp.revision, at.sampled_utc_us, at.sampled_utc_us))
                self._event('workflow_created', cp, at, definition=c.definition_record(prepared.definition),
                    workflow_inputs=prepared.original, **{event: prepared.link})
        self._definitions[cp.workflow_id] = prepared.definition
        return prepared

    @codec.reuse_immutable_values
    def activate_time_scope(self,workflow_id,saved,*,expected_revision,at):
        scopes=tuple(s for s in at.scopes if s.scope_key.token()!=saved.scope_key.token())+(saved,)
        at=replace(at,scopes=scopes)
        with self._transaction():
            cp=self._cas(workflow_id,expected_revision)
            self._event("scope_activated",cp,at,saved=saved)
            cp=self._save(cp,at)
        return cp

    def _active(self,cp):
        return cp.recovery.position if cp.recovery is not None else cp.main

    def _with_position(self,cp,position):
        if cp.recovery is None: return replace(cp,main=position)
        return replace(cp,recovery=replace(cp.recovery,position=position))

    def _validate_position(self,cp,position,definition):
        if isinstance(position,c.Ready):
            if definition is not None and position.step_id not in {step.step_id for step in definition.steps}:
                raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
        elif isinstance(position,c.InAction):
            rows=self._read("SELECT workflow_id,current_attempt_id FROM operations WHERE operation_id=?",(position.operation_id,))
            if not rows or rows[0]["workflow_id"]!=cp.workflow_id or rows[0]["current_attempt_id"]!=position.attempt_id: raise StorageFailure()
        elif isinstance(position,c.ActionResultAvailable):
            event=self._event_row(position.result_event_id)
            if event["workflow_id"]!=cp.workflow_id: raise StorageFailure()
            self.load_result(position)
        elif isinstance(position,c.Finished):
            if position.result_event_id is not None and self._event_row(position.result_event_id)["workflow_id"]!=cp.workflow_id: raise StorageFailure()
        else: raise StorageFailure()

    def _validate_final_completion(self, cp):
        if cp.main.result_event_id is None or cp.status != "finished" or cp.recovery is not None:
            raise StorageFailure()
        event = self._event_row(cp.main.result_event_id)
        if event["workflow_id"] != cp.workflow_id:
            raise StorageFailure()
        record = self.operation(event["operation_id"])
        if record is None or isinstance(record, LegacyOperationRecord):
            raise StorageFailure()
        result = self.load_result(c.ActionResultAvailable(record.operation_id, event["attempt_id"], event["event_id"]))
        owner = self._owner(cp, record.intent.step_id)
        if (not owner.final or record.intent.step_id != owner.completion_step_id
                or result.outcome != c.Outcome.CONFIRMED
                or type(result.output) not in (c.BlockCompletionOutput, c.AuthenticationCompletionOutput)):
            raise StorageFailure()
        events = self._read("SELECT payload_json FROM operation_events WHERE operation_id=? AND kind='result_consumed'", (record.operation_id,))
        if not any(_unpack(row[0])["from_result"].result_event_id == cp.main.result_event_id
                   and isinstance(_unpack(row[0])["next_position"], c.Finished) for row in events):
            raise StorageFailure()

    def _validate_successor(self, cp, record, result, next_position, *, main_only=True):
        definition = self._saved_definition(cp)
        if not definition.blocks:
            return
        owner = self._owner(cp, record.intent.step_id)
        if type(result.output) is c.BlockCompletionOutput:
            successor = result.output.successor_block_id
            if successor is None:
                if not isinstance(next_position, c.Finished):
                    raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
            else:
                target = next((block.entry_step_id for block in definition.blocks if block.block_id == successor), None)
                if not isinstance(next_position, c.Ready) or next_position.step_id != target:
                    raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
        if isinstance(next_position, c.Finished) and (main_only or record.intent.corrective_parent_id is None):
            if (not owner.final or record.intent.step_id != owner.completion_step_id
                    or type(result.output) not in (c.BlockCompletionOutput, c.AuthenticationCompletionOutput)):
                raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)

    def _unresolved(self,operation_id):
        unresolved=[]
        for row in self._read("SELECT kind,attempt_id,payload_json FROM operation_events WHERE operation_id=? ORDER BY sequence",(operation_id,)):
            if row["kind"] in {'effect_intent', 'undispatched_restore_admitted'}:
                if row['attempt_id'] not in unresolved:
                    unresolved.append(row["attempt_id"])
            elif row["kind"] == "unpublished_upload_retired":
                unresolved = [attempt for attempt in unresolved if attempt != row["attempt_id"]]
            elif row["kind"] in ("completion","resolution"):
                payload=_unpack(row["payload_json"])
                result=payload["envelope"].result if row["kind"]=="completion" else payload["result"]
                if result.outcome!=c.Outcome.UNKNOWN:
                    ids=(row["attempt_id"],) if row["kind"]=="completion" else payload["resolved_attempts"]
                    unresolved=[attempt for attempt in unresolved if attempt not in ids]
        return tuple(unresolved)

    def _pending_ids(self,workflow_id=None):
        # Select unresolved journal effects before decoding their full records.
        # Replaying every completed operation here made each new run slower.
        # The journal remains authoritative even if a derived counter is zero.
        candidates = """
            SELECT o.operation_id,o.current_phase FROM operations o
            WHERE (o.current_phase='legacy' OR o.unresolved_attempts>0 OR EXISTS (
                SELECT 1 FROM operation_events effect
                WHERE effect.operation_id=o.operation_id AND effect.kind='effect_intent'
                AND NOT EXISTS (
                    SELECT 1 FROM operation_events completion
                    WHERE completion.operation_id=effect.operation_id
                    AND completion.attempt_id=effect.attempt_id
                    AND completion.sequence>effect.sequence AND completion.kind='completion'
                    AND json_extract(completion.payload_json,
                        '$.envelope.fields.result.fields.outcome.value')
                        IN ('confirmed','rejected','not_run'))
                AND NOT EXISTS (
                    SELECT 1 FROM operation_events resolution,
                        json_each(resolution.payload_json,'$.resolved_attempts."$tuple"') attempt
                    WHERE resolution.operation_id=effect.operation_id
                    AND resolution.sequence>effect.sequence AND resolution.kind='resolution'
                    AND json_extract(resolution.payload_json,'$.result.fields.outcome.value')
                        IN ('confirmed','rejected','not_run')
                    AND attempt.value=effect.attempt_id)))
        """
        workflows = () if workflow_id is None else ((workflow_id,) if isinstance(workflow_id, str) else workflow_id)
        if workflow_id is not None:
            candidates += " AND o.workflow_id IN (" + ",".join("?" for _ in workflows) + ")"
        rows=self._read(candidates+" ORDER BY o.operation_id", workflows)
        return tuple(row["operation_id"] for row in rows if (self._legacy_pending(row["operation_id"]) if row["current_phase"]=="legacy" else self._unresolved(row["operation_id"])))

    def _legacy_pending(self,operation_id):
        return not self._read("SELECT 1 FROM operation_events WHERE operation_id=? AND kind='legacy_resolved'",(operation_id,))

    def _operation_row(self,row):
        if row["current_phase"]=="legacy":
            evidence=self._decode(row["intent_json"],c.LegacyEvidence)
            if (evidence.account_key!=self.account_key or row["root_operation_id"]!=row["operation_id"] or row["corrective_parent_id"] is not None
                    or row["step_id"]!="legacy.import" or row["current_attempt_id"] is not None or row["current_result_event_id"] is not None
                    or any(row[name]!=0 for name in ("attempts_used","effect_intents_used","recovery_used","max_attempts","max_effect_intents","max_recovery","unresolved_attempts"))
                    or _unpack(row["timing_json"])!={"unrestorable":True}):
                raise StorageFailure()
            imported=self._read("SELECT payload_json FROM operation_events WHERE operation_id=? AND kind='legacy_imported'",(row["operation_id"],))
            if len(imported)!=1 or _unpack(imported[0][0])!={"evidence":evidence}: raise StorageFailure()
            for resolved in self._read("SELECT payload_json FROM operation_events WHERE operation_id=? AND kind='legacy_resolved'",(row["operation_id"],)):
                proof=_unpack(resolved[0]).get("evidence")
                if (type(proof) is not c.LegacyResolutionEvidence or proof.source_id!=evidence.source_id
                        or proof.source_record_id!=evidence.source_record_id or proof.scope.account_key!=self.account_key):
                    raise StorageFailure()
            return LegacyOperationRecord(row["operation_id"],row["workflow_id"],evidence,self._legacy_pending(row["operation_id"]))
        intent=self._decode(row["intent_json"],c.OperationIntent)
        if (intent.operation_id!=row["operation_id"] or intent.workflow_id!=row["workflow_id"] or intent.step_id!=row["step_id"]
                or intent.occurrence_id!=row["occurrence_id"] or intent.root_operation_id!=row["root_operation_id"] or intent.corrective_parent_id!=row["corrective_parent_id"]):
            raise StorageFailure()
        timing=_unpack(row["timing_json"])
        result=self._decode(row["aggregate_result_json"],c.StoredActionResult) if row["aggregate_result_json"] else None
        unresolved=self._unresolved(intent.operation_id)
        if len(unresolved)!=row["unresolved_attempts"] or (unresolved and row["aggregate_outcome"]!="unknown"):
            raise StorageFailure()
        return OperationRecord(intent,row["current_phase"],row["current_attempt_id"],row["current_result_event_id"],
            c.Outcome(row["aggregate_outcome"]) if row["aggregate_outcome"] else None,result,timing["scope_key"],timing["limits"],
            *(row[name] for name in ("attempts_used","effect_intents_used","recovery_used","max_attempts","max_effect_intents","max_recovery")),unresolved)

    @codec.reuse_immutable_values
    def operation(self,operation_id):
        rows=self._read("SELECT * FROM operations WHERE operation_id=?",(_id(operation_id),))
        try: return self._operation_row(rows[0]) if rows else None
        except (StorageFailure,ValueError,TypeError,KeyError):
            self.healthy=False
            raise StorageFailure() from None

    @codec.reuse_immutable_values
    def pending_for(self, scope, *, workflow_id=None):
        """Read all history for diagnostics, or only this run's unresolved work.

        A return-path workflow belongs to its original action. Include those
        children so interrupted recovery cannot let the parent dispatch again.
        """
        if scope.account_key != self.account_key:
            raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
        workflows = None
        if workflow_id is not None:
            workflows = (workflow_id, *(self.recovery_workflow_id(row[0]) for row in self._read(
                "SELECT operation_id FROM operations WHERE workflow_id=?", (_id(workflow_id),))))
        records = [self.operation(opid) for opid in self._pending_ids(workflows)]
        return tuple(c.PendingOperation(r.operation_id, r.workflow_id, r.current_attempt_id,
            r.operation_id if isinstance(r, LegacyOperationRecord) else r.intent.root_operation_id,
            c.Outcome.UNKNOWN, () if isinstance(r, LegacyOperationRecord) else r.unresolved_attempt_ids,
            isinstance(r, LegacyOperationRecord)) for r in records)

    @codec.reuse_immutable_values
    def blocking_pending_for(self, scope, workflow_id):
        cp = self.load_checkpoint(workflow_id)
        return self.pending_for_request(scope, workflow_id, self._saved_definition(cp),
                                        self.workflow_inputs(workflow_id))

    @codec.reuse_immutable_values
    def result_ref_for(self,operation_id):
        record=self.operation(operation_id)
        return c.ActionResultAvailable(operation_id,record.current_attempt_id,record.current_result_event_id) if record and record.current_result_event_id else None

    @codec.reuse_immutable_values
    def results_for(self,workflow_id):
        rows=self._read("SELECT o.operation_id FROM operations o JOIN operation_events e ON o.operation_id=e.operation_id AND e.kind='admission' WHERE o.workflow_id=? GROUP BY o.operation_id ORDER BY MIN(e.sequence)",(workflow_id,))
        refs=(self.result_ref_for(row[0]) for row in rows)
        return tuple(self.load_result(ref) for ref in refs if ref is not None)

    @codec.reuse_immutable_values
    def evidence_for(self,workflow_id):
        cp = self.load_checkpoint(workflow_id)
        try:
            proofs=[]
            for row in self._read("SELECT * FROM operation_events WHERE workflow_id=? AND kind='observation' ORDER BY sequence",(workflow_id,)):
                proof=_unpack(row["payload_json"])["evidence"]
                if type(proof) is not c.CorrelatedEvidence or proof.operation_id!=row["operation_id"] or proof.scope.account_key!=self.account_key:
                    raise ValueError()
                self._validate_interruption_sources(cp, proof)
                proofs.append(proof)
            return tuple(proofs)
        except (ValueError,TypeError,KeyError):
            self.healthy=False
            raise StorageFailure() from None

    @codec.reuse_immutable_values
    def interruption_reports_for(self, workflow_id):
        """Read historical report references; these carry no live authority."""
        self.evidence_for(workflow_id)
        return tuple((c.EvidenceRef(row["event_id"]), proof.details)
                     for row in self._read("SELECT event_id,payload_json FROM operation_events WHERE workflow_id=? AND kind='observation' ORDER BY sequence", (workflow_id,))
                     if type((proof := _unpack(row["payload_json"])["evidence"]).details) is c.InterruptionReport)

    @codec.reuse_immutable_values
    def load_result(self,ref):
        self._typed(ref,c.ActionResultAvailable)
        row=self._event_row(ref.result_event_id)
        if row["kind"] not in ("completion","resolution") or row["operation_id"]!=ref.operation_id or row["attempt_id"]!=ref.attempt_id:
            raise StorageFailure()
        value=_unpack(row["payload_json"])
        result = value["envelope"].result if row["kind"]=="completion" else value["result"]
        if type(result) is not c.StoredActionResult or (result.operation_id, result.attempt_id) != (ref.operation_id, ref.attempt_id):
            raise StorageFailure()
        if row["kind"] == "completion" and value["envelope"].ticket.workflow_id != row["workflow_id"]:
            raise StorageFailure()
        return result

    def _owner(self, cp, step_id):
        matches = [block for block in self._saved_definition(cp).blocks if step_id in block.step_ids]
        if len(matches) != 1:
            raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
        return matches[0]

    @codec.reuse_immutable_values
    def output_ref_for(self, operation_id):
        """Build an exact reference; this does not consume or authorize its use."""
        record = self.operation(operation_id)
        position = self.result_ref_for(operation_id)
        if record is None or position is None:
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        result = self.load_result(position)
        if result.output is None:
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        cp = self.load_checkpoint(record.workflow_id)
        schema = codec.payload_schema(result.output)
        definition = self._saved_definition(cp)
        key = "output"
        if definition.blocks:
            owner = self._owner(cp, record.intent.step_id)
            bindings = [b for b in owner.output_bindings
                        if b.producer_step_id == record.intent.step_id and b.output_schema == schema]
            if len(bindings) > 1:
                raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
            if bindings:
                key = bindings[0].output_key
        return c.OutputRef(operation_id, position.result_event_id, key, schema)

    def _resolve_ref(self, ref, cp, *, require_consumed=True, declared=False, before_sequence=None):
        self._typed(ref, c.OutputRef)
        record = self.operation(ref.operation_id)
        event = self._event_row(ref.result_event_id)
        if (record is None or isinstance(record, LegacyOperationRecord) or record.workflow_id != cp.workflow_id
                or record.intent.scope != cp.scope or record.intent.definition_id != cp.definition_id
                or record.intent.definition_version != cp.definition_version or event["workflow_id"] != cp.workflow_id):
            raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
        if event["operation_id"] != ref.operation_id or event["kind"] not in ("completion", "resolution"):
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        result = self.load_result(c.ActionResultAvailable(ref.operation_id, event["attempt_id"], ref.result_event_id))
        if (result.outcome != c.Outcome.CONFIRMED or result.output is None or record.pending
                or record.aggregate_outcome != c.Outcome.CONFIRMED):
            raise TransitionDenied(c.StopReason.PREREQUISITE_UNSATISFIED)
        schema = codec.payload_schema(result.output)
        if ref.output_schema is not None and ref.output_schema != schema:
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        definition = self._saved_definition(cp)
        if declared or definition.blocks:
            if ref.output_schema is None:
                raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            owner = self._owner(cp, record.intent.step_id)
            binding = c.OutputBinding(ref.output_key, schema, record.intent.step_id)
            if binding not in owner.output_bindings and (declared or ref.output_key != "output"):
                raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
        elif ref.output_key != "output":
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        if require_consumed:
            consumed = self._read("SELECT sequence,payload_json FROM operation_events WHERE operation_id=? AND kind='result_consumed'", (ref.operation_id,))
            if not any(_unpack(row["payload_json"])["from_result"].result_event_id == ref.result_event_id
                       and (before_sequence is None or row["sequence"] < before_sequence) for row in consumed):
                raise TransitionDenied(c.StopReason.PREREQUISITE_UNSATISFIED)
        self._validate_referenced_output(cp, result, record.intent)
        return result.output

    @codec.reuse_immutable_values
    def resolve_output_ref(self, ref, *, workflow_id, producer_block_id, producer_step_id, output_schema, parent_ref=None):
        cp = self.load_checkpoint(workflow_id)
        output = self._resolve_ref(ref, cp, declared=True)
        record = self.operation(ref.operation_id)
        owner = self._owner(cp, record.intent.step_id)
        if (owner.block_id != producer_block_id or record.intent.step_id != producer_step_id
                or ref.output_schema != output_schema):
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        if parent_ref is not None and not self._parent_matches(output, self._resolve_ref(parent_ref, cp, declared=True)):
            raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
        return output

    def _validate_dependencies(self, intent, cp, *, before_sequence=None):
        references = _payload_refs(intent.permitted_inputs)
        if not references:
            if type(intent.permitted_inputs) is c.RouteInputsV2 and intent.permitted_inputs.resources:
                raise TransitionDenied(c.StopReason.PREREQUISITE_UNSATISFIED)
            return
        definition = self._saved_definition(cp)
        if not definition.blocks:
            raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
        consumer = self._owner(cp, intent.step_id)
        by_key = dict(references)
        for key, ref in references:
            output = self._resolve_ref(ref, cp, declared=True, before_sequence=before_sequence)
            producer = self.operation(ref.operation_id)
            owner = self._owner(cp, producer.intent.step_id)
            if owner.block_id != consumer.block_id:
                matches = [b for b in consumer.input_bindings
                           if b.input_key == key and b.producer_block_id == owner.block_id
                           and b.producer_step_id == producer.intent.step_id and b.output_key == ref.output_key
                           and b.output_schema == ref.output_schema]
                if len(matches) != 1:
                    raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
                binding = matches[0]
                if binding.parent_input_key is not None:
                    parent = by_key.get(binding.parent_input_key)
                    if parent is None or not self._parent_matches(output, self._resolve_ref(parent, cp, declared=True)):
                        raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
            if hasattr(output, "account_id") and output.account_id != cp.scope.expected_account_id:
                raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
        if type(intent.permitted_inputs) is c.RouteInputsV2:
            resources = {resource for _, ref in references for resource in self._output_resources(self._resolve_ref(ref, cp, declared=True))}
            if not set(intent.permitted_inputs.resources) <= resources:
                raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)

    def _t05_child(self, ref, cp, block, stage, key, schema, expected_type):
        output = self._resolve_ref(ref, cp, declared=True)
        source = self.operation(ref.operation_id)
        if (type(output) is not expected_type or self._owner(cp, source.intent.step_id).block_id != block.block_id
                or source.intent.step_id != stage.step_ids[0] or (ref.output_key, ref.output_schema) != (key, schema)):
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        return output

    def _t05_positive(self, cp, result, refs, states, resource_id, verified_utc_us):
        if not refs or len(set(refs)) != len(refs) or not set(refs) <= set(result.evidence_refs):
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        proofs = self._proofs(cp, replace(result, evidence_refs=refs))
        times = tuple(proof.observed_utc_us for proof in proofs
            if type(proof.details) is c.ObservationDetails and proof.details.state in states
            and proof.details.resource_id == resource_id
            and proof.correlation_kind in ("bound_transition", "resource_identity", "server_operation_id"))
        if result.outcome != c.Outcome.CONFIRMED or not times or max(times) != verified_utc_us:
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)

    def _consumed_operation(self, operation_id, cp):
        record = self.operation(operation_id)
        external = self._external_consumed_operation(record, cp)
        if external is not None:
            return external
        if (record is None or isinstance(record, LegacyOperationRecord) or record.workflow_id != cp.workflow_id
                or record.intent.scope != cp.scope or record.intent.definition_id != cp.definition_id
                or record.intent.definition_version != cp.definition_version):
            raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
        events = self._read("SELECT payload_json FROM operation_events WHERE operation_id=? AND kind='result_consumed'", (operation_id,))
        if not events or record.pending or record.aggregate_outcome != c.Outcome.CONFIRMED:
            raise TransitionDenied(c.StopReason.PREREQUISITE_UNSATISFIED)
        return record

    def _validate_links(self,intent):
        if intent.root_operation_id==intent.operation_id:
            if intent.corrective_parent_id is not None: raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            return
        if intent.corrective_parent_id is None: raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        rows=self._read("SELECT workflow_id,root_operation_id FROM operations WHERE operation_id IN (?,?)",(intent.root_operation_id,intent.corrective_parent_id))
        if not rows or any(row["workflow_id"]!=intent.workflow_id or row["root_operation_id"]!=intent.root_operation_id for row in rows):
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        if len(rows)!=(1 if intent.root_operation_id==intent.corrective_parent_id else 2): raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        parent=intent.corrective_parent_id; seen={intent.operation_id}
        while parent is not None:
            if parent in seen: raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            seen.add(parent)
            row=self._read("SELECT corrective_parent_id FROM operations WHERE operation_id=?",(parent,))[0]
            parent=row[0]

    @codec.reuse_immutable_values
    def reserve_attempt(self,intent,position,*,scope_key,expected_revision,at):
        self._typed(intent,c.OperationIntent); self._typed(position,c.Ready)
        self._legacy_guard()
        with self._transaction():
            cp=self._cas(intent.workflow_id,expected_revision)
            if cp.status=="finished" or (cp.status!="running" and cp.recovery is None):
                raise TransitionDenied(c.StopReason.NEEDS_INPUT)
            if self._active(cp)!=position: raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
            step=self._step(cp,position.step_id)
            if (intent.scope!=cp.scope or intent.definition_id!=cp.definition_id or intent.definition_version!=cp.definition_version
                    or intent.step_id!=position.step_id or intent.occurrence_id!=position.occurrence_id
                    or intent.effect_kind!=step.effect_kind or intent.policy_id!=step.policy.policy_id
                    or intent.operation_id!=c.operation_id_for(cp.scope,cp.workflow_id,position.step_id,position.occurrence_id)):
                raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            if self.blocking_pending_for(cp.scope, cp.workflow_id): raise TransitionDenied(c.StopReason.NEEDS_INPUT)
            self._validate_links(intent)
            self._validate_dependencies(intent, cp)
            self._validate_original_inputs(intent, cp)
            if cp.recovery is not None and (intent.root_operation_id!=cp.recovery.root_operation_id or intent.corrective_parent_id is None):
                raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            scopes={s.scope_key.token():s for s in (*cp.time_scopes,*at.scopes)}
            if scope_key not in scopes: raise TransitionDenied(c.StopReason.CLOCK_UNTRUSTED)
            limits=dict(at.action_limits).get(intent.operation_id)
            if limits is None: raise TransitionDenied(c.StopReason.CLOCK_UNTRUSTED)
            from automation_core.workflow.timing import validate_limits_tightening
            try: validate_limits_tightening(scopes[scope_key].limits,limits)
            except ValueError: raise TransitionDenied(c.StopReason.CLOCK_UNTRUSTED) from None
            if limits.completion_only or limits.normal_remaining_cap_s<=0 or limits.normal_expires_utc_us<=at.sampled_utc_us:
                raise TransitionDenied(c.StopReason.DEADLINE_EXHAUSTED)
            existing=self.operation(intent.operation_id)
            if existing:
                if codec.dumps(existing.intent)!=codec.dumps(intent): raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
                if existing.aggregate_outcome==c.Outcome.CONFIRMED and intent.effect_kind==c.EffectKind.CONSEQUENTIAL:
                    raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
                token=cp.retry_admission
                if token is None or token.operation_id!=intent.operation_id or token.scope_key!=scope_key:
                    raise TransitionDenied(c.StopReason.RECOVERY_EXHAUSTED)
                admission=_unpack(self._event_row(token.episode_event_id)["payload_json"])
                if admission.get("episode_kind")!=c.RecoveryKind.RETRY: raise TransitionDenied(c.StopReason.RECOVERY_EXHAUSTED)
                if existing.attempts_used>=existing.max_attempts: raise TransitionDenied(c.StopReason.ADMISSION_EXHAUSTED)
                if existing.timing_scope_key!=scope_key: raise TransitionDenied(c.StopReason.CLOCK_UNTRUSTED)
                try: validate_limits_tightening(existing.limits,limits)
                except ValueError: raise TransitionDenied(c.StopReason.CLOCK_UNTRUSTED) from None
            else:
                if self._read("SELECT 1 FROM operations WHERE workflow_id=? AND step_id=? AND occurrence_id=?",(intent.workflow_id,intent.step_id,intent.occurrence_id)):
                    raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            attempt=uuid4().hex
            ticket=c.ActionTicket(cp.workflow_id,intent.operation_id,attempt,"recovery" if cp.recovery else "main",
                cp.recovery.frame_id if cp.recovery else None,cp.revision+1,scope_key,limits)
            if existing:
                self._connection.execute("UPDATE operations SET current_attempt_id=?,current_phase='reserved',current_outcome=NULL,current_result_event_id=NULL,attempts_used=attempts_used+1,timing_json=? WHERE operation_id=?",
                    (attempt,_pack(scope_key=scope_key,limits=limits),intent.operation_id))
            else:
                policy=step.policy
                self._connection.execute("""INSERT INTO operations(operation_id,workflow_id,step_id,occurrence_id,intent_json,policy_json,root_operation_id,corrective_parent_id,
                    current_attempt_id,current_phase,timing_json,unresolved_attempts,attempts_used,effect_intents_used,recovery_used,max_attempts,max_effect_intents,max_recovery)
                    VALUES (?,?,?,?,?,?,?,?,?,'reserved',?,0,1,0,0,?,?,?)""",
                    (intent.operation_id,cp.workflow_id,intent.step_id,intent.occurrence_id,codec.dumps(intent),codec.dumps(policy),intent.root_operation_id,intent.corrective_parent_id,
                     attempt,_pack(scope_key=scope_key,limits=limits),policy.max_attempts,policy.max_effect_intents,policy.max_recovery_admissions if intent.root_operation_id==intent.operation_id else 0))
            self._event("admission",cp,at,operation_id=intent.operation_id,attempt_id=attempt,ticket=ticket,retry_admission=cp.retry_admission)
            cp=self._with_position(cp,c.InAction(intent.operation_id,attempt))
            self._save(replace(cp,retry_admission=None),at)
        return ticket

    @codec.reuse_immutable_values
    def load_ticket(self,workflow_id,position):
        self._typed(position,c.InAction)
        cp=self.load_checkpoint(workflow_id)
        if self._active(cp)!=position: raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
        rows=self._read("SELECT payload_json FROM operation_events WHERE operation_id=? AND attempt_id=? AND kind='admission'",(position.operation_id,position.attempt_id))
        if len(rows)!=1: raise StorageFailure()
        ticket=_unpack(rows[0][0])["ticket"]
        effects=self._read("SELECT event_id FROM operation_events WHERE operation_id=? AND attempt_id=? AND kind='effect_intent'",(position.operation_id,position.attempt_id))
        return replace(ticket,effect_intent_event_id=effects[0][0] if effects else None)

    def _check_ticket(self,cp,ticket):
        saved=self.load_ticket(cp.workflow_id,c.InAction(ticket.operation_id,ticket.attempt_id))
        if saved!=ticket or ticket.workflow_id!=cp.workflow_id or (ticket.frame_id!=(cp.recovery.frame_id if cp.recovery else None)):
            raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
        return self.operation(ticket.operation_id)

    @codec.reuse_immutable_values
    def begin_action(self,ticket,*,expected_revision,at):
        self._typed(ticket,c.ActionTicket)
        self._legacy_guard()
        with self._transaction():
            cp=self._cas(ticket.workflow_id,expected_revision)
            record=self._check_ticket(cp,ticket)
            if record.current_phase!="reserved" or ticket.effect_intent_event_id is not None or record.intent.effect_kind==c.EffectKind.READ_ONLY:
                raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            if record.effect_intents_used>=record.max_effect_intents: raise TransitionDenied(c.StopReason.ADMISSION_EXHAUSTED)
            event=self._event("effect_intent",cp,at,operation_id=ticket.operation_id,attempt_id=ticket.attempt_id,ticket=ticket)
            self._connection.execute("UPDATE operations SET current_phase='intent',aggregate_outcome='unknown',unresolved_attempts=unresolved_attempts+1,effect_intents_used=effect_intents_used+1 WHERE operation_id=?",(ticket.operation_id,))
            self._save(cp,at)
        return replace(ticket,effect_intent_event_id=event)

    def resume_reserved_attempt(self, ticket, *, expected_revision, at):
        """An admitted ticket with no effect intent can still enter once."""
        self._legacy_guard()
        with self._transaction():
            cp = self._cas(ticket.workflow_id, expected_revision)
            op = self._check_ticket(cp, ticket)
            if (cp.recovery is not None or cp.retry_admission is not None
                    or op.current_phase != 'reserved' or op.effect_intents_used
                    or ticket.effect_intent_event_id is not None
                    or self.blocking_pending_for(cp.scope, cp.workflow_id)):
                raise TransitionDenied(c.StopReason.NEEDS_INPUT)
            if (ticket.limits.completion_only
                    or at.sampled_utc_us >= ticket.limits.normal_expires_utc_us):
                raise TransitionDenied(c.StopReason.DEADLINE_EXHAUSTED)
            self._event('resumed', cp, at, operation_id=op.operation_id,
                attempt_id=ticket.attempt_id, source_kind='reserved_attempt.v1')
            return self._save(replace(cp, status='running', stop_reason=None), at)

    def _insert_evidence(self,cp,evidence,at):
        if type(evidence) is not tuple: raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        # Reports may reference observations supplied in this same transaction.
        # Insert proofs first; neither becomes acknowledged until COMMIT returns.
        for submission in evidence:
            self._typed(submission,c.EvidenceSubmission)
        ordered = tuple(item for item in evidence if type(item.evidence.details) is not c.InterruptionReport)
        ordered += tuple(item for item in evidence if type(item.evidence.details) is c.InterruptionReport)
        for submission in ordered:
            self._typed(submission,c.EvidenceSubmission)
            proof=submission.evidence
            op=self.operation(proof.operation_id)
            if op is None or op.workflow_id!=cp.workflow_id or proof.scope!=cp.scope:
                raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
            known={row[0] for row in self._read("SELECT attempt_id FROM operation_events WHERE operation_id=? AND kind='admission'",(proof.operation_id,))}
            if not proof.attempt_ids or not set(proof.attempt_ids)<=known: raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            self._validate_interruption_sources(cp, proof)
            old=self._read("SELECT * FROM operation_events WHERE event_id=?",(submission.event_id,))
            if old:
                if old[0]["kind"]!="observation" or _unpack(old[0]["payload_json"])!={"evidence":proof}:
                    raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            else:
                self._event("observation",cp,at,operation_id=proof.operation_id,
                    attempt_id=proof.attempt_ids[0] if len(proof.attempt_ids)==1 else None,event_id=submission.event_id,evidence=proof)

    def _validate_interruption_sources(self, cp, proof):
        """References preserve the owning observation's original scope/attempt."""
        if type(proof.details) is not c.InterruptionReport:
            return
        for condition in proof.details.conditions:
            for reference in condition.source_refs:
                rows = self._read("SELECT * FROM operation_events WHERE event_id=?", (reference.event_id,))
                if len(rows) != 1:
                    raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
                source = rows[0]
                if (source["kind"] != "observation" or source["workflow_id"] != cp.workflow_id
                        or source["operation_id"] != proof.operation_id):
                    raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
                original = _unpack(source["payload_json"]).get("evidence")
                if (type(original) is not c.CorrelatedEvidence or original.scope != proof.scope
                        or not set(proof.attempt_ids) <= set(original.attempt_ids)
                        or original.observed_utc_us > proof.observed_utc_us):
                    raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
                # A descriptive report is not its own positive page/receipt proof.
                if type(original.details) is c.InterruptionReport:
                    raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
                if condition.kind == "page_availability" and condition.owner == "browser_driver":
                    if (type(original.details) is not c.ObservationDetails
                            or original.details.state != condition.cause_code
                            or original.correlation_kind != "definitive_noncompletion"):
                        raise TransitionDenied(c.StopReason.INTENT_MISMATCH)

    def _proofs(self,cp,result):
        proofs=[]
        for ref in result.evidence_refs:
            row=self._event_row(ref.event_id)
            if row["kind"]!="observation" or row["workflow_id"]!=cp.workflow_id or row["operation_id"]!=result.operation_id:
                raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
            proof=_unpack(row["payload_json"])["evidence"]
            if proof.scope!=cp.scope or result.attempt_id not in proof.attempt_ids:
                raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
            proofs.append(proof)
        authoritative = tuple(proof for proof in proofs if type(proof.details) is not c.InterruptionReport)
        if result.outcome in (c.Outcome.CONFIRMED,c.Outcome.REJECTED) and not authoritative:
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        if result.outcome==c.Outcome.REJECTED and not any(p.correlation_kind=="definitive_noncompletion" for p in authoritative):
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        return tuple(proofs)

    def _facts(self,cp,facts,*,result=None):
        if type(facts) is not tuple: raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        for fact in facts:
            self._typed(fact,c.StateFact)
            if fact.account_key!=self.account_key or fact.provenance.profile_id!=cp.scope.profile_id:
                raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
            event=self._event_row(fact.evidence_event_id)
            if event["kind"]!="observation" or event["workflow_id"]!=cp.workflow_id or event["operation_id"]!=fact.source_operation_id:
                raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
            proof=_unpack(event["payload_json"])["evidence"]
            if proof.scope!=cp.scope or proof.observer_id!=fact.provenance.observer_id or proof.observer_version!=fact.provenance.observer_version or proof.observed_utc_us!=fact.verified_utc_us:
                raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
            if result is not None and (result.outcome!=c.Outcome.CONFIRMED or fact.source_operation_id!=result.operation_id or fact.evidence_event_id not in {ref.event_id for ref in result.evidence_refs}):
                raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            self._validate_business_fact(cp, fact, result)
            self._connection.execute("""INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(account_key,scope_key,fact_key) DO UPDATE SET
                value_schema=excluded.value_schema,value_json=excluded.value_json,verified_utc_us=excluded.verified_utc_us,
                source_operation_id=excluded.source_operation_id,evidence_event_id=excluded.evidence_event_id,provenance_json=excluded.provenance_json
                WHERE excluded.verified_utc_us>=facts.verified_utc_us""",
                (fact.account_key,fact.scope_key,fact.fact_key,fact.value_schema,codec.dumps(fact.value),fact.verified_utc_us,fact.source_operation_id,fact.evidence_event_id,codec.dumps(fact.provenance)))

    def _aggregate(self,operation_id,result,event_id):
        unresolved=self._unresolved(operation_id)
        row=self._read("SELECT aggregate_result_json FROM operations WHERE operation_id=?",(operation_id,))[0]
        previous=self._decode(row[0],c.StoredActionResult) if row[0] else None
        aggregate=result
        if unresolved: aggregate=replace(result,outcome=c.Outcome.UNKNOWN)
        elif previous and previous.outcome==c.Outcome.CONFIRMED:
            aggregate=previous
        self._connection.execute("UPDATE operations SET aggregate_outcome=?,aggregate_result_json=?,current_phase='completed',current_outcome=?,current_result_event_id=?,unresolved_attempts=? WHERE operation_id=?",
            (aggregate.outcome.value,codec.dumps(aggregate),result.outcome.value,event_id,len(unresolved),operation_id))

    @codec.reuse_immutable_values
    def complete_action(self,ticket,result,facts=(),evidence=(),*,expected_revision,at):
        self._typed(ticket,c.ActionTicket); self._typed(result,c.StoredActionResult)
        envelope=c.CompletionEnvelope(ticket,result,facts,evidence)
        self._typed(envelope,c.CompletionEnvelope)
        if result.operation_id!=ticket.operation_id or result.attempt_id!=ticket.attempt_id:
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        # Exact replay is a read even if later parent work changed the revision.
        previous=self._read("SELECT payload_json FROM operation_events WHERE operation_id=? AND attempt_id=? AND kind='completion'",(ticket.operation_id,ticket.attempt_id))
        if previous:
            # A failed page check has no dispatch. A supported resume keeps its
            # ticket and appends the new observation instead of spending an attempt.
            resumed = self._read("SELECT payload_json FROM operation_events WHERE operation_id=? "
                "AND attempt_id=? AND kind='resumed' ORDER BY sequence DESC",
                (ticket.operation_id, ticket.attempt_id))
            rechecking = any(_unpack(row[0]).get('source_kind') == 'precheck.v1' for row in resumed)
            if (_unpack(previous[0][0])=={"envelope":envelope}
                    and not (rechecking and self.operation(ticket.operation_id).current_phase == 'reserved')):
                return self.load_checkpoint(ticket.workflow_id)
            if not rechecking:
                raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        with self._transaction():
            cp=self._cas(ticket.workflow_id,expected_revision)
            record=self._check_ticket(cp,ticket)
            if record.current_phase not in ("reserved","intent"): raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            if record.current_phase=="reserved" and record.intent.effect_kind!=c.EffectKind.READ_ONLY and result.outcome!=c.Outcome.NOT_RUN:
                if record.effect_intents_used or not self._satisfied_without_dispatch(cp, record.intent, result):
                    raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            if result.outcome==c.Outcome.NOT_RUN and result.receipt is not None:
                receipt=result.receipt
                if receipt.possibly_dispatched:
                    raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            self._insert_evidence(cp,evidence,at)
            self._proofs(cp,result)
            self._validate_output(cp, result, record.intent)
            self._facts(cp,facts,result=result)
            if previous:
                event=self._event('resolution',cp,at,operation_id=ticket.operation_id,
                    attempt_id=ticket.attempt_id,result=result,facts=facts,evidence=evidence,
                    resolved_attempts=(ticket.attempt_id,) if result.outcome is not c.Outcome.UNKNOWN else (),
                    source_kind='precheck.v1')
            else:
                event=self._event("completion",cp,at,operation_id=ticket.operation_id,attempt_id=ticket.attempt_id,envelope=envelope)
            self._aggregate(ticket.operation_id,result,event)
            cp=self._with_position(cp,c.ActionResultAvailable(ticket.operation_id,ticket.attempt_id,event))
            stop=result.stop_reason or (c.StopReason.NEEDS_INPUT if result.outcome==c.Outcome.UNKNOWN else None)
            if stop: cp=replace(cp,status="suspended",stop_reason=stop)
            cp=self._save(cp,at)
        return cp

    @codec.reuse_immutable_values
    def advance_workflow(self,from_result,next_position,*,expected_revision,at):
        self._typed(from_result,c.ActionResultAvailable)
        record=self.operation(from_result.operation_id)
        if record is None: raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        with self._transaction():
            cp=self._cas(record.workflow_id,expected_revision)
            if self._active(cp)!=from_result or (cp.status!="running" and cp.recovery is None):
                raise TransitionDenied(c.StopReason.NEEDS_INPUT)
            result=self.load_result(from_result)
            if not self.result_consumption_allowed(from_result):
                raise TransitionDenied(c.StopReason.NEEDS_INPUT)
            step=self._step(cp,record.intent.step_id)
            self._validate_successor(cp, record, result, next_position, main_only=cp.recovery is None)
            if isinstance(next_position,c.Ready):
                if next_position.step_id not in step.normal_next: raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
            elif isinstance(next_position,c.Finished):
                if step.normal_next or next_position.result_event_id not in (None,from_result.result_event_id):
                    raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
                definition = self._saved_definition(cp)
                if cp.recovery is None and definition.blocks:
                    owner = self._owner(cp, record.intent.step_id)
                    if not owner.final or record.intent.step_id != owner.completion_step_id:
                        raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
            else: raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
            refs=cp.output_refs
            if result.output is not None:
                if self._saved_definition(cp).blocks:
                    ref=self.output_ref_for(result.operation_id)
                else:
                    ref=c.OutputRef(result.operation_id,from_result.result_event_id,"output")
                if ref not in refs: refs=(*refs,ref)
            self._event("result_consumed",cp,at,operation_id=result.operation_id,attempt_id=result.attempt_id,from_result=from_result,next_position=next_position)
            cp=self._with_position(replace(cp,output_refs=refs),next_position)
            if isinstance(next_position,c.Finished):
                if cp.recovery is None: cp=replace(cp,status="finished",stop_reason=None)
                else: cp=replace(cp,recovery=replace(cp.recovery,phase="verifying"))
            cp=self._save(cp,at)
        return cp

    @codec.reuse_immutable_values
    def result_consumption_allowed(self,ref):
        record=self.operation(ref.operation_id)
        if record is None: return False
        cp=self.load_checkpoint(record.workflow_id)
        if self._active(cp)!=ref or (cp.status!="running" and cp.recovery is None) or self.blocking_pending_for(cp.scope, cp.workflow_id):
            return False
        result=self.load_result(ref)
        if result.outcome not in (c.Outcome.CONFIRMED,c.Outcome.REJECTED): return False
        if result.stop_reason is None: return True
        for event in self._read("SELECT payload_json FROM operation_events WHERE workflow_id=? AND kind='resumed' ORDER BY sequence DESC",(cp.workflow_id,)):
            if _unpack(event[0])["target"]==ref: return True
        return False

    @codec.reuse_immutable_values
    def record_observation(self,workflow_id,facts,evidence=(),*,expected_revision,at):
        with self._transaction():
            cp=self._cas(workflow_id,expected_revision)
            self._insert_evidence(cp,evidence,at)
            self._facts(cp,facts)
            cp=self._save(cp,at)
        return cp

    @codec.reuse_immutable_values
    def snapshot(self,scope):
        if scope.account_key!=self.account_key: raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
        facts=[]
        for row in self._read("SELECT * FROM facts WHERE account_key=? ORDER BY scope_key,fact_key",(self.account_key,)):
            fact=c.StateFact(row["fact_key"],row["scope_key"],row["account_key"],row["value_schema"],self._decode(row["value_json"]),
                row["verified_utc_us"],row["source_operation_id"],row["evidence_event_id"],self._decode(row["provenance_json"],c.FactProvenance))
            self._typed(fact,c.StateFact)
            event=self._event_row(fact.evidence_event_id)
            if event["kind"]!="observation" or event["operation_id"]!=fact.source_operation_id: raise StorageFailure()
            evidence=_unpack(event["payload_json"])["evidence"]
            if evidence.scope.account_key!=self.account_key: raise StorageFailure()
            self._validate_snapshot_fact(fact, event, evidence)
            if evidence.scope==scope: facts.append(fact)
        return c.StateSnapshot(tuple(facts))

    @codec.reuse_immutable_values
    def suspend_workflow(self,workflow_id,stop_reason,*,expected_revision,at):
        if type(stop_reason) is not c.StopReason: raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        with self._transaction():
            cp=self._cas(workflow_id,expected_revision)
            self._event("suspended",cp,at,stop_reason=stop_reason)
            cp=self._save(replace(cp,status="suspended",stop_reason=stop_reason),at)
        return cp

    @codec.reuse_immutable_values
    def ticket_for(self,operation_id,attempt_id=None):
        record=self.operation(operation_id)
        if record is None: raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        attempt_id=attempt_id or record.current_attempt_id
        rows=self._read("SELECT payload_json FROM operation_events WHERE operation_id=? AND attempt_id=? AND kind='admission'",(operation_id,attempt_id))
        if len(rows)!=1: raise StorageFailure()
        ticket=_unpack(rows[0][0])["ticket"]
        effects=self._read("SELECT event_id FROM operation_events WHERE operation_id=? AND attempt_id=? AND kind='effect_intent'",(operation_id,attempt_id))
        return replace(ticket,effect_intent_event_id=effects[0][0] if effects else None)

    def admit_preparation_restore(self, ticket, evidence, *, expected_revision, at):
        """Charge correction of an observed empty field on the entered attempt.

        This cannot activate a submission or replace another field's value.
        The owning source supplies current native absence evidence before input.
        """
        with self._transaction():
            cp = self._cas(ticket.workflow_id, expected_revision)
            op = self.operation(ticket.operation_id)
            position_matches = (isinstance(cp.main, (c.InAction, c.ActionResultAvailable))
                and (cp.main.operation_id, cp.main.attempt_id) == (ticket.operation_id, ticket.attempt_id))
            if (not position_matches
                    or cp.recovery is not None or cp.retry_admission is not None
                    or op.intent.effect_kind is not c.EffectKind.REPEATABLE_PREPARATION
                    or op.aggregate_outcome is not c.Outcome.UNKNOWN or op.recovery_used >= op.max_recovery
                    or cp.pending_operation_ids != (op.operation_id,)
                    or ticket != self.ticket_for(op.operation_id)):
                raise TransitionDenied(c.StopReason.NEEDS_INPUT)
            proofs = tuple(item.evidence for item in evidence)
            if not proofs or any(proof.operation_id != op.operation_id
                    or proof.attempt_ids != (ticket.attempt_id,)
                    or type(proof.details) is not c.ObservationDetails
                    or proof.details.state != 'preparation_field_empty' for proof in proofs):
                raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            if at.sampled_utc_us >= op.limits.normal_expires_utc_us:
                raise TransitionDenied(c.StopReason.DEADLINE_EXHAUSTED)
            self._insert_evidence(cp, evidence, at)
            self._event('preparation_restore_admitted', cp, at,
                operation_id=op.operation_id, attempt_id=ticket.attempt_id)
            self._connection.execute('UPDATE operations SET recovery_used=recovery_used+1 WHERE operation_id=?',
                (op.operation_id,))
            return self._save(replace(cp, status='running', stop_reason=None), at)

    def finish_preparation_restore(self, ticket, result, evidence, *, expected_revision, at):
        """Append native readback to an interrupted preparation's original result."""
        with self._transaction():
            cp = self._cas(ticket.workflow_id, expected_revision)
            op = self.operation(ticket.operation_id)
            if (op.intent.effect_kind is not c.EffectKind.REPEATABLE_PREPARATION
                    or op.aggregate_outcome is not c.Outcome.UNKNOWN
                    or cp.pending_operation_ids != (op.operation_id,)
                    or ticket != self.ticket_for(op.operation_id)
                    or result.outcome is not c.Outcome.CONFIRMED or result.stop_reason is not None
                    or (result.operation_id, result.attempt_id) != (op.operation_id, ticket.attempt_id)
                    or cp.recovery is not None or cp.retry_admission is not None):
                raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            self._insert_evidence(cp, evidence, at)
            self._proofs(cp, result)
            self._validate_output(cp, result, op.intent)
            event = self._event('resolution', cp, at, operation_id=op.operation_id,
                attempt_id=ticket.attempt_id, result=result, facts=(), evidence=evidence,
                resolved_attempts=(ticket.attempt_id,), source_kind='preparation_native_readback.v1')
            self._aggregate(op.operation_id, result, event)
            return self._save(replace(cp, status='running', stop_reason=None,
                main=c.ActionResultAvailable(op.operation_id, ticket.attempt_id, event)), at)

    def undispatched_restore_available(self, cp):
        if (type(cp.main) is not c.ActionResultAvailable or cp.recovery is not None
                or cp.retry_admission is not None):
            return False
        # A crash after admission leaves the original action uncertain. Only
        # readback may resolve it; the earlier motion receipt cannot authorize
        # another activation.
        if self._read("SELECT 1 FROM operation_events admission "
                "JOIN operation_events result ON result.event_id=? "
                "WHERE admission.operation_id=? AND admission.kind='undispatched_restore_admitted' "
                "AND admission.sequence>result.sequence LIMIT 1",
                (cp.main.result_event_id, cp.main.operation_id)):
            return False
        op = self.operation(cp.main.operation_id)
        receipt = self.load_result(cp.main).receipt
        return (op.aggregate_outcome in {c.Outcome.UNKNOWN, c.Outcome.NOT_RUN}
            and cp.pending_operation_ids in ((), (op.operation_id,))
            and op.effect_intents_used == 1
            and op.recovery_used < op.max_recovery
            and type(receipt) is c.StoredDispatchReceipt
            and receipt.phase in {'move', 'scroll', 'preparation'}
            and not receipt.activation_entered and not receipt.activation_returned
            and not receipt.dialogs and not receipt.cleanup_attempted
            and (receipt.reason in {'dispatch_policy_unknown_effect', 'dispatch_policy_stale_assessment',
                                    'binding_changed', 'stale_view'}
                 or receipt.reason == 'browser_call_failed'
                    and receipt.status == 'not_attempted' and receipt.entered_at is None))

    def admit_undispatched_restore(self, ticket, *, expected_revision, at):
        """Resume before activation, proven by the saved technical-motion receipt."""
        with self._transaction():
            cp = self._cas(ticket.workflow_id, expected_revision)
            if not self.undispatched_restore_available(cp) or ticket != self.ticket_for(ticket.operation_id):
                raise TransitionDenied(c.StopReason.NEEDS_INPUT)
            op = self.operation(ticket.operation_id)
            if at.sampled_utc_us >= op.limits.normal_expires_utc_us:
                raise TransitionDenied(c.StopReason.DEADLINE_EXHAUSTED)
            self._event('undispatched_restore_admitted', cp, at, operation_id=op.operation_id,
                attempt_id=ticket.attempt_id, previous_result=cp.main)
            self._connection.execute('UPDATE operations SET recovery_used=recovery_used+1 WHERE operation_id=?',
                (op.operation_id,))
            self._connection.execute("UPDATE operations SET aggregate_outcome='unknown',unresolved_attempts=1 WHERE operation_id=?",
                (op.operation_id,))
            return self._save(replace(cp, status='running', stop_reason=None), at)

    def finish_undispatched_restore(self, ticket, result, evidence, facts, *, expected_revision, at):
        with self._transaction():
            cp = self._cas(ticket.workflow_id, expected_revision)
            op = self.operation(ticket.operation_id)
            admitted = self._read("SELECT workflow_revision FROM operation_events WHERE operation_id=? "
                "AND attempt_id=? AND kind='undispatched_restore_admitted' ORDER BY sequence DESC LIMIT 1",
                (ticket.operation_id, ticket.attempt_id))
            if (not admitted or op.aggregate_outcome is not c.Outcome.UNKNOWN
                    or cp.pending_operation_ids != (op.operation_id,)
                    or ticket != self.ticket_for(op.operation_id)
                    or (result.operation_id, result.attempt_id) != (op.operation_id, ticket.attempt_id)):
                raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            self._insert_evidence(cp, evidence, at)
            self._proofs(cp, result)
            self._validate_output(cp, result, op.intent)
            self._facts(cp, facts, result=result)
            event = self._event('resolution', cp, at, operation_id=op.operation_id,
                attempt_id=ticket.attempt_id, result=result, facts=facts, evidence=evidence,
                resolved_attempts=(ticket.attempt_id,) if result.outcome is not c.Outcome.UNKNOWN else (),
                source_kind='undispatched_action_restore.v1')
            self._aggregate(op.operation_id, result, event)
            return self._save(replace(cp, main=c.ActionResultAvailable(op.operation_id, ticket.attempt_id, event),
                status='suspended' if result.stop_reason or result.outcome is c.Outcome.UNKNOWN else 'running',
                stop_reason=result.stop_reason), at)

    def _recovery_rule(self,cp,rule_id):
        matches=[r for r in self._definition(cp).recovery_rules if r.rule_id==rule_id]
        if len(matches)!=1: raise TransitionDenied(c.StopReason.NO_RECOVERY_RULE)
        return matches[0]

    def _episode(self,cp):
        if cp.recovery is None: raise TransitionDenied(c.StopReason.RECOVERY_EXHAUSTED)
        rows=self._read("SELECT * FROM operation_events WHERE workflow_id=? AND kind='recovery_admitted' ORDER BY sequence DESC",(cp.workflow_id,))
        for row in rows:
            payload=_unpack(row["payload_json"])
            if payload["episode_id"]==cp.recovery.episode_id: return row,payload
        raise StorageFailure()

    def _action_retry_candidate(self, cp, recoverable):
        if (recoverable is None or cp.status != 'suspended'
                or type(cp.main) is not c.ActionResultAvailable
                or cp.recovery is not None or cp.retry_admission is not None
                or self.blocking_pending_for(cp.scope, cp.workflow_id)):
            return None
        record = self.operation(cp.main.operation_id)
        result = self.load_result(cp.main)
        declared = self._step(cp, record.intent.step_id)
        if (record.intent.root_operation_id != record.operation_id
                or record.intent.effect_kind is not declared.effect_kind
                or record.current_phase != 'completed' or record.pending
                or record.aggregate_outcome is not c.Outcome.NOT_RUN
                or result.outcome is not c.Outcome.NOT_RUN
                or result.output is not None or result.receipt is not None
                or record.effect_intents_used
                or self.result_ref_for(record.operation_id) != cp.main):
            return None
        return record if recoverable(self, cp, record, result) is True else None

    @codec.reuse_immutable_values
    def action_retry_available(self, workflow_id, recoverable):
        return self._action_retry_candidate(self.load_checkpoint(workflow_id), recoverable) is not None

    @codec.reuse_immutable_values
    def retry_action(self, workflow_id, recoverable, *, expected_revision, at):
        """Recheck a declared read or undispatched control on its original ticket.

        Preserve the operation, inputs, deadlines and original failed result.
        No effect or uncertain result is eligible for this source-owned repair.
        """
        self._legacy_guard()
        with self._transaction():
            cp = self._cas(workflow_id, expected_revision)
            record = self._action_retry_candidate(cp, recoverable)
            if record is None:
                return None
            limits = dict(at.action_limits).get(record.operation_id, record.limits)
            from automation_core.workflow.timing import validate_limits_tightening
            try:
                validate_limits_tightening(record.limits, limits)
            except ValueError:
                raise TransitionDenied(c.StopReason.CLOCK_UNTRUSTED) from None
            if (limits.completion_only or limits.normal_remaining_cap_s <= 0
                    or limits.normal_expires_utc_us <= at.sampled_utc_us):
                raise TransitionDenied(c.StopReason.DEADLINE_EXHAUSTED)
            return self._resume_precheck(cp, record, at)

    def _resume_precheck(self, cp, record, at):
        """Called only after qualification and deadline checks in the transaction."""
        if record.effect_intents_used or record.pending:
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        self._event('resumed', cp, at, operation_id=record.operation_id,
            attempt_id=record.current_attempt_id, previous_result=cp.main, source_kind='precheck.v1')
        self._connection.execute("UPDATE operations SET current_phase='reserved' WHERE operation_id=?",
            (record.operation_id,))
        return self._save(replace(cp, status='running', stop_reason=None,
            main=c.InAction(record.operation_id, record.current_attempt_id)), at)

    def incomplete_read_available(self, cp, record):
        if (record is None or record.workflow_id != cp.workflow_id
                or record.intent.effect_kind is not c.EffectKind.READ_ONLY
                or record.effect_intents_used or record.pending or cp.recovery is not None
                or type(cp.main) not in (c.InAction, c.ActionResultAvailable)
                or cp.main.operation_id != record.operation_id
                or cp.main.attempt_id != record.current_attempt_id):
            return False
        if type(cp.main) is c.InAction:
            return record.current_phase == 'reserved' and record.aggregate_result is None
        result = self.load_result(cp.main)
        technical_read_failure = (result.stop_reason is c.StopReason.IMPLEMENTATION_ERROR
            and bool(result.evidence_refs) and all(type(proof.details) is c.ObservationDetails
                and proof.details.state.startswith('observe.expected_evidence.observed_')
                for proof in self._proofs(cp, result)))
        return (record.current_phase == 'completed' and result.outcome is c.Outcome.NOT_RUN
            and result.output is None and result.receipt is None
            and (technical_read_failure or result.stop_reason is c.StopReason.STALE_VIEW or not result.evidence_refs
                and result.stop_reason in (c.StopReason.NEEDS_INPUT, c.StopReason.IMPLEMENTATION_ERROR)))

    def resume_incomplete_read(self, workflow_id, operation_id, *, expected_revision, at):
        """Continue an admitted pure observation without replacing its ticket."""
        with self._transaction():
            cp = self._cas(workflow_id, expected_revision)
            record = self.operation(operation_id)
            if (cp.status != 'suspended' or not self.incomplete_read_available(cp, record)
                    or self.blocking_pending_for(cp.scope, workflow_id)):
                raise TransitionDenied(c.StopReason.NEEDS_INPUT)
            limits = dict(at.action_limits).get(operation_id, record.limits)
            if (limits.completion_only or limits.normal_remaining_cap_s <= 0
                    or limits.normal_expires_utc_us <= at.sampled_utc_us):
                raise TransitionDenied(c.StopReason.DEADLINE_EXHAUSTED)
            self._event('resumed', cp, at, target=cp.main, read_only=True,
                operation_id=operation_id, attempt_id=record.current_attempt_id)
            return self._save(replace(cp, status='running', stop_reason=None), at)

    def complete_read_observation(self, ticket, result, facts=(), evidence=(), *, expected_revision, at):
        """Save fresh proof after the old interruption placeholder, retaining it."""
        with self._transaction():
            cp = self._cas(ticket.workflow_id, expected_revision)
            record = self.operation(ticket.operation_id)
            if (cp.status != 'running' or type(cp.main) is not c.ActionResultAvailable
                    or not self.incomplete_read_available(cp, record)
                    or ticket != self.ticket_for(ticket.operation_id)
                    or result.operation_id != ticket.operation_id or result.attempt_id != ticket.attempt_id
                    or result.receipt is not None):
                raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            self._insert_evidence(cp, evidence, at)
            self._proofs(cp, result)
            self._validate_output(cp, result, record.intent)
            self._facts(cp, facts, result=result)
            event = self._event('resolution', cp, at, operation_id=ticket.operation_id,
                attempt_id=ticket.attempt_id, result=result, facts=facts, evidence=evidence,
                resolved_attempts=())
            self._aggregate(ticket.operation_id, result, event)
            stop = result.stop_reason or (c.StopReason.NEEDS_INPUT if result.outcome is not c.Outcome.CONFIRMED else None)
            return self._save(replace(cp, main=c.ActionResultAvailable(ticket.operation_id,
                ticket.attempt_id, event), status='suspended' if stop else 'running', stop_reason=stop), at)

    def claim_automatic_recovery(self, workflow_id, *, expected_revision, at):
        """Count automatic continuations against the original failed action.

        The existing journal is the counter, including after an owner restart.
        Advancing to another action naturally starts its own allowance. This
        admits recovery work only; effect admissions and deadlines stay intact.
        """
        with self._transaction():
            cp = self._cas(workflow_id, expected_revision)
            if not isinstance(cp.main, (c.InAction, c.ActionResultAvailable)):
                raise TransitionDenied(c.StopReason.NO_RECOVERY_RULE)
            operation_id = (cp.recovery.root_operation_id if cp.recovery else
                self.operation(cp.main.operation_id).intent.root_operation_id)
            record = self.operation(operation_id)
            rows = self._read("SELECT payload_json FROM operation_events WHERE operation_id=? AND kind='resumed'",
                (operation_id,))
            used = max((_unpack(row['payload_json']).get('automatic_attempt', 0) for row in rows), default=0)
            if used >= 1:
                raise TransitionDenied(c.StopReason.RECOVERY_EXHAUSTED)
            limits = dict(at.action_limits).get(operation_id, record.limits)
            expires = limits.completion_expires_utc_us if record.pending else limits.normal_expires_utc_us
            if expires <= at.sampled_utc_us:
                raise TransitionDenied(c.StopReason.DEADLINE_EXHAUSTED)
            self._event('resumed', cp, at, operation_id=operation_id,
                attempt_id=record.current_attempt_id, automatic_attempt=used + 1)
            return self._save(cp, at)

    def automatic_recoveries_used(self, operation_id):
        rows = self._read("SELECT payload_json FROM operation_events WHERE operation_id=? AND kind='resumed'",
            (operation_id,))
        return max((_unpack(row[0]).get('automatic_attempt', 0) for row in rows), default=0)

    def checking_window(self, ticket, seconds, *, expected_revision, at):
        """Persist one window per cycle; reattachment never restarts its clock."""
        with self._transaction():
            cp = self._cas(ticket.workflow_id, expected_revision)
            record = self.operation(ticket.operation_id)
            cycle = min(1, self.automatic_recoveries_used(record.intent.root_operation_id))
            rows = self._read("SELECT payload_json FROM operation_events WHERE operation_id=? AND kind='resumed' ORDER BY rowid",
                (ticket.operation_id,))
            windows = [_unpack(row[0]) for row in rows]
            saved = next((item for item in reversed(windows) if item.get('checking_cycle') == cycle), None)
            if saved is None:
                expires = min(at.sampled_utc_us + int(seconds * 1e6),
                    record.limits.completion_expires_utc_us)
                if record.intent.root_operation_id != ticket.operation_id:
                    parent_expires = self.checking_window_expires(record.intent.root_operation_id)
                    if parent_expires is not None:
                        expires = min(expires, parent_expires)
                self._event('resumed', cp, at, operation_id=ticket.operation_id,
                    attempt_id=ticket.attempt_id, checking_cycle=cycle,
                    checking_started_utc_us=at.sampled_utc_us, checking_expires_utc_us=expires)
                cp = self._save(cp, at)
            else:
                expires = min(saved['checking_expires_utc_us'],
                    saved['checking_started_utc_us'] + int(seconds * 1e6))
                if expires < saved['checking_expires_utc_us']:
                    self._event('resumed', cp, at, operation_id=ticket.operation_id,
                        attempt_id=ticket.attempt_id, checking_cycle=cycle,
                        checking_started_utc_us=saved['checking_started_utc_us'], checking_expires_utc_us=expires)
                    cp = self._save(cp, at)
            return cp, expires

    def popup_response(self, ticket, handler_id, *, expected_revision, at):
        """Link a response to this action; retain an unfinished child's identity."""
        with self._transaction():
            cp = self._cas(ticket.workflow_id, expected_revision)
            rows = self._read("SELECT payload_json FROM operation_events WHERE operation_id=? AND kind='resumed' ORDER BY rowid",
                (ticket.operation_id,))
            children = [_unpack(row[0])['popup_response_workflow'] for row in rows
                if _unpack(row[0]).get('popup_handler') == handler_id]
            if children and (not self.workflow_exists(children[-1])
                    or self.load_checkpoint(children[-1]).status != 'finished'):
                return cp, children[-1]
            if self.blocking_pending_for(cp.scope, cp.workflow_id):
                raise TransitionDenied(c.StopReason.NEEDS_INPUT)
            child = str(uuid4())
            self._event('resumed', cp, at, operation_id=ticket.operation_id,
                attempt_id=ticket.attempt_id, popup_handler=handler_id, popup_response_workflow=child)
            return self._save(cp, at), child

    def checking_window_expires(self, operation_id):
        cycle = min(1, self.automatic_recoveries_used(operation_id))
        rows = self._read("SELECT payload_json FROM operation_events WHERE operation_id=? AND kind='resumed' ORDER BY rowid DESC",
            (operation_id,))
        return next((_unpack(row[0])['checking_expires_utc_us'] for row in rows
            if _unpack(row[0]).get('checking_cycle') == cycle), None)

    def claim_reconnect(self, workflow_id, *, expected_revision, at):
        """Charge before reconnecting; never renew action or workflow time."""
        with self._transaction():
            cp = self._cas(workflow_id, expected_revision)
            if cp.recovery_policy_version != 2 or cp.status == "finished":
                raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
            # Once an action exists, the runner has charged the shared automatic
            # recovery allowance. This count is then history, not a second cap.
            if not isinstance(cp.main, (c.InAction, c.ActionResultAvailable)) and cp.reconnect_attempts >= 3:
                raise TransitionDenied(c.StopReason.RECOVERY_EXHAUSTED)
            if isinstance(cp.main, (c.InAction, c.ActionResultAvailable)):
                root = self.operation(cp.main.operation_id)
                limits = dict(at.action_limits).get(root.operation_id, root.limits)
                if limits.normal_expires_utc_us <= at.sampled_utc_us or limits.normal_remaining_cap_s <= 0:
                    raise TransitionDenied(c.StopReason.DEADLINE_EXHAUSTED)
            self._event("resumed", cp, at, transport_only=True,
                        reconnect_attempt=cp.reconnect_attempts + 1)
            return self._save(replace(cp, reconnect_attempts=cp.reconnect_attempts + 1), at)

    def original_dispatch_receipt(self, operation_id, attempt_id):
        """Receipt of the attempt's input, independent of later readback.

        A restored dispatch supersedes its earlier pre-input rejection. An
        interrupted restoration has no receipt proving that input did not run.
        """
        record = self.operation(operation_id)
        if record is None:
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        rows = self._read("SELECT event_id,kind,payload_json FROM operation_events "
            "WHERE operation_id=? AND attempt_id=? "
            "AND kind IN ('completion','resolution','undispatched_restore_admitted') ORDER BY sequence DESC",
            (operation_id, attempt_id))
        for row in rows:
            if row['kind'] == 'undispatched_restore_admitted':
                return None
            if (row['kind'] == 'resolution'
                    and _unpack(row['payload_json']).get('source_kind') not in
                        {'undispatched_action_restore.v1', 'precheck.v1'}):
                continue
            receipt = self.load_result(c.ActionResultAvailable(operation_id, attempt_id, row['event_id'])).receipt
            if receipt is not None:
                return receipt
        return None

    def resume_or_begin_recovery(self, root_operation_id, rule_id, **kwargs):
        """Continue one saved recovery; legacy banks keep admission accounting."""
        return self._begin_recovery(root_operation_id, rule_id, resume_existing=True, **kwargs)

    def begin_recovery(self, root_operation_id, rule_id, **kwargs):
        return self._begin_recovery(root_operation_id, rule_id, resume_existing=False, **kwargs)

    @codec.reuse_immutable_values
    def _begin_recovery(self,root_operation_id,rule_id,*,episode_id,episode_kind,expected_revision,at,
                        resume_existing=False):
        _id(episode_id)
        if type(episode_kind) is not c.RecoveryKind: raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        root=self.operation(root_operation_id)
        if root is None or root.intent.root_operation_id!=root_operation_id:
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        # Repeating the same recorded admission never spends or enters it twice.
        events=self._read("SELECT * FROM operation_events WHERE workflow_id=? AND kind='recovery_admitted'",(root.workflow_id,))
        identity=dict(root_operation_id=root_operation_id,rule_id=rule_id,episode_id=episode_id,episode_kind=episode_kind,episode_epoch=at.clock_epoch)
        for event in events:
            payload=_unpack(event["payload_json"])
            if payload["episode_id"]==episode_id:
                if any(payload[key]!=value for key,value in identity.items()): raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
                return self.load_checkpoint(root.workflow_id)
        with self._transaction():
            cp=self._cas(root.workflow_id,expected_revision)
            rule=self._recovery_rule(cp,rule_id)
            if rule.kind!=episode_kind: raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            if cp.status=="finished": raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
            pending=self.blocking_pending_for(cp.scope, cp.workflow_id)
            if any(self.operation(p.operation_id).intent.root_operation_id!=root_operation_id for p in pending):
                raise TransitionDenied(c.StopReason.NEEDS_INPUT)
            if pending and episode_kind!=c.RecoveryKind.RECONCILIATION:
                raise TransitionDenied(c.StopReason.NEEDS_INPUT)
            root=self.operation(root_operation_id)
            continuing = (resume_existing and cp.recovery_policy_version == 2
                          and cp.recovery is not None
                          and cp.recovery.root_operation_id == root_operation_id)
            if not continuing and root.recovery_used>=root.max_recovery:
                raise TransitionDenied(c.StopReason.RECOVERY_EXHAUSTED)
            if continuing and at.sampled_utc_us >= cp.recovery.wait_expires_utc_us:
                raise TransitionDenied(c.StopReason.DEADLINE_EXHAUSTED)
            if episode_kind!=c.RecoveryKind.RECONCILIATION:
                limits=dict(at.action_limits).get(root_operation_id,root.limits)
                if limits.completion_only or limits.normal_remaining_cap_s<=0 or limits.normal_expires_utc_us<=at.sampled_utc_us:
                    raise TransitionDenied(c.StopReason.DEADLINE_EXHAUSTED)
            if cp.recovery is not None:
                if cp.recovery.root_operation_id!=root_operation_id:
                    raise TransitionDenied(c.StopReason.RECOVERY_EXHAUSTED)
                if (continuing and episode_kind == cp.recovery.episode_kind
                        and (episode_kind != c.RecoveryKind.CORRECTION or rule_id == cp.recovery.rule_id)):
                    # Same process needs no write. A new clock epoch records a
                    # fresh proof boundary, retaining the branch cursor and bank.
                    if cp.recovery.episode_epoch == at.clock_epoch:
                        return cp
                    frame = replace(cp.recovery, episode_id=episode_id, episode_epoch=at.clock_epoch)
                elif episode_kind == c.RecoveryKind.CORRECTION:
                    # A charged, read-only reconciliation may discover that a
                    # correction is needed before any branch action exists. Keep
                    # its original return point and frame; this is not nesting.
                    current = cp.recovery
                    current_rule = self._recovery_rule(cp, current.rule_id)
                    children = self._read("SELECT operation_id FROM operations WHERE workflow_id=? AND root_operation_id=? AND operation_id!=?",
                                          (cp.workflow_id, root_operation_id, root_operation_id))
                    if (current_rule.kind != c.RecoveryKind.RECONCILIATION
                            or current.episode_kind != c.RecoveryKind.RECONCILIATION
                            or current.episode_epoch != at.clock_epoch or current.phase != "running"
                            or current.position != current.return_position or children or pending):
                        raise TransitionDenied(c.StopReason.RECOVERY_EXHAUSTED)
                    self._step(cp, rule.branch_id)
                    frame = replace(current, rule_id=rule_id,
                                    position=c.Ready(rule.branch_id,"recovery."+str(root.recovery_used+1)),
                                    continuation_id=rule.continuation_id, episode_id=episode_id,
                                    episode_epoch=at.clock_epoch, episode_kind=episode_kind)
                else:
                    frame=replace(cp.recovery,episode_id=episode_id,episode_epoch=at.clock_epoch,episode_kind=episode_kind)
            else:
                position=c.Ready(rule.branch_id,"recovery."+str(root.recovery_used+1)) if rule.branch_id else cp.main
                if rule.branch_id is not None: self._step(cp,rule.branch_id)
                frame=c.RecoveryFrame(uuid4().hex,root_operation_id,rule_id,cp.main,position,rule.continuation_id,episode_id,at.clock_epoch,episode_kind)
            if cp.recovery_policy_version == 2 and not continuing:
                limits = dict(at.action_limits).get(root_operation_id, root.limits)
                # A recovery cannot extend the interrupted operation's deadline.
                frame = replace(frame, wait_expires_utc_us=limits.completion_expires_utc_us)
                if at.sampled_utc_us >= frame.wait_expires_utc_us:
                    raise TransitionDenied(c.StopReason.DEADLINE_EXHAUSTED)
            if not continuing:
                self._connection.execute("UPDATE operations SET recovery_used=recovery_used+1 WHERE operation_id=?",(root_operation_id,))
            self._event("recovery_admitted",cp,at,operation_id=root_operation_id,frame_id=frame.frame_id,
                        **identity)
            cp=self._save(replace(cp,status="suspended",recovery=frame),at)
        return cp

    @codec.reuse_immutable_values
    def record_resolution(self,operation_id,result,facts=(),evidence=(),*,expected_revision,at):
        self._typed(result,c.StoredActionResult)
        if result.outcome==c.Outcome.NOT_RUN:
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        record=self.operation(operation_id)
        if record is None or result.operation_id!=operation_id: raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        with self._transaction():
            cp=self._cas(record.workflow_id,expected_revision)
            if (cp.recovery is None or cp.recovery.root_operation_id!=record.intent.root_operation_id
                    or cp.recovery.episode_kind!=c.RecoveryKind.RECONCILIATION or cp.recovery.episode_epoch!=at.clock_epoch):
                raise TransitionDenied(c.StopReason.RECOVERY_EXHAUSTED)
            self._insert_evidence(cp,evidence,at)
            proofs=self._proofs(cp,result)
            self._validate_output(cp, result, record.intent)
            unresolved=self._unresolved(operation_id)
            if result.attempt_id not in unresolved: raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            identified=set().union(*(set(p.attempt_ids) for p in proofs)) if proofs else {result.attempt_id}
            resolved=tuple(attempt for attempt in unresolved if attempt in identified)
            if len(resolved)>1:
                policy=self._step(cp,record.intent.step_id).policy
                if policy.replay_policy_id is None or not any(p.correlation_kind=="website_idempotency" and set(resolved)<=set(p.attempt_ids) for p in proofs):
                    raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            self._facts(cp,facts,result=result)
            old_current=self.result_ref_for(operation_id)
            old_current_outcome=self.load_result(old_current).outcome if old_current is not None else None
            before=self.operation(operation_id)
            event=self._event("resolution",cp,at,operation_id=operation_id,attempt_id=result.attempt_id,
                result=result,facts=facts,evidence=evidence,resolved_attempts=resolved if result.outcome!=c.Outcome.UNKNOWN else ())
            self._aggregate(operation_id,result,event)
            if result.attempt_id!=record.current_attempt_id:
                self._connection.execute("UPDATE operations SET current_phase=?,current_outcome=?,current_result_event_id=? WHERE operation_id=?",
                    (before.current_phase,_value(old_current_outcome),before.current_result_event_id,operation_id))
            ref=c.ActionResultAvailable(operation_id,result.attempt_id,event)
            def updated(position):
                if isinstance(position,(c.InAction,c.ActionResultAvailable)) and position.operation_id==operation_id and position.attempt_id==result.attempt_id: return ref
                return position
            frame=replace(cp.recovery,position=updated(cp.recovery.position),return_position=updated(cp.recovery.return_position))
            cp=self._save(replace(cp,main=updated(cp.main),recovery=frame,status="suspended",stop_reason=result.stop_reason or c.StopReason.NEEDS_INPUT),at)
        return cp

    def _commit_current_observation(self, cp, result, evidence, at, *, source_kind):
        """Save a verified current result without changing its original timing bank."""
        with self._transaction():
            self._cas(cp.workflow_id, cp.revision)
            original_time = self.load_time_checkpoint(cp.workflow_id)
            self._insert_evidence(cp, evidence, at)
            self._proofs(cp, result)
            self._validate_output(cp, result, self.operation(result.operation_id).intent)
            event = self._event('resolution', cp, at, operation_id=result.operation_id,
                attempt_id=result.attempt_id, result=result, facts=(), evidence=evidence,
                resolved_attempts=(result.attempt_id,), source_kind=source_kind,
                original_checkpoint=cp, original_time=original_time)
            self._aggregate(result.operation_id, result, event)
            saved = replace(cp, revision=cp.revision + 1, status='running', stop_reason=None,
                main=c.ActionResultAvailable(result.operation_id, result.attempt_id, event),
                pending_operation_ids=self._pending_ids(cp.workflow_id))
            changed = self._connection.execute('UPDATE workflows SET checkpoint_json=?,revision=?,status=?,updated_utc_us=? '
                'WHERE workflow_id=? AND revision=?',
                (codec.dumps(saved), saved.revision, saved.status, at.sampled_utc_us, cp.workflow_id, cp.revision))
            if changed.rowcount != 1:
                raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
        return saved

    @codec.reuse_immutable_values
    def resume_workflow(self,workflow_id,continuation_id,observations,*,expected_revision,at):
        if type(observations) is not tuple or not observations: raise TransitionDenied(c.StopReason.NEEDS_INPUT)
        with self._transaction():
            cp=self._cas(workflow_id,expected_revision)
            frame=cp.recovery
            if frame is None or frame.continuation_id!=continuation_id or frame.episode_epoch!=at.clock_epoch:
                raise TransitionDenied(c.StopReason.RECOVERY_EXHAUSTED)
            if self.blocking_pending_for(cp.scope, cp.workflow_id): raise TransitionDenied(c.StopReason.NEEDS_INPUT)
            continuations=[item for item in self._definition(cp).continuations if item.continuation_id==continuation_id]
            if len(continuations)!=1: raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
            continuation=continuations[0]
            episode,payload=self._episode(cp)
            root=self.operation(frame.root_operation_id)
            for proof in observations:
                self._typed(proof,c.CorrelatedEvidence)
                if type(proof.details) is c.InterruptionReport:
                    raise TransitionDenied(c.StopReason.NEEDS_INPUT)
                if proof.scope!=cp.scope or proof.observed_utc_us<episode["utc_us"]:
                    raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
                operation=self.operation(proof.operation_id)
                if operation is None or operation.intent.root_operation_id!=root.operation_id:
                    raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
                if proof not in self.evidence_for(workflow_id):
                    raise TransitionDenied(c.StopReason.NEEDS_INPUT)
            if root.operation_id not in {p.operation_id for p in observations}: raise TransitionDenied(c.StopReason.NEEDS_INPUT)
            correction_rule=self._recovery_rule(cp,frame.rule_id)
            if correction_rule.kind==c.RecoveryKind.CORRECTION:
                if not isinstance(frame.position,c.Finished): raise TransitionDenied(c.StopReason.NEEDS_INPUT)
                children=self._read("SELECT operation_id FROM operations WHERE workflow_id=? AND root_operation_id=? AND operation_id!=?",(workflow_id,root.operation_id,root.operation_id))
                if any(self.operation(row[0]).aggregate_outcome!=c.Outcome.CONFIRMED for row in children):
                    raise TransitionDenied(c.StopReason.NEEDS_INPUT)
                if children and not ({row[0] for row in children}&{p.operation_id for p in observations}):
                    raise TransitionDenied(c.StopReason.NEEDS_INPUT)
            if any(key not in {ref.output_key for ref in cp.output_refs} for key in continuation.required_output_keys):
                raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
            retry=None
            if continuation.retry_original:
                if frame.episode_kind!=c.RecoveryKind.RETRY: raise TransitionDenied(c.StopReason.RECOVERY_EXHAUSTED)
                if root.aggregate_outcome not in (c.Outcome.NOT_RUN,c.Outcome.REJECTED): raise TransitionDenied(c.StopReason.NEEDS_INPUT)
                if root.aggregate_outcome==c.Outcome.REJECTED and self._step(cp,root.intent.step_id).policy.replay_policy_id is None:
                    raise TransitionDenied(c.StopReason.NEEDS_INPUT)
                retry=c.RetryAdmission(root.operation_id,episode["event_id"],root.timing_scope_key)
                target=c.Ready(root.intent.step_id,root.intent.occurrence_id)
                if continuation.target_step_id!=root.intent.step_id: raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
            elif root.aggregate_outcome==c.Outcome.CONFIRMED:
                target=self.result_ref_for(root.operation_id)
            else:
                target=c.Ready(continuation.target_step_id,"resume."+frame.frame_id)
            if continuation.restore_step_ids:
                # Restoration must be represented by declared normal successors.
                for index,name in enumerate(continuation.restore_step_ids):
                    step=self._step(cp,name)
                    successor=continuation.restore_step_ids[index+1] if index+1<len(continuation.restore_step_ids) else continuation.target_step_id
                    if not step.restore_allowed or step.effect_kind!=c.EffectKind.REPEATABLE_PREPARATION or successor not in step.normal_next:
                        raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
                target=c.Ready(continuation.restore_step_ids[0],"restore."+frame.frame_id)
            self._event("recovery_verified",cp,at,operation_id=root.operation_id,observations=observations,frame_id=frame.frame_id)
            self._event("resumed",cp,at,operation_id=root.operation_id,continuation_id=continuation_id,frame_id=frame.frame_id,target=target,retry_admission=retry)
            cp=self._save(replace(cp,main=target,recovery=None,retry_admission=retry,status="running",stop_reason=None),at)
        return cp

    @codec.reuse_immutable_values
    def legacy_records(self):
        return tuple(self._operation_row(row) for row in self._read("SELECT * FROM operations WHERE current_phase='legacy' ORDER BY operation_id"))

    def _resolved_legacy_sources(self):
        grouped={}
        for record in self.legacy_records():
            grouped.setdefault(record.evidence.source_id,[]).append(record)
        return tuple((items[0].evidence.source_path,items[0].evidence.source_identity)
                     for items in grouped.values() if all(not item.pending for item in items))

    @codec.reuse_immutable_values
    def resolve_legacy(self,evidence,*,expected_revisions):
        """Explicit reviewed evidence clears blockers; no legacy execution is admitted."""
        from automation_core.storage.legacy_state import _source_identity
        from automation_core.workflow.timing import TimeCheckpoint
        if type(evidence) is not tuple or not evidence:
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        records={(r.evidence.source_id,r.evidence.source_record_id):r for r in self.legacy_records()}
        if len({(p.source_id,p.source_record_id) for p in evidence})!=len(evidence):
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        for proof in evidence:
            self._typed(proof,c.LegacyResolutionEvidence)
            record=records.get((proof.source_id,proof.source_record_id))
            if record is None or proof.scope!=self.scope:
                raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
            historical=record.evidence.source_record_id.startswith(("metadata","fact:","scope:"))
            if proof.conclusion=="historical_metadata_reviewed" and (not historical or record.evidence.source_kind=="verification_journal"):
                raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            path=self.root/record.evidence.source_path
            if not path.resolve().is_relative_to(self.root) or _source_identity(path)!=record.evidence.source_identity:
                raise TransitionDenied(c.StopReason.LEGACY_STATE_BLOCKED)
        affected=tuple(sorted({records[(p.source_id,p.source_record_id)].workflow_id for p in evidence}))
        revisions=dict(expected_revisions)
        checkpoints=[]
        with self._transaction():
            for workflow_id in affected:
                cp=self.load_checkpoint(workflow_id)
                if cp.definition_id!="legacy.import" or cp.definition_version!=0 or revisions.get(workflow_id)!=cp.revision:
                    raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
                selected=tuple(p for p in evidence if records[(p.source_id,p.source_record_id)].workflow_id==workflow_id)
                changed=False; last_event=None
                for proof in selected:
                    record=records[(proof.source_id,proof.source_record_id)]
                    previous=self._read("SELECT payload_json,event_id FROM operation_events WHERE operation_id=? AND kind='legacy_resolved'",(record.operation_id,))
                    if previous:
                        if _unpack(previous[0][0])!={"evidence":proof}: raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
                        last_event=previous[0][1]
                        continue
                    moment=TimeCheckpoint("legacy-review",proof.observed_utc_us,max(cp.utc_high_water_us,proof.observed_utc_us),(),())
                    last_event=self._event("legacy_resolved",cp,moment,operation_id=record.operation_id,evidence=proof)
                    changed=True
                if changed:
                    pending=self._pending_ids(workflow_id)
                    moment=TimeCheckpoint("legacy-review",max(p.observed_utc_us for p in selected),max(cp.utc_high_water_us,*(p.observed_utc_us for p in selected)),(),())
                    cp=replace(cp,status="legacy_blocked" if pending else "finished",main=c.Finished(last_event),
                               stop_reason=c.StopReason.LEGACY_STATE_BLOCKED if pending else None)
                    cp=self._save(cp,moment)
                checkpoints.append(cp)
        return tuple(checkpoints)


    def _validate_transaction_guard(self, guard):
        if guard is not None:
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)

    def _can_resume_related_recovery(self, workflow_id):
        from automation_core.workflow.known_path_recovery import candidate
        return candidate(self, workflow_id, include_pending=True) is not None

    @classmethod
    def recovery_workflow_id(cls, operation_id):
        from automation_core.workflow.known_path_recovery import workflow_id
        return workflow_id(operation_id, cls.recovery_namespace)

    def recover_from_sources(self, app, sources):
        from automation_core.workflow.known_path_recovery import _recover_from_sources
        return _recover_from_sources(app, sources)

    def _validate_workflow_inputs(self, inputs, workflow_id, scope, definition):
        if inputs is not None:
            self._typed(inputs)
        if type(inputs) in (c.KnownPathRecoveryInputs, c.ComponentRecoveryInputs):
            from automation_core.workflow.known_path_recovery import validate_inputs
            validate_inputs(self, inputs, workflow_id, scope, definition)

    def _validate_new_workflow(self, definition, workflow_id, link):
        if link is not None:
            raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)

    def _continuation_fields(self, definition, workflow_id, link):
        self._validate_new_workflow(definition, workflow_id, link)
        return {}

    def _validate_continuation(self, link, cp):
        if link is not None:
            raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)

    def pending_for_request(self, scope, workflow_id, definition, inputs):
        """Fresh runs use current page checks; only their own effects can block.

        The original action may be observed by its registered recovery route.
        Its pending record remains intact until that observation resolves it.
        """
        if (definition.definition_id == self.recovery_namespace and definition.version in (1, 2, 3)
                and type(inputs) in (c.KnownPathRecoveryInputs, c.ComponentRecoveryInputs)):
            from automation_core.workflow.known_path_recovery import observed_pending, validate_source
            observation = observed_pending(self, inputs, workflow_id, scope, definition)
            parent, _ = validate_source(self, inputs, workflow_id, scope)
            pending = self.pending_for(scope, workflow_id=parent.workflow_id)
            return tuple(item for item in pending if not observation
                or item.operation_id != inputs.failed_result.operation_id)
        return self.pending_for(scope, workflow_id=workflow_id)

    def popup_prerequisite_pending(self, workflow_id):
        return ()

    def _validate_referenced_output(self, cp, result, intent):
        pass

    @staticmethod
    def _parent_matches(child, parent):
        return False

    def _external_consumed_operation(self, record, cp):
        return None

    def _validate_original_inputs(self, intent, cp):
        pass

    @staticmethod
    def _output_resources(output):
        if type(output) in (c.AccountOutput, c.AuthenticationCompletionOutput):
            return (c.ResourceIdentity('account', output.account_id),)
        return ()

    def _validate_business_fact(self, cp, fact, result=None):
        pass

    def _satisfied_without_dispatch(self, cp, intent, result):
        from automation_core.workflow.known_path_recovery import satisfied_without_dispatch
        return satisfied_without_dispatch(self, cp, intent, result)

    def _validate_snapshot_fact(self, fact, event, evidence):
        pass

    def _validate_output(self, cp, result, intent):
        self._validate_site_output(cp, result, intent)
        output = result.output
        if type(intent.permitted_inputs) is c.ConsentInputs:
            if output is not None and (type(output) is not c.ConsentOutput
                                      or output.choice_id != intent.permitted_inputs.choice_id):
                raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            if result.outcome == c.Outcome.CONFIRMED and (output is None or not output.preference_acknowledged):
                raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            if output is not None and (result.outcome == c.Outcome.NOT_RUN
                                      or (result.outcome == c.Outcome.REJECTED and output.preference_acknowledged)):
                raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        elif type(output) is c.ConsentOutput:
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        if result.receipt is not None and type(result.receipt) is c.ProviderReceipt:
            request = intent.permitted_inputs
            receipt = result.receipt
            if (type(request) is not c.ProviderCommandInputs or
                    (receipt.provider_id, receipt.command, receipt.request_id) !=
                    (request.provider_id, request.command, request.request_id) or
                    (request.activation is not None and receipt.activation not in (None, request.activation)) or
                    (request.activation is not None and receipt.status == "returned" and receipt.activation != request.activation)):
                raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        if output is None:
            return
        for _, ref in _payload_refs(output):
            self._resolve_ref(ref, cp, declared=True)
        prior_output = False
        for reference in cp.output_refs:
            if reference.operation_id == intent.operation_id:
                continue
            if self._resolve_ref(reference, cp) == output:
                prior_output = True
                break
        account = getattr(output, "account_id", getattr(output, "mapped_account_id", None))
        if account is not None and account != cp.scope.expected_account_id:
            raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
        self._validate_publication_output(cp, result, intent)
        # Embedded evidence is checked independently of its closed wire shape.
        proof_refs = tuple(getattr(output, "evidence_refs", ()))
        for field_name in ("eligibility_evidence_refs", "access_evidence_refs", "publication_evidence_refs"):
            proof_refs += tuple(getattr(output, field_name, ()))
        required, allowed_refs = self._output_evidence_policy(result, intent)
        if required and not prior_output and not set(proof_refs) <= set(allowed_refs):
            raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
        for proof_ref in proof_refs:
            event = self._event_row(proof_ref.event_id)
            proof_workflow = self._output_evidence_workflow(cp, output)
            if event["kind"] != "observation" or event["workflow_id"] != proof_workflow:
                raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
            proof = _unpack(event["payload_json"])["evidence"]
            if proof.scope != cp.scope:
                raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
        for source_name in ("creation_operation_id", "publication_operation_id"):
            source = getattr(output, source_name, None)
            if source is not None and source != intent.operation_id:
                self._consumed_operation(source, cp)
        self._validate_domain_output(cp, result, intent)
        if type(output) is c.BlockCompletionOutput:
            owner = self._owner(cp, intent.step_id)
            requested = intent.permitted_inputs
            if self._validate_special_completion(cp, result, intent, owner):
                return
            if (intent.step_id != owner.completion_step_id or output.block_id != owner.block_id
                    or type(requested) not in self.completion_input_types
                    or (output.block_id, output.outputs, output.successor_block_id) !=
                    (requested.block_id, requested.child_output_refs, requested.successor_block_id)
                    or (output.successor_block_id is not None and output.successor_block_id not in owner.allowed_successor_block_ids)
                    or (output.successor_block_id is None and not owner.final)):
                raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
        elif type(output) is c.AuthenticationCompletionOutput:
            owner = self._owner(cp, intent.step_id)
            if (intent.step_id != owner.completion_step_id
                    or any(self._owner(cp, self.operation(ref.operation_id).intent.step_id).block_id != owner.block_id
                           for ref in output.child_output_refs)):
                raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
        elif type(output) is c.PrerequisiteRequirement:
            owner = self._owner(cp, intent.step_id)
            caller = output.caller
            continuation = next((item for item in self._saved_definition(cp).continuations
                                 if item.continuation_id == caller.continuation_id), None)
            if (output.source_operation_id != intent.operation_id
                    or output.source_evidence_ref not in result.evidence_refs
                    or (caller.block_id, caller.step_id, caller.occurrence_id, caller.input_schema) !=
                    (owner.block_id, intent.step_id, intent.occurrence_id, intent.input_schema)
                    or continuation is None or continuation.target_step_id not in owner.step_ids
                    or set(caller.input_refs) != {ref for _, ref in _payload_refs(intent.permitted_inputs)}):
                raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            available = {resource for ref in (*caller.input_refs, *caller.output_refs)
                         for resource in self._output_resources(self._resolve_ref(ref, cp, declared=True))}
            if output.resource not in available:
                raise TransitionDenied(c.StopReason.IDENTITY_MISMATCH)
        elif type(output) is c.ActivationOutput:
            requested = intent.permitted_inputs
            receipt = result.receipt
            if (type(requested) is not c.ProviderCommandInputs or output.activation.provider_id != requested.provider_id
                    or output.request_id != requested.request_id or type(receipt) is not c.ProviderReceipt
                    or (requested.activation is not None and output.activation != requested.activation)
                    or (receipt.activation is not None and output.activation != receipt.activation)
                    or (requested.command == "acquire" and receipt.activation is None)
                    or (requested.max_cost_micros is not None and output.cost_micros is not None
                        and output.cost_micros > requested.max_cost_micros)):
                raise TransitionDenied(c.StopReason.INTENT_MISMATCH)

    def retry_known_path(self, request, recovery_id, *, expected_revision, at):
        """Recheck the original ticket after a Finished component return path."""
        from automation_core.workflow.known_path_recovery import candidate
        BLOCK, workflow_id = self.recovery_namespace, self.recovery_workflow_id
        from automation_core.workflow.timing import validate_limits_tightening
        self._legacy_guard()
        with self._transaction():
            cp = self._cas(request.workflow_id, expected_revision)
            if candidate(self, request.workflow_id) != c.KnownPathRecoveryInputs(request.workflow_id, request.failed_result):
                raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
            record = self.operation(request.failed_result.operation_id)
            recovery = self.load_checkpoint(recovery_id)
            ready = self.operation(c.operation_id_for(cp.scope, recovery_id, BLOCK + '.ready', '0'))
            if (recovery_id != workflow_id(record.operation_id) or recovery.scope != cp.scope
                    or self.workflow_inputs(recovery_id) != request
                    or recovery.definition_id != BLOCK or recovery.definition_version not in (1, 2, 3)
                    or recovery.status != 'finished' or type(recovery.main) is not c.Finished
                    or recovery.pending_operation_ids or recovery.recovery is not None
                    or ready is None or ready.aggregate_outcome is not c.Outcome.CONFIRMED):
                raise TransitionDenied(c.StopReason.PREREQUISITE_UNSATISFIED)
            limits = dict(at.action_limits).get(record.operation_id, record.limits)
            try:
                validate_limits_tightening(record.limits, limits)
            except ValueError:
                raise TransitionDenied(c.StopReason.CLOCK_UNTRUSTED) from None
            if (limits.completion_only or limits.normal_remaining_cap_s <= 0
                    or limits.normal_expires_utc_us <= at.sampled_utc_us):
                raise TransitionDenied(c.StopReason.DEADLINE_EXHAUSTED)
            return self._resume_precheck(cp, record, at)

    def validate_legacy_recovery(self, inputs, recovery_id, scope, definition):
        return False

    def observes_legacy_recovery(self, inputs):
        return False

    def resume_legacy_recovery(self, app, sources):
        return False

    def page_refresh_route(self, app, request, source=None, component_ready=None):
        return None

    def _validate_site_output(self, cp, result, intent):
        pass

    def _validate_publication_output(self, cp, result, intent):
        pass

    def _output_evidence_policy(self, result, intent):
        required = type(result.output) in (c.EmailAccessOutput, c.EmailBaselineOutput,
                                          c.EmailSelectionOutput, c.ActivationOutput)
        return required, result.evidence_refs

    def _output_evidence_workflow(self, cp, output):
        return cp.workflow_id

    def _validate_domain_output(self, cp, result, intent):
        pass

    def _validate_special_completion(self, cp, result, intent, owner):
        return False

    @property
    def recovery_version(self):
        from automation_core.workflow.known_path_recovery import CURRENT_VERSION
        return CURRENT_VERSION

    def recovery_failure_directory(self):
        from automation_core.diagnostics.failure_capture import default_failure_directory
        return default_failure_directory()
