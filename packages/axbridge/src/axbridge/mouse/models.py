"""Display-independent contracts for motion planning and execution."""

from dataclasses import asdict, dataclass, field
import math
from typing import Callable, Protocol

from ..models import BridgeError


class MouseError(BridgeError):
    """A failed action; result describes any input already sent."""

    def __init__(self, message, *, result=None):
        super().__init__(message)
        self.result = result


class InvalidTarget(MouseError): pass
class TargetChanged(MouseError): pass
class TargetNotExposed(MouseError): pass
class NoWanderSpace(MouseError): pass
class MouseBusy(MouseError): pass
class Cancelled(MouseError): pass
class FailSafeTriggered(MouseError): pass
class TimingOverrun(MouseError): pass
class BackendFailure(MouseError): pass
class PointerInterference(MouseError): pass


@dataclass(frozen=True)
class Point:
    x: float
    y: float

    def __post_init__(self):
        if not all(isinstance(v, (float, int)) and math.isfinite(v) for v in (self.x, self.y)):
            raise InvalidTarget("Point coordinates must be finite numbers.")

    def as_tuple(self):
        return (self.x, self.y)

    def __iter__(self):
        return iter(self.as_tuple())

    def distance(self, other):
        return math.hypot(self.x - other.x, self.y - other.y)


@dataclass(frozen=True)
class Rect:
    left: float
    top: float
    width: float
    height: float

    def __post_init__(self):
        values = (self.left, self.top, self.width, self.height)
        if not all(isinstance(v, (float, int)) and math.isfinite(v) for v in values):
            raise InvalidTarget("Rectangle coordinates must be finite numbers.")
        if self.width <= 0 or self.height <= 0 or not all(math.isfinite(v) for v in (self.right, self.bottom)):
            raise InvalidTarget("Rectangle dimensions must be positive and finite.")

    @property
    def right(self): return self.left + self.width

    @property
    def bottom(self): return self.top + self.height

    def contains(self, point):
        return self.left <= point.x < self.right and self.top <= point.y < self.bottom

    def integer_bounds(self):
        return (math.ceil(self.left), math.ceil(self.top), math.ceil(self.right) - 1, math.ceil(self.bottom) - 1)

    def intersection(self, other):
        left, top = max(self.left, other.left), max(self.top, other.top)
        right, bottom = min(self.right, other.right), min(self.bottom, other.bottom)
        return Rect(left, top, right - left, bottom - top) if right > left and bottom > top else None

    def as_tuple(self): return (self.left, self.top, self.width, self.height)


def as_rect(target):
    if isinstance(target, Rect): return target
    if isinstance(target, Point): return Rect(target.x - 4, target.y - 4, 8, 8)
    try:
        values = tuple(target)
        if len(values) == 2: return as_rect(Point(*values))
        if len(values) == 4: return Rect(*values)
    except (TypeError, ValueError):
        pass
    raise InvalidTarget("Use a Point, Rect, (x, y), or (left, top, width, height).")


@dataclass(frozen=True)
class Screen:
    width: int
    height: int
    edge_margin: int = 2

    def __post_init__(self):
        if any(type(v) is not int for v in (self.width, self.height, self.edge_margin)) or self.edge_margin < 0:
            raise InvalidTarget("Screen size and margin must be integers.")
        if min(self.width, self.height) <= 2 * self.edge_margin:
            raise InvalidTarget("No supported primary display is accessible.")

    @property
    def safe_rect(self):
        return Rect(self.edge_margin, self.edge_margin, self.width - 2*self.edge_margin, self.height - 2*self.edge_margin)


@dataclass(frozen=True)
class OperatorTraits:
    time_scale: float
    curve_side: int
    hold_mean: float
    settle_mean: float


@dataclass(frozen=True)
class MotionConfig:
    event_rate: float = 125.0
    edge_margin: int = 2
    lateness_tolerance: float = .250
    guard_budget: float = .05
    observation_grace: float = .05
    interference_radius: float = 3.0
    interference_timeout: float = .05
    wait_poll: float = .01
    time_scale_range: tuple = (.85, 1.20)
    hold_trait_mean: float = .090
    hold_trait_sd: float = .012
    settle_trait_range: tuple = (.10, .18)
    fitts_base: float = .12
    fitts_slope: float = .13
    move_random_range: tuple = (.85, 1.30)
    move_duration_range: tuple = (.15, 1.60)
    exponent_range: tuple = (.85, 1.0)
    curve_side_probability: float = .7
    s_curve_probability: float = .2
    bend1_range: tuple = (.06, .22)
    bend2_range: tuple = (.02, .12)
    bend1_cap: float = 140.
    bend2_cap: float = 80.
    along1_range: tuple = (.20, .40)
    along2_range: tuple = (.60, .85)
    shake_base: float = .25
    shake_speed: float = .9
    jitter_sd: float = .0012
    min_event_gap: float = .0005
    curve_attempts: int = 4
    bend_reduction: float = 1.6
    overshoot_base: float = .12
    overshoot_distance_scale: float = 2000.
    overshoot_probability_cap: float = .45
    undershoot_probability: float = .12
    overshoot_fraction: tuple = (.015, .04)
    overshoot_pixels: tuple = (3., 18.)
    undershoot_fraction: tuple = (.03, .08)
    undershoot_pixels: tuple = (5., 30.)
    miss_side_sd: float = 2.
    reaction_range: tuple = (.04, .12)
    correction_range: tuple = (.09, .20)
    correction_limits: tuple = (.06, .30)
    max_corrections: int = 2
    correction_miss_probability: float = .2
    settle_range: tuple = (.06, .30)
    settle_limits: tuple = (.05, .35)
    drift_probability: float = .25
    drift_duration_range: tuple = (.004, .008)
    hold_sd: float = .02
    hold_range: tuple = (.05, .16)
    double_gap_range: tuple = (.07, .14)
    after_range: tuple = (.12, .90)
    after_mode: float = .30
    relax_probability: float = .3
    relax_distance: tuple = (5., 25.)
    relax_duration: tuple = (.12, .25)
    drag_grip_range: tuple = (.10, .25)
    drag_slowdown_range: tuple = (1.3, 1.8)
    drag_release_range: tuple = (.05, .15)
    scroll_burst_range: tuple = (1, 4)
    scroll_step_range: tuple = (.012, .035)
    scroll_pause_range: tuple = (.15, .60)
    idle_range: tuple = (.4, 4.)
    idle_mode: float = 1.2
    wander_probability: float = .2
    wander_distance: tuple = (20., 120.)
    wander_slowdown: float = 1.4

    def __post_init__(self):
        for key, value in vars(self).items():
            values = value if isinstance(value, tuple) else (value,)
            if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in values):
                raise ValueError(f"Invalid motion configuration: {key}")
            if isinstance(value, tuple) and (len(value) != 2 or value[0] > value[1]):
                raise ValueError(f"Invalid motion range: {key}")
        if min(self.event_rate, self.wait_poll, self.min_event_gap, self.overshoot_distance_scale) <= 0:
            raise ValueError("Rates and timing intervals must be positive.")
        if type(self.max_corrections) is not int or self.max_corrections not in (0, 1, 2) or type(self.curve_attempts) is not int or not 1 <= self.curve_attempts <= 4:
            raise ValueError("Use at most two corrections and one to four curve attempts.")
        for key in ('time_scale_range', 'move_duration_range', 'move_random_range', 'exponent_range', 'correction_range', 'correction_limits', 'drift_duration_range', 'hold_range', 'double_gap_range', 'relax_duration', 'drag_slowdown_range'):
            if getattr(self, key)[0] <= 0: raise ValueError(f"Motion range must be positive: {key}")
        if self.bend_reduction < 1 or type(self.edge_margin) is not int or self.edge_margin < 2:
            raise ValueError("Keep at least a two-pixel margin and a bend reduction of at least one.")
        if any(type(v) is not int for v in self.scroll_burst_range) or self.scroll_burst_range[0] < 1:
            raise ValueError("Scroll bursts must contain positive integer steps.")
        for mode, limits in ((self.after_mode, self.after_range), (self.idle_mode, self.idle_range)):
            if not limits[0] <= mode <= limits[1]: raise ValueError("Triangular modes must lie inside their range.")
        for key in ('curve_side_probability', 's_curve_probability', 'overshoot_probability_cap', 'undershoot_probability', 'correction_miss_probability', 'drift_probability', 'relax_probability', 'wander_probability'):
            if getattr(self, key) > 1: raise ValueError(f"Invalid probability: {key}")


@dataclass(frozen=True)
class PathEvent:
    x: int
    y: int
    t: float

    @property
    def point(self): return Point(self.x, self.y)


@dataclass(frozen=True)
class PathCandidate:
    control1: Point
    control2: Point
    noise: tuple[Point, ...]
    jitter: tuple[float, ...]


@dataclass(frozen=True)
class PathParameters:
    exponent: float
    candidates: tuple[PathCandidate, ...]


@dataclass(frozen=True)
class MovePlan:
    start: Point
    end: Point
    events: tuple[PathEvent, ...]
    duration: float
    phase: str = 'move'
    constrained: bool = False


@dataclass(frozen=True)
class MousePlan:
    action: str
    moves: tuple[MovePlan, ...]
    sampled_target: Point | None
    duration: float
    notes: tuple[str, ...] = ()

    def to_dict(self): return asdict(self)


@dataclass
class MouseResult:
    action: str
    status: str = 'completed'
    sampled_target: Point | None = None
    click_point: Point | None = None
    final_pointer: Point | None = None
    planned_duration: float = 0.
    actual_duration: float = 0.
    correction_count: int = 0
    events_sent: int = 0
    backend: str = ''
    down_attempted: int = 0
    releases_completed: int = 0
    cleanup_succeeded: bool | None = None
    timings: dict = field(default_factory=dict)
    trace: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self, *, include_trace=False):
        result = asdict(self)
        if not include_trace: result.pop('trace')
        return result


class PressGuard(Protocol):
    def validate(self, point: Point, *, deadline: float) -> None: ...


class PointerBackend(Protocol):
    name: str
    def position(self) -> Point: ...
    def screen(self) -> Screen: ...
    def held_buttons(self) -> set[str]: ...
    def move(self, point: Point) -> None: ...
    def drag_move(self, point: Point, button: str) -> None: ...
    def button_down(self, button: str, click_count: int = 1) -> None: ...
    def button_up(self, button: str, click_count: int = 1) -> None: ...
    def wheel_step(self, sign: int) -> None: ...
    def check_fail_safe(self) -> None: ...
    def release_owned_button(self, button: str) -> None: ...


class Clock(Protocol):
    def now(self) -> float: ...
    def wait_until(self, deadline: float, check: Callable[[], None]) -> None: ...
