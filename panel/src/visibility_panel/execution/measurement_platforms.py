"""Current measurement admission policy, separate from historical contracts."""

from collections.abc import Mapping

from visibility_panel.platforms import parse_platforms


ACTIVE_MEASUREMENT_PLATFORMS = ("chatgpt", "gemini", "copilot")
AVAILABLE_MEASUREMENT_PLATFORMS = (*ACTIVE_MEASUREMENT_PLATFORMS, "github_copilot")
GOOGLE_AI_OVERVIEW_DEFERRED = (
    "Google AI Overview is deferred to further work. Existing frozen runs remain "
    "unchanged and available for inspection; Google measurement, resume, and "
    "Refetch are disabled."
)

def deferred_measurement_reason(measurement: Mapping) -> str:
    """Explain an execution refusal without filtering or rewriting its scope."""
    platforms = parse_platforms(measurement.get("platforms"), default=())
    if "google_ai_overview" in platforms:
        return GOOGLE_AI_OVERVIEW_DEFERRED
    return ""


def require_active_measurement(measurement: Mapping) -> None:
    reason = deferred_measurement_reason(measurement)
    if reason:
        raise ValueError(reason)
