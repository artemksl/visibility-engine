"""Pure geography and treatment normalization used by prompt planning."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Final

from .catalog import (
    PROMPT_GEOGRAPHY_COUNTRY_CODES,
    PROMPT_GEOGRAPHY_HASH,
    PROMPT_GEOGRAPHY_V1,
    PROMPT_GEOGRAPHY_VERSION,
)


_SPACE_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")
GEOGRAPHY_TIERS: Final[tuple[str, ...]] = (
    "local",
    "greater_area",
    "city",
)


class GeographyContractError(ValueError):
    """A target is outside the installed patient-facing taxonomy."""


def treatment_key(value: object) -> str:
    """Return the stable patient-language treatment comparison key."""

    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    return _SPACE_RE.sub(" ", _NON_WORD_RE.sub(" ", text)).strip()


def taxonomy_targets(tier: str) -> tuple[Mapping[str, object], ...]:
    """Return immutable installed targets for a controlled wider tier."""

    key = str(tier or "").strip()
    if key == "greater_area":
        resource_key = "greater_areas"
    elif key == "city":
        resource_key = "cities"
    else:
        raise GeographyContractError(
            "taxonomy tier must be 'greater_area' or 'city'"
        )
    return tuple(PROMPT_GEOGRAPHY_V1[resource_key])


def validate_taxonomy_target(
    target: Mapping[str, object],
    *,
    tier: str,
) -> None:
    """Require the exact id/name/country binding installed with the package."""

    if not isinstance(target, Mapping):
        raise GeographyContractError(f"{tier} target must be a mapping")
    identifier = str(target.get("id") or target.get("area_id") or "").strip()
    name = str(target.get("name") or target.get("area") or "").strip()
    country_code = str(target.get("country_code") or "").strip().upper()
    allowed = {
        (
            str(row.get("id") or "").strip(),
            str(row.get("name") or "").strip(),
            str(row.get("country_code") or country_code).strip().upper(),
        )
        for row in taxonomy_targets(tier)
    }
    if (identifier, name, country_code) not in allowed:
        raise GeographyContractError(
            f"{tier} must use an exact installed patient-facing geography"
        )
    if country_code not in PROMPT_GEOGRAPHY_COUNTRY_CODES:
        raise GeographyContractError(
            f"{tier}.country_code is outside the installed taxonomy"
        )


__all__ = [
    "GEOGRAPHY_TIERS",
    "GeographyContractError",
    "PROMPT_GEOGRAPHY_COUNTRY_CODES",
    "PROMPT_GEOGRAPHY_HASH",
    "PROMPT_GEOGRAPHY_VERSION",
    "taxonomy_targets",
    "treatment_key",
    "validate_taxonomy_target",
]
