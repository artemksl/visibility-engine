"""Versioned transport-independent recovery contracts; no provider dependencies."""
from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import math
from types import MappingProxyType
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

SCHEMA_VERSION = 1
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}$")


def immutable(value):
    if isinstance(value, Mapping):
        return MappingProxyType({k: immutable(v) for k, v in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(immutable(v) for v in value)
    return value


def safe_id(value: str) -> str:
    if not isinstance(value, str) or not _SAFE.fullmatch(value) or ".." in value or value.startswith("/"):
        raise ValueError("invalid recovery identifier")
    return value


def json_value(value: Any) -> Any:
    projection = getattr(value, "__json_value__", None)
    if callable(projection):
        return json_value(projection())
    if dataclasses.is_dataclass(value):
        return {f.name: json_value(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(json_value(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class RecoveryError(RuntimeError):
    """A safe code is carried separately from diagnostic details."""
    code = "recovery_error"


class RecoveryIntegrityError(RecoveryError):
    code = "recovery_integrity_error"


class StaleRecoveryPreview(RecoveryError):
    code = "stale_recovery_preview"


class RecoveryBlocked(RecoveryError):
    code = "recovery_blocked"


class AllowanceExceeded(RecoveryError):
    code = "allowance_exhausted"


class EffectCertainty(str, enum.Enum):
    ADMITTED = "admitted"
    UNKNOWN = "unknown"
    SUCCEEDED = "succeeded"
    CONFIRMED_NOT_SENT = "confirmed_not_sent"
    FAILED = "failed"


@dataclass(frozen=True)
class ArtifactRef:
    reference: str
    sha256: str
    media_type: str = "application/json"
    schema_version: str = "1"
    producing_attempt_id: str = ""
    captured_at: str | None = None
    retrieved_at: str | None = None


@dataclass(frozen=True)
class OperationReference:
    adapter_id: str
    adapter_version: str
    input_digest: str
    resource_scope: str
    reference: Mapping[str, Any]
    discovered_at: str
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "reference", immutable(self.reference))


@dataclass(frozen=True)
class OperationSpec:
    operation_id: str
    item_id: str
    adapter_id: str
    adapter_version: str
    input_digest: str
    dependencies: tuple[str, ...] = ()
    resource_scopes: tuple[str, ...] = ()
    max_attempts: int = 2
    max_effects: int = 4
    deadline_seconds: float = 270.0
    restart_unit: str = ""
    parser_version: str = ""
    validator_version: str = "1"
    cost_limits: tuple[tuple[str, float], ...] = ()
    local_only: bool = False
    reconciliation_effects: tuple[str, ...] = ("retrieve_existing", "status_poll", "download", "poll", "fetch")

    def __post_init__(self):
        for field_name in ("dependencies", "resource_scopes", "reconciliation_effects"):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))
        object.__setattr__(self, "cost_limits", tuple(tuple(pair) for pair in self.cost_limits))
        if not re.fullmatch(r"[a-f0-9]{64}", self.input_digest):
            raise ValueError("operation input digest must be SHA-256")
        for value in (self.operation_id, self.item_id, self.adapter_id, self.adapter_version, self.validator_version, *self.dependencies, *self.resource_scopes, *self.reconciliation_effects):
            safe_id(value)
        if self.parser_version:
            safe_id(self.parser_version)
        if (type(self.max_attempts) is not int or type(self.max_effects) is not int or self.max_attempts < 1 or self.max_effects < 0
            or type(self.deadline_seconds) not in (int, float) or not math.isfinite(self.deadline_seconds) or self.deadline_seconds <= 0):
            raise ValueError("operation allowances must be finite and nonnegative")
        if (len(dict(self.cost_limits)) != len(self.cost_limits)
            or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for _, v in self.cost_limits)):
            raise ValueError("invalid cost allowance")


@dataclass(frozen=True)
class WorkItem:
    item_id: str
    operation_ids: tuple[str, ...]
    output_contract: str = "1"

    def __post_init__(self):
        safe_id(self.item_id)
        object.__setattr__(self, "operation_ids", tuple(self.operation_ids))


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    scope_digest: str
    workflow_version: str
    policy_version: str
    items: tuple[WorkItem, ...]
    operations: tuple[OperationSpec, ...]
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        safe_id(self.run_id)
        safe_id(self.workflow_version)
        safe_id(self.policy_version)
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "operations", tuple(self.operations))
        if not re.fullmatch(r"[a-f0-9]{64}", self.scope_digest):
            raise ValueError("scope digest must be SHA-256")
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported recovery manifest schema")
        items = {i.item_id for i in self.items}
        operations = {o.operation_id: o for o in self.operations}
        if len(items) != len(self.items) or len(operations) != len(self.operations):
            raise ValueError("duplicate work or operation identity")
        visited: set[str] = set()
        for op in self.operations:
            if op.item_id not in items or any(d not in visited for d in op.dependencies):
                raise ValueError("operations must be in frozen dependency order")
            visited.add(op.operation_id)
        declared = [op for item in self.items for op in item.operation_ids]
        if len(declared) != len(set(declared)) or set(declared) != set(operations):
            raise ValueError("every operation must belong to exactly one work item")
        for item in self.items:
            if any(op not in operations or operations[op].item_id != item.item_id for op in item.operation_ids):
                raise ValueError("work operation binding mismatch")


@dataclass(frozen=True)
class ItemState:
    item_id: str
    status: str = "never_started"
    reason_code: str = ""
    unresolved_attempt_ids: tuple[str, ...] = ()
    checked_attempt_ids: tuple[str, ...] = ()
    reconstructable: bool = False
    saved_input_available: bool = False
    evidence_links: tuple[str, ...] = ()
    operation_states: tuple[tuple[str, str], ...] = ()
    historical_unresolved_attempt_ids: tuple[str, ...] = ()

    def __post_init__(self):
        safe_id(self.item_id)
        for field_name in ("unresolved_attempt_ids", "checked_attempt_ids", "evidence_links", "operation_states", "historical_unresolved_attempt_ids"):
            object.__setattr__(self, field_name, immutable(getattr(self, field_name)))
        if self.reason_code:
            safe_id(self.reason_code)
        for value in (*self.unresolved_attempt_ids, *self.checked_attempt_ids, *self.historical_unresolved_attempt_ids):
            safe_id(value)
        if self.status not in {"never_started", "failed", "active", "blocked", "succeeded", "cancelled"}:
            raise ValueError("invalid item state")


@dataclass(frozen=True)
class WorkerObservation:
    status: str = "unknown"
    sampled_at: str | None = None
    execution_id: str | None = None
    reason_code: str = ""
    heartbeat_at: str | None = None
    progress_at: str | None = None
    deadline_at: str | None = None
    ownership_status: str = "unknown"


@dataclass(frozen=True)
class RecoverySnapshot:
    manifest: RunManifest
    revision_digest: str
    items: tuple[ItemState, ...]
    events: tuple[Mapping[str, Any], ...] = ()
    worker: WorkerObservation | None = None
    resources: tuple[tuple[str, str], ...] = ()
    sampled_at: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "events", immutable(self.events))
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "resources", immutable(self.resources))
        if not re.fullmatch(r"[a-f0-9]{64}", self.revision_digest):
            raise ValueError("revision digest must be SHA-256")
        if tuple(i.item_id for i in self.items) != tuple(i.item_id for i in self.manifest.items):
            raise ValueError("snapshot must include every item in frozen order")


@dataclass(frozen=True)
class RecoverySelection:
    item_id: str
    action: str
    reason_code: str = ""
    replacement_attempt_ids: tuple[str, ...] = ()
    operation_ids: tuple[str, ...] = ()
    max_attempts: int = 0
    max_effects: int = 0

    def __post_init__(self):
        safe_id(self.item_id)
        if self.action not in {"retain", "wait", "blocked", "reconstruct", "reconcile", "reconcile_or_replace", "parse_saved", "execute"}:
            raise ValueError("invalid recovery action")
        if type(self.max_attempts) is not int or type(self.max_effects) is not int or min(self.max_attempts, self.max_effects) < 0:
            raise ValueError("invalid selection allowance")
        object.__setattr__(self, "replacement_attempt_ids", tuple(self.replacement_attempt_ids))
        object.__setattr__(self, "operation_ids", tuple(self.operation_ids))
        if self.reason_code:
            safe_id(self.reason_code)
        for value in (*self.operation_ids, *self.replacement_attempt_ids):
            safe_id(value)


@dataclass(frozen=True)
class ParserRepair:
    operation_id: str
    from_adapter_version: str
    to_adapter_version: str
    input_digest: str
    parser_version: str
    compatibility_id: str

    def __post_init__(self):
        for value in (self.operation_id, self.from_adapter_version, self.to_adapter_version, self.parser_version, self.compatibility_id):
            safe_id(value)
        if not re.fullmatch(r"[a-f0-9]{64}", self.input_digest):
            raise ValueError("parser input digest must be SHA-256")


@dataclass(frozen=True)
class RecoveryPreview:
    run_id: str
    scope_digest: str
    revision_digest: str
    manifest_digest: str
    selections: tuple[RecoverySelection, ...]
    preview_digest: str
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported recovery preview schema")
        object.__setattr__(self, "selections", tuple(self.selections))

    @property
    def selected_item_ids(self) -> tuple[str, ...]:
        return tuple(s.item_id for s in self.selections)

    def to_dict(self) -> dict[str, Any]:
        return json_value(self)


@dataclass(frozen=True)
class RecoveryRequest:
    request_id: str
    run_id: str
    scope_digest: str
    revision_digest: str
    manifest_digest: str
    selections: tuple[RecoverySelection, ...]
    preview_digest: str
    parser_repairs: tuple[ParserRepair, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported recovery request schema")
        safe_id(self.request_id)
        safe_id(self.run_id)
        object.__setattr__(self, "selections", tuple(self.selections))
        object.__setattr__(self, "parser_repairs", tuple(self.parser_repairs))
        if len({repair.operation_id for repair in self.parser_repairs}) != len(self.parser_repairs):
            raise ValueError("duplicate parser repair")

    @classmethod
    def from_preview(cls, preview: RecoveryPreview, request_id: str, *, parser_repairs=()):
        safe_id(request_id)
        return cls(request_id=request_id, parser_repairs=tuple(parser_repairs), **{k: v for k, v in preview.__dict__.items()})

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]):
        payload = dict(value)
        payload["selections"] = tuple(RecoverySelection(**{**v, "replacement_attempt_ids": tuple(v.get("replacement_attempt_ids", ())), "operation_ids": tuple(v.get("operation_ids", ()))}) for v in payload.get("selections", ()))
        payload["parser_repairs"] = tuple(ParserRepair(**v) for v in payload.get("parser_repairs", ()))
        return cls(**payload)

    def to_dict(self) -> dict[str, Any]:
        return json_value(self)


@dataclass(frozen=True)
class RecoveryPass:
    pass_id: str
    request_id: str
    execution_id: str
    request_digest: str
    selections: tuple[RecoverySelection, ...]
    duplicate: bool = False

    @property
    def selected_item_ids(self):
        return tuple(s.item_id for s in self.selections)

    def to_dict(self):
        return json_value(self)


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    operation_id: str
    item_id: str
    pass_id: str
    execution_id: str
    lifetime_ordinal: int
    pass_ordinal: int
    action: str
    predecessor_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EffectRecord:
    effect_id: str
    attempt_id: str
    operation_id: str
    pass_id: str
    execution_id: str
    action: str
    input_digest: str
    cost: tuple[tuple[str, float], ...] = ()
    idempotency_key: str | None = None
    replaces_attempt_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class StepObservation:
    status: str
    reason_code: str = ""
    output: Any = None
    artifact_refs: tuple[ArtifactRef, ...] = ()
    checked_attempt_ids: tuple[str, ...] = ()
    conditional_not_required: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "output", immutable(self.output))
        object.__setattr__(self, "artifact_refs", tuple(self.artifact_refs))
        object.__setattr__(self, "checked_attempt_ids", tuple(self.checked_attempt_ids))
        object.__setattr__(self, "conditional_not_required", tuple(self.conditional_not_required))
        if self.status not in {"completed", "failed", "active", "inconclusive", "unsupported", "blocked", "not_required"}:
            raise ValueError("invalid adapter observation")
        if self.reason_code:
            safe_id(self.reason_code)
