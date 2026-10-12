"""Immutable plans, budgets, artifact references, and execution results."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Optional

from .config import (
    MeasurementConfig,
    NONPUBLISHABLE_POLICY_VERSION,
    Platform,
    ProfileIdentity,
    RetryPolicy,
    normalize_platform,
)
from .errors import ContractError
from .contract_primitives import (
    RUN_PLAN_SCHEMA_VERSION,
    WORKER_REQUEST_SCHEMA_VERSION,
    canonical_sha256 as _plan_sha256,
    legacy_model_canonical_json_bytes as canonical_json_bytes,
    legacy_model_canonical_sha256 as canonical_sha256,
)
from .gaio_evidence import canonical_gaio_terminal_evidence
from .status import (
    ExtractionStatus,
    IssueSeverity,
    ObservationStatus,
    PacketStatus,
    RunStatus,
    StringEnum,
)


def _required_text(value: object, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContractError(f"{field_name} is required")
    return text


def _nonnegative_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(f"{field_name} must be a non-negative integer")
    return value


def _positive_int(value: object, *, field_name: str) -> int:
    result = _nonnegative_int(value, field_name=field_name)
    if result == 0:
        raise ContractError(f"{field_name} must be positive")
    return result


def _sha256(value: object, *, field_name: str, allow_empty: bool = False) -> str:
    text = str(value or "").strip().casefold()
    if not text and allow_empty:
        return ""
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ContractError(
            f"{field_name} must be exactly 64 hexadecimal characters"
        )
    return text


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


_LEGACY_TOPOLOGY_FIELDS = (
    "conversational_chats", "turn_1_messages", "turn_2_messages",
    "conversational_messages", "google_search_submissions",
    "standalone_extraction_chats", "standalone_extraction_messages",
    "total_chat_count", "total_external_submissions",
)
_SOURCE_TOPOLOGY_FIELDS = (
    "sdk_source_sessions", "sdk_source_messages",
    "source_parser_chats", "source_parser_messages",
)
_DIRECT_TOPOLOGY_FIELDS = ("browser_source_chats", "browser_source_messages", "browser_judge_chats", "browser_judge_messages")
_TOPOLOGY_FIELDS = (*_LEGACY_TOPOLOGY_FIELDS, *_SOURCE_TOPOLOGY_FIELDS, *_DIRECT_TOPOLOGY_FIELDS)
_DIRECT_TOPOLOGY_VERSION = "visibility_runner.submission_topology.v3"
_SOURCE_TOPOLOGY_VERSION = "visibility_runner.submission_topology.v2"


@dataclass(frozen=True)
class SubmissionTopology:
    """Separate browser turns, SDK source requests and dependent parser chats.

    Legacy serialization remains byte-identical when no source/parser lane exists.
    V2 explicitly represents two services without pretending they share a chat.
    """

    conversational_chats: int
    turn_1_messages: int
    turn_2_messages: int
    conversational_messages: int
    google_search_submissions: int
    standalone_extraction_chats: int
    standalone_extraction_messages: int
    total_chat_count: int
    total_external_submissions: int
    sdk_source_sessions: int = 0
    sdk_source_messages: int = 0
    source_parser_chats: int = 0
    source_parser_messages: int = 0
    topology_version: str = ""
    browser_source_chats: int = 0
    browser_source_messages: int = 0
    browser_judge_chats: int = 0
    browser_judge_messages: int = 0

    def __post_init__(self) -> None:
        for name in _TOPOLOGY_FIELDS:
            object.__setattr__(self, name, _nonnegative_int(getattr(self, name), field_name=f"submission_topology.{name}"))
        has_source = any(getattr(self, name) for name in _SOURCE_TOPOLOGY_FIELDS)
        has_direct = any(getattr(self, name) for name in _DIRECT_TOPOLOGY_FIELDS)
        if self.topology_version not in ("", _SOURCE_TOPOLOGY_VERSION, _DIRECT_TOPOLOGY_VERSION):
            raise ContractError("unsupported submission topology version")
        if has_direct:
            object.__setattr__(self, "topology_version", _DIRECT_TOPOLOGY_VERSION)
        elif has_source and not self.topology_version:
            object.__setattr__(self, "topology_version", _SOURCE_TOPOLOGY_VERSION)
        if self.browser_source_chats != self.browser_source_messages or self.browser_judge_chats != self.browser_judge_messages:
            raise ContractError("each browser source/judge chat receives exactly one message")
        if self.conversational_chats != self.turn_1_messages or self.turn_1_messages != self.turn_2_messages:
            raise ContractError("each conversational measurement chat must receive exactly one turn-1 and one turn-2 extraction message")
        if self.conversational_messages != self.turn_1_messages + self.turn_2_messages:
            raise ContractError("conversational_messages must equal turn_1_messages plus turn_2_messages")
        if self.standalone_extraction_chats != self.standalone_extraction_messages:
            raise ContractError("each standalone extraction chat must receive exactly one message")
        if self.sdk_source_sessions != self.sdk_source_messages:
            raise ContractError("each SDK source session must receive exactly one source message")
        if self.source_parser_chats != self.source_parser_messages:
            raise ContractError("each source parser chat must receive exactly one message")
        if self.total_chat_count != self.conversational_chats + self.standalone_extraction_chats + self.sdk_source_sessions + self.source_parser_chats + self.browser_source_chats + self.browser_judge_chats:
            raise ContractError("total_chat_count does not match its component counts")
        if self.total_external_submissions != self.conversational_messages + self.google_search_submissions + self.standalone_extraction_messages + self.sdk_source_messages + self.source_parser_messages + self.browser_source_messages + self.browser_judge_messages:
            raise ContractError("total_external_submissions does not match its component counts")

    @classmethod
    def zero(cls) -> "SubmissionTopology":
        return cls(**{name: 0 for name in _LEGACY_TOPOLOGY_FIELDS})

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SubmissionTopology":
        if not isinstance(value, Mapping):
            raise ContractError("submission topology must be an object")
        unknown = set(value) - {*_TOPOLOGY_FIELDS, "topology_version"}
        missing = set(_LEGACY_TOPOLOGY_FIELDS) - set(value)
        if unknown:
            raise ContractError("submission topology contains unsupported key(s): " + ", ".join(sorted(unknown)))
        if missing:
            raise ContractError("submission topology is missing key(s): " + ", ".join(sorted(missing)))
        if any(name in value for name in _SOURCE_TOPOLOGY_FIELDS):
            if value.get("topology_version") not in (_SOURCE_TOPOLOGY_VERSION, _DIRECT_TOPOLOGY_VERSION) or not set(_SOURCE_TOPOLOGY_FIELDS).issubset(value):
                raise ContractError("source/parser topology requires the complete v2 contract")
        if any(name in value for name in _DIRECT_TOPOLOGY_FIELDS) and (value.get("topology_version") != _DIRECT_TOPOLOGY_VERSION or not set(_DIRECT_TOPOLOGY_FIELDS).issubset(value)):
            raise ContractError("browser source/judge topology requires complete v3 contract")
        return cls(**dict(value))

    def to_mapping(self) -> dict[str, object]:
        names = _TOPOLOGY_FIELDS if self.topology_version == _DIRECT_TOPOLOGY_VERSION else (*_LEGACY_TOPOLOGY_FIELDS, *_SOURCE_TOPOLOGY_FIELDS) if self.topology_version else _LEGACY_TOPOLOGY_FIELDS
        result = {name: getattr(self, name) for name in names}
        if self.topology_version:
            result["topology_version"] = self.topology_version
        return result

    def __json_value__(self) -> dict[str, object]:
        return self.to_mapping()

    def scaled(self, factor: int) -> "SubmissionTopology":
        return self.with_attempt_limits(_positive_int(factor, field_name="submission topology scale"))

    def with_attempt_limits(self, browser: int, source: int | None = None, parser: int | None = None) -> "SubmissionTopology":
        values = {name: getattr(self, name) * browser for name in _TOPOLOGY_FIELDS}
        for name in ("sdk_source_sessions", "sdk_source_messages"):
            values[name] = getattr(self, name) * (source if source is not None else browser)
        for name in ("source_parser_chats", "source_parser_messages"):
            values[name] = getattr(self, name) * (parser if parser is not None else browser)
        values["total_chat_count"] = values["conversational_chats"] + values["standalone_extraction_chats"] + values["sdk_source_sessions"] + values["source_parser_chats"] + values["browser_source_chats"] + values["browser_judge_chats"]
        values["total_external_submissions"] = values["conversational_messages"] + values["google_search_submissions"] + values["standalone_extraction_messages"] + values["sdk_source_messages"] + values["source_parser_messages"] + values["browser_source_messages"] + values["browser_judge_messages"]
        return SubmissionTopology(**values, topology_version=self.topology_version)

    def __add__(self, other: object) -> "SubmissionTopology":
        if not isinstance(other, SubmissionTopology):
            return NotImplemented
        return SubmissionTopology(**{name: getattr(self, name) + getattr(other, name) for name in _TOPOLOGY_FIELDS}, topology_version=self.topology_version or other.topology_version)


@dataclass(frozen=True)
class SubmissionBudget:
    """Initial topology and independent, immutable source/parser retry ceilings."""

    conversational_chats: int
    turn_1_messages: int
    turn_2_messages: int
    conversational_messages: int
    google_search_submissions: int
    standalone_extraction_chats: int
    standalone_extraction_messages: int
    total_chat_count: int
    total_external_submissions: int
    retry_authorized_ceiling: Optional[SubmissionTopology] = None
    sdk_source_sessions: int = 0
    sdk_source_messages: int = 0
    source_parser_chats: int = 0
    source_parser_messages: int = 0
    topology_version: str = ""
    browser_source_chats: int = 0
    browser_source_messages: int = 0
    browser_judge_chats: int = 0
    browser_judge_messages: int = 0

    def __post_init__(self) -> None:
        initial = self.initial_topology
        object.__setattr__(self, "topology_version", initial.topology_version)
        ceiling = self.retry_authorized_ceiling or initial
        if not isinstance(ceiling, SubmissionTopology):
            raise ContractError("submission_budget.retry_authorized_ceiling must be a SubmissionTopology")
        if any(getattr(ceiling, name) < getattr(initial, name) for name in _TOPOLOGY_FIELDS):
            raise ContractError("retry_authorized_ceiling cannot be below the initial topology")
        object.__setattr__(self, "retry_authorized_ceiling", ceiling)

    @property
    def initial_topology(self) -> SubmissionTopology:
        return SubmissionTopology(**{name: getattr(self, name) for name in _TOPOLOGY_FIELDS}, topology_version=self.topology_version)

    @classmethod
    def zero(cls) -> "SubmissionBudget":
        return cls(**SubmissionTopology.zero().to_mapping())

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SubmissionBudget":
        if not isinstance(value, Mapping):
            raise ContractError("submission budget must be an object")
        initial = SubmissionTopology.from_mapping({name: item for name, item in value.items() if name != "retry_authorized_ceiling"})
        ceiling = value.get("retry_authorized_ceiling")
        return cls(**initial.to_mapping(), retry_authorized_ceiling=SubmissionTopology.from_mapping(ceiling) if ceiling is not None else None)

    def to_mapping(self) -> dict[str, object]:
        return {**self.initial_topology.to_mapping(), "retry_authorized_ceiling": self.retry_authorized_ceiling.to_mapping()}

    def __json_value__(self) -> dict[str, object]:
        return self.to_mapping()

    def __add__(self, other: object) -> "SubmissionBudget":
        if not isinstance(other, SubmissionBudget):
            return NotImplemented
        return SubmissionBudget(**(self.initial_topology + other.initial_topology).to_mapping(), retry_authorized_ceiling=self.retry_authorized_ceiling + other.retry_authorized_ceiling)


@dataclass(frozen=True)
class TransportCounts:
    """Actual durable transport milestones, never planned counts."""

    prepared: int = 0
    send_started: int = 0
    acknowledged: int = 0
    responses: int = 0
    retry_count: int = 0
    exhausted_retry_count: int = 0

    def __post_init__(self) -> None:
        for field_name in self.__dataclass_fields__:
            object.__setattr__(
                self,
                field_name,
                _nonnegative_int(
                    getattr(self, field_name),
                    field_name=f"transport_counts.{field_name}",
                ),
            )
        if not (
            self.acknowledged
            <= self.responses
            <= self.send_started
            <= self.prepared
        ):
            raise ContractError(
                "actual transport counts must be monotonic: acknowledged <= "
                "responses <= send_started <= prepared"
            )
        if self.retry_count > self.send_started:
            raise ContractError(
                "retry_count cannot exceed actual send_started attempts"
            )
        if self.exhausted_retry_count > self.send_started:
            raise ContractError(
                "exhausted_retry_count cannot exceed actual send_started "
                "attempts"
            )

    def __add__(self, other: object) -> "TransportCounts":
        if not isinstance(other, TransportCounts):
            return NotImplemented
        return TransportCounts(
            prepared=self.prepared + other.prepared,
            send_started=self.send_started + other.send_started,
            acknowledged=self.acknowledged + other.acknowledged,
            responses=self.responses + other.responses,
            retry_count=self.retry_count + other.retry_count,
            exhausted_retry_count=(
                self.exhausted_retry_count
                + other.exhausted_retry_count
            ),
        )


@dataclass(frozen=True)
class RunCounts:
    platform_prompt_count: int
    semantic_prompt_count: int
    planned_observation_count: int
    platform_prompt_counts: Mapping[str, int] = field(default_factory=dict)
    platform_observation_counts: Mapping[str, int] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        for field_name in (
            "platform_prompt_count",
            "semantic_prompt_count",
            "planned_observation_count",
        ):
            object.__setattr__(
                self,
                field_name,
                _nonnegative_int(
                    getattr(self, field_name),
                    field_name=f"run_counts.{field_name}",
                ),
            )
        for field_name in (
            "platform_prompt_counts",
            "platform_observation_counts",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, Mapping):
                raise ContractError(f"run_counts.{field_name} must be a map")
            normalized = {
                str(key): _nonnegative_int(
                    item,
                    field_name=f"run_counts.{field_name}[{key}]",
                )
                for key, item in value.items()
            }
            object.__setattr__(
                self,
                field_name,
                MappingProxyType(normalized),
            )
        if sum(self.platform_prompt_counts.values()) != self.platform_prompt_count:
            raise ContractError(
                "platform_prompt_counts must sum to platform_prompt_count"
            )
        if (
            sum(self.platform_observation_counts.values())
            != self.planned_observation_count
        ):
            raise ContractError(
                "platform_observation_counts must sum to "
                "planned_observation_count"
            )


# Snapshot-era compatibility spelling.
SnapshotRunCounts = RunCounts


class PacketKind(StringEnum):
    MEASUREMENT = "measurement"
    GAIO_EXTRACTION = "gaio_extraction"
    SOURCE_PARSER = "source_parser"


@dataclass(frozen=True)
class ObservationPlan:
    """One frozen prompt measurement identity and its exact prompt bytes."""

    observation_id: str
    prompt_id: str
    platform: Platform
    run_ordinal: int
    prompt_text: str
    prompt_sha256: str = ""
    semantic_prompt_id: Optional[str] = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observation_id",
            _required_text(
                self.observation_id,
                field_name="observation_id",
            ),
        )
        object.__setattr__(
            self,
            "prompt_id",
            _required_text(self.prompt_id, field_name="prompt_id"),
        )
        object.__setattr__(self, "platform", normalize_platform(self.platform))
        object.__setattr__(
            self,
            "run_ordinal",
            _positive_int(
                self.run_ordinal,
                field_name="run_ordinal",
            ),
        )
        if not isinstance(self.prompt_text, str):
            raise ContractError("prompt_text must be a string")
        actual_hash = hashlib.sha256(
            self.prompt_text.encode("utf-8")
        ).hexdigest()
        supplied_hash = _sha256(
            self.prompt_sha256,
            field_name="prompt_sha256",
            allow_empty=True,
        )
        if supplied_hash and supplied_hash != actual_hash:
            raise ContractError("prompt_sha256 does not match prompt_text bytes")
        object.__setattr__(self, "prompt_sha256", supplied_hash or actual_hash)
        semantic = str(self.semantic_prompt_id or "").strip() or None
        object.__setattr__(self, "semantic_prompt_id", semantic)
        if not isinstance(self.metadata, Mapping):
            raise ContractError("observation metadata must be an object")
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True)
class PacketPlan:
    """One independently executable frozen measurement group.

    Membership is the exact ordered tuple of prompt measurement IDs.  It is never
    reconstructed from mutable prompt or clinic names.
    """

    packet_id: str
    platform: Platform
    profile_id: str
    observation_ids: tuple[str, ...]
    submission_budget: SubmissionBudget
    kind: PacketKind = PacketKind.MEASUREMENT
    run_ordinal: Optional[int] = None
    conditional_on: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "packet_id",
            _required_text(self.packet_id, field_name="packet_id"),
        )
        object.__setattr__(self, "platform", normalize_platform(self.platform))
        object.__setattr__(
            self,
            "profile_id",
            _required_text(self.profile_id, field_name="profile_id"),
        )
        if isinstance(self.observation_ids, (str, bytes)):
            raise ContractError("packet observation_ids must be a list")
        observation_ids = tuple(
            _required_text(value, field_name="packet observation_id")
            for value in self.observation_ids
        )
        if not observation_ids:
            raise ContractError("a packet must contain at least one observation")
        if len(observation_ids) != len(set(observation_ids)):
            raise ContractError(
                "packet observation_ids must be exact and unique"
            )
        if not isinstance(self.submission_budget, SubmissionBudget):
            raise ContractError(
                "packet submission_budget must be a SubmissionBudget"
            )
        try:
            kind = (
                self.kind
                if isinstance(self.kind, PacketKind)
                else PacketKind(str(self.kind))
            )
        except ValueError as exc:
            raise ContractError(f"unknown packet kind: {self.kind!r}") from exc
        if (kind is PacketKind.SOURCE_PARSER) != (self.platform is Platform.GITHUB_COPILOT):
            raise ContractError("github_copilot packets require source_parser kind and vice versa")
        if self.run_ordinal is not None:
            object.__setattr__(
                self,
                "run_ordinal",
                _positive_int(
                    self.run_ordinal,
                    field_name="packet run_ordinal",
                ),
            )
        conditional = str(self.conditional_on or "").strip() or None
        if kind is PacketKind.GAIO_EXTRACTION and conditional is None:
            conditional = "gaio_shown_v1"
        object.__setattr__(self, "observation_ids", observation_ids)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "conditional_on", conditional)

    @property
    def budget(self) -> SubmissionBudget:
        return self.submission_budget


@dataclass(frozen=True)
class RunPlan:
    """Pure immutable authorized plan."""

    run_id: str
    measurement_config: MeasurementConfig
    packets: tuple[PacketPlan, ...]
    observation_ids: tuple[str, ...]
    submission_budget: SubmissionBudget
    selected_profile_ids: tuple[str, ...]
    profile_manifest_sha256: str
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy.disabled)
    selected_profiles: tuple[ProfileIdentity, ...] = ()
    counts: Optional[RunCounts] = None
    scope_manifest: Mapping[str, object] = field(default_factory=dict)
    snapshot_prompts: Mapping[str, object] = field(default_factory=dict)
    observation_plan: Mapping[str, object] = field(default_factory=dict)
    prompt_catalog_sha256: str = ""
    geography_version: str = ""
    geography_sha256: str = ""
    plan_sha256: str = ""
    schema_version: str = RUN_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        run_id = _required_text(self.run_id, field_name="run_id")
        if PurePosixPath(run_id).name != run_id or run_id in {".", ".."}:
            raise ContractError("run_id must be one safe path segment")
        if not isinstance(self.measurement_config, MeasurementConfig):
            raise ContractError(
                "measurement_config must be a MeasurementConfig"
            )
        if not isinstance(self.retry_policy, RetryPolicy):
            raise ContractError("retry_policy must be a RetryPolicy")
        packets = tuple(self.packets)
        if not packets or any(not isinstance(item, PacketPlan) for item in packets):
            raise ContractError("packets must contain PacketPlan values")
        packet_ids = [packet.packet_id for packet in packets]
        if len(packet_ids) != len(set(packet_ids)):
            raise ContractError("packet IDs must be unique")
        if isinstance(self.observation_ids, (str, bytes)):
            raise ContractError("observation_ids must be a list")
        observation_ids = tuple(
            _required_text(value, field_name="observation_id")
            for value in self.observation_ids
        )
        if not observation_ids:
            raise ContractError("a run plan must contain observations")
        if len(observation_ids) != len(set(observation_ids)):
            raise ContractError("run observation IDs must be unique")
        selected_profile_ids = tuple(
            _required_text(value, field_name="selected_profile_id")
            for value in self.selected_profile_ids
        )
        if not selected_profile_ids:
            raise ContractError("a run plan must pin selected profile IDs")
        if len(selected_profile_ids) != len(set(selected_profile_ids)):
            raise ContractError("selected profile IDs must be unique")
        selected_profiles = tuple(self.selected_profiles)
        if any(
            not isinstance(identity, ProfileIdentity)
            for identity in selected_profiles
        ):
            raise ContractError(
                "selected_profiles must contain ProfileIdentity values"
            )
        if selected_profiles and tuple(
            identity.id for identity in selected_profiles
        ) != selected_profile_ids:
            raise ContractError(
                "selected_profiles must match selected_profile_ids in exact "
                "order"
            )
        directories = tuple(
            identity.chrome_directory for identity in selected_profiles
        )
        if len(directories) != len(set(directories)):
            raise ContractError(
                "selected_profiles must freeze distinct raw Chrome "
                "directories"
            )
        known_observations = set(observation_ids)
        memberships: Counter[tuple[PacketKind, str]] = Counter()
        for packet in packets:
            if packet.profile_id not in selected_profile_ids:
                raise ContractError(
                    f"packet {packet.packet_id} uses an unselected profile"
                )
            unknown = set(packet.observation_ids) - known_observations
            if unknown:
                raise ContractError(
                    f"packet {packet.packet_id} contains unknown observation "
                    f"IDs: {', '.join(sorted(unknown))}"
                )
            memberships.update(
                (packet.kind, observation_id)
                for observation_id in packet.observation_ids
            )
        duplicated = sorted(
            f"{kind.value}:{observation_id}"
            for (kind, observation_id), count in memberships.items()
            if count > 1
        )
        if duplicated:
            raise ContractError(
                "an observation cannot belong to two packets of the same "
                "kind: "
                + ", ".join(duplicated)
            )
        measured = {
            observation_id
            for (kind, observation_id), count in memberships.items()
            if kind in {PacketKind.MEASUREMENT, PacketKind.SOURCE_PARSER} and count == 1
        }
        primary_counts = Counter()
        for (kind, observation_id), count in memberships.items():
            if kind in {PacketKind.MEASUREMENT, PacketKind.SOURCE_PARSER}:
                primary_counts[observation_id] += count
        if any(count > 1 for count in primary_counts.values()):
            raise ContractError("an observation cannot belong to multiple primary measurement packets")
        missing = sorted(known_observations - measured)
        if missing:
            raise ContractError(
                "every observation must belong to one measurement packet: "
                + ", ".join(missing)
            )
        if not isinstance(self.submission_budget, SubmissionBudget):
            raise ContractError(
                "submission_budget must be a SubmissionBudget"
            )
        authorized_attempts = self.retry_policy.authorized_max_attempts
        budgets = (
            self.submission_budget,
            *(packet.submission_budget for packet in packets),
        )
        for budget in budgets:
            source_config = self.measurement_config.github_copilot
            expected_send_limit = budget.initial_topology.with_attempt_limits(
                authorized_attempts,
                source_config.max_source_attempts if source_config else None,
                source_config.max_parser_attempts if source_config else None,
            )
            if budget.retry_authorized_ceiling != expected_send_limit:
                raise ContractError(
                    "submission retry ceiling does not match the frozen "
                    "retry policy"
                )
        if self.counts is not None:
            if not isinstance(self.counts, RunCounts):
                raise ContractError("counts must be a RunCounts")
            if self.counts.planned_observation_count != len(observation_ids):
                raise ContractError(
                    "counts.planned_observation_count does not match "
                    "observation_ids"
                )
        digest = _sha256(
            self.profile_manifest_sha256,
            field_name="profile_manifest_sha256",
        )
        if self.schema_version != RUN_PLAN_SCHEMA_VERSION:
            raise ContractError(
                f"unsupported run plan schema: {self.schema_version!r}"
            )
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "packets", packets)
        object.__setattr__(self, "observation_ids", observation_ids)
        object.__setattr__(
            self,
            "selected_profile_ids",
            selected_profile_ids,
        )
        object.__setattr__(self, "selected_profiles", selected_profiles)
        object.__setattr__(self, "profile_manifest_sha256", digest)
        for field_name in (
            "scope_manifest",
            "snapshot_prompts",
            "observation_plan",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, Mapping):
                raise ContractError(f"{field_name} must be an object")
            object.__setattr__(self, field_name, _freeze(value))
        for field_name in ("prompt_catalog_sha256", "geography_sha256"):
            value = getattr(self, field_name)
            if value:
                object.__setattr__(
                    self,
                    field_name,
                    _sha256(value, field_name=field_name),
                )
        supplied_plan_hash = _sha256(
            self.plan_sha256,
            field_name="plan_sha256",
            allow_empty=True,
        )
        object.__setattr__(self, "plan_sha256", supplied_plan_hash)

    @classmethod
    def _build_hashed(cls, **values: Any) -> "RunPlan":
        """Validate/freeze once, then seal the hash before publishing the plan.

        Keep direct construction's historical optional-hash contract. The
        planner owns this private factory and hashes the normalized, frozen
        fields rather than predicting dataclass defaults or normalization.
        """

        plan = cls(**values)
        material = {
            item.name: getattr(plan, item.name)
            for item in fields(plan)
            if item.name != "plan_sha256"
        }
        object.__setattr__(plan, "plan_sha256", _plan_sha256(material))
        return plan

    @property
    def config(self) -> MeasurementConfig:
        return self.measurement_config

    @property
    def packet_ids(self) -> tuple[str, ...]:
        return tuple(packet.packet_id for packet in self.packets)

    @property
    def policy_version(self) -> str:
        return self.measurement_config.execution_policy_version

    @property
    def prompt_catalog_version(self) -> str:
        return self.measurement_config.prompt_catalog_version

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return self.selected_profile_ids

    @property
    def profile_directories(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                identity.id: identity.chrome_directory
                for identity in self.selected_profiles
            }
        )

    @property
    def selected_chrome_directories(self) -> tuple[str, ...]:
        return tuple(
            identity.chrome_directory
            for identity in self.selected_profiles
        )


@dataclass(frozen=True)
class ArtifactReference:
    """Hash-bound reference relative to a run's artifact directory."""

    relative_path: str
    sha256: str
    byte_length: int
    media_type: str = "application/octet-stream"

    def __post_init__(self) -> None:
        path = str(self.relative_path or "").strip().replace("\\", "/")
        pure = PurePosixPath(path)
        if (
            not path
            or pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise ContractError(
                "artifact relative_path must stay inside the run directory"
            )
        object.__setattr__(self, "relative_path", str(pure))
        object.__setattr__(
            self,
            "sha256",
            _sha256(self.sha256, field_name="artifact sha256"),
        )
        object.__setattr__(
            self,
            "byte_length",
            _nonnegative_int(
                self.byte_length,
                field_name="artifact byte_length",
            ),
        )
        object.__setattr__(
            self,
            "media_type",
            _required_text(self.media_type, field_name="artifact media_type"),
        )


@dataclass(frozen=True)
class ArtifactReferences:
    """Separate independently hashed evidence for each measurement stage."""

    turn_1: Optional[ArtifactReference] = None
    turn_2: Optional[ArtifactReference] = None
    google_source: Optional[ArtifactReference] = None
    grounded_names: Optional[ArtifactReference] = None
    state: Optional[ArtifactReference] = None
    additional: tuple[ArtifactReference, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "turn_1",
            "turn_2",
            "google_source",
            "grounded_names",
            "state",
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, ArtifactReference):
                raise ContractError(
                    f"artifact_references.{field_name} must be an "
                    "ArtifactReference"
                )
        additional = tuple(self.additional)
        if any(not isinstance(item, ArtifactReference) for item in additional):
            raise ContractError(
                "artifact_references.additional must contain "
                "ArtifactReference values"
            )
        refs = tuple(
            value
            for value in (
                self.turn_1,
                self.turn_2,
                self.google_source,
                self.grounded_names,
                self.state,
                *additional,
            )
            if value is not None
        )
        paths = [reference.relative_path for reference in refs]
        if len(paths) != len(set(paths)):
            raise ContractError(
                "artifact references must use distinct relative paths"
            )
        if (
            self.turn_1 is not None
            and self.turn_2 is not None
            and self.turn_1.relative_path == self.turn_2.relative_path
        ):
            raise ContractError(
                "turn-1 and turn-2 evidence must remain separate"
            )
        object.__setattr__(self, "additional", additional)

    @property
    def all(self) -> tuple[ArtifactReference, ...]:
        return tuple(
            value
            for value in (
                self.turn_1,
                self.turn_2,
                self.google_source,
                self.grounded_names,
                self.state,
                *self.additional,
            )
            if value is not None
        )


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    severity: IssueSeverity = IssueSeverity.ERROR
    observation_id: Optional[str] = None
    packet_id: Optional[str] = None
    retry_safe: bool = False
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "code",
            _required_text(self.code, field_name="issue code"),
        )
        object.__setattr__(
            self,
            "message",
            _required_text(self.message, field_name="issue message"),
        )
        try:
            severity = (
                self.severity
                if isinstance(self.severity, IssueSeverity)
                else IssueSeverity(str(self.severity))
            )
        except ValueError as exc:
            raise ContractError(
                f"unknown issue severity: {self.severity!r}"
            ) from exc
        if not isinstance(self.retry_safe, bool):
            raise ContractError("issue retry_safe must be a boolean")
        if not isinstance(self.detail, Mapping):
            raise ContractError("issue detail must be an object")
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "detail", _freeze(self.detail))
        for field_name in ("observation_id", "packet_id"):
            value = str(getattr(self, field_name) or "").strip() or None
            object.__setattr__(self, field_name, value)

    @property
    def requires_attention(self) -> bool:
        return (
            self.severity is IssueSeverity.CRITICAL
            or not self.retry_safe
            and self.code
            in {
                "ambiguous_send",
                "identity_mismatch",
                "integrity_error",
            }
        )


@dataclass(frozen=True)
class GroundedOccurrence:
    """One runner-recorded exact occurrence in a captured answer."""

    name: Optional[str] = None
    position: Optional[int] = None
    character_start: Optional[int] = None
    character_end: Optional[int] = None
    byte_start: Optional[int] = None
    byte_end: Optional[int] = None

    def __post_init__(self) -> None:
        if self.name is not None and not isinstance(self.name, str):
            raise ContractError("grounded occurrence name must be text or None")
        for field_name in (
            "position",
            "character_start",
            "character_end",
            "byte_start",
            "byte_end",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _nonnegative_int(
                    value,
                    field_name=f"grounded occurrence {field_name}",
                )


@dataclass(frozen=True)
class ObservationResult:
    observation_id: str
    packet_id: str
    platform: Platform
    profile_id: str
    status: ObservationStatus
    extraction_status: ExtractionStatus
    prompt_sha256: Optional[str] = None
    chrome_directory: Optional[str] = None
    artifacts: ArtifactReferences = field(default_factory=ArtifactReferences)
    ordered_names: tuple[Optional[str], ...] = ()
    issues: tuple[Issue, ...] = ()
    transport_counts: TransportCounts = field(default_factory=TransportCounts)
    accepted_attempt_id: Optional[str] = None
    accepted_attempt_ids: Mapping[str, str] = field(default_factory=dict)
    attempt_ids: tuple[str, ...] = ()
    capture_evidence: Mapping[str, object] = field(default_factory=dict)
    grounded_occurrences: tuple[GroundedOccurrence, ...] = ()
    collection_metadata: Mapping[str, object] = field(default_factory=dict)
    client_research: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "client_research", _freeze(self.client_research))
        for field_name in ("observation_id", "packet_id", "profile_id"):
            object.__setattr__(
                self,
                field_name,
                _required_text(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        object.__setattr__(self, "platform", normalize_platform(self.platform))
        try:
            status = (
                self.status
                if isinstance(self.status, ObservationStatus)
                else ObservationStatus(str(self.status))
            )
            extraction_status = (
                self.extraction_status
                if isinstance(self.extraction_status, ExtractionStatus)
                else ExtractionStatus(str(self.extraction_status))
            )
        except ValueError as exc:
            raise ContractError("unknown observation result status") from exc
        names = tuple(self.ordered_names)
        if any(name is not None and not isinstance(name, str) for name in names):
            raise ContractError("ordered_names may contain only strings or None")
        grounded_occurrences = tuple(self.grounded_occurrences)
        if any(
            not isinstance(occurrence, GroundedOccurrence)
            for occurrence in grounded_occurrences
        ):
            raise ContractError(
                "grounded_occurrences must contain GroundedOccurrence values"
            )
        if grounded_occurrences and names != tuple(
            occurrence.name for occurrence in grounded_occurrences
        ):
            raise ContractError(
                "ordered_names must match grounded occurrence names exactly"
            )
        issues = tuple(self.issues)
        if any(not isinstance(issue, Issue) for issue in issues):
            raise ContractError("observation issues must contain Issue values")
        if not isinstance(self.artifacts, ArtifactReferences):
            raise ContractError(
                "observation artifacts must be ArtifactReferences"
            )
        if not isinstance(self.capture_evidence, Mapping):
            raise ContractError(
                "observation capture_evidence must be an object"
            )
        if not isinstance(self.collection_metadata, Mapping):
            raise ContractError("observation collection metadata must be an object")
        collection = dict(self.collection_metadata)
        dates = {"captured_at", "original_captured_at", "retrieved_at", "recovered_at", "request_sent_at", "provider_generated_at"}
        identifiers = {"source_attempt_id", "recovery_pass_id", "result_revision_id", "prior_revision_id", "parser_version", "validator_version"}
        if set(collection) - dates - identifiers:
            raise ContractError("unknown collection metadata field")
        for key, value in collection.items():
            if not isinstance(value, str) or not value or len(value) > 256 or any(ord(character) < 32 for character in value):
                raise ContractError("invalid collection metadata value")
            if key in dates:
                try:
                    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        raise ValueError
                except ValueError:
                    raise ContractError("collection timestamp must include its timezone") from None
        object.__setattr__(self, "collection_metadata", _freeze(collection))
        if not isinstance(self.transport_counts, TransportCounts):
            raise ContractError(
                "observation transport_counts must be TransportCounts"
            )
        prompt_sha256 = str(self.prompt_sha256 or "").strip()
        if prompt_sha256:
            prompt_sha256 = _sha256(
                prompt_sha256,
                field_name="observation prompt_sha256",
            )
        chrome_directory = (
            str(self.chrome_directory or "").strip() or None
        )
        accepted_attempt_id = (
            str(self.accepted_attempt_id or "").strip() or None
        )
        if not isinstance(self.accepted_attempt_ids, Mapping):
            raise ContractError(
                "accepted_attempt_ids must be an object"
            )
        accepted_attempt_ids = {
            _required_text(key, field_name="accepted attempt action"):
            _required_text(value, field_name="accepted attempt id")
            for key, value in self.accepted_attempt_ids.items()
        }
        attempt_ids = tuple(
            _required_text(value, field_name="attempt id")
            for value in self.attempt_ids
        )
        if len(attempt_ids) != len(set(attempt_ids)):
            raise ContractError("attempt_ids must be unique")
        referenced = set(accepted_attempt_ids.values())
        if accepted_attempt_id is not None:
            referenced.add(accepted_attempt_id)
        if not referenced.issubset(set(attempt_ids)):
            raise ContractError(
                "accepted attempt IDs must be present in attempt_ids"
            )
        if (
            status is ObservationStatus.SUCCEEDED
            and extraction_status
            not in {
                ExtractionStatus.SUCCEEDED,
                ExtractionStatus.NOT_REQUIRED,
            }
        ):
            raise ContractError(
                "a succeeded observation requires completed or not-required "
                "extraction"
            )
        if (
            status is ObservationStatus.NOT_SHOWN
            and extraction_status is not ExtractionStatus.NOT_REQUIRED
        ):
            raise ContractError(
                "Google not_shown must not create an extraction requirement"
            )
        capture_evidence: Mapping[str, object] = self.capture_evidence
        if (
            self.platform is Platform.GOOGLE_AI_OVERVIEW
            and status
            in {
                ObservationStatus.SUCCEEDED,
                ObservationStatus.NOT_SHOWN,
            }
        ):
            if self.artifacts.google_source is None:
                raise ContractError(
                    "successful Google observation lacks source evidence"
                )
            if not prompt_sha256:
                raise ContractError(
                    "Google terminal result lacks its prompt SHA-256"
                )
            if chrome_directory is None:
                raise ContractError(
                    "Google terminal result lacks its Chrome directory"
                )
            capture_evidence = canonical_gaio_terminal_evidence(
                self.capture_evidence,
                expected_state=(
                    "not_shown"
                    if status is ObservationStatus.NOT_SHOWN
                    else "shown"
                ),
                expected_prompt_sha256=prompt_sha256,
                expected_response_sha256=(
                    self.artifacts.google_source.sha256
                    if status is ObservationStatus.SUCCEEDED
                    else None
                ),
            )
            evidence_identity = capture_evidence[
                "browser_target_identity_binding"
            ]
            if str(evidence_identity.get("profile_id") or "") != self.profile_id:
                raise ContractError(
                    "Google terminal evidence is bound to a different profile"
                )
            if (
                str(evidence_identity.get("chrome_directory") or "")
                != chrome_directory
            ):
                raise ContractError(
                    "Google terminal evidence is bound to a different Chrome "
                    "directory"
                )
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "extraction_status", extraction_status)
        object.__setattr__(
            self,
            "prompt_sha256",
            prompt_sha256 or None,
        )
        object.__setattr__(
            self,
            "chrome_directory",
            chrome_directory,
        )
        object.__setattr__(
            self,
            "capture_evidence",
            _freeze(capture_evidence),
        )
        object.__setattr__(self, "ordered_names", names)
        object.__setattr__(
            self,
            "grounded_occurrences",
            grounded_occurrences,
        )
        object.__setattr__(self, "issues", issues)
        object.__setattr__(
            self,
            "accepted_attempt_id",
            accepted_attempt_id,
        )
        object.__setattr__(
            self,
            "accepted_attempt_ids",
            _freeze(accepted_attempt_ids),
        )
        object.__setattr__(self, "attempt_ids", attempt_ids)


@dataclass(frozen=True)
class PacketResult:
    packet_id: str
    status: PacketStatus
    observations: tuple[ObservationResult, ...]
    issues: tuple[Issue, ...] = ()
    artifacts: ArtifactReferences = field(default_factory=ArtifactReferences)
    transport_counts: TransportCounts = field(default_factory=TransportCounts)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "packet_id",
            _required_text(self.packet_id, field_name="packet_id"),
        )
        try:
            status = (
                self.status
                if isinstance(self.status, PacketStatus)
                else PacketStatus(str(self.status))
            )
        except ValueError as exc:
            raise ContractError("unknown packet status") from exc
        observations = tuple(self.observations)
        if any(
            not isinstance(observation, ObservationResult)
            for observation in observations
        ):
            raise ContractError(
                "packet observations must contain ObservationResult values"
            )
        if any(
            observation.packet_id != self.packet_id
            for observation in observations
        ):
            raise ContractError(
                "packet observation result has a different packet_id"
            )
        issues = tuple(self.issues)
        if any(not isinstance(issue, Issue) for issue in issues):
            raise ContractError("packet issues must contain Issue values")
        if not isinstance(self.artifacts, ArtifactReferences):
            raise ContractError("packet artifacts must be ArtifactReferences")
        if not isinstance(self.transport_counts, TransportCounts):
            raise ContractError(
                "packet transport_counts must be TransportCounts"
            )
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "observations", observations)
        object.__setattr__(self, "issues", issues)


@dataclass(frozen=True)
class RunResult:
    """Terminal or inspected module result; publication state is absent."""

    run_id: str
    status: RunStatus
    packets: tuple[PacketResult, ...]
    submission_budget: SubmissionBudget
    transport_counts: TransportCounts = field(default_factory=TransportCounts)
    issues: tuple[Issue, ...] = ()
    artifacts: ArtifactReferences = field(default_factory=ArtifactReferences)
    plan: Optional[RunPlan] = None
    policy_version: str = NONPUBLISHABLE_POLICY_VERSION

    def __post_init__(self) -> None:
        run_id = _required_text(self.run_id, field_name="run_id")
        try:
            status = (
                self.status
                if isinstance(self.status, RunStatus)
                else RunStatus(str(self.status))
            )
        except ValueError as exc:
            raise ContractError("unknown run status") from exc
        packets = tuple(self.packets)
        if any(not isinstance(packet, PacketResult) for packet in packets):
            raise ContractError("run packets must contain PacketResult values")
        packet_ids = [packet.packet_id for packet in packets]
        if len(packet_ids) != len(set(packet_ids)):
            raise ContractError("run packet results must have unique IDs")
        issues = tuple(self.issues)
        if any(not isinstance(issue, Issue) for issue in issues):
            raise ContractError("run issues must contain Issue values")
        if not isinstance(self.submission_budget, SubmissionBudget):
            raise ContractError(
                "run submission_budget must be a SubmissionBudget"
            )
        if not isinstance(self.transport_counts, TransportCounts):
            raise ContractError(
                "run transport_counts must be TransportCounts"
            )
        if not isinstance(self.artifacts, ArtifactReferences):
            raise ContractError("run artifacts must be ArtifactReferences")
        if self.plan is not None:
            if not isinstance(self.plan, RunPlan):
                raise ContractError("run plan must be a RunPlan")
            if self.plan.run_id != run_id:
                raise ContractError("run result and plan IDs do not match")
        if self.policy_version != NONPUBLISHABLE_POLICY_VERSION:
            raise ContractError("run result uses an unsupported policy")
        if status is RunStatus.SUCCEEDED:
            incomplete = [
                packet.packet_id
                for packet in packets
                if packet.status is not PacketStatus.SUCCEEDED
            ]
            if incomplete:
                raise ContractError(
                    "SUCCEEDED requires every packet to succeed: "
                    + ", ".join(incomplete)
                )
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "packets", packets)
        object.__setattr__(self, "issues", issues)

    @property
    def observations(self) -> tuple[ObservationResult, ...]:
        return tuple(
            observation
            for packet in self.packets
            for observation in packet.observations
        )

    @property
    def succeeded(self) -> bool:
        return self.status is RunStatus.SUCCEEDED


class ResumeAction(StringEnum):
    NOTHING_TO_DO = "nothing_to_do"
    RUN_PACKET = "run_packet"
    REFUSE_AMBIGUOUS = "refuse_ambiguous"
    REFUSE_INTEGRITY = "refuse_integrity"


@dataclass(frozen=True)
class ResumeDecision:
    action: ResumeAction
    packet_ids: tuple[str, ...] = ()
    issues: tuple[Issue, ...] = ()

    def __post_init__(self) -> None:
        try:
            action = (
                self.action
                if isinstance(self.action, ResumeAction)
                else ResumeAction(str(self.action))
            )
        except ValueError as exc:
            raise ContractError("unknown resume action") from exc
        packet_ids = tuple(
            _required_text(value, field_name="resume packet_id")
            for value in self.packet_ids
        )
        if len(packet_ids) != len(set(packet_ids)):
            raise ContractError("resume packet IDs must be unique")
        issues = tuple(self.issues)
        if any(not isinstance(issue, Issue) for issue in issues):
            raise ContractError("resume issues must contain Issue values")
        if action is ResumeAction.RUN_PACKET and not packet_ids:
            raise ContractError("RUN_PACKET requires at least one packet ID")
        if action is not ResumeAction.RUN_PACKET and packet_ids:
            raise ContractError(
                "only RUN_PACKET may authorize packet execution"
            )
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "packet_ids", packet_ids)
        object.__setattr__(self, "issues", issues)


# Concise compatibility aliases.
SnapshotRunPlan = RunPlan
SnapshotExecutionResult = RunResult


__all__ = [
    "ArtifactReference",
    "ArtifactReferences",
    "GroundedOccurrence",
    "Issue",
    "ObservationPlan",
    "ObservationResult",
    "PacketKind",
    "PacketPlan",
    "PacketResult",
    "RUN_PLAN_SCHEMA_VERSION",
    "ResumeAction",
    "ResumeDecision",
    "RunCounts",
    "RunPlan",
    "RunResult",
    "SnapshotExecutionResult",
    "SnapshotRunCounts",
    "SnapshotRunPlan",
    "SubmissionBudget",
    "SubmissionTopology",
    "TransportCounts",
    "WORKER_REQUEST_SCHEMA_VERSION",
    "canonical_json_bytes",
    "canonical_sha256",
]
