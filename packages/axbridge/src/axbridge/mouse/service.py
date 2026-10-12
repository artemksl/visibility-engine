"""Session-scoped mouse actions, independent of page selectors."""

from contextlib import contextmanager
import math
import random
import statistics
import threading

from .models import (
    BackendFailure, Cancelled, InvalidTarget, MotionConfig, MouseBusy, MouseError,
    MousePlan, MouseResult, NoWanderSpace, Point, Rect, Screen, TargetChanged,
    TimingOverrun, as_rect,
)
from .planner import MotionPlanner
from .profile import BehaviorSampler


_POINTER_LOCK = threading.Lock()


def _clamp(value, limits):
    return max(limits[0], min(limits[1], value))


class HumanMouse:
    """Retain one instance per workflow; actions are synchronous and cancellable.

    ``backend``, ``clock`` and ``rng`` are dependency injection seams for tests.
    Constructing or previewing this object never imports a desktop driver.
    """

    def __init__(self, *, backend=None, clock=None, config=None, rng=None, traits=None):
        self.config = config or MotionConfig()
        self.sampler = BehaviorSampler(config=self.config, rng=rng, traits=traits)
        self.planner = MotionPlanner(config=self.config)
        self._backend, self._clock = backend, clock
        self._executor = None
        self._cancel = threading.Event()
        self.last_result = None
        self._plans = []
        self._preview_mode = False

    @property
    def traits(self): return self.sampler.traits

    def __enter__(self): return self

    def __exit__(self, *exc):
        if self._executor is not None:
            if not self._executor.cleanup() and not exc[0]:
                raise BackendFailure("Could not release an owned mouse button.", result=self.last_result)

    def cancel(self):
        self._cancel.set()

    def _driver(self):
        if self._executor is None:
            from .backends import default_backend
            from .executor import MotionExecutor
            if self._backend is None:
                self._backend = default_backend()
            self._executor = MotionExecutor(self._backend, clock=self._clock, config=self.config)
        return self._executor

    @staticmethod
    def _button(button, count=1):
        if button not in ('left', 'right', 'middle') or type(count) is not int or count not in (1, 2):
            raise InvalidTarget("Use left/right/middle and a click count of one or two.")

    @staticmethod
    def _distance(distance):
        try:
            low, high = distance
            if not all(math.isfinite(v) for v in (low, high)) or low < 0 or high < low or high <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise InvalidTarget("Distance must be a finite positive range.") from None
        return (low, high)

    @staticmethod
    def _probability(probability):
        if not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0 <= probability <= 1:
            raise InvalidTarget("Wander probability must be between zero and one.")

    @staticmethod
    def _validate_guard(guard):
        if guard is not None and not callable(getattr(guard, 'validate', None)):
            raise InvalidTarget("A target guard must provide validate(point, deadline=...).")

    @contextmanager
    def _action(self, name, cancel):
        if cancel is not None and not callable(cancel) and not callable(getattr(cancel, 'is_set', None)):
            raise InvalidTarget("Cancellation requires a callable or an event with is_set().")
        locked = self._preview_mode or _POINTER_LOCK.acquire(blocking=False)
        if not locked:
            raise MouseBusy("Another mouse action is already running.")
        self._cancel.clear()
        result = MouseResult(name)
        self.last_result = result
        self._plans = []
        executor, started = None, None
        failure = None
        try:
            executor = self._driver()
            started = executor.clock.now()
            result.backend = self._backend.name
            def cancelled():
                return self._cancel.is_set() or bool(cancel and (cancel.is_set() if hasattr(cancel, 'is_set') else cancel()))
            executor.begin(cancel=cancelled)
            screen = self._backend.screen()
            self._region = Screen(screen.width, screen.height, self.config.edge_margin).safe_rect
            if not self._region.contains(self._backend.position()):
                raise InvalidTarget("Pointer starts outside the supported primary-display safe area.")
            yield result
        except BaseException as error:
            if isinstance(error, KeyboardInterrupt):
                failure = Cancelled("Mouse action interrupted.")
            elif isinstance(error, MouseError):
                failure = error
            elif isinstance(error, Exception):
                failure = BackendFailure(f"Mouse action failed: {type(error).__name__}: {error}")
            else:
                failure = error
        finally:
            if executor is not None:
                try:
                    cleaned = executor.cleanup()
                    if not cleaned:
                        result.notes.append("Emergency release failed; inspect the held button state.")
                        if failure is None:
                            failure = BackendFailure("Could not release an owned mouse button.")
                except Exception as error:
                    result.notes.append(f"Emergency release failed: {error}")
                    if failure is None: failure = BackendFailure("Could not release an owned mouse button.")
                result.actual_duration = executor.clock.now() - started
                result.events_sent = executor.events_sent
                result.down_attempted = executor.down_attempted
                result.releases_completed = executor.releases_completed
                result.cleanup_succeeded = executor.cleanup_succeeded
                result.trace = list(executor.trace)
                try: result.final_pointer = self._backend.position()
                except Exception: pass
                result.timings.update(self._timing_summary(result.trace))
            if failure is not None:
                # A driver may raise after delivering input. A dispatch attempt
                # is therefore a partial action even without confirmed delivery.
                attempted = any(e.get('event') in ('move', 'drag', 'down', 'up', 'wheel', 'cleanup_up') for e in result.trace)
                result.status = 'partial' if attempted or result.down_attempted else 'failed'
                if isinstance(failure, MouseError): failure.result = result
            if not self._preview_mode: _POINTER_LOCK.release()
        if failure is not None:
            raise failure

    @staticmethod
    def _timing_summary(trace):
        # Preserve phase boundaries: event gaps across two movements are not rates.
        moves = [event for event in trace if event.get('event') in ('move', 'drag')]
        gaps = [b['actual'] - a['actual'] for a, b in zip(moves, moves[1:])
                if a.get('move_id') == b.get('move_id') and a.get('phase') == b.get('phase')
                and 0 < b['actual'] - a['actual'] < .035]
        holds, press = [], None
        for event in trace:
            if event.get('event') == 'down': press = event
            elif event.get('event') in ('up', 'cleanup_up') and press is not None:
                holds.append(event['actual'] - press['actual'])
                press = None
        return {'movement_gap_median': statistics.median(gaps) if gaps else None,
                'movement_events': len(moves), 'button_holds': holds,
                'max_dispatch_lateness': max((e.get('lateness', 0.) for e in moves), default=0.)}

    def _region_for(self, region):
        if region is None: return self._region
        allowed = self._region.intersection(region)
        if allowed is None: raise NoWanderSpace("The requested region has no safe screen pixels.")
        if not allowed.contains(self._backend.position()):
            raise InvalidTarget("Pointer must start inside the wandering region.")
        return allowed

    def _sample(self, rect):
        point = self.sampler.target(rect, self._region)
        if self.sampler.last_target_fallback:
            self.last_result.notes.append("Small target: used its feasible integer pixels.")
        return point

    def _path(self, start, end, duration, *, straightness=1., phase='main', region=None, button=None):
        params = self.sampler.parameters(start, end, duration, straightness=straightness)
        plan = self.planner.build(start, end, duration, params, region=region or self._region, phase=phase)
        self._plans.append(plan)
        self.last_result.planned_duration += plan.duration
        started = self._executor.clock.now()
        actual = None
        try:
            self._executor.run(plan, phase=phase, button=button)
            actual = self._executor.settled_position(end)
            return actual
        finally:
            self.last_result.timings.setdefault('movements', []).append({
                'phase': phase, 'planned_duration': plan.duration,
                'actual_duration': self._executor.clock.now() - started,
                'endpoint_error': actual.distance(end) if actual is not None else None,
                'constrained': plan.constrained,
            })

    def _wait(self, seconds):
        self.last_result.planned_duration += seconds
        self._executor.wait(seconds)

    def _move(self, rect, *, final=None):
        rng, config = self.sampler.rng, self.config
        final = final or self._sample(rect)
        self.last_result.sampled_target = final
        start = self._backend.position()
        if start == final: return final
        duration = self.sampler.duration(start.distance(final), min(rect.width, rect.height))
        end = self.sampler.miss(start, final, self._region)
        actual = self._path(start, end, duration)
        for correction in range(config.max_corrections):
            if actual == final: break
            self._wait(rng.uniform(*config.reaction_range) * self.traits.time_scale)
            aim = final
            if correction == 0 and rng.random() < config.correction_miss_probability:
                candidate = Point(final.x + rng.choice((-2, -1, 1, 2)), final.y + rng.choice((-2, -1, 1, 2)))
                if self._region.contains(candidate): aim = candidate
            duration = _clamp(rng.uniform(*config.correction_range) * self.traits.time_scale, config.correction_limits)
            actual = self._path(self._backend.position(), aim, duration, straightness=3., phase='correction')
            self.last_result.correction_count += 1
        if actual != final:
            raise TargetChanged("Pointer did not arrive at the sampled target; no press was sent.")
        return actual

    def _guard(self, guard, rect, *, latest=None):
        executor = self._executor
        executor.check()
        point = self._backend.position()
        if not rect.contains(point) or not self._region.contains(point):
            raise TargetChanged("Pointer is no longer inside the intended target.")
        if guard is not None:
            deadline = executor.clock.now() + self.config.guard_budget
            if latest is not None: deadline = min(deadline, latest)
            guard.validate(point, deadline=deadline)
            if executor.clock.now() > deadline:
                raise TimingOverrun("Target validation exceeded its press eligibility deadline.")
        executor.check()
        if self._backend.position() != point:
            raise TargetChanged("Pointer changed while validating the target.")
        if latest is not None and executor.clock.now() > latest:
            raise TimingOverrun("The stop before pressing exceeded its time limit.")
        return point

    def _settle(self, rect, guard=None):
        rng, config, executor = self.sampler.rng, self.config, self._executor
        start = executor.clock.now()
        total = _clamp(rng.triangular(*config.settle_range, self.traits.settle_mean), config.settle_limits)
        if rng.random() < config.drift_probability:
            first = rng.uniform(.2, .8) * total
            self._wait(first)
            current = self._backend.position()
            dx, dy = rng.choice(((1, 0), (-1, 0), (0, 1), (0, -1)))
            drift = Point(current.x + dx, current.y + dy)
            if rect.contains(drift) and self._region.contains(drift):
                self._path(current, drift, rng.uniform(*config.drift_duration_range), straightness=3., phase='settle')
        # Validate at the end of the sampled stop so no deliberate wait separates
        # validation from down. Validation cost remains inside the hard stop bound.
        self._wait(max(0., start + total - executor.clock.now()))
        try:
            return self._guard(guard, rect, latest=start + config.settle_limits[1])
        finally:
            self.last_result.timings['settle'] = {'planned_duration': total,
                                                 'actual_duration': executor.clock.now() - start}

    def _after(self):
        rng, config = self.sampler.rng, self.config
        self._wait(rng.triangular(*config.after_range, config.after_mode) * self.traits.time_scale)
        if rng.random() < config.relax_probability:
            start = self._backend.position()
            try: end = self.sampler.wander(start, self._region, config.relax_distance)
            except NoWanderSpace: return
            actual = self._path(start, end, rng.uniform(*config.relax_duration) * self.traits.time_scale,
                                straightness=2., phase='relax')
            if actual != end:
                raise TargetChanged("The post-click relaxation did not reach its endpoint.")

    def move_to(self, target, *, cancel=None):
        rect = as_rect(target)
        with self._action('move_to', cancel): self._move(rect)
        return self.last_result

    def click(self, target, *, button='left', count=1, guard=None, cancel=None):
        self._button(button, count)
        self._validate_guard(guard)
        rect = as_rect(target)
        with self._action('click', cancel) as result:
            self._move(rect)
            result.click_point = self._settle(rect, guard)
            for index in range(count):
                if index:
                    released = self._executor.clock.now()
                    low, high = self.config.double_gap_range
                    # A live target check is part of the release-to-down gap.
                    # Reserve its budget before sampling the waiting portion;
                    # otherwise gaps near 140 ms routinely reject valid targets.
                    reserve = min(self.config.guard_budget, high - low) if guard is not None else 0.
                    self._wait(self.sampler.rng.uniform(low, high - reserve))
                    result.click_point = self._guard(guard, rect, latest=released + self.config.double_gap_range[1])
                self._executor.down(button, click_count=index + 1)
                hold = _clamp(self.sampler.rng.gauss(self.traits.hold_mean, self.config.hold_sd), self.config.hold_range)
                self._wait(hold)
                self._executor.up(button, click_count=index + 1)
            self._after()
        return self.last_result

    def double_click(self, target, **kwargs):
        return self.click(target, count=2, **kwargs)

    def right_click(self, target, **kwargs):
        return self.click(target, button='right', **kwargs)

    def random_move(self, *, region=None, distance=(20., 120.), cancel=None):
        distance = self._distance(distance)
        region = as_rect(region) if region is not None else None
        with self._action('random_move', cancel):
            allowed = self._region_for(region)
            start = self._backend.position()
            end = self.sampler.wander(start, allowed, distance)
            self.last_result.sampled_target = end
            self._path(start, end, self.sampler.duration(start.distance(end), 40.) * self.config.wander_slowdown,
                       region=allowed, phase='wander')
            if self._backend.position() != end:
                raise TargetChanged("Wandering movement did not reach its endpoint.")
        return self.last_result

    def idle(self, *, region=None, wander_probability=.2, cancel=None):
        self._probability(wander_probability)
        region = as_rect(region) if region is not None else None
        with self._action('idle', cancel):
            allowed = self._region_for(region)
            rng, config = self.sampler.rng, self.config
            self._wait(rng.triangular(*config.idle_range, config.idle_mode) * self.traits.time_scale)
            if rng.random() < wander_probability:
                start = self._backend.position()
                try: end = self.sampler.wander(start, allowed, config.wander_distance)
                except NoWanderSpace: end = start
                if end != start:
                    self.last_result.sampled_target = end
                    actual = self._path(start, end, self.sampler.duration(start.distance(end), 40.) * config.wander_slowdown,
                                        region=allowed, phase='wander')
                    if actual != end:
                        raise TargetChanged("The idle movement did not reach its endpoint.")
        return self.last_result

    def drag(self, source, destination, *, source_guard=None, destination_guard=None, button='left', cancel=None):
        self._button(button)
        self._validate_guard(source_guard)
        self._validate_guard(destination_guard)
        source, destination = as_rect(source), as_rect(destination)
        with self._action('drag', cancel) as result:
            # Validate both before any input, including approach to the source.
            final = self._sample(destination)
            self._move(source)
            result.click_point = self._settle(source, source_guard)
            self._executor.down(button)
            self._wait(self.sampler.rng.uniform(*self.config.drag_grip_range))
            start = self._backend.position()
            duration = self.sampler.duration(start.distance(final), min(destination.width, destination.height))
            duration *= self.sampler.rng.uniform(*self.config.drag_slowdown_range)
            if self._path(start, final, duration, phase='drag', button=button) != final:
                raise TargetChanged("Drag did not reach its destination; releasing the owned button.")
            self._wait(self.sampler.rng.uniform(*self.config.drag_release_range))
            self._guard(destination_guard, destination)
            result.sampled_target = final
            self._executor.up(button)
            self._after()
        return self.last_result

    def scroll(self, notches, *, over=None, guard=None, cancel=None):
        if type(notches) is not int: raise InvalidTarget("Wheel notches must be an integer.")
        self._validate_guard(guard)
        rect = as_rect(over) if over is not None else None
        if guard is not None and rect is None: raise InvalidTarget("A scroll guard requires an 'over' target.")
        with self._action('scroll', cancel):
            if notches == 0: return self.last_result
            if rect is not None:
                self._move(rect)
                self._settle(rect, guard)
            remaining, sign = abs(notches), 1 if notches > 0 else -1
            while remaining:
                burst = min(remaining, self.sampler.rng.randint(*self.config.scroll_burst_range))
                for index in range(burst):
                    if rect is not None and guard is not None: self._guard(guard, rect)
                    self._executor.wheel(sign)
                    remaining -= 1
                    if index + 1 < burst:
                        self._wait(self.sampler.rng.uniform(*self.config.scroll_step_range))
                if remaining: self._wait(self.sampler.rng.uniform(*self.config.scroll_pause_range))
        return self.last_result

    def preview(self, request, *, start, screen):
        """Simulate an action with a copied RNG and explicit display geometry.

        request is a dictionary, e.g. {'action': 'click', 'target': Rect(...)}.
        No guard is run and this diagnostic plan is never reused for execution.
        """
        if not isinstance(start, Point): start = Point(*start)
        if not isinstance(screen, Screen): screen = Screen(*screen)
        if not isinstance(request, dict): raise InvalidTarget("Preview requires an action dictionary.")
        args = dict(request)
        name = args.pop('action', None)
        methods = {'move_to', 'click', 'double_click', 'right_click', 'random_move', 'idle', 'drag', 'scroll'}
        if name not in methods: raise InvalidTarget("Unknown preview action.")
        if any(key in args for key in ('guard', 'source_guard', 'destination_guard', 'cancel')):
            raise InvalidTarget("Preview accepts geometry and behavior only, without live guards.")
        rng = random.Random()
        rng.setstate(self.sampler.rng.getstate())
        mouse = HumanMouse(backend=_PreviewBackend(start, screen), clock=_PreviewClock(),
                           config=self.config, rng=rng, traits=self.traits)
        mouse._preview_mode = True
        try: result = getattr(mouse, name)(**args)
        except TypeError as error: raise InvalidTarget(f"Invalid preview request: {error}") from error
        return MousePlan(name, tuple(mouse._plans), result.sampled_target, result.planned_duration,
                         tuple(result.notes + ['Preview only; execution will plan from live state.']))


class _PreviewClock:
    def __init__(self): self.time = 0.
    def now(self): return self.time
    def wait_until(self, deadline, check):
        check()
        self.time = max(self.time, deadline)
        check()


class _PreviewBackend:
    name = 'preview'
    def __init__(self, point, screen): self.point, self.display, self.held = point, screen, set()
    def position(self): return self.point
    def screen(self): return self.display
    def held_buttons(self): return set(self.held)
    def move(self, point): self.point = point
    def drag_move(self, point, button): self.point = point
    def button_down(self, button, click_count=1): self.held.add(button)
    def button_up(self, button, click_count=1): self.held.discard(button)
    def wheel_step(self, sign): pass
    def check_fail_safe(self): pass
    def release_owned_button(self, button): self.held.discard(button)
