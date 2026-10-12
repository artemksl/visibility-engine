"""Session traits and randomness, kept outside the deterministic planner."""

from __future__ import annotations

import math
import random

from .models import (InvalidTarget, MotionConfig, NoWanderSpace, OperatorTraits,
                     PathCandidate, PathParameters, Point, Rect)
from .planner import integer_bounds, min_jerk_speed


def _clamp(value, low, high):
    return max(low, min(high, value))


class BehaviorSampler:
    """One private RNG and one immutable operator for a mouse session.

    Production constructs ``random.Random()`` without a fixed seed. Supplying an
    RNG is a test seam; preview uses a separate sampler and cannot advance this
    session's stream.
    """

    def __init__(self, config: MotionConfig | None = None, rng=None,
                 traits: OperatorTraits | None = None):
        self.config = config or MotionConfig()
        self.rng = random.Random() if rng is None else rng
        config, rng = self.config, self.rng
        self.traits = traits or OperatorTraits(
            rng.uniform(*config.time_scale_range), rng.choice((-1, 1)),
            rng.gauss(config.hold_trait_mean, config.hold_trait_sd),
            rng.uniform(*config.settle_trait_range),
        )
        if (not math.isfinite(self.traits.time_scale) or self.traits.time_scale <= 0
                or self.traits.curve_side not in (-1, 1)
                or not math.isfinite(self.traits.hold_mean)
                or not math.isfinite(self.traits.settle_mean)
                or not config.settle_range[0] <= self.traits.settle_mean <= config.settle_range[1]):
            raise InvalidTarget("Operator traits are invalid for this motion profile")
        self.last_target_fallback = False

    def target(self, rect: Rect, region: Rect) -> Point:
        """Sample inside both the real target and the permitted pixel lattice."""
        self.last_target_fallback = False
        permitted = integer_bounds(region)
        # The guide's 15%-85% clipping interval includes both ends; the real
        # target and permitted region retain their half-open rectangle contract.
        original = integer_bounds(rect)
        bounds = (
            max(math.ceil(rect.left + rect.width * .15), permitted[0], original[0]),
            max(math.ceil(rect.top + rect.height * .15), permitted[1], original[1]),
            min(math.floor(rect.left + rect.width * .85), permitted[2], original[2]),
            min(math.floor(rect.top + rect.height * .85), permitted[3], original[3]),
        )
        try:
            if bounds[0] > bounds[2] or bounds[1] > bounds[3]:
                raise InvalidTarget("No central target pixel")
        except InvalidTarget:
            feasible = rect.intersection(region)
            if feasible is None:
                raise InvalidTarget("Target does not intersect the permitted region") from None
            bounds = integer_bounds(feasible)
            self.last_target_fallback = True
        x = self.rng.gauss(rect.left + rect.width / 2, rect.width / 6)
        y = self.rng.gauss(rect.top + rect.height / 2, rect.height / 6)
        return Point(int(_clamp(round(x), bounds[0], bounds[2])),
                     int(_clamp(round(y), bounds[1], bounds[3])))

    def duration(self, distance: float, target_size: float) -> float:
        if (not math.isfinite(distance) or distance < 0
                or not math.isfinite(target_size) or target_size <= 0):
            raise InvalidTarget("Movement distance and target size must be finite and valid")
        if distance == 0:
            return 0.0
        config = self.config
        base = config.fitts_base + config.fitts_slope * math.log2(distance / max(target_size, 4.) + 1.)
        return _clamp(base * self.rng.uniform(*config.move_random_range) * self.traits.time_scale,
                      *config.move_duration_range)

    def parameters(self, start: Point, end: Point, duration: float,
                   straightness: float = 1.) -> PathParameters:
        if (not math.isfinite(duration) or duration < 0 or not math.isfinite(straightness)
                or straightness <= 0):
            raise InvalidTarget("Duration and straightness must be finite and valid")
        config, rng = self.config, self.rng
        intervals = max(8, round(duration * config.event_rate))
        exponent = rng.uniform(*config.exponent_range)
        candidates = []
        length = start.distance(end)
        for attempt in range(config.curve_attempts):
            control1, control2 = self._controls(start, end, straightness * config.bend_reduction**attempt)
            noise, jitter = [], []
            for index in range(intervals + 1):
                if 0 < index < intervals - 1 and length > 2.:
                    fraction = (index / intervals) ** exponent
                    sigma = config.shake_base + config.shake_speed * min_jerk_speed(fraction) / 1.875
                    # Short corrections should not acquire disproportionately
                    # large loops solely from the guide's absolute noise floor.
                    sigma = min(sigma, length / 6.)
                    noise.append(Point(rng.gauss(0., sigma), rng.gauss(0., sigma)))
                else:
                    noise.append(Point(0, 0))
                jitter.append(rng.gauss(0., config.jitter_sd) if 0 < index < intervals else 0.)
            candidates.append(PathCandidate(control1, control2, tuple(noise), tuple(jitter)))
        return PathParameters(exponent, tuple(candidates))

    def _controls(self, start: Point, end: Point, straightness: float):
        rng, config = self.rng, self.config
        dx, dy = end.x - start.x, end.y - start.y
        length = math.hypot(dx, dy)
        if length == 0:
            return start, end
        nx, ny = -dy / length, dx / length
        side = self.traits.curve_side if rng.random() < config.curve_side_probability else -self.traits.curve_side
        if length < 30.:
            c1, c2 = rng.uniform(-2., 2.), rng.uniform(-2., 2.)
        else:
            c1 = side * min(config.bend1_cap, rng.uniform(*config.bend1_range) * length)
            c2 = side * min(config.bend2_cap, rng.uniform(*config.bend2_range) * length)
            if rng.random() < config.s_curve_probability:
                c2 = -c2
        c1, c2 = c1 / straightness, c2 / straightness
        a1, a2 = rng.uniform(*config.along1_range), rng.uniform(*config.along2_range)
        return (Point(start.x + a1 * dx + c1 * nx, start.y + a1 * dy + c1 * ny),
                Point(start.x + a2 * dx + c2 * nx, start.y + a2 * dy + c2 * ny))

    def miss(self, start: Point, final: Point, region: Rect) -> Point:
        """Choose direct arrival or a bounded main-movement overshoot/undershoot."""
        if not region.contains(final):
            raise InvalidTarget("Final target is outside the permitted region")
        distance = start.distance(final)
        if distance < 60.:
            return final
        config, rng = self.config, self.rng
        choice = rng.random()
        over = min(config.overshoot_probability_cap,
                   config.overshoot_base + distance / config.overshoot_distance_scale)
        under = config.undershoot_probability if distance >= 150. else 0.
        if choice < over:
            along = _clamp(rng.uniform(*config.overshoot_fraction) * distance, *config.overshoot_pixels)
        elif choice < over + under:
            along = -_clamp(rng.uniform(*config.undershoot_fraction) * distance, *config.undershoot_pixels)
        else:
            return final
        side = rng.gauss(0., config.miss_side_sd)
        ux, uy = (final.x - start.x) / distance, (final.y - start.y) / distance
        bounds = integer_bounds(region)
        return Point(int(_clamp(round(final.x + along * ux - side * uy), bounds[0], bounds[2])),
                     int(_clamp(round(final.y + along * uy + side * ux), bounds[1], bounds[3])))

    def wander(self, start: Point, region: Rect, distance=(20., 120.)) -> Point:
        """Uniformly sample a feasible integer pixel from an annulus in O(height)."""
        try:
            lower, upper = distance
        except (TypeError, ValueError):
            raise InvalidTarget("Wandering distance must be a (minimum, maximum) pair") from None
        if (not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (lower, upper))
                or lower < 0 or upper < lower or upper <= 0):
            raise InvalidTarget("Wandering distance range must be finite, ordered and nonnegative")
        if not region.contains(start) or start.x != round(start.x) or start.y != round(start.y):
            raise InvalidTarget("The current integer pointer must be inside the wandering region")
        try:
            left, top, right, bottom = integer_bounds(region)
        except InvalidTarget:
            raise NoWanderSpace("No integer pixel exists in the wandering region") from None
        rows = []
        total = 0
        for y in range(max(top, math.ceil(start.y - upper)), min(bottom, math.floor(start.y + upper)) + 1):
            dy = y - start.y
            extent = upper * math.sqrt(max(0., 1. - (dy / upper) ** 2))
            # Include one provisional boundary pixel on either side; hypot
            # below resolves sqrt roundoff, including exact diagonal radii.
            first = max(left, math.ceil(start.x - extent) - 1)
            last = min(right, math.floor(start.x + extent) + 1)
            if first > last:
                continue
            if abs(dy) < lower:
                inner = lower * math.sqrt(max(0., 1. - (dy / lower) ** 2))
                spans = ((first, min(last, math.floor(start.x - inner) + 1)),
                         (max(first, math.ceil(start.x + inner) - 1), last))
            elif dy == 0:
                # Even radius zero means a nonzero movement in this API.
                spans = ((first, min(last, int(start.x) - 1)),
                         (max(first, int(start.x) + 1), last))
            else:
                spans = ((first, last),)
            for begin, end in spans:
                # Correct sqrt rounding at each boundary using actual distance.
                while begin <= end and not lower <= math.hypot(begin - start.x, dy) <= upper:
                    begin += 1
                while end >= begin and not lower <= math.hypot(end - start.x, dy) <= upper:
                    end -= 1
                if begin <= end:
                    count = end - begin + 1
                    rows.append((y, begin, count))
                    total += count
        if total == 0:
            raise NoWanderSpace("No nonzero destination fits the wandering distance and region")
        index = self.rng.randrange(total)
        for y, begin, count in rows:
            if index < count:
                return Point(begin + index, y)
            index -= count
        raise AssertionError("Wandering interval accounting failed")
