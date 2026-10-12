"""Human-style motion with deterministic planning and replaceable input drivers."""

from .models import (
    BackendFailure, Cancelled, FailSafeTriggered, InvalidTarget, MotionConfig,
    MouseBusy, MouseError, MousePlan, MouseResult, NoWanderSpace, OperatorTraits,
    Point, PointerInterference, Rect, Screen, TargetChanged, TargetNotExposed,
    TimingOverrun,
)


def __getattr__(name):
    if name == 'HumanMouse':
        from .service import HumanMouse
        return HumanMouse
    raise AttributeError(name)


__all__ = ['HumanMouse', 'Point', 'Rect', 'Screen', 'MotionConfig', 'OperatorTraits',
           'MousePlan', 'MouseResult', 'MouseError', 'InvalidTarget', 'TargetChanged',
           'TargetNotExposed', 'NoWanderSpace', 'MouseBusy', 'Cancelled',
           'FailSafeTriggered', 'TimingOverrun', 'BackendFailure', 'PointerInterference']
