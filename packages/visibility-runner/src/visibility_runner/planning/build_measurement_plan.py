"""Build fixed visibility measurement plans from the selected inputs."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from copy import deepcopy
from collections.abc import Sequence
from typing import Iterable, Mapping, Optional

from ..config import (
    GitHubCopilotConfig,
    COPILOT_BROWSER_MEASUREMENT_MODE,
    MeasurementConfig,
    Platform,
    ProfileConfig,
    ProfileIdentity,
    RetryPolicy,
)
from ..contract_primitives import (
    PLATFORM_ALIASES as _PLATFORM_ALIASES,
    platform_key,
)
from ..errors import ConfigurationError
from ..models import (
    PacketKind,
    PacketPlan,
    RunCounts,
    RunPlan,
)
from .count_allowed_sends import (
    build_gaio_extraction_packet_budget,
    build_measurement_packet_budget,
    build_submission_budget,
    exact_chat_message_budget,
)
from .catalog import (
    BROWSER_MEASUREMENT_MODE,
    COPILOT_DERIVED_MEASUREMENT_MODE,
    COPY_POLICY_VERSION,
    DEFAULT_PLANNED_RUNS,
    DEFAULT_PLATFORMS,
    MAX_MODULE_MEASUREMENT_RUNS,
    MODULE_MEASUREMENT_PLATFORMS,
    NAME_RECONCILIATION_ARTIFACT_HASH,
    NAME_RECONCILIATION_POLICY_VERSION,
    MAX_SELECTED_AREAS,
    MIN_SELECTED_AREAS,
    MODULE_MEASUREMENT_EXECUTION_POLICY_VERSION,
    PROMPT_CATALOG_V4_CONTENT_HASH,
    PROMPT_CATALOG_V4_VERSION,
    PROMPT_GEOGRAPHY_HASH,
    PROMPT_GEOGRAPHY_COUNTRY_CODES,
    PROMPT_GEOGRAPHY_V1,
    PROMPT_GEOGRAPHY_VERSION,
    PROMPT_PACK_CONTRACTS,
    PROMPT_PACK_V4_VERSION,
    PROMPT_PACK_VERSION,
    TEST_MODE_PLANNED_RUNS,
)
from . import legacy_snapshot
from .geography import treatment_key
from .hashing import canonical_sha256


def normalize_platform(value: object) -> str:
    """Return the canonical website key without importing browser code."""

    try:
        return platform_key(value, legacy_planning=True)
    except KeyError as exc:
        raise ValueError(f"Unsupported platform: {value!r}") from exc


_SPACE_RE = re.compile(r"\s+")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_VARIANT_TYPES = ("area", "postcode", "station", "landmark")


def _clean(value: object) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()


class FrozenScopeError(ValueError):
    """The supplied scope is incomplete, invalid, or no longer hash-identical."""


def _prompt_contract(version: object) -> Mapping:
    normalized = _clean(version or PROMPT_PACK_VERSION)
    contract = PROMPT_PACK_CONTRACTS.get(normalized)
    if contract is None:
        supported = ", ".join(PROMPT_PACK_CONTRACTS)
        raise FrozenScopeError(
            f"unsupported prompt_pack_version {normalized!r}; expected one of: {supported}"
        )
    return contract


def _slug(value: object, *, fallback: str = "item") -> str:
    text = unicodedata.normalize("NFKD", _clean(value)).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    return _SLUG_RE.sub("-", text).strip("-") or fallback


def _dedupe(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = _clean(value)
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return result


def _scope_archetype(scope: Mapping) -> Mapping:
    if scope.get("scope_kind") == "area_treatments":
        return scope.get("clinic_archetype") or {}
    clinic = scope.get("clinic") or {}
    return clinic.get("archetype") if isinstance(clinic, Mapping) else {}


def _normalize_archetype(source: object, *, field_name: str) -> dict:
    if not isinstance(source, Mapping):
        raise FrozenScopeError(f"v4 Snapshot scope requires {field_name}")
    archetype = {
        "id": _clean(source.get("id")),
        "singular": _clean(source.get("singular")),
        "plural": _clean(source.get("plural")),
        "confirmation_status": _clean(source.get("confirmation_status")).casefold(),
    }
    for field in ("id", "singular", "plural"):
        if not archetype[field]:
            raise FrozenScopeError(f"{field_name}.{field} is required for v4")
    if archetype["confirmation_status"] != "confirmed":
        raise FrozenScopeError(f"{field_name} must be confirmed before freezing v4")
    return archetype


def _normalize_clinic(config: Mapping, *, require_archetype: bool = False) -> dict:
    source = config.get("clinic") if isinstance(config.get("clinic"), Mapping) else {}
    name = _clean(source.get("name") or config.get("clinic_name"))
    if not name:
        raise FrozenScopeError("Snapshot scope requires clinic.name (or legacy clinic_name)")

    explicit_account_id = _clean(source.get("account_id") or config.get("account_id"))
    account_id = explicit_account_id or f"external-{_slug(name)}"
    aliases = source.get("verified_aliases")
    if aliases is None:
        aliases = config.get("clinic_aliases") or []
    if not isinstance(aliases, (list, tuple)):
        raise FrozenScopeError("clinic.verified_aliases must be a list")
    aliases = [alias for alias in _dedupe(aliases) if alias.casefold() != name.casefold()]
    identity_status = _clean(
        source.get("identity_status")
        or config.get("clinic_identity_status")
        or ("confirmed" if explicit_account_id else "unconfirmed")
    ).casefold()
    normalized = {
        "account_id": account_id,
        "name": name,
        "verified_aliases": aliases,
        "identity_status": identity_status,
    }
    if not require_archetype:
        return normalized

    normalized["archetype"] = _normalize_archetype(
        source.get("archetype"), field_name="clinic.archetype"
    )
    return normalized


def _variant_rows(source: Mapping, area_name: str) -> list[dict]:
    raw = source.get("variants")
    values: dict[str, str] = {}
    if isinstance(raw, Mapping):
        for kind in _VARIANT_TYPES:
            values[kind] = _clean(raw.get(kind))
    elif isinstance(raw, (list, tuple)):
        for row in raw:
            if isinstance(row, Mapping):
                kind = _clean(row.get("type") or row.get("kind")).casefold()
                value = _clean(row.get("value") or row.get("name"))
                if kind in _VARIANT_TYPES and value and kind not in values:
                    values[kind] = value
            elif isinstance(row, str) and "area" not in values:
                values["area"] = _clean(row)
    for kind in _VARIANT_TYPES:
        if kind not in values:
            values[kind] = _clean(source.get(kind))
    values["area"] = values.get("area") or area_name
    return [
        {"type": kind, "value": values[kind]}
        for kind in _VARIANT_TYPES
        if values.get(kind)
    ]


def _normalize_catchment(config: Mapping) -> dict:
    source = config.get("catchment") if isinstance(config.get("catchment"), Mapping) else {}
    name = _clean(source.get("name") or source.get("area") or config.get("area"))
    if not name:
        raise FrozenScopeError("Snapshot scope requires catchment.name (or legacy area)")
    explicit_area_id = _clean(source.get("area_id") or config.get("area_id"))
    country_code = _clean(source.get("country_code") or config.get("country_code") or "GB").upper()
    if len(country_code) != 2 or not country_code.isalpha():
        raise FrozenScopeError("catchment.country_code must be a two-letter country code")
    confirmation_status = _clean(
        source.get("confirmation_status")
        or source.get("relevance_status")
        or config.get("catchment_confirmation_status")
        or ("confirmed" if explicit_area_id else "unconfirmed")
    ).casefold()
    return {
        "area_id": explicit_area_id or _slug(name, fallback="area"),
        "name": name,
        "country_code": country_code,
        "variants": _variant_rows(source, name),
        "confirmation_status": confirmation_status,
    }


def _normalize_treatments(config: Mapping) -> list[dict]:
    raw = config.get("treatments")
    if raw is None:
        raw = [config.get(f"treatment_{index}") for index in range(1, 4)]
    if not isinstance(raw, (list, tuple)):
        raise FrozenScopeError("treatments must be a list containing one to three treatments")

    result: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, 1):
        source = item if isinstance(item, Mapping) else {}
        name = _clean(source.get("name") if source else item)
        if not name:
            continue
        key = treatment_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        explicit_id = _clean(source.get("id") if source else "")
        status = _clean(
            (source.get("commercial_relevance_status") if source else None)
            or config.get(f"treatment_{index}_commercial_relevance_status")
            or ("confirmed" if source else "unconfirmed")
        ).casefold()
        result.append(
            {
                "id": explicit_id or _slug(name, fallback=f"treatment-{index}"),
                "name": name,
                "commercial_relevance_status": status,
            }
        )
    if not 1 <= len(result) <= 3:
        raise FrozenScopeError("Snapshot scope requires one to three unique treatments")
    ids = [row["id"] for row in result]
    if len(ids) != len(set(ids)):
        raise FrozenScopeError("treatment ids must be unique")
    return result


def _normalize_geography_target(
    value: object,
    *,
    tier: str,
    country_code: str,
) -> dict:
    if not isinstance(value, Mapping):
        raise FrozenScopeError(f"areas[].geographies.{tier} must be an object")
    target = {
        "id": _clean(value.get("id") or value.get("area_id")),
        "name": _clean(value.get("name") or value.get("area")),
        "country_code": _clean(value.get("country_code") or country_code).upper(),
        "confirmation_status": _clean(value.get("confirmation_status")).casefold(),
    }
    if not target["id"] or not target["name"]:
        raise FrozenScopeError(
            f"areas[].geographies.{tier} requires confirmed id and name"
        )
    if target["confirmation_status"] != "confirmed":
        raise FrozenScopeError(
            f"areas[].geographies.{tier} must be confirmed before freezing v4"
        )
    if len(target["country_code"]) != 2 or not target["country_code"].isalpha():
        raise FrozenScopeError(
            f"areas[].geographies.{tier}.country_code must be a two-letter country code"
        )
    return target


def _assert_patient_facing_taxonomy_target(target: Mapping, *, tier: str) -> None:
    taxonomy_key = "greater_areas" if tier == "greater_area" else "cities"
    allowed = {
        (_clean(row.get("id")), _clean(row.get("name")))
        for row in PROMPT_GEOGRAPHY_V1[taxonomy_key]
        if isinstance(row, Mapping)
    }
    if (_clean(target.get("id")), _clean(target.get("name"))) not in allowed:
        raise FrozenScopeError(
            f"areas[].geographies.{tier} must use the frozen patient-facing geography taxonomy"
        )
    if _clean(target.get("country_code")).upper() not in (
        PROMPT_GEOGRAPHY_COUNTRY_CODES
    ):
        raise FrozenScopeError(
            f"areas[].geographies.{tier}.country_code is outside the "
            "frozen patient-facing geography taxonomy"
        )


def _normalize_area_scopes(
    config: Mapping,
    *,
    require_v4_geographies: bool = False,
) -> list[dict]:
    """Normalize one or two independently validated selected-area scopes.

    ``areas`` is the production contract.  The legacy top-level
    ``catchment`` + ``treatments`` shape remains readable so frozen v2 inputs
    and command-line fixtures do not become unusable.
    """

    raw = config.get("areas")
    if raw is None:
        if require_v4_geographies:
            raise FrozenScopeError(
                "v4 Snapshot scope requires areas with local, greater_area, and city geographies"
            )
        return [
            {
                "branch": {
                    "id": _clean(config.get("branch_id")) or "primary-branch",
                    "name": _clean(config.get("branch_name"))
                    or _clean((config.get("catchment") or {}).get("name"))
                    or "Primary branch",
                },
                "catchment": _normalize_catchment(config),
                "treatments": _normalize_treatments(config),
                "validation_status": _clean(
                    config.get("branch_treatment_validation_status") or "confirmed"
                ).casefold(),
            }
        ]
    if not isinstance(raw, (list, tuple)):
        raise FrozenScopeError("areas must be a list containing one or two selected-area scopes")
    if not MIN_SELECTED_AREAS <= len(raw) <= MAX_SELECTED_AREAS:
        raise FrozenScopeError(
            "Free Snapshot scope requires one or two selected areas (maximum 2)"
        )

    result: list[dict] = []
    area_ids: set[str] = set()
    branch_ids: set[str] = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            raise FrozenScopeError(f"areas[{index - 1}] must be an object")
        branch_source = item.get("branch") if isinstance(item.get("branch"), Mapping) else {}
        branch_id = _clean(branch_source.get("id") or item.get("branch_id"))
        branch_name = _clean(branch_source.get("name") or item.get("branch_name"))
        catchment = _normalize_catchment(item)
        treatments = _normalize_treatments(item)
        branch_id = branch_id or catchment["area_id"]
        branch_name = branch_name or catchment["name"]
        if branch_id in branch_ids:
            raise FrozenScopeError("selected branch ids must be unique")
        if catchment["area_id"] in area_ids:
            raise FrozenScopeError("selected branches must use distinct area ids")
        branch_ids.add(branch_id)
        area_ids.add(catchment["area_id"])
        normalized_area = {
                "branch": {
                    "id": branch_id,
                    "name": branch_name,
                    "address": _clean(branch_source.get("address") or item.get("address")) or None,
                    "source_url": _clean(
                        branch_source.get("source_url") or item.get("source_url")
                    )
                    or None,
                    "member_uids": [
                        _clean(value)
                        for value in branch_source.get("member_uids") or []
                        if _clean(value)
                    ],
                    "location_confidence": _clean(
                        branch_source.get("location_confidence")
                        or item.get("location_confidence")
                    )
                    or None,
                    "requires_manual_confirmation": bool(
                        branch_source.get("requires_manual_confirmation")
                        or item.get("requires_manual_confirmation")
                    ),
                },
                "catchment": catchment,
                "treatments": treatments,
                "validation_status": _clean(
                    item.get("validation_status")
                    or item.get("branch_treatment_validation_status")
                    or "unconfirmed"
                ).casefold(),
                "validated_by": _clean(item.get("validated_by")) or None,
                "validated_at": _clean(item.get("validated_at")) or None,
            }
        if require_v4_geographies:
            geography_source = item.get("geographies")
            if not isinstance(geography_source, Mapping):
                raise FrozenScopeError(
                    f"areas[{index - 1}].geographies is required for v4"
                )
            geographies = {
                tier: _normalize_geography_target(
                    geography_source.get(tier),
                    tier=tier,
                    country_code=catchment["country_code"],
                )
                for tier in ("local", "greater_area", "city")
            }
            local = geographies["local"]
            if local["id"] != catchment["area_id"] or local["name"] != catchment["name"]:
                raise FrozenScopeError(
                    "areas[].geographies.local must mirror catchment area_id and name"
                )
            if len(
                {
                    geographies[tier]["country_code"]
                    for tier in ("local", "greater_area", "city")
                }
            ) != 1:
                raise FrozenScopeError(
                    "all v4 geography tiers must use the same country code"
                )
            _assert_patient_facing_taxonomy_target(
                geographies["greater_area"], tier="greater_area"
            )
            _assert_patient_facing_taxonomy_target(geographies["city"], tier="city")
            normalized_area["geographies"] = geographies
        result.append(normalized_area)
    return result


def area_scopes(scope: Mapping) -> list[dict]:
    """Return the area-treatment matrix for new and legacy frozen scopes."""

    raw = scope.get("areas")
    if isinstance(raw, list) and raw:
        return [dict(row) for row in raw if isinstance(row, Mapping)]
    return [
        {
            "branch": {
                "id": str((scope.get("catchment") or {}).get("area_id") or "primary-branch"),
                "name": str((scope.get("catchment") or {}).get("name") or "Primary branch"),
            },
            "catchment": scope.get("catchment") or {},
            "treatments": scope.get("treatments") or [],
            "validation_status": "confirmed",
        }
    ]


def _normalize_platforms(
    config: Mapping,
    *,
    exact_platforms: Optional[Iterable[str]] = None,
) -> list[str]:
    raw = config.get("platforms")
    values = DEFAULT_PLATFORMS if raw in (None, "") else raw
    if isinstance(values, str):
        values = re.split(r"[,;\s]+", values.strip())
    if not isinstance(values, (list, tuple)):
        raise FrozenScopeError("platforms must be a list or comma-separated string")
    result: list[str] = []
    for value in values:
        if not _clean(value):
            continue
        try:
            platform = normalize_platform(_clean(value))
        except ValueError as exc:
            raise FrozenScopeError(str(exc)) from exc
        if platform not in result:
            result.append(platform)
    if not result:
        raise FrozenScopeError("Snapshot scope requires at least one platform")
    if exact_platforms is not None:
        canonical = [str(platform) for platform in exact_platforms]
        if set(result) != set(canonical) or len(result) != len(canonical):
            raise FrozenScopeError(
                "v4 Snapshot scope requires exactly the four frozen platforms: "
                + ", ".join(canonical)
            )
        # Input aliases and ordering are normalized before freezing so prompt
        # and prompt measurement ordering is deterministic across equivalent configs.
        return canonical
    return result


def _variants_by_type(catchment: Mapping) -> dict[str, str]:
    return {
        str(row.get("type")): _clean(row.get("value"))
        for row in catchment.get("variants") or []
        if isinstance(row, Mapping) and _clean(row.get("type")) and _clean(row.get("value"))
    }


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _v4_evidence_cells(prompt: Mapping) -> list[dict]:
    cells: list[dict] = []
    subject = prompt["subject"]
    for origin in prompt["origin_branches"]:
        payload = {
            "contract": "free_snapshot.evidence_cell.v4",
            "basket_id": prompt["basket_id"],
            "platform": prompt["platform"],
            "origin_branch_id": origin["branch_id"],
            "origin_area_id": origin["local_area_id"],
            "subject_kind": prompt["subject_kind"],
            "subject_id": subject["id"],
        }
        digest = canonical_sha256(payload)[:24]
        cells.append(
            {
                "analysis_scope_id": f"analysis-scope-{digest}",
                "evidence_cell_id": f"evidence-cell-{digest}",
                "basket_id": prompt["basket_id"],
                "platform": prompt["platform"],
                "origin_branch_id": origin["branch_id"],
                "origin_branch_name": origin["branch_name"],
                "origin_area_id": origin["local_area_id"],
                "origin_area_name": origin["local_area_name"],
                "subject_kind": prompt["subject_kind"],
                "subject_id": subject["id"],
                "subject_name": subject["name"],
                "treatment_id": prompt["treatment_id"],
                "treatment_name": prompt["treatment_name"],
            }
        )
    return cells


def _v4_prompt_rows(scope: Mapping) -> list[dict]:
    contract = _prompt_contract(scope.get("prompt_pack_version"))
    archetype = _scope_archetype(scope)
    accumulated: dict[tuple, dict] = {}

    for platform in scope["platforms"]:
        rendering_platform = "copilot" if platform == "github_copilot" else platform
        if rendering_platform not in contract["platforms"]:
            raise FrozenScopeError(
                f"{scope['prompt_pack_version']} has no frozen rendering for {platform}"
            )
        for area_order, area_scope in enumerate(area_scopes(scope), 1):
            catchment = area_scope["catchment"]
            branch = area_scope.get("branch") or {}
            geographies = area_scope["geographies"]
            variants = _variants_by_type(catchment)
            local_area = variants.get("area") or catchment["name"]
            origin_base = {
                "branch_id": branch.get("id"),
                "branch_name": branch.get("name"),
                "local_area_id": catchment["area_id"],
                "local_area_name": catchment["name"],
                "area_order": area_order,
            }
            subjects: list[tuple[dict, Optional[dict], Optional[int]]] = [
                (
                    {"id": treatment["id"], "name": treatment["name"]},
                    treatment,
                    treatment_order,
                )
                for treatment_order, treatment in enumerate(area_scope["treatments"], 1)
            ]
            if archetype:
                subjects.append(
                    (
                        {
                            "id": archetype["id"],
                            "name": archetype["singular"],
                            "singular": archetype["singular"],
                            "plural": archetype["plural"],
                        },
                        None,
                        None,
                    )
                )
            for subject, treatment, treatment_order in subjects:
                subject_kind = "treatment" if treatment is not None else "clinic_archetype"
                for slot in contract["slots"]:
                    if slot["subject_kind"] != subject_kind:
                        continue
                    geography_tier = str(slot["geography_level"])
                    target = deepcopy(dict(geographies[geography_tier]))
                    rendering = slot["renderings"][rendering_platform]
                    template = str(rendering["text"])
                    requested_variant = geography_tier
                    actual_variant_type = geography_tier
                    actual_variant_value = target["name"]
                    used_fallback = False
                    if "{station_or_area}" in template:
                        requested_variant = "station"
                        actual_variant_type = "station" if variants.get("station") else "area"
                        actual_variant_value = variants.get("station") or local_area
                        used_fallback = actual_variant_type != "station"
                    elif "{postcode_or_area}" in template:
                        requested_variant = "postcode"
                        actual_variant_type = "postcode" if variants.get("postcode") else "area"
                        actual_variant_value = variants.get("postcode") or local_area
                        used_fallback = actual_variant_type != "postcode"
                    values = {
                        "treatment": treatment["name"] if treatment is not None else "",
                        "local_area": local_area,
                        "station_or_area": variants.get("station") or local_area,
                        "postcode_or_area": variants.get("postcode") or local_area,
                        "greater_area": geographies["greater_area"]["name"],
                        "city": geographies["city"]["name"],
                        "clinic_archetype_singular": archetype.get("singular", ""),
                        "clinic_archetype_plural": archetype.get("plural", ""),
                    }
                    text = template.format(**values)
                    text_hash = _text_sha256(text)
                    semantic_payload = {
                        "contract": "free_snapshot.semantic_prompt.v4",
                        "semantic_slot_id": slot["id"],
                        "subject_kind": subject_kind,
                        "subject_id": subject["id"],
                        "target_geography": target,
                    }
                    semantic_prompt_id = (
                        f"semantic-{str(slot['id']).casefold()}-"
                        f"{canonical_sha256(semantic_payload)[:20]}"
                    )
                    prompt_identity = {
                        **semantic_payload,
                        "platform": platform,
                        "prompt_text_sha256": text_hash,
                    }
                    prompt_id = (
                        f"prompt-v4-{platform}-{str(slot['id']).casefold()}-"
                        f"{canonical_sha256(prompt_identity)[:20]}"
                    )
                    collection_payload = {
                        "contract": "free_snapshot.collection_scope.v4",
                        "basket_id": slot["basket_id"],
                        "platform": platform,
                        "subject_kind": subject_kind,
                        "subject_id": subject["id"],
                        "target_geography": target,
                    }
                    collection_scope_id = (
                        f"collection-scope-{canonical_sha256(collection_payload)[:24]}"
                    )
                    origin = {
                        **origin_base,
                        "treatment_order": treatment_order,
                    }
                    dedupe_key = (
                        platform,
                        text,
                        subject_kind,
                        canonical_sha256(subject),
                        geography_tier,
                        canonical_sha256(target),
                    )
                    row = accumulated.get(dedupe_key)
                    if row is not None and geography_tier in {"greater_area", "city"}:
                        if origin["branch_id"] not in row["origin_branch_ids"]:
                            row["origin_branches"].append(origin)
                            row["origin_branch_ids"].append(origin["branch_id"])
                            row["origin_area_ids"].append(origin["local_area_id"])
                        continue

                    # Local prompts intentionally remain branch-specific.  Their
                    # target IDs are required to be distinct, but include the
                    # origin in the key as an additional fail-closed guard.
                    if geography_tier == "local":
                        dedupe_key = (*dedupe_key, origin["branch_id"])
                    row = {
                        "id": prompt_id,
                        "prompt_id": prompt_id,
                        "platform": platform,
                        "text": text,
                        "prompt_text_sha256": text_hash,
                        "text_sha256": text_hash,
                        "prompt_pack_version": scope["prompt_pack_version"],
                        "catalog_version": contract["catalog_version"],
                        "catalog_content_sha256": contract["catalog_content_sha256"],
                        "basket_id": slot["basket_id"],
                        "basket_sequence": int(slot["sequence"]),
                        "semantic_slot_id": slot["id"],
                        "semantic_prompt_id": semantic_prompt_id,
                        "semantic_intent": slot["semantic_intent"],
                        "prompt_intent": slot["semantic_intent"],
                        "intent": slot["semantic_intent"],
                        "prompt_family": str(slot["id"]).casefold(),
                        "subject_kind": subject_kind,
                        "subject_id": subject["id"],
                        "subject_name": subject["name"],
                        "subject": subject,
                        "treatment_id": treatment["id"] if treatment is not None else None,
                        "treatment_name": treatment["name"] if treatment is not None else None,
                        "treatment_order": treatment_order,
                        "clinic_archetype_id": archetype["id"] if treatment is None else None,
                        "clinic_archetype_singular": (
                            archetype["singular"] if treatment is None else None
                        ),
                        "clinic_archetype_plural": (
                            archetype["plural"] if treatment is None else None
                        ),
                        "geography_tier": geography_tier,
                        "target_geography": target,
                        "area_id": target["id"],
                        "area_name": target["name"],
                        "origin_branches": [origin],
                        "origin_branch_ids": [origin["branch_id"]],
                        "origin_area_ids": [origin["local_area_id"]],
                        "collection_scope_id": collection_scope_id,
                        "scope_id": collection_scope_id,
                        "requested_variant_type": requested_variant,
                        "actual_variant": {
                            "type": actual_variant_type,
                            "value": actual_variant_value,
                            "used_fallback": used_fallback,
                        },
                        "naturalness_score": int(rendering["naturalness_score"]),
                        "slot_naturalness_score": int(slot["naturalness_score"]),
                        "report_eligible": True,
                    }
                    accumulated[dedupe_key] = row

    rows = list(accumulated.values())
    for row in rows:
        cells = _v4_evidence_cells(row)
        row["origin"] = {"branches": deepcopy(row["origin_branches"])}
        row["branch_id"] = (
            row["origin_branches"][0]["branch_id"]
            if len(row["origin_branches"]) == 1
            else None
        )
        row["branch_name"] = (
            row["origin_branches"][0]["branch_name"]
            if len(row["origin_branches"]) == 1
            else None
        )
        row["area_order"] = (
            row["origin_branches"][0]["area_order"]
            if len(row["origin_branches"]) == 1
            else None
        )
        row["evidence_cells"] = cells
        row["analysis_scope_ids"] = [cell["analysis_scope_id"] for cell in cells]
        row["evidence_cell_ids"] = [cell["evidence_cell_id"] for cell in cells]
        row["analysis_scope_id"] = (
            row["analysis_scope_ids"][0] if len(cells) == 1 else None
        )
        row["evidence_cell_id"] = row["evidence_cell_ids"][0] if len(cells) == 1 else None
    return rows


def _prompt_rows(scope: Mapping) -> list[dict]:
    if str(scope.get("scope_kind", "")).startswith("client_research_"):
        from .client_research import prompt_rows
        return prompt_rows(scope)
    if scope.get("scope_kind") == "diagnostic":
        from .diagnostic import prompt_rows
        return prompt_rows(scope)
    if _clean(scope.get("prompt_pack_version")) == PROMPT_PACK_V4_VERSION:
        return _v4_prompt_rows(scope)
    return legacy_snapshot.prompt_rows(
        scope,
        templates=_prompt_contract(scope.get("prompt_pack_version"))["templates"],
        areas=area_scopes(scope),
    )


def _prompt_hash_payload(scope: Mapping, rows: list[dict]) -> dict:
    if str(scope.get("scope_kind", "")).startswith("client_research_"):
        from .client_research import prompt_hash_payload
        return prompt_hash_payload(scope, rows)
    if scope.get("scope_kind") == "diagnostic":
        from .diagnostic import prompt_hash_payload
        return prompt_hash_payload(scope, rows)
    contract = _prompt_contract(scope.get("prompt_pack_version"))
    return {
        "schema_version": contract["schema_version"],
        "snapshot_id": scope["snapshot_id"],
        "prompt_pack_version": scope["prompt_pack_version"],
        "report_eligible": True,
        "prompts": rows,
    }


def prompt_pack_hash(scope: Mapping) -> str:
    """Recompute the prompt hash implied by a scope's treatment/catchment data."""

    return canonical_sha256(_prompt_hash_payload(scope, _prompt_rows(scope)))


def scope_hash(scope: Mapping) -> str:
    payload = deepcopy(dict(scope))
    payload.pop("scope_hash", None)
    return canonical_sha256(payload)


def freeze_scope(
    config: MeasurementConfig | Mapping,
    *,
    snapshot_id: Optional[str] = None,
    test_mode: bool = False,
    prompt_pack_version: Optional[str] = None,
) -> dict:
    """Normalize and hash the immutable Snapshot scope manifest.

    Production mode remains exactly three runs and test mode remains one.
    The explicit application measurement policy may select one to twenty
    repetitions, but is permanently marked as ineligible for publication.
    """

    scope, _ = _freeze_scope_and_prompt_rows(
        config,
        snapshot_id=snapshot_id,
        test_mode=test_mode,
        prompt_pack_version=prompt_pack_version,
    )
    return scope


def _freeze_scope_and_prompt_rows(
    config: MeasurementConfig | Mapping,
    *,
    snapshot_id: Optional[str],
    test_mode: bool,
    prompt_pack_version: Optional[str],
) -> tuple[dict, list[dict]]:
    """Normalize once and retain the exact rows used to hash the scope."""

    if not isinstance(config, (MeasurementConfig, Mapping)):
        raise FrozenScopeError("Snapshot config must be a mapping")
    research = str(config.scope_kind if isinstance(config, MeasurementConfig) else config.get("scope_kind", "")).startswith("client_research_")
    if research:
        from .client_research import freeze
        from ..client_research import CLIENT_RESEARCH_PROMPT_VERSION
        if test_mode or prompt_pack_version not in (None, CLIENT_RESEARCH_PROMPT_VERSION):
            raise FrozenScopeError("client research requires its own non-test prompt contract")
        resolved_id = _clean(snapshot_id)
        if not resolved_id:
            raise FrozenScopeError("snapshot_id is required")
        return freeze(config, snapshot_id=resolved_id)
    diagnostic = (
        config.scope_kind == "diagnostic"
        if isinstance(config, MeasurementConfig)
        else config.get("scope_kind") == "diagnostic"
    )
    if diagnostic:
        from ..config import DIAGNOSTIC_PROMPT_CATALOG_VERSION
        from .diagnostic import freeze
        if test_mode:
            raise FrozenScopeError("diagnostic scope is distinct from Snapshot test mode")
        if prompt_pack_version not in (None, DIAGNOSTIC_PROMPT_CATALOG_VERSION):
            raise FrozenScopeError("diagnostic scope requires its diagnostic prompt pack")
        resolved_id = _clean(snapshot_id or (
            config.get("snapshot_id") if isinstance(config, Mapping) else None
        ))
        if not resolved_id:
            raise FrozenScopeError("snapshot_id is required for deterministic pure planning")
        try:
            return freeze(config, snapshot_id=resolved_id)
        except (ConfigurationError, ValueError) as exc:
            raise FrozenScopeError(str(exc)) from exc
    if isinstance(config, MeasurementConfig):
        config = config.to_snapshot_mapping()
    execution_policy_version = _clean(
        config.get("execution_policy_version")
    )
    if execution_policy_version and (
        execution_policy_version
        != MODULE_MEASUREMENT_EXECUTION_POLICY_VERSION
    ):
        raise FrozenScopeError(
            "unsupported execution_policy_version "
            f"{execution_policy_version!r}"
        )
    module_measurement = (
        execution_policy_version
        == MODULE_MEASUREMENT_EXECUTION_POLICY_VERSION
    )
    if module_measurement and test_mode:
        raise FrozenScopeError(
            "module measurement policy is distinct from Snapshot test mode"
        )
    resolved_prompt_pack_version = _clean(
        prompt_pack_version or config.get("prompt_pack_version") or PROMPT_PACK_VERSION
    )
    prompt_contract = _prompt_contract(resolved_prompt_pack_version)
    if config.get("platform_name_reconciliation") is False:
        raise FrozenScopeError(
            "Free Snapshot requires platform_name_reconciliation=true"
        )
    for key, expected in (
        ("scope_version", prompt_contract["scope_version"]),
        ("prompt_pack_version", resolved_prompt_pack_version),
        ("evidence_policy_version", prompt_contract["evidence_policy_version"]),
        ("copy_policy_version", COPY_POLICY_VERSION),
    ):
        supplied = _clean(config.get(key))
        if supplied and supplied != expected:
            raise FrozenScopeError(f"{key} is immutable and must equal {expected!r}")

    configured_runs = config.get("planned_runs")
    if module_measurement:
        if isinstance(configured_runs, bool) or not isinstance(
            configured_runs,
            int,
        ):
            raise FrozenScopeError(
                "module measurement planned_runs must be an integer"
            )
        if not 1 <= configured_runs <= MAX_MODULE_MEASUREMENT_RUNS:
            raise FrozenScopeError(
                "module measurement planned_runs must be between 1 and "
                f"{MAX_MODULE_MEASUREMENT_RUNS}"
            )
        planned_runs = configured_runs
    else:
        planned_runs = (
            TEST_MODE_PLANNED_RUNS if test_mode else DEFAULT_PLANNED_RUNS
        )
        # Explicit test mode is a safe one-run override of the normal config.
        # Normal production mode remains frozen at three repetitions.
        if not test_mode and configured_runs not in (None, ""):
            if isinstance(configured_runs, bool):
                raise FrozenScopeError(
                    f"normal mode requires planned_runs={planned_runs}"
                )
            try:
                supplied_runs = int(configured_runs)
            except (TypeError, ValueError) as exc:
                raise FrozenScopeError(
                    f"normal mode requires planned_runs={planned_runs}"
                ) from exc
            if supplied_runs != planned_runs:
                raise FrozenScopeError(
                    f"normal mode requires planned_runs={planned_runs}"
                )

    prompt_intents = config.get("prompt_intents") or list(prompt_contract["prompt_intents"])
    if list(prompt_intents) != list(prompt_contract["prompt_intents"]):
        raise FrozenScopeError(
            f"{resolved_prompt_pack_version} requires its frozen prompt intents in order"
        )

    resolved_snapshot_id = _clean(snapshot_id or config.get("snapshot_id"))
    if not resolved_snapshot_id:
        raise FrozenScopeError(
            "snapshot_id is required for deterministic pure planning"
        )

    is_v4 = resolved_prompt_pack_version == PROMPT_PACK_V4_VERSION
    if module_measurement and not is_v4:
        raise FrozenScopeError(
            "module measurement policy requires the v4 prompt catalog"
        )
    if is_v4 and config.get("areas") is None:
        raise FrozenScopeError(
            "v4 Snapshot scope requires the areas matrix; use an explicit v2/v3 prompt pack "
            "for legacy flat scope shapes"
        )
    selected_areas = (
        _normalize_area_scopes(config, require_v4_geographies=is_v4)
        if config.get("areas") is not None
        else None
    )
    scope_dimensions = (
        {
            "areas": selected_areas,
            # Compatibility mirrors for older readers.  The authoritative mapping
            # is always ``areas``; treatments must never be inferred across areas.
            "catchment": deepcopy(selected_areas[0]["catchment"]),
            "treatments": deepcopy(selected_areas[0]["treatments"]),
        }
        if selected_areas
        else {
            "catchment": _normalize_catchment(config),
            "treatments": _normalize_treatments(config),
        }
    )
    platforms = _normalize_platforms(
        config,
        exact_platforms=(
            prompt_contract["platforms"]
            if is_v4 and not module_measurement
            else None
        ),
    )
    if module_measurement and any(
        platform not in MODULE_MEASUREMENT_PLATFORMS
        for platform in platforms
    ):
        raise FrozenScopeError(
            "module measurement platforms are currently limited to: "
            + ", ".join(MODULE_MEASUREMENT_PLATFORMS)
        )
    platform_data_modes = {
        platform: (
            (COPILOT_BROWSER_MEASUREMENT_MODE if module_measurement else COPILOT_DERIVED_MEASUREMENT_MODE)
            if platform == "copilot"
            else "github_copilot_sdk_source_gemini_parser_v1" if platform == "github_copilot"
            else BROWSER_MEASUREMENT_MODE
        )
        for platform in platforms
    }
    configured_data_modes = config.get("platform_data_modes")
    if configured_data_modes is not None:
        if not isinstance(configured_data_modes, Mapping):
            raise FrozenScopeError("platform_data_modes must be a mapping")
        if dict(configured_data_modes) != platform_data_modes:
            raise FrozenScopeError(
                "platform_data_modes is immutable and must match the current collection contract"
            )
    scope_kind = config.get("scope_kind", "clinic")
    if scope_kind == "area_treatments":
        if not module_measurement or not is_v4:
            raise FrozenScopeError("area_treatments scope requires the v4 module measurement policy")
        if "clinic" in config or any(
            key in config for key in ("clinic_name", "clinic_aliases", "account_id", "clinic_identity_status")
        ):
            raise FrozenScopeError("area_treatments scope must omit clinic identity")
        identity = {"scope_kind": scope_kind}
        if config.get("clinic_archetype") is not None:
            identity["clinic_archetype"] = _normalize_archetype(
                config["clinic_archetype"], field_name="clinic_archetype"
            )
    elif scope_kind == "clinic":
        if "clinic_archetype" in config:
            raise FrozenScopeError("clinic scope uses clinic.archetype, not clinic_archetype")
        identity = {"clinic": _normalize_clinic(config, require_archetype=is_v4)}
    else:
        raise FrozenScopeError("scope_kind must be clinic or area_treatments")
    manifest = {
        "schema_version": prompt_contract["scope_schema_version"],
        "scope_version": prompt_contract["scope_version"],
        "snapshot_id": resolved_snapshot_id,
        "outside_in": bool(config.get("outside_in", True)),
        **identity,
        **scope_dimensions,
        "platforms": platforms,
        "platform_data_modes": platform_data_modes,
        "prompt_intents": list(prompt_contract["prompt_intents"]),
        "planned_runs": planned_runs,
        "test_mode": bool(test_mode),
        "prompt_pack_version": resolved_prompt_pack_version,
        "evidence_policy_version": prompt_contract["evidence_policy_version"],
        "copy_policy_version": COPY_POLICY_VERSION,
        "competitor_link_verification": False,
        "platform_name_reconciliation": True,
        "name_reconciliation_policy_version": NAME_RECONCILIATION_POLICY_VERSION,
        "name_reconciliation_artifact_hash": NAME_RECONCILIATION_ARTIFACT_HASH,
        "prompt_template_artifact_hash": prompt_contract["template_artifact_hash"],
        "evidence_policy_artifact_hash": prompt_contract["evidence_policy_artifact_hash"],
    }
    if module_measurement:
        manifest.update(
            {
                "execution_policy_version": (
                    MODULE_MEASUREMENT_EXECUTION_POLICY_VERSION
                ),
                "publication_eligible": False,
            }
        )
    if is_v4:
        manifest.update(
            {
                "prompt_catalog_version": PROMPT_CATALOG_V4_VERSION,
                "prompt_catalog_content_sha256": PROMPT_CATALOG_V4_CONTENT_HASH,
                "prompt_geography_version": PROMPT_GEOGRAPHY_VERSION,
                "prompt_geography_hash": PROMPT_GEOGRAPHY_HASH,
            }
        )
    if isinstance(config.get("branch_treatment_selection"), Mapping):
        manifest["selection_provenance"] = deepcopy(dict(config["branch_treatment_selection"]))
    if "github_copilot" in platforms:
        try:
            manifest["github_copilot"] = GitHubCopilotConfig.from_mapping(config.get("github_copilot")).to_mapping()
        except (TypeError, ValueError) as exc:
            raise FrozenScopeError("github_copilot requires explicit valid source configuration") from exc
    elif config.get("github_copilot") is not None:
        raise FrozenScopeError("github_copilot configuration requires the github_copilot platform")
    rows = _prompt_rows(manifest)
    manifest["prompt_pack_hash"] = canonical_sha256(
        _prompt_hash_payload(manifest, rows)
    )
    manifest["scope_hash"] = scope_hash(manifest)
    return manifest, rows


def assert_frozen_scope(scope: Mapping) -> None:
    _validated_scope_prompt_rows(scope)


def _validated_scope_prompt_rows(
    scope: Mapping, *, rows: list[dict] | None = None
) -> list[dict]:
    """Validate scope integrity, reusing rows only inside the pure build path."""

    if not isinstance(scope, Mapping):
        raise FrozenScopeError("scope manifest must be a mapping")
    if str(scope.get("scope_kind", "")).startswith("client_research_"):
        from .client_research import validate
        return rows if rows is not None else validate(scope)
    if scope.get("scope_kind") == "diagnostic":
        from .diagnostic import validate
        # Only the pure build path supplies the exact rows just produced by freeze.
        # Public validation receives no rows and rebuilds the entire manifest.
        if rows is not None:
            return rows
        try:
            return validate(scope)
        except (ConfigurationError, ValueError, KeyError, TypeError) as exc:
            raise FrozenScopeError(str(exc)) from exc
    contract = _prompt_contract(scope.get("prompt_pack_version"))
    execution_policy_version = _clean(
        scope.get("execution_policy_version")
    )
    if execution_policy_version and (
        execution_policy_version
        != MODULE_MEASUREMENT_EXECUTION_POLICY_VERSION
    ):
        raise FrozenScopeError(
            "frozen execution policy is unsupported"
        )
    module_measurement = (
        execution_policy_version
        == MODULE_MEASUREMENT_EXECUTION_POLICY_VERSION
    )
    if module_measurement:
        if scope.get("prompt_pack_version") != PROMPT_PACK_V4_VERSION:
            raise FrozenScopeError(
                "frozen module measurement must use the v4 prompt catalog"
            )
        planned_runs = scope.get("planned_runs")
        if (
            isinstance(planned_runs, bool)
            or not isinstance(planned_runs, int)
            or not 1
            <= planned_runs
            <= MAX_MODULE_MEASUREMENT_RUNS
        ):
            raise FrozenScopeError(
                "frozen module measurement repetitions are invalid"
            )
        if scope.get("publication_eligible") is not False:
            raise FrozenScopeError(
                "module measurement scopes must be non-publishable"
            )
        if scope.get("test_mode") is not False:
            raise FrozenScopeError(
                "module measurement scopes cannot use Snapshot test mode"
            )
    scope_kind = scope.get("scope_kind", "clinic")
    if scope_kind == "area_treatments":
        if not module_measurement or "clinic" in scope:
            raise FrozenScopeError("area_treatments scope requires module measurement without a clinic")
    elif scope_kind == "clinic":
        if "clinic_archetype" in scope:
            raise FrozenScopeError("clinic scope cannot contain standalone clinic_archetype")
    else:
        raise FrozenScopeError("frozen scope_kind is unsupported")
    if scope.get("scope_version") != contract["scope_version"]:
        raise FrozenScopeError(
            f"scope_version must equal {contract['scope_version']!r} for "
            f"{scope.get('prompt_pack_version')}"
        )
    if scope.get("schema_version") != contract["scope_schema_version"]:
        raise FrozenScopeError(
            f"scope schema must equal {contract['scope_schema_version']!r}"
        )
    if list(scope.get("prompt_intents") or []) != list(contract["prompt_intents"]):
        raise FrozenScopeError(
            f"{scope.get('prompt_pack_version')} prompt intents do not match its frozen contract"
        )
    if scope.get("prompt_template_artifact_hash") != contract["template_artifact_hash"]:
        raise FrozenScopeError(
            f"{scope.get('prompt_pack_version')} template artifact hash does not match its contract"
        )
    if scope.get("evidence_policy_version") != contract["evidence_policy_version"]:
        raise FrozenScopeError("evidence policy version does not match the prompt contract")
    if scope.get("evidence_policy_artifact_hash") != contract["evidence_policy_artifact_hash"]:
        raise FrozenScopeError("evidence policy hash does not match the prompt contract")
    if scope.get("platform_name_reconciliation"):
        if scope.get("competitor_link_verification"):
            raise FrozenScopeError(
                "new scopes cannot enable retired conversational link follow-ups"
            )
        if (
            scope.get("name_reconciliation_policy_version")
            != NAME_RECONCILIATION_POLICY_VERSION
        ):
            raise FrozenScopeError("name-reconciliation policy version does not match")
        if (
            scope.get("name_reconciliation_artifact_hash")
            != NAME_RECONCILIATION_ARTIFACT_HASH
        ):
            raise FrozenScopeError("name-reconciliation artifact hash does not match")
    if scope.get("platform_data_modes") is not None:
        expected_data_modes = {
            platform: (
                (COPILOT_BROWSER_MEASUREMENT_MODE if (scope.get("platform_data_modes") or {}).get("copilot") == COPILOT_BROWSER_MEASUREMENT_MODE else COPILOT_DERIVED_MEASUREMENT_MODE)
                if platform == "copilot"
                else "github_copilot_sdk_source_gemini_parser_v1" if platform == "github_copilot"
                else BROWSER_MEASUREMENT_MODE
            )
            for platform in scope.get("platforms") or []
        }
        if dict(scope.get("platform_data_modes") or {}) != expected_data_modes:
            raise FrozenScopeError("platform data modes no longer match the frozen contract")
    if "github_copilot" in scope.get("platforms", ()):
        try:
            source_config = GitHubCopilotConfig.from_mapping(scope.get("github_copilot"))
        except ConfigurationError as exc:
            raise FrozenScopeError("frozen github_copilot configuration is invalid") from exc
        if dict(scope["github_copilot"]) != source_config.to_mapping():
            raise FrozenScopeError("frozen github_copilot configuration is incomplete")
    elif scope.get("github_copilot") is not None:
        raise FrozenScopeError("unexpected github_copilot source configuration")
    selected_areas = scope.get("areas")
    if selected_areas is not None:
        if not isinstance(selected_areas, list):
            raise FrozenScopeError("frozen areas must be a list")
        if not MIN_SELECTED_AREAS <= len(selected_areas) <= MAX_SELECTED_AREAS:
            raise FrozenScopeError(
                "Free Snapshot scope requires one or two selected areas (maximum 2)"
            )
    if scope.get("prompt_pack_version") == PROMPT_PACK_V4_VERSION:
        if not isinstance(selected_areas, list):
            raise FrozenScopeError("v4 frozen scope requires the areas matrix")
        if scope.get("prompt_catalog_version") != PROMPT_CATALOG_V4_VERSION:
            raise FrozenScopeError("v4 prompt catalog version no longer matches its contract")
        if scope.get("prompt_catalog_content_sha256") != PROMPT_CATALOG_V4_CONTENT_HASH:
            raise FrozenScopeError("v4 prompt catalog content hash no longer matches its contract")
        if scope.get("prompt_geography_version") != PROMPT_GEOGRAPHY_VERSION:
            raise FrozenScopeError("v4 prompt geography version no longer matches its contract")
        if scope.get("prompt_geography_hash") != PROMPT_GEOGRAPHY_HASH:
            raise FrozenScopeError("v4 prompt geography hash no longer matches its contract")
        frozen_platforms = list(scope.get("platforms") or [])
        if module_measurement:
            if (
                not frozen_platforms
                or len(frozen_platforms) != len(set(frozen_platforms))
                or any(
                    platform not in MODULE_MEASUREMENT_PLATFORMS
                    for platform in frozen_platforms
                )
            ):
                raise FrozenScopeError(
                    "module measurement platforms must be a non-empty "
                    "subset of the approved module platform set"
                )
        elif frozen_platforms != list(contract["platforms"]):
            raise FrozenScopeError(
                "v4 frozen platforms must exactly match the canonical "
                "four-platform contract"
            )
        archetype = _scope_archetype(scope)
        if scope_kind != "area_treatments" or scope.get("clinic_archetype") is not None:
            if not isinstance(archetype, Mapping) or archetype.get("confirmation_status") != "confirmed":
                raise FrozenScopeError("v4 frozen scope requires a confirmed clinic archetype")
            if any(not _clean(archetype.get(field)) for field in ("id", "singular", "plural")):
                raise FrozenScopeError("v4 frozen clinic archetype is incomplete")
        for selected in selected_areas:
            if scope_kind == "area_treatments":
                if not isinstance(selected, Mapping) or selected.get("validation_status") != "confirmed":
                    raise FrozenScopeError("area_treatments areas must remain confirmed")
                if (selected.get("catchment") or {}).get("confirmation_status") != "confirmed":
                    raise FrozenScopeError("area_treatments catchments must remain confirmed")
                if any(
                    treatment.get("commercial_relevance_status") != "confirmed"
                    for treatment in selected.get("treatments") or []
                ):
                    raise FrozenScopeError("area_treatments treatments must remain confirmed")
            geographies = selected.get("geographies") if isinstance(selected, Mapping) else None
            if not isinstance(geographies, Mapping):
                raise FrozenScopeError("v4 frozen areas require geographies")
            for tier in ("local", "greater_area", "city"):
                target = geographies.get(tier)
                if not isinstance(target, Mapping) or target.get("confirmation_status") != "confirmed":
                    raise FrozenScopeError(f"v4 {tier} geography must remain confirmed")
            catchment = selected.get("catchment") or {}
            local = geographies["local"]
            if local.get("id") != catchment.get("area_id") or local.get("name") != catchment.get("name"):
                raise FrozenScopeError("v4 local geography must mirror its catchment")
            if len(
                {
                    _clean(geographies[tier].get("country_code")).upper()
                    for tier in ("local", "greater_area", "city")
                }
            ) != 1:
                raise FrozenScopeError(
                    "v4 geography tiers no longer share one country code"
                )
            _assert_patient_facing_taxonomy_target(
                geographies["greater_area"], tier="greater_area"
            )
            _assert_patient_facing_taxonomy_target(geographies["city"], tier="city")
    if rows is None:
        rows = _prompt_rows(scope)
    expected_prompt_hash = canonical_sha256(_prompt_hash_payload(scope, rows))
    if scope.get("prompt_pack_hash") != expected_prompt_hash:
        raise FrozenScopeError("prompt pack hash no longer matches the frozen scope")
    expected_scope_hash = scope_hash(scope)
    if scope.get("scope_hash") != expected_scope_hash:
        raise FrozenScopeError("scope hash no longer matches the frozen manifest")
    return rows


def build_prompt_pack(scope: Mapping) -> dict:
    """Build the versioned report-eligible prompts for every treatment and area."""

    return _build_prompt_pack(scope, _validated_scope_prompt_rows(scope))


def _build_prompt_pack(scope: Mapping, rows: list[dict]) -> dict:
    """Build from the prompt rows that validated this scope in the same call."""

    payload = _prompt_hash_payload(scope, rows)
    computed_hash = canonical_sha256(payload)
    if computed_hash != scope["prompt_pack_hash"]:
        raise FrozenScopeError("rebuilt prompt pack differs from its frozen hash")
    return {
        **payload,
        "scope_hash": scope["scope_hash"],
        "prompt_pack_hash": computed_hash,
        "prompt_count": len(rows),
    }


_V4_OBSERVATION_PROMPT_FIELDS = (
    "prompt_pack_version",
    "catalog_version",
    "catalog_content_sha256",
    "basket_id",
    "basket_sequence",
    "semantic_slot_id",
    "semantic_prompt_id",
    "semantic_intent",
    "subject_kind",
    "subject_id",
    "subject_name",
    "subject",
    "treatment_name",
    "treatment_order",
    "clinic_archetype_id",
    "clinic_archetype_singular",
    "clinic_archetype_plural",
    "geography_tier",
    "target_geography",
    "origin",
    "origin_branches",
    "origin_branch_ids",
    "origin_area_ids",
    "collection_scope_id",
    "analysis_scope_id",
    "analysis_scope_ids",
    "evidence_cell_id",
    "evidence_cell_ids",
    "evidence_cells",
    "actual_variant",
    "naturalness_score",
    "slot_naturalness_score",
    "report_eligible",
)


def _build_v4_observation_plan(scope: Mapping, prompt_pack: Mapping) -> dict:
    contract = _prompt_contract(scope.get("prompt_pack_version"))
    rows: list[dict] = []
    for prompt in prompt_pack["prompts"]:
        platform = str(prompt["platform"])
        if platform not in scope["platforms"]:
            raise FrozenScopeError("v4 prompt platform is outside the frozen scope")
        repetitions = int(scope["github_copilot"]["repetitions"]) if platform == "github_copilot" else int(scope["planned_runs"])
        for run in range(1, repetitions + 1):
            identity = {
                "snapshot_id": scope["snapshot_id"],
                "scope_id": prompt["collection_scope_id"],
                "prompt_id": prompt["prompt_id"],
                "run": run,
                "platform": platform,
            }
            observation_id = f"obs-v4-{canonical_sha256(identity)[:24]}"
            target = prompt["target_geography"]
            row = {
                "observation_id": observation_id,
                "scope_id": prompt["collection_scope_id"],
                "scope_hash": scope["scope_hash"],
                "prompt_pack_hash": scope["prompt_pack_hash"],
                "prompt_id": prompt["prompt_id"],
                "prompt_intent": prompt["prompt_intent"],
                "prompt_text": prompt["text"],
                "prompt_text_sha256": prompt["prompt_text_sha256"],
                "branch_id": prompt.get("branch_id"),
                "branch_name": prompt.get("branch_name"),
                "treatment_id": prompt.get("treatment_id"),
                "area_id": target["id"],
                "run": run,
                "platform": platform,
                "model_or_surface": platform,
                "access_mode": "browser",
                "location_context": {
                    "country_code": target["country_code"],
                    "area_id": target["id"],
                    "area": target["name"],
                    "geography_tier": prompt["geography_tier"],
                    "target_geography": deepcopy(target),
                    "origin_branches": deepcopy(prompt["origin_branches"]),
                    "prompt_variant": deepcopy(prompt["actual_variant"]),
                },
                "started_at": None,
                "collected_at": None,
                "status": None,
                "raw_answer_reference": None,
                "raw_answer_sha256": None,
                "citations": [],
                "attempts": 0,
            }
            for field in _V4_OBSERVATION_PROMPT_FIELDS:
                row[field] = deepcopy(prompt.get(field))
            if platform == "github_copilot":
                row.update({"access_mode": "sdk", "model_or_surface": scope["github_copilot"]["model"], "github_copilot": deepcopy(scope["github_copilot"]), "parser_platform": "gemini", "submission_topology_version": "visibility_runner.submission_topology.v2"})
            rows.append(row)
    plan_hash_payload = {
        "schema_version": contract["observation_plan_schema_version"],
        "snapshot_id": scope["snapshot_id"],
        "scope_hash": scope["scope_hash"],
        "prompt_pack_hash": scope["prompt_pack_hash"],
        "prompt_pack_version": scope["prompt_pack_version"],
        "prompt_catalog_version": scope["prompt_catalog_version"],
        "prompt_catalog_content_sha256": scope["prompt_catalog_content_sha256"],
        "observations": rows,
    }
    return {
        **plan_hash_payload,
        "observation_plan_hash": canonical_sha256(plan_hash_payload),
        "planned_observations": len(rows),
    }


def build_observation_plan(scope: Mapping, prompt_pack: Mapping) -> dict:
    """Expand prompts to every configured run and platform.

    Planned rows use ``status=None`` and ``attempts=0``.  Once attempted, the
    normalizer replaces that null with one of the seven standardized statuses.
    """

    assert_frozen_scope(scope)
    if prompt_pack.get("prompt_pack_hash") != scope["prompt_pack_hash"]:
        raise FrozenScopeError("observation plan prompt hash does not match scope")
    if canonical_sha256(_prompt_hash_payload(scope, list(prompt_pack.get("prompts") or []))) != scope["prompt_pack_hash"]:
        raise FrozenScopeError("observation plan cannot use a mutated prompt pack")
    return _build_observation_plan(scope, prompt_pack)


def _build_observation_plan(scope: Mapping, prompt_pack: Mapping) -> dict:
    """Expand a scope and prompt pack already validated by the current call."""

    if str(scope.get("scope_kind", "")).startswith("client_research_"):
        from .client_research import observation_plan
        return observation_plan(scope, prompt_pack)
    if scope.get("scope_kind") == "diagnostic":
        from .diagnostic import observation_plan
        return observation_plan(scope, prompt_pack)
    if scope.get("prompt_pack_version") == PROMPT_PACK_V4_VERSION:
        return _build_v4_observation_plan(scope, prompt_pack)

    catchments = {
        str(row["catchment"]["area_id"]): row["catchment"]
        for row in area_scopes(scope)
    }
    if any(not catchments.get(str(prompt.get("area_id"))) for prompt in prompt_pack["prompts"]):
        raise FrozenScopeError("prompt references an area outside the frozen scope")
    return legacy_snapshot.observation_plan(
        scope, prompt_pack, catchments=catchments,
        schema_version=_prompt_contract(scope.get("prompt_pack_version"))[
            "observation_plan_schema_version"
        ],
    )


def freeze_snapshot(
    config: MeasurementConfig | Mapping,
    *,
    snapshot_id: Optional[str] = None,
    test_mode: bool = False,
    prompt_pack_version: Optional[str] = None,
) -> dict:
    scope, rows = _freeze_scope_and_prompt_rows(
        config,
        snapshot_id=snapshot_id,
        test_mode=test_mode,
        prompt_pack_version=prompt_pack_version,
    )
    _validated_scope_prompt_rows(scope, rows=rows)
    prompts = _build_prompt_pack(scope, rows)
    plan = _build_observation_plan(scope, prompts)
    return {
        "scope_manifest": scope,
        "snapshot_prompts": prompts,
        "observation_plan": plan,
    }


def preview_snapshot_counts(
    config: MeasurementConfig | Mapping,
    *,
    prompt_pack_version: str | None = None,
    test_mode: bool = False,
) -> dict:
    """Return the established preview counts without allocating profiles or tabs."""

    return summarize_measurement(
        config, prompt_pack_version=prompt_pack_version, test_mode=test_mode,
    )["counts"]


def summarize_measurement(
    config: MeasurementConfig | Mapping,
    *,
    prompt_pack_version: str | None = None,
    test_mode: bool = False,
) -> dict:
    """Summarize exact observation counts and initial sends without a RunPlan.

    ``counts`` preserves the Snapshot preview contract. ``submission_topology``
    uses the established browser/SDK send accounting, before retry allowances.
    No filesystem, browser, environment or profile allocation occurs here.
    """

    frozen = freeze_snapshot(
        config,
        snapshot_id="snapshot-preview-counts",
        test_mode=test_mode,
        prompt_pack_version=prompt_pack_version,
    )
    scope = frozen["scope_manifest"]
    prompts = frozen["snapshot_prompts"]["prompts"]
    observations = frozen["observation_plan"]["observations"]
    counts = _snapshot_counts(scope, prompts, observations)
    return {
        "counts": _preview_counts(scope, prompts, counts),
        "submission_topology": exact_chat_message_budget(
            observations, platform_data_modes=scope.get("platform_data_modes"),
        ),
    }


def _snapshot_counts(
    scope: Mapping,
    prompts: Sequence[Mapping],
    observations: Sequence[Mapping],
) -> RunCounts:
    platforms = tuple(scope["platforms"])
    platform_specific = (
        scope["prompt_pack_version"] == PROMPT_PACK_V4_VERSION
        or scope.get("scope_kind") == "diagnostic"
        or str(scope.get("scope_kind", "")).startswith("client_research_")
    )
    prompt_counts = {
        platform: (
            sum(row.get("platform") == platform for row in prompts)
            if platform_specific else len(prompts)
        )
        for platform in platforms
    }
    return RunCounts(
        platform_prompt_count=sum(prompt_counts.values()),
        semantic_prompt_count=(
            len({str(row.get("semantic_prompt_id") or "") for row in prompts})
            if platform_specific else len(prompts)
        ),
        planned_observation_count=len(observations),
        platform_prompt_counts=prompt_counts,
        platform_observation_counts={
            platform: sum(row.get("platform") == platform for row in observations)
            for platform in platforms
        },
    )


def _preview_counts(scope: Mapping, prompts: Sequence[Mapping], counts: RunCounts) -> dict:
    if scope["prompt_pack_version"] != PROMPT_PACK_V4_VERSION:
        platform_count = len(scope["platforms"])
        return {
            "prompt_pack_version": scope["prompt_pack_version"],
            "platform_prompt_count": counts.platform_prompt_count,
            "semantic_prompt_count": counts.semantic_prompt_count,
            "prompts_per_platform": len(prompts),
            "platform_prompt_counts": dict(counts.platform_prompt_counts),
            "prompts_by_basket": {},
            "prompt_details_by_basket": {},
            "planned_runs": int(scope["planned_runs"]),
            "platform_count": platform_count,
            "planned_observation_count": counts.planned_observation_count,
            **({
                "github_copilot_observation_count": len(prompts),
                "sdk_source_messages": len(prompts),
                "source_parser_messages": len(prompts),
            } if scope.get("scope_kind") == "diagnostic" and "github_copilot" in scope["platforms"] else {}),
            "deduplicated_origin_assignments": 0,
        }
    platform_prompt_counts = dict(counts.platform_prompt_counts)
    baskets: dict[str, int] = {}
    basket_details: dict[str, dict] = {}
    for basket_id in ("treatment_local", "treatment_expanded", "clinic_archetype"):
        basket_rows = [row for row in prompts if row.get("basket_id") == basket_id]
        semantic_basket_count = len(
            {row.get("semantic_prompt_id") for row in basket_rows}
        )
        baskets[basket_id] = semantic_basket_count
        basket_details[basket_id] = {
            "platform_prompt_count": len(basket_rows),
            "semantic_prompt_count": semantic_basket_count,
            "platform_prompt_counts": {
                platform: sum(row.get("platform") == platform for row in basket_rows)
                for platform in scope["platforms"]
            },
        }
    return {
        "prompt_pack_version": scope["prompt_pack_version"],
        "platform_prompt_count": counts.platform_prompt_count,
        "semantic_prompt_count": counts.semantic_prompt_count,
        "prompts_per_platform": next(iter(platform_prompt_counts.values()), 0),
        "platform_prompt_counts": platform_prompt_counts,
        "prompts_by_basket": baskets,
        "prompt_details_by_basket": basket_details,
        "planned_runs": int(scope["planned_runs"]),
        "platform_count": len(scope["platforms"]),
        "planned_observation_count": counts.planned_observation_count,
        **({
            "github_copilot_observation_count": counts.platform_observation_counts["github_copilot"],
            "sdk_source_messages": counts.platform_observation_counts["github_copilot"],
            "source_parser_messages": counts.platform_observation_counts["github_copilot"],
        } if "github_copilot" in scope["platforms"] else {}),
        "deduplicated_origin_assignments": sum(
            max(0, len(row.get("origin_branches") or []) - 1) for row in prompts
        ),
    }


def verify_frozen_snapshot(
    scope: Mapping,
    prompt_pack: Mapping,
    observation_plan: Mapping,
) -> list[str]:
    """Return integrity errors; an empty list means all three artifacts agree."""

    errors: list[str] = []
    try:
        assert_frozen_scope(scope)
    except FrozenScopeError as exc:
        errors.append(str(exc))
        return errors
    try:
        expected_pack = build_prompt_pack(scope)
        if prompt_pack != expected_pack:
            errors.append("snapshot prompt pack differs from the frozen pack")
    except FrozenScopeError as exc:
        errors.append(str(exc))
    try:
        expected_plan = build_observation_plan(scope, prompt_pack)
        if observation_plan != expected_plan:
            errors.append("observation plan differs from the frozen plan")
    except FrozenScopeError as exc:
        errors.append(str(exc))
    return errors


def _measurement_config(
    value: MeasurementConfig | Mapping[str, object],
) -> MeasurementConfig:
    if isinstance(value, MeasurementConfig):
        return value
    if not isinstance(value, Mapping):
        raise FrozenScopeError(
            "measurement config must be a MeasurementConfig or mapping"
        )
    try:
        return MeasurementConfig.from_mapping(value)
    except (TypeError, ValueError) as exc:
        raise FrozenScopeError(str(exc)) from exc


def _profile_selection(
    *,
    profile_config: ProfileConfig | None,
    selected_profile_ids: Sequence[str] | None,
    profile_manifest_sha256: str | None,
    selected_profiles: Sequence[ProfileIdentity],
) -> tuple[tuple[str, ...], str, tuple[ProfileIdentity, ...]]:
    if profile_config is not None:
        if not isinstance(profile_config, ProfileConfig):
            raise FrozenScopeError(
                "profile_config must be a ProfileConfig"
            )
        if (
            selected_profile_ids is not None
            or profile_manifest_sha256 is not None
            or tuple(selected_profiles)
        ):
            raise FrozenScopeError(
                "profile_config cannot be combined with explicit profile "
                "selection arguments"
            )
        return (
            profile_config.selected_profile_ids,
            profile_config.manifest_sha256,
            profile_config.selected_profiles,
        )

    if selected_profile_ids is None:
        raise FrozenScopeError(
            "selected_profile_ids or profile_config is required"
        )
    if isinstance(selected_profile_ids, (str, bytes)):
        raise FrozenScopeError("selected_profile_ids must be a sequence")
    identifiers = tuple(str(value or "").strip() for value in selected_profile_ids)
    if not identifiers or any(not value for value in identifiers):
        raise FrozenScopeError("selected_profile_ids cannot be empty")
    if profile_manifest_sha256 is None:
        raise FrozenScopeError("profile_manifest_sha256 is required")
    identities = tuple(selected_profiles)
    if any(not isinstance(value, ProfileIdentity) for value in identities):
        raise FrozenScopeError(
            "selected_profiles must contain ProfileIdentity values"
        )
    return identifiers, str(profile_manifest_sha256), identities


def _packet_id(
    *,
    run_id: str,
    platform: str,
    profile_id: str,
    observation_ids: tuple[str, ...],
    stage: str = "measurement",
) -> str:
    payload = {
        "run_id": run_id,
        "stage": stage,
        "platform": platform,
        "profile_id": profile_id,
        "observation_ids": list(observation_ids),
    }
    return f"packet-{canonical_sha256(payload)[:24]}"


def _split_bucket(
    bucket: Sequence[Mapping[str, object]],
    tabs_per_provider: int,
) -> tuple[tuple[Mapping[str, object], ...], ...]:
    """Split one (profile, platform) bucket round-robin into measurement group groups.

    Each returned group later executes in its own owned tab; the count is at
    most ``tabs_per_provider`` and never exceeds the bucket size.  Membership
    is a pure function of the bucket order, so the plan stays deterministic.
    """

    if tabs_per_provider < 1:
        raise FrozenScopeError("tabs_per_provider must be a positive integer")
    group_count = min(int(tabs_per_provider), len(bucket))
    groups: list[list[Mapping[str, object]]] = [
        [] for _ in range(group_count)
    ]
    for index, row in enumerate(bucket):
        groups[index % group_count].append(row)
    return tuple(tuple(group) for group in groups)


def _build_packets(
    *,
    run_id: str,
    platforms: Sequence[object],
    profile_ids: tuple[str, ...],
    observations: Sequence[Mapping[str, object]],
    platform_data_modes: Mapping[str, object],
    retry_policy: RetryPolicy,
    tabs_per_provider: int = 1,
) -> tuple[PacketPlan, ...]:
    measurement_packets: list[PacketPlan] = []
    extraction_packets: list[PacketPlan] = []
    for configured_platform in platforms:
        platform = str(
            getattr(configured_platform, "value", configured_platform)
        )
        platform_rows = tuple(
            row
            for row in observations
            if str(row.get("platform") or "") == platform
        )
        buckets: list[list[Mapping[str, object]]] = [
            [] for _ in profile_ids
        ]
        for index, row in enumerate(platform_rows):
            buckets[profile_ids.index(row["profile_id"]) if row.get("client_research_capability") else index % len(profile_ids)].append(row)
        for profile_id, profile_bucket in zip(profile_ids, buckets):
            if not profile_bucket:
                continue
            # One measurement group per owned tab.  A Google AI Overview measurement group and its
            # conditional Gemini name listing measurement group keep identical membership
            # so the split name listing runs on the matching Gemini tab.
            for bucket in _split_bucket(profile_bucket, tabs_per_provider):
                observation_ids = tuple(
                    str(row["observation_id"]) for row in bucket
                )
                measurement_packets.append(
                    PacketPlan(
                        packet_id=_packet_id(
                            run_id=run_id,
                            platform=platform,
                            profile_id=profile_id,
                            observation_ids=observation_ids,
                        ),
                        platform=platform,
                        profile_id=profile_id,
                        observation_ids=observation_ids,
                        submission_budget=build_measurement_packet_budget(
                            bucket,
                            platform_data_modes=platform_data_modes,
                            retry_policy=retry_policy,
                        ),
                        kind=PacketKind.SOURCE_PARSER if platform == "github_copilot" else PacketKind.MEASUREMENT,
                    )
                )
                if platform == Platform.GOOGLE_AI_OVERVIEW.value:
                    extraction_packets.append(
                        PacketPlan(
                            packet_id=_packet_id(
                                run_id=run_id,
                                platform=Platform.GEMINI.value,
                                profile_id=profile_id,
                                observation_ids=observation_ids,
                                stage="gaio_extraction",
                            ),
                            platform=Platform.GEMINI,
                            profile_id=profile_id,
                            observation_ids=observation_ids,
                            submission_budget=(
                                build_gaio_extraction_packet_budget(
                                    bucket,
                                    retry_policy=retry_policy,
                                )
                            ),
                            kind=PacketKind.GAIO_EXTRACTION,
                            conditional_on="gaio_shown_v1",
                        )
                    )
    return tuple((*measurement_packets, *extraction_packets))


def build_run_plan(
    config: MeasurementConfig | Mapping[str, object],
    *,
    run_id: str,
    profile_config: ProfileConfig | None = None,
    selected_profile_ids: Sequence[str] | None = None,
    profile_manifest_sha256: str | None = None,
    selected_profiles: Sequence[ProfileIdentity] = (),
    retry_policy: RetryPolicy | None = None,
    tabs_per_provider: int = 1,
) -> RunPlan:
    """Build one deterministic immutable authorized run plan.

    No file, browser, environment, or website access occurs here.  Profile
    identities and the already-computed manifest hash are treated as exact
    application inputs.  Prompt measurement membership is frozen round-robin within
    each platform's ordered prompt measurement stream and is never reconstructed at
    execution time.  ``tabs_per_provider`` freezes how many measurement groups, and so
    how many simultaneous owned tabs, one (profile, platform) pair receives.
    """

    from ..client_research import ClientIdentificationConfig, ClientRecommendationConfig
    if isinstance(config, (ClientIdentificationConfig, ClientRecommendationConfig)):
        config = config.to_mapping()
    measurement = _measurement_config(config)
    if isinstance(tabs_per_provider, bool) or not isinstance(
        tabs_per_provider, int
    ) or tabs_per_provider < 1:
        raise FrozenScopeError("tabs_per_provider must be a positive integer")
    effective_retry_policy = (
        retry_policy if retry_policy is not None else RetryPolicy()
    )
    if not isinstance(effective_retry_policy, RetryPolicy):
        raise FrozenScopeError("retry_policy must be a RetryPolicy")
    (
        profile_ids,
        manifest_sha256,
        profile_identities,
    ) = _profile_selection(
        profile_config=profile_config,
        selected_profile_ids=selected_profile_ids,
        profile_manifest_sha256=profile_manifest_sha256,
        selected_profiles=selected_profiles,
    )
    frozen = freeze_snapshot(measurement, snapshot_id=run_id)
    if measurement.scope_kind.startswith("client_research_"):
        from .client_research import bind_profiles
        frozen = bind_profiles(frozen, profile_ids)
    scope = frozen["scope_manifest"]
    prompt_rows = tuple(frozen["snapshot_prompts"]["prompts"])
    observation_rows = tuple(frozen["observation_plan"]["observations"])
    if any(not isinstance(row, Mapping) for row in observation_rows):
        raise FrozenScopeError(
            "the frozen observation plan contains a non-object row"
        )

    counts = _snapshot_counts(scope, prompt_rows, observation_rows)
    platform_data_modes = scope.get("platform_data_modes")
    if not isinstance(platform_data_modes, Mapping):
        raise FrozenScopeError(
            "the frozen scope lacks platform_data_modes"
        )
    packets = _build_packets(
        run_id=run_id,
        platforms=measurement.platforms,
        profile_ids=profile_ids,
        observations=observation_rows,
        platform_data_modes=platform_data_modes,
        retry_policy=effective_retry_policy,
        tabs_per_provider=tabs_per_provider,
    )
    budget = build_submission_budget(
        observation_rows,
        platform_data_modes=platform_data_modes,
        retry_policy=effective_retry_policy,
    )
    packet_budget = sum(
        (packet.submission_budget for packet in packets),
        start=budget.zero(),
    )
    if packet_budget != budget:
        raise FrozenScopeError(
            "packet submission budgets do not sum to the run budget"
        )
    observation_ids = tuple(
        str(row["observation_id"]) for row in observation_rows
    )
    return RunPlan._build_hashed(
        run_id=run_id,
        measurement_config=measurement,
        packets=packets,
        observation_ids=observation_ids,
        submission_budget=budget,
        selected_profile_ids=profile_ids,
        selected_profiles=profile_identities,
        profile_manifest_sha256=manifest_sha256,
        retry_policy=effective_retry_policy,
        counts=counts,
        scope_manifest=scope,
        snapshot_prompts=frozen["snapshot_prompts"],
        observation_plan=frozen["observation_plan"],
        prompt_catalog_sha256=(scope["prompt_pack_hash"] if measurement.scope_kind == "diagnostic" or measurement.scope_kind.startswith("client_research_") else PROMPT_CATALOG_V4_CONTENT_HASH),
        geography_version=("" if measurement.scope_kind == "diagnostic" or measurement.scope_kind.startswith("client_research_") else PROMPT_GEOGRAPHY_VERSION),
        geography_sha256=("" if measurement.scope_kind == "diagnostic" or measurement.scope_kind.startswith("client_research_") else PROMPT_GEOGRAPHY_HASH),
    )


# Compact spelling for internal callers.
build_plan = build_run_plan
