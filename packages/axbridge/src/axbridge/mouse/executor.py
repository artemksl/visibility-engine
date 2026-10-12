"""Absolute-time replay with cancellation, interference checks and button cleanup."""

from __future__ import annotations

from collections import deque
import math
import time

from .models import (BackendFailure, Cancelled, MotionConfig, MouseBusy,
                     MouseError, Point, PointerInterference, TimingOverrun)


class RealClock:
    """Calibrate once when explicitly constructed, never at module import."""

    def __init__(self, *, poll_interval=.01):
        self.poll_interval = poll_interval
        oversleep = 0.0
        for _ in range(5):
            before = time.monotonic()
            time.sleep(.001)
            oversleep = max(oversleep, time.monotonic() - before - .001)
        self.sleep_margin = min(.016, max(.0015, oversleep + .0005))

    def now(self):
        return time.monotonic()

    def wait_until(self, deadline, check):
        next_check = self.now()
        while True:
            now = self.now()
            if now >= next_check:
                check()
                next_check = self.now() + self.poll_interval
            remaining = deadline - self.now()
            if remaining <= 0:
                return
            if remaining > self.sleep_margin:
                time.sleep(min(remaining - self.sleep_margin, self.poll_interval))


def _segment_distance(point, start, end):
    dx, dy = end.x - start.x, end.y - start.y
    denominator = dx * dx + dy * dy
    ratio = 0.0 if denominator == 0 else min(1.0, max(0.0,
        ((point.x - start.x) * dx + (point.y - start.y) * dy) / denominator))
    return math.hypot(point.x - start.x - ratio * dx,
                      point.y - start.y - ratio * dy)


class MotionExecutor:
    def __init__(self, backend, *, clock=None, config=MotionConfig()):
        self.backend = backend
        self.config = config
        self.clock = clock if clock is not None else RealClock(poll_interval=config.wait_poll)
        self.trace = []
        self.events_sent = 0
        self.down_attempted = 0
        self.releases_completed = 0
        self.cleanup_succeeded = None
        self.owned_buttons = set()
        self._cancel = None
        self._origin = self.clock.now()
        self._history = deque()
        self._last_sent = None
        self._outside_since = None
        self._move_id = 0

    def _call(self, method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except MouseError:
            raise
        except Exception as error:
            raise BackendFailure(f"Pointer backend {method.__name__} failed: {error}") from error

    def begin(self, cancel=None):
        self.trace.clear()
        self.events_sent = self.down_attempted = self.releases_completed = 0
        self.cleanup_succeeded = None
        self._cancel = cancel
        self._origin = self.clock.now()
        self._history.clear()
        self._outside_since = None
        self._move_id = 0
        if self.owned_buttons:
            raise MouseBusy("A previous mouse action still owns an unreleased button.")
        self._call(self.backend.check_fail_safe)
        self._check_cancel()
        held = self._call(self.backend.held_buttons)
        if held:
            raise MouseBusy(f"Cannot start while mouse buttons are already held: {', '.join(sorted(held))}.")
        position = self._call(self.backend.position)
        self._last_sent = position
        self._history.append((self.clock.now(), position))

    def _check_cancel(self):
        if self._cancel is not None:
            cancelled = self._cancel.is_set() if hasattr(self._cancel, "is_set") else self._cancel()
            if cancelled:
                raise Cancelled("Mouse action was cancelled.")

    def _observe(self):
        observed = self._call(self.backend.position)
        if not self._history:
            return observed
        now = self.clock.now()
        cutoff = now - self.config.observation_grace
        # Retain the point immediately before the window to form its first
        # segment, but do not retain an old segment when all dispatches expired.
        while len(self._history) > 1 and self._history[1][0] < cutoff:
            self._history.popleft()
        points = [point for _, point in self._history]
        if self._history[-1][0] < cutoff:
            points = points[-1:]
        if len(points) == 1:
            distance = math.hypot(observed.x - points[0].x, observed.y - points[0].y)
        else:
            distance = min(_segment_distance(observed, a, b) for a, b in zip(points, points[1:]))
        if distance <= self.config.interference_radius:
            self._outside_since = None
        elif self._outside_since is None:
            self._outside_since = now
        elif now - self._outside_since >= self.config.interference_timeout:
            raise PointerInterference("Pointer remained outside the recently dispatched path.")
        return observed

    def check(self):
        self._check_cancel()
        self._call(self.backend.check_fail_safe)
        return self._observe()

    def wait_until(self, deadline):
        self.clock.wait_until(deadline, self.check)
        self.check()

    def wait(self, seconds):
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Wait duration must be finite and nonnegative.")
        self.wait_until(self.clock.now() + seconds)

    def _record(self, event, *, phase, requested=None, point=None, **fields):
        record = {"event": event, "phase": phase, "actual": self.clock.now() - self._origin}
        if requested is not None:
            record["requested"] = requested - self._origin
            record["lateness"] = record["actual"] - record["requested"]
        if point is not None:
            record.update(x=point.x, y=point.y)
        record.update(fields)
        self.trace.append(record)
        return record

    def _deadline(self, deadline):
        self.wait_until(deadline)
        late = self.clock.now() - deadline
        if late > self.config.lateness_tolerance:
            raise TimingOverrun(f"Mouse event deadline missed by {late * 1000:.1f} ms.")

    def run(self, plan, *, phase="main", button=None):
        if self.owned_buttons and button not in self.owned_buttons:
            raise MouseBusy("Ordinary pointer movement is prohibited while a button is held.")
        if button is not None and button not in self.owned_buttons:
            raise MouseBusy("A drag requires an owned mouse button.")
        self.check()
        origin = self.clock.now()
        self._move_id += 1
        index = 0
        while index < len(plan.events):
            event = plan.events[index]
            deadline = origin + event.t
            self._deadline(deadline)
            # A short scheduler delay can make several interior samples due at
            # once. Keep the newest due sample instead of emitting a burst of
            # stale positions; a delay beyond tolerance already aborts above.
            now = self.clock.now()
            while index + 1 < len(plan.events) and origin + plan.events[index + 1].t <= now:
                self._record("skipped_deadline", phase=phase, requested=deadline,
                             point=Point(event.x, event.y), move_id=self._move_id)
                index += 1
                event = plan.events[index]
                deadline = origin + event.t
            index += 1
            point = Point(event.x, event.y)
            if point == self._last_sent:
                continue
            record = self._record("drag" if button else "move", phase=phase,
                                  requested=deadline, point=point, move_id=self._move_id)
            if button is None:
                self._call(self.backend.move, point)
            else:
                self._call(self.backend.drag_move, point, button)
            record["completed"] = self.clock.now() - self._origin
            self.events_sent += 1
            self._last_sent = point
            self._history.append((self.clock.now(), point))
        # Deduplication can eliminate the last sample; retain its resting tail.
        self._deadline(origin + plan.duration)

    def settled_position(self, expected):
        deadline = self.clock.now() + self.config.observation_grace
        while True:
            observed = self.check()
            if observed == expected or self.clock.now() >= deadline:
                return observed
            self.wait_until(min(deadline, self.clock.now() + self.config.wait_poll))

    def down(self, button, click_count=1):
        self.check()
        held = self._call(self.backend.held_buttons)
        if held or self.owned_buttons:
            raise MouseBusy("Cannot press while a mouse button is already held.")
        # Ownership must precede dispatch: an exception can follow a delivered down.
        self.owned_buttons.add(button)
        self.down_attempted += 1
        record = self._record("down", phase="press", point=self._call(self.backend.position),
                              button=button, click_count=click_count)
        self._call(self.backend.button_down, button, click_count=click_count)
        self.events_sent += 1
        record["completed"] = self.clock.now() - self._origin

    def up(self, button, click_count=1):
        self.check()
        if button not in self.owned_buttons:
            raise MouseBusy("Cannot release a button this action does not own.")
        record = self._record("up", phase="release", point=self._call(self.backend.position),
                              button=button, click_count=click_count)
        self._call(self.backend.button_up, button, click_count=click_count)
        self.events_sent += 1
        self.releases_completed += 1
        self.owned_buttons.remove(button)
        record["completed"] = self.clock.now() - self._origin

    def wheel(self, sign):
        self.check()
        if self.owned_buttons:
            raise MouseBusy("Cannot scroll while this action holds a button.")
        record = self._record("wheel", phase="scroll", sign=sign)
        self._call(self.backend.wheel_step, sign)
        self.events_sent += 1
        record["completed"] = self.clock.now() - self._origin

    def cleanup(self):
        """Attempt every owned release, including when cancel/fail-safe is set.

        Never mask the action's original exception; failure remains in state and
        the trace for the service's partial result. No unrelated release occurs.
        """
        if not self.owned_buttons:
            return True
        succeeded = True
        for button in tuple(self.owned_buttons):
            record = self._record("cleanup_up", phase="cleanup", button=button)
            try:
                self._call(self.backend.release_owned_button, button)
            except Exception as error:
                succeeded = False
                record["error"] = str(error)
            else:
                self.owned_buttons.remove(button)
                self.events_sent += 1
                self.releases_completed += 1
                record["completed"] = self.clock.now() - self._origin
        self.cleanup_succeeded = succeeded
        return succeeded
