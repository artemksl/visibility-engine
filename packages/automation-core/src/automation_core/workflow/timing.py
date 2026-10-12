"""Cooperative monotonic scopes. No timers, scheduling, or browser ownership."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
import json
import math
import threading
import time
from typing import Callable, Protocol, TypeVar, TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from automation_core.workflow.runtime import RunContext


_PROCESS_EPOCH = uuid4().hex

# Result observation only: these never authorize another input or extend a
# saved action/workflow deadline. Special provider waits keep their own limits.
CONTROL_RESULT_S = 10.
DIALOG_RESULT_S = 20.
PAGE_RESULT_S = 60.
CREATION_RESULT_S = 120.
RESULT_RETRY_MULTIPLIER = 1.5


class Clock(Protocol):
    epoch_id: str
    def monotonic(self) -> float: ...
    def utc_now(self) -> datetime: ...


class SystemClock:
    epoch_id = _PROCESS_EPOCH

    def monotonic(self) -> float:
        return time.monotonic()

    def utc_now(self) -> datetime:
        return datetime.now(timezone.utc)


class BudgetAdmissionDenied(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _number(value: float, *, positive: bool = False) -> None:
    try:
        valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                 and math.isfinite(value) and value >= 0 and (not positive or value >= .001))
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("Timing values must be finite nonnegative seconds.")


@dataclass(frozen=True)
class BudgetSpec:
    allowance_s: float
    normal_fraction: float = .70

    def __post_init__(self):
        _number(self.allowance_s, positive=True)
        _number(self.normal_fraction)
        if not 0 < self.normal_fraction < 1:
            raise ValueError("A wrapped scope must reserve normal and completion time.")


@lru_cache(maxsize=4096)
def _scope_token(account_key, workflow_operation_id, component_path, occurrence_id):
    # This is only a deterministic identifier. Clock readings and limits are
    # deliberately absent; every actual deadline check still reads the clock.
    return sha256(json.dumps((account_key, workflow_operation_id,
                             component_path, occurrence_id)).encode()).hexdigest()


@dataclass(frozen=True)
class ScopeKey:
    account_key: str
    workflow_operation_id: str
    component_path: tuple[str, ...]
    occurrence_id: str = "0"

    def __post_init__(self):
        parts = (self.account_key, self.workflow_operation_id, *self.component_path, self.occurrence_id)
        if not self.component_path or any(not isinstance(p, str) or not p or len(p) > 256 for p in parts):
            raise ValueError("A scope needs bounded stable identifiers.")

    def token(self) -> str:
        try:
            return _scope_token(self.account_key, self.workflow_operation_id,
                                self.component_path, self.occurrence_id)
        except TypeError:
            # Preserve the existing representation for unhashable callers.
            return sha256(json.dumps((self.account_key, self.workflow_operation_id,
                                     self.component_path, self.occurrence_id)).encode()).hexdigest()


@dataclass(frozen=True)
class ScopeStart:
    event_id: str
    clock_epoch_id: str
    monotonic_at: float


@dataclass(frozen=True)
class DeadlineBudget:
    scope_key: ScopeKey
    clock_epoch_id: str
    started_at: float
    normal_until: float
    completion_until: float
    ancestor_keys: tuple[ScopeKey, ...] = ()
    completion_only: bool = False

    def remaining_normal(self, now: float) -> float:
        return max(0., self.normal_until - now)

    def remaining_completion(self, now: float) -> float:
        return max(0., self.completion_until - now)

    def clipped(self, normal_until: float, completion_until: float):
        hard = min(self.completion_until, completion_until)
        return replace(self, normal_until=min(self.normal_until, normal_until, hard),
                       completion_until=hard)


@dataclass(frozen=True)
class ActionTiming:
    prepare_s: float
    dispatch_s: float
    observe_s: float
    record_s: float
    cleanup_s: float

    def __post_init__(self):
        for value in self.__dict__.values():
            _number(value)
        _number(self.planned_cost(), positive=True)

    def planned_cost(self) -> float:
        return sum(self.__dict__.values())


@dataclass(frozen=True)
class TimePlan:
    kind: str
    children: tuple["TimePlan", ...] = ()
    action: ActionTiming | None = None
    allocation: BudgetSpec | None = None
    overhead_s: float = 0.

    def __post_init__(self):
        _number(self.overhead_s)
        if self.kind == "action":
            valid = self.action is not None and not self.children and self.allocation is None
        elif self.kind in {"sequence", "choice"}:
            valid = self.action is None and self.allocation is None
        elif self.kind == "wrapped":
            valid = self.action is None and self.allocation is not None and len(self.children) == 1
            if valid and self.children[0].cost_s() + self.overhead_s > self.allocation.allowance_s * self.allocation.normal_fraction:
                raise ValueError("Planned work exceeds a wrapped scope's normal allowance.")
        else:
            valid = False
        if not valid:
            raise ValueError("Invalid time-plan shape.")
        _number(self.cost_s())

    def cost_s(self) -> float:
        if self.kind == "wrapped":
            return self.allocation.allowance_s
        if self.kind == "action":
            return self.overhead_s + self.action.planned_cost()
        costs = [child.cost_s() for child in self.children]
        return self.overhead_s + (max(costs, default=0.) if self.kind == "choice" else sum(costs))

    def fingerprint(self) -> str:
        # Only declared costs and topology; no input values or runtime references.
        return sha256(repr(self).encode()).hexdigest()


@dataclass(frozen=True)
class ActionWindow:
    operation_id: str
    input_until: float
    normal_observe_until: float
    completion_observe_until: float
    record_until: float
    cleanup_until: float

    @classmethod
    def allocate(cls, budget: DeadlineBudget, timing: ActionTiming, operation_id: str,
                 submission_cutoff: float | None = None):
        normal_obs = budget.normal_until - timing.record_s - timing.cleanup_s
        input_until = normal_obs - timing.observe_s
        if submission_cutoff is not None:
            # A long-expired UTC input can map before monotonic zero. It is an
            # expired absolute cutoff, not an invalid negative duration.
            try:
                valid = (not isinstance(submission_cutoff, bool)
                         and isinstance(submission_cutoff, (int, float))
                         and math.isfinite(submission_cutoff))
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError("Input cutoffs must be finite timestamps.")
            input_until = min(input_until, submission_cutoff)
        return cls(operation_id, input_until, normal_obs,
                   budget.completion_until - timing.record_s - timing.cleanup_s,
                   budget.completion_until - timing.cleanup_s, budget.completion_until)


@dataclass(frozen=True)
class WorkPermission:
    mode: str = "normal"
    operation_id: str | None = None
    frame_id: str | None = None

    def __post_init__(self):
        if (self.mode not in {"normal", "recovery", "completion"}
                or (self.mode == "completion" and not self.operation_id)
                or (self.mode == "recovery" and not self.frame_id)):
            raise ValueError("Completion permission must identify an operation.")


@dataclass(frozen=True)
class RestartClockPolicy:
    trusted_host_utc: bool = False
    uncertainty_s: float = .25

    def __post_init__(self):
        if type(self.trusted_host_utc) is not bool:
            raise ValueError("Clock trust must be explicit.")
        _number(self.uncertainty_s)


@dataclass(frozen=True)
class SavedTimeLimits:
    normal_expires_utc_us: int
    completion_expires_utc_us: int
    checkpoint_utc_us: int
    normal_remaining_cap_s: float
    completion_remaining_cap_s: float
    completion_only: bool

    def __post_init__(self):
        for value in (self.normal_expires_utc_us, self.completion_expires_utc_us, self.checkpoint_utc_us):
            if type(value) is not int:
                raise ValueError("Persisted UTC timestamps must be integer microseconds.")
        _number(self.normal_remaining_cap_s)
        _number(self.completion_remaining_cap_s)
        if (self.normal_expires_utc_us > self.completion_expires_utc_us
                or self.normal_remaining_cap_s > self.completion_remaining_cap_s
                or type(self.completion_only) is not bool):
            raise ValueError("Invalid saved timing bounds.")


@dataclass(frozen=True)
class SavedTimeScope:
    scope_key: ScopeKey
    parent_scope_key: str | None
    plan_fingerprint: str
    allowance_s: float
    normal_fraction: float
    start_event_id: str
    activated_utc_us: int
    clock_epoch: str
    limits: SavedTimeLimits

    def __post_init__(self):
        if not isinstance(self.scope_key, ScopeKey) or not isinstance(self.limits, SavedTimeLimits):
            raise ValueError("A saved scope requires typed identity and limits.")
        _number(self.allowance_s, positive=True)
        _number(self.normal_fraction)
        if (not 0 < self.normal_fraction <= 1 or type(self.activated_utc_us) is not int
                or not all(isinstance(v, str) and v for v in
                           (self.plan_fingerprint, self.start_event_id, self.clock_epoch))
                or (self.parent_scope_key is not None and not isinstance(self.parent_scope_key, str))):
            raise ValueError("Invalid saved timing scope.")


@dataclass(frozen=True)
class TimeCheckpoint:
    clock_epoch: str
    sampled_utc_us: int
    utc_high_water_us: int
    scopes: tuple[SavedTimeScope, ...]
    action_limits: tuple[tuple[str, SavedTimeLimits], ...]

    def __post_init__(self):
        if (not isinstance(self.clock_epoch, str) or not self.clock_epoch
                or type(self.sampled_utc_us) is not int or type(self.utc_high_water_us) is not int
                or self.utc_high_water_us < self.sampled_utc_us
                or type(self.scopes) is not tuple or type(self.action_limits) is not tuple
                or any(not isinstance(s, SavedTimeScope) for s in self.scopes)
                or len({s.scope_key.token() for s in self.scopes}) != len(self.scopes)):
            raise ValueError("Invalid timing checkpoint.")
        ids = []
        for entry in self.action_limits:
            if (type(entry) is not tuple or len(entry) != 2 or not isinstance(entry[0], str)
                    or not entry[0] or not isinstance(entry[1], SavedTimeLimits)):
                raise ValueError("Invalid action timing checkpoint.")
            ids.append(entry[0])
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate action timing checkpoint.")


def validate_limits_tightening(previous: SavedTimeLimits, new: SavedTimeLimits) -> None:
    if (new.normal_expires_utc_us > previous.normal_expires_utc_us
            or new.completion_expires_utc_us > previous.completion_expires_utc_us
            or new.normal_remaining_cap_s > previous.normal_remaining_cap_s
            or new.completion_remaining_cap_s > previous.completion_remaining_cap_s
            or (previous.completion_only and not new.completion_only)):
        raise ValueError("Saved timing limits cannot renew.")


def validate_scope_tightening(previous: SavedTimeScope, new: SavedTimeScope) -> None:
    for name in ("scope_key", "parent_scope_key", "plan_fingerprint", "allowance_s",
                 "normal_fraction", "start_event_id", "activated_utc_us"):
        if getattr(previous, name) != getattr(new, name):
            raise ValueError("Timing activation identity cannot change.")
    validate_limits_tightening(previous.limits, new.limits)


@dataclass(frozen=True)
class TimingDiagnostic:
    scope_id: str
    elapsed_s: float
    normal_remaining_s: float
    completion_remaining_s: float
    completion_only: bool
    overrun_s: float


class ScopeRegistry:
    """Workflow-bound scopes; all acknowledgments update one shared checkpoint."""

    def __init__(self, clock: Clock, memory, checkpoint, *, policy=RestartClockPolicy()):
        self.clock, self.memory, self.checkpoint_ref = clock, memory, checkpoint
        self.policy = policy
        self.records: dict[str, SavedTimeScope] = {}
        self.budgets: dict[str, DeadlineBudget] = {}
        self._acknowledged_bounds = {}
        self.active: set[str] = set()
        self._thread = threading.get_ident()
        self._last_now = -math.inf
        self._anchor_baseline = None
        self._clock_untrusted = False
        self._high_water = getattr(checkpoint.current, "utc_high_water_us", 0)
        self._sync_records()

    def _sync_records(self):
        for saved in self.checkpoint_ref.current.time_scopes:
            if not isinstance(saved, SavedTimeScope):
                raise BudgetAdmissionDenied("clock_untrusted")
            token = saved.scope_key.token()
            old = self.records.get(token)
            if old is not None and old.limits.completion_only and not saved.limits.completion_only:
                saved = replace(saved, limits=replace(saved.limits, completion_only=True))
            self.records[token] = saved
        self._high_water = max(self._high_water, getattr(self.checkpoint_ref.current, "utc_high_water_us", 0))

    def now(self) -> float:
        if threading.get_ident() != self._thread:
            raise BudgetAdmissionDenied("wrong_owner_thread")
        now = self.clock.monotonic()
        _number(now)
        if now < self._last_now:
            raise BudgetAdmissionDenied("clock_reversed")
        self._last_now = now
        return now

    def _anchor(self):
        self.now()
        anchor = capture_expiry_anchor(self.clock)
        utc = utc_microseconds(anchor.utc_at)
        if utc + math.floor(self.policy.uncertainty_s * 1_000_000) < self._high_water:
            self._clock_untrusted = True
        prior = self._anchor_baseline
        if prior is not None:
            wall_delta = (anchor.utc_at - prior.utc_at).total_seconds()
            lo = anchor.monotonic_before - prior.monotonic_after - self.policy.uncertainty_s
            hi = anchor.monotonic_after - prior.monotonic_before + self.policy.uncertainty_s
            if not lo - 1e-6 <= wall_delta <= hi + 1e-6:
                self._clock_untrusted = True
        if self._clock_untrusted:
            raise BudgetAdmissionDenied("clock_untrusted")
        if prior is None:
            self._anchor_baseline = anchor
        self._high_water = max(self._high_water, utc)
        return anchor

    def _read(self, key: ScopeKey):
        self._sync_records()
        return self.records.get(key.token())

    def _restore_all(self, anchor):
        self._sync_records()
        remaining = {k: v for k, v in self.records.items() if k not in self.budgets}
        while remaining:
            progressed = False
            for token, saved in tuple(remaining.items()):
                parent_key = saved.parent_scope_key
                if parent_key is not None and parent_key not in self.budgets:
                    continue
                self.budgets[token] = restore_limits(saved, anchor=anchor,
                    parent=self.budgets.get(parent_key), clock_epoch_id=self.clock.epoch_id,
                    policy=self.policy)
                self._acknowledged_bounds[token] = self._bounds(self.budgets[token])
                del remaining[token]
                progressed = True
            if not progressed:
                raise BudgetAdmissionDenied("scope_identity_mismatch")

    def _checkpoint_at(self, anchor, action_limits=(), *, include=None):
        self._restore_all(anchor)
        records = dict(self.records)
        if include is not None:
            records[include.scope_key.token()] = include
        pending = {}
        for token, saved in records.items():
            budget = self.budgets[token]
            limits = snapshot_limits(budget, anchor, saved.limits, self.policy)
            pending[token] = replace(saved, clock_epoch=self.clock.epoch_id, limits=limits)
        scopes = {}
        while pending:
            progressed = False
            for token, saved in tuple(pending.items()):
                parent = saved.parent_scope_key
                if parent is not None:
                    if parent not in scopes:
                        continue
                    saved = replace(saved, limits=_tighten_saved_limits(saved.limits, scopes[parent].limits))
                scopes[token] = saved
                del pending[token]
                progressed = True
            if not progressed:
                raise BudgetAdmissionDenied("scope_identity_mismatch")
        return TimeCheckpoint(self.clock.epoch_id, utc_microseconds(anchor.utc_at),
                              self._high_water, tuple(scopes.values()), tuple(action_limits))

    def time_checkpoint(self, action_limits=(), *, include=None):
        """Prepare persistence data only; no transaction or browser work occurs."""
        return self._checkpoint_at(self._anchor(), action_limits, include=include)

    def checkpoint(self, action_budgets=()):
        # Read acknowledged action bounds before the shared clock sample. In
        # particular, an exhausted restored slot may use `now` as its monotonic
        # cutoff, but must retain its original (earlier) durable UTC expiry.
        actions = tuple((op, budget, self.memory.operation(op)) for op, budget in action_budgets)
        anchor = self._anchor()
        checkpoint = self._checkpoint_at(anchor)
        scopes = {saved.scope_key.token(): saved.limits for saved in checkpoint.scopes}
        limits = []
        for op, budget, record in actions:
            parent = scopes.get(budget.scope_key.token())
            if parent is None:
                raise BudgetAdmissionDenied("unregistered_scope")
            saved = snapshot_limits(budget, anchor, record.limits if record else None, self.policy)
            # Persisted scope bounds may already be tighter after conservative
            # UTC rounding. An action must also respect those exact bounds.
            limits.append((op, _tighten_saved_limits(saved, parent)))
        return replace(checkpoint, action_limits=tuple(limits))

    def snapshot_action(self, budget, previous=None):
        return snapshot_limits(budget, self._anchor(), previous, self.policy)

    def restore_action(self, saved, scope):
        return restore_action_limits(saved, scope=scope, anchor=self._anchor(), policy=self.policy)

    def budget_for(self, scope_key_token: str) -> DeadlineBudget:
        """Restore a registered scope and its parents; never allocate replacement time."""
        self._restore_all(self._anchor())
        saved = self.records.get(scope_key_token)
        if saved is None:
            raise BudgetAdmissionDenied("unregistered_scope")
        self._write(saved)
        return self.budgets[scope_key_token]

    def _write(self, saved):
        token = saved.scope_key.token()
        bounds = self._bounds(self.budgets[token])
        if self._acknowledged_bounds.get(token) == bounds:
            # A deadline is absolute. Reading/re-entering it does not need a
            # database write just to save a smaller remaining-time counter.
            return self.records[token]
        at = self.time_checkpoint(include=saved)
        saved = next(s for s in at.scopes if s.scope_key == saved.scope_key)
        cp = self.memory.activate_time_scope(saved.scope_key.workflow_operation_id, saved,
            expected_revision=self.checkpoint_ref.current.revision, at=at)
        self.checkpoint_ref.replace_acknowledged(cp)
        self._sync_records()
        self._acknowledged_bounds[token] = bounds
        return saved

    @staticmethod
    def _bounds(budget):
        return budget.normal_until, budget.completion_until, budget.completion_only

    def _budget(self, key, record, ancestors=()):
        self._restore_all(self._anchor())
        budget = self.budgets[key.token()]
        return replace(budget, ancestor_keys=ancestors or budget.ancestor_keys)

    def root_budget(self, key: ScopeKey, allowance_s: float, *, start: ScopeStart | None = None) -> DeadlineBudget:
        _number(allowance_s, positive=True)
        cp = self.checkpoint_ref.current
        if key.workflow_operation_id != cp.workflow_id or key.account_key != self.memory.account_key:
            raise BudgetAdmissionDenied("scope_identity_mismatch")
        anchor = self._anchor()
        now = anchor.monotonic_before
        record = self._read(key)
        if record is None:
            started = now if start is None else start.monotonic_at
            _number(started)
            if start is not None and (start.clock_epoch_id != self.clock.epoch_id or started > now):
                raise BudgetAdmissionDenied("invalid_start_event")
            budget = DeadlineBudget(key, self.clock.epoch_id, started, started + allowance_s, started + allowance_s)
            record = SavedTimeScope(key, None, "root", allowance_s, 1., start.event_id if start else "root",
                utc_microseconds(anchor.utc_at) + math.floor((started - anchor.monotonic_after) * 1_000_000), self.clock.epoch_id,
                snapshot_limits(budget, anchor, policy=self.policy))
            self.budgets[key.token()] = budget
            self._write(record)
        else:
            if record.allowance_s != allowance_s or record.plan_fingerprint != "root":
                raise BudgetAdmissionDenied("scope_conflict")
            self._restore_all(anchor)
            budget = self.budgets[key.token()]
            self._write(record)  # Reconstructed limits are acknowledged before work.
        return budget

    def open_or_reuse(self, key, spec, parent, *, plan, start=None, completion_cap=None, elapsed_reservation_s=0.):
        anchor = self._anchor()
        now = anchor.monotonic_before
        if parent.clock_epoch_id != self.clock.epoch_id:
            raise BudgetAdmissionDenied("clock_untrusted")
        if key.account_key != parent.scope_key.account_key or key.workflow_operation_id != parent.scope_key.workflow_operation_id:
            raise BudgetAdmissionDenied("scope_identity_mismatch")
        if key.token() in self.active or key in (*parent.ancestor_keys, parent.scope_key):
            raise BudgetAdmissionDenied("recursive_scope")
        if completion_cap is not None:
            _number(completion_cap)
        _number(elapsed_reservation_s)
        record = self._read(key)
        fingerprint = plan.fingerprint()
        ancestors = (*parent.ancestor_keys, parent.scope_key)
        if record is not None:
            if elapsed_reservation_s:
                raise BudgetAdmissionDenied("scope_conflict")
            if (record.allowance_s != spec.allowance_s or record.normal_fraction != spec.normal_fraction
                    or record.plan_fingerprint != fingerprint or record.parent_scope_key != parent.scope_key.token()):
                raise BudgetAdmissionDenied("scope_conflict")
            self._restore_all(anchor)
            original = self.budgets[key.token()]
            if start is not None and (start.clock_epoch_id != original.clock_epoch_id
                                      or start.monotonic_at != original.started_at
                                      or start.event_id != record.start_event_id):
                raise BudgetAdmissionDenied("scope_conflict")
            hard = min(original.completion_until, parent.completion_until,
                       completion_cap if completion_cap is not None else math.inf)
            tightened = original.clipped(parent.normal_until, hard)
            self.budgets[key.token()] = replace(tightened, ancestor_keys=ancestors)
            self._write(record)
            return self.budgets[key.token()]
        started = now if start is None else start.monotonic_at
        _number(started)
        # Explicit startup work may have elapsed before its first durable scope
        # can be written. Only that spent reservation is credited; the immutable
        # plan remains unchanged and no future work or time is manufactured.
        if elapsed_reservation_s and (start is None or elapsed_reservation_s > now - started
                                      or elapsed_reservation_s > plan.cost_s()):
            raise BudgetAdmissionDenied("invalid_start_event")
        if start is not None and (start.clock_epoch_id != self.clock.epoch_id
                                  or started > now or started < parent.started_at):
            raise BudgetAdmissionDenied("invalid_start_event")
        if started + spec.allowance_s > parent.normal_until:
            raise BudgetAdmissionDenied("allocation_does_not_fit")
        hard = min(started + spec.allowance_s, parent.completion_until,
                   completion_cap if completion_cap is not None else math.inf)
        normal = min(parent.normal_until, started + spec.normal_fraction * (hard - started))
        if normal <= now or plan.cost_s() - elapsed_reservation_s > normal - now:
            raise BudgetAdmissionDenied("planned_work_does_not_fit")
        budget = DeadlineBudget(key, self.clock.epoch_id, started, normal, hard, ancestors)
        record = SavedTimeScope(key, parent.scope_key.token(), fingerprint, spec.allowance_s,
            spec.normal_fraction, start.event_id if start else "entry",
            utc_microseconds(anchor.utc_at) + math.floor((started - anchor.monotonic_after) * 1_000_000),
            self.clock.epoch_id, snapshot_limits(budget, anchor, policy=self.policy))
        self.budgets[key.token()] = budget
        self._write(record)
        return budget

    def mark_completion_only(self, key: ScopeKey, reason: str = "normal_exhausted"):
        record = self._read(key)
        if record is not None and not record.limits.completion_only:
            self._restore_all(self._anchor())
            update = replace(record, limits=replace(record.limits, completion_only=True))
            self.records[key.token()] = update  # Latch even if storage acknowledgment fails.
            self.budgets[key.token()] = replace(self.budgets[key.token()], completion_only=True)
            self._write(update)

    def _check(self, budget: DeadlineBudget, *, completion: bool, until: float):
        now = self.now()
        if budget.clock_epoch_id != self.clock.epoch_id:
            raise BudgetAdmissionDenied("clock_untrusted")
        if not completion:
            if not self.memory.healthy:
                raise BudgetAdmissionDenied("storage_unavailable")
            self._anchor()  # Known UTC/monotonic discontinuity never admits new input.
        cap = min(until, budget.completion_until if completion else budget.normal_until)
        if self.memory.healthy:
            self._sync_records()
        for key in (*budget.ancestor_keys, budget.scope_key):
            record = self.records.get(key.token())
            if record is None:
                raise BudgetAdmissionDenied("unregistered_scope")
            cached = self.budgets.get(key.token())
            if cached is None:
                raise BudgetAdmissionDenied("unregistered_scope")
            if not completion and (record.limits.completion_only or cached.completion_only or budget.completion_only):
                raise BudgetAdmissionDenied("completion_only")
            cap = min(cap, cached.completion_until if completion else cached.normal_until)
        if now >= cap:
            raise BudgetAdmissionDenied("deadline_exhausted")
        return cap - now

    def require_normal(self, budget, *, until):
        return self._check(budget, completion=False, until=until)

    def require_completion(self, budget, *, operation_id, until):
        if not operation_id:
            raise BudgetAdmissionDenied("missing_operation")
        return self._check(budget, completion=True, until=until)

    def diagnostic(self, budget: DeadlineBudget) -> TimingDiagnostic:
        now = self.now()
        if budget.clock_epoch_id != self.clock.epoch_id:
            raise BudgetAdmissionDenied("clock_untrusted")
        self._sync_records()
        records = [self.records.get(key.token()) for key in (*budget.ancestor_keys, budget.scope_key)]
        if any(record is None for record in records):
            raise BudgetAdmissionDenied("unregistered_scope")
        budgets = [self.budgets[r.scope_key.token()] for r in records]
        normal = min(budget.normal_until, *(b.normal_until for b in budgets))
        hard = min(budget.completion_until, *(b.completion_until for b in budgets))
        return TimingDiagnostic(budget.scope_key.token(), max(0., now - budget.started_at),
            max(0., normal - now), max(0., hard - now),
            any(r.limits.completion_only for r in records) or now >= normal, max(0., now - hard))


T = TypeVar("T")


class TimeSensitive:
    def __init__(self, spec: BudgetSpec):
        self.spec = spec

    def __call__(self, ctx: "RunContext", invoke: Callable[["RunContext"], T], *, key: ScopeKey,
                 plan: TimePlan, start: ScopeStart | None = None, completion_cap: float | None = None) -> T:
        ctx.normal()
        reused = ctx.scopes._read(key) is not None
        budget = ctx.scopes.open_or_reuse(key, self.spec, ctx.budget, plan=plan,
                                         start=start, completion_cap=completion_cap)
        child = ctx.with_budget(budget)
        child.normal()
        # Scope persistence time is real elapsed time, never a later start event.
        if not reused and plan.cost_s() > budget.remaining_normal(ctx.scopes.now()):
            raise BudgetAdmissionDenied("planned_work_does_not_fit")
        ctx.scopes.active.add(key.token())
        try:
            return invoke(child)
        finally:
            ctx.scopes.active.discard(key.token())
            # Never replace a returned semantic result with a timeout or write error.
            if ctx.clock.monotonic() >= ctx.budget.normal_until:
                try:
                    ctx.ownership.assert_owned()
                    ctx.scopes.mark_completion_only(ctx.budget.scope_key)
                except Exception:
                    pass  # Storage already latches unhealthy; callers stop progression.


def open_original_window(ctx, *, window_id: str, plan: TimePlan,
                         allowance_s: float = 180., normal_fraction: float = .70):
    """Persist one window before its first observation; reentry never renews it.

    Call from the same registered parent context on every invocation. Access,
    baseline, request, polling, preparation and submission borrow the returned
    context. Its hard expiry is a work-window bound, not proof of code expiry.
    All co-occurring stages must be included in ``plan``. This uses the existing
    scope registry, checkpoint transaction and normal/completion permissions.
    """
    if (type(window_id) is not str or not window_id or len(window_id) > 220
            or any(ord(c) < 33 or ord(c) == 127 for c in window_id)):
        raise ValueError("A window requires a bounded stable identifier.")
    if type(plan) is not TimePlan:
        raise TypeError("A window requires an explicit time plan.")
    spec = BudgetSpec(allowance_s, normal_fraction)
    ctx.normal()
    child = ctx.child_path("window." + window_id)
    budget = ctx.scopes.open_or_reuse(child.scope_key(), spec, ctx.budget, plan=plan)
    result = child.with_budget(budget)
    result.normal()
    return result


def original_window_utc_bounds(ctx) -> tuple[int, int]:
    """Read the persisted window anchors, including tightened parent expiry."""
    ctx.ownership.assert_owned()
    ctx.scopes.now()  # Enforce the registry's owning-thread and clock checks.
    record = ctx.scopes._read(ctx.budget.scope_key)
    if record is None or record.parent_scope_key is None:
        raise BudgetAdmissionDenied("scope_conflict")
    return record.activated_utc_us, record.limits.completion_expires_utc_us


@dataclass(frozen=True, repr=False)
class CallBudget:
    context: "RunContext"
    until: float
    permission: WorkPermission

    def checkpoint(self):
        ctx = self.context
        ctx.ownership.assert_owned()
        if self.permission.mode in {"normal", "recovery"}:
            if self.permission.mode == "recovery":
                frame = ctx.checkpoint.current.recovery
                if frame is None or frame.frame_id != self.permission.frame_id:
                    raise BudgetAdmissionDenied("recovery_exhausted")
            if ctx.cancellation.is_set():
                raise BudgetAdmissionDenied("cancelled")
            return ctx.scopes.require_normal(ctx.budget, until=self.until)
        return ctx.scopes.require_completion(ctx.budget, operation_id=self.permission.operation_id,
                                             until=self.until)

    def timeout_ms(self, cap_ms: int | None = None) -> int:
        remaining = self.checkpoint()
        if cap_ms is not None:
            _number(cap_ms, positive=True)
            remaining = min(remaining, cap_ms / 1000)
        value = math.floor(remaining * 1000)
        if value < 1:
            raise BudgetAdmissionDenied("deadline_exhausted")
        return value

    def timeout_s(self, cap_s: float | None = None) -> float:
        remaining = self.checkpoint()
        if cap_s is not None:
            _number(cap_s, positive=True)
            remaining = min(remaining, cap_s)
        if remaining < .001:
            raise BudgetAdmissionDenied("deadline_exhausted")
        return remaining


@dataclass(frozen=True)
class ExpiryAnchor:
    clock_epoch_id: str
    monotonic_before: float
    utc_at: datetime
    monotonic_after: float


def capture_expiry_anchor(clock: Clock) -> ExpiryAnchor:
    before = clock.monotonic()
    utc = clock.utc_now()
    after = clock.monotonic()
    _number(before)
    _number(after)
    if utc.tzinfo is None or utc.utcoffset() is None or after < before:
        raise BudgetAdmissionDenied("invalid_expiry_anchor")
    return ExpiryAnchor(clock.epoch_id, before, utc, after)


def utc_microseconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BudgetAdmissionDenied("clock_untrusted")
    delta = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def empty_time_checkpoint(clock: Clock, policy: RestartClockPolicy = RestartClockPolicy()) -> TimeCheckpoint:
    anchor = capture_expiry_anchor(clock)
    utc = utc_microseconds(anchor.utc_at)
    return TimeCheckpoint(clock.epoch_id, utc, utc, (), ())


def snapshot_limits(budget: DeadlineBudget, anchor: ExpiryAnchor,
                    previous: SavedTimeLimits | None = None,
                    policy: RestartClockPolicy = RestartClockPolicy()) -> SavedTimeLimits:
    if budget.clock_epoch_id != anchor.clock_epoch_id:
        raise BudgetAdmissionDenied("clock_untrusted")
    utc = utc_microseconds(anchor.utc_at)
    normal_delta = budget.normal_until - anchor.monotonic_after
    hard_delta = budget.completion_until - anchor.monotonic_after
    saved = SavedTimeLimits(
        utc + math.floor((normal_delta - policy.uncertainty_s) * 1_000_000),
        utc + math.floor((hard_delta - policy.uncertainty_s) * 1_000_000), utc,
        max(0., normal_delta), max(0., hard_delta),
        budget.completion_only or normal_delta <= 0)
    return _tighten_saved_limits(saved, previous) if previous is not None else saved


def _tighten_saved_limits(saved: SavedTimeLimits, previous: SavedTimeLimits) -> SavedTimeLimits:
    return replace(saved,
        normal_expires_utc_us=min(saved.normal_expires_utc_us, previous.normal_expires_utc_us),
        completion_expires_utc_us=min(saved.completion_expires_utc_us, previous.completion_expires_utc_us),
        normal_remaining_cap_s=min(saved.normal_remaining_cap_s, previous.normal_remaining_cap_s),
        completion_remaining_cap_s=min(saved.completion_remaining_cap_s, previous.completion_remaining_cap_s),
        completion_only=saved.completion_only or previous.completion_only)


def _restored_bounds(saved: SavedTimeLimits, anchor: ExpiryAnchor,
                     policy: RestartClockPolicy) -> tuple[float, float]:
    utc = utc_microseconds(anchor.utc_at)
    if utc + math.floor(policy.uncertainty_s * 1_000_000) < saved.checkpoint_utc_us:
        raise BudgetAdmissionDenied("clock_untrusted")
    def remaining(expiry, cap):
        return max(0., min(cap, (expiry - utc) / 1_000_000 - policy.uncertainty_s))
    return (anchor.monotonic_before + remaining(saved.normal_expires_utc_us, saved.normal_remaining_cap_s),
            anchor.monotonic_before + remaining(saved.completion_expires_utc_us, saved.completion_remaining_cap_s))


def restore_limits(saved: SavedTimeScope, *, anchor: ExpiryAnchor,
                   parent: DeadlineBudget | None, clock_epoch_id: str,
                   policy: RestartClockPolicy = RestartClockPolicy()) -> DeadlineBudget:
    if (clock_epoch_id != anchor.clock_epoch_id
            or (saved.clock_epoch != clock_epoch_id and not policy.trusted_host_utc)):
        raise BudgetAdmissionDenied("clock_untrusted")
    if parent is None:
        if saved.parent_scope_key is not None:
            raise BudgetAdmissionDenied("scope_identity_mismatch")
        ancestors = ()
    else:
        if (saved.parent_scope_key != parent.scope_key.token()
                or saved.scope_key.account_key != parent.scope_key.account_key
                or saved.scope_key.workflow_operation_id != parent.scope_key.workflow_operation_id
                or parent.clock_epoch_id != clock_epoch_id):
            raise BudgetAdmissionDenied("scope_identity_mismatch")
        ancestors = (*parent.ancestor_keys, parent.scope_key)
    normal, hard = _restored_bounds(saved.limits, anchor, policy)
    if parent is not None:
        hard = min(hard, parent.completion_until)
        normal = min(normal, parent.normal_until, hard)
    return DeadlineBudget(saved.scope_key, clock_epoch_id, anchor.monotonic_before,
                          normal, hard, ancestors, saved.limits.completion_only)


def restore_action_limits(saved: SavedTimeLimits, *, scope: DeadlineBudget,
                          anchor: ExpiryAnchor,
                          policy: RestartClockPolicy = RestartClockPolicy()) -> DeadlineBudget:
    if scope.clock_epoch_id != anchor.clock_epoch_id:
        raise BudgetAdmissionDenied("clock_untrusted")
    normal, hard = _restored_bounds(saved, anchor, policy)
    return replace(scope.clipped(normal, hard), completion_only=scope.completion_only or saved.completion_only)


def submission_cutoff(expiry: datetime, anchor: ExpiryAnchor, *, clock: Clock,
                      uncertainty_s: float, delivery_margin_s: float,
                      previous_cutoff: float | None = None) -> float:
    _number(uncertainty_s)
    _number(delivery_margin_s)
    if expiry.tzinfo is None or expiry.utcoffset() is None or anchor.clock_epoch_id != clock.epoch_id:
        raise BudgetAdmissionDenied("invalid_expiry_anchor")
    # A wall jump invalidates input permission; it never grants additional time.
    drift = abs((clock.utc_now() - anchor.utc_at).total_seconds()
                - (clock.monotonic() - anchor.monotonic_before))
    if drift > uncertainty_s + (anchor.monotonic_after - anchor.monotonic_before):
        raise BudgetAdmissionDenied("wall_clock_discontinuity")
    cutoff = anchor.monotonic_before + (expiry - anchor.utc_at).total_seconds() - uncertainty_s - delivery_margin_s
    return min(cutoff, previous_cutoff) if previous_cutoff is not None else cutoff
