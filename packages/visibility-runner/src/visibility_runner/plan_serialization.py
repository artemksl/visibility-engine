"""Load frozen plans without rebuilding prompts or accessing runtime resources."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import MISSING, fields
import math

from .config import MeasurementConfig, Platform, ProfileIdentity, RetryPolicy
from .contract_primitives import RUN_PLAN_SCHEMA_VERSION, canonical_sha256, json_value, text_sha256
from .errors import ContractError, IntegrityError
from .models import PacketKind, PacketPlan, RunCounts, RunPlan, SubmissionBudget, _freeze
from .planning.count_allowed_sends import (
    build_gaio_extraction_packet_budget,
    build_measurement_packet_budget,
    build_submission_budget,
)
from .planning.catalog import PROMPT_PACK_CONTRACTS


def _object(value, name, *, model=None):
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be an object")
    if model is not None:
        known = {field.name for field in fields(model)}
        unknown = set(value) - known
        required = {field.name for field in fields(model) if field.default is MISSING and field.default_factory is MISSING}
        missing = required - set(value)
        if unknown or missing:
            raise ContractError(f"{name} has unsupported or missing keys: {sorted(unknown | missing)}")
    return value


def _sequence(value, name):
    if not isinstance(value, (list, tuple)):
        raise ContractError(f"{name} must be a list")
    return value


def _json_types(value, name="plan"):
    """Reject lossy canonicalization such as integer keys, sets and NaN."""
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ContractError(f"{name} keys must be strings")
        for key, item in value.items():
            _json_types(item, f"{name}.{key}")
    elif isinstance(value, (list, tuple)):
        for item in value:
            _json_types(item, f"{name}[]")
    elif value is not None and type(value) not in (str, int, float, bool):
        raise ContractError(f"{name} must contain JSON values")
    elif type(value) is float and not math.isfinite(value):
        raise ContractError(f"{name} must contain finite numbers")


def _field_types(value, expected, name):
    """Check wire types before model constructors can normalize values."""
    if isinstance(expected, Mapping):
        _object(value, name)
        for key in value.keys() & expected.keys():
            _field_types(value[key], expected[key], f"{name}.{key}")
    elif isinstance(expected, (list, tuple)):
        _sequence(value, name)
        for item, normalized in zip(value, expected):
            _field_types(item, normalized, f"{name}[]")
    elif expected is not None:
        allowed = (int, float) if type(expected) is float else (type(expected),)
        if type(value) not in allowed:
            raise ContractError(f"{name} has an invalid value type")


def load_run_plan(value: Mapping[str, object]) -> RunPlan:
    """Validate and load an approved plan from its serialized JSON mapping.

    This is a pure operation: it verifies the supplied hash and relationships,
    then constructs immutable public models. It never renders prompts, reads
    configuration files or authorizes execution. Older omitted optional fields
    retain their established defaults and the original serialized plan hash.
    """
    _object(value, "run plan", model=RunPlan)
    _json_types(value)
    supplied = value.get("plan_sha256")
    if type(supplied) is not str or supplied != canonical_sha256({key: item for key, item in value.items() if key != "plan_sha256"}):
        raise IntegrityError("run plan hash does not match its contents")
    plan = _run_plan_from_mapping(value)
    if json_value(plan) != json_value(value):
        # Preserve omitted historical defaults outside the model's wire/hash
        # fields. Copy and freeze, so neither the caller nor returned JSON can
        # alter the original verified representation.
        object.__setattr__(plan, "_verified_wire_projection", _freeze(value))
    return plan


def _run_plan_from_mapping(value: Mapping[str, object]) -> RunPlan:
    """Historical parse-only helper; public/execution loads verify raw hashes."""
    config = MeasurementConfig.from_mapping(value["measurement_config"])
    _field_types(value["measurement_config"], json_value(config), "measurement_config")
    retry = RetryPolicy.from_mapping(value["retry_policy"]) if "retry_policy" in value else RetryPolicy.disabled()
    if "retry_policy" in value:
        _field_types(value["retry_policy"], json_value(retry), "retry_policy")
    identities = tuple(ProfileIdentity.from_mapping(_object(row, "selected profile")) for row in _sequence(value.get("selected_profiles") or (), "selected_profiles"))
    for row, identity in zip(value.get("selected_profiles") or (), identities):
        _field_types(row, json_value(identity), "selected profile")
    packets = []
    for row in _sequence(value["packets"], "packets"):
        _object(row, "packet", model=PacketPlan)
        packet = PacketPlan(
            packet_id=row["packet_id"], platform=row["platform"], profile_id=row["profile_id"],
            observation_ids=tuple(_sequence(row["observation_ids"], "packet observation_ids")),
            submission_budget=SubmissionBudget.from_mapping(row["submission_budget"]),
            kind=row.get("kind", PacketKind.MEASUREMENT), run_ordinal=row.get("run_ordinal"), conditional_on=row.get("conditional_on"),
        )
        _field_types(row, json_value(packet), "packet")
        packets.append(packet)
    counts = None
    if value.get("counts") is not None:
        row = _object(value["counts"], "counts", model=RunCounts)
        counts = RunCounts(**dict(row))
    plan = RunPlan(
        run_id=value["run_id"], measurement_config=config, packets=tuple(packets),
        observation_ids=tuple(_sequence(value["observation_ids"], "observation_ids")),
        submission_budget=SubmissionBudget.from_mapping(value["submission_budget"]),
        selected_profile_ids=tuple(_sequence(value["selected_profile_ids"], "selected_profile_ids")),
        selected_profiles=identities, profile_manifest_sha256=value["profile_manifest_sha256"], retry_policy=retry, counts=counts,
        scope_manifest=value.get("scope_manifest") or {}, snapshot_prompts=value.get("snapshot_prompts") or {}, observation_plan=value.get("observation_plan") or {},
        prompt_catalog_sha256=value.get("prompt_catalog_sha256") or "", geography_version=value.get("geography_version") or "", geography_sha256=value.get("geography_sha256") or "",
        plan_sha256=value["plan_sha256"], schema_version=value.get("schema_version", RUN_PLAN_SCHEMA_VERSION),
    )
    _field_types(value, json_value(plan), "run plan")
    validate_run_plan(plan, check_hash=False)
    return plan


def serialize_run_plan(plan: RunPlan) -> dict[str, object]:
    """Return a fresh JSON copy of the exact current or loaded sealed plan."""
    validate_run_plan(plan, check_hash=False)
    return _sealed_projection(plan)


def _sealed_projection(plan):
    original = getattr(plan, "_verified_wire_projection", None)
    payload = json_value(plan if original is None else original)
    _json_types(payload)
    supplied = payload.get("plan_sha256")
    if supplied != plan.plan_sha256 or not supplied or canonical_sha256({key: item for key, item in payload.items() if key != "plan_sha256"}) != supplied:
        raise IntegrityError("run plan hash does not match its contents")
    if original is not None and _run_plan_from_mapping(original) != plan:
        raise IntegrityError("original serialized plan no longer binds to its loaded model")
    return payload


def _artifact_hash(value, name, excluded):
    if name in value and value[name] != canonical_sha256({key: item for key, item in value.items() if key not in excluded}):
        raise IntegrityError(f"{name} does not match its frozen artifact")


def _validate_nested_artifacts(plan):
    scope, prompts, observations = plan.scope_manifest, plan.snapshot_prompts, plan.observation_plan
    version = scope.get("prompt_pack_version")
    if version is not None:
        if str(scope.get("scope_kind", "")).startswith("client_research_"):
            from .planning.client_research import SCHEMAS
            schemas = SCHEMAS
        elif scope.get("scope_kind") == "diagnostic":
            schemas = ("visibility_runner.diagnostic_scope.v1", "visibility_runner.diagnostic_prompts.v1", "visibility_runner.diagnostic_observations.v1")
        else:
            contract = PROMPT_PACK_CONTRACTS.get(version)
            if contract is None:
                raise IntegrityError("unsupported frozen prompt contract")
            schemas = tuple(contract[name] for name in ("scope_schema_version", "schema_version", "observation_plan_schema_version"))
        for artifact, schema in zip((scope, prompts, observations), schemas):
            if "schema_version" in artifact and artifact["schema_version"] != schema:
                raise IntegrityError("frozen artifact schema differs from its prompt contract")
    for name, expected in (
        ("planned_runs", plan.measurement_config.repetitions),
        ("platforms", tuple(platform.value for platform in plan.measurement_config.platforms)),
        ("outside_in", plan.measurement_config.outside_in),
        ("execution_policy_version", plan.measurement_config.execution_policy_version),
    ):
        if name in scope:
            actual = tuple(scope[name]) if name == "platforms" else scope[name]
            if type(actual) is not type(expected) or actual != expected:
                raise IntegrityError(f"frozen scope {name} differs from measurement configuration")
    if plan.measurement_config.scope_kind.startswith("client_research_"):
        from .planning.client_research import freeze, prompt_hash_payload, observation_plan, bind_profiles
        expected_scope, expected_rows = freeze(plan.measurement_config, snapshot_id=plan.run_id)
        expected_prompts = {**prompt_hash_payload(expected_scope, expected_rows), "scope_hash":expected_scope["scope_hash"],
                            "prompt_pack_hash":expected_scope["prompt_pack_hash"],"prompt_count":len(expected_rows)}
        expected = bind_profiles({"scope_manifest":expected_scope,"snapshot_prompts":expected_prompts,
                                  "observation_plan":observation_plan(expected_scope, expected_prompts)}, plan.selected_profile_ids)
        if any(json_value(actual) != expected[key] for key,actual in (("scope_manifest",scope),("snapshot_prompts",prompts),("observation_plan",observations))):
            raise IntegrityError("client research frozen inputs or role/profile bindings differ from configuration")
    source_config = plan.measurement_config.github_copilot
    if "github_copilot" in scope and scope["github_copilot"] != json_value(source_config):
        raise IntegrityError("frozen scope source configuration differs from measurement configuration")
    for outer, nested in (("prompt_catalog_sha256", "prompt_catalog_content_sha256"), ("geography_sha256", "prompt_geography_hash"), ("geography_version", "prompt_geography_version")):
        if getattr(plan, outer) and nested in scope and getattr(plan, outer) != scope[nested]:
            raise IntegrityError(f"{outer} differs from frozen scope")
    _artifact_hash(scope, "scope_hash", {"scope_hash"})
    _artifact_hash(prompts, "prompt_pack_hash", {"scope_hash", "prompt_pack_hash", "prompt_count"})
    _artifact_hash(observations, "observation_plan_hash", {"observation_plan_hash", "planned_observations"})
    for name in ("snapshot_id", "scope_hash", "prompt_pack_hash", "prompt_pack_version"):
        values = [artifact[name] for artifact in (scope, prompts, observations) if name in artifact]
        if values and any(value != values[0] for value in values):
            raise IntegrityError(f"frozen artifacts disagree on {name}")
    rows = observations["observations"]
    for row in rows:
        for name in ("observation_id", "platform", "prompt_id", "prompt_text"):
            if name in row and type(row[name]) is not str:
                raise ContractError(f"observation {name} must be a string")
        for name in ("run", "run_ordinal", "diagnostic_index"):
            if name in row and (type(row[name]) is not int or row[name] < 1):
                raise ContractError(f"observation {name} must be a positive integer")
        if "github_copilot" in row and row["github_copilot"] != json_value(source_config):
            raise IntegrityError("observation source configuration differs from measurement configuration")
    if "planned_observations" in observations and (type(observations["planned_observations"]) is not int or observations["planned_observations"] != len(rows)):
        raise IntegrityError("planned_observations differs from frozen observations")
    if plan.counts is not None:
        actual = Counter(str(row.get("platform") or "") for row in rows)
        if any(actual[key] != value for key, value in plan.counts.platform_observation_counts.items()) or any(key not in plan.counts.platform_observation_counts for key in actual):
            raise IntegrityError("platform observation counts differ from frozen observations")
    prompt_rows = prompts.get("prompts")
    if prompt_rows is None:
        return  # Historical execution-only plans did not carry a prompt pack.
    _sequence(prompt_rows, "snapshot prompts")
    by_id = {}
    for row in prompt_rows:
        _object(row, "snapshot prompt")
        identity = row.get("prompt_id")
        if type(identity) is not str or not identity or identity in by_id:
            raise IntegrityError("snapshot prompt identities must be exact and unique")
        by_id[identity] = row
        text = row.get("text", row.get("prompt_text"))
        if "prompt_text_sha256" in row and (type(text) is not str or row["prompt_text_sha256"] != text_sha256(text)):
            raise IntegrityError("snapshot prompt hash does not match its exact text")
    if "prompt_count" in prompts and (type(prompts["prompt_count"]) is not int or prompts["prompt_count"] != len(prompt_rows)):
        raise IntegrityError("prompt_count differs from frozen prompt rows")
    for row in rows:
        prompt = by_id.get(row.get("prompt_id"))
        if prompt is None:
            raise IntegrityError("observation references an unknown frozen prompt")
        for name in ("scope_hash", "prompt_pack_hash"):
            if name in row and row[name] != observations.get(name):
                raise IntegrityError(f"observation differs from frozen {name}")
        text = row.get("prompt_text")
        if text is not None and text != prompt.get("text", prompt.get("prompt_text")):
            raise IntegrityError("observation text differs from frozen prompt")
        if "prompt_text_sha256" in row and (type(text) is not str or row["prompt_text_sha256"] != text_sha256(text)):
            raise IntegrityError("observation prompt hash does not match its exact text")
        if "platform" in prompt and row["platform"] != prompt["platform"]:
            raise IntegrityError("observation platform differs from frozen prompt")
    if plan.counts is not None:
        actual = Counter(str(row.get("platform") or "") for row in prompt_rows)
        if plan.counts.platform_prompt_count != len(prompt_rows) or any(actual[key] != value for key, value in plan.counts.platform_prompt_counts.items()) or any(key not in plan.counts.platform_prompt_counts for key in actual):
            raise IntegrityError("platform prompt counts differ from frozen prompt rows")
        semantics = {str(row.get("semantic_prompt_id") or "") for row in prompt_rows}
        if plan.counts.semantic_prompt_count != len(semantics):
            raise IntegrityError("semantic prompt count differs from frozen prompt rows")


def validate_run_plan(plan: RunPlan, *, check_hash: bool = True) -> None:
    """Check frozen topology independently of runtime profile/account binding."""
    if not isinstance(plan, RunPlan):
        raise TypeError("plan must be a RunPlan")
    rows_value = plan.observation_plan.get("observations")
    if not isinstance(rows_value, (list, tuple)) or any(
        not isinstance(row, Mapping) for row in rows_value
    ):
        raise IntegrityError(
            "plan observation topology is missing immutable observations"
        )
    rows = tuple(rows_value)
    rows_by_id = {
        str(row.get("observation_id") or ""): row
        for row in rows
    }
    if (
        not all(rows_by_id)
        or tuple(rows_by_id) != plan.observation_ids
        or len(rows_by_id) != len(rows)
    ):
        raise IntegrityError(
            "plan observation topology differs from observation_ids"
        )
    modes = plan.scope_manifest.get("platform_data_modes")
    platform_data_modes = modes if isinstance(modes, Mapping) else None
    expected_run_budget = build_submission_budget(
        rows,
        platform_data_modes=platform_data_modes,
        retry_policy=plan.retry_policy,
    )
    if plan.submission_budget != expected_run_budget:
        raise IntegrityError(
            "run submission budget does not match frozen observations"
        )
    split_gaio_ids = {
        observation_id
        for packet in plan.packets
        if packet.kind is PacketKind.GAIO_EXTRACTION
        for observation_id in packet.observation_ids
    }
    for packet in plan.packets:
        packet_rows = tuple(
            rows_by_id[observation_id]
            for observation_id in packet.observation_ids
        )
        if packet.kind in {PacketKind.MEASUREMENT, PacketKind.SOURCE_PARSER} and any(
            str(row.get("platform") or "") != packet.platform.value
            for row in packet_rows
        ):
            raise IntegrityError(
                f"measurement packet {packet.packet_id} platform differs "
                "from its frozen observations"
            )
        if packet.kind is PacketKind.GAIO_EXTRACTION and (
            packet.platform is not Platform.GEMINI
            or any(
                str(row.get("platform") or "")
                != Platform.GOOGLE_AI_OVERVIEW.value
                for row in packet_rows
            )
        ):
            raise IntegrityError(
                f"GAIO extraction packet {packet.packet_id} must bind "
                "Gemini to Google AI Overview source observations"
            )
        if packet.kind is PacketKind.GAIO_EXTRACTION:
            expected_packet_budget = (
                build_gaio_extraction_packet_budget(
                    packet_rows,
                    retry_policy=plan.retry_policy,
                )
            )
        elif any(
            str(row.get("platform") or "")
            == Platform.GOOGLE_AI_OVERVIEW.value
            and str(row.get("observation_id") or "")
            not in split_gaio_ids
            for row in packet_rows
        ):
            # Read/create compatibility for an explicitly unsplit Google
            # measurement group that owns both search and conditional parser work.
            expected_packet_budget = build_submission_budget(
                packet_rows,
                platform_data_modes=platform_data_modes,
                retry_policy=plan.retry_policy,
            )
        else:
            expected_packet_budget = build_measurement_packet_budget(
                packet_rows,
                platform_data_modes=platform_data_modes,
                retry_policy=plan.retry_policy,
            )
        if packet.submission_budget != expected_packet_budget:
            raise IntegrityError(
                f"packet {packet.packet_id} submission budget does not "
                "match its frozen observations"
            )
    packet_budget = sum(
        (packet.submission_budget for packet in plan.packets),
        start=SubmissionBudget.zero(),
    )
    if packet_budget != plan.submission_budget:
        raise IntegrityError(
            "packet submission budgets do not sum to the run budget"
        )
    _validate_nested_artifacts(plan)
    if check_hash:
        _sealed_projection(plan)
