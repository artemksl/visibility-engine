"""Append-only, checksummed recovery journal and committed result history.

Every record is one JSON file ``recovery/events/<sequence>.json`` whose
checksum binds it to its predecessor. ``recovery/head.json`` records a durable
lower bound. The run-root ``recovery-journal.json`` initialization binding requires
that established head to remain present, even if the whole recovery directory is
removed. It is published once, after the first complete event and head. Records that this instance has already verified are kept in
memory together with the name, inode, size, modification and change time of their
files. Bounded native inode notifications avoid repeated prefix stats on macOS;
unsupported hosts or uncertain notifications conservatively scan all metadata.
A cached-file change forces checksum verification from the first file. Every
read still validates both durable bounds. Inside one transaction
(the exclusive ``.journal.lock`` is held) the directory is read once and every
own append extends the verified view directly.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional

from .._file_changes import ChangeTrackingUnavailable, FileChanges, WatchCapacityReached
from .contracts import RecoveryIntegrityError, digest, json_value, safe_id
from .durable import publish_json


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(freeze(v) for v in value)
    return value


_REQUIRED = {
    "manifest": {"manifest", "manifest_digest", "baseline_revision_digest"},
    "pass_authorized": {"pass_id", "execution_id", "request", "request_digest"},
    "pass_resumed": {"pass_id", "execution_id"},
    "pass_completed": {"pass_id"}, "pass_cancelled": {"pass_id"},
    "attempt_started": {"attempt_id", "operation_id", "item_id", "pass_id", "execution_id", "lifetime_ordinal", "pass_ordinal", "action"},
    "attempt_resumed": {"attempt_id", "operation_id", "item_id", "pass_id", "execution_id"},
    "attempt_finished": {"attempt_id", "operation_id", "item_id", "pass_id", "status"},
    "effect_admitted": {"effect_id", "attempt_id", "operation_id", "pass_id", "execution_id", "action", "input_digest", "cost"},
    "effect_observed": {"effect_id", "attempt_id", "operation_id", "pass_id", "certainty"},
    "operation_reference": {"attempt_id", "pass_id", "operation_id", "reference"},
    "failure": {"pass_id", "item_id", "reason_code", "checked_attempt_ids"},
    "revision_prepared": {"revision_id", "item_id", "pass_id", "parent_revision_id", "output", "output_digest"},
    "revision_committed": {"revision_id", "item_id", "pass_id", "parent_revision_id", "output", "output_digest"},
    "resource_observed": {"resource_id", "status"},
}


def validate_record(kind, payload):
    safe_id(kind)
    if not isinstance(payload, Mapping) or not _REQUIRED.get(kind, set()).issubset(payload):
        raise ValueError("incomplete recovery record")
    for field_name in ("attempt_id", "operation_id", "item_id", "pass_id", "execution_id", "effect_id", "revision_id", "resource_id"):
        if payload.get(field_name) is not None:
            safe_id(payload[field_name])
    if kind == "manifest" and digest(payload["manifest"]) != payload["manifest_digest"]:
        raise ValueError("manifest digest mismatch")
    if kind == "pass_authorized" and digest(payload["request"]) != payload["request_digest"]:
        raise ValueError("request digest mismatch")
    if kind == "effect_observed" and payload["certainty"] not in {"admitted", "unknown", "succeeded", "confirmed_not_sent", "failed"}:
        raise ValueError("invalid effect certainty")


def record_name(number: int) -> str:
    return f"{number:012d}.json"


_FileVersion = tuple[int, int, int, int, int]


def _file_version(metadata: os.stat_result) -> _FileVersion:
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


class _EventIndex:
    """Lookups over the normalized event order (baseline events, then journal).

    ``select(kind, field, value)`` answers "all events of this kind whose
    payload field equals value" in normalized order. Per-field tables are
    built on first use for a (kind, field) pair and extended on every append,
    so the coordinator never scans the whole history for one identifier.
    """

    def __init__(self, events: Iterable[Mapping[str, Any]] = ()):
        self.rebuild(events)

    def rebuild(self, events: Iterable[Mapping[str, Any]]) -> None:
        self.events: tuple[Mapping[str, Any], ...] = tuple(events)
        self.by_kind: dict[str, list[Mapping[str, Any]]] = {}
        self.by_field: dict[tuple[str, str], dict[Any, list[Mapping[str, Any]]]] = {}
        self.position: dict[int, int] = {}
        for pos, event in enumerate(self.events):
            self._add(event, pos)

    def _add(self, event: Mapping[str, Any], pos: int) -> None:
        self.position[id(event)] = pos
        kind = event["kind"]
        self.by_kind.setdefault(kind, []).append(event)
        for (indexed_kind, field), table in self.by_field.items():
            if indexed_kind == kind:
                self._file(table, event, field)

    @staticmethod
    def _file(table: dict[Any, list[Mapping[str, Any]]], event: Mapping[str, Any], field: str) -> None:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            return
        value = payload.get(field)
        try:
            hash(value)
        except TypeError:
            return
        if value is not None:
            table.setdefault(value, []).append(event)

    def extend(self, event: Mapping[str, Any]) -> None:
        self.events = self.events + (event,)
        self._add(event, len(self.events) - 1)

    def of_kind(self, kind: str) -> tuple[Mapping[str, Any], ...]:
        return tuple(self.by_kind.get(kind, ()))

    def select(self, kind: str, field: str, value: Any) -> tuple[Mapping[str, Any], ...]:
        table = self.by_field.get((kind, field))
        if table is None:
            table = {}
            for event in self.by_kind.get(kind, ()):
                self._file(table, event, field)
            self.by_field[(kind, field)] = table
        return tuple(table.get(value, ()))

    def merged(self, kinds: Iterable[str], field: Optional[str] = None, value: Any = None) -> tuple[Mapping[str, Any], ...]:
        """Events of several kinds in normalized order, optionally filtered by one field."""
        selected: list[Mapping[str, Any]] = []
        for kind in kinds:
            selected.extend(self.of_kind(kind) if field is None else self.select(kind, field, value))
        selected.sort(key=lambda event: self.position[id(event)])
        return tuple(selected)


class _RevisionIndex:
    """Projection of an immutable, verified event prefix, in journal order."""

    def __init__(self):
        self.processed = 0
        self.prepared: dict[str, Mapping[str, Any]] = {}
        self.effective: dict[str, Mapping[str, Any]] = {}
        self.current_ids: dict[str, str] = {}

    def extend(self, events):
        for event in events:
            payload = event["payload"]
            if event["kind"] == "revision_prepared":
                revision_id = payload["revision_id"]
                if revision_id in self.prepared or digest(payload["output"]) != payload["output_digest"]:
                    raise RecoveryIntegrityError("result_revision_invalid")
                self.prepared[revision_id] = payload
            elif event["kind"] == "revision_committed":
                revision = self.prepared.get(payload["revision_id"])
                if revision is None or payload["output_digest"] != revision["output_digest"]:
                    raise RecoveryIntegrityError("result_commit_invalid")
                item_id = revision["item_id"]
                prior = self.current_ids.get(item_id)
                if revision.get("parent_revision_id") != prior:
                    raise RecoveryIntegrityError("result_revision_chain_invalid")
                if revision["revision_id"] == prior:
                    raise RecoveryIntegrityError("duplicate_result_commit")
                self.current_ids[item_id] = revision["revision_id"]
                self.effective[item_id] = revision
            self.processed += 1


class RecoveryLedger:
    def __init__(self, run_dir: Path | str, *, watch_changes: bool = True):
        self.run_dir = Path(run_dir)
        self.root = self.run_dir / "recovery"
        self._guard = threading.RLock()
        self._depth = 0
        # Verified journal records (journal order) and their file identities.
        self._cache_records: tuple[Mapping[str, Any], ...] = ()
        self._cache_names: tuple[str, ...] = ()
        self._cache_versions: tuple[_FileVersion, ...] = ()
        self._cache_name_set: set[str] = set()
        # Quota-limited files retain metadata checks while healthy prefix
        # watches remain active. None means this new file is not verified yet.
        self._unwatched_versions: dict[str, _FileVersion | None] = {}
        self._unwatched_paths: dict[str, Path] = {}
        self._watch_changes = watch_changes
        self._changes: FileChanges | None = None
        # Lookups over baseline + journal records; rebuilt with the cache.
        self._index = _EventIndex()
        self._revision_index = _RevisionIndex()
        # True while the lock is held and the cache already matches the disk.
        self._transaction_fresh = False
        self._manifest_count = 0
        self._baseline_events: tuple[Mapping[str, Any], ...] = ()

    def close(self) -> None:
        """Release bounded native descriptors; subsequent reads verify by stat."""
        self._watch_changes = False
        if self._changes is not None:
            self._changes.close()
            self._changes = None

    def __del__(self):
        changes = getattr(self, "_changes", None)
        if changes is not None:
            changes.close()

    def _changed_paths(self, directory):
        if not self._watch_changes:
            return None
        try:
            if self._changes is None:
                # A read-only inspector must not create a journal directory.
                if not directory.exists():
                    return None
                self._changes = FileChanges(directory)
            return self._changes.changed_paths()
        except (OSError, ValueError, ChangeTrackingUnavailable):
            self.close()
            return None

    def _watch(self, path):
        if self._changes is not None:
            try:
                watched = self._changes.watch(path)
                self._unwatched_versions.pop(path.name, None)
                self._unwatched_paths.pop(path.name, None)
                return watched
            except WatchCapacityReached:
                # Capacity is known and bounded, not a lost notification. Keep
                # every existing watch and verify this file by metadata instead.
                self._unwatched_versions.setdefault(path.name, None)
                self._unwatched_paths.setdefault(path.name, path)
                self._changes.remember_unwatched(path)
            except (OSError, ValueError, ChangeTrackingUnavailable):
                self.close()
        return None

    def _remember_version(self, name, version):
        if name in self._unwatched_versions:
            self._unwatched_versions[name] = version

    # ------------------------------------------------------------------ reads
    @staticmethod
    def _read_bound(path):
        head = None
        try:
            head = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        if (not isinstance(head, dict)
            or type(head.get("sequence")) is not int or head["sequence"] < 1
            or not isinstance(head.get("checksum"), str) or len(head["checksum"]) != 64):
            raise ValueError("invalid journal head")
        return head

    def _read_head(self):
        return self._read_bound(self.root / "head.json")

    def _read_binding(self):
        try:
            binding = self._read_bound(self.run_dir / "recovery-journal.json")
            if binding is not None and (type(binding.get("schema_version")) is not int or binding["schema_version"] != 1):
                raise ValueError("invalid journal binding")
            return binding
        except (OSError, ValueError, TypeError):
            raise RecoveryIntegrityError("recovery_journal_binding_unavailable") from None

    def _legacy_pass_bound(self):
        """Recognize pre-anchor runs without mutating their historical evidence.

        A pass is bound only after its authorization journal record and head
        exist. Thus its absence is legitimate before initial publication; its
        presence plus a missing head is loss of established recovery evidence.
        Older releases kept this binding in the owner/control records and their
        histories, outside the recovery directory.
        """
        try:
            root = self.run_dir.resolve()
            owner_path = self.run_dir / "execution-owner.json"
            paths = [owner_path, self.run_dir / "execution-control.json"]
            for directory in ("execution-history", "execution-control-history"):
                directory_path = self.run_dir / directory
                if not directory_path.resolve().is_relative_to(root):
                    raise RecoveryIntegrityError("recovery_journal_path_escapes_run")
                paths.extend(sorted(directory_path.glob("*.json")))
            for path in paths:
                if not path.resolve().is_relative_to(root):
                    raise RecoveryIntegrityError("recovery_journal_path_escapes_run")
                try:
                    value = json.loads(path.read_text())
                    if path == owner_path:
                        # Preserve ownership's established diagnostic and repair
                        # path instead of masking it as a journal failure.
                        from ..execution.run_ownership import _validate_owner
                        _validate_owner(value)
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, TypeError):
                    if path == owner_path:
                        raise RecoveryIntegrityError("execution_owner_corrupt") from None
                    raise
                if not isinstance(value, dict):
                    raise ValueError("invalid execution binding")
                bindings = [value]
                if path.name.endswith(".operator-release.json"):
                    # Repair can replace a malformed owner with pass_id=None.
                    # Its lossless archive still proves prior initialization
                    # when the old pass ID is readable, even if status was not.
                    raw = base64.b64decode(value["owner_record_base64"], validate=True)
                    if hashlib.sha256(raw).hexdigest() != value.get("owner_record_sha256"):
                        raise ValueError("archived owner binding changed")
                    try:
                        archived = json.loads(raw)
                    except (ValueError, UnicodeError):
                        archived = None
                    if isinstance(archived, dict):
                        bindings.append(archived)
                for binding in bindings:
                    if binding.get("pass_id"):
                        safe_id(binding["pass_id"])
                        return True
            return False
        except (OSError, ValueError, TypeError, KeyError):
            raise RecoveryIntegrityError("recovery_journal_binding_unavailable") from None

    @staticmethod
    def _verify_bounds(records, bounds):
        for bound in bounds:
            if (bound["sequence"] > len(records)
                or records[bound["sequence"] - 1]["checksum"] != bound["checksum"]):
                raise RecoveryIntegrityError("recovery_journal_history_truncated")

    def read(self) -> tuple[Mapping[str, Any], ...]:
        """Return every verified journal record in sequence order."""
        with self._guard:
            if self._depth and self._transaction_fresh:
                # Only the holder of the exclusive journal lock can append, and
                # every own append already extended the verified view.
                return self._cache_records
            try:
                records = self._read_incremental()
            except BaseException:
                # Never reuse a notification-only view after partial verification.
                self.close()
                self._revision_index = _RevisionIndex()
                raise
            if self._depth:
                self._transaction_fresh = True
            return records

    def _read_incremental(self) -> tuple[Mapping[str, Any], ...]:
        directory = self.root / "events"
        cached_count = len(self._cache_records)
        # The durable head is sampled before notifications/listing. A clean
        # watched prefix needs no per-file stat; every inode was registered
        # before reading its bytes. Unsupported or uncertain notification state
        # takes the complete metadata path below, including ctime checks.
        head = None
        try:
            head = self._read_head()
            binding = self._read_binding()
            if head is None and (binding is not None or self._legacy_pass_bound()):
                # Initial publication may complete between the head sample and
                # the independent binding sample. Confirm absence once before
                # reporting loss; this never rewrites or recreates the head.
                head = self._read_head()
                if head is None:
                    raise RecoveryIntegrityError("recovery_journal_anchor_missing")
            bounds = tuple(value for value in (head, binding) if value is not None)
            changed = self._changed_paths(directory)
            dirty_unwatched = False
            if changed is not None:
                # A bounded watcher cannot cover every historical inode. Check
                # every verified unwatched file on every independent read,
                # including ctime so restoring mtime cannot hide an edit.
                for name, version in self._unwatched_versions.items():
                    if version is not None and _file_version(self._unwatched_paths[name].stat()) != version:
                        dirty_unwatched = True
                        break
                # Directory notifications compare names with watched names, so
                # they also report unchanged unwatched files. Their mandatory
                # metadata checks above supply the missing validation. New,
                # unverified names must still enter sequence/hash verification.
                changed = {path for path in changed
                           if self._unwatched_versions.get(path.name) is None}
            dirty_prefix = dirty_unwatched or (changed is not None and any(
                path.name in self._cache_name_set for path in changed))
            if dirty_prefix:
                # A vnode event is evidence to re-verify even when a writer has
                # restored size/mtime. Never merely refresh its cached version.
                self._reset_cache()
                cached_count = 0
            full_scan = changed is None or dirty_prefix
            if not full_scan:
                candidates = sorted(path.name for path in changed if path.name.endswith(".json"))
                # A head newer than a quiet notification sample is insufficient
                # proof of truncation; conservatively enumerate and verify.
                if any(bound["sequence"] > cached_count + len(candidates) for bound in bounds):
                    full_scan = True
            if full_scan:
                try:
                    with os.scandir(directory) as entries:
                        listed = sorted((entry.name, entry) for entry in entries if entry.name.endswith(".json"))
                except FileNotFoundError:
                    if bounds:
                        raise ValueError("journal history missing")
                    listed = []
                names, versions = [], []
                for name, entry in listed:
                    watched = self._watch(directory / name)
                    metadata = entry.stat()
                    if watched is not None and watched != (metadata.st_dev, metadata.st_ino):
                        # A replacement raced registration. Full metadata reads
                        # remain authoritative; no partially watched cache.
                        self.close()
                    names.append(name)
                    versions.append(_file_version(metadata))
                if cached_count and (tuple(names[:cached_count]) != self._cache_names
                                     or tuple(versions[:cached_count]) != self._cache_versions):
                    self._reset_cache()
                    cached_count = 0
                candidates = names[cached_count:]
        except (OSError, ValueError, TypeError):
            raise RecoveryIntegrityError("recovery_journal_unavailable") from None
        if not candidates:
            self._verify_bounds(self._cache_records, bounds)
            return self._cache_records
        records = list(self._cache_records)
        previous = records[-1]["checksum"] if records else ""
        added: list[Mapping[str, Any]] = []
        new_versions: list[_FileVersion] = []
        for offset, name in enumerate(candidates):
            number = cached_count + offset + 1
            path = directory / name
            try:
                if name != record_name(number):
                    raise ValueError("noncontiguous journal")
                watched = self._watch(path)
                metadata = path.stat()
                if watched is not None and watched != (metadata.st_dev, metadata.st_ino):
                    self.close()
                value = json.loads(path.read_text())
                if not isinstance(value, dict):
                    raise ValueError("journal record object required")
                checksum = value.pop("checksum")
                if (type(value["schema_version"]) is not int or value["schema_version"] != 1
                    or type(value["sequence"]) is not int or value["sequence"] != number or value["previous"] != previous):
                    raise ValueError("journal binding")
                if not isinstance(value["payload"], dict) or digest(value) != checksum:
                    raise ValueError("journal checksum")
                validate_record(value["kind"], value["payload"])
                value["checksum"] = checksum
                frozen = freeze(value)
                records.append(frozen)
                added.append(frozen)
                new_versions.append(_file_version(metadata))
                self._remember_version(name, new_versions[-1])
                previous = checksum
            except (OSError, ValueError, TypeError, KeyError):
                raise RecoveryIntegrityError("recovery_journal_corrupt") from None
        self._verify_bounds(records, bounds)
        self._cache_records = tuple(records)
        self._cache_names = self._cache_names[:cached_count] + tuple(candidates)
        self._cache_name_set.update(candidates)
        self._cache_versions = self._cache_versions[:cached_count] + tuple(new_versions)
        self._index_added(added, rebuilt=cached_count == 0)
        return self._cache_records

    def _reset_cache(self) -> None:
        self._cache_records = ()
        self._cache_names = ()
        self._cache_versions = ()
        self._cache_name_set.clear()
        self._unwatched_versions.clear()
        self._unwatched_paths.clear()
        self._index = _EventIndex()
        self._revision_index = _RevisionIndex()
        self._manifest_count = 0
        self._baseline_events = ()

    def _baseline(self) -> tuple[Mapping[str, Any], ...]:
        if self._manifest_count > 1:
            raise RecoveryIntegrityError("manifest_duplicated")
        return self._baseline_events

    def _index_added(self, added: Iterable[Mapping[str, Any]], *, rebuilt: bool) -> None:
        added = tuple(added)
        if rebuilt or any(event["kind"] == "manifest" for event in added):
            # Baseline events live inside the manifest record and precede
            # every journal record in the normalized order.
            manifests = [e for e in self._cache_records if e["kind"] == "manifest"]
            self._manifest_count = len(manifests)
            self._baseline_events = (tuple(manifests[0]["payload"].get("baseline_events", ()))
                                     if len(manifests) == 1 else ())
            self._index = _EventIndex(self._baseline_events + self._cache_records)
            self._revision_index = _RevisionIndex()
            return
        for event in added:
            self._index.extend(event)

    def normalized_events(self):
        """Baseline events imported with the manifest, then every journal record."""
        self.read()
        self._baseline()  # raises on a duplicated manifest, as before
        return self._index.events

    def events(self, kind: Optional[str] = None) -> tuple[Mapping[str, Any], ...]:
        """Normalized events, optionally only those of one kind."""
        self.read()
        self._baseline()
        return self._index.events if kind is None else self._index.of_kind(kind)

    def select(self, kind: str, **field_value: Any) -> tuple[Mapping[str, Any], ...]:
        """Normalized events of ``kind`` whose payload field equals the value."""
        if len(field_value) != 1:
            raise ValueError("select takes exactly one payload field")
        (field, value), = field_value.items()
        self.read()
        self._baseline()
        return self._index.select(kind, field, value)

    def select_many(self, kinds: Iterable[str], **field_value: Any) -> tuple[Mapping[str, Any], ...]:
        """Normalized events of several kinds, in normalized order, optionally filtered."""
        if len(field_value) > 1:
            raise ValueError("select_many takes at most one payload field")
        self.read()
        self._baseline()
        if field_value:
            (field, value), = field_value.items()
            return self._index.merged(tuple(kinds), field, value)
        return self._index.merged(tuple(kinds))

    @property
    def watermark(self) -> str:
        records = self.read()
        return str(records[-1]["checksum"]) if records else ""

    # ----------------------------------------------------------------- writes
    @contextmanager
    def transaction(self, owner):
        owner.assert_owned()
        with self._guard:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self.root.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.root / ".journal.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._depth = 1
                self._transaction_fresh = False
                owner.assert_owned()
                yield
            finally:
                self._depth = 0
                self._transaction_fresh = False
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def append(self, kind: str, payload: Mapping[str, Any], owner) -> Mapping[str, Any]:
        with self.transaction(owner):
            validate_record(kind, payload)
            records = self.read()
            value = {"schema_version": 1, "sequence": len(records) + 1,
                     "previous": records[-1]["checksum"] if records else "", "kind": kind,
                     "execution_id": owner.execution_id, "recorded_at": now(), "payload": json_value(payload)}
            value["checksum"] = digest(value)
            owner.assert_owned()
            name = record_name(len(records) + 1)
            path = self.root / "events" / name
            publish_json(path, value, exclusive=True)
            # The head follows its fully synced immutable event. A stale head
            # is safe after interruption and never hides the verified suffix.
            bound = {"sequence": value["sequence"], "checksum": value["checksum"]}
            publish_json(self.root / "head.json", bound, full_sync=False)
            # Bind initialization outside recovery/ only after both files exist.
            # This adds one device flush per run, not one per event. Existing
            # pre-binding journals acquire it only during an authorized write.
            binding_path = self.run_dir / "recovery-journal.json"
            if not binding_path.exists():
                publish_json(binding_path, {"schema_version": 1, **bound}, exclusive=True)
            # Publication precedes watch registration. Verify this one file
            # after registering so an intervening write cannot become a clean,
            # trusted cache entry merely by supplying its new metadata.
            try:
                watched = self._watch(path)
                metadata = path.stat()
                if watched is not None and watched != (metadata.st_dev, metadata.st_ino):
                    self.close()
                published = json.loads(path.read_text())
                if published != value or digest({key: item for key, item in published.items()
                                                if key != "checksum"}) != value["checksum"]:
                    raise ValueError("published journal record changed")
            except (OSError, ValueError, TypeError, KeyError):
                self.close()
                self._reset_cache()
                self._transaction_fresh = False
                raise RecoveryIntegrityError("recovery_journal_corrupt") from None
            frozen = freeze(value)
            # Extend the verified view with exactly the file published above.
            self._cache_records = records + (frozen,)
            self._cache_names = self._cache_names + (name,)
            self._cache_name_set.add(name)
            self._cache_versions = self._cache_versions + (_file_version(metadata),)
            self._remember_version(name, self._cache_versions[-1])
            self._index_added((frozen,), rebuilt=False)
            return frozen

    # ---------------------------------------------------------------- results
    def effective_results(self, records=None) -> dict[str, Mapping[str, Any]]:
        """Only fully committed linked revisions enter the mutable current view."""
        if records is not None:
            # Caller-owned events may be mutable and are not the verified disk
            # prefix. Validate them independently on every call.
            revision_index = _RevisionIndex()
            revision_index.extend(records)
            return dict(revision_index.effective)
        with self._guard:
            # Freshness and durable bounds remain mandatory even when no new
            # results were committed. Cache invalidation replaces both indexes.
            self.read()
            try:
                self._revision_index.extend(self._index.events[self._revision_index.processed:])
            except BaseException:
                # A partial projection must never hide the event that failed.
                self._revision_index = _RevisionIndex()
                raise
            return dict(self._revision_index.effective)

    def incomplete_revisions(self) -> tuple[Mapping[str, Any], ...]:
        self.read()
        committed = {e["payload"]["revision_id"] for e in self._index.of_kind("revision_committed")}
        return tuple(e["payload"] for e in self._index.of_kind("revision_prepared") if e["payload"]["revision_id"] not in committed)
