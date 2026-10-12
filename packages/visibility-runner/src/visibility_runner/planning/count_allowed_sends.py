"""Derive allowed sends from browser and SDK observation counts.

Historical Google and derived observations retain their original accounting.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from ..config import GitHubCopilotConfig, RetryPolicy
from ..models import SubmissionBudget, SubmissionTopology


CONVERSATIONAL_PLATFORMS: Final[frozenset[str]] = frozenset(
    {"chatgpt", "gemini", "copilot"}
)
GOOGLE_AI_OVERVIEW_PLATFORM: Final[str] = "google_ai_overview"
GOOGLE_GENERATED_ANSWER_CAPTURE_VERSION: Final[str] = (
    "google-ai-overview-generated-answer-v1"
)


def _legacy_data_mode(
    observation: Mapping[str, object],
    *,
    platform: str,
    platform_data_modes: Mapping[str, object] | None,
) -> str:
    return str(
        observation.get("platform_data_mode")
        or observation.get("measurement_mode")
        or (
            platform_data_modes.get(platform)
            if isinstance(platform_data_modes, Mapping)
            else ""
        )
        or ""
    )


def _legacy_google_extraction_allowed(observation: Mapping[str, object]) -> bool:
    status = str(observation.get("status") or "")
    return not status or (
        status == "collected"
        and str(observation.get("generated_answer_capture_version") or "")
        == GOOGLE_GENERATED_ANSWER_CAPTURE_VERSION
    )


@dataclass(frozen=True)
class _LaneCounts:
    browser: int = 0
    source_parser: int = 0
    google_search: int = 0
    google_extraction: int = 0
    direct: int = 0

    def to_mapping(self, *, include_google_extraction: bool = True) -> dict[str, object]:
        extraction = self.google_extraction if include_google_extraction else 0
        result = {
            "conversational_chats": self.browser,
            "turn_1_messages": self.browser,
            "turn_2_messages": self.browser,
            "conversational_messages": self.browser * 2,
            "google_search_submissions": self.google_search,
            "standalone_extraction_chats": extraction,
            "standalone_extraction_messages": extraction,
            "total_chat_count": self.browser + extraction + self.source_parser * 2 + self.direct * 2,
            "total_external_submissions": (
                self.browser * 2 + self.google_search + extraction
                + self.source_parser * 2 + self.direct * 2
            ),
        }
        if self.source_parser:
            result.update({
                "topology_version": "visibility_runner.submission_topology.v2",
                "sdk_source_sessions": self.source_parser,
                "sdk_source_messages": self.source_parser,
                "source_parser_chats": self.source_parser,
                "source_parser_messages": self.source_parser,
            })
        if self.direct:
            result.update({"topology_version":"visibility_runner.submission_topology.v3",
                           "sdk_source_sessions":self.source_parser,"sdk_source_messages":self.source_parser,
                           "source_parser_chats":self.source_parser,"source_parser_messages":self.source_parser,
                           "browser_source_chats":self.direct,"browser_source_messages":self.direct,
                           "browser_judge_chats":self.direct,"browser_judge_messages":self.direct})
        return result


def _count_lanes(
    observations: Sequence[Mapping[str, object]],
    platform_data_modes: Mapping[str, object] | None = None,
) -> _LaneCounts:
    if isinstance(observations, (str, bytes)):
        raise TypeError("observations must be a sequence of mappings")
    browser = source_parser = google_search = google_extraction = direct = 0
    for index, observation in enumerate(observations):
        if not isinstance(observation, Mapping):
            raise TypeError(f"observations[{index}] must be a mapping")
        platform = str(observation.get("platform") or "")
        # These historical modes remain readable without admitting execution.
        if "derived" in _legacy_data_mode(
            observation, platform=platform, platform_data_modes=platform_data_modes,
        ):
            continue
        if observation.get("client_research_capability") == "client_research.identification.v1":
            direct += 1
        elif platform in CONVERSATIONAL_PLATFORMS:
            browser += 1
        elif platform == "github_copilot":
            source_parser += 1
        elif platform == GOOGLE_AI_OVERVIEW_PLATFORM:
            google_search += 1
            google_extraction += int(_legacy_google_extraction_allowed(observation))
    return _LaneCounts(browser, source_parser, google_search, google_extraction, direct)


def exact_chat_message_budget(
    observations: Sequence[Mapping[str, object]],
    *,
    platform_data_modes: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return the established initial send topology, including historical lanes."""

    return _count_lanes(observations, platform_data_modes).to_mapping()


def _source_policy(observations: Sequence[Mapping[str, object]]) -> GitHubCopilotConfig | None:
    configurations = [
        GitHubCopilotConfig.from_mapping(row["github_copilot"])
        for row in observations if row.get("platform") == "github_copilot"
    ]
    if configurations and any(config != configurations[0] for config in configurations):
        raise ValueError("source parser observations must freeze one GitHub account configuration")
    return configurations[0] if configurations else None


def build_submission_budget(
    observations: Sequence[Mapping[str, object]],
    *,
    platform_data_modes: Mapping[str, object] | None = None,
    retry_policy: RetryPolicy | None = None,
) -> SubmissionBudget:
    """Build the immutable typed budget for ``observations``."""

    counts = _count_lanes(observations, platform_data_modes)
    return _allowed_sends_with_retries(
        SubmissionTopology(**counts.to_mapping()),
        retry_policy=retry_policy,
        source_policy=_source_policy(observations),
    )


def build_measurement_packet_budget(
    observations: Sequence[Mapping[str, object]],
    *,
    platform_data_modes: Mapping[str, object] | None = None,
    retry_policy: RetryPolicy | None = None,
) -> SubmissionBudget:
    """Budget measurement sends only, excluding conditional GAIO name listing."""

    counts = _count_lanes(observations, platform_data_modes)
    return _allowed_sends_with_retries(
        SubmissionTopology(**counts.to_mapping(include_google_extraction=False)),
        retry_policy=retry_policy,
        source_policy=_source_policy(observations),
    )


def build_gaio_extraction_packet_budget(
    observations: Sequence[Mapping[str, object]],
    *,
    retry_policy: RetryPolicy | None = None,
) -> SubmissionBudget:
    """Budget fresh Gemini name listing for the measurement group's possible shown rows."""

    if isinstance(observations, (str, bytes)):
        raise TypeError("observations must be a sequence of mappings")
    eligible = 0
    for index, observation in enumerate(observations):
        if not isinstance(observation, Mapping):
            raise TypeError(f"observations[{index}] must be a mapping")
        if str(observation.get("platform") or "") != GOOGLE_AI_OVERVIEW_PLATFORM:
            raise ValueError(
                "GAIO extraction packets may contain only Google AI "
                "Overview observations"
            )
        if _legacy_google_extraction_allowed(observation):
            eligible += 1
    return _allowed_sends_with_retries(
        SubmissionTopology(**_LaneCounts(google_extraction=eligible).to_mapping()),
        retry_policy=retry_policy,
    )


def _authorized_attempts(retry_policy: RetryPolicy | None) -> int:
    policy = retry_policy if retry_policy is not None else RetryPolicy()
    if not isinstance(policy, RetryPolicy):
        raise TypeError("retry_policy must be a RetryPolicy")
    return policy.authorized_max_attempts


def _allowed_sends_with_retries(
    initial: SubmissionTopology,
    *,
    retry_policy: RetryPolicy | None,
    source_policy: GitHubCopilotConfig | None = None,
) -> SubmissionBudget:
    return SubmissionBudget(
        **initial.to_mapping(),
        retry_authorized_ceiling=initial.with_attempt_limits(
            _authorized_attempts(retry_policy),
            source_policy.max_source_attempts if source_policy else None,
            source_policy.max_parser_attempts if source_policy else None,
        ),
    )


__all__ = [
    "CONVERSATIONAL_PLATFORMS",
    "GOOGLE_AI_OVERVIEW_PLATFORM",
    "GOOGLE_GENERATED_ANSWER_CAPTURE_VERSION",
    "build_gaio_extraction_packet_budget",
    "build_measurement_packet_budget",
    "build_submission_budget",
    "exact_chat_message_budget",
]
