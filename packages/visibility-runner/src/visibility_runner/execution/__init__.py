"""Execution adapters are loaded only when their public export is requested."""

__all__ = ["MeasurementGroupWorker", "RecoveryAction", "MeasurementGroupOutcome", "recovery_action"]


def __getattr__(name):
    if name in {"RecoveryAction", "recovery_action"}:
        from . import recovery
        return getattr(recovery, name)
    if name in {"MeasurementGroupWorker", "MeasurementGroupOutcome"}:
        from . import run_measurement_group
        return getattr(run_measurement_group, name)
    raise AttributeError(name)
