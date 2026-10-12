"""Versioned, closed JSON codecs for durable workflow records.

No pickle, arbitrary mappings, callback reconstruction or dataclass introspection of
unregistered types is permitted. Errors never include the rejected payload.
"""
from __future__ import annotations

from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import fields
from enum import Enum
from functools import wraps
import json
import math
import re
import threading
import types
from typing import Literal, Union, get_args, get_origin, get_type_hints

from automation_core import contracts as c

MAX_BYTES = 1_048_576
MAX_ITEMS = 4096
MAX_DEPTH = 32
MAX_TEXT = 4096
SCHEMA_VERSION = 1


class PersistenceError(ValueError):
    def __init__(self):
        super().__init__("Invalid durable workflow record.")


_CONTRACT_RECORDS = (
    'ExpectedScope',
    'PrivateInputRef',
    'EmptyPayload',
    'AuthenticationInputs',
    'ResourceInputs',
    'AccountOutput',
    'ResourceOutput',
    'ObservationDetails',
    'RouteInputs',
    'RouteOutput',
    'ProviderInputs',
    'CodeSubmissionInputs',
    'ChallengeOutput',
    'ProviderOutput',
    'OperationIntent',
    'AdmissionPolicy',
    'Ready',
    'InAction',
    'ActionResultAvailable',
    'Finished',
    'RecoveryFrame',
    'RetryAdmission',
    'OutputRef',
    'CompletedRunReference',
    'EvidenceRef',
    'WorkflowCheckpoint',
    'ActionTicket',
    'CorrelatedEvidence',
    'EvidenceSubmission',
    'FactProvenance',
    'StateFact',
    'DialogReceipt',
    'StoredDispatchReceipt',
    'StoredActionResult',
    'CompletionEnvelope',
    'PendingOperation',
    'StateSnapshot',
    'StepDefinition',
    'Continuation',
    'RecoveryRuleDefinition',
    'WorkflowDefinitionRecord',
    'DialogPolicy',
    'LegacyEvidence',
    'LegacyResolutionEvidence',
    'PrivateAssetRef',
    'ProductionChallengeOutput',
    'AuthenticationCompletionOutput',
    'EmailAccessOutput',
    'EmailBaselineOutput',
    'EmailRequestMetadata',
    'EmailSelectionOutput',
    'ResourceIdentity',
    'RouteInputsV2',
    'KnownPathRecoveryInputs',
    'ComponentRecoveryInputs',
    'BlockCompletionInputs',
    'BlockCompletionOutput',
    'CallerContinuation',
    'PrerequisiteRequirement',
    'ActivationRef',
    'ProviderCommandInputs',
    'ProviderReceipt',
    'ActivationOutput',
    'InputBinding',
    'OutputBinding',
    'BlockDefinition',
    'ImageUploadInputs',
    'PrerequisiteInputs',
    'BlockerRuleRef',
    'BlockerCandidateEvidence',
    'BlockerEvidence',
    'PopupInstanceEvidence',
    'ConsentTransportEvidence',
    'ConsentInputs',
    'ConsentOutput',
    'InterruptionRuleRef',
    'InterruptionConditionEvidence',
    'InterruptionReport',
)
_TIMING_RECORDS = ("ScopeKey", "SavedTimeLimits", "SavedTimeScope", "TimeCheckpoint", "RestartClockPolicy")
_ENUMS = {cls.__name__: cls for cls in (c.Outcome, c.StopReason, c.Recording, c.EffectKind, c.RecoveryKind)}
_SCHEMAS = {
    c.CompletedRunReference: 'completed_run_reference.v1',
    c.EmptyPayload: 'empty.v1',
    c.AuthenticationInputs: 'authentication_inputs.v1',
    c.ResourceInputs: 'resource_inputs.v1',
    c.AccountOutput: 'account.v1',
    c.ResourceOutput: 'resource.v1',
    c.ObservationDetails: 'observation.v1',
    c.RouteInputs: 'route_inputs.v1',
    c.RouteOutput: 'route_output.v1',
    c.ProviderInputs: 'provider_inputs.v1',
    c.CodeSubmissionInputs: 'code_submission_inputs.v1',
    c.ChallengeOutput: 'challenge_output.v1',
    c.ProviderOutput: 'provider_output.v1',
    c.ProductionChallengeOutput: 'production_challenge.v1',
    c.AuthenticationCompletionOutput: 'authentication_completion.v1',
    c.EmailAccessOutput: 'email_access.v1',
    c.EmailBaselineOutput: 'email_baseline.v1',
    c.EmailRequestMetadata: 'email_request.v1',
    c.EmailSelectionOutput: 'email_selection.v1',
    c.RouteInputsV2: 'route_inputs.v2',
    c.KnownPathRecoveryInputs: 'known_path_recovery_inputs.v1',
    c.ComponentRecoveryInputs: 'component_recovery_inputs.v1',
    c.BlockCompletionInputs: 'block_completion_inputs.v1',
    c.BlockCompletionOutput: 'block_completion.v1',
    c.PrerequisiteRequirement: 'prerequisite_requirement.v1',
    c.ProviderCommandInputs: 'provider_command_inputs.v1',
    c.ActivationOutput: 'activation.v1',
    c.ImageUploadInputs: 'image_upload_inputs.v1',
    c.PrerequisiteInputs: 'prerequisite_inputs.v1',
    c.BlockerEvidence: 'blocker_evidence.v1',
    c.ConsentInputs: 'consent_inputs.v1',
    c.ConsentOutput: 'consent_output.v1',
    c.InterruptionReport: 'interruption_report.v1',
}

# Exact historical field sets and value constraints are decoded
# deliberately; defaults on arbitrary dataclasses never grant compatibility.
_RECORD_VERSIONS = {
    'OutputRef': 2,
    'WorkflowDefinitionRecord': 2,
    'ProductionChallengeOutput': 2,
    'CorrelatedEvidence': 5,
    'WorkflowCheckpoint': 2,
    'RecoveryFrame': 2,
}
_V1_DEFAULTS = {
    'OutputRef': {'output_schema': None},
    'WorkflowDefinitionRecord': {'blocks': ()},
    'ProductionChallengeOutput': {},
    'CorrelatedEvidence': {},
    'WorkflowCheckpoint': {'recovery_policy_version': 1, 'reconnect_attempts': 0},
    'RecoveryFrame': {'wait_expires_utc_us': 0},
}


def payload_schema(value) -> str:
    try:
        return _SCHEMAS[type(value)]
    except KeyError:
        raise PersistenceError() from None


_HINTS = {}
_REGISTRY = None
_REUSE = ContextVar('durable_value_reuse', default=None)
_CACHE_LIMIT = 2048
_CACHE_BYTES = 16 * 1_048_576
_MISSING = object()
_REUSE_LOCAL = threading.local()
_CONTROL_CHARACTERS = re.compile(r'[\x00-\x1f\x7f]')


class _ValueReuse:
    """Bounded pure values for an exact schema, never database verdicts."""
    def __init__(self, registry):
        self.registry = registry
        self.values = OrderedDict()
        self.bytes = 0
        self.record_fields = {cls: tuple(member.name for member in fields(cls))
            for cls in registry[0].values() if cls.__dataclass_params__.frozen}
        self.enum_types = frozenset(_ENUMS.values())
        self.compilations = {}

    def get(self, key):
        item = self.values.get(key)
        if item is None:
            return _MISSING
        self.values.move_to_end(key)
        return item[0]

    def put(self, key, value, size):
        if size > MAX_BYTES or key in self.values:
            return
        self.values[key] = (value, size)
        self.bytes += size
        while len(self.values) > _CACHE_LIMIT or self.bytes > _CACHE_BYTES:
            _, (_, removed) = self.values.popitem(last=False)
            self.bytes -= removed


def reuse_immutable_values(function):
    """Share decoding/schema work only across nested synchronous storage calls.

    Schema declarations are fixed for the process. SQL, ownership, source
    lineage, pending effects and timing checks still execute on every call.
    Tests replacing schema/decoder code must explicitly reset pure-value reuse.
    """
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _REUSE.get() is not None:
            return function(*args, **kwargs)
        state = getattr(_REUSE_LOCAL, 'state', None)
        if state is None or state.registry is not _registry():
            state = _ValueReuse(_registry())
            _REUSE_LOCAL.state = state
        token = _REUSE.set(state)
        try:
            return function(*args, **kwargs)
        finally:
            # Unbound executable recipes are shared only within this call.
            state.compilations.clear()
            _REUSE.reset(token)
    return wrapped


def _immutable_key(value):
    """Type-sensitive content key for deeply immutable durable values."""
    cls = type(value)
    if cls is float:
        # Equal Python floats can have distinct canonical JSON (-0.0 / 0.0).
        return cls, value.hex()
    if value is None or cls in (bool, int, float, str):
        return cls, value
    state = _REUSE.get()
    if cls in (state.enum_types if state is not None else _ENUMS.values()):
        return cls, value.value
    if cls is tuple:
        key = cls, tuple(_immutable_key(item) for item in value)
    elif state is not None and cls in state.record_fields:
        key = cls, tuple(_immutable_key(getattr(value, name)) for name in state.record_fields[cls])
    else:
        raise TypeError('Not an immutable durable value.')
    return key


def _cached_wire(value):
    state = _REUSE.get()
    if state is None:
        return None, None
    cached = state.get(('encoded_identity', id(value)))
    if cached is not _MISSING and cached[0] is value:
        return None, cached[1]
    try:
        key = ('encoded', _immutable_key(value))
        wire = state.get(key)
        if wire is not _MISSING:
            state.put(('encoded_identity', id(value)), (value, wire), len(wire.encode('utf-8')))
        return key, None if wire is _MISSING else wire
    except (TypeError, AttributeError, RecursionError):
        return None, None


def reset_schema_reuse():
    """Reset process-fixed metadata and this thread's values for isolated tests.

    Production changes to schemas require a process restart. This does not reset
    durable records, and must not run inside a storage operation.
    """
    global _REGISTRY
    if _REUSE.get() is not None:
        raise RuntimeError('Cannot reset schema reuse during a storage call.')
    _REGISTRY = None
    _HINTS.clear()
    _REUSE_LOCAL.state = None


def _registry():
    global _REGISTRY
    if _REGISTRY is None:
        from automation_core.workflow import timing
        records = {name: getattr(c, name) for name in _CONTRACT_RECORDS}
        records.update(_EXTENSION_RECORDS)
        records.update({name: getattr(timing, name) for name in _TIMING_RECORDS if hasattr(timing, name)})
        names = dict(vars(c))
        names.update(vars(timing))
        names.update(_EXTENSION_RECORDS)
        _REGISTRY = records, names
    return _REGISTRY


def _schema_hints(cls, names):
    """Resolve the fixed closed schema once; validate each actual payload."""
    if cls not in _HINTS:
        _HINTS[cls] = get_type_hints(cls, globalns={**names, **vars(__import__(cls.__module__, fromlist=["*"]))})
    return _HINTS[cls]


def _require(condition):
    if not condition:
        raise PersistenceError()


def _text(value, *, identifier=False):
    _require(type(value) is str and 0 < len(value) <= (256 if identifier else MAX_TEXT)
             and _CONTROL_CHARACTERS.search(value) is None)


def _integer(value, *, minimum=0):
    _require(type(value) is int and minimum <= value <= 2**63 - 1)


def _manifest_id(value, manifest):
    _require(type(value) is str and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", value) is not None)
    _require(value in manifest)


def _manifest_version(identifier, version, manifest):
    _integer(version, minimum=1)
    _manifest_id(identifier, frozenset(key for key, _ in manifest))
    _require((identifier, version) in manifest)


def _number(value, *, minimum=0):
    _require(type(value) in (int, float))
    try:
        _require(math.isfinite(value) and value >= minimum)
    except OverflowError:
        raise PersistenceError() from None


def _interruption_condition(value):
    """Validate closed descriptive combinations, without asserting site truth."""
    _integer(value.condition_id, minimum=1)
    _require(value.condition_id <= 128)
    _require(len(value.rule_refs) <= 32 and len(set(value.rule_refs)) == len(value.rule_refs))
    _require(len(value.source_refs) <= 32 and len(set(value.source_refs)) == len(value.source_refs))
    _require(value.relation == "unproven" or bool(value.source_refs))
    _require(value.recognition != "supported" or bool(value.source_refs))
    _require(value.problem_class is None or value.relation == "obstructs")
    metadata = {(row[0], row[1]): row[2:] for row in c.INTERRUPTION_RULE_MANIFEST}
    rules = [metadata[(ref.rule_id, ref.rule_version)] for ref in value.rule_refs]
    if value.recognition == "unknown":
        _require(not rules)
    elif value.recognition == "supported":
        driver_failure = (value.kind == "page_availability" and value.owner == "browser_driver"
                          and value.cause_code in {"navigation_failed", "network_failed"})
        _require(bool(rules) or driver_failure)
        _require(all(kind == value.kind and owner == value.owner and value.presentation in presentations
                     for kind, owner, presentations, _ in rules))
    else:
        _require(bool(rules))
    _require((value.kind == "native_dialog") == (value.presentation == "native_dialog"))
    _require(value.kind != "native_dialog" or value.owner == "browser_driver")
    _require(value.recognition != "supported" or value.kind != "unknown_overlay")
    availability = {"site_availability_error", "navigation_failed", "network_failed"}
    human = {"human_only", "missing_choice"}
    unsupported = {"unknown_obstruction", "unsupported_owner", "unsupported_control",
                   "unsupported_response", "conflicting_evidence"}
    if value.cause_code in human:
        _require(value.recognition == "supported" and value.relation == "obstructs"
                 and value.kind != "page_availability")
        if value.cause_code == "human_only":
            _require(bool(rules) and all(row[3] for row in rules))
        else:
            _require(value.kind == "cookie_consent" and not any(row[3] for row in rules))
    if value.cause_code in availability:
        _require(value.kind == "page_availability" and value.recognition == "supported"
                 and value.relation == "obstructs" and bool(value.source_refs))
        _require((value.owner == "page_component" and value.cause_code == "site_availability_error")
                 or (value.owner == "browser_driver" and value.presentation == "browser_surface"
                     and value.cause_code in {"navigation_failed", "network_failed"} and not rules))
    if value.cause_code in unsupported:
        _require(value.kind != "page_availability" and value.relation == "obstructs"
                 and value.presentation in {"popup", "overlay", "native_dialog"})
        _require(not (value.recognition == "supported" and any(row[3] for row in rules)))
        if value.cause_code == "unknown_obstruction":
            _require(value.recognition == "unknown")
        elif value.cause_code == "conflicting_evidence":
            _require(value.recognition == "conflicting")
        elif value.cause_code in {"unsupported_control", "unsupported_response"}:
            _require(value.recognition == "supported")
    if value.relation != "obstructs":
        _require(value.problem_class is None and value.cause_code in
                 {"none", "transient_loading", "requirement_not_ready"})
    if value.problem_class == "human_action_required":
        _require(value.cause_code in human and value.recognition == "supported")
    elif value.problem_class == "page_unavailable":
        _require(value.cause_code in availability and value.kind == "page_availability"
                 and value.recognition == "supported")
    elif value.problem_class == "popup_not_yet_supported":
        _require(value.cause_code in unsupported)
    # A positive unresolved classification cannot silently discard its class.
    if value.cause_code in human | availability | unsupported:
        expected = ("human_action_required" if value.cause_code in human else
                    "page_unavailable" if value.cause_code in availability else "popup_not_yet_supported")
        _require(value.problem_class == expected)


def _type_ok(value, hint):
    if hint in _PAYLOAD_TYPES:
        return type(value) in _PAYLOAD_TYPES[hint]
    origin, args = get_origin(hint), get_args(hint)
    if origin in (Union, types.UnionType):
        return any(_type_ok(value, h) for h in args)
    if origin is Literal:
        return any(type(value) is type(v) and value == v for v in args)
    if origin is tuple:
        if type(value) is not tuple or len(value) > MAX_ITEMS:
            return False
        if len(args) == 2 and args[1] is Ellipsis:
            return all(_type_ok(v, args[0]) for v in value)
        return len(value) == len(args) and all(_type_ok(v, h) for v, h in zip(value, args))
    if hint is float:
        return type(value) in (int, float) and not isinstance(value, bool)
    if hint is type(None):
        return value is None
    return type(value) is hint


def _semantic(value):
    validator = _SEMANTIC_VALIDATORS.get(type(value))
    if validator is not None:
        validator(value)
    """Cross-field rules complement the exact annotated field types."""
    name = type(value).__name__
    for field in fields(value):
        item = getattr(value, field.name)
        if isinstance(item, str) and not isinstance(item, Enum):
            _text(item, identifier=field.name not in {"intended_name", "portfolio_name", "ad_account_name", "entry_url", "fallback_url", "provider_url", "state_root"})
        if item is not None and (field.name.endswith("_utc_us") or field.name in {
            "definition_version", "version", "route_version", "revision", "admitted_revision", "observer_version",
            "dialog_count", "max_effect_intents", "max_recovery_admissions", "source_schema",
            "reconnect_attempts", "recovery_policy_version",
        }):
            _integer(item)
        if field.name in {"entered_at", "returned_at"} and item is not None:
            _number(item)
        if field.name in {"reason", "cleanup_reason"} and item is not None:
            _require(re.fullmatch(r"[a-z][a-z0-9_]{0,95}", item) is not None)
        if field.name in {"pending_operation_ids", "required_output_keys", "normal_next",
                          "restore_step_ids", "attempt_ids", "component_path", "unresolved_attempt_ids"}:
            for identifier in item:
                _text(identifier, identifier=True)
    if hasattr(value, "evidence_refs") and name != "StoredActionResult":
        _require(bool(value.evidence_refs) and len(set(value.evidence_refs)) == len(value.evidence_refs))
    if name == "BlockerRuleRef":
        _manifest_version(value.rule_id, value.rule_version, c.BLOCKER_RULE_MANIFEST)
    elif name == "InterruptionRuleRef":
        _manifest_version(value.rule_id, value.rule_version,
                          frozenset((row[0], row[1]) for row in c.INTERRUPTION_RULE_MANIFEST))
    elif name == "InterruptionConditionEvidence":
        _interruption_condition(value)
    elif name == "InterruptionReport":
        _manifest_version(value.requirement_id, value.requirement_version,
                          frozenset((row[0], row[1]) for row in c.INTERRUPTION_REQUIREMENT_MANIFEST))
        _require(len(value.conditions) <= 128)
        _require(len({item.condition_id for item in value.conditions}) == len(value.conditions))
        union = {item.problem_class for item in value.conditions if item.problem_class is not None}
        _require(value.classes == tuple(item for item in c.INTERRUPTION_CLASS_ORDER if item in union))
        _require(not value.limit_hit or not value.coverage_complete)
        if value.requirement_status == "ready":
            _require(value.coverage_complete and not value.limit_hit
                     and not any(item.relation == "obstructs" for item in value.conditions))
    elif name == "BlockerCandidateEvidence":
        _require(len(set(value.matched_rules)) == len(value.matched_rules))
        if value.classification == "unknown":
            _require(not value.matched_rules)
        elif value.classification == "recognized":
            _require(len(value.matched_rules) == 1)
        else:
            # One rule can match duplicate roots/controls. Preserve that rule
            # once rather than inventing a second match to encode ambiguity.
            _require(bool(value.matched_rules))
        _require((value.kind == "native_dialog") == (value.presentation == "native_dialog"))
    elif name == "BlockerEvidence":
        blocking = any(candidate.blocking for candidate in value.candidates)
        ambiguous = any(candidate.classification == "ambiguous" for candidate in value.candidates)
        _require(not value.limit_hit or not value.coverage_complete)
        _require(not ambiguous or value.status == "indeterminate")
        if value.status == "clear":
            _require(value.coverage_complete and not value.limit_hit and value.requirement_ready
                     and not blocking and value.reason == "none")
        elif value.status == "blocked":
            _require(value.coverage_complete and not value.limit_hit and not value.requirement_ready
                     and blocking and value.reason in {"recognized_blocker", "unknown_blocker"})
            unknown = any(candidate.blocking and candidate.classification == "unknown" for candidate in value.candidates)
            _require((value.reason == "unknown_blocker") == unknown)
        elif value.status == "rendering":
            _require(value.coverage_complete and not value.limit_hit and not value.requirement_ready
                     and not blocking and value.reason == "transient_loading")
        else:
            _require(not value.requirement_ready and value.reason not in
                     {"none", "recognized_blocker", "unknown_blocker", "transient_loading"})
        if value.reason == "limit_exhausted":
            _require(value.limit_hit)
        if value.reason == "incomplete_coverage":
            _require(not value.coverage_complete)
    elif name == "PopupInstanceEvidence":
        _require(re.fullmatch(r"[0-9a-f]{64}", value.document_id) is not None)
        _require((value.status == "clear" and value.instance_id is None)
                 or (value.status == "present" and type(value.instance_id) is str
                     and re.fullmatch(r"[0-9a-f]{64}", value.instance_id) is not None))
    elif name == "ConsentTransportEvidence":
        _integer(value.request_count)
        _require(value.request_count <= 65)
        _require(not value.acknowledged or value.reason == "none")
    elif name == "ConsentInputs":
        _manifest_id(value.choice_id, c.CONSENT_CHOICE_MANIFEST)
        _manifest_version(value.policy_id, value.policy_version, c.BLOCKER_POLICY_MANIFEST)
        _manifest_version(value.rule_id, value.rule_version, c.BLOCKER_RULE_MANIFEST)
        _require(value.rule_id in c.CONSENT_RULE_MANIFEST)
    elif name == "ConsentOutput":
        _manifest_id(value.choice_id, c.CONSENT_CHOICE_MANIFEST)
        _require(not value.continuation_ready or (value.preference_acknowledged and value.panel_removed
                                                 and value.remaining_obstruction == "none"))
    elif name == "PrivateAssetRef":
        _require(all(re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", getattr(value, field)) is not None
                     for field in ("provider_id", "reference_id", "version_id")))
    elif name == "OutputRef":
        _require(value.output_schema is None or value.output_schema in set(_SCHEMAS.values()))
    elif name in {"RouteInputs", "RouteInputsV2"}:
        _integer(value.route_version, minimum=1)
        if name == "RouteInputsV2":
            _require(bool(value.entry_states) and len(set(value.entry_states)) == len(value.entry_states))
            _require(len({r.kind for r in value.resources}) == len(value.resources))
            for state in value.entry_states:
                _text(state, identifier=True)
            _require(value.declared_origin is not None)
    elif name == "ResourceIdentity":
        _require((value.parent_kind is None) == (value.parent_id is None))
        _require(value.parent_kind != value.kind)
        expected_parent = {"ad_account": "portfolio", "post": "page"}.get(value.kind)
        if expected_parent is not None:
            _require(value.parent_kind == expected_parent)
    elif name == "ProductionChallengeOutput":
        _integer(value.document_generation)
    elif name == "EmailBaselineOutput":
        _require(value.complete and len(value.baseline_keys) <= 4096
                 and len(set(value.baseline_keys)) == len(value.baseline_keys))
        for key in value.baseline_keys:
            _text(key, identifier=True)
        _require(value.expires_utc_us > value.started_utc_us
                 and value.expires_utc_us - value.started_utc_us <= 180_000_000
                 and value.started_utc_us <= value.verified_utc_us <= value.expires_utc_us)
    elif name == "EmailRequestMetadata":
        _require(value.expires_utc_us > value.requested_utc_us)
    elif name == "EmailSelectionOutput":
        # Preserve the existing matcher: message server clocks may lead the
        # observation by at most five seconds; selection itself is local UTC.
        _require(value.received_utc_us <= value.selected_utc_us + 5_000_000
                 and value.selected_utc_us <= value.verified_utc_us)
    elif name == "ProviderCommandInputs":
        _require((value.command == "acquire") == (value.activation is None))
        if value.activation is not None:
            _require(value.activation.provider_id == value.provider_id)
            _require(value.service_id is None and value.country_id is None and value.max_cost_micros is None)
        if value.max_cost_micros is not None:
            _integer(value.max_cost_micros)
    elif name == "ProviderReceipt":
        if value.activation is not None:
            _require(value.activation.provider_id == value.provider_id)
        if value.returned_at is not None:
            _require(value.entered_at is not None and value.returned_at >= value.entered_at)
        if value.status == "returned":
            _require(value.entered_at is not None and value.returned_at is not None)
        if value.status == "not_attempted":
            _require(value.entered_at is None and value.returned_at is None and value.activation is None)
        _require((value.cost_micros is None) == (value.currency is None))
        if value.cost_micros is not None:
            _integer(value.cost_micros)
        if value.provider_status is not None:
            _require(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value.provider_status) is not None)
    elif name == "ActivationOutput":
        _require((value.cost_micros is None) == (value.currency is None))
        if value.cost_micros is not None:
            _integer(value.cost_micros)
    elif name == "CallerContinuation":
        _require(len(set(value.input_refs)) == len(value.input_refs)
                 and len(set(value.output_refs)) == len(value.output_refs))
    elif name == "BlockDefinition":
        _require(bool(value.step_ids) and len(set(value.step_ids)) == len(value.step_ids))
        _require(value.entry_step_id in value.step_ids and value.completion_step_id in value.step_ids)
        _require(all(s.startswith(value.block_id + ".") for s in value.step_ids))
        _require(len({b.input_key for b in value.input_bindings}) == len(value.input_bindings))
        _require(len({b.output_key for b in value.output_bindings}) == len(value.output_bindings))
        _require(len(set(value.allowed_successor_block_ids)) == len(value.allowed_successor_block_ids))
        _require(not value.final or not value.allowed_successor_block_ids)
        for binding in value.output_bindings:
            _require(binding.producer_step_id in value.step_ids)
        for binding in value.input_bindings:
            earlier = {b.input_key for b in value.input_bindings[:value.input_bindings.index(binding)]}
            _require(binding.parent_input_key is None or binding.parent_input_key in earlier)
    elif name in {"ChallengeOutput", "ProviderOutput"}:
        _integer(value.document_generation)
        if name == "ProviderOutput" and value.state in {"code_generated", "code_available"}:
            _require(value.input_revision is not None and value.output_revision == value.input_revision
                     and value.window_id is not None and value.generated_utc_us is not None
                     and value.expires_utc_us is not None and value.expires_utc_us > value.generated_utc_us)
    elif name == "ProviderInputs":
        _integer(value.minimum_lifetime_ms, minimum=1)
    elif name == "RouteOutput":
        _require((value.view_id is None) == (value.generation is None))
        _require((value.state == "new") == (value.view_id is None))
        _require(value.account_id is not None or value.state in {"new", "opened"})
    elif name == "ExpectedScope":
        _require(value.resource_ids == tuple(sorted(value.resource_ids)))
        _require(len({p[0] for p in value.resource_ids}) == len(value.resource_ids))
        for pair in value.resource_ids:
            for item in pair:
                _text(item, identifier=True)
    elif name == "OperationIntent":
        _require(value.input_schema == payload_schema(value.permitted_inputs))
        _require(len(set(value.private_inputs)) == len(value.private_inputs))
        _require(value.corrective_parent_id != value.operation_id)
        if value.corrective_parent_id is None:
            _require(value.root_operation_id == value.operation_id)
        if value.dialog_policy is not None and value.dialog_policy.prompt_ref is not None:
            _require(value.dialog_policy.prompt_ref in value.private_inputs)
        if type(value.permitted_inputs) is c.ConsentInputs:
            _require(value.corrective_parent_id is not None and value.root_operation_id != value.operation_id
                     and not value.private_inputs and value.effect_kind == c.EffectKind.CONSEQUENTIAL)
    elif name == "AdmissionPolicy":
        _integer(value.max_attempts, minimum=1)
        _integer(value.max_effect_intents)
        _integer(value.max_recovery_admissions)
        _require(value.max_effect_intents <= value.max_attempts)
    elif name == "ActionTicket":
        _require((value.lane == "main" and value.frame_id is None)
                 or (value.lane == "recovery" and value.frame_id is not None))
    elif name == "WorkflowCheckpoint":
        _require(len(set(value.pending_operation_ids)) == len(value.pending_operation_ids))
        _require(len({s.scope_key.token() for s in value.time_scopes}) == len(value.time_scopes))
        if value.status == "finished":
            _require(type(value.main) is c.Finished and value.recovery is None
                     and value.retry_admission is None and not value.pending_operation_ids)
    elif name == "CorrelatedEvidence":
        _require(bool(value.attempt_ids) and len(set(value.attempt_ids)) == len(value.attempt_ids))
        if type(value.details) is c.InterruptionReport:
            expected = next(row[2:] for row in c.INTERRUPTION_REQUIREMENT_MANIFEST
                            if row[:2] == (value.details.requirement_id, value.details.requirement_version))
            _require((value.observer_id, value.observer_version) == expected)
    elif name == "StateFact":
        _require(value.value_schema == payload_schema(value.value))
    elif name == "StoredActionResult":
        if value.outcome in {c.Outcome.CONFIRMED, c.Outcome.REJECTED}:
            _require(bool(value.evidence_refs))
        if value.outcome == c.Outcome.NOT_RUN:
            _require(value.stop_reason is not None)
        _require(len(set(value.evidence_refs)) == len(value.evidence_refs))
    elif name == "CompletionEnvelope":
        _require((value.ticket.operation_id, value.ticket.attempt_id) ==
                 (value.result.operation_id, value.result.attempt_id))
    elif name == "StoredDispatchReceipt":
        _require(len(value.dialogs) <= 64 and value.dialog_count >= len(value.dialogs))
    elif name == "DialogReceipt":
        if value.returned_at is not None:
            _require(value.entered_at is not None and value.returned_at >= value.entered_at)
    elif name == "DialogPolicy":
        _integer(value.max_count, minimum=1)
        _require(value.max_count <= 64)
        if value.prompt_ref is not None:
            _require(value.dialog_type == "prompt" and value.response == "accept")
    elif name == "StepDefinition":
        _require(len(set(value.normal_next)) == len(value.normal_next))
        if value.effect_kind == c.EffectKind.READ_ONLY:
            _require(value.policy.max_effect_intents == 0)
        else:
            _require(value.policy.max_effect_intents > 0)
    elif name == "RecoveryRuleDefinition":
        _integer(value.priority, minimum=-(2**63))
        if value.kind == c.RecoveryKind.CORRECTION:
            _require(value.branch_id is not None)
    elif name == "WorkflowDefinitionRecord":
        step_map = {s.step_id: s for s in value.steps}
        cont_map = {s.continuation_id: s for s in value.continuations}
        _require(bool(value.steps) and len(step_map) == len(value.steps))
        _require(len(cont_map) == len(value.continuations))
        _require(len({r.rule_id for r in value.recovery_rules}) == len(value.recovery_rules))
        for step in value.steps:
            _require(all(key in step_map for key in step.normal_next))
        for continuation in value.continuations:
            _require(continuation.target_step_id in step_map)
            _require(len(set(continuation.restore_step_ids)) == len(continuation.restore_step_ids))
            _require(all(key in step_map and step_map[key].restore_allowed
                         and step_map[key].effect_kind == c.EffectKind.REPEATABLE_PREPARATION
                         for key in continuation.restore_step_ids))
        _require(all(r.continuation_id in cont_map for r in value.recovery_rules))
        if value.blocks:
            block_map = {b.block_id: b for b in value.blocks}
            block_order = {b.block_id: index for index, b in enumerate(value.blocks)}
            _require(len(block_map) == len(value.blocks) and sum(b.final for b in value.blocks) == 1)
            _require(value.steps[0].step_id == value.blocks[0].entry_step_id)
            owners = [s for b in value.blocks for s in b.step_ids]
            _require(len(owners) == len(set(owners)) and set(owners) == set(step_map))
            for block in value.blocks:
                _require(block.input_schema in set(_SCHEMAS.values()))
                _require(step_map[block.completion_step_id].effect_kind == c.EffectKind.READ_ONLY)
                _require(set(block.allowed_successor_block_ids) <= set(block_map))
                _require(block.block_id not in block.allowed_successor_block_ids)
                _require(all(block_order[key] > block_order[block.block_id] for key in block.allowed_successor_block_ids))
                expected = {block_map[key].entry_step_id for key in block.allowed_successor_block_ids}
                _require(set(step_map[block.completion_step_id].normal_next) == expected)
                _require(block.final == (not expected))
                for step_id in block.step_ids:
                    for next_id in step_map[step_id].normal_next:
                        _require(next_id in block.step_ids or step_id == block.completion_step_id)
                for binding in block.input_bindings:
                    _require(binding.producer_block_id in block_map and binding.producer_block_id != block.block_id)
                    _require(block_order[binding.producer_block_id] < block_order[block.block_id])
                    producer = block_map[binding.producer_block_id]
                    _require(c.OutputBinding(binding.output_key, binding.output_schema, binding.producer_step_id)
                             in producer.output_bindings)
                for binding in block.output_bindings:
                    _require(binding.output_schema in set(_SCHEMAS.values()))
    elif name == "SavedTimeLimits":
        _number(value.normal_remaining_cap_s)
        _number(value.completion_remaining_cap_s)
        _require(value.normal_remaining_cap_s <= value.completion_remaining_cap_s)
        _require(value.normal_expires_utc_us <= value.completion_expires_utc_us)
    elif name == "SavedTimeScope":
        _number(value.allowance_s, minimum=.001)
        _number(value.normal_fraction)
        _require(0 < value.normal_fraction <= 1)
        _require(value.parent_scope_key != value.scope_key.token())
    elif name == "RestartClockPolicy":
        _number(value.uncertainty_s)
    elif name == "TimeCheckpoint":
        _require(value.utc_high_water_us >= value.sampled_utc_us)
        _require(len({s.scope_key.token() for s in value.scopes}) == len(value.scopes))
        _require(len({key for key, _ in value.action_limits}) == len(value.action_limits))
    elif name == "LegacyEvidence":
        from pathlib import PurePosixPath
        path = PurePosixPath(value.source_path)
        _require(not path.is_absolute() and ".." not in path.parts and path.parts != ())
        for part in value.source_identity:
            _integer(part)
    elif name == "LegacyResolutionEvidence":
        allowed = {"confirmed_existing": {"server_operation_id", "resource_identity"},
                   "definitive_noncompletion": {"definitive_noncompletion"},
                   "historical_metadata_reviewed": {"source_metadata"}}
        _require(value.correlation_kind in allowed[value.conclusion])


def _encode(value, records, names, depth):
    _require(depth <= MAX_DEPTH)
    cls = type(value)
    if value is None or cls in (bool, int, float, str):
        if cls is str:
            _text(value)
        elif cls is int:
            _require(-(2**63) <= value <= 2**63 - 1)
        elif cls is float:
            _require(math.isfinite(value))
        return value
    if cls is tuple:
        _require(len(value) <= MAX_ITEMS)
        return {"$tuple": [_encode(v, records, names, depth + 1) for v in value]}
    if cls in _ENUMS.values():
        return {"$enum": cls.__name__, "value": value.value}
    _require(records.get(cls.__name__) is cls)
    hints = _schema_hints(cls, names)
    for field in fields(value):
        _require(_type_ok(getattr(value, field.name), hints[field.name]))
    _semantic(value)
    version = _RECORD_VERSIONS.get(cls.__name__, SCHEMA_VERSION)
    if cls is c.CorrelatedEvidence and type(value.details) is c.ObservationDetails:
        version = 1  # Ordinary evidence retains its exact historical wire form.
    elif cls is c.CorrelatedEvidence and type(value.details) is c.BlockerEvidence:
        version = 2  # Blocker evidence also keeps its existing explicit envelope.
    elif cls is c.CorrelatedEvidence and type(value.details) is c.InterruptionReport:
        version = 3  # Reports retain the existing historical envelope.
    elif cls is c.CorrelatedEvidence and type(value.details) is c.PopupInstanceEvidence:
        version = 4  # Instance evidence retains its previously saved envelope.
    omitted = set()
    if cls.__name__ in _COMPACT_V1_RECORDS:
        defaults = _V1_DEFAULTS[cls.__name__]
        if all(getattr(value, name) == default for name, default in defaults.items()):
            # Retain historical continuation digests and legacy policy on reads.
            version, omitted = 1, set(defaults)
    return {"$type": cls.__name__, "version": version,
            "fields": {f.name: _encode(getattr(value, f.name), records, names, depth + 1)
                       for f in fields(value) if f.name not in omitted}}


def encode(value):
    try:
        key, wire = _cached_wire(value)
        if wire is not None:
            # Encoded dictionaries belong to the caller and may be mutated.
            return json.loads(wire)
        result = _encode(value, *_registry(), 0)
        if key is not None:
            wire = json.dumps(result, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=True, allow_nan=False)
            _REUSE.get().put(key, wire, len(wire.encode('utf-8')))
            # Retain the deeply frozen value so object ids cannot be recycled.
            _REUSE.get().put(('encoded_identity', id(value)), (value, wire), len(wire.encode('utf-8')))
        return result
    except PersistenceError:
        raise
    except Exception:
        raise PersistenceError() from None


def _decode(value, records, names, depth):
    _require(depth <= MAX_DEPTH)
    if value is None or type(value) in (bool, int, float, str):
        return _encode(value, records, names, depth)
    _require(type(value) is dict)
    if set(value) == {"$tuple"}:
        _require(type(value["$tuple"]) is list and len(value["$tuple"]) <= MAX_ITEMS)
        return tuple(_decode(v, records, names, depth + 1) for v in value["$tuple"])
    if set(value) == {"$enum", "value"}:
        _require(type(value["$enum"]) is str and value["$enum"] in _ENUMS)
        _require(type(value["value"]) is str)
        return _ENUMS[value["$enum"]](value["value"])
    _require(set(value) == {"$type", "version", "fields"})
    _require(type(value["$type"]) is str and value["$type"] in records)
    cls = records[value["$type"]]
    version = value["version"]
    current_version = _RECORD_VERSIONS.get(cls.__name__, SCHEMA_VERSION)
    historical_evidence = cls is c.CorrelatedEvidence and version in (1, 2, 3, 4, 5, 6)
    _require(type(version) is int and (version == current_version or historical_evidence
                                      or (version == 1 and cls.__name__ in _V1_DEFAULTS)))
    raw = value["fields"]
    historical = _V1_DEFAULTS.get(cls.__name__, {}) if version != current_version else {}
    _require(type(raw) is dict and set(raw) == {f.name for f in fields(cls)} - set(historical))
    decoded = {key: _decode(item, records, names, depth + 1) for key, item in raw.items()}
    decoded.update(historical)
    # v1 required a real string. v2 explicitly represents absent site IDs; its
    # retained form/document/source evidence is never reconstructed from this row.
    if cls.__name__ == "ProductionChallengeOutput" and version == 1:
        _require(type(decoded["challenge_id"]) is str)
    if cls is c.CorrelatedEvidence and version == 1:
        _require(type(decoded["details"]) is c.ObservationDetails)
    elif cls is c.CorrelatedEvidence and version == 2:
        _require(type(decoded["details"]) in (c.ObservationDetails, c.BlockerEvidence))
    elif cls is c.CorrelatedEvidence and version == 3:
        _require(type(decoded["details"]) is c.InterruptionReport)
    elif cls is c.CorrelatedEvidence and version == 4:
        _require(type(decoded["details"]) is c.PopupInstanceEvidence)
    elif cls is c.CorrelatedEvidence:
        _require(type(decoded["details"]) is c.ConsentTransportEvidence)
    hints = _schema_hints(cls, names)
    _require(all(_type_ok(v, hints[k]) for k, v in decoded.items()))
    result = cls(**decoded)
    # Decoding must not silently normalize an untrusted noncanonical representation.
    _require(all(getattr(result, k) == v for k, v in decoded.items()))
    _semantic(result)
    return result


def decode(value, expected_type=None):
    try:
        result = _decode(value, *_registry(), 0)
        if expected_type is not None:
            _require(_type_ok(result, expected_type))
        return result
    except PersistenceError:
        raise
    except Exception:
        raise PersistenceError() from None


def dumps(value) -> str:
    try:
        _, cached = _cached_wire(value)
        if cached is not None:
            _require(len(cached.encode('utf-8')) <= MAX_BYTES)
            return cached
        result = json.dumps(encode(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        _require(len(result.encode("utf-8")) <= MAX_BYTES)
        return result
    except PersistenceError:
        raise
    except Exception:
        raise PersistenceError() from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def loads(value: str, expected_type=None):
    try:
        _require(type(value) is str and len(value.encode("utf-8")) <= MAX_BYTES)
        state = _REUSE.get()
        key = ('decoded', value, expected_type)
        if state is not None:
            cached = state.get(key)
            if cached is not _MISSING:
                return cached
        raw = json.loads(value, object_pairs_hook=_pairs,
                         parse_constant=lambda _: (_ for _ in ()).throw(PersistenceError()))
        result = decode(raw, expected_type)
        if state is not None:
            try:
                _immutable_key(result)
            except (TypeError, AttributeError, RecursionError):
                pass
            else:
                state.put(key, result, len(value.encode('utf-8')))
        return result
    except PersistenceError:
        raise
    except Exception:
        raise PersistenceError() from None


def validate(value) -> None:
    _, wire = _cached_wire(value)
    if wire is None:
        encode(value)


def public_interruption_report(reference, report):
    """Project only the closed durable vocabulary to a plain public JSON value."""
    _require(type(reference) is c.EvidenceRef and type(report) is c.InterruptionReport)
    validate(reference)
    validate(report)
    conditions = []
    for condition in report.conditions:
        conditions.append({
            "condition_id": condition.condition_id, "kind": condition.kind,
            "presentation": condition.presentation, "owner": condition.owner,
            "relation": condition.relation, "recognition": condition.recognition,
            "rule_refs": [{"rule_id": ref.rule_id, "rule_version": ref.rule_version}
                          for ref in condition.rule_refs],
            "cause_code": condition.cause_code,
            "source_refs": [{"event_id": ref.event_id} for ref in condition.source_refs],
            "problem_class": condition.problem_class,
        })
    return {
        "event_id": reference.event_id, "state": report.state,
        "requirement_id": report.requirement_id, "requirement_version": report.requirement_version,
        "phase": report.phase, "requirement_status": report.requirement_status,
        "coverage_complete": report.coverage_complete, "limit_hit": report.limit_hit,
        "conditions": conditions, "classes": list(report.classes),
    }


def validate_public_interruption_report(value) -> None:
    """Validate the exact public projection, without accepting arbitrary details."""
    try:
        expected = {field.name for field in fields(c.InterruptionReport)} | {"event_id"}
        _require(type(value) is dict and set(value) == expected)
        validate(c.EvidenceRef(value["event_id"]))
        _require(type(value["conditions"]) is list and len(value["conditions"]) <= 128)
        _require(type(value["classes"]) is list)
        conditions = []
        for condition in value["conditions"]:
            _require(type(condition) is dict
                     and set(condition) == {field.name for field in fields(c.InterruptionConditionEvidence)})
            _require(type(condition["rule_refs"]) is list and type(condition["source_refs"]) is list)
            _require(all(type(ref) is dict and set(ref) == {"rule_id", "rule_version"}
                         for ref in condition["rule_refs"]))
            _require(all(type(ref) is dict and set(ref) == {"event_id"} for ref in condition["source_refs"]))
            conditions.append(c.InterruptionConditionEvidence(**{
                **condition,
                "rule_refs": tuple(c.InterruptionRuleRef(**ref) for ref in condition["rule_refs"]),
                "source_refs": tuple(c.EvidenceRef(**ref) for ref in condition["source_refs"]),
            }))
        report = c.InterruptionReport(**{key: item for key, item in value.items()
                                         if key not in {"event_id", "conditions", "classes"}},
                                      conditions=tuple(conditions), classes=tuple(value["classes"]))
        validate(report)
        _require(public_interruption_report(c.EvidenceRef(value["event_id"]), report) == value)
    except PersistenceError:
        raise
    except Exception:
        raise PersistenceError() from None


_COMPACT_V1_RECORDS = {"WorkflowCheckpoint", "RecoveryFrame"}
_EXTENSION_RECORDS = {}
_SEMANTIC_VALIDATORS = {}
_PAYLOAD_TYPES = {c.PermittedInputs: set(), c.SafeOutput: set(), c.FactValue: set()}

def register_records(namespace, records, schemas, versions, defaults, payload_types, validator=None, manifests=None, *, compact_v1_records=()):
    """Register source-declared records at application import, never from saved data."""
    if _REUSE.get() is not None:
        raise RuntimeError('Cannot change schemas during a storage operation.')
    for name in records:
        cls = namespace[name]
        existing = _EXTENSION_RECORDS.get(name, getattr(c, name, None))
        if existing is not None and existing is not cls:
            raise ValueError('Conflicting durable record name.')
        _EXTENSION_RECORDS[name] = cls
        if validator is not None:
            _SEMANTIC_VALIDATORS[cls] = validator
    _SCHEMAS.update(schemas)
    _RECORD_VERSIONS.update(versions)
    _V1_DEFAULTS.update(defaults)
    _COMPACT_V1_RECORDS.update(compact_v1_records)
    for marker, types in payload_types.items():
        _PAYLOAD_TYPES[marker].update(types)
    for name, values in (manifests or {}).items():
        existing = getattr(c, name)
        setattr(c, name, tuple(dict.fromkeys((*existing, *values))))
    reset_schema_reuse()

_PAYLOAD_TYPES[c.PermittedInputs].update((c.AuthenticationInputs, c.BlockCompletionInputs, c.CodeSubmissionInputs, c.ComponentRecoveryInputs, c.ConsentInputs, c.EmailRequestMetadata, c.EmptyPayload, c.ImageUploadInputs, c.KnownPathRecoveryInputs, c.PrerequisiteInputs, c.PrerequisiteRequirement, c.ProviderCommandInputs, c.ProviderInputs, c.ResourceInputs, c.RouteInputs, c.RouteInputsV2,))

_PAYLOAD_TYPES[c.SafeOutput].update((c.AccountOutput, c.ActivationOutput, c.AuthenticationCompletionOutput, c.BlockCompletionOutput, c.BlockerEvidence, c.ChallengeOutput, c.ConsentOutput, c.EmailAccessOutput, c.EmailBaselineOutput, c.EmailRequestMetadata, c.EmailSelectionOutput, c.EmptyPayload, c.ObservationDetails, c.PrerequisiteRequirement, c.ProductionChallengeOutput, c.ProviderOutput, c.ResourceOutput, c.RouteOutput,))

_PAYLOAD_TYPES[c.FactValue].update((c.AccountOutput, c.ObservationDetails, c.ResourceOutput,))


def is_payload(value, marker):
    """Exact membership in a source-declared input/output/fact family."""
    return type(value) in _PAYLOAD_TYPES[marker]
