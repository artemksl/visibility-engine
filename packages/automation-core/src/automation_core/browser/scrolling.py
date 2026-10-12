"""One admitted scroll effect followed by bounded, read-only layout checks.

The driver owns browser I/O and thread/lease checks. These runtime records contain
only ephemeral geometry; they must never enter the durable action codec. A returned
input call is preserved even when the requested layout was not subsequently seen.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal

from automation_core.contracts import TargetRef


@dataclass(frozen=True)
class ScrollIntent:
    """Relative CSS-pixel offsets in an explicit container or the bound document.

    Offset mode changes only that scroll scope. Wheel mode additionally requires
    the driver to certify a previously commanded pointer and wheel routing; it
    never invents or relocates a pointer to make the request dispatchable.
    """

    delta_x: float = 0.0
    delta_y: float = 0.0
    scope: TargetRef | None = None
    mode: Literal["offset", "wheel"] = "offset"


@dataclass(frozen=True)
class ScrollLayout:
    """Read-only scoped offsets and optional target bounds in viewport CSS px."""

    x: float
    y: float
    max_x: float
    max_y: float
    viewport_width: float
    viewport_height: float
    target_rect: tuple[float, float, float, float] | None
    target_in_view: bool | None
    observed_at: float


def _finite(value) -> bool:
    try:
        return (not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value))
    except OverflowError:
        return False


def _layout_key(layout: ScrollLayout) -> tuple:
    return (layout.x, layout.y, layout.max_x, layout.max_y,
            layout.viewport_width, layout.viewport_height,
            layout.target_rect, layout.target_in_view)


class ScrollController:
    """Execute once; observe up to 100 layout samples without retrying input."""

    def __init__(self, driver):
        self.driver = driver

    @staticmethod
    def _error(reason):
        # Import at use time: interaction composes this controller and owns the
        # one sanitized browser-error vocabulary.
        from automation_core.browser.interaction import InteractionError
        return InteractionError(reason)

    def _guard(self, deadline, state) -> None:
        if state["reason"] is not None:
            raise self._error(state["reason"])
        self.driver._guard(deadline)

    def _observe(self, target, scope, binding, deadline) -> ScrollLayout:
        layout = self.driver.scroll_layout(target, scope, binding, deadline)
        if type(layout) is not ScrollLayout:
            raise self._error("invalid_scroll_observation")
        numbers = (layout.x, layout.y, layout.max_x, layout.max_y,
                   layout.viewport_width, layout.viewport_height, layout.observed_at)
        if (not all(_finite(number) for number in numbers)
                or min(layout.x, layout.y, layout.max_x, layout.max_y) < 0
                or layout.x > layout.max_x + 1 or layout.y > layout.max_y + 1
                or min(layout.viewport_width, layout.viewport_height) <= 0):
            raise self._error("invalid_scroll_observation")
        if target is None:
            if layout.target_rect is not None or layout.target_in_view is not None:
                raise self._error("invalid_scroll_observation")
        elif (type(layout.target_rect) is not tuple or len(layout.target_rect) != 4
                or not all(_finite(number) for number in layout.target_rect)
                or min(layout.target_rect[2:]) < 0
                or type(layout.target_in_view) is not bool):
            raise self._error("invalid_scroll_observation")
        return layout

    def execute(self, request, binding, deadline: float, state: dict) -> None:
        """Use the executor's existing input lease and technical receipt builder.

        Target-only requests reveal a unique locator. Intent requests issue one
        scoped relative scroll; an optional target is observed without implying
        that the offsets were intended to reveal it. Completion here is only
        stable observed layout, never an application-level success claim.
        """
        driver = self.driver
        self._guard(deadline, state)
        target, intent = request.target, request.scroll
        if target is not None and type(target) is not TargetRef:
            raise self._error("invalid_scroll_request")
        if intent is None:
            if target is None:
                raise self._error("scroll_target_or_intent_required")
            scope = None
        else:
            if (type(intent) is not ScrollIntent
                    or not _finite(intent.delta_x) or not _finite(intent.delta_y)
                    or type(intent.mode) is not str or intent.mode not in {"offset", "wheel"}
                    or (intent.scope is not None and type(intent.scope) is not TargetRef)
                    or (intent.delta_x == 0 and intent.delta_y == 0)):
                raise self._error("invalid_scroll_request")
            scope = intent.scope

        before = self._observe(target, scope, binding, deadline)
        state["scroll"] = (before,)
        self._guard(deadline, state)
        if intent is not None and intent.mode == "wheel":
            # Certification is a read-only preparation step. Unsupported wheel
            # routing therefore stops before any effect call is entered.
            driver.prepare_scroll_wheel(scope, binding, intent.delta_x,
                                        intent.delta_y, deadline)
        self._guard(deadline, state)
        # The driver marks entry after its final preparation checks, immediately
        # before the effect. A refused locator/guard is still not attempted.
        if intent is None:
            driver.scroll_into_view(target, deadline, state)
        elif intent.mode == "offset":
            driver.scroll_by(scope, binding, intent.delta_x, intent.delta_y, deadline, state)
        else:
            driver.scroll_wheel(scope, binding, intent.delta_x, intent.delta_y, deadline, state)
        state.update(status="returned", returned_at=driver.clock.monotonic())

        expected = None
        if intent is not None:
            expected = (min(before.max_x, max(0.0, before.x + intent.delta_x)),
                        min(before.max_y, max(0.0, before.y + intent.delta_y)))
        stable_key = None
        stable_since = None
        for sample in range(100):
            # Completion reads may preserve available evidence after cancellation;
            # the normal guard below then prevents another wait/input progression.
            after = self._observe(target, scope, binding, deadline)
            state["scroll"] = (before, after)
            self._guard(deadline, state)
            key = _layout_key(after)
            if key != stable_key:
                stable_key, stable_since = key, after.observed_at
            requested_seen = (after.target_in_view if expected is None else
                              abs(after.x - expected[0]) <= 1.0
                              and abs(after.y - expected[1]) <= 1.0)
            if requested_seen and after.observed_at - stable_since >= 0.020:
                return
            if sample < 99:
                driver.pump_wait(0.005, deadline)
        raise self._error("scroll_layout_not_observed")
