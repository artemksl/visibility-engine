"""Conservative read-only admission guard during legacy/new-store coexistence.

This first boundary does not migrate, delete or reinterpret a legacy journal.
An explicit evidence-based migration can later grant source-specific exceptions.
"""
import hashlib
import json
import os
from pathlib import Path
import stat


class LegacyStateBlocked(RuntimeError):
    def __init__(self):
        super().__init__("Legacy account state requires explicit reconciliation.")


class LegacyStateGuard:
    def __init__(self,state_root:Path,scope,ownership,resolved_sources=(), *, owned_transaction_journal=None):
        self.root=Path(state_root).resolve()
        self.scope=scope
        self.ownership=ownership
        self.resolved_sources=dict(resolved_sources)
        self.owned_transaction_journal=owned_transaction_journal

    def _resolved(self,path):
        relative=path.relative_to(self.root).as_posix()
        return self.resolved_sources.get(relative)==_source_identity(path)

    def check(self):
        self.ownership.assert_owned()
        if self.root!=Path(self.ownership.root).resolve(): raise LegacyStateBlocked()
        account=self.root/"accounts"/self.scope.account_key
        try:
            if not account.resolve().is_relative_to(self.root): raise LegacyStateBlocked()
            # Existence alone is enough; reading private journal payloads is unnecessary.
            journal=account/"verification.json"
            if (journal.exists() or journal.is_symlink()) and not self._resolved(journal): raise LegacyStateBlocked()
            if account.exists():
                for path in account.iterdir():
                    if path == self.owned_transaction_journal:
                        info = path.lstat()
                        if (path != account / "automation.sqlite3-journal"
                                or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                                or info.st_mode & 0o077):
                            raise LegacyStateBlocked()
                        continue
                    if path.name!="automation.sqlite3" and (".sqlite" in path.name or path.suffix==".db") and not self._resolved(path):
                        raise LegacyStateBlocked()
            binding=self.root/"profiles"/hashlib.sha256(self.scope.profile_id.encode()).hexdigest()[:32]/"binding.json"
            if binding.exists() or binding.is_symlink():
                if binding.is_symlink() or not stat.S_ISREG(binding.stat().st_mode): raise LegacyStateBlocked()
                data=json.loads(binding.read_text())
                if (type(data) is not dict or data.get("version")!=1 or data.get("account_key")!=self.scope.account_key
                        or data.get("profile_id")!=self.scope.profile_id or data.get("status") not in ("ready","existing")):
                    raise LegacyStateBlocked()
        except (OSError,ValueError,TypeError):
            raise LegacyStateBlocked() from None


def _source_identity(path):
    info=path.stat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink(): raise LegacyStateBlocked()
    return info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns


def _source_records(path,scope,root):
    """Read only a closed projection; never copy private payloads or their hashes."""
    import sqlite3
    import re
    from uuid import NAMESPACE_URL,uuid5
    from automation_core.contracts import LegacyEvidence
    from automation_core.storage.persistence import dumps
    relative=path.relative_to(root).as_posix()
    identity=_source_identity(path)
    source_id=str(uuid5(NAMESPACE_URL,"automation-legacy-source:"+relative+":"+str(identity)))
    base=dict(source_id=source_id,source_schema=1,account_key=scope.account_key,source_path=relative,source_identity=identity)
    def safe(value):
        if value is None: return None
        if not isinstance(value,str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}",value) is None: raise LegacyStateBlocked()
        return value
    records=[]
    if path.name=="verification.json":
        data=json.loads(path.read_text())
        if type(data) is not dict or data.get("version")!=1 or data.get("phase") not in {"request_intent","awaiting_message","submit_intent","callback_completed"}:
            raise LegacyStateBlocked()
        records.append(LegacyEvidence(**base,source_kind="verification_journal",source_record_id="journal:"+safe(data.get("run_id")),
            profile_id=scope.profile_id,phase=data["phase"],outcome="unknown"))
    else:
        db=sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)
        try:
            db.row_factory=sqlite3.Row
            if db.execute("PRAGMA integrity_check").fetchone()[0]!="ok": raise LegacyStateBlocked()
            tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables!={"metadata","scopes","operations","events","facts"} or db.execute("PRAGMA user_version").fetchone()[0]!=1:
                raise LegacyStateBlocked()
            meta=db.execute("SELECT version,account_key FROM metadata").fetchall()
            if len(meta)!=1 or tuple(meta[0])!=(1,scope.account_key): raise LegacyStateBlocked()
            records.append(LegacyEvidence(**base,source_kind="sqlite_v1",source_record_id="metadata",phase="unknown"))
            for row in db.execute("SELECT * FROM operations ORDER BY operation_id"):
                data=dict(row)
                records.append(LegacyEvidence(**base,source_kind="sqlite_v1",source_record_id="operation:"+safe(data["operation_id"]),
                    workflow_id=safe(data["workflow_id"]),operation_id=safe(data["operation_id"]),attempt_id=safe(data["attempt_id"]),
                    action_id=safe(data["action_id"]),phase="intent" if data["outcome"] is None else "completed",outcome=data["outcome"],
                    timing_scope_key=safe(data.get("timing_scope_key")),normal_until=data.get("normal_until"),completion_until=data.get("completion_until")))
            for row in db.execute("SELECT * FROM events ORDER BY sequence"):
                data=dict(row)
                records.append(LegacyEvidence(**base,source_kind="sqlite_v1",source_record_id="event:"+str(data["sequence"]),
                    workflow_id=safe(data["workflow_id"]),operation_id=safe(data["operation_id"]),attempt_id=safe(data["attempt_id"]),
                    action_id=safe(data["action_id"]),phase=data["phase"],outcome=data["outcome"],
                    timing_scope_key=safe(data.get("timing_scope_key")),normal_until=data.get("normal_until"),completion_until=data.get("completion_until")))
            for row in db.execute("SELECT * FROM facts ORDER BY key"):
                # A historical fact is not promoted to a trusted v2 fact. Its private
                # value is unnecessary to preserve the blocker and is not imported.
                records.append(LegacyEvidence(**base,source_kind="sqlite_v1",source_record_id="fact:"+safe(row["key"]),
                    operation_id=safe(row["operation_id"]),phase="verified"))
            for row in db.execute("SELECT scope_key FROM scopes ORDER BY scope_key"):
                records.append(LegacyEvidence(**base,source_kind="sqlite_v1",source_record_id="scope:"+safe(row[0]),
                    timing_scope_key=safe(row[0]),phase="unknown"))
        finally: db.close()
    if _source_identity(path)!=identity: raise LegacyStateBlocked()
    for record in records: dumps(record)
    return tuple(records)


def migrate_legacy_store(state_dir,scope,ownership,*,source_database=None,include_journal=True,fault_hook=None):
    """Explicit offline preservation + blocked import. Never grants resume rights.

    The outer caller holds account/profile ownership. This function additionally
    holds the existing mailbox-journal lock while examining its source. Interrupted
    publication leaves a migrating marker and requires explicit repair.
    """
    from contextlib import ExitStack
    from dataclasses import replace
    import os
    from uuid import uuid4,NAMESPACE_URL,uuid5
    from automation_core import contracts as c
    from automation_core.storage import persistence as codec
    from automation_core.storage.memory import AccountMemory,StorageFailure,_SCHEMA,_json,_pack
    from automation_core.workflow.timing import TimeCheckpoint
    from automation_core.storage.locking import _lock
    from automation_core.storage.private_files import _save
    connection=None
    try:
        root,folder,target,marker_path=AccountMemory._paths(state_dir,scope,ownership)
        folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        if marker_path.exists(): raise LegacyStateBlocked()
        source=Path(source_database).resolve() if source_database is not None else (target if target.exists() else None)
        if source is not None and (not source.is_relative_to(root) or not source.is_file()): raise LegacyStateBlocked()
        if target.exists() and source!=target: raise LegacyStateBlocked()
        journal=folder/"verification.json"
        sources=[source] if source is not None else []
        if include_journal and (journal.exists() or journal.is_symlink()): sources.append(journal)
        if not sources: raise LegacyStateBlocked()
        with ExitStack() as stack:
            if journal in sources:
                mailbox_digest=hashlib.sha256(scope.account_key.encode()).hexdigest()
                stack.enter_context(_lock(folder/(".mailverify-"+mailbox_digest+".lock")))
            records=tuple(item for path in sources for item in _source_records(path,scope,root))
            store_uuid=uuid4().hex
            staged=folder/(".automation-migration-"+store_uuid+".sqlite3")
            archive=folder/("automation-v1-"+store_uuid+".sqlite3") if source==target else None
            manifests=tuple(dict(path=path.relative_to(root).as_posix(),identity=list(_source_identity(path)),
                                 record_count=sum(item.source_path==path.relative_to(root).as_posix() for item in records)) for path in sources)
            if archive is not None:
                original_relative=source.relative_to(root).as_posix()
                records=tuple(replace(record,source_path=archive.relative_to(root).as_posix())
                    if record.source_path==original_relative else record for record in records)
            marker=dict(version=1,state="migrating",schema_version=2,account_key=scope.account_key,store_uuid=store_uuid,database=target.name,
                        sources=manifests,staged=staged.name,archive=archive.name if archive else None)
            _save(marker_path,marker)
            if fault_hook: fault_hook("after_migrating_marker")
            descriptor=os.open(staged,os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o600); os.close(descriptor)
            connection=AccountMemory._connect(staged)
            connection.execute("BEGIN IMMEDIATE"); connection.executescript(_SCHEMA); connection.execute("PRAGMA user_version=2")
            connection.execute("INSERT INTO operation_events(event_id,run_id,kind,utc_us,payload_json) VALUES (?,?,?,?,?)",
                (uuid4().hex,"migration","store_initialized",0,_json(dict(schema_version=2,account_key=scope.account_key,store_uuid=store_uuid))))
            for source_id in sorted({r.source_id for r in records}):
                items=tuple(r for r in records if r.source_id==source_id)
                wid="legacy."+source_id
                opids=tuple("legacy."+str(uuid5(NAMESPACE_URL,source_id+":"+record.source_record_id)) for record in items)
                cp=c.WorkflowCheckpoint(workflow_id=wid,definition_id="legacy.import",definition_version=0,revision=0,scope=scope,
                    status="legacy_blocked",main=c.Finished(),pending_operation_ids=tuple(sorted(opids)),stop_reason=c.StopReason.LEGACY_STATE_BLOCKED)
                at=TimeCheckpoint("legacy",0,0,(),())
                connection.execute("INSERT INTO workflows VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (wid,"legacy.import",0,scope.account_key,codec.dumps(scope),cp.status,codec.dumps(cp),codec.dumps(at),0,0,0))
                for opid,record in zip(opids,items):
                    connection.execute("""INSERT INTO operations(operation_id,workflow_id,step_id,occurrence_id,intent_json,policy_json,root_operation_id,
                        aggregate_outcome,current_phase,timing_json,unresolved_attempts,attempts_used,effect_intents_used,recovery_used,max_attempts,max_effect_intents,max_recovery)
                        VALUES (?,?,'legacy.import',?,?,?,?,'unknown','legacy',?,0,0,0,0,0,0,0)""",
                        (opid,wid,record.source_record_id,codec.dumps(record),codec.dumps(c.EmptyPayload()),opid,_pack(unrestorable=True)))
                    connection.execute("INSERT INTO operation_events(event_id,workflow_id,operation_id,run_id,kind,workflow_revision,utc_us,payload_json) VALUES (?,?,?,?,?,?,?,?)",
                        (uuid4().hex,wid,opid,"migration","legacy_imported",0,0,_pack(evidence=record)))
            if fault_hook: fault_hook("before_import_commit")
            connection.execute("COMMIT")
            if fault_hook: fault_hook("after_import_commit")
            connection.close(); connection=None
            # Validate all source counts/identities before publication. No source
            # payload is removed, rewritten or promoted into executable intent.
            for path,manifest in zip(sources,manifests):
                if list(_source_identity(path))!=manifest["identity"]: raise LegacyStateBlocked()
            validation=object.__new__(AccountMemory)
            validation._connection=AccountMemory._connect(staged)
            validation.healthy=True; validation.ownership=ownership; validation.scope=scope
            validation.root=root; validation.path=staged; validation.account_key=scope.account_key; validation.store_uuid=store_uuid
            validation._definitions={}; validation._thread=__import__("threading").get_ident(); validation._fault_hook=None
            try: validation._validate_database()
            finally: validation.close()
            descriptor=os.open(staged,os.O_RDONLY)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
            if archive is not None:
                os.replace(source,archive)
                if fault_hook: fault_hook("after_archive")
            os.replace(staged,target)
            descriptor=os.open(folder,os.O_RDONLY)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
            if fault_hook: fault_hook("after_publish")
            # Source/archive provenance remains in typed events. A migrating marker
            # carries repair paths until publication; ready has the strict runtime schema.
            _save(marker_path,{key:marker[key] for key in ("version","schema_version","account_key","store_uuid","database")}|{"state":"ready"})
            return len(records)
    except LegacyStateBlocked:
        raise
    except Exception:
        raise StorageFailure() from None
    finally:
        if connection is not None: connection.close()


def _migration_marker(root,scope,marker_path):
    value=json.loads(marker_path.read_text())
    if (type(value) is not dict or value.get("version")!=1 or value.get("schema_version")!=2 or value.get("state")!="migrating"
            or value.get("account_key")!=scope.account_key or value.get("database")!="automation.sqlite3"
            or type(value.get("sources")) not in (list,tuple)):
        raise LegacyStateBlocked()
    for name in ("staged","archive"):
        item=value.get(name)
        if item is not None and (not isinstance(item,str) or Path(item).name!=item): raise LegacyStateBlocked()
    for source in value["sources"]:
        if (type(source) is not dict or set(source)!={"path","identity","record_count"}
                or not isinstance(source["path"],str) or Path(source["path"]).is_absolute()
                or ".." in Path(source["path"]).parts or not (root/source["path"]).resolve().is_relative_to(root)
                or type(source["record_count"]) is not int or source["record_count"]<1):
            raise LegacyStateBlocked()
    return value


def _has_genesis(path,store_uuid):
    import sqlite3
    if not path.is_file() or path.is_symlink(): return False
    db=sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)
    try:
        row=db.execute("SELECT payload_json FROM operation_events WHERE kind='store_initialized'").fetchone()
        return row is not None and json.loads(row[0]).get("store_uuid")==store_uuid
    except (sqlite3.Error,ValueError,TypeError): return False
    finally: db.close()


def _validate_migration_candidate(path,root,scope,ownership,store_uuid):
    import threading
    from automation_core.storage.memory import AccountMemory
    memory=object.__new__(AccountMemory)
    memory._connection=AccountMemory._connect(path)
    memory.healthy=True; memory.ownership=ownership; memory.scope=scope
    memory.root=root; memory.path=path; memory.account_key=scope.account_key; memory.store_uuid=store_uuid
    memory._definitions={}; memory._thread=threading.get_ident(); memory._fault_hook=None
    try:
        memory._validate_database()
        return memory.legacy_records()
    finally: memory.close()


def repair_legacy_migration(state_dir,scope,ownership,*,action):
    """Explicit repair only: validate and publish, or retain artifacts and abort."""
    from contextlib import ExitStack
    import os
    from automation_core.storage.memory import AccountMemory,StorageFailure
    from automation_core.storage.private_files import _save
    from automation_core.storage.locking import _lock
    if action not in ("complete","abort"): raise LegacyStateBlocked()
    try:
        root,folder,target,marker_path=AccountMemory._paths(state_dir,scope,ownership)
        AccountMemory._private_file(marker_path)
        initial=json.loads(marker_path.read_text())
        if type(initial) is dict and initial.get("state")=="initializing":
            from automation_core.storage.memory import _MARKER_FIELDS,_id
            if (set(initial)!=_MARKER_FIELDS or initial.get("version")!=1 or initial.get("schema_version")!=2
                    or initial.get("account_key")!=scope.account_key or initial.get("database")!=target.name):
                raise LegacyStateBlocked()
            _id(initial["store_uuid"])
            if action=="complete":
                AccountMemory._private_file(target)
                records=_validate_migration_candidate(target,root,scope,ownership,initial["store_uuid"])
                if records: raise LegacyStateBlocked()
                _save(marker_path,dict(initial,state="ready"))
            else:
                aborted=folder/(".automation-aborted-"+initial["store_uuid"])
                if aborted.exists(): raise LegacyStateBlocked()
                aborted.mkdir(mode=0o700)
                if target.exists():
                    AccountMemory._private_file(target)
                    os.replace(target,aborted/"target.sqlite3")
                os.replace(marker_path,aborted/"initialization.json")
                for directory in (aborted,folder):
                    descriptor=os.open(directory,os.O_RDONLY)
                    try: os.fsync(descriptor)
                    finally: os.close(descriptor)
            return action
        marker=_migration_marker(root,scope,marker_path)
        staged=folder/marker["staged"]
        archive=folder/marker["archive"] if marker["archive"] else None
        with ExitStack() as stack:
            if any(Path(source["path"]).name=="verification.json" for source in marker["sources"]):
                digest=hashlib.sha256(scope.account_key.encode()).hexdigest()
                stack.enter_context(_lock(folder/(".mailverify-"+digest+".lock")))
            for source in marker["sources"]:
                path=root/source["path"]
                if path==target and archive is not None and archive.exists(): path=archive
                if list(_source_identity(path))!=source["identity"]: raise LegacyStateBlocked()
            target_is_import=_has_genesis(target,marker["store_uuid"])
            if action=="complete":
                candidate=target if target_is_import else staged
                if not _has_genesis(candidate,marker["store_uuid"]): raise LegacyStateBlocked()
                records=_validate_migration_candidate(candidate,root,scope,ownership,marker["store_uuid"])
                for source in marker["sources"]:
                    imported=[record for record in records if list(record.evidence.source_identity)==source["identity"]]
                    if len(imported)!=source["record_count"]: raise LegacyStateBlocked()
                if not target_is_import:
                    if archive is not None and not archive.exists(): os.replace(target,archive)
                    elif target.exists(): raise LegacyStateBlocked()
                    os.replace(staged,target)
                descriptor=os.open(folder,os.O_RDONLY)
                try: os.fsync(descriptor)
                finally: os.close(descriptor)
                _save(marker_path,{key:marker[key] for key in ("version","schema_version","account_key","store_uuid","database")}|{"state":"ready"})
            else:
                # These are failed target publications, not additional legacy
                # sources. Keep them together outside the admission source list.
                aborted=folder/(".automation-aborted-"+marker["store_uuid"])
                if aborted.exists(): raise LegacyStateBlocked()
                aborted.mkdir(mode=0o700)
                destinations=[]
                if target_is_import: destinations.append((target,aborted/"target.sqlite3"))
                if staged.exists(): destinations.append((staged,aborted/"stage.sqlite3"))
                marker_archive=aborted/"migration.json"
                for original,destination in destinations: os.replace(original,destination)
                if archive is not None and archive.exists():
                    if target.exists(): raise LegacyStateBlocked()
                    os.replace(archive,target)
                os.replace(marker_path,marker_archive)
                for directory in (aborted,folder):
                    descriptor=os.open(directory,os.O_RDONLY)
                    try: os.fsync(descriptor)
                    finally: os.close(descriptor)
            return action
    except LegacyStateBlocked: raise
    except Exception: raise StorageFailure() from None


def resolve_legacy_store(state_dir,scope,ownership,*,evidence_file):
    """Record explicitly supplied correlated evidence; never replay legacy inputs."""
    from contextlib import ExitStack
    from automation_core import contracts as c
    from automation_core.storage import persistence as codec
    from automation_core.storage.memory import AccountMemory,StorageFailure
    from automation_core.storage.locking import _lock
    memory=None
    try:
        evidence_path=Path(evidence_file)
        if evidence_path.stat().st_size>codec.MAX_BYTES: raise LegacyStateBlocked()
        evidence=codec.loads(evidence_path.read_text())
        if type(evidence) is not tuple or not evidence or any(type(item) is not c.LegacyResolutionEvidence for item in evidence):
            raise LegacyStateBlocked()
        memory=AccountMemory.open(state_dir,scope,ownership)
        records=memory.legacy_records()
        with ExitStack() as stack:
            if any(record.evidence.source_kind=="verification_journal" for record in records):
                digest=hashlib.sha256(scope.account_key.encode()).hexdigest()
                stack.enter_context(_lock(memory.path.parent/(".mailverify-"+digest+".lock")))
            revisions=tuple((wid,memory.load_checkpoint(wid).revision) for wid in sorted({r.workflow_id for r in records}))
            memory.resolve_legacy(evidence,expected_revisions=revisions)
        return len(evidence)
    except LegacyStateBlocked: raise
    except Exception as error:
        from automation_core.storage.memory import TransitionDenied
        if isinstance(error,(TransitionDenied,StorageFailure)): raise
        raise StorageFailure() from None
    finally:
        if memory is not None: memory.close()
