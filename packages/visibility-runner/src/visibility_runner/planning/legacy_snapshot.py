"""Historical flat/matrix Snapshot construction; no new execution contracts.

The current planner supplies its already resolved template/area data. Keeping
these loops here leaves current v4 and diagnostic construction on direct paths.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Mapping, Sequence

from .hashing import canonical_sha256


_SPACE_RE = re.compile(r"\s+")


def _clean(value: object) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()


def _variants_by_type(catchment: Mapping) -> dict[str, str]:
    return {
        str(row.get("type")): _clean(row.get("value"))
        for row in catchment.get("variants") or []
        if isinstance(row, Mapping) and _clean(row.get("type")) and _clean(row.get("value"))
    }


def prompt_rows(scope: Mapping, *, templates: Sequence[Mapping], areas: Sequence[Mapping]) -> list[dict]:
    if not isinstance(scope.get("areas"), list):
        catchment = scope["catchment"]
        variants = _variants_by_type(catchment)
        area = variants.get("area") or _clean(catchment.get("name"))
        legacy_rows: list[dict] = []
        for treatment_order, treatment in enumerate(scope["treatments"], 1):
            for template in templates:
                sequence = int(template["sequence"])
                requested_variant = str(template["variant_type"])
                actual_value = variants.get(requested_variant) or area
                actual_type = requested_variant if variants.get(requested_variant) else "area"
                text = str(template["template"]).format(
                    treatment=treatment["name"],
                    area=area,
                    station_or_area=variants.get("station") or area,
                    postcode_or_area=variants.get("postcode") or area,
                    landmark_or_area=variants.get("landmark") or area,
                )
                prompt_id = f"{treatment['id']}:p{sequence:02d}"
                legacy_rows.append(
                    {
                        "id": prompt_id,
                        "prompt_id": prompt_id,
                        "text": text,
                        "treatment_id": treatment["id"],
                        "treatment_name": treatment["name"],
                        "treatment_order": treatment_order,
                        "area_id": catchment["area_id"],
                        "prompt_intent": template["intent"],
                        "intent": template["intent"],
                        "prompt_family": f"p{sequence:02d}",
                        "requested_variant_type": requested_variant,
                        "actual_variant": {
                            "type": actual_type,
                            "value": actual_value,
                            "used_fallback": actual_type != requested_variant,
                        },
                        "report_eligible": True,
                    }
                )
        return legacy_rows

    rows: list[dict] = []
    matrix = areas
    for area_order, area_scope in enumerate(matrix, 1):
        catchment = area_scope["catchment"]
        variants = _variants_by_type(catchment)
        area = variants.get("area") or _clean(catchment.get("name"))
        branch = area_scope.get("branch") or {}
        for treatment_order, treatment in enumerate(area_scope["treatments"], 1):
            for template in templates:
                sequence = int(template["sequence"])
                requested_variant = str(template["variant_type"])
                actual_value = variants.get(requested_variant) or area
                actual_type = requested_variant if variants.get(requested_variant) else "area"
                text = str(template["template"]).format(
                    treatment=treatment["name"],
                    area=area,
                    station_or_area=variants.get("station") or area,
                    postcode_or_area=variants.get("postcode") or area,
                    landmark_or_area=variants.get("landmark") or area,
                )
                prompt_id = f"{treatment['id']}:{catchment['area_id']}:p{sequence:02d}"
                rows.append(
                    {
                        "id": prompt_id,
                        "prompt_id": prompt_id,
                        "text": text,
                        "branch_id": branch.get("id"),
                        "branch_name": branch.get("name"),
                        "area_id": catchment["area_id"],
                        "area_name": catchment["name"],
                        "area_order": area_order,
                        "treatment_id": treatment["id"],
                        "treatment_name": treatment["name"],
                        "treatment_order": treatment_order,
                        "prompt_intent": template["intent"],
                        "intent": template["intent"],
                        "prompt_family": f"p{sequence:02d}",
                        "requested_variant_type": requested_variant,
                        "actual_variant": {
                            "type": actual_type,
                            "value": actual_value,
                            "used_fallback": actual_type != requested_variant,
                        },
                        "report_eligible": True,
                    }
                )
    return rows


def observation_plan(
    scope: Mapping, prompt_pack: Mapping, *, catchments: Mapping, schema_version: str,
) -> dict:
    rows: list[dict] = []
    for prompt in prompt_pack["prompts"]:
        catchment = catchments[str(prompt.get("area_id"))]
        for platform in scope["platforms"]:
            scope_id = f"{prompt['treatment_id']}:{prompt['area_id']}:{platform}"
            for run in range(1, int(scope["planned_runs"]) + 1):
                identity = {
                    "snapshot_id": scope["snapshot_id"],
                    "scope_id": scope_id,
                    "prompt_id": prompt["prompt_id"],
                    "run": run,
                    "platform": platform,
                }
                observation_id = f"obs-{canonical_sha256(identity)[:20]}"
                rows.append(
                    {
                        "observation_id": observation_id,
                        "scope_id": scope_id,
                        "scope_hash": scope["scope_hash"],
                        "prompt_pack_hash": scope["prompt_pack_hash"],
                        "prompt_id": prompt["prompt_id"],
                        "prompt_intent": prompt["prompt_intent"],
                        **(
                            {
                                "branch_id": prompt.get("branch_id"),
                                "branch_name": prompt.get("branch_name"),
                            }
                            if isinstance(scope.get("areas"), list)
                            else {}
                        ),
                        "treatment_id": prompt["treatment_id"],
                        "area_id": catchment["area_id"],
                        "run": run,
                        "platform": platform,
                        "model_or_surface": platform,
                        "access_mode": "browser",
                        "location_context": {
                            "country_code": catchment["country_code"],
                            "area_id": catchment["area_id"],
                            "area": catchment["name"],
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
                )
    plan_hash_payload = {
        "schema_version": schema_version,
        "snapshot_id": scope["snapshot_id"],
        "scope_hash": scope["scope_hash"],
        "prompt_pack_hash": scope["prompt_pack_hash"],
        "observations": rows,
    }
    return {
        **plan_hash_payload,
        "observation_plan_hash": canonical_sha256(plan_hash_payload),
        "planned_observations": len(rows),
    }
