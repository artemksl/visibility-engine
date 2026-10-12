"""Bounded custom pointer dispatch; the driver alone performs browser I/O.

The supplied movement planner is unchanged. These execution bounds come from its
DISPATCH.md: one absolute epoch, 40 ms maximum lateness, 5 ms pump slices, and at
most one explicitly permitted pre-activation replan. They are engineering bounds,
not claims about delivered events or human movement. Trace data is runtime only.
"""

from dataclasses import dataclass, field, replace
import math

from automation_core.browser.movement import (
    Budget, EPS, Geometry, Gesture, MovementPlan, MovementStrategy, PointerState,
    Policy, Stamp, Status, Vec, inside, validate_policy,
)


class PointerError(RuntimeError):
    """A fixed technical reason suitable for the executor receipt."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class MovementOptions:
    """Per-interaction opt-in; no sensitive-control policy is inferred here."""

    policy: Policy
    mode: str = "locator"
    pre_activation_replan_allowed: bool = False
    button: str = "left"


@dataclass(frozen=True)
class PointerCheck:
    kind: str
    geometry: Geometry


@dataclass
class PointerLedger:
    """Known last commanded coordinate, never an assumed browser start."""

    state: PointerState = PointerState(None)
    binding: object | None = None
    last_command_start: float | None = None
    last_command_return: float | None = None
    possible_buttons: set[str] = field(default_factory=set)
    viewport: tuple[float, float] | None = None
    invalidated_by: str = "initial_unknown"

    def invalidate(self, reason: str) -> None:
        # Keep command-start spacing across operations even when position is lost.
        self.state = PointerState(None)
        self.binding = None
        self.viewport = None
        self.invalidated_by = reason

    def commanded(self, point: Vec, stamp: Stamp, binding, start: float,
                  returned: float, viewport: tuple[float, float]) -> None:
        self.state = PointerState(point, "commanded", stamp, True)
        self.binding = binding
        self.viewport = viewport
        self.last_command_start = start
        self.last_command_return = returned


@dataclass(frozen=True)
class PlanTrace:
    status: str
    reason: str
    stamp: Stamp
    stream_id: int
    start_provenance: str
    endpoint: Vec | None
    locator_position: Vec | None
    travel_s: float
    arrival_pause_s: float
    hold_s: float


@dataclass(frozen=True)
class CommandTrace:
    kind: str
    started_at: float
    returned_at: float | None = None
    scheduled_at: float | None = None
    requested: Vec | None = None


@dataclass(frozen=True)
class SkippedPoint:
    plan_index: int
    point_index: int
    reason: str


@dataclass(frozen=True)
class MovementTrace:
    """Ephemeral technical evidence; no full trajectory or business outcome."""

    plans: tuple[PlanTrace, ...] = ()
    commands: tuple[CommandTrace, ...] = ()
    skipped: tuple[SkippedPoint, ...] = ()
    replans: tuple[str, ...] = ()
    recertifications: tuple[tuple[Stamp, Stamp], ...] = ()
    arrival_observation: str = "unknown_no_new_move"
    locator_position: Vec | None = None


class _Replan(Exception):
    def __init__(self, reason):
        self.reason = reason


class PointerController:
    """Runs within the executor's existing sequential input/dialog admission."""

    LATE_LIMIT_S = .040
    WAIT_QUANTUM_S = .005

    def __init__(self, driver, strategy=None):
        self.driver = driver
        self.strategy = strategy if strategy is not None else MovementStrategy()
        self.ledger = PointerLedger()

    def invalidate(self, reason="external_input") -> None:
        self.ledger.invalidate(reason)

    def _guard(self, binding, deadline, state, *, after_proof=False) -> None:
        if state["reason"] is not None:
            raise PointerError(state["reason"])
        try:
            guard = (getattr(self.driver, "pointer_postproof_guard", None)
                     if after_proof and getattr(self.driver, "_native_input_guard", None) is not None
                     else None)
            (guard or self.driver.pointer_guard)(binding, deadline)
        except Exception as error:
            if getattr(error, "reason", None) in {
                "ownership_lost", "wrong_thread", "binding_changed", "stale_view",
                "binding_origin_changed", "input_quarantined",
            }:
                self.invalidate(getattr(error, "reason"))
            raise

    def _native_guard(self, binding, deadline, state):
        # Controlled geometry fixtures predate guarded production dispatch.
        # The actual BrowserDriver always supplies the native boundary port.
        guard = getattr(self.driver, "_before_native_input", None)
        if guard is not None:
            guard(binding, deadline, state)

    def _wait_until(self, when, binding, deadline, state) -> None:
        while True:
            self._guard(binding, deadline, state)
            now = self.driver.clock.monotonic()
            if now >= when:
                return
            self.driver.pointer_wait(binding, min(self.WAIT_QUANTUM_S,
                when - now, deadline - now), deadline)

    def _timeout_ms(self, binding, deadline, state, *, after_proof=False) -> int:
        self._guard(binding, deadline, state, after_proof=after_proof)
        timeout = math.floor(1000 * (deadline - self.driver.clock.monotonic()))
        if timeout < 1:
            raise PointerError("insufficient_call_budget")
        return timeout

    def _check_layout(self, binding, plan) -> None:
        stamp = self.driver.pointer_stamp(binding)
        if (stamp.view, stamp.document) != (plan.stamp.view, plan.stamp.document):
            self.invalidate("binding_changed")
            raise PointerError("binding_changed")
        if stamp.layout != plan.stamp.layout:
            raise _Replan("layout_changed")

    @staticmethod
    def _enter(state, kind, now):
        if state["entered_at"] is None:
            state["entered_at"] = now
        state.update(status="attempted", phase=kind)

    def _move(self, point, scheduled, plan, binding, deadline, state, commands, viewport,
              next_due=None):
        self._guard(binding, deadline, state)
        self._check_layout(binding, plan)
        # The layout inspection itself can pump cancellation or dialog events.
        self._guard(binding, deadline, state)
        now = self.driver.clock.monotonic()
        # Geometry I/O can itself overrun the selected waypoint. Apply schedule
        # admission to the actual dispatch time, including this final inspection.
        if plan.status == Status.PLANNED:
            if now - scheduled > self.LATE_LIMIT_S:
                raise _Replan("dispatch_late")
            if next_due is not None and next_due <= now:
                return None
        else:
            scheduled = now  # relocation has no earlier scheduled trajectory
        if now + plan.arrival_pause_s + plan.hold_s + plan.reserve_s >= deadline:
            raise PointerError("insufficient_remaining_budget")
        self._native_guard(binding, deadline, state)
        self._guard(binding, deadline, state, after_proof=True)
        now = self.driver.clock.monotonic()
        if plan.status == Status.PLANNED:
            if now - scheduled > self.LATE_LIMIT_S:
                raise _Replan("dispatch_late")
            if next_due is not None and next_due <= now:
                return None
        if now + plan.arrival_pause_s + plan.hold_s + plan.reserve_s >= deadline:
            raise PointerError("insufficient_remaining_budget")
        self._enter(state, "move", now)
        commands.append(CommandTrace("move", now, scheduled_at=scheduled,
                                     requested=point))
        self.invalidate("move_in_flight")
        self.ledger.last_command_start = now
        self.driver.pointer_move(binding, point.x, point.y)
        returned = self.driver.clock.monotonic()
        self.ledger.commanded(point, plan.stamp, binding, now, returned, viewport)
        commands[-1] = replace(commands[-1], returned_at=returned)
        state["returned_at"] = returned
        self._guard(binding, deadline, state)
        return returned

    def _stream(self, plan, policy, binding, deadline, state, commands, skipped,
                plan_index, viewport):
        self._guard(binding, deadline, state)
        if (not 2 <= len(plan.points) <= 256 or plan.points[0].relative_time != 0
                or plan.coordinate_space != "main_frame_viewport_css_px"):
            raise PointerError("invalid_movement_plan")
        epoch = self.driver.clock.monotonic()
        due = tuple(epoch + point.relative_time for point in plan.points)
        end, index = len(plan.points) - 1, 1
        post = plan.arrival_pause_s + plan.hold_s + policy.reserve_s
        if epoch + plan.travel_s + post >= deadline:
            raise PointerError("insufficient_remaining_budget")
        returned = epoch
        while index <= end:
            self._guard(binding, deadline, state)
            self._check_layout(binding, plan)
            now = self.driver.clock.monotonic()
            if now - due[index] > self.LATE_LIMIT_S:
                raise _Replan("dispatch_late")
            last = self.ledger.last_command_start
            earliest = -math.inf if last is None else last + policy.min_command_gap_s
            while index < end and (due[index] <= now or due[index] < earliest):
                skipped.append(SkippedPoint(plan_index, index, "overdue_or_spacing"))
                index += 1
            chosen = index
            desired = max(due[chosen], earliest)
            if desired + post >= deadline:
                raise PointerError("insufficient_remaining_budget")
            self._wait_until(desired, binding, deadline, state)
            self._guard(binding, deadline, state)
            now = self.driver.clock.monotonic()
            if now - due[chosen] > self.LATE_LIMIT_S:
                raise _Replan("dispatch_late")
            if chosen < end and due[chosen + 1] <= now:
                skipped.append(SkippedPoint(plan_index, chosen, "obsolete_after_wait"))
                index = chosen + 1
                continue
            point = plan.points[chosen]
            returned = self._move(Vec(point.x, point.y), due[chosen], plan,
                binding, deadline, state, commands, viewport,
                due[chosen + 1] if chosen < end else None)
            if returned is None:
                skipped.append(SkippedPoint(plan_index, chosen, "obsolete_before_dispatch"))
            index = chosen + 1
        return returned

    def _relocate(self, plan, policy, binding, deadline, state, commands, viewport):
        if plan.endpoint is None:
            raise PointerError("invalid_movement_plan")
        last = self.ledger.last_command_start
        if last is not None:
            self._wait_until(last + policy.min_command_gap_s, binding, deadline, state)
        return self._move(plan.endpoint, self.driver.clock.monotonic(), plan,
            binding, deadline, state, commands, viewport)

    @staticmethod
    def _locator_position(check, plan, policy, kind):
        if plan.status == Status.LOCATOR_ONLY:
            if policy.allow_locator_fallback:
                return None
            raise PointerError("locator_fallback_not_allowed")
        geometry = check.geometry
        if geometry.locator_origin is None:
            if kind == "hover" and policy.allow_locator_fallback:
                return None
            raise PointerError("locator_mapping_uncertified")
        if plan.endpoint is None or plan.locator_position is None:
            raise PointerError("locator_mapping_uncertified")
        position = plan.endpoint - geometry.locator_origin
        if ((position - plan.locator_position).norm() > EPS
                or position.x < 0 or position.y < 0
                or not any(inside(region, plan.endpoint) for region in geometry.regions)):
            raise _Replan("locator_mapping_changed")
        return position

    def execute(self, request, binding, deadline: float, state: dict) -> None:
        """Mutate the executor receipt builder, then raise failures after cleanup.

        There is no activation retry, even when a browser call raises after an
        external event. Cleanup may itself complete an effect and remains visible.
        """
        plans, commands, skipped, replans, recertifications = [], [], [], [], []
        arrival = "unknown_no_new_move"
        position = None
        possible = self.ledger.possible_buttons
        owned_buttons = set()
        try:
            self._guard(binding, deadline, state)
            options = request.movement
            if (type(options) is not MovementOptions or type(options.policy) is not Policy
                    or type(options.mode) is not str or options.mode not in {"locator", "raw"}
                    or type(options.pre_activation_replan_allowed) is not bool
                    or type(options.button) is not str or options.button not in {"left", "middle", "right"}
                    or request.kind not in {"move", "hover", "click"}
                    or (options.mode == "raw" and request.kind != "click")):
                raise PointerError("invalid_movement_request")
            try:
                validate_policy(options.policy)
            except (ValueError, TypeError, AttributeError):
                raise PointerError("invalid_movement_policy") from None
            if possible:
                raise PointerError("input_quarantined")
            max_plans = 2 if options.pre_activation_replan_allowed else 1
            for attempt in range(max_plans):
                self._guard(binding, deadline, state)
                state["phase"] = "pointer_preparation"
                geometry = self.driver.prepare_pointer(request.target, binding, deadline, state)
                self._guard(binding, deadline, state)
                if options.policy.stream_id + attempt >= 2**64:
                    raise PointerError("stream_id_exhausted")
                policy = replace(options.policy, stream_id=options.policy.stream_id + attempt)
                start = self.ledger.state
                if (start.stamp != geometry.stamp and start.position is not None
                        and start.provenance in {"commanded", "observed"}
                        and start.exclusive_owner
                        and self.driver.certify_pointer_continuity(self.ledger, geometry, binding)):
                    recertifications.append((start.stamp, geometry.stamp))
                    start = replace(start, stamp=geometry.stamp)
                    self.ledger.state = start
                now = self.driver.clock.monotonic()
                plan = self.strategy.plan(Gesture(request.kind, start, options.mode),
                    geometry, policy, Budget(now, deadline - now,
                                            self.driver.pointer_stamp(binding)))
                plans.append(PlanTrace(plan.status.value, plan.reason, plan.stamp,
                    plan.stream_id, plan.start_provenance, plan.endpoint,
                    plan.locator_position, plan.travel_s, plan.arrival_pause_s, plan.hold_s))
                if plan.status in {Status.UNKNOWN_START, Status.INSUFFICIENT_BUDGET,
                    Status.INVALID_GEOMETRY, Status.RESOURCE_LIMIT, Status.UNSUPPORTED}:
                    raise PointerError(plan.status.value)
                if plan.status == Status.STALE_GEOMETRY:
                    if attempt + 1 < max_plans:
                        replans.append("stale_geometry")
                        continue
                    raise PointerError("stale_geometry_after_replan")
                try:
                    state["phase"] = "move"
                    if plan.status == Status.PLANNED:
                        pause_base = self._stream(plan, policy, binding, deadline,
                            state, commands, skipped, attempt, (geometry.width, geometry.height))
                        arrival = "terminal_command_return"
                    elif plan.status == Status.RELOCATE:
                        pause_base = self._relocate(plan, policy, binding, deadline, state, commands, (geometry.width, geometry.height))
                        arrival = "terminal_command_return"
                    elif plan.status == Status.ALREADY_INSIDE:
                        pause_base = self.driver.clock.monotonic()
                        arrival = "unknown_no_new_move"
                    elif plan.status == Status.LOCATOR_ONLY:
                        if not (policy.allow_locator_fallback and options.mode == "locator"
                                and request.kind in {"click", "hover"}):
                            raise PointerError("locator_fallback_not_allowed")
                        pause_base = self.driver.clock.monotonic()
                        arrival = "unknown_no_custom_move"
                    else:
                        raise PointerError("unexpected_plan_status")
                    state["phase"] = "pointer_revalidation"
                    check = self.driver.revalidate_pointer(request.target, binding,
                        plan.endpoint, plan.stamp, deadline)
                    self._guard(binding, deadline, state)
                    if check.kind != "same":
                        raise _Replan("post_move_geometry_changed")
                    if plan.arrival_pause_s > 0:
                        if (max(self.driver.clock.monotonic(), pause_base + plan.arrival_pause_s)
                                + plan.hold_s + policy.reserve_s >= deadline):
                            raise PointerError("insufficient_pause_budget")
                        self._wait_until(pause_base + plan.arrival_pause_s, binding, deadline, state)
                        check = self.driver.revalidate_pointer(request.target, binding,
                            plan.endpoint, check.geometry.stamp, deadline)
                        self._guard(binding, deadline, state)
                        if check.kind != "same":
                            raise _Replan("post_pause_geometry_changed")
                    if options.mode == "locator" and request.kind in {"click", "hover"}:
                        position = self._locator_position(check, plan, policy, request.kind)
                    break
                except _Replan as change:
                    if state["activation_entered"] or attempt + 1 >= max_plans:
                        raise PointerError("replan_limit") from None
                    replans.append(change.reason)
            self._guard(binding, deadline, state)
            if request.kind == "move":
                state.update(status="returned", phase="move")
                return
            if plan.hold_s + policy.reserve_s >= deadline - self.driver.clock.monotonic():
                raise PointerError("insufficient_activation_budget")
            if options.mode == "locator":
                self._native_guard(binding, deadline, state)
                timeout = self._timeout_ms(binding, deadline, state, after_proof=True)
                self._guard(binding, deadline, state, after_proof=True)
                if plan.hold_s + policy.reserve_s >= deadline - self.driver.clock.monotonic():
                    raise PointerError("insufficient_activation_budget")
                now = self.driver.clock.monotonic()
                self._enter(state, request.kind, now)
                self.invalidate("opaque_locator_action")
                if request.kind == "click":
                    possible.add(options.button)
                    owned_buttons.add(options.button)
                    state["activation_entered"] = True
                commands.append(CommandTrace(request.kind, now))
                self.driver.pointer_locator(binding, request.target, request.kind,
                    position, options.button, 1000 * plan.hold_s, timeout)
                if request.kind == "click":
                    possible.discard(options.button)
                    owned_buttons.discard(options.button)
                    state["activation_returned"] = True
                returned = self.driver.clock.monotonic()
                commands[-1] = replace(commands[-1], returned_at=returned)
                state.update(status="returned", returned_at=returned)
                self._guard(binding, deadline, state)
                return
            pointer = self.ledger.state
            if (pointer.position is None or pointer.provenance == "unknown"
                    or not pointer.exclusive_owner or pointer.stamp != check.geometry.stamp
                    or plan.endpoint is None or pointer.position != plan.endpoint
                    or not check.geometry.certified_usable
                    or not any(inside(region, pointer.position) for region in check.geometry.regions)):
                raise PointerError("raw_endpoint_not_certified")
            self._guard(binding, deadline, state)
            if getattr(self.driver, "_native_input_guard", None) is not None:
                fresh = self.driver.revalidate_pointer(request.target, binding,
                    plan.endpoint, check.geometry.stamp, deadline)
                if fresh.kind != "same" or not fresh.geometry.certified_usable:
                    raise PointerError("raw_endpoint_not_certified")
                self._guard(binding, deadline, state)
            self._native_guard(binding, deadline, state)
            self._guard(binding, deadline, state, after_proof=True)
            if plan.hold_s + policy.reserve_s >= deadline - self.driver.clock.monotonic():
                raise PointerError("insufficient_activation_budget")
            possible.add(options.button)
            owned_buttons.add(options.button)
            state["activation_entered"] = True
            now = self.driver.clock.monotonic()
            self._enter(state, "down", now)
            commands.append(CommandTrace("down", now))
            try:
                self.driver.pointer_down(binding, options.button)
            except Exception:
                self.invalidate("down_uncertain")
                raise
            returned = self.driver.clock.monotonic()
            commands[-1] = replace(commands[-1], returned_at=returned)
            state["returned_at"] = returned
            self._guard(binding, deadline, state)
            if returned + plan.hold_s >= deadline:
                raise PointerError("insufficient_hold_budget")
            self._wait_until(returned + plan.hold_s, binding, deadline, state)
            self._guard(binding, deadline, state)
            self._native_guard(binding, deadline, state)
            self._guard(binding, deadline, state, after_proof=True)
            now = self.driver.clock.monotonic()
            self._enter(state, "up", now)
            commands.append(CommandTrace("up", now))
            try:
                self.driver.pointer_up(binding, options.button)
            except Exception:
                self.invalidate("up_uncertain")
                raise
            possible.discard(options.button)
            owned_buttons.discard(options.button)
            returned = self.driver.clock.monotonic()
            commands[-1] = replace(commands[-1], returned_at=returned)
            state.update(status="returned", returned_at=returned, activation_returned=True)
            self._guard(binding, deadline, state)
        finally:
            # The executor still holds its admission. Cancellation/deadline cannot
            # authorize foreign releases; unresolved operation-owned state stops
            # later input. There is exactly one cleanup call per owned button.
            for button in tuple(owned_buttons):
                try:
                    allowed = self.driver.pointer_cleanup_allowed(binding)
                except Exception:
                    allowed = False
                if allowed:
                    state["cleanup_attempted"] = True
                    now = self.driver.clock.monotonic()
                    commands.append(CommandTrace("cleanup_up", now))
                    try:
                        self.driver.pointer_up(binding, button, cleanup=True)
                        possible.discard(button)
                        commands[-1] = replace(commands[-1], returned_at=self.driver.clock.monotonic())
                        state["cleanup_returned"] = True
                    except Exception:
                        self.invalidate("cleanup_uncertain")
                        state["cleanup_reason"] = "cleanup_uncertain"
                else:
                    state["cleanup_reason"] = "cleanup_unavailable"
                if button in possible:
                    self.driver.quarantine_pointer()
            state["movement"] = MovementTrace(tuple(plans), tuple(commands), tuple(skipped),
                tuple(replans), tuple(recertifications), arrival, position)
