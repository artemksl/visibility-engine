"""Immutable module-v4 prompt and geography contracts.

The catalogs are installed package resources.  Loading them performs only
local, read-only validation; it never consults a repository checkout,
environment variable, browser, or website.
"""

from __future__ import annotations

import json
from importlib.resources import files
from types import MappingProxyType
from typing import Any, Final, Mapping

from .hashing import canonical_sha256


SCOPE_V4_VERSION: Final[str] = "4.0"
SCOPE_VERSION: Final[str] = SCOPE_V4_VERSION
SCOPE_V4_SCHEMA_VERSION: Final[str] = "free_snapshot.scope.v4"
SCOPE_SCHEMA_VERSION: Final[str] = SCOPE_V4_SCHEMA_VERSION
PROMPT_PACK_V4_VERSION: Final[str] = "snapshot-prompts-v4"
PROMPT_PACK_VERSION: Final[str] = PROMPT_PACK_V4_VERSION
PROMPT_PACK_V4_SCHEMA_VERSION: Final[str] = "free_snapshot.prompts.v4"
PROMPT_PACK_SCHEMA_VERSION: Final[str] = PROMPT_PACK_V4_SCHEMA_VERSION
OBSERVATION_PLAN_V4_SCHEMA_VERSION: Final[str] = (
    "free_snapshot.observation_plan.v4"
)
OBSERVATION_PLAN_SCHEMA_VERSION: Final[str] = (
    OBSERVATION_PLAN_V4_SCHEMA_VERSION
)

MODULE_MEASUREMENT_EXECUTION_POLICY_VERSION: Final[str] = (
    "snapshot-module-measurement-v1"
)
MODULE_MEASUREMENT_PLATFORMS: Final[tuple[str, ...]] = (
    "chatgpt",
    "gemini",
    "copilot",
    "google_ai_overview",
    "github_copilot",
)
DEFAULT_PLATFORMS: Final[tuple[str, ...]] = ("chatgpt", "gemini", "copilot")
DEFAULT_PLANNED_RUNS: Final[int] = 3
TEST_MODE_PLANNED_RUNS: Final[int] = 1
MAX_MODULE_MEASUREMENT_RUNS: Final[int] = 20
MIN_SELECTED_AREAS: Final[int] = 1
MAX_SELECTED_AREAS: Final[int] = 2
BROWSER_MEASUREMENT_MODE: Final[str] = "browser_measured"
COPILOT_DERIVED_MEASUREMENT_MODE: Final[str] = "derived_floor_average_v1"

# These values remain in the compatibility scope manifest because removing
# them would change existing module-v4 hashes.  The standalone runner does not
# interpret report, copy, reconciliation, or publication policy.
EVIDENCE_POLICY_V4_VERSION: Final[str] = "snapshot-evidence-v4"
EVIDENCE_POLICY_ARTIFACT_HASH_V4: Final[str] = (
    "89b9e3ceb5b0b162735989b72afef396bd61bd100752bcd199ddb07e349f1c93"
)
COPY_POLICY_VERSION: Final[str] = "snapshot-copy-v1"
NAME_RECONCILIATION_POLICY_VERSION: Final[str] = (
    "snapshot-name-reconciliation-v1"
)
NAME_RECONCILIATION_ARTIFACT_HASH: Final[str] = (
    "8e0cf91aa62cce48f0550f099ca571949e33b1d1e5a25827772f9ee1a08a3cae"
)

EXPECTED_PROMPT_CATALOG_VERSION: Final[str] = (
    "snapshot-prompts-v4-content-2026-07-17"
)
EXPECTED_PROMPT_CATALOG_CONTENT_HASH: Final[str] = (
    "caf84efc1f217f8eefae2fd28b6cb91d27f860a24b089847bdd27978159190ab"
)
EXPECTED_PROMPT_GEOGRAPHY_VERSION: Final[str] = (
    "prompt-geography-london-2026-07-v1"
)
EXPECTED_PROMPT_GEOGRAPHY_HASH: Final[str] = (
    "ebe6b80c04fa1f4765f2418c7616f8183a45012c202a9310763f44124441c1c5"
)
EXPECTED_PROMPT_TEMPLATE_ARTIFACT_HASH: Final[str] = (
    "725c56dbd095185b8853353b0b419fa732d02e0b0dfb29b8513838c6bd030cf1"
)


class CatalogIntegrityError(RuntimeError):
    """An installed immutable planning resource failed validation."""


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _read_json_resource(filename: str) -> dict[str, Any]:
    resource = files("visibility_runner.resources.prompts").joinpath(filename)
    try:
        with resource.open("r", encoding="utf-8", newline="") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogIntegrityError(
            f"cannot load installed planning resource {filename}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise CatalogIntegrityError(
            f"installed planning resource {filename} must contain an object"
        )
    return value


def _read_prompt_catalog() -> Mapping[str, Any]:
    raw = _read_json_resource("prompt_catalog.json")
    if raw.get("schema_version") != "free_snapshot.prompt_content_spec.v1":
        raise CatalogIntegrityError("unsupported v4 prompt catalog schema")
    if raw.get("runtime_enabled") is not False:
        raise CatalogIntegrityError(
            "the fixed prompt catalog must remain runtime_enabled=false"
        )
    content = {
        key: raw[key]
        for key in ("platforms", "rendering_policy", "baskets", "slots")
    }
    content_hash = canonical_sha256(content)
    if (
        raw.get("catalog_version") != EXPECTED_PROMPT_CATALOG_VERSION
        or raw.get("content_sha256") != EXPECTED_PROMPT_CATALOG_CONTENT_HASH
        or content_hash != EXPECTED_PROMPT_CATALOG_CONTENT_HASH
    ):
        raise CatalogIntegrityError(
            "the v4 prompt catalog content hash or version changed"
        )
    slots = raw.get("slots")
    platforms = raw.get("platforms")
    if not isinstance(slots, list) or len(slots) != 21:
        raise CatalogIntegrityError(
            "the v4 prompt catalog must contain exactly 21 slots"
        )
    if platforms != [
        "chatgpt",
        "gemini",
        "copilot",
        "google_ai_overview",
    ]:
        raise CatalogIntegrityError(
            "the v4 prompt catalog platform order changed"
        )
    basket_counts = {
        basket_id: sum(
            isinstance(row, Mapping)
            and row.get("basket_id") == basket_id
            for row in slots
        )
        for basket_id in (
            "treatment_local",
            "treatment_expanded",
            "clinic_archetype",
        )
    }
    if set(basket_counts.values()) != {7}:
        raise CatalogIntegrityError(
            "the v4 prompt catalog requires seven slots per basket"
        )
    for slot in slots:
        renderings = (
            slot.get("renderings") if isinstance(slot, Mapping) else None
        )
        if not isinstance(renderings, Mapping) or set(renderings) != set(
            platforms
        ):
            raise CatalogIntegrityError(
                f"v4 slot {slot.get('id') if isinstance(slot, Mapping) else None} "
                "lacks an exact platform rendering"
            )
    return _deep_freeze(raw)


def _read_prompt_geography() -> Mapping[str, Any]:
    raw = _read_json_resource("prompt_geography.json")
    if raw.get("schema_version") != "free_snapshot.prompt_geography.v1":
        raise CatalogIntegrityError("unsupported prompt geography schema")
    if raw.get("version") != EXPECTED_PROMPT_GEOGRAPHY_VERSION:
        raise CatalogIntegrityError("the prompt geography version changed")
    if "Patient-facing geography choices" not in str(raw.get("purpose") or ""):
        raise CatalogIntegrityError(
            "the prompt geography must remain patient-facing only"
        )
    if canonical_sha256(raw) != EXPECTED_PROMPT_GEOGRAPHY_HASH:
        raise CatalogIntegrityError("the prompt geography hash changed")
    return _deep_freeze(raw)


PROMPT_CATALOG_V4 = _read_prompt_catalog()
PROMPT_CATALOG_V4_VERSION: Final[str] = str(
    PROMPT_CATALOG_V4["catalog_version"]
)
PROMPT_CATALOG_V4_CONTENT_HASH: Final[str] = str(
    PROMPT_CATALOG_V4["content_sha256"]
)
PROMPT_GEOGRAPHY_V1 = _read_prompt_geography()
PROMPT_GEOGRAPHY_VERSION: Final[str] = str(PROMPT_GEOGRAPHY_V1["version"])
PROMPT_GEOGRAPHY_HASH: Final[str] = canonical_sha256(PROMPT_GEOGRAPHY_V1)
PROMPT_GEOGRAPHY_COUNTRY_CODES: Final[tuple[str, ...]] = tuple(
    sorted(
        {
            str(row.get("country_code") or "").upper()
            for row in PROMPT_GEOGRAPHY_V1["cities"]
            if isinstance(row, Mapping)
            and str(row.get("country_code") or "").strip()
        }
    )
)
PROMPT_INTENTS_V4: Final[tuple[str, ...]] = tuple(
    str(row["semantic_intent"]) for row in PROMPT_CATALOG_V4["slots"]
)

PROMPT_TEMPLATE_ARTIFACT_V4 = MappingProxyType(
    {
        "version": PROMPT_PACK_V4_VERSION,
        "schema_version": PROMPT_PACK_V4_SCHEMA_VERSION,
        "runtime_contract_version": "snapshot-prompts-v4-runtime-v1",
        "catalog_version": PROMPT_CATALOG_V4_VERSION,
        "catalog_content_sha256": PROMPT_CATALOG_V4_CONTENT_HASH,
        "platforms": PROMPT_CATALOG_V4["platforms"],
        "baskets": PROMPT_CATALOG_V4["baskets"],
        "slots": PROMPT_CATALOG_V4["slots"],
    }
)
PROMPT_TEMPLATE_ARTIFACT_HASH_V4: Final[str] = canonical_sha256(
    PROMPT_TEMPLATE_ARTIFACT_V4
)
if PROMPT_TEMPLATE_ARTIFACT_HASH_V4 != EXPECTED_PROMPT_TEMPLATE_ARTIFACT_HASH:
    raise CatalogIntegrityError("the v4 prompt template artifact hash changed")

PROMPT_PACK_CONTRACTS = MappingProxyType(
    {
        PROMPT_PACK_V4_VERSION: MappingProxyType(
            {
                "schema_version": PROMPT_PACK_V4_SCHEMA_VERSION,
                "prompt_intents": PROMPT_INTENTS_V4,
                "templates": (),
                "slots": PROMPT_CATALOG_V4["slots"],
                "baskets": PROMPT_CATALOG_V4["baskets"],
                "platforms": PROMPT_CATALOG_V4["platforms"],
                "catalog_version": PROMPT_CATALOG_V4_VERSION,
                "catalog_content_sha256": (
                    PROMPT_CATALOG_V4_CONTENT_HASH
                ),
                "template_artifact_hash": (
                    PROMPT_TEMPLATE_ARTIFACT_HASH_V4
                ),
                "scope_version": SCOPE_V4_VERSION,
                "scope_schema_version": SCOPE_V4_SCHEMA_VERSION,
                "observation_plan_schema_version": (
                    OBSERVATION_PLAN_V4_SCHEMA_VERSION
                ),
                "evidence_policy_version": EVIDENCE_POLICY_V4_VERSION,
                "evidence_policy_artifact_hash": (
                    EVIDENCE_POLICY_ARTIFACT_HASH_V4
                ),
            }
        )
    }
)


__all__ = [
    "BROWSER_MEASUREMENT_MODE",
    "COPY_POLICY_VERSION",
    "CatalogIntegrityError",
    "DEFAULT_PLANNED_RUNS",
    "DEFAULT_PLATFORMS",
    "EVIDENCE_POLICY_ARTIFACT_HASH_V4",
    "EVIDENCE_POLICY_V4_VERSION",
    "MAX_MODULE_MEASUREMENT_RUNS",
    "MAX_SELECTED_AREAS",
    "MIN_SELECTED_AREAS",
    "MODULE_MEASUREMENT_EXECUTION_POLICY_VERSION",
    "MODULE_MEASUREMENT_PLATFORMS",
    "NAME_RECONCILIATION_ARTIFACT_HASH",
    "NAME_RECONCILIATION_POLICY_VERSION",
    "OBSERVATION_PLAN_V4_SCHEMA_VERSION",
    "PROMPT_CATALOG_V4",
    "PROMPT_CATALOG_V4_CONTENT_HASH",
    "PROMPT_CATALOG_V4_VERSION",
    "PROMPT_GEOGRAPHY_COUNTRY_CODES",
    "PROMPT_GEOGRAPHY_HASH",
    "PROMPT_GEOGRAPHY_V1",
    "PROMPT_GEOGRAPHY_VERSION",
    "PROMPT_INTENTS_V4",
    "PROMPT_PACK_CONTRACTS",
    "PROMPT_PACK_V4_SCHEMA_VERSION",
    "PROMPT_PACK_V4_VERSION",
    "PROMPT_PACK_VERSION",
    "PROMPT_TEMPLATE_ARTIFACT_HASH_V4",
    "SCOPE_V4_SCHEMA_VERSION",
    "SCOPE_V4_VERSION",
    "TEST_MODE_PLANNED_RUNS",
]
