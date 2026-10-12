"""Runtime view discovery and freshness checks; no cached readiness proof."""

import math
import re

from automation_core.contracts import AuthObservation, ViewRef
from automation_core.browser.interaction import InteractionError, _origin


class ViewRegistry:
    """Resolve runtime hints without changing tabs or promoting ownership.

    The driver is the source of live references and task ownership. Callers must
    separately inspect origin, readiness and expected identity through their
    PageComponent. A saved view ID/generation is only a discovery hint.
    """

    def __init__(self, driver):
        self.driver = driver

    def candidates(self, deadline: float) -> tuple[ViewRef, ...]:
        return self.driver.discover_views(deadline=deadline)

    def resolve(self, view_id: str, generation: str, deadline: float,
                require_owned: bool = False) -> ViewRef:
        if type(require_owned) is not bool:
            raise InteractionError("invalid_view")
        ref = ViewRef(view_id, generation)
        owned = self.driver.view_owned(ref, deadline=deadline)
        if require_owned and not owned:
            raise InteractionError("view_not_owned")
        return ref

    def owned(self, ref: ViewRef, deadline: float) -> bool:
        return self.driver.view_owned(ref, deadline=deadline)

    def observe(self, component, ref: ViewRef, deadline: float) -> object:
        document = self.driver.document_generation(ref, deadline=deadline)
        started = self.driver.clock.monotonic()
        observation = component.observe(ref, deadline)
        # This is a check of runtime metadata after the already admitted read,
        # not another browser read. Keep available evidence if that read ran
        # late, while still refusing lost ownership, closure or replacement.
        current = self.driver._view(ref)
        if current.document != document:
            raise InteractionError("binding_changed")
        if type(observation) not in _OBSERVATION_VALIDATORS or observation.view != ref:
            raise InteractionError("observation_failed")
        validate = _OBSERVATION_VALIDATORS[type(observation)]
        if validate is not None:
            validate(observation, component, current, started, self.driver.clock.monotonic())
        return observation


_OBSERVATION_VALIDATORS = {AuthObservation: None}


def register_observation_type(record_type, validate):
    """Add a source-declared observation and its page-specific checks."""
    _OBSERVATION_VALIDATORS[record_type] = validate
