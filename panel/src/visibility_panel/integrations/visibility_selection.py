"""Validate exact diagnostic prompt batches through the runner public facade."""
from collections.abc import Mapping
from visibility_panel.execution.measurement_platforms import require_active_measurement
from visibility_panel.integrations.visibility_runner import coerce_measurement_config


def measurement_from_frozen_snapshot(frozen):
    if not isinstance(frozen, Mapping) or frozen.get('scope_kind') != 'diagnostic':
        raise ValueError('This panel accepts visibility diagnostic prompt batches only.')
    value = dict(frozen)
    for name in ('runs', 'test_mode'):
        value.pop(name, None)
    require_active_measurement(value)
    return coerce_measurement_config(value).to_mapping()
