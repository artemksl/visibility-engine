"""Exception hierarchy for :mod:`visibility_runner`.

The hierarchy is deliberately dependency-free.  In particular, importing an
error or configuration contract must never initialize Playwright or reach into
the embedding application.
"""

from __future__ import annotations

from typing import Mapping, Optional


class VisibilityRunnerError(Exception):
    """Base class for every package-owned failure."""


class ContractError(VisibilityRunnerError, ValueError):
    """A typed public contract is malformed or internally inconsistent."""


class ConfigurationError(ContractError):
    """Explicit measurement or runtime configuration is invalid."""


class ConfigurationConflictError(ConfigurationError):
    """Explicit configuration conflicts with inherited legacy environment."""


class PlanningError(ContractError):
    """A pure run plan cannot be constructed safely."""


class IntegrityError(VisibilityRunnerError):
    """Stored or browser-visible state does not match its frozen identity."""


class ArtifactIntegrityError(IntegrityError):
    """An immutable artifact is missing or has the wrong hash or byte length."""


class ArtifactHashMismatchError(ArtifactIntegrityError):
    """Artifact bytes do not match the reference's mandatory SHA-256."""


class ProfileManifestError(IntegrityError):
    """The pinned logged-in profile manifest is invalid."""


class ProfileIdentityMismatchError(IntegrityError):
    """A browser context does not match the frozen profile identity."""


class StateTransitionError(IntegrityError):
    """A durable state machine transition failed its compare-and-set guard."""


class StateConflictError(StateTransitionError):
    """Stored state conflicts with the requested transition."""


class FenceConflictError(StateConflictError):
    """An irreversible send attempt cannot make the requested transition."""


class RunAlreadyExistsError(StateTransitionError):
    """A new-run operation was given an already-persisted run ID."""


class RunNotFoundError(VisibilityRunnerError):
    """No package-owned state exists for the requested run ID."""


class UnknownArtifactReferenceError(RunNotFoundError):
    """An artifact reference does not identify stored run evidence."""


class UnsafeResumeError(StateTransitionError):
    """Durable send attempts prohibit automatic recovery."""


class AmbiguousSendError(UnsafeResumeError):
    """A send may have happened but acknowledgement is not durable."""


class AcknowledgedSendError(UnsafeResumeError):
    """A caller attempted to repeat an already-acknowledged send."""


class BrowserRuntimeError(VisibilityRunnerError):
    """The configured persistent-browser runtime cannot be used safely."""


class BrowserInteractionError(BrowserRuntimeError):
    """A browser interaction failed before a safe website result existed."""


class ProviderAccessBlockedError(BrowserInteractionError):
    """A visible website-owned access check prevents normal measurement."""

    def __init__(
        self, *args: object, reason: str = "authentication_required",
        usage_limit: Optional[Mapping[str, object]] = None,
    ) -> None:
        super().__init__(*args)
        self.reason = reason
        self.usage_limit = dict(usage_limit) if usage_limit is not None else None


class HumanVerificationRequiredError(ProviderAccessBlockedError):
    """A visible human-verification challenge requires operator action."""


class BrowserUnavailableError(BrowserRuntimeError):
    """The configured CDP browser is unavailable."""


class ProfileLeaseError(BrowserRuntimeError):
    """A profile and website pair could not be leased."""


class ProfileLeaseTimeout(ProfileLeaseError):
    """A profile and website pair remained owned beyond its configured timeout."""


class ProfileLeaseQuarantined(ProfileLeaseError):
    """A prior owner could not confirm cleanup of a profile and website pair."""


class ProviderError(VisibilityRunnerError):
    """A website-specific prompt measurement failed."""


class ComposerInputMismatchError(ProviderError):
    """The browser composer did not retain the exact prepared prompt."""


class ResponseCaptureError(ProviderError):
    """A complete website response could not be captured durably."""


class ExtractionError(VisibilityRunnerError):
    """Ordered-name name listing failed closed."""


class ExtractionParseError(ExtractionError):
    """An name listing response is not one closed JSON array of strings."""


class GroundingError(ExtractionError):
    """Extracted names cannot be matched exactly and in order."""


class CancellationError(VisibilityRunnerError):
    """Package-owned work was cancelled."""


# Compatibility aliases with concise names for callers that already use a
# Snapshot-flavoured vocabulary.
ConfigError = ConfigurationError
SnapshotRunConfigError = ConfigurationError
AlreadyExistsError = RunAlreadyExistsError
NotFoundError = RunNotFoundError
IdentityMismatchError = ProfileIdentityMismatchError


__all__ = [
    "AcknowledgedSendError",
    "AlreadyExistsError",
    "AmbiguousSendError",
    "ArtifactIntegrityError",
    "ArtifactHashMismatchError",
    "BrowserInteractionError",
    "BrowserRuntimeError",
    "BrowserUnavailableError",
    "CancellationError",
    "ComposerInputMismatchError",
    "ConfigError",
    "ConfigurationConflictError",
    "ConfigurationError",
    "ContractError",
    "ExtractionError",
    "ExtractionParseError",
    "FenceConflictError",
    "GroundingError",
    "HumanVerificationRequiredError",
    "IntegrityError",
    "IdentityMismatchError",
    "NotFoundError",
    "PlanningError",
    "ProfileIdentityMismatchError",
    "ProfileLeaseError",
    "ProfileLeaseQuarantined",
    "ProfileLeaseTimeout",
    "ProfileManifestError",
    "ProviderError",
    "ProviderAccessBlockedError",
    "ResponseCaptureError",
    "RunAlreadyExistsError",
    "RunNotFoundError",
    "SnapshotRunConfigError",
    "StateTransitionError",
    "StateConflictError",
    "UnsafeResumeError",
    "UnknownArtifactReferenceError",
    "VisibilityRunnerError",
]
