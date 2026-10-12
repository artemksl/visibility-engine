"""Pure safe projection; never performs I/O or interprets adapter content."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .contracts import RecoverySnapshot, json_value
from .coordinator import plan_recovery


def _code(value: Any) -> str:
    from .contracts import safe_id
    try:
        return safe_id(str(value))
    except ValueError:
        return "unrecognized_reason"


def _link(value: Any) -> str | None:
    # Adapter routes are opaque relative evidence references, never arbitrary remote URLs.
    value = str(value)
    if len(value) > 500 or ".." in value or any(c in value for c in ("?", "#", "\\", "\n", "\r")):
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or value.startswith("/"):
        return None
    return value if value.startswith(("evidence/", "artifacts/", "conversations/")) else None



@dataclass(frozen=True)
class FailureReadout:
    schema_version: int
    run_id: str
    scope_digest: str
    revision_digest: str
    workflow_version: str
    policy_version: str
    implementations: tuple[dict[str, Any], ...]
    sampled_at: str | None
    worker: dict[str, Any]
    resources: tuple[dict[str, str], ...]
    items: tuple[dict[str, Any], ...]
    history: tuple[dict[str, Any], ...]
    accounting: dict[str, Any]
    preview: dict[str, Any]

    def to_dict(self):
        return json_value(self)


def build_failure_readout(snapshot: RecoverySnapshot) -> FailureReadout:
    preview = plan_recovery(snapshot)
    selections = {s.item_id: s for s in preview.selections}
    effects = {}
    passes = {}
    attempts = []
    history = []
    operations = {op.operation_id: op for op in snapshot.manifest.operations}
    for event in snapshot.events:
        kind, payload = event.get("kind"), event.get("payload", {})
        if kind == "pass_authorized":
            pass_id = _code(payload.get("pass_id"))
            authorized_selections = payload.get("request", {}).get("selections", ())
            authorized_cost = {}
            authorized_operations = {op for selected in authorized_selections for op in selected.get("operation_ids", ())}
            if payload.get("operation_limits"):
                authorized_operations &= set(payload["operation_limits"])
            for operation_id in authorized_operations:
                for dimension, amount in operations[operation_id].cost_limits:
                    authorized_cost[dimension] = authorized_cost.get(dimension, 0) + amount
            passes[pass_id] = {"pass_id": pass_id, "status": "authorized", "attempts": 0, "effects": 0,
                               "authorized_cost": authorized_cost,
                               "authorized_attempts": sum(s.get("max_attempts", 0) for s in authorized_selections),
                               "authorized_effects": sum(s.get("max_effects", 0) for s in authorized_selections),
                               "reserved_cost": {}, "actual_cost": {}, "unknown_cost": {}}
            if payload.get("operation_limits"):
                passes[pass_id]["authorized_attempts"] = sum(v["max_attempts"] for v in payload["operation_limits"].values())
                passes[pass_id]["authorized_effects"] = sum(v["max_effects"] for v in payload["operation_limits"].values())
            history.append({"kind": kind, "pass_id": pass_id, "recorded_at": event.get("recorded_at")})
        elif kind in {"pass_completed", "pass_cancelled", "pass_resumed"}:
            pass_id = _code(payload.get("pass_id"))
            if pass_id in passes:
                passes[pass_id]["status"] = kind.removeprefix("pass_")
            history.append({"kind": kind, "pass_id": pass_id, "recorded_at": event.get("recorded_at")})
        elif kind == "attempt_started":
            safe = {key: _code(payload[key]) for key in ("attempt_id", "operation_id", "item_id", "pass_id", "action") if payload.get(key)}
            safe.update(kind=kind, recorded_at=event.get("recorded_at"), lifetime_ordinal=payload.get("lifetime_ordinal"), pass_ordinal=payload.get("pass_ordinal"))
            history.append(safe)
            attempts.append(safe)
        elif kind == "effect_admitted":
            effects[payload["effect_id"]] = {"pass_id": payload["pass_id"], "certainty": "admitted",
                                            "cost": dict(payload.get("cost", ())), "actual_cost": {}}
            history.append({"kind": kind, **{key: _code(payload[key]) for key in ("effect_id", "attempt_id", "operation_id", "pass_id", "action")},
                            "replaces_attempt_ids": tuple(_code(a) for a in payload.get("replaces_attempt_ids", ())),
                            "recorded_at": event.get("recorded_at")})
        elif kind == "effect_observed":
            if payload.get("effect_id") in effects:
                effects[payload["effect_id"]].update(certainty=payload["certainty"], actual_cost=dict(payload.get("actual_cost", ())))
                if payload.get("reconciliation_attempt_id"):
                    history.append({"kind": "effect_reconciled", "effect_id": _code(payload["effect_id"]),
                                    "reconciliation_attempt_id": _code(payload["reconciliation_attempt_id"]),
                                    "reconciliation_pass_id": _code(payload.get("reconciliation_pass_id")),
                                    "original_attempt_token": _code(payload.get("original_attempt_token")),
                                    "recorded_at": event.get("recorded_at")})
        elif kind == "failure":
            history.append({"kind": kind, "item_id": _code(payload.get("item_id")), "pass_id": _code(payload.get("pass_id")),
                            "reason_code": _code(payload.get("reason_code")), "checked_attempt_ids": tuple(_code(a) for a in payload.get("checked_attempt_ids", ())),
                            "recorded_at": event.get("recorded_at")})
        elif kind == "revision_committed":
            history.append({"kind": kind, "item_id": _code(payload.get("item_id")), "pass_id": _code(payload.get("pass_id")),
                            "revision_id": _code(payload.get("revision_id")), "captured_at": payload.get("captured_at"),
                            "recovered_at": payload.get("recovered_at")})
    for attempt in attempts:
        if attempt.get("pass_id") not in passes:
            pass_id = attempt.get("pass_id")
            passes[pass_id] = {"pass_id": pass_id, "status": "historical", "attempts": 0, "effects": 0,
                               "authorized_attempts": None, "authorized_effects": None, "authorized_cost": None,
                               "reserved_cost": {}, "actual_cost": {}, "unknown_cost": {}}
        if attempt.get("pass_id") in passes:
            passes[attempt["pass_id"]]["attempts"] += 1
    for effect in effects.values():
        target = passes.get(effect["pass_id"])
        if target:
            target["effects"] += 1
            for key, value in effect["cost"].items():
                target["reserved_cost"][key] = target["reserved_cost"].get(key, 0) + value
                if effect["certainty"] in {"admitted", "unknown"}:
                    target["unknown_cost"][key] = target["unknown_cost"].get(key, 0) + value
            for key, value in effect["actual_cost"].items():
                target["actual_cost"][key] = target["actual_cost"].get(key, 0) + value
    for accounting in passes.values():
        for dimension in ("attempts", "effects"):
            authorized = accounting.get("authorized_" + dimension)
            accounting["remaining_" + dimension] = None if authorized is None else max(0, authorized - accounting[dimension])
    items = []
    for state in snapshot.items:
        selection = selections.get(state.item_id)
        items.append({"item_id": state.item_id, "status": state.status,
                      "reason_code": _code(state.reason_code) if state.reason_code else "",
                      "next_action": selection.action if selection else "retain",
                      "unresolved_attempt_ids": tuple(_code(value) for value in sorted(set(state.unresolved_attempt_ids) | set(state.historical_unresolved_attempt_ids))),
                      "actionable_unresolved_attempt_ids": tuple(_code(value) for value in state.unresolved_attempt_ids),
                      "historical_unresolved_attempt_ids": tuple(_code(value) for value in state.historical_unresolved_attempt_ids),
                      "checked_attempt_ids": tuple(_code(value) for value in state.checked_attempt_ids),
                      "external_outcome_unknown": bool(state.unresolved_attempt_ids or state.historical_unresolved_attempt_ids),
                      "replacement_attempt_ids": tuple(_code(value) for value in selection.replacement_attempt_ids) if selection else (),
                      "evidence_links": tuple(link for value in state.evidence_links if (link := _link(value))),
                      "additional_attempt_allowance": selection.max_attempts if selection else 0,
                      "additional_effect_allowance": selection.max_effects if selection else 0})
    worker = json_value(snapshot.worker) if snapshot.worker else {"status": "unknown", "reason_code": "not_sampled"}
    for field_name in ("status", "reason_code", "execution_id", "ownership_status"):
        if worker.get(field_name):
            worker[field_name] = _code(worker[field_name])
    implementations = tuple({"operation_id": op.operation_id, "adapter_id": op.adapter_id,
                             "adapter_version": op.adapter_version, "parser_version": _code(op.parser_version) if op.parser_version else "",
                             "validator_version": _code(op.validator_version), "input_digest": op.input_digest,
                             "resource_scopes": tuple(_code(scope) for scope in op.resource_scopes),
                             "deadline_seconds": op.deadline_seconds, "local_only": op.local_only}
                            for op in snapshot.manifest.operations)
    return FailureReadout(1, snapshot.manifest.run_id, snapshot.manifest.scope_digest, snapshot.revision_digest,
                          snapshot.manifest.workflow_version, snapshot.manifest.policy_version, implementations, snapshot.sampled_at,
                          worker, tuple({"resource_id": _code(k), "status": _code(v)} for k, v in snapshot.resources),
                          tuple(items), tuple(history), {"lifetime_attempts": len(attempts), "lifetime_effects": len(effects),
                          "unknown_effects": sum(e["certainty"] in {"admitted", "unknown"} for e in effects.values()),
                          "passes": tuple(passes.values())}, preview.to_dict())
