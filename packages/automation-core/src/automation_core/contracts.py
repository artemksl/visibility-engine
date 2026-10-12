from __future__ import annotations

from dataclasses import dataclass, field

from enum import Enum

from typing import Callable, Generic, Literal, TypeVar, TYPE_CHECKING

from uuid import NAMESPACE_URL, uuid5

import json

if TYPE_CHECKING:
    from automation_core.workflow.timing import RestartClockPolicy, SavedTimeLimits, SavedTimeScope, TimeCheckpoint
    from automation_core.browser.pointer import MovementOptions, MovementTrace
    from automation_core.browser.scrolling import ScrollIntent, ScrollLayout
    from automation_core.assets import ResolvedPrivateAsset

class Outcome(str, Enum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    NOT_RUN = "not_run"

class StopReason(str, Enum):
    NEEDS_INPUT = "needs_input"
    SESSION_EXPIRED = "session_expired"
    CONNECTION_LOST = "connection_lost"
    NETWORK_FAILED = "network_failed"
    NAVIGATION_FAILED = "navigation_failed"
    STALE_VIEW = "stale_view"
    PAGE_NOT_READY = "page_not_ready"
    DEADLINE_EXHAUSTED = "deadline_exhausted"
    CANCELLED = "cancelled"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    IDENTITY_MISMATCH = "identity_mismatch"
    WORKFLOW_INCOMPATIBLE = "workflow_incompatible"
    INTENT_MISMATCH = "intent_mismatch"
    RECOVERY_EXHAUSTED = "recovery_exhausted"
    ADMISSION_EXHAUSTED = "admission_exhausted"
    NO_RECOVERY_RULE = "no_recovery_rule"
    AMBIGUOUS_RECOVERY_RULE = "ambiguous_recovery_rule"
    PREREQUISITE_UNSATISFIED = "prerequisite_unsatisfied"
    INPUT_UNAVAILABLE = "input_unavailable"
    INPUT_EXPIRED = "input_expired"
    CLOCK_UNTRUSTED = "clock_untrusted"
    LEGACY_STATE_BLOCKED = "legacy_state_blocked"
    OWNERSHIP_LOST = "ownership_lost"
    INTERACTION_UNSETTLED = "interaction_unsettled"
    UNEXPECTED_DIALOG = "unexpected_dialog"
    DIALOG_LIMIT = "dialog_limit"
    IMPLEMENTATION_ERROR = "implementation_error"
    UNSUPPORTED_AUTH_STATE = "unsupported_auth_state"
    CHALLENGE_CONTINUITY_LOST = "challenge_continuity_lost"
    PROVIDER_ORIGIN_MISMATCH = "provider_origin_mismatch"
    PROVIDER_OUTPUT_UNVERIFIED = "provider_output_unverified"

class Recording(str, Enum):
    SAVED = "saved"
    UNCONFIRMED = "unconfirmed"
    NOT_REQUIRED = "not_required"

class EffectKind(str, Enum):
    READ_ONLY = "read_only"
    REPEATABLE_PREPARATION = "repeatable_preparation"
    CONSEQUENTIAL = "consequential"

class RecoveryKind(str, Enum):
    CORRECTION = "correction"
    RECONCILIATION = "reconciliation"
    RETRY = "retry"

@dataclass(frozen=True)
class ExpectedScope:
    account_key: str
    expected_account_id: str
    profile_id: str
    resource_ids: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        if not isinstance(self.resource_ids, tuple) or any(
            not isinstance(p, tuple) or len(p) != 2 or not all(isinstance(v, str) and v for v in p)
            for p in self.resource_ids
        ) or len({p[0] for p in self.resource_ids}) != len(self.resource_ids):
            raise ValueError("Invalid resource identities.")
        object.__setattr__(self, "resource_ids", tuple(sorted(self.resource_ids)))

@dataclass(frozen=True)
class PrivateInputRef:
    provider_id: str
    reference_id: str
    version_id: str

@dataclass(frozen=True)
class PrivateAssetRef:
    provider_id: str
    reference_id: str
    version_id: str

@dataclass(frozen=True)
class EmptyPayload:
    pass

@dataclass(frozen=True)
class AuthenticationInputs:
    expected_account_id: str

@dataclass(frozen=True)
class ResourceInputs:
    resource_kind: str
    intended_name: str

@dataclass(frozen=True)
class AccountOutput:
    account_id: str
    verified_utc_us: int = 0

@dataclass(frozen=True)
class ResourceOutput:
    resource_id: str

@dataclass(frozen=True)
class ObservationDetails:
    state: str
    resource_id: str | None = None

BlockerKind = Literal["cookie_consent", "captcha", "account_security", "native_dialog", "unknown_overlay"]

@dataclass(frozen=True)
class BlockerRuleRef:
    rule_id: str
    rule_version: int

@dataclass(frozen=True)
class BlockerCandidateEvidence:
    kind: BlockerKind
    presentation: Literal["popup", "full_page", "native_dialog", "overlay"]
    classification: Literal["recognized", "unknown", "ambiguous"]
    blocking: bool
    control_state: Literal["unique", "missing", "duplicate", "inaccessible", "not_applicable"]
    matched_rules: tuple[BlockerRuleRef, ...]

@dataclass(frozen=True)
class BlockerEvidence:
    """Closed observation summary; never reconstructs a browser target."""
    state: Literal["blocker_observation"]
    status: Literal["clear", "blocked", "indeterminate", "rendering"]
    reason: Literal["none", "recognized_blocker", "unknown_blocker", "ambiguous_match",
                    "unsupported_variant", "incomplete_coverage", "limit_exhausted",
                    "document_changed", "root_changed", "transient_loading", "requirement_not_ready"]
    coverage_complete: bool
    limit_hit: bool
    requirement_ready: bool
    candidates: tuple[BlockerCandidateEvidence, ...]

@dataclass(frozen=True)
class PopupInstanceEvidence:
    """Opaque source identity; never reconstructs a browser target or permission."""
    state: Literal["popup_instance"]
    document_id: str
    instance_id: str | None
    status: Literal["present", "clear"]

@dataclass(frozen=True)
class ConsentTransportEvidence:
    """Closed transport diagnostics; neither private wire nor outcome authority."""
    state: Literal["consent_transport"]
    version: Literal[2]
    request_count: int
    requested_after_return: bool
    acknowledged: bool
    reason: Literal["none", "missing_request", "ambiguous_request", "invalid_request",
        "stale_scope", "invalid_response", "transport_failed", "parse_failed", "site_error",
        "body_limit", "depth_limit", "event_limit", "budget_exhausted", "cancelled", "unqualified"]

INTERRUPTION_CLASS_ORDER = ("human_action_required", "popup_not_yet_supported", "page_unavailable")

InterruptionClass = Literal["human_action_required", "popup_not_yet_supported", "page_unavailable"]

InterruptionCause = Literal["none", "human_only", "missing_choice", "unknown_obstruction",
                            "unsupported_owner", "unsupported_control", "unsupported_response",
                            "conflicting_evidence", "site_availability_error", "navigation_failed",
                            "network_failed", "transient_loading", "requirement_not_ready"]

@dataclass(frozen=True)
class InterruptionRuleRef:
    rule_id: str
    rule_version: int

@dataclass(frozen=True)
class InterruptionConditionEvidence:
    condition_id: int
    kind: Literal["cookie_consent", "captcha", "account_security", "native_dialog", "unknown_overlay", "page_availability"]
    presentation: Literal["popup", "full_page", "native_dialog", "overlay", "inline", "browser_surface"]
    owner: Literal["page_component", "browser_driver"]
    relation: Literal["obstructs", "passive", "unproven"]
    recognition: Literal["supported", "unknown", "conflicting"]
    rule_refs: tuple[InterruptionRuleRef, ...]
    cause_code: InterruptionCause
    source_refs: tuple[EvidenceRef, ...]
    problem_class: InterruptionClass | None

@dataclass(frozen=True)
class InterruptionReport:
    """Descriptive historical proof only; never a target or action permission."""
    state: Literal["interruption_report"]
    requirement_id: str
    requirement_version: int
    phase: Literal["entry", "precheck", "effect", "continuation"]
    requirement_status: Literal["ready", "unready", "indeterminate"]
    coverage_complete: bool
    limit_hit: bool
    conditions: tuple[InterruptionConditionEvidence, ...]
    classes: tuple[InterruptionClass, ...]

@dataclass(frozen=True)
class ConsentInputs:
    choice_id: str
    policy_id: str
    policy_version: int
    rule_id: str
    rule_version: int

@dataclass(frozen=True)
class ConsentOutput:
    choice_id: str
    preference_acknowledged: bool
    panel_removed: bool
    continuation_ready: bool
    remaining_obstruction: Literal["none", "cookie_consent", "captcha", "account_security",
                                   "native_dialog", "unknown_overlay", "indeterminate", "multiple"]

@dataclass(frozen=True)
class RouteInputs:
    route_name: str
    route_version: int
    expected_account_id: str
    entry_url: str
    fallback_url: str | None = None

@dataclass(frozen=True)
class RouteOutput:
    """Saved route decision and discovery hint; never a browser binding."""
    state: Literal["reuse", "new", "opened", "entry", "fallback_allowed"]
    view_id: str | None = None
    generation: str | None = None
    account_id: str | None = None

@dataclass(frozen=True)
class ProviderInputs:
    expected_account_id: str
    challenge_generation: str
    provider_url: str
    minimum_lifetime_ms: int

@dataclass(frozen=True)
class CodeSubmissionInputs:
    expected_account_id: str
    challenge_generation: str
    issuance_id: str

@dataclass(frozen=True)
class ChallengeOutput:
    """Opaque continuity evidence; no live binding or private input."""
    account_id: str
    view_id: str
    generation: str
    document_generation: int
    method: str
    challenge_generation: str
    challenge_id: str
    source_password_operation_id: str | None

@dataclass(frozen=True)
class ProviderOutput:
    state: Literal["opened", "entry", "secret_prepared", "code_generated", "code_available"]
    view_id: str
    generation: str
    document_generation: int
    input_revision: str | None = None
    output_revision: str | None = None
    window_id: str | None = None
    generated_utc_us: int | None = None
    expires_utc_us: int | None = None

@dataclass(frozen=True)
class ProductionChallengeOutput:
    account_id: str
    view_id: str
    generation: str
    document_generation: int
    method: Literal["authenticator", "email"]
    challenge_generation: str
    # A site-issued public identifier, when actually exposed. Runtime continuity
    # is retained separately; an app nonce must never masquerade as a website ID.
    challenge_id: str | None
    source_kind: Literal["cookie_entry", "password_transition"]
    source_operation_id: str
    source_revision: str

@dataclass(frozen=True)
class AuthenticationCompletionOutput:
    account_id: str
    verified_utc_us: int
    child_output_refs: tuple[OutputRef, ...]
    evidence_refs: tuple[EvidenceRef, ...]

@dataclass(frozen=True)
class EmailAccessOutput:
    mailbox_ref: PrivateInputRef
    provider_id: str
    account_id: str
    challenge_ref: OutputRef
    verified_utc_us: int
    evidence_refs: tuple[EvidenceRef, ...]

@dataclass(frozen=True)
class EmailBaselineOutput:
    access_ref: OutputRef
    challenge_ref: OutputRef
    baseline_keys: tuple[str, ...]
    started_utc_us: int
    expires_utc_us: int
    complete: bool
    verified_utc_us: int
    evidence_refs: tuple[EvidenceRef, ...]

@dataclass(frozen=True)
class EmailRequestMetadata:
    baseline_ref: OutputRef
    challenge_ref: OutputRef
    request_operation_id: str
    requested_utc_us: int
    expires_utc_us: int

@dataclass(frozen=True)
class EmailSelectionOutput:
    request_ref: OutputRef
    provider_message_id: str
    internet_message_id: str
    received_utc_us: int
    selected_utc_us: int
    verified_utc_us: int
    evidence_refs: tuple[EvidenceRef, ...]

ResourceKind = Literal["account", "page", "portfolio", "ad_account", "principal", "post"]

@dataclass(frozen=True)
class ResourceIdentity:
    kind: ResourceKind
    resource_id: str
    parent_kind: ResourceKind | None = None
    parent_id: str | None = None

@dataclass(frozen=True)
class KnownPathRecoveryInputs:
    workflow_id: str
    failed_result: ActionResultAvailable

@dataclass(frozen=True)
class ComponentRecoveryInputs:
    workflow_id: str
    failed_result: ActionResultAvailable
    route_id: str
    route_version: int

@dataclass(frozen=True)
class RouteInputsV2:
    namespace: str
    route_name: str
    route_version: int
    expected_account_id: str
    entry_url: str
    fallback_url: str | None = None
    resources: tuple[ResourceIdentity, ...] = ()
    entry_states: tuple[str, ...] = ()
    origin_policy_id: str = "controlled.v1"
    declared_origin: str | None = None
    resource_refs: tuple[OutputRef, ...] = ()

@dataclass(frozen=True)
class ImageUploadInputs:
    asset_ref: PrivateAssetRef

@dataclass(frozen=True)
class BlockCompletionInputs:
    block_id: str
    child_output_refs: tuple[OutputRef, ...]
    successor_block_id: str | None = None

@dataclass(frozen=True)
class BlockCompletionOutput:
    block_id: str
    outputs: tuple[OutputRef, ...]
    successor_block_id: str | None
    verified_utc_us: int
    evidence_refs: tuple[EvidenceRef, ...]

@dataclass(frozen=True)
class CallerContinuation:
    block_id: str
    continuation_id: str
    step_id: str
    occurrence_id: str
    input_schema: str
    input_refs: tuple[OutputRef, ...]
    output_refs: tuple[OutputRef, ...]

@dataclass(frozen=True)
class PrerequisiteRequirement:
    kind: Literal["payment_deferred", "phone_verification", "email_verification"]
    resource: ResourceIdentity
    account_id: str
    source_operation_id: str
    source_evidence_ref: EvidenceRef
    challenge_id: str | None
    caller: CallerContinuation
    verified_utc_us: int

@dataclass(frozen=True)
class PrerequisiteInputs:
    requirement_ref: OutputRef

@dataclass(frozen=True)
class ActivationRef:
    provider_id: str
    activation_id: str

@dataclass(frozen=True)
class ProviderCommandInputs:
    provider_id: str
    command: Literal["acquire", "finish", "cancel"]
    request_id: str
    activation: ActivationRef | None = None
    service_id: str | None = None
    country_id: str | None = None
    max_cost_micros: int | None = None

@dataclass(frozen=True)
class ProviderReceipt:
    status: Literal["not_attempted", "attempted", "returned"]
    provider_id: str
    command: Literal["acquire", "finish", "cancel"]
    request_id: str
    reason: str | None = None
    entered_at: float | None = None
    returned_at: float | None = None
    activation: ActivationRef | None = None
    provider_status: str | None = None
    expires_utc_us: int | None = None
    cost_micros: int | None = None
    currency: Literal["USD"] | None = None

    @property
    def possibly_dispatched(self):
        return self.status != "not_attempted" or self.entered_at is not None or self.returned_at is not None

    def stored(self):
        return self

@dataclass(frozen=True)
class ActivationOutput:
    activation: ActivationRef
    request_id: str
    status: Literal["active", "finished", "cancelled", "expired"]
    verified_utc_us: int
    evidence_refs: tuple[EvidenceRef, ...]
    expires_utc_us: int | None = None
    cost_micros: int | None = None
    currency: Literal["USD"] | None = None

@dataclass(frozen=True)
class OperationIntent:
    operation_id: str
    workflow_id: str
    definition_id: str
    definition_version: int
    step_id: str
    occurrence_id: str
    scope: ExpectedScope
    effect_kind: EffectKind
    semantic_target: str
    input_schema: str
    permitted_inputs: PermittedInputs
    private_inputs: tuple[PrivateInputRef, ...]
    root_operation_id: str
    corrective_parent_id: str | None
    policy_id: str
    dialog_policy: DialogPolicy | None = None

@dataclass(frozen=True)
class AdmissionPolicy:
    policy_id: str
    max_attempts: int = 2
    max_effect_intents: int = 1
    max_recovery_admissions: int = 2
    replay_policy_id: str | None = None

@dataclass(frozen=True)
class Ready:
    step_id: str
    occurrence_id: str

@dataclass(frozen=True)
class InAction:
    operation_id: str
    attempt_id: str

@dataclass(frozen=True)
class ActionResultAvailable:
    operation_id: str
    attempt_id: str
    result_event_id: str

@dataclass(frozen=True)
class Finished:
    result_event_id: str | None = None

WorkflowPosition = Ready | InAction | ActionResultAvailable | Finished

@dataclass(frozen=True)
class RecoveryFrame:
    frame_id: str
    root_operation_id: str
    rule_id: str
    return_position: WorkflowPosition
    position: WorkflowPosition
    continuation_id: str
    episode_id: str
    episode_epoch: str
    episode_kind: RecoveryKind
    phase: Literal["running", "verifying", "awaiting_resume", "blocked"] = "running"
    wait_expires_utc_us: int = 0

@dataclass(frozen=True)
class RetryAdmission:
    operation_id: str
    episode_event_id: str
    scope_key: str

@dataclass(frozen=True)
class OutputRef:
    operation_id: str
    result_event_id: str
    output_key: str
    output_schema: str | None = None

@dataclass(frozen=True)
class CompletedRunReference:
    """Exact Finished bank/final receipt and consumed business outputs.

resource_refs is either one Observed/SavedBusinessResourcesOutput or the three
Created/VerifiedPortfolioOutput, Created/VerifiedAdAccountOutput and AdministratorAccessOutput refs.
page_ref optionally supplies the original SelectedPage/CurrentPage output when
resource_refs do not contain the Page ID. Saved resources remain historical proof.
For manual_step_5_post, the final completion also contains the verified SelectedPost.
"""
    workflow_id: str
    revision: int
    completion_ref: OutputRef
    resource_refs: tuple[OutputRef, ...]
    page_ref: OutputRef | None = None
    endpoint: Literal['manual_step_4', 'manual_step_5_post'] = 'manual_step_4'

@dataclass(frozen=True)
class EvidenceRef:
    event_id: str

@dataclass(frozen=True, kw_only=True)
class WorkflowCheckpoint:
    workflow_id: str
    definition_id: str
    definition_version: int
    revision: int
    scope: ExpectedScope
    status: Literal["running", "suspended", "finished", "legacy_blocked"]
    main: WorkflowPosition
    recovery: RecoveryFrame | None = None
    retry_admission: RetryAdmission | None = None
    pending_operation_ids: tuple[str, ...] = ()
    output_refs: tuple[OutputRef, ...] = ()
    time_scopes: tuple[SavedTimeScope, ...] = ()
    stop_reason: StopReason | None = None
    utc_high_water_us: int = 0
    recovery_policy_version: Literal[1, 2] = 1
    reconnect_attempts: int = 0

@dataclass(frozen=True)
class ActionTicket:
    workflow_id: str
    operation_id: str
    attempt_id: str
    lane: Literal["main", "recovery"]
    frame_id: str | None
    admitted_revision: int
    scope_key: str
    limits: SavedTimeLimits
    effect_intent_event_id: str | None = None

@dataclass(frozen=True)
class CorrelatedEvidence:
    observer_id: str
    observer_version: int
    scope: ExpectedScope
    observed_utc_us: int
    operation_id: str
    attempt_ids: tuple[str, ...]
    correlation_kind: Literal["bound_transition", "server_operation_id", "resource_identity", "definitive_noncompletion", "website_idempotency"]
    details: ObservationDetails | BlockerEvidence | InterruptionReport | PopupInstanceEvidence | ConsentTransportEvidence

@dataclass(frozen=True)
class EvidenceSubmission:
    event_id: str
    evidence: CorrelatedEvidence

@dataclass(frozen=True)
class FactProvenance:
    observer_id: str
    observer_version: int
    run_id: str
    logic_id: str
    page_id: str
    step_id: str
    profile_id: str

@dataclass(frozen=True)
class StateFact:
    fact_key: str
    scope_key: str
    account_key: str
    value_schema: str
    value: FactValue
    verified_utc_us: int
    source_operation_id: str
    evidence_event_id: str
    provenance: FactProvenance

@dataclass(frozen=True)
class DialogReceipt:
    type: Literal["alert", "confirm", "prompt", "beforeunload", "unknown"]
    expected: bool
    response: Literal["accept", "dismiss", "unresolved"]
    entered_at: float | None = None
    returned_at: float | None = None
    reason: str | None = None

@dataclass(frozen=True)
class StoredDispatchReceipt:
    status: Literal["not_attempted", "attempted", "returned"]
    reason: str | None = None
    entered_at: float | None = None
    returned_at: float | None = None
    phase: str = "preparation"
    activation_entered: bool = False
    activation_returned: bool = False
    cleanup_attempted: bool = False
    cleanup_returned: bool = False
    cleanup_reason: str | None = None
    dialogs: tuple[DialogReceipt, ...] = ()
    dialog_count: int = 0
    view_closed: bool | None = None

    @property
    def possibly_dispatched(self) -> bool:
        return (self.status != "not_attempted" or self.entered_at is not None
                or self.returned_at is not None or self.activation_entered
                or self.activation_returned or self.cleanup_attempted or self.cleanup_returned
                or any(d.entered_at is not None or d.returned_at is not None for d in self.dialogs))

@dataclass(frozen=True, kw_only=True)
class StoredActionResult:
    operation_id: str
    attempt_id: str
    outcome: Outcome
    stop_reason: StopReason | None = None
    output: SafeOutput | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    receipt: StoredDispatchReceipt | ProviderReceipt | None = None

@dataclass(frozen=True)
class CompletionEnvelope:
    ticket: ActionTicket
    result: StoredActionResult
    facts: tuple[StateFact, ...]
    evidence: tuple[EvidenceSubmission, ...]

@dataclass(frozen=True)
class PendingOperation:
    operation_id: str
    workflow_id: str
    attempt_id: str | None
    root_operation_id: str
    outcome: Outcome
    unresolved_attempt_ids: tuple[str, ...] = ()
    legacy: bool = False

@dataclass(frozen=True)
class StateSnapshot:
    facts: tuple[StateFact, ...] = ()

@dataclass(frozen=True)
class LegacyEvidence:
    source_id: str
    source_kind: Literal["sqlite_v1", "verification_journal"]
    source_schema: int
    source_record_id: str
    account_key: str
    source_path: str
    source_identity: tuple[int, int, int, int]
    profile_id: str | None = None
    workflow_id: str | None = None
    operation_id: str | None = None
    attempt_id: str | None = None
    action_id: str | None = None
    phase: Literal["reserved", "intent", "completed", "not_started", "callback_started", "callback_completed", "verified", "unknown", "request_intent", "awaiting_message", "submit_intent", "outcome"] | None = None
    outcome: Literal["confirmed", "rejected", "unknown", "not_run", "needs_input"] | None = None
    verified_utc_us: int | None = None
    timing_scope_key: str | None = None
    normal_until: float | None = None
    completion_until: float | None = None

@dataclass(frozen=True)
class LegacyResolutionEvidence:
    source_id: str
    source_record_id: str
    scope: ExpectedScope
    observer_id: str
    observer_version: int
    observed_utc_us: int
    conclusion: Literal["confirmed_existing", "definitive_noncompletion", "historical_metadata_reviewed"]
    correlation_kind: Literal["server_operation_id", "resource_identity", "definitive_noncompletion", "source_metadata"]
    correlation_id: str

@dataclass(frozen=True)
class StepDefinition:
    step_id: str
    effect_kind: EffectKind
    policy: AdmissionPolicy
    normal_next: tuple[str, ...] = ()
    restore_allowed: bool = False

@dataclass(frozen=True)
class Continuation:
    continuation_id: str
    target_step_id: str
    restore_step_ids: tuple[str, ...] = ()
    required_output_keys: tuple[str, ...] = ()
    retry_original: bool = False

@dataclass(frozen=True)
class RecoveryRuleDefinition:
    rule_id: str
    priority: int
    kind: RecoveryKind
    branch_id: str | None
    continuation_id: str

@dataclass(frozen=True)
class InputBinding:
    input_key: str
    producer_block_id: str
    producer_step_id: str
    output_key: str
    output_schema: str
    parent_input_key: str | None = None

@dataclass(frozen=True)
class OutputBinding:
    output_key: str
    output_schema: str
    producer_step_id: str

@dataclass(frozen=True)
class BlockDefinition:
    block_id: str
    entry_step_id: str
    completion_step_id: str
    step_ids: tuple[str, ...]
    input_schema: str
    input_bindings: tuple[InputBinding, ...] = ()
    output_bindings: tuple[OutputBinding, ...] = ()
    allowed_successor_block_ids: tuple[str, ...] = ()
    final: bool = False

@dataclass(frozen=True)
class WorkflowDefinitionRecord:
    definition_id: str
    version: int
    steps: tuple[StepDefinition, ...]
    continuations: tuple[Continuation, ...]
    recovery_rules: tuple[RecoveryRuleDefinition, ...]
    blocks: tuple[BlockDefinition, ...] = ()

@dataclass(frozen=True)
class RecoveryRule:
    rule_id: str
    priority: int
    kind: RecoveryKind
    matches: Callable[[RecoverySnapshot], bool] = field(repr=False)
    branch_id: str | None
    verify: Callable[[RecoveryEvidence], bool] = field(repr=False)
    continuation_id: str

@dataclass(frozen=True)
class WorkflowDefinition:
    definition_id: str
    version: int
    steps: tuple[StepDefinition, ...]
    continuations: tuple[Continuation, ...] = ()
    recovery_rules: tuple[RecoveryRule, ...] = ()
    blocks: tuple[BlockDefinition, ...] = ()

    @property
    def entry_step_id(self) -> str:
        if not self.steps:
            raise ValueError("A workflow requires an entry step.")
        return self.steps[0].step_id

@dataclass(frozen=True)
class RecoverySnapshot:
    checkpoint: WorkflowCheckpoint
    pending_operations: tuple[PendingOperation, ...] = ()
    stored_results: tuple[StoredActionResult, ...] = ()
    observations: tuple[CorrelatedEvidence, ...] = ()

@dataclass(frozen=True)
class RecoveryEvidence:
    scope: ExpectedScope
    observations: tuple[CorrelatedEvidence, ...]
    correction_confirmed: bool
    original_resolved: bool
    prerequisites_satisfied: bool
    replay_safe: bool = False

def operation_id_for(scope: ExpectedScope, workflow_id: str, step_id: str, occurrence_id: str) -> str:
    key = json.dumps((scope.account_key, workflow_id, step_id, occurrence_id), separators=(",", ":"))
    return str(uuid5(NAMESPACE_URL, "automation-operation:" + key))

def definition_record(definition: WorkflowDefinition) -> WorkflowDefinitionRecord:
    return WorkflowDefinitionRecord(definition.definition_id, definition.version, definition.steps,
        definition.continuations, tuple(RecoveryRuleDefinition(r.rule_id, r.priority, r.kind,
        r.branch_id, r.continuation_id) for r in definition.recovery_rules), definition.blocks)

@dataclass(frozen=True)
class ViewRef:
    view_id: str
    generation: str

@dataclass(frozen=True)
class PageBinding:
    view: ViewRef
    component_id: str
    origin: str

@dataclass(frozen=True)
class TargetRef:
    view: ViewRef
    selector: str

class SensitiveValue:
    __slots__ = ("__value",)

    def __init__(self, value: str):
        if not isinstance(value, str):
            raise TypeError("Sensitive input must be text.")
        self.__value = value

    def reveal(self) -> str:
        return self.__value

    def clear(self) -> None:
        self.__value = ""

    def __repr__(self) -> str:
        return "SensitiveValue(<redacted>)"

    def __reduce__(self):
        raise TypeError("Sensitive input cannot be serialized.")

@dataclass(frozen=True)
class ResolvedPrivateInput:
    reference: PrivateInputRef
    value: SensitiveValue = field(repr=False)

@dataclass(frozen=True)
class DialogPolicy:
    policy_id: str
    dialog_type: Literal["alert", "confirm", "prompt", "beforeunload"]
    matcher_id: str
    response: Literal["accept", "dismiss"]
    prompt_ref: PrivateInputRef | None = None
    max_count: int = 1
    expect_dialog: bool = True
    cancel_response: Literal["dismiss"] = "dismiss"

@dataclass(frozen=True)
class PreparedDialogPolicy:
    policy: DialogPolicy
    view: ViewRef
    matches_message: Callable[[str], bool] = field(repr=False)
    require_acceptance: Callable[[], None] = field(repr=False)
    prompt_value: SensitiveValue | None = field(default=None, repr=False)

@dataclass(frozen=True)
class ActionRequest:
    kind: Literal["click", "move", "hover", "scroll", "fill", "selectOption", "setChecked", "uploadImage", "setSlider", "navigate", "openTab", "activateTab", "closeOwnedTab", "closeTab"]
    target: TargetRef | None = None
    value: SensitiveValue | None = field(default=None, repr=False)
    url: str | None = field(default=None, repr=False)
    dialog: PreparedDialogPolicy | None = field(default=None, repr=False)
    run_before_unload: bool = False
    movement: MovementOptions | None = None
    scroll: ScrollIntent | None = None
    option_value: str | None = None
    checked: bool | None = None
    asset: ResolvedPrivateAsset | None = field(default=None, repr=False)
    slider_fraction: float | None = None
    slider_track: TargetRef | None = None

@dataclass(frozen=True)
class DispatchReceipt:
    status: str
    reason: str | None = None
    entered_at: float | None = None
    returned_at: float | None = None
    phase: str = "preparation"
    created_view: ViewRef | None = None
    activation_entered: bool = False
    activation_returned: bool = False
    cleanup_attempted: bool = False
    cleanup_returned: bool = False
    cleanup_reason: str | None = None
    dialogs: tuple[DialogReceipt, ...] = ()
    dialog_count: int = 0
    view_closed: bool | None = None
    # Geometry and trajectories are runtime evidence, never durable projections.
    movement: MovementTrace | None = field(default=None, repr=False)
    scroll: tuple[ScrollLayout, ...] = field(default=(), repr=False)

    @property
    def possibly_dispatched(self) -> bool:
        return (self.status != "not_attempted" or self.entered_at is not None
                or self.returned_at is not None or self.activation_entered
                or self.activation_returned or self.cleanup_attempted or self.cleanup_returned
                or any(d.entered_at is not None or d.returned_at is not None for d in self.dialogs))

    def stored(self) -> StoredDispatchReceipt:
        return StoredDispatchReceipt(**{name: getattr(self, name)
            for name in StoredDispatchReceipt.__dataclass_fields__})

@dataclass(frozen=True)
class ActionExecutionScope:
    action_id: str
    operation_id: str
    attempt_id: str
    parent_workflow_operation_id: str
    timing_scope_key: str | None = None
    normal_until: float | None = None
    completion_until: float | None = None

@dataclass(frozen=True)
class AuthObservation:
    state: str
    view: ViewRef
    observed_at: float
    account_id: str | None = None
    protected: bool = False

@dataclass(frozen=True)
class ChallengeBinding:
    binding: PageBinding
    expected_account_id: str
    source_password_operation_id: str | None
    method: str
    challenge_generation: str
    document_generation: int
    observed_at: float
    challenge_id: str

@dataclass(frozen=True)
class CodeRequest:
    challenge: ChallengeBinding
    authenticator_secret_ref: PrivateInputRef
    deadline: float
    minimum_lifetime_s: float

@dataclass(frozen=True)
class CodeEnvelope:
    value: SensitiveValue = field(repr=False)
    source_origin: str
    observed_at: float
    expires_at: float
    clock_epoch_id: str
    challenge_generation: str
    private_input_ref: PrivateInputRef
    provider_view: ViewRef
    provider_document_generation: int
    input_generation: str
    window_id: str

@dataclass(frozen=True)
class CodeResult:
    status: Literal["available", "unavailable", "needs_input"]
    envelope: CodeEnvelope | None = field(default=None, repr=False)
    reason: StopReason | None = None
    stages: tuple[StageResult, ...] = ()

T = TypeVar("T")

@dataclass(frozen=True)
class Evidence(Generic[T]):
    outcome: Outcome
    output: T | None = field(default=None, repr=False)
    observations: tuple[AuthObservation, ...] = ()
    facts: tuple[StateFact, ...] = ()
    stop_reason: StopReason | None = None
    evidence: tuple[EvidenceSubmission, ...] = ()

@dataclass(frozen=True, kw_only=True)
class ActionResult(Generic[T]):
    outcome: Outcome
    operation_id: str
    attempt_id: str | None = None
    stop_reason: StopReason | None = None
    output: T | None = field(default=None, repr=False)
    observations: tuple[AuthObservation, ...] = ()
    receipt: DispatchReceipt | ProviderReceipt | None = None
    recording: Recording = Recording.NOT_REQUIRED
    result_event_id: str | None = None

    @property
    def can_advance(self) -> bool:
        return self.outcome == Outcome.CONFIRMED and self.stop_reason is None and self.recording == Recording.SAVED

@dataclass(frozen=True, kw_only=True)
class StageResult(Generic[T]):
    outcome: Outcome
    stop_reason: StopReason | None = None
    recording: Recording = Recording.NOT_REQUIRED
    output: T | None = field(default=None, repr=False)
    actions: tuple[ActionResult, ...] = ()
    last_confirmed_action: str | None = None
    pending_operation_ids: tuple[str, ...] = ()

@dataclass(frozen=True, kw_only=True)
class BlockResult(Generic[T]):
    outcome: Outcome
    stop_reason: StopReason | None = None
    recording: Recording = Recording.NOT_REQUIRED
    output: T | None = field(default=None, repr=False)
    stages: tuple[StageResult, ...] = ()
    last_confirmed_stage: str | None = None
    pending_operation_ids: tuple[str, ...] = ()
    problem_reports: tuple = ()

@dataclass(frozen=True, kw_only=True)
class RunResult:
    outcome: Outcome
    stop_reason: StopReason | None = None
    recording: Recording = Recording.NOT_REQUIRED
    blocks: tuple[BlockResult, ...] = ()
    last_confirmed_block: str | None = None
    pending_operation_ids: tuple[str, ...] = ()
    problem_reports: tuple = ()

@dataclass(frozen=True, kw_only=True)
class NavigationResult:
    outcome: Outcome
    binding: PageBinding | None = None
    stop_reason: StopReason | None = None
    stages: tuple[StageResult, ...] = ()
    recording: Recording = Recording.NOT_REQUIRED
    assessment: object | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ProviderCommandRequest:
    inputs: ProviderCommandInputs
    authorization_ref: PrivateInputRef | None = field(default=None, repr=False)
    private_value: SensitiveValue | None = field(default=None, repr=False)

@dataclass(frozen=True)
class AuthenticatedAccount:
    verified_account_id: str
    binding: PageBinding | None
    verified_at: float

class PermittedInputs:
    """A registered action input record (codec marker)."""

class SafeOutput:
    """A registered action output record (codec marker)."""

class FactValue:
    """A registered durable fact value (codec marker)."""

INTERRUPTION_RULE_MANIFEST = ()

INTERRUPTION_REQUIREMENT_MANIFEST = ()

CONSENT_CHOICE_MANIFEST = ()

CONSENT_ACKNOWLEDGEMENT_MANIFEST = ()

BLOCKER_POLICY_MANIFEST = ()

BLOCKER_RULE_MANIFEST = ()

CONSENT_RULE_MANIFEST = ()
