"""Current execution availability, separate from immutable historical contracts."""

from __future__ import annotations

from collections.abc import Mapping

from .errors import ConfigurationError, StateTransitionError
from .config import COPILOT_BROWSER_MEASUREMENT_MODE


ACTIVE_MEASUREMENT_PLATFORMS = ("chatgpt", "gemini", "copilot", "github_copilot")
COPILOT_LEGACY_EXECUTION_UNAVAILABLE = (
    "copilot_legacy_transport_unavailable: This frozen Copilot plan does not "
    "use the restored browser same-chat contract. Create a new run; historical "
    "derived or removed native Copilot work cannot execute through this driver."
)
GOOGLE_AI_OVERVIEW_DEFERRED = (
    "google_ai_overview_deferred: Google AI Overview is deferred to further work. "
    "Its searches and Gemini extraction pipeline cannot execute. "
    "Historical results remain available for inspection."
)


def _value(value):
    return getattr(value, "value", value)


def require_active_selection(platforms) -> None:
    """Reject unavailable surfaces before any new work is planned."""
    values = tuple(_value(platform) for platform in platforms)
    if "google_ai_overview" in values:
        raise ConfigurationError(GOOGLE_AI_OVERVIEW_DEFERRED)


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def require_active_execution(plan) -> None:
    """Refuse an unavailable frozen plan before acquiring execution ownership.

    Parser packets use Gemini as their provider, so provider membership alone
    cannot establish that a frozen plan belongs to the active browser platforms.
    No frozen observation, packet, or result is removed or reinterpreted here.
    """
    config = _field(plan, "measurement_config", {})
    scope = _field(plan, "scope_manifest", {})
    declared = tuple(_field(config, "platforms", ()) or ()) + tuple(_field(scope, "platforms", ()) or ())
    observations = _field(_field(plan, "observation_plan", {}), "observations", ()) or ()
    packets = _field(plan, "packets", ()) or ()
    if (
        any(_value(value) == "copilot" for value in declared)
        or any(_value(_field(row, "platform")) == "copilot" for row in observations)
        or any(_value(_field(packet, "platform")) == "copilot" for packet in packets)
    ):
        modes = _field(scope, "platform_data_modes", {}) or {}
        if _field(modes, "copilot") != COPILOT_BROWSER_MEASUREMENT_MODE:
            raise StateTransitionError(COPILOT_LEGACY_EXECUTION_UNAVAILABLE)
    if (
        any(_value(value) == "google_ai_overview" for value in declared)
        or any(_value(_field(row, "platform")) == "google_ai_overview" for row in observations)
        or any(
            _value(_field(packet, "platform")) == "google_ai_overview"
            or _value(_field(packet, "kind")) == "gaio_extraction"
            for packet in packets
        )
    ):
        raise StateTransitionError(GOOGLE_AI_OVERVIEW_DEFERRED)


def require_active_action(action: str) -> None:
    """Keep deferred effects closed even for direct recovery-adapter callers."""
    if action in {"google_search", "gaio_gemini_extractor"}:
        raise StateTransitionError(GOOGLE_AI_OVERVIEW_DEFERRED)
