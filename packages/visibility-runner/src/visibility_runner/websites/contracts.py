"""Website configuration and the common submission/result contract."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Mapping, Optional, Protocol, TYPE_CHECKING
from urllib.parse import urlsplit

from ..errors import BrowserInteractionError, IdentityMismatchError
from ..gaio_evidence import GAIO_GENERATED_ANSWER_CAPTURE_VERSION, canonical_gaio_terminal_evidence

if TYPE_CHECKING:
    from ..browser.input import ComposerPreSendFailure

class ProviderStatus(str, Enum):
    COLLECTED = "collected"
    NOT_SHOWN = "not_shown"
    BLOCKED = "blocked"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


@dataclass(frozen=True)
class ResponseBaseline:
    """Durable proof of the visible response state before one send."""

    visible_response_count: int
    newest_response_sha256: str

    @classmethod
    def from_snapshot(
        cls,
        snapshot: "VisibleAnswerSnapshot",
    ) -> "ResponseBaseline":
        return cls(
            visible_response_count=snapshot.count,
            newest_response_sha256=hashlib.sha256(snapshot.text.encode("utf-8")).hexdigest(),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ResponseBaseline":
        count = int(value.get("visible_response_count", 0))
        digest = str(value.get("newest_response_sha256") or "")
        if (
            count < 0
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("invalid response baseline")
        return cls(
            visible_response_count=count,
            newest_response_sha256=digest,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "visible_response_count": self.visible_response_count,
            "newest_response_sha256": self.newest_response_sha256,
        }


class ReconciliationDisposition(str, Enum):
    LATE_RESPONSE_CAPTURED = "late_response_captured"
    CONFIRMED_TERMINAL_NO_RESPONSE = "confirmed_terminal_no_response"
    TERMINAL_NONRETRYABLE = "terminal_nonretryable"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class BrowserIdentity:
    profile_id: str
    chrome_directory: str
    browser_context_reference: str
    tab_reference: str
    browser_target_reference: str = ""
    conversation_reference: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "BrowserIdentity":
        required = (
            "profile_id",
            "chrome_directory",
            "browser_context_reference",
            "tab_reference",
        )
        missing = [key for key in required if not str(value.get(key) or "")]
        if missing:
            raise IdentityMismatchError(
                "browser identity is missing: " + ", ".join(missing)
            )
        return cls(
            profile_id=str(value["profile_id"]),
            chrome_directory=str(value["chrome_directory"]),
            browser_context_reference=str(value["browser_context_reference"]),
            tab_reference=str(value["tab_reference"]),
            browser_target_reference=str(
                value.get("browser_target_reference") or ""
            ),
            conversation_reference=str(value.get("conversation_reference") or ""),
        )

    def to_mapping(self) -> dict[str, str]:
        value = {
            "profile_id": self.profile_id,
            "chrome_directory": self.chrome_directory,
            "browser_context_reference": self.browser_context_reference,
            "tab_reference": self.tab_reference,
            "conversation_reference": self.conversation_reference,
        }
        if self.browser_target_reference:
            value["browser_target_reference"] = self.browser_target_reference
        return value

    def assert_same_tab(
        self,
        other: "BrowserIdentity",
        *,
        require_same_conversation: bool = False,
        canonical: Optional[Callable[[str], str]] = None,
    ) -> None:
        for field_name in (
            "profile_id",
            "chrome_directory",
            "browser_context_reference",
            "tab_reference",
            "browser_target_reference",
        ):
            if getattr(self, field_name) != getattr(other, field_name):
                raise IdentityMismatchError(
                    f"same-chat identity mismatch for {field_name}"
                )
        if require_same_conversation:
            if (
                not self.conversation_reference
                or not other.conversation_reference
            ):
                raise IdentityMismatchError(
                    "same-chat sequencing lacks a durable conversation reference"
                )
            mine, theirs = self.conversation_reference, other.conversation_reference
            if canonical is not None:
                mine, theirs = canonical(mine), canonical(theirs)
            if mine != theirs:
                raise IdentityMismatchError(
                    "the provider conversation reference changed between turns"
                )


@dataclass(frozen=True)
class ProviderAnswer:
    status: ProviderStatus
    text: str
    identity: BrowserIdentity
    citations: tuple[Mapping[str, object], ...] = ()
    capture_contract_version: Optional[str] = None
    detail: Optional[str] = None
    capture_evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.identity, BrowserIdentity):
            raise BrowserInteractionError(
                "provider answer lacks a browser identity"
            )
        if not isinstance(self.capture_evidence, Mapping):
            raise BrowserInteractionError(
                "provider capture evidence must be an object"
            )
        expected_state: Optional[str] = None
        expected_response_sha256: Optional[str] = None
        identity_url = urlsplit(self.identity.conversation_reference)
        google_search_identity = (
            identity_url.scheme == "https"
            and identity_url.hostname in {"google.com", "www.google.com"}
            and identity_url.path == "/search"
        )
        if self.status is ProviderStatus.NOT_SHOWN:
            if str(self.text):
                raise BrowserInteractionError(
                    "Google NOT_SHOWN cannot contain response text"
                )
            expected_state = "not_shown"
        elif (
            self.status is ProviderStatus.COLLECTED
            and (
                self.capture_contract_version
                == GAIO_GENERATED_ANSWER_CAPTURE_VERSION
                or google_search_identity
            )
        ):
            if (
                self.capture_contract_version
                != GAIO_GENERATED_ANSWER_CAPTURE_VERSION
            ):
                raise BrowserInteractionError(
                    "collected Google answer lacks the generated-answer "
                    "capture contract"
                )
            if not str(self.text):
                raise BrowserInteractionError(
                    "collected Google evidence requires response text"
                )
            expected_state = "shown"
            expected_response_sha256 = hashlib.sha256(
                str(self.text).encode("utf-8")
            ).hexdigest()
        if expected_state is None:
            return
        try:
            evidence = canonical_gaio_terminal_evidence(
                self.capture_evidence,
                expected_state=expected_state,
                expected_identity=self.identity.to_mapping(),
                expected_response_sha256=expected_response_sha256,
            )
        except ValueError as exc:
            raise BrowserInteractionError(
                f"invalid Google terminal evidence: {exc}"
            ) from exc
        object.__setattr__(self, "capture_evidence", evidence)


@dataclass(frozen=True)
class ReconciliationResult:
    disposition: ReconciliationDisposition
    identity: BrowserIdentity
    evidence: Mapping[str, object]
    answer: Optional[ProviderAnswer] = None
    failure_classification: Optional[str] = None
    native_retry_available: bool = False


class Submission(Protocol):
    @property
    def identity(self) -> BrowserIdentity: ...

    @property
    def response_baseline(self) -> Optional[ResponseBaseline]: ...

    async def prepare_exact(
        self,
        prompt: str,
        *,
        on_pre_send_failure: Optional[
            Callable[[ComposerPreSendFailure], object]
        ] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None: ...

    async def validate_pre_send_identity(self) -> None: ...

    async def submit_once(self) -> None: ...

    async def wait_response(self) -> ProviderAnswer: ...

    async def reconcile_no_response(
        self,
        grace_seconds: float,
    ) -> ReconciliationResult: ...

    async def prepare_native_retry(self) -> bool: ...

    def restore_confirmed_attempt(
        self,
        *,
        prompt: str,
        response_baseline: Mapping[str, object],
        failure_classification: str,
        native_retry: bool = False,
        binding: Optional[Mapping[str, object]] = None,
    ) -> None: ...

    def next_turn(self) -> "Submission": ...

    async def close(self) -> None: ...


class ProviderDriver(Protocol):
    platform: str

    async def open(
        self,
        *,
        profile_id: str,
        chrome_directory: str,
        observation_id: str,
        fresh: bool = True,
    ) -> Submission: ...

    async def restore(
        self,
        identity: BrowserIdentity,
        *,
        observation_id: str,
    ) -> Optional[Submission]: ...


@dataclass
class TransportCounter:
    prepared: int = 0
    send_started: int = 0
    acknowledged: int = 0
    responses: int = 0

    def to_mapping(self) -> dict[str, int]:
        return {
            "prepared": self.prepared,
            "send_started": self.send_started,
            "acknowledged": self.acknowledged,
            "responses": self.responses,
        }


@dataclass
class ProviderRegistry:
    drivers: dict[str, ProviderDriver] = field(default_factory=dict)

    def register(self, driver: ProviderDriver) -> None:
        key = str(driver.platform)
        if key in self.drivers:
            raise ValueError(f"provider driver already registered: {key}")
        self.drivers[key] = driver

    def require(self, platform: object) -> ProviderDriver:
        key = getattr(platform, "value", platform)
        try:
            return self.drivers[str(key)]
        except KeyError:
            raise BrowserInteractionError(
                f"no provider driver is configured for {key}"
            ) from None


@dataclass(frozen=True)
class PageProviderSpec:
    platform: str
    start_url: str
    composer_selectors: tuple[str, ...]
    send_selectors: tuple[str, ...]
    answer_selectors: tuple[str, ...]
    response_node_selectors: tuple[str, ...] = ()
    user_message_selectors: tuple[str, ...] = ()
    rendered_user_label_selectors: tuple[str, ...] = ()
    stop_selectors: tuple[str, ...] = ()
    blocked_markers: tuple[str, ...] = ()
    terminal_error_selectors: tuple[str, ...] = ()
    terminal_error_within_response_selectors: tuple[str, ...] = ()
    terminal_error_markers: tuple[str, ...] = ()
    native_retry_selectors: tuple[str, ...] = ()
    native_retry_within_response_selectors: tuple[str, ...] = ()
    conversation_url_prefixes: tuple[str, ...] = ()
    rendered_answer_label_selectors: tuple[str, ...] = ()
    response_timeout_seconds: float = 180.0
    stable_seconds: float = 1.0
    access_wall_selectors: tuple[str, ...] = ()
    human_verification_selectors: tuple[str, ...] = ()
    human_verification_text_selectors: tuple[str, ...] = ()
    human_verification_text_markers: tuple[str, ...] = ()
    authentication_url_prefixes: tuple[str, ...] = ()
    unauthenticated_control_selectors: tuple[str, ...] = ()
    other_blocker_selectors: tuple[str, ...] = ()
    cookie_notice_selectors: tuple[str, ...] = ()
    response_content_selectors: tuple[str, ...] = ()
    collapse_nested_message_aliases: bool = False
    # Optional semantic metadata on a message's nearest owning ancestor.
    response_turn_state_attribute: str = ""
    current_turn_owner_selector: str = ""
    message_id_ancestor_attribute: str = ""
    # Message ids the website shows only while it is still writing an answer
    # and replaces once the answer is complete. Such an id is read as unknown
    # (None): it is never saved in a correlation checkpoint, never compared
    # against a later read, and, when an earlier release saved one, it is not
    # compared on reopen. ChatGPT: ``request-placeholder-``.
    temporary_message_id_prefixes: tuple[str, ...] = ()
    type_composer_input: bool = False
    copilot_lexical_readback: bool = False
    composer_typing_min_delay_ms: float = 5.0
    composer_typing_max_delay_ms: float = 15.0
    # Alternate new-chat routes, never conversation URL prefixes. Freshness
    # uses the provider origin and route only; query/fragment values and
    # transcript markup do not determine whether this is a new-chat route.
    blank_start_url_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class VisibleAnswerSnapshot:
    count: int
    text: str


@dataclass
class _ResponseScope:
    """Poll-local wrapper binding, never part of the public text snapshot."""

    latest_response: Optional[object] = None
    latest_user: Optional[object] = None


PageFactory = Callable[
    [str, str, str, bool, Optional[BrowserIdentity]], Awaitable[object]
]
