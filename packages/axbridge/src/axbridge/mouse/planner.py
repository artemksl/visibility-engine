"""Deterministic pointer geometry. This module never reads the display or clock."""

from __future__ import annotations

import math

from .models import InvalidTarget, MotionConfig, MovePlan, PathEvent, PathParameters, Point, Rect


def min_jerk(fraction: float) -> float:
    """Distance fraction for the minimum-jerk temporal profile."""
    return fraction**3 * (10.0 + fraction * (-15.0 + 6.0 * fraction))


def min_jerk_speed(fraction: float) -> float:
    return 30.0 * fraction**2 * (1.0 - fraction) ** 2


def bezier_point(start: Point, control1: Point, control2: Point, end: Point,
                 fraction: float) -> Point:
    remainder = 1.0 - fraction
    coefficients = (remainder**3, 3 * remainder**2 * fraction,
                    3 * remainder * fraction**2, fraction**3)
    points = (start, control1, control2, end)
    return Point(sum(weight * point.x for weight, point in zip(coefficients, points)),
                 sum(weight * point.y for weight, point in zip(coefficients, points)))


def integer_bounds(region: Rect) -> tuple[int, int, int, int]:
    """Inclusive integer lattice inside a half-open rectangle."""
    bounds = (math.ceil(region.left), math.ceil(region.top),
              math.ceil(region.right) - 1, math.ceil(region.bottom) - 1)
    if bounds[0] > bounds[2] or bounds[1] > bounds[3]:
        raise InvalidTarget("The permitted region has no integer pixel")
    return bounds


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _integer_point(point: Point, region: Rect) -> None:
    if (not math.isfinite(point.x) or not math.isfinite(point.y)
            or point.x != round(point.x) or point.y != round(point.y)
            or not region.contains(point)):
        raise InvalidTarget("Movement endpoints must be integer pixels inside the permitted region")


class MotionPlanner:
    """Build immutable movement plans from fully sampled immutable parameters."""

    def __init__(self, config: MotionConfig | None = None):
        self.config = config or MotionConfig()

    def build(self, start: Point, end: Point, duration: float,
              parameters: PathParameters, *, region: Rect, phase: str = "move") -> MovePlan:
        _integer_point(start, region)
        _integer_point(end, region)
        if not math.isfinite(duration) or duration < 0:
            raise InvalidTarget("Movement duration must be finite and nonnegative")
        if start == end:
            return MovePlan(start, end, (), 0.0, phase=phase)
        if duration == 0:
            raise InvalidTarget("A nonzero movement requires a positive duration")
        if not math.isfinite(parameters.exponent) or parameters.exponent <= 0:
            raise InvalidTarget("The temporal exponent must be positive and finite")
        if not parameters.candidates:
            raise InvalidTarget("Movement parameters require at least one curve candidate")

        bounds = integer_bounds(region)
        intervals = max(8, round(duration * self.config.event_rate))
        candidates = parameters.candidates[:self.config.curve_attempts]
        for candidate in candidates:
            if len(candidate.noise) != intervals + 1 or len(candidate.jitter) != intervals + 1:
                raise InvalidTarget("Movement parameters have the wrong sample count")
            if any(not math.isfinite(value) for value in candidate.jitter):
                raise InvalidTarget("Movement jitter must be finite")
            if any(not math.isfinite(value) for point in
                   (candidate.control1, candidate.control2, *candidate.noise)
                   for value in (point.x, point.y)):
                raise InvalidTarget("Movement controls and noise must be finite")

        # There are too few pixels to express a bend or shake at this scale.
        # A monotone path avoids turning a one-pixel drift into a noisy loop.
        tiny = start.distance(end) <= 2.0
        raw = None
        selected = candidates[-1]
        constrained = False
        for selected in candidates:
            raw = self._sample(start, end, selected, parameters.exponent, intervals,
                               bounds, tiny=tiny, constrained=False)
            if all(bounds[0] <= p.x <= bounds[2] and bounds[1] <= p.y <= bounds[3]
                   for p in raw):
                break
        else:
            constrained = True
            raw = self._sample(start, end, selected, parameters.exponent, intervals,
                               bounds, tiny=tiny, constrained=True)

        times = self._times(duration, selected.jitter, intervals)
        events = []
        previous = start
        for point, timestamp in zip(raw, times):
            # Any clipping here is only floating-point roundoff after the
            # bounded curve/noise construction; samples themselves are valid.
            pixel = Point(int(_clamp(round(point.x), bounds[0], bounds[2])),
                          int(_clamp(round(point.y), bounds[1], bounds[3])))
            if pixel == previous or timestamp <= 0:
                continue
            if events and timestamp <= events[-1].t:
                continue
            events.append(PathEvent(int(pixel.x), int(pixel.y), timestamp))
            previous = pixel
        if not events or events[-1].point != end:
            # The final sample is forced to the exact endpoint and its deadline.
            # Retaining it also handles sub-microsecond synthetic test plans.
            while events and events[-1].t >= duration:
                events.pop()
            events.append(PathEvent(int(end.x), int(end.y), duration))
        return MovePlan(start, end, tuple(events), duration, phase=phase,
                        constrained=constrained)

    def _times(self, duration: float, jitter: tuple[float, ...], intervals: int) -> list[float]:
        gap = min(self.config.min_event_gap, duration / intervals)
        times = [0.0]
        for index in range(1, intervals):
            low = max(index * gap, times[-1] + gap)
            high = duration - (intervals - index) * gap
            times.append(_clamp(duration * index / intervals + jitter[index], low, high))
        times.append(duration)
        return times

    @staticmethod
    def _sample(start, end, candidate, exponent, intervals, bounds, *, tiny, constrained):
        control1, control2 = candidate.control1, candidate.control2
        if constrained:
            control1 = Point(_clamp(control1.x, bounds[0], bounds[2]),
                             _clamp(control1.y, bounds[1], bounds[3]))
            control2 = Point(_clamp(control2.x, bounds[0], bounds[2]),
                             _clamp(control2.y, bounds[1], bounds[3]))
        points = []
        for index in range(intervals + 1):
            if index == 0:
                points.append(start)
                continue
            if index == intervals:
                points.append(end)
                continue
            fraction = min_jerk((index / intervals) ** exponent)
            if tiny:
                points.append(Point(start.x + (end.x - start.x) * fraction,
                                    start.y + (end.y - start.y) * fraction))
                continue
            point = bezier_point(start, control1, control2, end, fraction)
            noise = candidate.noise[index]
            if constrained:
                # The convex hull keeps the noiseless curve inside. Scale the
                # noise vector by available local clearance instead of clipping
                # the finished curve into flat lines along a screen edge.
                scale = 1.0
                for coordinate, shake, low, high in (
                    (point.x, noise.x, bounds[0], bounds[2]),
                    (point.y, noise.y, bounds[1], bounds[3]),
                ):
                    if shake > 0:
                        scale = min(scale, max(0.0, high - coordinate) / shake)
                    elif shake < 0:
                        scale = min(scale, max(0.0, coordinate - low) / -shake)
                point = Point(_clamp(point.x, bounds[0], bounds[2]),
                              _clamp(point.y, bounds[1], bounds[3]))
                noise = Point(noise.x * scale, noise.y * scale)
            points.append(Point(point.x + noise.x, point.y + noise.y))
        return points
