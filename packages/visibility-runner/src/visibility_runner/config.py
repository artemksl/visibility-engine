"""Immutable measurement and browser-runtime configuration.

This module is intentionally stdlib-only and reads no environment at import
time.  :meth:`RuntimeConfig.from_environment` is an explicit legacy adapter;
normal callers should construct :class:`RuntimeConfig` directly.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Final, Optional
from urllib.parse import urlsplit

from .errors import ConfigurationConflictError, ConfigurationError
from .contract_primitives import (
    PLATFORM_ALIASES as _PLATFORM_ALIASES,
    platform_key,
)
from .status import StringEnum


PROMPT_CATALOG_VERSION: Final[str] = "snapshot-prompts-v4"
DIAGNOSTIC_PROMPT_CATALOG_VERSION: Final[str] = "visibility-diagnostic-prompts-v1"
DIAGNOSTIC_POLICY_VERSION: Final[str] = "visibility_runner.diagnostic.v1"
NONPUBLISHABLE_POLICY_VERSION: Final[str] = "snapshot-module-measurement-v1"
MODULE_MEASUREMENT_EXECUTION_POLICY_VERSION: Final[str] = (
    NONPUBLISHABLE_POLICY_VERSION
)
MODULE_MEASUREMENT_PUBLICATION_ELIGIBLE: Final[bool] = False
MODULE_MEASUREMENT_PUBLICATION_BLOCKER: Final[str] = (
    "Browser measurement runs are permanently ineligible for prospect "
    "publication."
)
MAX_MEASUREMENT_REPETITIONS: Final[int] = 20
MAX_MODULE_MEASUREMENT_RUNS: Final[int] = MAX_MEASUREMENT_REPETITIONS
SUPPORTED_GEOGRAPHY_COUNTRY_CODES: Final[tuple[str, ...]] = ("GB",)
MAX_AUTOMATIC_RETRY_ATTEMPTS: Final[int] = 3
COPILOT_BROWSER_MEASUREMENT_MODE: Final[str] = "copilot_browser_same_chat_v1"
COPILOT_BROWSER_TURN_TIMEOUT_SECONDS: Final[float] = 240.0
GITHUB_COPILOT_LEGACY_REQUEST_POLICY: Final[str] = "github_copilot.sdk_default.v1"
GITHUB_COPILOT_OPTIMIZED_REQUEST_POLICY: Final[str] = "github_copilot.cost_optimized.v1"
GITHUB_COPILOT_REQUEST_POLICIES: Final[tuple[str, ...]] = (
    GITHUB_COPILOT_LEGACY_REQUEST_POLICY,
    GITHUB_COPILOT_OPTIMIZED_REQUEST_POLICY,
)

_SPACE_RE = re.compile(r"\s+")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_PROFILE_ID_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class Platform(StringEnum):
    """Browser measurement surfaces understood by the v4 prompt catalog."""

    CHATGPT = "chatgpt"
    GEMINI = "gemini"
    GOOGLE_AI_OVERVIEW = "google_ai_overview"
    COPILOT = "copilot"
    GITHUB_COPILOT = "github_copilot"


MODULE_MEASUREMENT_PLATFORMS: Final[tuple[str, ...]] = (
    Platform.CHATGPT.value,
    Platform.GEMINI.value,
    Platform.COPILOT.value,
    Platform.GOOGLE_AI_OVERVIEW.value,
    Platform.GITHUB_COPILOT.value,
)
# Google remains a recognized historical contract value, but is deferred for
# current measurement execution and omitted from newly selected defaults.
DEFAULT_MODULE_PLATFORMS: Final[tuple[Platform, ...]] = (
    Platform.CHATGPT,
    Platform.GEMINI,
    Platform.COPILOT,
)
DEFAULT_PLATFORMS = DEFAULT_MODULE_PLATFORMS

class LaunchPolicy(StringEnum):
    """Ownership policy for the shared persistent Chrome process."""

    ATTACH_ONLY = "attach_only"
    LAUNCH_IF_REQUIRED = "launch_if_required"


class BrowserDriver(StringEnum):
    """Closed driver selection; importing configuration loads no browser library."""

    PLAYWRIGHT = "playwright"
    PATCHRIGHT = "patchright"


_BROWSER_DRIVER_FIELDS = frozenset({
    "driver", "driver_version", "expected_browser_version", "driver_experiment",
})
_BROWSER_DRIVER_ENVIRONMENT = {
    "VISIBILITY_RUNNER_BROWSER_DRIVER": "driver",
    "VISIBILITY_RUNNER_BROWSER_DRIVER_VERSION": "driver_version",
    "VISIBILITY_RUNNER_EXPECTED_BROWSER_VERSION": "expected_browser_version",
    "VISIBILITY_RUNNER_BROWSER_DRIVER_EXPERIMENT": "driver_experiment",
}


def _exact_version(value: object, *, field_name: str, components: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){" + str(components - 1) + "}", value
    ):
        raise ConfigurationError(f"browser {field_name} must be an exact {components}-part version")
    return value


def _validate_driver_experiment(cdp_url: str, policy: LaunchPolicy, user_data_dir: Optional[Path]) -> None:
    """Reject shared production endpoints/state before any process is attached."""
    parsed = urlsplit(cdp_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("browser driver experiment requires an explicit loopback port") from exc
    if (
        parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or port is None or port == 9222 or port == 0
        or parsed.username is not None or parsed.password is not None
        or parsed.path or parsed.query or parsed.fragment
    ):
        raise ConfigurationError(
            "browser driver experiment requires a dedicated loopback CDP endpoint with an explicit non-9222 port"
        )
    if policy is not LaunchPolicy.ATTACH_ONLY:
        raise ConfigurationError("browser driver experiment requires attach_only")
    if user_data_dir is None:
        raise ConfigurationError("browser driver experiment requires a dedicated user_data_dir")
    home = Path.home().resolve(strict=False)
    personal_roots = (
        home / "Library/Application Support/Google/Chrome",
        home / "Library/Application Support/Chromium",
        home / ".config/google-chrome",
        home / ".config/chromium",
        home / "AppData/Local/Google/Chrome/User Data",
    )
    # Resolve symlinks too; a differently named link to a personal profile is
    # not isolated state. Parent directories cannot be used as profile roots.
    if user_data_dir == Path(user_data_dir.anchor) or user_data_dir == home or any(
        user_data_dir.is_relative_to(root.resolve(strict=False))
        or root.resolve(strict=False).is_relative_to(user_data_dir)
        for root in personal_roots
    ):
        raise ConfigurationError("browser driver experiment cannot use a personal Chrome user_data_dir")


def normalize_platform(value: object) -> Platform:
    try:
        return Platform(platform_key(value))
    except (KeyError, ValueError) as exc:
        known = ", ".join(platform.value for platform in Platform)
        raise ConfigurationError(
            f"Unknown measurement platform {value!r}. Known: {known}"
        ) from exc


def _clean(value: object, *, field_name: str) -> str:
    result = _SPACE_RE.sub(" ", str(value or "")).strip()
    if not result:
        raise ConfigurationError(f"{field_name} is required")
    return result


def _optional_clean(value: object) -> Optional[str]:
    result = _SPACE_RE.sub(" ", str(value or "")).strip()
    return result or None


def _slug(value: str, *, field_name: str) -> str:
    text = unicodedata.normalize("NFKD", value).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    result = _SLUG_RE.sub("-", text).strip("-")
    if not result:
        raise ConfigurationError(
            f"{field_name} cannot be converted to a stable id"
        )
    return result


def _treatment_key(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    return _SPACE_RE.sub(" ", _SLUG_RE.sub(" ", text)).strip()


def _country_code(value: object, *, field_name: str) -> str:
    result = _clean(value, field_name=field_name).upper()
    if len(result) != 2 or not result.isalpha():
        raise ConfigurationError(
            f"{field_name} must be a two-letter country code"
        )
    return result


def _mapping(value: object, *, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{field_name} must be an object")
    return value


def _optional_mapping(
    value: object,
    *,
    field_name: str,
) -> Mapping[str, object]:
    if value is None:
        return {}
    return _mapping(value, field_name=field_name)


def _sequence(value: object, *, field_name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ConfigurationError(f"{field_name} must be a list")
    return value


def _reject_unknown_keys(
    value: Mapping[object, object],
    *,
    allowed: set[str],
    field_name: str,
) -> None:
    unknown = sorted(str(key) for key in value if str(key) not in allowed)
    if unknown:
        raise ConfigurationError(
            f"{field_name} contains unsupported key(s): "
            + ", ".join(unknown)
        )


def _require_confirmed_if_supplied(
    value: object,
    *,
    field_name: str,
) -> None:
    if value is None:
        return
    supplied = _SPACE_RE.sub(" ", str(value)).strip()
    if supplied.casefold() != "confirmed":
        raise ConfigurationError(
            f"{field_name} must be 'confirmed' when supplied"
        )


def _path(value: object, *, field_name: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ConfigurationError(f"{field_name} is required")
    return Path(text).expanduser().resolve(strict=False)


def _optional_path(value: object) -> Optional[Path]:
    text = str(value or "").strip()
    return Path(text).expanduser().resolve(strict=False) if text else None


def _positive_float(value: object, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise ConfigurationError(f"{field_name} must be a positive number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(
            f"{field_name} must be a positive number"
        ) from exc
    if not math.isfinite(result) or result <= 0:
        raise ConfigurationError(f"{field_name} must be a positive number")
    return result


def _nonnegative_float(value: object, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise ConfigurationError(f"{field_name} must be a non-negative number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(
            f"{field_name} must be a non-negative number"
        ) from exc
    if not math.isfinite(result) or result < 0:
        raise ConfigurationError(
            f"{field_name} must be a non-negative number"
        )
    return result


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f"{field_name} must be a positive integer")
    return value


def _nonnegative_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigurationError(
            f"{field_name} must be a non-negative integer"
        )
    return value


def _strict_bool(value: object, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{field_name} must be a boolean")
    return value


def _alias_value(
    source: Mapping[str, object],
    names: tuple[str, ...],
    *,
    field_name: str,
) -> object:
    supplied = [(name, source[name]) for name in names if name in source]
    if not supplied:
        return None
    first_name, first_value = supplied[0]
    for name, value in supplied[1:]:
        if value != first_value:
            raise ConfigurationError(
                f"{first_name} and {name} cannot conflict for {field_name}"
            )
    return first_value


@dataclass(frozen=True)
class Treatment:
    """One application-confirmed treatment for an area."""

    name: str
    id: Optional[str] = None

    def __post_init__(self) -> None:
        name = _clean(self.name, field_name="treatment.name")
        identifier = _optional_clean(self.id) or _slug(
            name,
            field_name="treatment.name",
        )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "id", identifier)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "Treatment":
        source = _mapping(value, field_name="treatment")
        _reject_unknown_keys(
            source,
            allowed={"id", "name", "commercial_relevance_status"},
            field_name="treatment",
        )
        _require_confirmed_if_supplied(
            source.get("commercial_relevance_status"),
            field_name="treatment.commercial_relevance_status",
        )
        return cls(name=source.get("name"), id=source.get("id"))

    def to_mapping(self) -> dict[str, object]:
        return {"id": self.id, "name": self.name}

    def to_snapshot_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "commercial_relevance_status": "confirmed",
        }


@dataclass(frozen=True)
class Geography:
    """One confirmed patient-facing geography taxonomy value."""

    name: str
    id: Optional[str] = None
    country_code: str = "GB"

    def __post_init__(self) -> None:
        name = _clean(self.name, field_name="geography.name")
        identifier = _optional_clean(self.id) or _slug(
            name,
            field_name="geography.name",
        )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "id", identifier)
        object.__setattr__(
            self,
            "country_code",
            _country_code(
                self.country_code,
                field_name="geography.country_code",
            ),
        )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        *,
        default_country_code: str = "GB",
    ) -> "Geography":
        source = _mapping(value, field_name="geography")
        _reject_unknown_keys(
            source,
            allowed={
                "id",
                "area_id",
                "name",
                "area",
                "country_code",
                "confirmation_status",
            },
            field_name="geography",
        )
        _require_confirmed_if_supplied(
            source.get("confirmation_status"),
            field_name="geography.confirmation_status",
        )
        return cls(
            name=source.get("name") or source.get("area"),
            id=source.get("id") or source.get("area_id"),
            country_code=source.get("country_code") or default_country_code,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "country_code": self.country_code,
        }

    def to_snapshot_mapping(self) -> dict[str, object]:
        return {
            **self.to_mapping(),
            "confirmation_status": "confirmed",
        }


@dataclass(frozen=True)
class ClinicArchetype:
    """Application-confirmed clinic category used by archetype prompts."""

    singular: str
    plural: str
    id: Optional[str] = None

    def __post_init__(self) -> None:
        singular = _clean(
            self.singular,
            field_name="clinic.archetype.singular",
        )
        plural = _clean(
            self.plural,
            field_name="clinic.archetype.plural",
        )
        identifier = _optional_clean(self.id) or _slug(
            singular,
            field_name="clinic.archetype.singular",
        )
        object.__setattr__(self, "singular", singular)
        object.__setattr__(self, "plural", plural)
        object.__setattr__(self, "id", identifier)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> "ClinicArchetype":
        source = _mapping(value, field_name="clinic.archetype")
        _reject_unknown_keys(
            source,
            allowed={"id", "singular", "plural", "confirmation_status"},
            field_name="clinic.archetype",
        )
        _require_confirmed_if_supplied(
            source.get("confirmation_status"),
            field_name="clinic.archetype.confirmation_status",
        )
        return cls(
            singular=source.get("singular"),
            plural=source.get("plural"),
            id=source.get("id"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "singular": self.singular,
            "plural": self.plural,
        }

    def to_snapshot_mapping(self) -> dict[str, object]:
        return {
            **self.to_mapping(),
            "confirmation_status": "confirmed",
        }


@dataclass(frozen=True)
class Clinic:
    """Clinic identity supplied and confirmed by the embedding application."""

    name: str
    archetype: ClinicArchetype
    account_id: Optional[str] = None
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        name = _clean(self.name, field_name="clinic.name")
        if not isinstance(self.archetype, ClinicArchetype):
            raise ConfigurationError(
                "clinic.archetype must be a ClinicArchetype"
            )
        account_id = _optional_clean(self.account_id) or (
            f"application-{_slug(name, field_name='clinic.name')}"
        )
        if isinstance(self.aliases, (str, bytes)):
            raise ConfigurationError("clinic.aliases must be a list")
        aliases: list[str] = []
        seen = {name.casefold()}
        for index, value in enumerate(self.aliases):
            alias = _clean(value, field_name=f"clinic.aliases[{index}]")
            key = alias.casefold()
            if key in seen:
                raise ConfigurationError(
                    "clinic aliases must be unique and cannot repeat clinic.name"
                )
            seen.add(key)
            aliases.append(alias)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "account_id", account_id)
        object.__setattr__(self, "aliases", tuple(aliases))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "Clinic":
        source = _mapping(value, field_name="clinic")
        _reject_unknown_keys(
            source,
            allowed={
                "account_id",
                "name",
                "aliases",
                "verified_aliases",
                "identity_status",
                "archetype",
            },
            field_name="clinic",
        )
        _require_confirmed_if_supplied(
            source.get("identity_status"),
            field_name="clinic.identity_status",
        )
        aliases_value = _alias_value(
            source,
            ("aliases", "verified_aliases"),
            field_name="clinic.aliases",
        )
        return cls(
            name=source.get("name"),
            account_id=source.get("account_id"),
            aliases=tuple(
                _sequence(
                    aliases_value if aliases_value is not None else (),
                    field_name="clinic.aliases",
                )
            ),
            archetype=ClinicArchetype.from_mapping(
                _mapping(
                    source.get("archetype"),
                    field_name="clinic.archetype",
                )
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "name": self.name,
            "aliases": list(self.aliases),
            "archetype": self.archetype.to_mapping(),
        }

    def to_snapshot_mapping(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "name": self.name,
            "verified_aliases": list(self.aliases),
            "identity_status": "confirmed",
            "archetype": self.archetype.to_snapshot_mapping(),
        }


@dataclass(frozen=True)
class Area:
    """One branch/local area with isolated treatments and wider geographies."""

    name: str
    greater_area: Geography
    city: Geography
    treatments: tuple[Treatment, ...]
    id: Optional[str] = None
    branch_id: Optional[str] = None
    branch_name: Optional[str] = None
    country_code: str = "GB"
    station: Optional[str] = None
    postcode: Optional[str] = None
    landmark: Optional[str] = None

    def __post_init__(self) -> None:
        name = _clean(self.name, field_name="area.name")
        identifier = _optional_clean(self.id) or _slug(
            name,
            field_name="area.name",
        )
        country = _country_code(
            self.country_code,
            field_name="area.country_code",
        )
        if country not in SUPPORTED_GEOGRAPHY_COUNTRY_CODES:
            raise ConfigurationError(
                "area.country_code must use the frozen prompt geography "
                "country: "
                + ", ".join(SUPPORTED_GEOGRAPHY_COUNTRY_CODES)
            )
        if not isinstance(self.greater_area, Geography):
            raise ConfigurationError(
                "area.greater_area must be a Geography"
            )
        if not isinstance(self.city, Geography):
            raise ConfigurationError("area.city must be a Geography")
        if (
            self.greater_area.country_code != country
            or self.city.country_code != country
        ):
            raise ConfigurationError(
                "area and wider geographies must use the same country code"
            )
        if isinstance(self.treatments, (str, bytes)):
            raise ConfigurationError("area.treatments must be a list")
        treatments = tuple(self.treatments)
        if not 1 <= len(treatments) <= 3:
            raise ConfigurationError(
                "each area requires one to three treatments"
            )
        if any(not isinstance(value, Treatment) for value in treatments):
            raise ConfigurationError(
                "area.treatments may contain only Treatment values"
            )
        name_keys = [_treatment_key(value.name) for value in treatments]
        if len(name_keys) != len(set(name_keys)):
            raise ConfigurationError(
                "treatment names must be unique within each area"
            )
        id_keys = [str(value.id).casefold() for value in treatments]
        if len(id_keys) != len(set(id_keys)):
            raise ConfigurationError(
                "treatment ids must be unique within each area"
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "id", identifier)
        object.__setattr__(
            self,
            "branch_id",
            _optional_clean(self.branch_id) or identifier,
        )
        object.__setattr__(
            self,
            "branch_name",
            _optional_clean(self.branch_name) or name,
        )
        object.__setattr__(self, "country_code", country)
        object.__setattr__(self, "treatments", treatments)
        for field_name in ("station", "postcode", "landmark"):
            object.__setattr__(
                self,
                field_name,
                _optional_clean(getattr(self, field_name)),
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "Area":
        source = _mapping(value, field_name="area")
        _reject_unknown_keys(
            source,
            allowed={
                "name",
                "id",
                "area_id",
                "greater_area",
                "city",
                "treatments",
                "branch_id",
                "branch_name",
                "country_code",
                "station",
                "postcode",
                "landmark",
                "variants",
                "catchment",
                "geographies",
                "branch",
                "validation_status",
                "branch_treatment_validation_status",
                "validated_by",
            },
            field_name="area",
        )
        _require_confirmed_if_supplied(
            source.get("validation_status"),
            field_name="area.validation_status",
        )
        _require_confirmed_if_supplied(
            source.get("branch_treatment_validation_status"),
            field_name="area.branch_treatment_validation_status",
        )
        catchment = _optional_mapping(
            source.get("catchment"),
            field_name="area.catchment",
        )
        _reject_unknown_keys(
            catchment,
            allowed={
                "area_id",
                "name",
                "area",
                "country_code",
                "confirmation_status",
                "variants",
            },
            field_name="area.catchment",
        )
        _require_confirmed_if_supplied(
            catchment.get("confirmation_status"),
            field_name="area.catchment.confirmation_status",
        )
        geographies = _optional_mapping(
            source.get("geographies"),
            field_name="area.geographies",
        )
        _reject_unknown_keys(
            geographies,
            allowed={"local", "greater_area", "city"},
            field_name="area.geographies",
        )
        branch = _optional_mapping(
            source.get("branch"),
            field_name="area.branch",
        )
        _reject_unknown_keys(
            branch,
            allowed={"id", "name"},
            field_name="area.branch",
        )
        country = (
            source.get("country_code")
            or catchment.get("country_code")
            or "GB"
        )
        name = (
            source.get("name")
            or catchment.get("name")
            or catchment.get("area")
        )
        identifier = (
            source.get("id")
            or source.get("area_id")
            or catchment.get("area_id")
        )
        if isinstance(geographies.get("local"), Mapping):
            local = Geography.from_mapping(
                geographies["local"],
                default_country_code=str(country),
            )
            expected_name = _clean(name, field_name="area.name")
            expected_id = _optional_clean(identifier) or _slug(
                local.name,
                field_name="area.name",
            )
            expected_country = _country_code(
                country,
                field_name="area.country_code",
            )
            if (
                expected_name != local.name
                or expected_id != local.id
                or expected_country != local.country_code
            ):
                raise ConfigurationError(
                    "area.geographies.local must match the area id, name, "
                    "and country"
                )
        variants: dict[str, str] = {}
        raw_variants = (
            source.get("variants")
            if "variants" in source
            else catchment.get("variants")
        )
        if isinstance(raw_variants, Mapping):
            variants = {
                str(key): str(item)
                for key, item in raw_variants.items()
                if item is not None
            }
        elif raw_variants is not None:
            for row in _sequence(
                raw_variants,
                field_name="area.variants",
            ):
                row_mapping = _mapping(
                    row,
                    field_name="area.variants[]",
                )
                _reject_unknown_keys(
                    row_mapping,
                    allowed={"type", "kind", "value", "name"},
                    field_name="area.variants[]",
                )
                key = str(
                    row_mapping.get("type")
                    or row_mapping.get("kind")
                    or ""
                )
                item = str(
                    row_mapping.get("value")
                    or row_mapping.get("name")
                    or ""
                )
                if key and item:
                    variants[key] = item
        treatment_values = _sequence(
            source.get("treatments"),
            field_name="area.treatments",
        )
        greater_source = (
            source.get("greater_area")
            or geographies.get("greater_area")
        )
        city_source = source.get("city") or geographies.get("city")
        return cls(
            name=name,
            id=identifier,
            branch_id=source.get("branch_id") or branch.get("id"),
            branch_name=source.get("branch_name") or branch.get("name"),
            country_code=country,
            greater_area=Geography.from_mapping(
                _mapping(
                    greater_source,
                    field_name="area.greater_area",
                ),
                default_country_code=str(country),
            ),
            city=Geography.from_mapping(
                _mapping(city_source, field_name="area.city"),
                default_country_code=str(country),
            ),
            treatments=tuple(
                Treatment.from_mapping(
                    _mapping(row, field_name="area.treatments[]")
                )
                if isinstance(row, Mapping)
                else Treatment(name=row)
                for row in treatment_values
            ),
            station=source.get("station") or variants.get("station"),
            postcode=source.get("postcode") or variants.get("postcode"),
            landmark=source.get("landmark") or variants.get("landmark"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "branch_id": self.branch_id,
            "branch_name": self.branch_name,
            "country_code": self.country_code,
            "greater_area": self.greater_area.to_mapping(),
            "city": self.city.to_mapping(),
            "treatments": [item.to_mapping() for item in self.treatments],
            "station": self.station,
            "postcode": self.postcode,
            "landmark": self.landmark,
        }

    def to_snapshot_mapping(self) -> dict[str, object]:
        variants = [{"type": "area", "value": self.name}]
        variants.extend(
            {"type": kind, "value": value}
            for kind, value in (
                ("station", self.station),
                ("postcode", self.postcode),
                ("landmark", self.landmark),
            )
            if value
        )
        local = Geography(
            name=self.name,
            id=self.id,
            country_code=self.country_code,
        )
        return {
            "branch": {"id": self.branch_id, "name": self.branch_name},
            "catchment": {
                "area_id": self.id,
                "name": self.name,
                "country_code": self.country_code,
                "confirmation_status": "confirmed",
                "variants": variants,
            },
            "geographies": {
                "local": local.to_snapshot_mapping(),
                "greater_area": self.greater_area.to_snapshot_mapping(),
                "city": self.city.to_snapshot_mapping(),
            },
            "treatments": [
                treatment.to_snapshot_mapping()
                for treatment in self.treatments
            ],
            "validation_status": "confirmed",
            "validated_by": "application",
        }


@dataclass(frozen=True)
class GitHubCopilotConfig:
    """Frozen source identity and source-to-parser policy; never credentials."""

    account_ref: str
    github_account_id: str
    source_method: str = "github_copilot_web_v1"
    model: str = "auto"
    repetitions: int = 1
    max_source_attempts: int = 2
    max_parser_attempts: int = 2
    source_timeout_seconds: float = 240.0
    parser_timeout_seconds: float = 240.0
    reconciliation_grace_seconds: float = 30.0
    sdk_version: str = "1.0.13"
    runtime_version: str = "1.0.83"
    parser_contract_version: str = "free_snapshot.source_text_name_extraction.v1"
    prompt_version: str = "github_copilot.catalog_copilot.v1"
    tool_call_limit: int = 10
    # None preserves the absence of this field in historical frozen mappings.
    # New callers select a version explicitly; omission retains SDK defaults.
    request_policy_version: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("account_ref", "github_account_id"):
            object.__setattr__(self, name, _clean(getattr(self, name), field_name=f"github_copilot.{name}"))
        if self.source_method not in {"github_copilot_web_v1", "github_copilot_model_only_v1"}:
            raise ConfigurationError("github_copilot.source_method is unsupported")
        if self.model != "auto":
            raise ConfigurationError("GitHub Copilot Student requires model=auto")
        if self.request_policy_version is not None and self.request_policy_version not in GITHUB_COPILOT_REQUEST_POLICIES:
            raise ConfigurationError("github_copilot.request_policy_version is unsupported")
        for name, maximum in (("repetitions", MAX_MEASUREMENT_REPETITIONS), ("max_source_attempts", 3), ("max_parser_attempts", 3), ("tool_call_limit", 100)):
            value = _positive_int(getattr(self, name), field_name=f"github_copilot.{name}")
            if value > maximum:
                raise ConfigurationError(f"github_copilot.{name} must not exceed {maximum}")
        for name in ("source_timeout_seconds", "parser_timeout_seconds", "reconciliation_grace_seconds"):
            object.__setattr__(self, name, _positive_float(getattr(self, name), field_name=f"github_copilot.{name}"))
        for name, expected in (("sdk_version", "1.0.13"), ("runtime_version", "1.0.83"), ("parser_contract_version", "free_snapshot.source_text_name_extraction.v1"), ("prompt_version", "github_copilot.catalog_copilot.v1")):
            if getattr(self, name) != expected:
                raise ConfigurationError(f"github_copilot.{name} must equal {expected}")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "GitHubCopilotConfig":
        source = _mapping(value, field_name="github_copilot")
        _reject_unknown_keys(source, allowed=set(cls.__dataclass_fields__), field_name="github_copilot")
        if "request_policy_version" in source and source["request_policy_version"] not in GITHUB_COPILOT_REQUEST_POLICIES:
            raise ConfigurationError("github_copilot.request_policy_version is unsupported")
        for name in ("account_ref", "github_account_id"):
            _clean(source.get(name), field_name=f"github_copilot.{name}")
        return cls(**dict(source))

    @property
    def effective_request_policy_version(self) -> str:
        return self.request_policy_version or GITHUB_COPILOT_LEGACY_REQUEST_POLICY

    def __json_value__(self) -> dict[str, object]:
        return self.to_mapping()

    def to_mapping(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__
                if name != "request_policy_version" or self.request_policy_version is not None}


@dataclass(frozen=True)
class GitHubCopilotRuntimeConfig:
    """Explicit OS credential reference and local SDK worker settings."""

    account_ref: str
    github_account_id: str
    credential_ref: str
    node_binary: str = "node"
    worker_path: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("account_ref", "github_account_id", "credential_ref", "node_binary"):
            object.__setattr__(self, name, _clean(getattr(self, name), field_name=f"github_copilot.{name}"))
        parts = self.credential_ref.split(":", 2)
        if len(parts) != 3 or parts[0] != "keychain" or not all(parts[1:]):
            raise ConfigurationError("github_copilot.credential_ref must be keychain:<service>:<account>")
        if self.worker_path is not None:
            object.__setattr__(self, "worker_path", str(_path(self.worker_path, field_name="github_copilot.worker_path")))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "GitHubCopilotRuntimeConfig":
        source = _mapping(value, field_name="github_copilot runtime")
        _reject_unknown_keys(source, allowed=set(cls.__dataclass_fields__), field_name="github_copilot runtime")
        for name in ("account_ref", "github_account_id", "credential_ref"):
            _clean(source.get(name), field_name=f"github_copilot.{name}")
        return cls(**dict(source))

    def to_mapping(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__ if getattr(self, name) is not None}


@dataclass(frozen=True)
class MeasurementConfig:
    """Evidence configuration for one application-owned browser measurement."""

    clinic: Optional[Clinic] = None
    areas: tuple[Area, ...] = ()
    repetitions: int = 1
    platforms: tuple[Platform, ...] = DEFAULT_MODULE_PLATFORMS
    outside_in: bool = True
    prompt_catalog_version: str = PROMPT_CATALOG_VERSION
    execution_policy_version: str = NONPUBLISHABLE_POLICY_VERSION
    scope_kind: str = "clinic"
    clinic_archetype: Optional[ClinicArchetype] = None
    github_copilot: Optional[GitHubCopilotConfig] = None
    diagnostic_prompts: tuple[str, ...] = ()
    client_research: Optional[Mapping[str, object]] = None

    def __post_init__(self) -> None:
        from .client_research import CLIENT_RESEARCH_SCOPES, CLIENT_RESEARCH_PROMPT_VERSION, validate_client_config
        research = self.scope_kind in CLIENT_RESEARCH_SCOPES
        diagnostic = self.scope_kind == "diagnostic"
        if research:
            if self.clinic is not None or self.clinic_archetype is not None or self.areas or self.diagnostic_prompts or self.github_copilot is not None or self.repetitions != 1:
                raise ConfigurationError("client research scope must omit legacy inputs and use repetitions=1")
            if self.prompt_catalog_version == PROMPT_CATALOG_VERSION:
                object.__setattr__(self, "prompt_catalog_version", CLIENT_RESEARCH_PROMPT_VERSION)
        elif self.client_research is not None:
            raise ConfigurationError("client_research requires a client research scope")
        if diagnostic:
            if self.clinic is not None or self.clinic_archetype is not None or self.areas:
                raise ConfigurationError("diagnostic scope must omit clinic, archetype and areas")
            if isinstance(self.diagnostic_prompts, (str, bytes)) or not isinstance(self.diagnostic_prompts, (list, tuple)):
                raise ConfigurationError("diagnostic_prompts must be a list")
            if not self.diagnostic_prompts or any(not isinstance(text, str) or not text.strip() for text in self.diagnostic_prompts):
                raise ConfigurationError("diagnostic_prompts must contain non-empty strings")
            object.__setattr__(self, "diagnostic_prompts", tuple(self.diagnostic_prompts))
            if self.repetitions != 1:
                raise ConfigurationError("diagnostic scope requires repetitions=1; repeat entries explicitly")
            if self.prompt_catalog_version == PROMPT_CATALOG_VERSION:
                object.__setattr__(self, "prompt_catalog_version", DIAGNOSTIC_PROMPT_CATALOG_VERSION)
        elif self.diagnostic_prompts:
            raise ConfigurationError("diagnostic_prompts requires diagnostic scope")
        if self.scope_kind == "area_treatments":
            if self.clinic is not None:
                raise ConfigurationError("area_treatments scope must not contain a clinic")
            if self.clinic_archetype is not None and not isinstance(self.clinic_archetype, ClinicArchetype):
                raise ConfigurationError("clinic_archetype must be a ClinicArchetype when provided")
        elif self.scope_kind == "clinic":
            if not isinstance(self.clinic, Clinic):
                raise ConfigurationError("clinic must be a Clinic")
            if self.clinic_archetype is not None:
                raise ConfigurationError("clinic scope uses clinic.archetype, not clinic_archetype")
        elif not diagnostic and not research:
            raise ConfigurationError("unsupported scope_kind")
        if isinstance(self.areas, (str, bytes)):
            raise ConfigurationError("areas must be a list")
        areas = tuple(self.areas)
        if not diagnostic and not research and not 1 <= len(areas) <= 2:
            raise ConfigurationError("one or two areas are required")
        if any(not isinstance(area, Area) for area in areas):
            raise ConfigurationError("areas may contain only Area values")
        if isinstance(self.repetitions, bool) or not isinstance(
            self.repetitions,
            int,
        ):
            raise ConfigurationError("repetitions must be an integer")
        if not 1 <= self.repetitions <= MAX_MEASUREMENT_REPETITIONS:
            raise ConfigurationError(
                "repetitions must be between 1 and "
                f"{MAX_MEASUREMENT_REPETITIONS}"
            )
        if isinstance(self.platforms, (str, bytes)):
            raw_platforms: tuple[object, ...] = (self.platforms,)
        else:
            raw_platforms = tuple(self.platforms)
        if not raw_platforms:
            raise ConfigurationError("at least one platform is required")
        platforms: list[Platform] = []
        for value in raw_platforms:
            normalized = normalize_platform(value)
            if normalized.value not in MODULE_MEASUREMENT_PLATFORMS:
                raise ConfigurationError(
                    "platform is excluded from module measurements"
                )
            if normalized in platforms:
                raise ConfigurationError(
                    "platforms must be unique after alias normalization"
                )
            platforms.append(normalized)
        if diagnostic and (len(platforms) != 1 or platforms[0] not in (Platform.CHATGPT, Platform.GEMINI, Platform.COPILOT, Platform.GITHUB_COPILOT)):
            raise ConfigurationError("diagnostic scope requires exactly one of chatgpt, gemini, copilot or github_copilot")
        if Platform.GITHUB_COPILOT in platforms:
            if not isinstance(self.github_copilot, GitHubCopilotConfig):
                raise ConfigurationError("github_copilot requires an explicit account and source configuration")
            if diagnostic and self.github_copilot.repetitions != 1:
                raise ConfigurationError("diagnostic github_copilot requires repetitions=1")
        elif self.github_copilot is not None:
            raise ConfigurationError("github_copilot configuration requires the github_copilot platform")
        area_ids = [str(area.id).casefold() for area in areas]
        branch_ids = [str(area.branch_id).casefold() for area in areas]
        if len(area_ids) != len(set(area_ids)):
            raise ConfigurationError("area ids must be unique")
        if len(branch_ids) != len(set(branch_ids)):
            raise ConfigurationError("branch ids must be unique")
        treatment_names_by_id: dict[str, str] = {}
        treatment_ids_by_name: dict[str, str] = {}
        for area in areas:
            for treatment in area.treatments:
                id_key = str(treatment.id).casefold()
                name_key = _treatment_key(treatment.name)
                previous_name = treatment_names_by_id.setdefault(
                    id_key,
                    name_key,
                )
                previous_id = treatment_ids_by_name.setdefault(
                    name_key,
                    id_key,
                )
                if previous_name != name_key or previous_id != id_key:
                    raise ConfigurationError(
                        "the same treatment must use one stable id and name "
                        "across all areas"
                    )
        if not isinstance(self.outside_in, bool):
            raise ConfigurationError("outside_in must be a boolean")
        if research:
            from .models import _freeze
            object.__setattr__(self, "client_research", _freeze(validate_client_config(self.scope_kind, self.client_research, [p.value for p in platforms])))
        expected_catalog_version = CLIENT_RESEARCH_PROMPT_VERSION if research else DIAGNOSTIC_PROMPT_CATALOG_VERSION if diagnostic else PROMPT_CATALOG_VERSION
        if self.prompt_catalog_version != expected_catalog_version:
            raise ConfigurationError(
                f"module measurements require {expected_catalog_version}"
            )
        if self.execution_policy_version != NONPUBLISHABLE_POLICY_VERSION:
            raise ConfigurationError(
                "module measurement execution policy is unsupported"
            )
        object.__setattr__(self, "areas", areas)
        object.__setattr__(self, "platforms", tuple(platforms))

    @property
    def archetype(self) -> Optional[ClinicArchetype]:
        if self.scope_kind == "diagnostic" or self.scope_kind.startswith("client_research_"):
            return None
        if self.scope_kind == "area_treatments":
            return self.clinic_archetype
        return self.clinic.archetype

    def __json_value__(self) -> dict[str, object]:
        """Keep existing clinic plan hashes stable when additive fields are absent."""
        return self.to_mapping()

    @property
    def prompt_pack_version(self) -> str:
        """Compatibility spelling used by frozen Snapshot v4 evidence."""

        return self.prompt_catalog_version

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> "MeasurementConfig":
        source = _mapping(value, field_name="measurement config")
        _reject_unknown_keys(
            source,
            allowed={
                "clinic",
                "github_copilot",
                "scope_kind",
                "clinic_archetype",
                "diagnostic_prompts",
                "client_research",
                "areas",
                "repetitions",
                "planned_runs",
                "platforms",
                "outside_in",
                "prompt_catalog_version",
                "prompt_pack_version",
                "execution_policy_version",
                "platform_name_reconciliation",
            },
            field_name="measurement config",
        )
        repetitions = _alias_value(
            source,
            ("repetitions", "planned_runs"),
            field_name="repetitions",
        )
        prompt_catalog = _alias_value(
            source,
            ("prompt_catalog_version", "prompt_pack_version"),
            field_name="prompt_catalog_version",
        )
        if source.get("platform_name_reconciliation", True) is not True:
            raise ConfigurationError(
                "platform_name_reconciliation must remain enabled"
            )
        scope_kind = source.get("scope_kind", "clinic")
        from .client_research import CLIENT_RESEARCH_SCOPES
        if scope_kind not in ("clinic", "area_treatments", "diagnostic", *CLIENT_RESEARCH_SCOPES):
            raise ConfigurationError("unsupported scope_kind")
        if scope_kind in CLIENT_RESEARCH_SCOPES and any(key in source for key in ("clinic", "clinic_archetype", "areas", "diagnostic_prompts", "github_copilot")):
            raise ConfigurationError("client research scope must omit legacy inputs")
        if scope_kind == "diagnostic" and any(key in source for key in ("clinic", "clinic_archetype", "areas")):
            raise ConfigurationError("diagnostic scope must omit clinic, archetype and areas")
        if scope_kind != "diagnostic" and "diagnostic_prompts" in source:
            raise ConfigurationError("diagnostic_prompts requires diagnostic scope")
        if scope_kind == "area_treatments" and "clinic" in source:
            raise ConfigurationError("area_treatments scope must omit clinic")
        if scope_kind == "clinic" and "clinic_archetype" in source:
            raise ConfigurationError("clinic scope uses clinic.archetype, not clinic_archetype")
        return cls(
            github_copilot=(GitHubCopilotConfig.from_mapping(source["github_copilot"]) if source.get("github_copilot") is not None else None),
            scope_kind=scope_kind,
            client_research=source.get("client_research"),
            diagnostic_prompts=tuple(_sequence(source.get("diagnostic_prompts"), field_name="diagnostic_prompts")) if scope_kind == "diagnostic" else (),
            clinic=(
                Clinic.from_mapping(_mapping(source.get("clinic"), field_name="clinic"))
                if scope_kind == "clinic" else None
            ),
            clinic_archetype=(
                ClinicArchetype.from_mapping(
                    _mapping(source.get("clinic_archetype"), field_name="clinic_archetype")
                )
                if scope_kind == "area_treatments" and source.get("clinic_archetype") is not None else None
            ),
            areas=tuple(
                Area.from_mapping(
                    _mapping(row, field_name="areas[]")
                )
                for row in _sequence(
                    source.get("areas", () if scope_kind == "diagnostic" or scope_kind in CLIENT_RESEARCH_SCOPES else None),
                    field_name="areas",
                )
            ),
            repetitions=1 if repetitions is None else repetitions,
            platforms=tuple(
                _sequence(
                    source.get("platforms", DEFAULT_MODULE_PLATFORMS),
                    field_name="platforms",
                )
            ),
            outside_in=source.get("outside_in", True),
            prompt_catalog_version=(
                PROMPT_CATALOG_VERSION
                if prompt_catalog is None
                else str(prompt_catalog)
            ),
            execution_policy_version=str(
                source.get(
                    "execution_policy_version",
                    NONPUBLISHABLE_POLICY_VERSION,
                )
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        if self.scope_kind.startswith("client_research_"):
            from .contract_primitives import json_value
            return {"scope_kind":self.scope_kind,"client_research":json_value(self.client_research),
                    "repetitions":1,"platforms":[p.value for p in self.platforms],"outside_in":self.outside_in,
                    "prompt_catalog_version":self.prompt_catalog_version,"execution_policy_version":self.execution_policy_version}
        if self.scope_kind == "diagnostic":
            return {
                "scope_kind": "diagnostic",
                "diagnostic_prompts": list(self.diagnostic_prompts),
                "repetitions": 1,
                "platforms": [platform.value for platform in self.platforms],
                "outside_in": self.outside_in,
                "prompt_catalog_version": self.prompt_catalog_version,
                "execution_policy_version": self.execution_policy_version,
                **({"github_copilot": self.github_copilot.to_mapping()} if self.github_copilot is not None else {}),
            }
        identity = (
            {"scope_kind": self.scope_kind, **({"clinic_archetype": self.archetype.to_mapping()} if self.archetype is not None else {})}
            if self.scope_kind == "area_treatments"
            else {"clinic": self.clinic.to_mapping()}
        )
        return {
            **({"github_copilot": self.github_copilot.to_mapping()} if self.github_copilot is not None else {}),
            **identity,
            "areas": [area.to_mapping() for area in self.areas],
            "repetitions": self.repetitions,
            "platforms": [platform.value for platform in self.platforms],
            "outside_in": self.outside_in,
            "prompt_catalog_version": self.prompt_catalog_version,
            "execution_policy_version": self.execution_policy_version,
        }

    def to_snapshot_mapping(self) -> dict[str, object]:
        """Return the existing workflow's JSON-compatible v4 input shape."""

        if self.scope_kind == "diagnostic" or self.scope_kind.startswith("client_research_"):
            result = self.to_mapping()
            result["planned_runs"] = result.pop("repetitions")
            result["prompt_pack_version"] = result.pop("prompt_catalog_version")
            result["platform_name_reconciliation"] = True
            return result

        identity = (
            {"scope_kind": self.scope_kind, **({"clinic_archetype": self.archetype.to_snapshot_mapping()} if self.archetype is not None else {})}
            if self.scope_kind == "area_treatments"
            else {"clinic": self.clinic.to_snapshot_mapping()}
        )
        return {
            **({"github_copilot": self.github_copilot.to_mapping()} if self.github_copilot is not None else {}),
            "prompt_pack_version": self.prompt_catalog_version,
            "execution_policy_version": self.execution_policy_version,
            "planned_runs": self.repetitions,
            "outside_in": self.outside_in,
            **identity,
            "areas": [area.to_snapshot_mapping() for area in self.areas],
            "platforms": [platform.value for platform in self.platforms],
            "platform_name_reconciliation": True,
        }


@dataclass(frozen=True)
class ProfileIdentity:
    """One stable manifest identity and its exact raw Chrome directory."""

    id: str
    display_name: str
    chrome_directory: str
    project_url: str = ""

    def __post_init__(self) -> None:
        identifier = str(self.id or "").strip()
        if not _PROFILE_ID_RE.fullmatch(identifier):
            raise ConfigurationError(
                "profile id must contain lowercase letters, numbers, and "
                "single underscores only"
            )
        display_name = str(self.display_name or "").strip()
        chrome_directory = str(self.chrome_directory or "").strip()
        if not display_name:
            raise ConfigurationError("profile display_name is required")
        if not chrome_directory:
            raise ConfigurationError("profile chrome_directory is required")
        object.__setattr__(self, "id", identifier)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "chrome_directory", chrome_directory)
        object.__setattr__(
            self,
            "project_url",
            str(self.project_url or "").strip(),
        )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> "ProfileIdentity":
        source = _mapping(value, field_name="profile")
        _reject_unknown_keys(
            source,
            allowed={
                "id",
                "display_name",
                "chrome_directory",
                "project_url",
                "profile",
                "label",
            },
            field_name="profile",
        )
        directory = _alias_value(
            source,
            ("chrome_directory", "profile"),
            field_name="profile chrome_directory",
        )
        supplied_display_name = _alias_value(
            source,
            ("display_name", "label"),
            field_name="profile display_name",
        )
        display_name = supplied_display_name or directory
        identifier = str(source.get("id") or "").strip()
        if not identifier:
            legacy = str(display_name or directory or "").strip()
            wrapped = re.fullmatch(
                r"loggedin\((.+)\)",
                legacy,
                flags=re.IGNORECASE,
            )
            if wrapped:
                legacy = wrapped.group(1)
            elif re.fullmatch(r"loggedin\d+", legacy, flags=re.IGNORECASE):
                legacy = re.sub(
                    r"(?i)^loggedin",
                    "loggedin_",
                    legacy,
                )
            identifier = re.sub(
                r"[^a-z0-9]+",
                "_",
                legacy.casefold(),
            ).strip("_")
        return cls(
            id=identifier,
            display_name=display_name,
            chrome_directory=directory,
            project_url=str(source.get("project_url") or ""),
        )

    def to_mapping(self) -> dict[str, str]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "chrome_directory": self.chrome_directory,
            "project_url": self.project_url,
        }


@dataclass(frozen=True)
class ProfileConfig:
    """Pinned exact profile selection for a run.

    The manifest is verified by the runtime before browser attachment.  A
    profile count is intentionally absent: it is always derivable from the
    exact ordered IDs.
    """

    selected_profile_ids: tuple[str, ...]
    manifest_path: Path
    manifest_sha256: str
    selected_profiles: tuple[ProfileIdentity, ...] = ()
    lease_timeout_seconds: float = 300.0
    lease_lock_root: Optional[Path] = None

    def __post_init__(self) -> None:
        if isinstance(self.selected_profile_ids, (str, bytes)):
            raise ConfigurationError(
                "selected_profile_ids must be a list of exact IDs"
            )
        identifiers = tuple(
            str(value or "").strip()
            for value in self.selected_profile_ids
        )
        if not identifiers:
            raise ConfigurationError(
                "at least one selected_profile_id is required"
            )
        for identifier in identifiers:
            if not _PROFILE_ID_RE.fullmatch(identifier):
                raise ConfigurationError(
                    f"invalid stable profile id: {identifier!r}"
                )
        if len(identifiers) != len(set(identifiers)):
            raise ConfigurationError(
                "selected_profile_ids must be exact and unique"
            )
        if isinstance(self.selected_profiles, (str, bytes)):
            raise ConfigurationError("selected_profiles must be a list")
        identities = tuple(self.selected_profiles)
        if any(
            not isinstance(identity, ProfileIdentity)
            for identity in identities
        ):
            raise ConfigurationError(
                "selected_profiles may contain only ProfileIdentity values"
            )
        if identities:
            identity_ids = tuple(identity.id for identity in identities)
            if identity_ids != identifiers:
                raise ConfigurationError(
                    "selected_profiles must match selected_profile_ids in "
                    "exact order"
                )
            directories = tuple(
                identity.chrome_directory for identity in identities
            )
            if len(directories) != len(set(directories)):
                raise ConfigurationError(
                    "selected_profiles must use distinct raw Chrome "
                    "directories"
                )
        manifest_path = _path(
            self.manifest_path,
            field_name="profile manifest_path",
        )
        digest = str(self.manifest_sha256 or "").strip().casefold()
        if not _SHA256_RE.fullmatch(digest):
            raise ConfigurationError(
                "profile manifest_sha256 must be exactly 64 hexadecimal "
                "characters"
            )
        object.__setattr__(self, "selected_profile_ids", identifiers)
        object.__setattr__(self, "selected_profiles", identities)
        object.__setattr__(self, "manifest_path", manifest_path)
        object.__setattr__(self, "manifest_sha256", digest)
        object.__setattr__(
            self,
            "lease_timeout_seconds",
            _positive_float(
                self.lease_timeout_seconds,
                field_name="profile lease_timeout_seconds",
            ),
        )
        object.__setattr__(
            self,
            "lease_lock_root",
            _optional_path(self.lease_lock_root),
        )

    @property
    def profile_count(self) -> int:
        return len(self.selected_profile_ids)

    @property
    def profile_directories(self) -> Mapping[str, str]:
        """Stable ID to exact raw Chrome directory mapping."""

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

    @property
    def profile_manifest_path(self) -> Path:
        return self.manifest_path

    @property
    def profile_manifest_sha256(self) -> str:
        return self.manifest_sha256

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> "ProfileConfig":
        source = _mapping(value, field_name="profile config")
        _reject_unknown_keys(
            source,
            allowed={
                "selected_profile_ids",
                "manifest_path",
                "profile_manifest_path",
                "manifest_sha256",
                "profile_manifest_sha256",
                "selected_profiles",
                "lease_timeout_seconds",
                "lease_lock_root",
            },
            field_name="profile config",
        )
        path_value = _alias_value(
            source,
            ("manifest_path", "profile_manifest_path"),
            field_name="profile manifest_path",
        )
        hash_value = _alias_value(
            source,
            ("manifest_sha256", "profile_manifest_sha256"),
            field_name="profile manifest_sha256",
        )
        return cls(
            selected_profile_ids=tuple(
                _sequence(
                    source.get("selected_profile_ids"),
                    field_name="selected_profile_ids",
                )
            ),
            manifest_path=path_value,
            manifest_sha256=hash_value,
            selected_profiles=tuple(
                ProfileIdentity.from_mapping(
                    _mapping(row, field_name="selected_profiles[]")
                )
                for row in _sequence(
                    source.get("selected_profiles", ()),
                    field_name="selected_profiles",
                )
            ),
            lease_timeout_seconds=source.get(
                "lease_timeout_seconds",
                300.0,
            ),
            lease_lock_root=source.get("lease_lock_root"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "selected_profile_ids": list(self.selected_profile_ids),
            "manifest_path": str(self.manifest_path),
            "manifest_sha256": self.manifest_sha256,
            "selected_profiles": [
                identity.to_mapping() for identity in self.selected_profiles
            ],
            "lease_timeout_seconds": self.lease_timeout_seconds,
            "lease_lock_root": (
                str(self.lease_lock_root)
                if self.lease_lock_root is not None
                else None
            ),
        }


@dataclass(frozen=True)
class ProviderResponseTimeouts:
    """Website/turn-specific response bounds used by retry reconciliation."""

    chatgpt_turn_1_seconds: float = 180.0
    chatgpt_turn_2_seconds: float = 240.0
    gemini_turn_1_seconds: float = 180.0
    gemini_turn_2_seconds: float = 240.0
    google_ai_overview_seconds: float = 240.0
    gaio_gemini_parser_seconds: float = 240.0

    def __post_init__(self) -> None:
        for field_name in self.__dataclass_fields__:
            object.__setattr__(
                self,
                field_name,
                _positive_float(
                    getattr(self, field_name),
                    field_name=f"retry_policy.response_timeouts.{field_name}",
                ),
            )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> "ProviderResponseTimeouts":
        source = _mapping(
            value,
            field_name="retry_policy response_timeouts",
        )
        allowed = set(cls.__dataclass_fields__)
        _reject_unknown_keys(
            source,
            allowed=allowed,
            field_name="retry_policy response_timeouts",
        )
        return cls(
            **{
                field_name: source.get(
                    field_name,
                    getattr(cls(), field_name),
                )
                for field_name in cls.__dataclass_fields__
            }
        )

    @classmethod
    def from_legacy_browser(
        cls,
        browser: "BrowserConfig",
    ) -> "ProviderResponseTimeouts":
        """Expand the former three generic browser timeouts losslessly."""

        return cls(
            chatgpt_turn_1_seconds=(
                browser.provider_response_timeout_seconds
            ),
            chatgpt_turn_2_seconds=browser.extraction_timeout_seconds,
            gemini_turn_1_seconds=(
                browser.provider_response_timeout_seconds
            ),
            gemini_turn_2_seconds=browser.extraction_timeout_seconds,
            google_ai_overview_seconds=browser.response_timeout_seconds,
            gaio_gemini_parser_seconds=browser.extraction_timeout_seconds,
        )

    def to_mapping(self) -> dict[str, float]:
        return {
            field_name: getattr(self, field_name)
            for field_name in self.__dataclass_fields__
        }


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded authorization for confirmed transport/no-response retries."""

    enabled: bool = True
    max_attempts: int = 2
    late_response_grace_seconds: float = 30.0
    backoff_seconds: float = 5.0
    response_timeouts: ProviderResponseTimeouts = field(
        default_factory=ProviderResponseTimeouts
    )

    def __post_init__(self) -> None:
        enabled = _strict_bool(
            self.enabled,
            field_name="retry_policy.enabled",
        )
        attempts = _positive_int(
            self.max_attempts,
            field_name="retry_policy.max_attempts",
        )
        if attempts > MAX_AUTOMATIC_RETRY_ATTEMPTS:
            raise ConfigurationError(
                "retry_policy.max_attempts cannot exceed "
                f"{MAX_AUTOMATIC_RETRY_ATTEMPTS}"
            )
        if not isinstance(self.response_timeouts, ProviderResponseTimeouts):
            raise ConfigurationError(
                "retry_policy.response_timeouts must be a "
                "ProviderResponseTimeouts"
            )
        object.__setattr__(self, "enabled", enabled)
        object.__setattr__(self, "max_attempts", attempts)
        object.__setattr__(
            self,
            "late_response_grace_seconds",
            _nonnegative_float(
                self.late_response_grace_seconds,
                field_name="retry_policy.late_response_grace_seconds",
            ),
        )
        object.__setattr__(
            self,
            "backoff_seconds",
            _nonnegative_float(
                self.backoff_seconds,
                field_name="retry_policy.backoff_seconds",
            ),
        )

    @property
    def authorized_max_attempts(self) -> int:
        """Return the frozen send-attempt limit after the disable switch."""

        return self.max_attempts if self.enabled else 1

    @classmethod
    def disabled(
        cls,
        *,
        response_timeouts: Optional[ProviderResponseTimeouts] = None,
    ) -> "RetryPolicy":
        return cls(
            enabled=False,
            max_attempts=1,
            response_timeouts=(
                response_timeouts
                if response_timeouts is not None
                else ProviderResponseTimeouts()
            ),
        )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> "RetryPolicy":
        source = _mapping(value, field_name="retry_policy")
        _reject_unknown_keys(
            source,
            allowed={
                "enabled",
                "max_attempts",
                "late_response_grace_seconds",
                "backoff_seconds",
                "response_timeouts",
            },
            field_name="retry_policy",
        )
        timeout_value = source.get("response_timeouts")
        return cls(
            enabled=source.get("enabled", True),
            max_attempts=source.get("max_attempts", 2),
            late_response_grace_seconds=source.get(
                "late_response_grace_seconds",
                30.0,
            ),
            backoff_seconds=source.get("backoff_seconds", 5.0),
            response_timeouts=(
                ProviderResponseTimeouts.from_mapping(timeout_value)
                if timeout_value is not None
                else ProviderResponseTimeouts()
            ),
        )

    @classmethod
    def from_legacy_browser(cls, browser: "BrowserConfig") -> "RetryPolicy":
        return cls(
            response_timeouts=(
                ProviderResponseTimeouts.from_legacy_browser(browser)
            )
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "max_attempts": self.max_attempts,
            "late_response_grace_seconds": (
                self.late_response_grace_seconds
            ),
            "backoff_seconds": self.backoff_seconds,
            "response_timeouts": self.response_timeouts.to_mapping(),
        }


@dataclass(frozen=True)
class BrowserConfig:
    """Explicit shared-Chrome/CDP behavior and interaction pacing."""

    cdp_url: str = "http://127.0.0.1:9222"
    chrome_binary: Optional[Path] = None
    user_data_dir: Optional[Path] = None
    launch_policy: LaunchPolicy = LaunchPolicy.ATTACH_ONLY
    launcher_path: Optional[Path] = None
    connect_timeout_seconds: float = 15.0
    health_timeout_seconds: float = 0.75
    launch_timeout_seconds: float = 40.0
    response_timeout_seconds: float = 240.0
    provider_response_timeout_seconds: float = 180.0
    extraction_timeout_seconds: float = 240.0
    interaction_min_delay_seconds: float = 4.0
    interaction_max_delay_seconds: float = 11.0
    background_only: bool = True
    copilot_verification_policy: str = "deadline_cleanup_v1"
    driver: BrowserDriver = BrowserDriver.PLAYWRIGHT
    driver_version: Optional[str] = None
    expected_browser_version: Optional[str] = None
    driver_experiment: bool = False

    def __post_init__(self) -> None:
        cdp_url = str(self.cdp_url or "").strip().rstrip("/")
        parsed = urlsplit(cdp_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigurationError(
                "browser cdp_url must be an absolute HTTP(S) URL"
            )
        try:
            policy = (
                self.launch_policy
                if isinstance(self.launch_policy, LaunchPolicy)
                else LaunchPolicy(str(self.launch_policy))
            )
        except ValueError as exc:
            raise ConfigurationError(
                "browser launch_policy must be attach_only or "
                "launch_if_required"
            ) from exc
        chrome_binary = _optional_path(self.chrome_binary)
        user_data_dir = _optional_path(self.user_data_dir)
        launcher_path = _optional_path(self.launcher_path)
        background_only = _strict_bool(self.background_only, field_name="browser background_only")
        if (not isinstance(self.copilot_verification_policy, str)
                or self.copilot_verification_policy not in {"deadline_cleanup_v1", "manual_hold_v1", "automatic_hold_v1"}):
            raise ConfigurationError("unsupported browser copilot_verification_policy")
        try:
            driver = BrowserDriver(self.driver)
        except (ValueError, TypeError) as exc:
            raise ConfigurationError("browser driver must be playwright or patchright") from exc
        experiment = _strict_bool(self.driver_experiment, field_name="browser driver_experiment")
        driver_version = _exact_version(self.driver_version, field_name="driver_version", components=3)
        browser_version = _exact_version(self.expected_browser_version, field_name="expected_browser_version", components=4)
        if driver is BrowserDriver.PATCHRIGHT and not experiment:
            raise ConfigurationError("patchright requires browser driver_experiment=true")
        if experiment:
            if driver_version is None or browser_version is None:
                raise ConfigurationError("browser driver experiment requires exact driver_version and expected_browser_version")
            _validate_driver_experiment(cdp_url, policy, user_data_dir)
        if (
            policy is LaunchPolicy.LAUNCH_IF_REQUIRED
            and launcher_path is None
            and (chrome_binary is None or user_data_dir is None)
        ):
            raise ConfigurationError(
                "launch_if_required needs launcher_path or both "
                "chrome_binary and user_data_dir"
            )
        minimum_delay = _nonnegative_float(
            self.interaction_min_delay_seconds,
            field_name="browser interaction_min_delay_seconds",
        )
        maximum_delay = _nonnegative_float(
            self.interaction_max_delay_seconds,
            field_name="browser interaction_max_delay_seconds",
        )
        if maximum_delay < minimum_delay:
            raise ConfigurationError(
                "browser interaction_max_delay_seconds cannot be less than "
                "interaction_min_delay_seconds"
            )
        object.__setattr__(self, "cdp_url", cdp_url)
        object.__setattr__(self, "launch_policy", policy)
        object.__setattr__(self, "chrome_binary", chrome_binary)
        object.__setattr__(self, "user_data_dir", user_data_dir)
        object.__setattr__(self, "launcher_path", launcher_path)
        object.__setattr__(self, "background_only", background_only)
        object.__setattr__(self, "driver", driver)
        object.__setattr__(self, "driver_version", driver_version)
        object.__setattr__(self, "expected_browser_version", browser_version)
        object.__setattr__(self, "driver_experiment", experiment)
        for field_name in (
            "connect_timeout_seconds",
            "health_timeout_seconds",
            "launch_timeout_seconds",
            "response_timeout_seconds",
            "provider_response_timeout_seconds",
            "extraction_timeout_seconds",
        ):
            object.__setattr__(
                self,
                field_name,
                _positive_float(
                    getattr(self, field_name),
                    field_name=f"browser {field_name}",
                ),
            )
        object.__setattr__(
            self,
            "interaction_min_delay_seconds",
            minimum_delay,
        )
        object.__setattr__(
            self,
            "interaction_max_delay_seconds",
            maximum_delay,
        )

    @property
    def chrome_user_data_dir(self) -> Optional[Path]:
        return self.user_data_dir

    @property
    def has_driver_contract(self) -> bool:
        """Whether this runtime declares a versioned driver policy."""
        return bool(self.driver_experiment or self.driver is not BrowserDriver.PLAYWRIGHT
                    or self.driver_version is not None or self.expected_browser_version is not None)

    def __json_value__(self) -> dict[str, object]:
        # Preserve historical dataclass hashes for omitted/default stock fields.
        return {name: getattr(self, name) for name in self.__dataclass_fields__
                if (self.has_driver_contract or name not in _BROWSER_DRIVER_FIELDS)
                and (name != "background_only" or not self.background_only)
                and (name != "copilot_verification_policy" or self.copilot_verification_policy != "deadline_cleanup_v1")}

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> "BrowserConfig":
        source = _mapping(value, field_name="browser config")
        _reject_unknown_keys(
            source,
            allowed={
                *_BROWSER_DRIVER_FIELDS,
                "background_only",
                "copilot_verification_policy",
                "cdp_url",
                "chrome_binary",
                "user_data_dir",
                "chrome_user_data_dir",
                "launch_policy",
                "launcher_path",
                "connect_timeout_seconds",
                "health_timeout_seconds",
                "launch_timeout_seconds",
                "response_timeout_seconds",
                "provider_response_timeout_seconds",
                "extraction_timeout_seconds",
                "interaction_min_delay_seconds",
                "interaction_max_delay_seconds",
            },
            field_name="browser config",
        )
        user_data_dir = _alias_value(
            source,
            ("user_data_dir", "chrome_user_data_dir"),
            field_name="browser user_data_dir",
        )
        return cls(
            background_only=source.get("background_only", True),
            copilot_verification_policy=source.get("copilot_verification_policy", "deadline_cleanup_v1"),
            driver=source.get("driver", BrowserDriver.PLAYWRIGHT),
            driver_version=source.get("driver_version"),
            expected_browser_version=source.get("expected_browser_version"),
            driver_experiment=source.get("driver_experiment", False),
            cdp_url=source.get("cdp_url", "http://127.0.0.1:9222"),
            chrome_binary=source.get("chrome_binary"),
            user_data_dir=user_data_dir,
            launch_policy=source.get(
                "launch_policy",
                LaunchPolicy.ATTACH_ONLY,
            ),
            launcher_path=source.get("launcher_path"),
            connect_timeout_seconds=source.get(
                "connect_timeout_seconds",
                15.0,
            ),
            health_timeout_seconds=source.get(
                "health_timeout_seconds",
                0.75,
            ),
            launch_timeout_seconds=source.get(
                "launch_timeout_seconds",
                40.0,
            ),
            response_timeout_seconds=source.get(
                "response_timeout_seconds",
                240.0,
            ),
            provider_response_timeout_seconds=source.get(
                "provider_response_timeout_seconds",
                180.0,
            ),
            extraction_timeout_seconds=source.get(
                "extraction_timeout_seconds",
                240.0,
            ),
            interaction_min_delay_seconds=source.get(
                "interaction_min_delay_seconds",
                4.0,
            ),
            interaction_max_delay_seconds=source.get(
                "interaction_max_delay_seconds",
                11.0,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            **({"background_only": False} if not self.background_only else {}),
            **({"copilot_verification_policy": self.copilot_verification_policy}
               if self.copilot_verification_policy != "deadline_cleanup_v1" else {}),
            **({
                "driver": self.driver.value,
                "driver_version": self.driver_version,
                "expected_browser_version": self.expected_browser_version,
                "driver_experiment": self.driver_experiment,
            } if self.has_driver_contract else {}),
            "cdp_url": self.cdp_url,
            "chrome_binary": (
                str(self.chrome_binary)
                if self.chrome_binary is not None
                else None
            ),
            "user_data_dir": (
                str(self.user_data_dir)
                if self.user_data_dir is not None
                else None
            ),
            "launch_policy": self.launch_policy.value,
            "launcher_path": (
                str(self.launcher_path)
                if self.launcher_path is not None
                else None
            ),
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "health_timeout_seconds": self.health_timeout_seconds,
            "launch_timeout_seconds": self.launch_timeout_seconds,
            "response_timeout_seconds": self.response_timeout_seconds,
            "provider_response_timeout_seconds": (
                self.provider_response_timeout_seconds
            ),
            "extraction_timeout_seconds": self.extraction_timeout_seconds,
            "interaction_min_delay_seconds": (
                self.interaction_min_delay_seconds
            ),
            "interaction_max_delay_seconds": (
                self.interaction_max_delay_seconds
            ),
        }


_PROFILE_FLAT_KEYS = {
    "selected_profile_ids",
    "manifest_path",
    "profile_manifest_path",
    "manifest_sha256",
    "profile_manifest_sha256",
    "selected_profiles",
    "lease_timeout_seconds",
    "lease_lock_root",
}
_BROWSER_FLAT_KEYS = {
    "background_only",
    "copilot_verification_policy",
    *_BROWSER_DRIVER_FIELDS,
    "cdp_url",
    "chrome_binary",
    "user_data_dir",
    "chrome_user_data_dir",
    "launch_policy",
    "launcher_path",
    "connect_timeout_seconds",
    "health_timeout_seconds",
    "launch_timeout_seconds",
    "response_timeout_seconds",
    "provider_response_timeout_seconds",
    "extraction_timeout_seconds",
    "interaction_min_delay_seconds",
    "interaction_max_delay_seconds",
}


@dataclass(frozen=True)
class RuntimeConfig:
    """Filesystem, exact profile selection, browser, and worker settings."""

    artifact_root: Path
    profiles: ProfileConfig
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    retry_policy: Optional[RetryPolicy] = None
    max_concurrency: int = 4
    # Owned tabs that may run at the same time for one (profile, website)
    # pair, each holding its own chat.  Planning splits every pair's
    # prompt measurements into this many measurement groups; execution gives each measurement group its
    # own tab.  1 keeps the historical one-tab-per-pair shape.
    tabs_per_provider: int = 1
    # Delay before a measurement group's first action, multiplied by its owned-tab slot.
    # Slot 0 starts at once, slot 1 after one delay, and so on, so the tabs of
    # one profile/website pair do not all load their website page together.
    packet_start_stagger_seconds: float = 0.0
    # How many website page openings (a new owned tab plus its first
    # navigation, or the reopening of a saved conversation) may be in flight
    # at the same time across the whole pass.  A packet whose turn has not
    # come waits in Python before its opening deadline starts.  0 means no
    # limit.  This value is not frozen into a run's execution policy: it
    # changes timing only, never what is measured.
    max_concurrent_page_opens: int = 4
    worker_timeout_seconds: float = 900.0
    cleanup_timeout_seconds: float = 20.0
    cancellation_settlement_seconds: float = 0.25
    github_copilot: Optional[GitHubCopilotRuntimeConfig] = None

    def __post_init__(self) -> None:
        if self.github_copilot is not None and not isinstance(self.github_copilot, GitHubCopilotRuntimeConfig):
            raise ConfigurationError("github_copilot must be a GitHubCopilotRuntimeConfig")
        if not isinstance(self.profiles, ProfileConfig):
            raise ConfigurationError("profiles must be a ProfileConfig")
        if not isinstance(self.browser, BrowserConfig):
            raise ConfigurationError("browser must be a BrowserConfig")
        object.__setattr__(
            self,
            "tabs_per_provider",
            _positive_int(
                self.tabs_per_provider,
                field_name="tabs_per_provider",
            ),
        )
        object.__setattr__(
            self,
            "max_concurrent_page_opens",
            _nonnegative_int(
                self.max_concurrent_page_opens,
                field_name="max_concurrent_page_opens",
            ),
        )
        retry_policy = self.retry_policy
        if retry_policy is None:
            retry_policy = RetryPolicy.from_legacy_browser(self.browser)
        if not isinstance(retry_policy, RetryPolicy):
            raise ConfigurationError(
                "retry_policy must be a RetryPolicy"
            )
        object.__setattr__(self, "retry_policy", retry_policy)
        object.__setattr__(
            self,
            "artifact_root",
            _path(self.artifact_root, field_name="artifact_root"),
        )
        object.__setattr__(
            self,
            "max_concurrency",
            _positive_int(
                self.max_concurrency,
                field_name="max_concurrency",
            ),
        )
        object.__setattr__(
            self,
            "packet_start_stagger_seconds",
            _nonnegative_float(
                self.packet_start_stagger_seconds,
                field_name="packet_start_stagger_seconds",
            ),
        )
        object.__setattr__(
            self,
            "worker_timeout_seconds",
            _positive_float(
                self.worker_timeout_seconds,
                field_name="worker_timeout_seconds",
            ),
        )
        object.__setattr__(
            self,
            "cancellation_settlement_seconds",
            _positive_float(
                self.cancellation_settlement_seconds,
                field_name="cancellation_settlement_seconds",
            ),
        )
        object.__setattr__(
            self,
            "cleanup_timeout_seconds",
            _positive_float(
                self.cleanup_timeout_seconds,
                field_name="cleanup_timeout_seconds",
            ),
        )

    @property
    def selected_profile_ids(self) -> tuple[str, ...]:
        return self.profiles.selected_profile_ids

    @property
    def selected_profiles(self) -> tuple[ProfileIdentity, ...]:
        return self.profiles.selected_profiles

    @property
    def profile_directories(self) -> Mapping[str, str]:
        return self.profiles.profile_directories

    @property
    def selected_chrome_directories(self) -> tuple[str, ...]:
        return self.profiles.selected_chrome_directories

    @property
    def profile_manifest_path(self) -> Path:
        return self.profiles.manifest_path

    @property
    def profile_manifest_sha256(self) -> str:
        return self.profiles.manifest_sha256

    @property
    def cdp_url(self) -> str:
        return self.browser.cdp_url

    @property
    def chrome_binary(self) -> Optional[Path]:
        return self.browser.chrome_binary

    @property
    def chrome_user_data_dir(self) -> Optional[Path]:
        return self.browser.user_data_dir

    @property
    def launch_policy(self) -> LaunchPolicy:
        return self.browser.launch_policy

    @property
    def connect_timeout_seconds(self) -> float:
        return self.browser.connect_timeout_seconds

    @property
    def response_timeout_seconds(self) -> float:
        return self.browser.response_timeout_seconds

    @property
    def provider_response_timeout_seconds(self) -> float:
        return self.browser.provider_response_timeout_seconds

    @property
    def extraction_timeout_seconds(self) -> float:
        return self.browser.extraction_timeout_seconds

    @property
    def interaction_min_delay_seconds(self) -> float:
        return self.browser.interaction_min_delay_seconds

    @property
    def interaction_max_delay_seconds(self) -> float:
        return self.browser.interaction_max_delay_seconds

    @property
    def provider_response_timeouts(self) -> ProviderResponseTimeouts:
        assert self.retry_policy is not None
        return self.retry_policy.response_timeouts

    @property
    def concurrency(self) -> int:
        return self.max_concurrency

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> "RuntimeConfig":
        source = _mapping(value, field_name="runtime config")
        _reject_unknown_keys(
            source,
            allowed={
                "artifact_root",
                "github_copilot",
                "profiles",
                "browser",
                "retry_policy",
                "max_concurrency",
                "tabs_per_provider",
                "packet_start_stagger_seconds",
                "max_concurrent_page_opens",
                "worker_timeout_seconds",
                "cancellation_settlement_seconds",
                "cleanup_timeout_seconds",
                *_PROFILE_FLAT_KEYS,
                *_BROWSER_FLAT_KEYS,
            },
            field_name="runtime config",
        )
        nested_profiles = dict(
            _optional_mapping(
                source.get("profiles"),
                field_name="runtime profiles",
            )
        )
        for key in _PROFILE_FLAT_KEYS:
            if key not in source:
                continue
            if key in nested_profiles and nested_profiles[key] != source[key]:
                raise ConfigurationError(
                    f"runtime profiles.{key} conflicts with runtime {key}"
                )
            nested_profiles[key] = source[key]
        nested_browser = dict(
            _optional_mapping(
                source.get("browser"),
                field_name="runtime browser",
            )
        )
        for key in _BROWSER_FLAT_KEYS:
            if key not in source:
                continue
            if key in nested_browser and nested_browser[key] != source[key]:
                raise ConfigurationError(
                    f"runtime browser.{key} conflicts with runtime {key}"
                )
            nested_browser[key] = source[key]
        return cls(
            github_copilot=(GitHubCopilotRuntimeConfig.from_mapping(source["github_copilot"]) if source.get("github_copilot") is not None else None),
            artifact_root=source.get("artifact_root"),
            profiles=ProfileConfig.from_mapping(nested_profiles),
            browser=BrowserConfig.from_mapping(nested_browser),
            retry_policy=(
                RetryPolicy.from_mapping(source["retry_policy"])
                if source.get("retry_policy") is not None
                else None
            ),
            max_concurrency=source.get("max_concurrency", 4),
            tabs_per_provider=source.get("tabs_per_provider", 1),
            packet_start_stagger_seconds=source.get(
                "packet_start_stagger_seconds",
                0.0,
            ),
            max_concurrent_page_opens=source.get(
                "max_concurrent_page_opens",
                4,
            ),
            worker_timeout_seconds=source.get(
                "worker_timeout_seconds",
                900.0,
            ),
            cancellation_settlement_seconds=source.get(
                "cancellation_settlement_seconds",
                0.25,
            ),
            cleanup_timeout_seconds=source.get(
                "cleanup_timeout_seconds",
                20.0,
            ),
        )

    def __json_value__(self) -> dict[str, object]:
        # Match the historical dataclass projection exactly when the lane is absent.
        return {name: getattr(self, name) for name in self.__dataclass_fields__ if name != "github_copilot" or self.github_copilot is not None}

    def to_mapping(self) -> dict[str, object]:
        return {
            **({"github_copilot": self.github_copilot.to_mapping()} if self.github_copilot is not None else {}),
            "artifact_root": str(self.artifact_root),
            "profiles": self.profiles.to_mapping(),
            "browser": self.browser.to_mapping(),
            "retry_policy": self.retry_policy.to_mapping(),
            "max_concurrency": self.max_concurrency,
            "tabs_per_provider": self.tabs_per_provider,
            "packet_start_stagger_seconds": (
                self.packet_start_stagger_seconds
            ),
            "max_concurrent_page_opens": self.max_concurrent_page_opens,
            "worker_timeout_seconds": self.worker_timeout_seconds,
            "cancellation_settlement_seconds": (
                self.cancellation_settlement_seconds
            ),
            "cleanup_timeout_seconds": self.cleanup_timeout_seconds,
        }

    @classmethod
    def from_environment(
        cls,
        environment: Optional[Mapping[str, object]] = None,
        *,
        explicit: Optional["RuntimeConfig" | Mapping[str, object]] = None,
    ) -> "RuntimeConfig":
        """Build from legacy environment only when explicitly requested.

        If ``explicit`` is supplied, it wins only when every inherited legacy
        value agrees.  Conflicts are errors rather than hidden precedence.
        Neither the supplied mapping nor :data:`os.environ` is mutated.
        """

        values = (
            dict(os.environ)
            if environment is None
            else {str(key): value for key, value in environment.items()}
        )
        if explicit is not None:
            typed = (
                explicit
                if isinstance(explicit, RuntimeConfig)
                else cls.from_mapping(explicit)
            )
            cls._reject_environment_conflicts(typed, values)
            return typed

        supplied_driver_keys = [key for key in _BROWSER_DRIVER_ENVIRONMENT if key in values]
        if supplied_driver_keys:
            raise ConfigurationError(
                "browser driver selection requires explicit runtime configuration; "
                "environment-only driver settings are unsupported: " + ", ".join(supplied_driver_keys)
            )
        artifact_root = values.get("VISIBILITY_RUNNER_ARTIFACT_ROOT")
        manifest_path = _environment_alias_value(
            values,
            (
                "VISIBILITY_RUNNER_PROFILE_MANIFEST_PATH",
                "LOGGEDIN_PROFILES_PATH",
            ),
            field_name="profile manifest path",
            parser=lambda value: _path(
                value,
                field_name="profile manifest_path",
            ),
        )
        if not artifact_root:
            raise ConfigurationError(
                "VISIBILITY_RUNNER_ARTIFACT_ROOT is required by the legacy "
                "environment adapter"
            )
        if not manifest_path:
            raise ConfigurationError(
                "VISIBILITY_RUNNER_PROFILE_MANIFEST_PATH or "
                "LOGGEDIN_PROFILES_PATH is required"
            )
        resolved_manifest = _path(
            manifest_path,
            field_name="profile manifest_path",
        )
        selected_raw = _environment_alias_value(
            values,
            (
                "VISIBILITY_RUNNER_PROFILE_IDS",
                "SNAPSHOT_OWNED_PROFILE_IDS",
            ),
            field_name="selected profile IDs",
            parser=_parse_profile_ids,
        )
        manifest_profiles = _profiles_from_manifest(resolved_manifest)
        selected_ids = (
            _parse_profile_ids(selected_raw)
            if selected_raw
            else tuple(identity.id for identity in manifest_profiles)
        )
        manifest_by_id = {
            identity.id: identity for identity in manifest_profiles
        }
        missing_ids = [
            identifier
            for identifier in selected_ids
            if identifier not in manifest_by_id
        ]
        if missing_ids:
            raise ConfigurationError(
                "selected profile IDs are absent from the pinned manifest: "
                + ", ".join(missing_ids)
            )
        selected_profiles = tuple(
            manifest_by_id[identifier] for identifier in selected_ids
        )
        digest = str(
            values.get("VISIBILITY_RUNNER_PROFILE_MANIFEST_SHA256")
            or ""
        ).strip()
        if not digest:
            try:
                digest = _canonical_profile_manifest_sha256(
                    _read_profile_manifest_payload(resolved_manifest)
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise ConfigurationError(
                    f"cannot hash profile manifest {resolved_manifest}: {exc}"
                ) from exc
        profile_mapping: dict[str, object] = {
            "selected_profile_ids": list(selected_ids),
            "manifest_path": str(resolved_manifest),
            "manifest_sha256": digest,
            "selected_profiles": [
                identity.to_mapping() for identity in selected_profiles
            ],
        }
        if values.get("SNAPSHOT_PROFILE_LEASE_TIMEOUT_SECONDS"):
            profile_mapping["lease_timeout_seconds"] = values[
                "SNAPSHOT_PROFILE_LEASE_TIMEOUT_SECONDS"
            ]
        if values.get("SNAPSHOT_PROFILE_LEASE_LOCK_ROOT"):
            profile_mapping["lease_lock_root"] = values[
                "SNAPSHOT_PROFILE_LEASE_LOCK_ROOT"
            ]
        browser_mapping = _browser_mapping_from_environment(values)
        runtime_mapping: dict[str, object] = {
            "artifact_root": artifact_root,
            "profiles": profile_mapping,
            "browser": browser_mapping,
        }
        if values.get("VISIBILITY_RUNNER_MAX_CONCURRENCY"):
            try:
                runtime_mapping["max_concurrency"] = int(
                    str(values["VISIBILITY_RUNNER_MAX_CONCURRENCY"])
                )
            except ValueError as exc:
                raise ConfigurationError(
                    "VISIBILITY_RUNNER_MAX_CONCURRENCY must be an integer"
                ) from exc
        if values.get("VISIBILITY_RUNNER_TABS_PER_PROVIDER"):
            try:
                runtime_mapping["tabs_per_provider"] = int(
                    str(values["VISIBILITY_RUNNER_TABS_PER_PROVIDER"])
                )
            except ValueError as exc:
                raise ConfigurationError(
                    "VISIBILITY_RUNNER_TABS_PER_PROVIDER must be an integer"
                ) from exc
        if values.get("VISIBILITY_RUNNER_PACKET_START_STAGGER_SECONDS"):
            runtime_mapping["packet_start_stagger_seconds"] = values[
                "VISIBILITY_RUNNER_PACKET_START_STAGGER_SECONDS"
            ]
        if values.get("VISIBILITY_RUNNER_WORKER_TIMEOUT_SECONDS"):
            runtime_mapping["worker_timeout_seconds"] = values[
                "VISIBILITY_RUNNER_WORKER_TIMEOUT_SECONDS"
            ]
        if values.get("VISIBILITY_RUNNER_CLEANUP_TIMEOUT_SECONDS"):
            runtime_mapping["cleanup_timeout_seconds"] = values[
                "VISIBILITY_RUNNER_CLEANUP_TIMEOUT_SECONDS"
            ]
        return cls.from_mapping(runtime_mapping)

    @staticmethod
    def _reject_environment_conflicts(
        explicit: "RuntimeConfig",
        environment: Mapping[str, object],
    ) -> None:
        checks: tuple[tuple[tuple[str, ...], object, object], ...] = (
            (
                ("VISIBILITY_RUNNER_BROWSER_DRIVER",),
                explicit.browser.driver,
                lambda value: BrowserDriver(value),
            ),
            (
                ("VISIBILITY_RUNNER_BROWSER_DRIVER_VERSION",),
                explicit.browser.driver_version,
                lambda value: _exact_version(value, field_name="driver_version", components=3),
            ),
            (
                ("VISIBILITY_RUNNER_EXPECTED_BROWSER_VERSION",),
                explicit.browser.expected_browser_version,
                lambda value: _exact_version(value, field_name="expected_browser_version", components=4),
            ),
            (
                ("VISIBILITY_RUNNER_BROWSER_DRIVER_EXPERIMENT",),
                explicit.browser.driver_experiment,
                _parse_environment_bool,
            ),
            (
                ("VISIBILITY_RUNNER_ARTIFACT_ROOT",),
                explicit.artifact_root,
                lambda value: _path(value, field_name="artifact_root"),
            ),
            (
                (
                    "VISIBILITY_RUNNER_PROFILE_MANIFEST_PATH",
                    "LOGGEDIN_PROFILES_PATH",
                ),
                explicit.profile_manifest_path,
                lambda value: _path(
                    value,
                    field_name="profile manifest_path",
                ),
            ),
            (
                ("VISIBILITY_RUNNER_PROFILE_MANIFEST_SHA256",),
                explicit.profile_manifest_sha256,
                lambda value: str(value).strip().casefold(),
            ),
            (
                (
                    "VISIBILITY_RUNNER_PROFILE_IDS",
                    "SNAPSHOT_OWNED_PROFILE_IDS",
                ),
                explicit.selected_profile_ids,
                _parse_profile_ids,
            ),
            (
                ("CDP_URL",),
                explicit.browser.cdp_url,
                lambda value: str(value).strip().rstrip("/"),
            ),
            (
                (
                    "VISIBILITY_RUNNER_CHROME_BINARY",
                    "CHROME_BINARY",
                    "CHROME_PATH",
                ),
                explicit.browser.chrome_binary,
                _optional_path,
            ),
            (
                ("CHROME_USER_DATA_DIR",),
                explicit.browser.user_data_dir,
                _optional_path,
            ),
            (
                (
                    "VISIBILITY_RUNNER_LAUNCH_POLICY",
                    "CHROME_LAUNCH_POLICY",
                ),
                explicit.browser.launch_policy,
                lambda value: LaunchPolicy(str(value)),
            ),
            (
                ("CHROME_LAUNCHER",),
                explicit.browser.launcher_path,
                _optional_path,
            ),
            (
                ("VISIBILITY_RUNNER_CDP_CONNECT_TIMEOUT_SECONDS",),
                explicit.browser.connect_timeout_seconds,
                lambda value: float(str(value)),
            ),
            (
                ("VISIBILITY_RUNNER_CDP_HEALTH_TIMEOUT_SECONDS",),
                explicit.browser.health_timeout_seconds,
                lambda value: float(str(value)),
            ),
            (
                ("VISIBILITY_RUNNER_CHROME_LAUNCH_TIMEOUT_SECONDS",),
                explicit.browser.launch_timeout_seconds,
                lambda value: float(str(value)),
            ),
            (
                ("RESPONSE_TIMEOUT_SECONDS",),
                explicit.browser.response_timeout_seconds,
                lambda value: float(str(value)),
            ),
            (
                ("AI_PROVIDER_RESPONSE_TIMEOUT",),
                explicit.browser.provider_response_timeout_seconds,
                lambda value: float(str(value)),
            ),
            (
                ("EXTRACTION_RESPONSE_TIMEOUT_SECONDS",),
                explicit.browser.extraction_timeout_seconds,
                lambda value: float(str(value)),
            ),
            (
                ("MIN_DELAY_SECONDS",),
                explicit.browser.interaction_min_delay_seconds,
                lambda value: float(str(value)),
            ),
            (
                ("MAX_DELAY_SECONDS",),
                explicit.browser.interaction_max_delay_seconds,
                lambda value: float(str(value)),
            ),
            (
                ("SNAPSHOT_PROFILE_LEASE_TIMEOUT_SECONDS",),
                explicit.profiles.lease_timeout_seconds,
                lambda value: float(str(value)),
            ),
            (
                ("SNAPSHOT_PROFILE_LEASE_LOCK_ROOT",),
                explicit.profiles.lease_lock_root,
                _optional_path,
            ),
            (
                ("VISIBILITY_RUNNER_MAX_CONCURRENCY",),
                explicit.max_concurrency,
                lambda value: int(str(value)),
            ),
            (
                ("VISIBILITY_RUNNER_TABS_PER_PROVIDER",),
                explicit.tabs_per_provider,
                lambda value: int(str(value)),
            ),
            (
                ("VISIBILITY_RUNNER_PACKET_START_STAGGER_SECONDS",),
                explicit.packet_start_stagger_seconds,
                lambda value: float(str(value)),
            ),
            (
                ("VISIBILITY_RUNNER_WORKER_TIMEOUT_SECONDS",),
                explicit.worker_timeout_seconds,
                lambda value: float(str(value)),
            ),
            (
                ("VISIBILITY_RUNNER_CLEANUP_TIMEOUT_SECONDS",),
                explicit.cleanup_timeout_seconds,
                lambda value: float(str(value)),
            ),
        )
        for names, expected, parser in checks:
            for name in names:
                if name not in environment or (
                    name not in _BROWSER_DRIVER_ENVIRONMENT
                    and not str(environment.get(name) or "").strip()
                ):
                    continue
                try:
                    inherited = parser(environment[name])
                except (TypeError, ValueError, ConfigurationError) as exc:
                    raise ConfigurationConflictError(
                        f"inherited {name} is invalid: {exc}"
                    ) from exc
                if inherited != expected:
                    raise ConfigurationConflictError(
                        f"explicit runtime configuration conflicts with "
                        f"inherited {name}"
                    )
        raw_count = str(environment.get("NUM_PROFILES") or "").strip()
        if raw_count:
            try:
                count = int(raw_count)
            except ValueError as exc:
                raise ConfigurationConflictError(
                    "inherited NUM_PROFILES is invalid"
                ) from exc
            if count != len(explicit.selected_profile_ids):
                raise ConfigurationConflictError(
                    "explicit selected_profile_ids conflict with inherited "
                    "NUM_PROFILES; exact IDs are authoritative"
                )


def _parse_environment_bool(value: object) -> bool:
    if value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    raise ConfigurationError("browser driver_experiment must be true or false")


def _parse_profile_ids(value: object) -> tuple[str, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        raw = list(value)
    else:
        text = str(value or "").strip()
        if not text:
            return ()
        if text.startswith("["):
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ConfigurationError(
                    "profile IDs environment value is malformed JSON"
                ) from exc
            if not isinstance(raw, list):
                raise ConfigurationError(
                    "profile IDs environment value must be a JSON array"
                )
        else:
            raw = [item.strip() for item in text.split(",")]
    return tuple(str(item or "").strip() for item in raw if str(item or "").strip())


def _environment_alias_value(
    values: Mapping[str, object],
    names: tuple[str, ...],
    *,
    field_name: str,
    parser: Callable[[object], object] = lambda value: str(value).strip(),
) -> object:
    supplied = [
        (name, values[name])
        for name in names
        if str(values.get(name) or "").strip()
    ]
    if not supplied:
        return None
    parsed: list[tuple[str, object, object]] = []
    for name, raw in supplied:
        try:
            normalized = parser(raw)
        except (TypeError, ValueError, ConfigurationError) as exc:
            raise ConfigurationError(
                f"inherited {name} is invalid for {field_name}: {exc}"
            ) from exc
        parsed.append((name, raw, normalized))
    first_name, first_raw, first_normalized = parsed[0]
    for name, _raw, normalized in parsed[1:]:
        if normalized != first_normalized:
            raise ConfigurationConflictError(
                f"inherited {first_name} conflicts with {name} for "
                f"{field_name}"
            )
    return first_raw


def _read_profile_manifest_payload(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(
            f"profile manifest is missing: {path}"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            f"profile manifest is invalid: {path}: {exc}"
        ) from exc


def _canonical_profile_manifest_sha256(payload: object) -> str:
    covered = payload
    if isinstance(payload, Mapping):
        covered = {
            str(key): value
            for key, value in payload.items()
            if str(key) not in {"canonical_sha256", "manifest_sha256"}
        }
    return hashlib.sha256(
        json.dumps(
            covered,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _profiles_from_manifest(path: Path) -> tuple[ProfileIdentity, ...]:
    payload = _read_profile_manifest_payload(path)
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, Mapping) and isinstance(
        payload.get("profiles"),
        list,
    ):
        rows = payload["profiles"]
    else:
        raise ConfigurationError(
            "profile manifest must contain a JSON list or an object with a "
            "profiles list"
        )
    identities: tuple[ProfileIdentity, ...] = tuple(
        ProfileIdentity.from_mapping(
            _mapping(row, field_name="profile manifest row")
        )
        for row in rows
    )
    ids = tuple(identity.id for identity in identities)
    if len(ids) != len(set(ids)):
        raise ConfigurationError("profile manifest contains duplicate IDs")
    if not ids:
        raise ConfigurationError("profile manifest contains no profiles")
    directories = tuple(identity.chrome_directory for identity in identities)
    if len(directories) != len(set(directories)):
        raise ConfigurationError(
            "profile manifest contains duplicate raw Chrome directories"
        )
    return identities


def _profile_ids_from_manifest(path: Path) -> tuple[str, ...]:
    """Compatibility helper returning exact IDs from a validated manifest."""

    return tuple(identity.id for identity in _profiles_from_manifest(path))


def _browser_mapping_from_environment(
    values: Mapping[str, object],
) -> dict[str, object]:
    result: dict[str, object] = {}
    aliases: tuple[
        tuple[str, tuple[str, ...], Callable[[object], object]],
        ...,
    ] = (
        (
            "cdp_url",
            ("CDP_URL",),
            lambda value: str(value).strip().rstrip("/"),
        ),
        (
            "chrome_binary",
            (
                "VISIBILITY_RUNNER_CHROME_BINARY",
                "CHROME_BINARY",
                "CHROME_PATH",
            ),
            _optional_path,
        ),
        ("user_data_dir", ("CHROME_USER_DATA_DIR",), _optional_path),
        (
            "launch_policy",
            (
                "VISIBILITY_RUNNER_LAUNCH_POLICY",
                "CHROME_LAUNCH_POLICY",
            ),
            lambda value: LaunchPolicy(str(value)),
        ),
        ("launcher_path", ("CHROME_LAUNCHER",), _optional_path),
        (
            "connect_timeout_seconds",
            ("VISIBILITY_RUNNER_CDP_CONNECT_TIMEOUT_SECONDS",),
            lambda value: float(str(value)),
        ),
        (
            "health_timeout_seconds",
            ("VISIBILITY_RUNNER_CDP_HEALTH_TIMEOUT_SECONDS",),
            lambda value: float(str(value)),
        ),
        (
            "launch_timeout_seconds",
            ("VISIBILITY_RUNNER_CHROME_LAUNCH_TIMEOUT_SECONDS",),
            lambda value: float(str(value)),
        ),
        (
            "response_timeout_seconds",
            ("RESPONSE_TIMEOUT_SECONDS",),
            lambda value: float(str(value)),
        ),
        (
            "provider_response_timeout_seconds",
            ("AI_PROVIDER_RESPONSE_TIMEOUT",),
            lambda value: float(str(value)),
        ),
        (
            "extraction_timeout_seconds",
            ("EXTRACTION_RESPONSE_TIMEOUT_SECONDS",),
            lambda value: float(str(value)),
        ),
        (
            "interaction_min_delay_seconds",
            ("MIN_DELAY_SECONDS",),
            lambda value: float(str(value)),
        ),
        (
            "interaction_max_delay_seconds",
            ("MAX_DELAY_SECONDS",),
            lambda value: float(str(value)),
        ),
    )
    for target, names, parser in aliases:
        value = _environment_alias_value(
            values,
            names,
            field_name=f"browser {target}",
            parser=parser,
        )
        if value is not None:
            result[target] = value
    if (
        "launch_policy" not in result
        and (
            result.get("launcher_path")
            or (
                result.get("chrome_binary")
                and result.get("user_data_dir")
            )
        )
    ):
        result["launch_policy"] = LaunchPolicy.LAUNCH_IF_REQUIRED.value
    return result


# Snapshot-era compatibility name retained at the application adapter seam.
SnapshotRunConfig = MeasurementConfig
RunConfig = MeasurementConfig


__all__ = [
    "Area",
    "BrowserConfig",
    "BrowserDriver",
    "Clinic",
    "ClinicArchetype",
    "DEFAULT_MODULE_PLATFORMS",
    "DEFAULT_PLATFORMS",
    "DIAGNOSTIC_POLICY_VERSION",
    "DIAGNOSTIC_PROMPT_CATALOG_VERSION",
    "Geography",
    "LaunchPolicy",
    "MAX_AUTOMATIC_RETRY_ATTEMPTS",
    "MAX_MEASUREMENT_REPETITIONS",
    "MAX_MODULE_MEASUREMENT_RUNS",
    "MODULE_MEASUREMENT_EXECUTION_POLICY_VERSION",
    "MODULE_MEASUREMENT_PLATFORMS",
    "MODULE_MEASUREMENT_PUBLICATION_BLOCKER",
    "MODULE_MEASUREMENT_PUBLICATION_ELIGIBLE",
    "MeasurementConfig",
    "NONPUBLISHABLE_POLICY_VERSION",
    "PROMPT_CATALOG_VERSION",
    "Platform",
    "ProfileConfig",
    "ProfileIdentity",
    "ProviderResponseTimeouts",
    "RetryPolicy",
    "RunConfig",
    "RuntimeConfig",
    "SUPPORTED_GEOGRAPHY_COUNTRY_CODES",
    "SnapshotRunConfig",
    "Treatment",
    "normalize_platform",
]
