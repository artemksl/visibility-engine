"""Standalone, synthetic candidate v1. No browser I/O or validated human model.

Python >=3.10; standard library only. All tunings are unvalidated engineering
choices. Geometry is supplied by the driver, never inferred by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import math
from typing import Literal

EPS = 1e-9  # CSS px / numerical comparisons, not a hit-test tolerance
MIN_AREA = 1e-6  # CSS px squared: reject numerically degenerate regions
MAX_COMPONENTS = 16
MAX_VERTICES = 32
MAX_COORD = 1e6  # finite input arithmetic guard, CSS px


@dataclass(frozen=True)
class Vec:
    x: float
    y: float

    def __add__(self, b: Vec) -> Vec:
        return Vec(self.x + b.x, self.y + b.y)

    def __sub__(self, b: Vec) -> Vec:
        return Vec(self.x - b.x, self.y - b.y)

    def __mul__(self, s: float) -> Vec:
        return Vec(self.x * s, self.y * s)

    def dot(self, b: Vec) -> float:
        return self.x * b.x + self.y * b.y

    def norm(self) -> float:
        return math.hypot(self.x, self.y)


Polygon = tuple[Vec, ...]


@dataclass(frozen=True)
class Stamp:
    view: str
    document: int
    layout: int


@dataclass(frozen=True)
class PointerState:
    position: Vec | None
    provenance: Literal["unknown", "commanded", "observed"] = "unknown"
    stamp: Stamp | None = None
    exclusive_owner: bool = False


@dataclass(frozen=True)
class Gesture:
    kind: Literal["move", "hover", "click", "drag"]
    start: PointerState
    activation: Literal["locator", "raw"] = "locator"


@dataclass(frozen=True)
class Geometry:
    width: float
    height: float
    regions: tuple[Polygon, ...]  # convex usable cells, main-frame CSS coordinates
    stamp: Stamp
    observed_at_s: float  # monotonic clock used by caller
    certified_usable: bool = True
    # Driver-certified viewport origin of locator padding box, translation only.
    # None for unverified transforms/scaling/frame mappings. Never bbox alone.
    locator_origin: Vec | None = None
    prefer_locator: bool = False


@dataclass(frozen=True)
class Budget:
    now_s: float
    available_s: float
    expected_stamp: Stamp


@dataclass(frozen=True)
class Policy:
    seed: int
    stream_id: int = 0  # operation/replan index assigned explicitly by caller
    unknown_mode: Literal["fail", "relocate"] = "fail"
    allow_locator_fallback: bool = True
    target_mode: Literal["sample", "centroid"] = "sample"
    reserve_s: float = 0.150
    geometry_max_age_s: float = 0.100
    max_waypoints: int = 256
    sample_period_s: float = 0.012
    min_command_gap_s: float = 0.006
    viewport_margin_px: float = 0.5
    target_margin_px: float = 2.0
    target_margin_fraction: float = 0.15
    tiny_radius_px: float = 0.5
    center_weight: float = 0.20
    straight_probability: float = 0.25
    short_distance_px: float = 8.0
    curve_distance_fraction: float = 0.12
    curve_max_px: float = 48.0
    curve_width_factor: float = 0.5
    curve_correlation: float = 0.8
    duration_intercept_s: float = 0.070
    duration_difficulty_s: float = 0.100
    duration_distance_s_per_px: float = 0.00012
    duration_log_spread: float = 0.18
    min_travel_s: float = 0.040
    max_travel_s: float = 2.0
    arrival_probability: float = 0.25
    arrival_low_s: float = 0.020
    arrival_high_s: float = 0.070
    hold_low_s: float = 0.040
    hold_mode_s: float = 0.060
    hold_high_s: float = 0.100


class Status(str, Enum):
    PLANNED = "planned"
    ALREADY_INSIDE = "already_inside"
    UNKNOWN_START = "unknown_start"
    RELOCATE = "relocate"
    LOCATOR_ONLY = "locator_only"
    INSUFFICIENT_BUDGET = "insufficient_budget"
    INVALID_GEOMETRY = "invalid_geometry"
    STALE_GEOMETRY = "stale_geometry"
    RESOURCE_LIMIT = "resource_limit"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class Waypoint:
    x: float
    y: float
    relative_time: float


@dataclass(frozen=True)
class MovementPlan:
    status: Status
    reason: str
    stamp: Stamp
    geometry_observed_at_s: float
    seed: int
    stream_id: int
    coordinate_space: str = "main_frame_viewport_css_px"
    points: tuple[Waypoint, ...] = ()
    endpoint: Vec | None = None
    locator_position: Vec | None = None
    safe_regions: tuple[Polygon, ...] = ()
    controls: tuple[Vec, ...] = ()
    travel_s: float = 0.0
    arrival_pause_s: float = 0.0
    hold_s: float = 0.0
    reserve_s: float = 0.0
    effective_width_px: float = 0.0
    distance_px: float = 0.0
    curve_scale: float = 0.0
    start_provenance: str = "unknown"
    # points[0] is a non-dispatched start anchor for PLANNED, but a commanded
    # relocation for RELOCATE. No success/business outcome is represented here.


class Rng:
    """SHA-256 domain-separated SplitMix64, then open-interval 52-bit uniforms."""
    def __init__(self, seed: int, stream: int, label: str):
        key = f"movement-v1:{seed}:{stream}:{label}".encode("ascii")
        self.state = int.from_bytes(hashlib.sha256(key).digest()[:8], "big")

    def uniform(self) -> float:
        mask = (1 << 64) - 1
        self.state = (self.state + 0x9E3779B97F4A7C15) & mask
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & mask
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & mask
        z ^= z >> 31
        return ((z >> 12) + 0.5) / (1 << 52)

    def symmetric_triangle(self) -> float:
        return self.uniform() - self.uniform()

    def triangle(self, low: float, mode: float, high: float) -> float:
        if high == low:
            return low
        u = self.uniform()
        split = (mode - low) / (high - low)
        if u < split:
            return low + math.sqrt(u * (high - low) * (mode - low))
        return high - math.sqrt((1 - u) * (high - low) * (high - mode))


def cross(a: Vec, b: Vec) -> float:
    return a.x * b.y - a.y * b.x


def area(poly: Polygon) -> float:
    # Translate to reduce cancellation for large viewport coordinates.
    o = poly[0]
    return sum(cross(poly[i] - o, poly[i + 1] - o)
               for i in range(1, len(poly) - 1)) / 2


def centroid(poly: Polygon) -> Vec:
    o = poly[0]
    total = 0.0
    result = Vec(0, 0)
    for i in range(1, len(poly) - 1):
        a, b = poly[i] - o, poly[i + 1] - o
        w = cross(a, b)
        total += w
        result = result + (a + b) * w
    return o + result * (1 / (3 * total))


def normals(poly: Polygon) -> tuple[tuple[Vec, float], ...]:
    out = []
    for i, a in enumerate(poly):
        d = poly[(i + 1) % len(poly)] - a
        if d.norm() <= EPS:
            raise ValueError("duplicate/degenerate polygon edge")
        n = Vec(-d.y, d.x) * (1 / d.norm())
        out.append((n, n.dot(a)))  # inward: n.dot(x) >= c
    return tuple(out)


def inside(poly: Polygon, p: Vec) -> bool:
    return all(n.dot(p) >= c - EPS for n, c in normals(poly))


def clip(poly: Polygon, n: Vec, c: float) -> Polygon:
    if not poly:
        return ()
    result: list[Vec] = []
    for i, b in enumerate(poly):
        a = poly[i - 1]
        da, db = n.dot(a) - c, n.dot(b) - c
        if (da >= 0) != (db >= 0):
            result.append(a + (b - a) * (da / (da - db)))
        if db >= 0:
            result.append(b)
    clean: list[Vec] = []
    for v in result:
        if not clean or (v - clean[-1]).norm() > EPS:
            clean.append(v)
    if len(clean) > 1 and (clean[0] - clean[-1]).norm() <= EPS:
        clean.pop()
    return tuple(clean)


def rectangle(x: float, y: float, w: float, h: float) -> Polygon:
    return (Vec(x, y), Vec(x + w, y), Vec(x + w, y + h), Vec(x, y + h))


def safe_regions(g: Geometry, p: Policy) -> tuple[Polygon, ...]:
    result = []
    m = p.viewport_margin_px
    bounds = ((Vec(1, 0), m), (Vec(-1, 0), -g.width + m),
              (Vec(0, 1), m), (Vec(0, -1), -g.height + m))
    for original in g.regions:
        if len(original) < 3 or any(not finite_vec(v) for v in original):
            raise ValueError("invalid vertices")
        if any((a - b).norm() <= EPS for i, a in enumerate(original)
               for b in original[i + 1:]):
            raise ValueError("repeated polygon vertex")
        if area(original) <= MIN_AREA:
            raise ValueError("polygon must have positive area and CCW winding")
        planes = normals(original)
        if any(n.dot(v) < c - EPS for n, c in planes for v in original):
            raise ValueError("polygon must be convex and correctly ordered")
        q = original
        for n, c in bounds:
            q = clip(q, n, c)
        if len(q) < 3 or area(q) <= MIN_AREA:
            continue
        center = centroid(q)
        planes = normals(q)
        radius = min(n.dot(center) - c for n, c in planes)
        margin = min(p.target_margin_px, p.target_margin_fraction * radius)
        for n, c in planes:
            q = clip(q, n, c + margin)
        if len(q) >= 3 and area(q) > MIN_AREA:
            result.append(q)
    return tuple(result)


def finite_vec(v: Vec) -> bool:
    return all(math.isfinite(x) and abs(x) <= MAX_COORD for x in (v.x, v.y))


def uniform_area(poly: Polygon, rng: Rng) -> Vec:
    weights = [cross(poly[i] - poly[0], poly[i + 1] - poly[0]) / 2
               for i in range(1, len(poly) - 1)]
    k = weighted_index(weights, rng.uniform()) + 1
    r, v = math.sqrt(rng.uniform()), rng.uniform()
    return poly[0] * (1 - r) + poly[k] * (r * (1 - v)) + poly[k + 1] * (r * v)


def weighted_index(weights: list[float], u: float) -> int:
    threshold = u * sum(weights)
    total = 0.0
    for i, w in enumerate(weights):
        total += w
        if threshold < total:
            return i
    return len(weights) - 1


def chord_width(poly: Polygon, endpoint: Vec, direction: Vec) -> float:
    lo, hi = -math.inf, math.inf
    for n, c in normals(poly):
        a, b = n.dot(direction), c - n.dot(endpoint)
        if a > EPS:
            lo = max(lo, b / a)
        elif a < -EPS:
            hi = min(hi, b / a)
    return max(0.0, hi - lo)


def validate_policy(p: Policy) -> None:
    """Bad configuration raises ValueError; runtime limitations return statuses."""
    if type(p.seed) is not int or not 0 <= p.seed < 2**64:
        raise ValueError("seed must be a uint64")
    if type(p.stream_id) is not int or not 0 <= p.stream_id < 2**64:
        raise ValueError("stream_id must be a uint64")
    if p.unknown_mode not in ("fail", "relocate") or p.target_mode not in ("sample", "centroid"):
        raise ValueError("unknown policy mode")
    if type(p.max_waypoints) is not int or not 2 <= p.max_waypoints <= 256:
        raise ValueError("waypoint cap must be 2..256")
    excluded = {"seed", "stream_id", "max_waypoints", "unknown_mode",
                "allow_locator_fallback", "target_mode"}
    values = [v for k, v in vars(p).items() if k not in excluded]
    if not all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in values):
        raise ValueError("policy numbers must be finite/nonnegative")
    if type(p.allow_locator_fallback) is not bool:
        raise ValueError("allow_locator_fallback must be bool")
    for k in ("center_weight", "straight_probability", "curve_correlation", "arrival_probability"):
        if not 0 <= getattr(p, k) <= 1:
            raise ValueError(k + " outside [0,1]")
    if not 0 <= p.target_margin_fraction <= .5:
        raise ValueError("margin fraction outside [0,.5]")
    if not (.001 <= p.min_command_gap_s <= p.sample_period_s <= .100):
        raise ValueError("require .001 <= min gap <= period <= .100 seconds")
    if not (.001 <= p.min_travel_s <= p.max_travel_s <= 2.0):
        raise ValueError("require .001 <= min travel <= max travel <= 2 seconds")
    if not (0 <= p.hold_low_s <= p.hold_mode_s <= p.hold_high_s <= .5):
        raise ValueError("invalid hold range, cap .5 seconds")
    if not (0 <= p.arrival_low_s <= p.arrival_high_s <= .5):
        raise ValueError("invalid arrival range, cap .5 seconds")
    if p.reserve_s > 2 or p.geometry_max_age_s > 1 or p.duration_log_spread > 1:
        raise ValueError("reserve/age/log spread exceeds hard limit")
    for k in ("viewport_margin_px", "target_margin_px", "tiny_radius_px", "short_distance_px", "curve_max_px"):
        if getattr(p, k) > 100:
            raise ValueError(k + " exceeds 100 px")
    for k in ("curve_distance_fraction", "curve_width_factor"):
        if getattr(p, k) > 1:
            raise ValueError(k + " exceeds 1")
    if p.duration_intercept_s > 2 or p.duration_difficulty_s > 2 or p.duration_distance_s_per_px > .01:
        raise ValueError("duration coefficient exceeds hard limit")


class MovementStrategy:
    def plan(self, gesture: Gesture, geometry: Geometry, policy: Policy,
             budget: Budget) -> MovementPlan:
        g, p, b = geometry, policy, budget
        validate_policy(p)
        if not all(math.isfinite(v) for v in (b.now_s, b.available_s, g.observed_at_s)):
            raise ValueError("clock/budget must be finite")

        def result(status: Status, reason: str, **kw) -> MovementPlan:
            return MovementPlan(status, reason, g.stamp, g.observed_at_s, p.seed,
                                p.stream_id, reserve_s=p.reserve_s,
                                start_provenance=gesture.start.provenance, **kw)

        if b.available_s <= 0:
            return result(Status.INSUFFICIENT_BUDGET, "deadline exhausted")
        if gesture.kind not in ("move", "hover", "click", "drag") or gesture.activation not in ("locator", "raw"):
            return result(Status.UNSUPPORTED, "unsupported gesture/activation")
        if g.stamp != b.expected_stamp or not 0 <= b.now_s - g.observed_at_s <= p.geometry_max_age_s:
            return result(Status.STALE_GEOMETRY, "generation mismatch or stale snapshot")
        if not all(math.isfinite(v) and 0 < v <= MAX_COORD for v in (g.width, g.height)):
            return result(Status.INVALID_GEOMETRY, "invalid viewport")
        if len(g.regions) > MAX_COMPONENTS or any(len(q) > MAX_VERTICES for q in g.regions):
            return result(Status.RESOURCE_LIMIT, "geometry complexity exceeds caps")

        pause_rng = Rng(p.seed, p.stream_id, "pause")
        hold_rng = Rng(p.seed, p.stream_id, "hold")
        pause = (p.arrival_low_s + (p.arrival_high_s - p.arrival_low_s) * pause_rng.uniform()
                 if gesture.kind == "click" and pause_rng.uniform() < p.arrival_probability else 0.0)
        hold = (hold_rng.triangle(p.hold_low_s, p.hold_mode_s, p.hold_high_s)
                if gesture.kind == "click" else 0.0)
        overhead = pause + hold + p.reserve_s
        if b.available_s < overhead:
            return result(Status.INSUFFICIENT_BUDGET, "pause/hold/reserve do not fit")

        def locator_fallback(reason: str) -> MovementPlan:
            if p.allow_locator_fallback and gesture.activation == "locator" and gesture.kind in ("click", "hover"):
                return result(Status.LOCATOR_ONLY, reason, hold_s=hold)
            return result(Status.INVALID_GEOMETRY, reason)

        # Non-activating move may use an in-viewport guidance region. Hover and
        # click still require driver-certified target usability/hit geometry.
        if g.prefer_locator or (not g.certified_usable and gesture.kind != "move"):
            return locator_fallback("ordinary locator requested or usable region uncertified")
        if gesture.kind == "click" and gesture.activation == "locator" and g.locator_origin is None:
            return locator_fallback("no certified locator padding-box translation")
        if g.locator_origin is not None and not finite_vec(g.locator_origin):
            return result(Status.INVALID_GEOMETRY, "invalid locator origin")
        try:
            regions = safe_regions(g, p)
        except ValueError as exc:
            return result(Status.INVALID_GEOMETRY, str(exc))
        if not regions:
            return result(Status.INVALID_GEOMETRY, "no usable region after clipping/erosion")
        s = gesture.start
        known = (s.position is not None and finite_vec(s.position) and
                 s.provenance in ("commanded", "observed") and s.exclusive_owner and
                 s.stamp == g.stamp and 0 <= s.position.x < g.width and 0 <= s.position.y < g.height)
        # Do not draw an unnecessary endpoint or move when already safely inside.
        if known and any(inside(poly, s.position) for poly in regions):
            end = s.position
            local = end - g.locator_origin if g.locator_origin is not None else None
            if local is not None and (local.x < 0 or local.y < 0):
                return locator_fallback("certified region inconsistent with locator origin")
            return result(Status.ALREADY_INSIDE, "pointer already in safe target", endpoint=end,
                          locator_position=local, safe_regions=regions, arrival_pause_s=pause, hold_s=hold)
        target_rng = Rng(p.seed, p.stream_id, "target")
        k = weighted_index([area(q) for q in regions], target_rng.uniform())
        q = regions[k]
        center = centroid(q)
        radius = min(n.dot(center) - c for n, c in normals(q))
        if radius < p.tiny_radius_px:
            return locator_fallback("selected safe region too narrow for custom placement")
        if p.target_mode == "centroid":
            end = center
        else:
            average = (uniform_area(q, target_rng) + uniform_area(q, target_rng)) * .5
            end = center * p.center_weight + average * (1 - p.center_weight)
        local = end - g.locator_origin if g.locator_origin is not None else None
        if local is not None and (local.x < 0 or local.y < 0):
            return locator_fallback("certified region inconsistent with locator origin")
        common = dict(endpoint=end, locator_position=local, safe_regions=regions,
                      arrival_pause_s=pause, hold_s=hold)
        if not known:
            if p.unknown_mode == "relocate" and gesture.kind != "drag":
                return result(Status.RELOCATE, "one explicit relocation; previous path unknown",
                              points=(Waypoint(end.x, end.y, 0.0),), **common)
            return result(Status.UNKNOWN_START, "no owned current position; no invented (0,0) origin", **common)

        start = s.position
        delta = end - start
        distance = delta.norm()
        if distance <= EPS:
            return result(Status.ALREADY_INSIDE, "zero distance", **common)
        e = delta * (1 / distance)
        n = Vec(-e.y, e.x)
        width = chord_width(q, end, e)
        nominal = (p.duration_intercept_s + p.duration_difficulty_s * math.log2(1 + distance / max(1.0, width))
                   + p.duration_distance_s_per_px * distance)
        duration_rng = Rng(p.seed, p.stream_id, "duration")
        travel = min(p.max_travel_s, max(p.min_travel_s,
                     nominal * math.exp(p.duration_log_spread * duration_rng.symmetric_triangle())))
        if travel + overhead > b.available_s:
            return result(Status.INSUFFICIENT_BUDGET, "sampled travel does not fit; never compress to catch up")
        curve_rng = Rng(p.seed, p.stream_id, "curve")
        alpha, beta = .25 + .15 * curve_rng.uniform(), .60 + .15 * curve_rng.uniform()
        h1 = h2 = 0.0
        if distance >= p.short_distance_px and curve_rng.uniform() >= p.straight_probability:
            cap = min(p.curve_max_px, p.curve_distance_fraction * distance,
                      p.curve_width_factor * math.sqrt(distance * width))
            z1, z2 = curve_rng.symmetric_triangle(), curve_rng.symmetric_triangle()
            h1 = cap * z1
            h2 = cap * (p.curve_correlation * z1 + (1 - p.curve_correlation) * z2)
        scale = 0.0
        controls: tuple[Vec, ...] = ()
        for attempt in range(9):  # 1, 1/2, ..., 1/128; then exact straight line
            scale = 2.0 ** (-attempt) if attempt < 8 else 0.0
            c1 = start + e * (alpha * distance) + n * (scale * h1)
            c2 = start + e * (beta * distance) + n * (scale * h2)
            if all(0 <= v.x < g.width and 0 <= v.y < g.height for v in (c1, c2)):
                controls = (start, c1, c2, end)
                break
        if not controls:
            return result(Status.INVALID_GEOMETRY, "no bounded curve")
        count = max(1, math.ceil(travel / p.sample_period_s))
        # Weights are >=.75; this bound preserves minimum commanded spacing.
        count = min(count, max(1, math.floor(.75 * travel / p.min_command_gap_s)))
        if travel < p.min_command_gap_s or count + 1 > p.max_waypoints:
            return result(Status.RESOURCE_LIMIT, "sampling/minimum command spacing cannot fit caps")
        phase = 2 * math.pi * Rng(p.seed, p.stream_id, "schedule").uniform()
        weights = [1 + .25 * math.sin(2 * math.pi * (i + .5) / count + phase)
                   for i in range(count)]
        total = sum(weights)
        cumulative = 0.0
        times = [0.0]
        for w in weights:
            cumulative += w
            times.append(travel * cumulative / total)
        times[-1] = travel
        points_list = []
        for t in times:
            xy = position(controls, travel, t)
            points_list.append(Waypoint(xy.x, xy.y, t))
        points = tuple(points_list)
        return result(Status.PLANNED, "bounded candidate path", points=points, controls=controls,
                      travel_s=travel, effective_width_px=width, distance_px=distance,
                      curve_scale=scale, **common)


def kinematics(controls: tuple[Vec, ...], travel_s: float, t: float) -> tuple[Vec, Vec, Vec]:
    """Analytic intended continuous curve, velocity (px/s), acceleration (px/s²)."""
    if len(controls) != 4 or travel_s <= 0:
        raise ValueError("kinematics requires a planned curve")
    u = max(0.0, min(1.0, t / travel_s))
    q = u**3 * (10 + u * (-15 + 6 * u))
    dq = 30 * u*u * (1-u)**2 / travel_s
    ddq = 60 * u * (1-u) * (1-2*u) / travel_s**2
    a, b, c, d = controls
    v = 1 - q
    pos = a * v**3 + b * (3*v*v*q) + c * (3*v*q*q) + d * q**3
    first = (b-a)*(3*v*v) + (c-b)*(6*v*q) + (d-c)*(3*q*q)
    second = (c-b*2+a)*(6*v) + (d-c*2+b)*(6*q)
    return pos, first * dq, second * (dq*dq) + first * ddq


def position(controls: tuple[Vec, ...], travel_s: float, t: float) -> Vec:
    return kinematics(controls, travel_s, t)[0]
