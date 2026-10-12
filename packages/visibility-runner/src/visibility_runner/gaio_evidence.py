"""Canonical Google AI Overview terminal-evidence contract.

The browser website, worker, durable send attempt store, repository, and typed
result model all call the same validator.  Successful Google terminal states
therefore cannot be manufactured by omitting evidence at a later boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from .errors import ContractError


GAIO_GENERATED_ANSWER_CAPTURE_VERSION = (
    "google-ai-overview-generated-answer-v1"
)
GAIO_TERMINAL_CAPTURE_EVIDENCE_VERSION = (
    "google-ai-overview-terminal-evidence-v1"
)
GAIO_NOT_SHOWN_MARKERS = (
    "an ai overview is not available for this search",
    "ai overview is not available for this search",
    "can't generate an ai overview right now",
    "cannot generate an ai overview right now",
)
GAIO_TERMINAL_ERROR_MARKERS = (
    "something went wrong",
    "there was an error",
    "try again",
)

MAX_GAIO_EVIDENCE_DEPTH = 8
MAX_GAIO_EVIDENCE_MAPPING_KEYS = 64
MAX_GAIO_EVIDENCE_KEY_CHARS = 256
MAX_GAIO_EVIDENCE_SEQUENCE_ITEMS = 32
MAX_GAIO_EVIDENCE_STRING_CHARS = 2_048
MAX_GAIO_QUERY_PREVIEW_CHARS = 512
MAX_GAIO_URL_PREVIEW_CHARS = 2_048
MAX_GAIO_BUSY_MATCHES = 12
MAX_GAIO_EVIDENCE_NODES = 2_048
MAX_GAIO_TERMINAL_EVIDENCE_BYTES = 131_072
MAX_GAIO_RECONCILIATION_EVIDENCE_BYTES = 131_072
MAX_GAIO_RECONCILIATION_CHECKS = 32

_SHA256_HEX = frozenset("0123456789abcdef")
_REQUIRED_IDENTITY_FIELDS = (
    "profile_id",
    "chrome_directory",
    "browser_context_reference",
    "tab_reference",
    "browser_target_reference",
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _required_sha256(value: object, *, field: str) -> str:
    result = str(value or "").strip().casefold()
    if len(result) != 64 or any(char not in _SHA256_HEX for char in result):
        raise ContractError(f"{field} must be one SHA-256 hexadecimal digest")
    return result


def _required_text(value: object, *, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{field} is required")
    if len(result) > MAX_GAIO_EVIDENCE_STRING_CHARS:
        raise ContractError(f"{field} exceeds the evidence string bound")
    return result


def _finite_number(value: object, *, field: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{field} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContractError(f"{field} must be a finite number") from exc
    if not math.isfinite(result):
        raise ContractError(f"{field} must be a finite number")
    return result


def _nonnegative_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(f"{field} must be a non-negative integer")
    return value


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{field} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ContractError(f"{field} may contain only string keys")
    return value


def _empty_optional_text(value: object) -> bool:
    return value is None or value == ""


def _strict_json_copy(
    value: object,
    *,
    field: str,
    depth: int = 0,
    ancestors: Optional[set[int]] = None,
    node_count: Optional[list[int]] = None,
) -> object:
    """Return a defensive bounded JSON copy without silently losing proof."""

    if depth > MAX_GAIO_EVIDENCE_DEPTH:
        raise ContractError(f"{field} exceeds the evidence depth bound")
    if ancestors is None:
        ancestors = set()
    if node_count is None:
        node_count = [0]
    node_count[0] += 1
    if node_count[0] > MAX_GAIO_EVIDENCE_NODES:
        raise ContractError(f"{field} exceeds the evidence node bound")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError(f"{field} contains a non-finite number")
        return value
    if isinstance(value, str):
        if len(value) > MAX_GAIO_EVIDENCE_STRING_CHARS:
            raise ContractError(f"{field} exceeds the evidence string bound")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in ancestors:
            raise ContractError(f"{field} contains a cycle")
        if len(value) > MAX_GAIO_EVIDENCE_MAPPING_KEYS:
            raise ContractError(f"{field} exceeds the mapping-key bound")
        ancestors.add(identity)
        try:
            result: dict[str, object] = {}
            for key in sorted(value):
                if not isinstance(key, str):
                    raise ContractError(
                        f"{field} may contain only string keys"
                    )
                if len(key) > MAX_GAIO_EVIDENCE_KEY_CHARS:
                    raise ContractError(
                        f"{field} contains an overlong mapping key"
                    )
                result[key] = _strict_json_copy(
                    value[key],
                    field=f"{field}.{key}",
                    depth=depth + 1,
                    ancestors=ancestors,
                    node_count=node_count,
                )
            return result
        finally:
            ancestors.remove(identity)
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        identity = id(value)
        if identity in ancestors:
            raise ContractError(f"{field} contains a cycle")
        if len(value) > MAX_GAIO_EVIDENCE_SEQUENCE_ITEMS:
            raise ContractError(f"{field} exceeds the sequence bound")
        ancestors.add(identity)
        try:
            return [
                _strict_json_copy(
                    item,
                    field=f"{field}[{index}]",
                    depth=depth + 1,
                    ancestors=ancestors,
                    node_count=node_count,
                )
                for index, item in enumerate(value)
            ]
        finally:
            ancestors.remove(identity)
    raise ContractError(f"{field} contains a non-JSON value")


def canonical_gaio_reconciliation_evidence(
    value: object,
) -> dict[str, object]:
    """Return one bounded defensive copy of GAIO reconciliation evidence."""

    source = _mapping(value, field="GAIO reconciliation evidence")
    if not source:
        raise ContractError("GAIO reconciliation evidence must not be empty")
    if "checks" in source:
        checks = source["checks"]
        if not isinstance(checks, Sequence) or isinstance(
            checks,
            (str, bytes, bytearray),
        ):
            raise ContractError(
                "GAIO reconciliation evidence checks must be a sequence"
            )
        if len(checks) > MAX_GAIO_RECONCILIATION_CHECKS:
            raise ContractError(
                "GAIO reconciliation evidence has too many checks"
            )
    copied = _strict_json_copy(
        source,
        field="GAIO reconciliation evidence",
    )
    assert isinstance(copied, dict)
    payload = json.dumps(
        copied,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_GAIO_RECONCILIATION_EVIDENCE_BYTES:
        raise ContractError(
            "GAIO reconciliation evidence exceeds its byte bound"
        )
    return copied


def canonical_gaio_retry_authorization_evidence(
    value: object,
    *,
    expected_prompt_sha256: str,
    expected_identity: Mapping[str, object],
    expected_response_baseline: Mapping[str, object],
) -> dict[str, object]:
    """Validate the positive proof required before a Google resend."""

    evidence = canonical_gaio_reconciliation_evidence(value)
    if evidence.get("reason") != "stable_google_terminal_error":
        raise ContractError(
            "Google retry evidence lacks a stable terminal-error decision"
        )
    checks = evidence.get("checks")
    if not isinstance(checks, Sequence) or isinstance(
        checks,
        (str, bytes, bytearray),
    ):
        raise ContractError("Google retry evidence checks are invalid")
    retained_count = len(checks)
    if retained_count < 2:
        raise ContractError("Google retry evidence requires repeated checks")
    stable_count = _nonnegative_int(
        evidence.get("stable_terminal_checks"),
        field="reconciliation_evidence.stable_terminal_checks",
    )
    if stable_count < 2 or stable_count > retained_count:
        raise ContractError(
            "Google retry evidence lacks two retained stable checks"
        )
    declared_retained = _nonnegative_int(
        evidence.get("retained_check_count"),
        field="reconciliation_evidence.retained_check_count",
    )
    total_count = _nonnegative_int(
        evidence.get("total_check_count"),
        field="reconciliation_evidence.total_check_count",
    )
    truncated_count = _nonnegative_int(
        evidence.get("truncated_check_count"),
        field="reconciliation_evidence.truncated_check_count",
    )
    if (
        declared_retained != retained_count
        or total_count < retained_count
        or truncated_count != total_count - retained_count
    ):
        raise ContractError(
            "Google retry evidence check counts are inconsistent"
        )

    query_binding = _canonical_query_binding(evidence.get("query_binding"))
    expected_prompt = _required_sha256(
        expected_prompt_sha256,
        field="expected_prompt_sha256",
    )
    if query_binding["query_sha256"] != expected_prompt:
        raise ContractError(
            "Google retry evidence is bound to a different prompt"
        )
    identity_binding = _canonical_identity(
        evidence.get("browser_target_identity_binding")
    )
    if identity_binding != _canonical_identity(expected_identity):
        raise ContractError(
            "Google retry evidence is bound to a different browser target"
        )
    if (
        identity_binding["conversation_reference_sha256"]
        != query_binding["document_url_sha256"]
    ):
        raise ContractError(
            "Google retry target is not the captured search URL"
        )
    baseline = _canonical_response_baseline(
        expected_response_baseline,
        field="expected_response_baseline",
    )

    stable_checks = checks[-stable_count:]
    prior_checked_at: Optional[float] = None
    prior_ordinal: Optional[int] = None
    failure_classification: Optional[str] = None
    selected_root_key: Optional[tuple[str, str, str]] = None
    terminal_marker_key: Optional[tuple[str, str, str, str]] = None
    for offset, raw_check in enumerate(stable_checks):
        field = f"reconciliation_evidence.stable_checks[{offset}]"
        check = _mapping(raw_check, field=field)
        checked_at = _finite_number(
            check.get("checked_at_monotonic_seconds"),
            field=f"{field}.checked_at_monotonic_seconds",
        )
        if prior_checked_at is not None and checked_at <= prior_checked_at:
            raise ContractError(
                "Google retry evidence checks are not distinctly ordered"
            )
        prior_checked_at = checked_at
        ordinal = _nonnegative_int(
            check.get("probe_ordinal"),
            field=f"{field}.probe_ordinal",
        )
        if ordinal < 1 or (
            prior_ordinal is not None and ordinal <= prior_ordinal
        ):
            raise ContractError(
                "Google retry evidence probe ordinals are not ordered"
            )
        prior_ordinal = ordinal
        if (
            check.get("identity_valid") is not True
            or check.get("expected_query_present") is not True
            or check.get("capture_state") != "terminal_error"
            or check.get("busy") is not False
            or not _empty_optional_text(check.get("busy_reason"))
            or str(check.get("expected_prompt_sha256") or "")
            != expected_prompt
        ):
            raise ContractError(
                "Google retry evidence contains a non-terminal check"
            )
        current_failure = _required_text(
            check.get("failure_classification"),
            field=f"{field}.failure_classification",
        )
        if not current_failure.startswith("google_terminal_error:"):
            raise ContractError(
                "Google retry evidence has an unsupported terminal failure"
            )
        if (
            failure_classification is not None
            and current_failure != failure_classification
        ):
            raise ContractError(
                "Google retry evidence changed terminal failure"
            )
        failure_classification = current_failure
        marker = current_failure.removeprefix("google_terminal_error:")
        if marker not in GAIO_TERMINAL_ERROR_MARKERS:
            raise ContractError(
                "Google retry evidence has an unsupported terminal marker"
            )
        check_baseline = _canonical_response_baseline(
            check.get("response_baseline"),
            field=f"{field}.response_baseline",
        )
        if check_baseline != baseline:
            raise ContractError(
                "Google retry evidence changed the pre-send baseline"
            )
        root, current_root_key = _canonical_retry_root_selection(
            check.get("root_selection"),
            field=f"{field}.root_selection",
        )
        if selected_root_key is not None and current_root_key != selected_root_key:
            raise ContractError(
                "Google retry evidence changed the selected Overview root"
            )
        selected_root_key = current_root_key
        _canonical_retry_busy_evidence(
            check.get("busy_evidence"),
            field=f"{field}.busy_evidence",
            selected_root_key=current_root_key,
        )
        check_query_sha256 = _required_sha256(
            check.get("query_sha256"),
            field=f"{field}.query_sha256",
        )
        check_document_url_sha256 = _required_sha256(
            check.get("document_url_sha256"),
            field=f"{field}.document_url_sha256",
        )
        if (
            check_query_sha256 != expected_prompt
            or check_document_url_sha256
            != query_binding["document_url_sha256"]
        ):
            raise ContractError(
                "Google retry evidence changed the captured search query"
            )
        check_identity = _canonical_retry_identity_summary(
            check.get("browser_target_identity_binding"),
            field=f"{field}.browser_target_identity_binding",
        )
        expected_check_identity = {
            key: identity_binding[key]
            for key in (
                *_REQUIRED_IDENTITY_FIELDS,
                "conversation_reference_sha256",
            )
        }
        if check_identity != expected_check_identity:
            raise ContractError(
                "Google retry evidence changed the browser target"
            )
        terminal, current_terminal_key = _canonical_retry_terminal_evidence(
            check.get("terminal_evidence"),
            field=f"{field}.terminal_evidence",
            selected_root_key=current_root_key,
            marker=marker,
        )
        if (
            terminal_marker_key is not None
            and current_terminal_key != terminal_marker_key
        ):
            raise ContractError(
                "Google retry evidence changed the rendered terminal marker"
            )
        terminal_marker_key = current_terminal_key

        # Replace attacker-controlled aliases with the compact canonical proof.
        assert isinstance(checks, list)
        canonical_check = dict(check)
        canonical_check["failure_classification"] = current_failure
        canonical_check["response_baseline"] = check_baseline
        canonical_check["root_selection"] = root
        canonical_check["busy_evidence"] = _canonical_retry_busy_evidence(
            check.get("busy_evidence"),
            field=f"{field}.busy_evidence",
            selected_root_key=current_root_key,
        )
        canonical_check["query_sha256"] = check_query_sha256
        canonical_check["document_url_sha256"] = check_document_url_sha256
        canonical_check["browser_target_identity_binding"] = check_identity
        canonical_check["terminal_evidence"] = terminal
        checks[retained_count - stable_count + offset] = canonical_check

    evidence["query_binding"] = query_binding
    evidence["browser_target_identity_binding"] = identity_binding
    return evidence


def _canonical_response_baseline(
    value: object,
    *,
    field: str,
) -> dict[str, object]:
    source = _mapping(value, field=field)
    count = _nonnegative_int(
        source.get("visible_response_count"),
        field=f"{field}.visible_response_count",
    )
    digest = _required_sha256(
        source.get("newest_response_sha256"),
        field=f"{field}.newest_response_sha256",
    )
    return {
        "visible_response_count": count,
        "newest_response_sha256": digest,
    }


def _canonical_retry_identity_summary(
    value: object,
    *,
    field: str,
) -> dict[str, object]:
    source = _mapping(value, field=field)
    result = {
        key: _required_text(source.get(key), field=f"{field}.{key}")
        for key in _REQUIRED_IDENTITY_FIELDS
    }
    result["conversation_reference_sha256"] = _required_sha256(
        source.get("conversation_reference_sha256"),
        field=f"{field}.conversation_reference_sha256",
    )
    return result


def _canonical_retry_root_selection(
    value: object,
    *,
    field: str,
) -> tuple[dict[str, object], tuple[str, str, str]]:
    source = _mapping(value, field=field)
    if source.get("status") != "selected":
        raise ContractError(
            "Google retry evidence lacks a selected Overview root"
        )
    reason = source.get("reason")
    if reason not in {
        "unique_visible_exact_heading_root",
        "sole_visible_ai_overview_root",
    }:
        raise ContractError(
            "Google retry evidence has an unsupported root-selection reason"
        )
    candidate_count = _nonnegative_int(
        source.get("candidate_count"),
        field=f"{field}.candidate_count",
    )
    visible_count = _nonnegative_int(
        source.get("visible_candidate_count"),
        field=f"{field}.visible_candidate_count",
    )
    heading_count = _nonnegative_int(
        source.get("exact_visible_heading_count"),
        field=f"{field}.exact_visible_heading_count",
    )
    if (
        candidate_count < 1
        or visible_count < 1
        or visible_count > candidate_count
        or (
            reason == "unique_visible_exact_heading_root"
            and heading_count != 1
        )
        or (
            reason == "sole_visible_ai_overview_root"
            and (visible_count != 1 or heading_count not in {0, 1})
        )
    ):
        raise ContractError(
            "Google retry evidence has contradictory root-selection counts"
        )
    selected_root = _compact_selected_root(
        source.get("selected_root"),
        field=f"{field}.selected_root",
        include_visibility=False,
    )
    selected_root_key = _root_key(selected_root)
    return (
        {
            "status": "selected",
            "reason": reason,
            "candidate_count": candidate_count,
            "visible_candidate_count": visible_count,
            "exact_visible_heading_count": heading_count,
            "selected_root": selected_root,
        },
        selected_root_key,
    )


def _canonical_retry_busy_evidence(
    value: object,
    *,
    field: str,
    selected_root_key: tuple[str, str, str],
) -> dict[str, object]:
    source = _mapping(value, field=field)
    if (
        source.get("scope") != "selected_visible_ai_overview_root"
        or source.get("trusted_root_present") is not True
        or source.get("evidence_available") is not True
        or not _empty_optional_text(source.get("busy_reason"))
    ):
        raise ContractError(
            "Google retry evidence does not prove an idle selected root"
        )
    candidate_count = _nonnegative_int(
        source.get("candidate_count"),
        field=f"{field}.candidate_count",
    )
    active_count = _nonnegative_int(
        source.get("active_candidate_count"),
        field=f"{field}.active_candidate_count",
    )
    truncated_count = _nonnegative_int(
        source.get("truncated_candidate_count"),
        field=f"{field}.truncated_candidate_count",
    )
    if active_count != 0 or truncated_count > candidate_count:
        raise ContractError(
            "Google retry evidence contains active or contradictory busy proof"
        )
    selected_root = _compact_selected_root(
        source.get("selected_root"),
        field=f"{field}.selected_root",
        include_visibility=False,
    )
    if _root_key(selected_root) != selected_root_key:
        raise ContractError(
            "Google retry busy evidence changed the selected root"
        )
    return {
        "scope": "selected_visible_ai_overview_root",
        "trusted_root_present": True,
        "evidence_available": True,
        "candidate_count": candidate_count,
        "active_candidate_count": 0,
        "truncated_candidate_count": truncated_count,
        "busy_reason": None,
        "selected_root": selected_root,
    }


def _canonical_retry_terminal_evidence(
    value: object,
    *,
    field: str,
    selected_root_key: tuple[str, str, str],
    marker: str,
) -> tuple[dict[str, object], tuple[str, str, str, str]]:
    source = _mapping(value, field=field)
    if source.get("selector") != '[role="alert"], .g-error-card':
        raise ContractError(
            "Google retry evidence has an unsupported terminal selector"
        )
    matched_node = _mapping(
        source.get("matched_node"),
        field=f"{field}.matched_node",
    )
    matched_dom_path = _required_text(
        matched_node.get("dom_path"),
        field=f"{field}.matched_node.dom_path",
    )
    matched_tag = _required_text(
        matched_node.get("tag"),
        field=f"{field}.matched_node.tag",
    ).casefold()
    normalized_text = _required_text(
        matched_node.get("normalized_text"),
        field=f"{field}.matched_node.normalized_text",
    ).casefold()
    if marker not in normalized_text:
        raise ContractError(
            "Google retry terminal classification is absent from the "
            "rendered marker"
        )
    selected_root = _compact_selected_root(
        source.get("selected_root"),
        field=f"{field}.selected_root",
        include_visibility=False,
    )
    if _root_key(selected_root) != selected_root_key:
        raise ContractError(
            "Google retry terminal marker changed the selected root"
        )
    visibility = _mapping(
        source.get("rendered_visibility"),
        field=f"{field}.rendered_visibility",
    )
    bounds = _mapping(
        visibility.get("rendered_bounds"),
        field=f"{field}.rendered_visibility.rendered_bounds",
    )
    rejected_by = visibility.get("rejected_by")
    width = _finite_number(
        bounds.get("width"),
        field=f"{field}.rendered_visibility.rendered_bounds.width",
    )
    height = _finite_number(
        bounds.get("height"),
        field=f"{field}.rendered_visibility.rendered_bounds.height",
    )
    if (
        visibility.get("rendered") is not True
        or bounds.get("positive") is not True
        or width <= 0
        or height <= 0
        or not isinstance(rejected_by, Sequence)
        or isinstance(rejected_by, (str, bytes, bytearray))
        or len(rejected_by) != 0
    ):
        raise ContractError(
            "Google retry terminal marker is not rendered"
        )
    captured_at = _finite_number(
        source.get("captured_at_epoch_ms"),
        field=f"{field}.captured_at_epoch_ms",
    )
    canonical = {
        "selector": '[role="alert"], .g-error-card',
        "matched_node": {
            "dom_path": matched_dom_path,
            "tag": matched_tag,
            "normalized_text": normalized_text,
        },
        "rendered_visibility": {
            "rendered": True,
            "rendered_bounds": {
                "width": width,
                "height": height,
                "positive": True,
            },
            "rejected_by": [],
        },
        "selected_root": selected_root,
        "captured_at_epoch_ms": captured_at,
    }
    return (
        canonical,
        (marker, matched_dom_path, matched_tag, normalized_text),
    )


def _search_contract(url: str) -> Optional[dict[str, object]]:
    try:
        parsed = urlsplit(url)
        values = parse_qs(parsed.query, keep_blank_values=True)
    except (TypeError, ValueError):
        return None
    query = values.get("q")
    hl = values.get("hl")
    gl = values.get("gl")
    pws = values.get("pws")
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"www.google.com", "google.com"}
        or parsed.path != "/search"
        or not isinstance(query, list)
        or len(query) != 1
        or not isinstance(hl, list)
        or len(hl) != 1
        or not isinstance(gl, list)
        or len(gl) != 1
        or pws != ["0"]
    ):
        return None
    return {
        "scheme": "https",
        "hostname": str(parsed.hostname),
        "path": "/search",
        "query_sha256": _digest(query[0]),
        "query_length": len(query[0]),
        "hl": hl[0].casefold(),
        "gl": gl[0].casefold(),
        "pws": "0",
    }


def _bounded_text_record(
    value: str,
    *,
    preview_limit: int,
    prefix: str,
) -> dict[str, object]:
    return {
        prefix: value[:preview_limit],
        f"{prefix}_length": len(value),
        f"{prefix}_sha256": _digest(value),
        f"{prefix}_truncated": len(value) > preview_limit,
    }


def build_gaio_query_binding(
    *,
    prompt: str,
    expected_url: str,
    document_url: str,
    document_url_source: str,
    hl: str,
    gl: str,
) -> dict[str, object]:
    """Build compact proof after validating both complete Google URLs."""

    expected_contract = _search_contract(expected_url)
    document_contract = _search_contract(document_url)
    prompt_sha256 = _digest(prompt)
    locale_hl = str(hl).casefold()
    locale_gl = str(gl).casefold()
    required_contract = {
        "query_sha256": prompt_sha256,
        "query_length": len(prompt),
        "hl": locale_hl,
        "gl": locale_gl,
        "pws": "0",
    }
    for label, contract in (
        ("expected_url", expected_contract),
        ("document_url", document_contract),
    ):
        if contract is None or any(
            contract.get(key) != expected
            for key, expected in required_contract.items()
        ):
            raise ContractError(
                f"Google {label} does not contain the exact bound query"
            )
    if str(document_url_source or "") != "capture_script":
        raise ContractError(
            "Google document URL must come from the atomic capture script"
        )
    return {
        **_bounded_text_record(
            prompt,
            preview_limit=MAX_GAIO_QUERY_PREVIEW_CHARS,
            prefix="query",
        ),
        **_bounded_text_record(
            expected_url,
            preview_limit=MAX_GAIO_URL_PREVIEW_CHARS,
            prefix="expected_url",
        ),
        **_bounded_text_record(
            document_url,
            preview_limit=MAX_GAIO_URL_PREVIEW_CHARS,
            prefix="document_url",
        ),
        "expected_url_contract": expected_contract,
        "document_url_contract": document_contract,
        "document_url_source": "capture_script",
        "hl": locale_hl,
        "gl": locale_gl,
        "exact_query_present": True,
    }


def _canonical_text_record(
    value: Mapping[str, Any],
    *,
    prefix: str,
    preview_limit: int,
) -> tuple[dict[str, object], str, int]:
    preview = str(value.get(prefix) or "")
    length_value = value.get(f"{prefix}_length")
    digest_value = value.get(f"{prefix}_sha256")
    truncated_value = value.get(f"{prefix}_truncated")
    if length_value is None and digest_value is None and truncated_value is None:
        full_value = preview
        length = len(full_value)
        digest = _digest(full_value)
        truncated = len(full_value) > preview_limit
        preview = full_value[:preview_limit]
    else:
        length = _nonnegative_int(
            length_value,
            field=f"query_binding.{prefix}_length",
        )
        digest = _required_sha256(
            digest_value,
            field=f"query_binding.{prefix}_sha256",
        )
        if not isinstance(truncated_value, bool):
            raise ContractError(
                f"query_binding.{prefix}_truncated must be a boolean"
            )
        truncated = truncated_value
        if len(preview) > preview_limit:
            raise ContractError(
                f"query_binding.{prefix} exceeds its preview bound"
            )
        if truncated:
            if length <= len(preview):
                raise ContractError(
                    f"query_binding.{prefix} truncation metadata is invalid"
                )
        else:
            if length != len(preview) or digest != _digest(preview):
                raise ContractError(
                    f"query_binding.{prefix} fingerprint is invalid"
                )
    return (
        {
            prefix: preview,
            f"{prefix}_length": length,
            f"{prefix}_sha256": digest,
            f"{prefix}_truncated": truncated,
        },
        digest,
        length,
    )


def _canonical_contract(
    value: object,
    *,
    field: str,
    query_sha256: str,
    query_length: int,
    hl: str,
    gl: str,
    full_url: Optional[str],
) -> dict[str, object]:
    observed = _search_contract(full_url) if full_url is not None else None
    if observed is None:
        source = _mapping(value, field=field)
        observed_copy = _strict_json_copy(source, field=field)
        assert isinstance(observed_copy, dict)
        observed = observed_copy
    required = {
        "scheme": "https",
        "path": "/search",
        "query_sha256": query_sha256,
        "query_length": query_length,
        "hl": hl,
        "gl": gl,
        "pws": "0",
    }
    if str(observed.get("hostname") or "") not in {
        "www.google.com",
        "google.com",
    } or any(observed.get(key) != expected for key, expected in required.items()):
        raise ContractError(f"{field} does not prove the exact Google query")
    return {
        **required,
        "hostname": str(observed["hostname"]),
    }


def _canonical_query_binding(value: object) -> dict[str, object]:
    source = _mapping(value, field="query_binding")
    query_record, query_sha256, query_length = _canonical_text_record(
        source,
        prefix="query",
        preview_limit=MAX_GAIO_QUERY_PREVIEW_CHARS,
    )
    expected_record, _, _ = _canonical_text_record(
        source,
        prefix="expected_url",
        preview_limit=MAX_GAIO_URL_PREVIEW_CHARS,
    )
    document_record, _, _ = _canonical_text_record(
        source,
        prefix="document_url",
        preview_limit=MAX_GAIO_URL_PREVIEW_CHARS,
    )
    hl = _required_text(source.get("hl"), field="query_binding.hl").casefold()
    gl = _required_text(source.get("gl"), field="query_binding.gl").casefold()
    if source.get("exact_query_present") is not True:
        raise ContractError("query_binding must prove the exact query")
    if source.get("document_url_source") != "capture_script":
        raise ContractError(
            "query_binding document URL lacks atomic-capture provenance"
        )
    expected_full = (
        str(source.get("expected_url") or "")
        if expected_record["expected_url_truncated"] is False
        else None
    )
    document_full = (
        str(source.get("document_url") or "")
        if document_record["document_url_truncated"] is False
        else None
    )
    expected_contract = _canonical_contract(
        source.get("expected_url_contract"),
        field="query_binding.expected_url_contract",
        query_sha256=query_sha256,
        query_length=query_length,
        hl=hl,
        gl=gl,
        full_url=expected_full,
    )
    document_contract = _canonical_contract(
        source.get("document_url_contract"),
        field="query_binding.document_url_contract",
        query_sha256=query_sha256,
        query_length=query_length,
        hl=hl,
        gl=gl,
        full_url=document_full,
    )
    return {
        **query_record,
        **expected_record,
        **document_record,
        "expected_url_contract": expected_contract,
        "document_url_contract": document_contract,
        "document_url_source": "capture_script",
        "hl": hl,
        "gl": gl,
        "exact_query_present": True,
    }


def _canonical_identity(value: object) -> dict[str, object]:
    source = _mapping(value, field="browser_target_identity_binding")
    result: dict[str, object] = {}
    for key in _REQUIRED_IDENTITY_FIELDS:
        result[key] = _required_text(
            source.get(key),
            field=f"browser_target_identity_binding.{key}",
        )
    conversation, _, _ = _canonical_text_record(
        source,
        prefix="conversation_reference",
        preview_limit=MAX_GAIO_URL_PREVIEW_CHARS,
    )
    result.update(conversation)
    return result


def canonical_gaio_identity_binding(
    value: Mapping[str, object],
) -> dict[str, object]:
    """Return the bounded canonical browser-target identity representation."""

    return _canonical_identity(value)


def _root_key(value: object) -> tuple[str, str, str]:
    root = _mapping(value, field="selected_root")
    return (
        _required_text(root.get("dom_path"), field="selected_root.dom_path"),
        _required_text(root.get("tag"), field="selected_root.tag").casefold(),
        str(root.get("selector") or ""),
    )


def _compact_selected_root(
    value: object,
    *,
    field: str,
    include_visibility: bool,
) -> dict[str, object]:
    """Project a validated root to its stable identity and required proof."""

    source = _mapping(value, field=field)
    result: dict[str, object] = {
        "dom_path": _required_text(
            source.get("dom_path"),
            field=f"{field}.dom_path",
        ),
        "tag": _required_text(
            source.get("tag"),
            field=f"{field}.tag",
        ),
        "selector": str(source.get("selector") or ""),
    }
    if "candidate_index" in source:
        result["candidate_index"] = _nonnegative_int(
            source["candidate_index"],
            field=f"{field}.candidate_index",
        )
    if "exact_visible_heading_count" in source:
        result["exact_visible_heading_count"] = _nonnegative_int(
            source["exact_visible_heading_count"],
            field=f"{field}.exact_visible_heading_count",
        )
    if include_visibility:
        visibility = _mapping(
            source.get("visibility"),
            field=f"{field}.visibility",
        )
        bounds = _mapping(
            visibility.get("rendered_bounds"),
            field=f"{field}.visibility.rendered_bounds",
        )
        result["visibility"] = {
            "rendered": True,
            "rendered_bounds": {
                "width": _finite_number(
                    bounds.get("width"),
                    field=f"{field}.visibility.rendered_bounds.width",
                ),
                "height": _finite_number(
                    bounds.get("height"),
                    field=f"{field}.visibility.rendered_bounds.height",
                ),
                "positive": True,
            },
            "rejected_by": [],
        }
    return result


def _compact_root_selection(
    value: object,
    *,
    field: str,
) -> dict[str, object]:
    """Retain the selected-root proof without repeating candidate diagnostics."""

    source = _mapping(value, field=field)
    return {
        "status": "selected",
        "reason": source["reason"],
        "candidate_count": source["candidate_count"],
        "visible_candidate_count": source["visible_candidate_count"],
        "exact_visible_heading_count": source[
            "exact_visible_heading_count"
        ],
        "selected_root": _compact_selected_root(
            source.get("selected_root"),
            field=f"{field}.selected_root",
            include_visibility=True,
        ),
    }


def _canonical_root_selection(
    value: object,
    *,
    field: str,
    compact: bool = False,
) -> tuple[dict[str, object], tuple[str, str, str]]:
    source = _mapping(value, field=field)
    if source.get("status") != "selected":
        raise ContractError(f"{field} must select one unambiguous root")
    reason = source.get("reason")
    if reason not in {
        "unique_visible_exact_heading_root",
        "sole_visible_ai_overview_root",
    }:
        raise ContractError(f"{field} has no accepted selection reason")
    candidate_count = _nonnegative_int(
        source.get("candidate_count"),
        field=f"{field}.candidate_count",
    )
    visible_candidate_count = _nonnegative_int(
        source.get("visible_candidate_count"),
        field=f"{field}.visible_candidate_count",
    )
    exact_visible_heading_count = _nonnegative_int(
        source.get("exact_visible_heading_count"),
        field=f"{field}.exact_visible_heading_count",
    )
    if (
        candidate_count < 1
        or visible_candidate_count < 1
        or visible_candidate_count > candidate_count
    ):
        raise ContractError(f"{field} candidate counts are inconsistent")
    if reason == "unique_visible_exact_heading_root":
        if exact_visible_heading_count != 1:
            raise ContractError(
                f"{field} exact-heading count contradicts its reason"
            )
    elif visible_candidate_count != 1 or exact_visible_heading_count not in {
        0,
        1,
    }:
        raise ContractError(
            f"{field} visible-root counts contradict its reason"
        )
    selected_root = _mapping(
        source.get("selected_root"),
        field=f"{field}.selected_root",
    )
    visibility = _mapping(
        selected_root.get("visibility"),
        field=f"{field}.selected_root.visibility",
    )
    rejected = visibility.get("rejected_by")
    bounds = _mapping(
        visibility.get("rendered_bounds"),
        field=f"{field}.selected_root.visibility.rendered_bounds",
    )
    if (
        visibility.get("rendered") is not True
        or not isinstance(rejected, Sequence)
        or isinstance(rejected, (str, bytes, bytearray))
        or len(rejected) != 0
        or bounds.get("positive") is not True
        or _finite_number(
            bounds.get("width"),
            field=(
                f"{field}.selected_root.visibility."
                "rendered_bounds.width"
            ),
        )
        <= 0
        or _finite_number(
            bounds.get("height"),
            field=(
                f"{field}.selected_root.visibility."
                "rendered_bounds.height"
            ),
        )
        <= 0
    ):
        raise ContractError(f"{field} selected root is not rendered")
    selected_root_key = _root_key(selected_root)
    selected_index: Optional[int] = None
    if "candidate_index" in selected_root:
        selected_index = _nonnegative_int(
            selected_root["candidate_index"],
            field=f"{field}.selected_root.candidate_index",
        )
        if selected_index >= candidate_count:
            raise ContractError(
                f"{field} selected root index exceeds candidate count"
            )
    if "exact_visible_heading_count" in selected_root:
        selected_heading_count = _nonnegative_int(
            selected_root["exact_visible_heading_count"],
            field=(
                f"{field}.selected_root.exact_visible_heading_count"
            ),
        )
        required_selected_heading_count = (
            1
            if reason == "unique_visible_exact_heading_root"
            else 0
        )
        if selected_heading_count != required_selected_heading_count:
            raise ContractError(
                f"{field} selected root heading count is inconsistent"
            )

    truncated_count: Optional[int] = None
    if "truncated_candidate_count" in source:
        truncated_count = _nonnegative_int(
            source["truncated_candidate_count"],
            field=f"{field}.truncated_candidate_count",
        )
        if truncated_count > candidate_count:
            raise ContractError(
                f"{field} truncated candidate count is inconsistent"
            )
    candidates_value = source.get("candidates")
    if candidates_value is not None:
        if not isinstance(candidates_value, Sequence) or isinstance(
            candidates_value,
            (str, bytes, bytearray),
        ):
            raise ContractError(f"{field}.candidates must be a sequence")
        if len(candidates_value) > candidate_count:
            raise ContractError(
                f"{field} retains more candidates than it counted"
            )
        if (
            truncated_count is not None
            and len(candidates_value) + truncated_count != candidate_count
        ):
            raise ContractError(
                f"{field} retained and truncated counts are inconsistent"
            )
        candidate_keys: list[tuple[str, str, str]] = []
        candidate_rendered: list[bool] = []
        complete_visibility = True
        for index, candidate in enumerate(candidates_value):
            candidate_source = _mapping(
                candidate,
                field=f"{field}.candidates[{index}]",
            )
            if "candidate_index" in candidate_source:
                candidate_index = _nonnegative_int(
                    candidate_source["candidate_index"],
                    field=(
                        f"{field}.candidates[{index}].candidate_index"
                    ),
                )
                if candidate_index != index:
                    raise ContractError(
                        f"{field} candidate indexes are inconsistent"
                    )
            candidate_key = _root_key(candidate_source)
            if candidate_key in candidate_keys:
                raise ContractError(
                    f"{field} contains duplicate root candidates"
                )
            candidate_keys.append(candidate_key)
            candidate_visibility = candidate_source.get("visibility")
            if isinstance(candidate_visibility, Mapping) and isinstance(
                candidate_visibility.get("rendered"),
                bool,
            ):
                candidate_rendered.append(
                    bool(candidate_visibility["rendered"])
                )
            else:
                complete_visibility = False
        if (
            selected_index is not None
            and selected_index < len(candidate_keys)
            and candidate_keys[selected_index] != selected_root_key
        ):
            raise ContractError(
                f"{field} selected root contradicts its candidate"
            )
        complete_candidates = (
            len(candidate_keys) == candidate_count
            and truncated_count in {None, 0}
        )
        if complete_candidates and selected_root_key not in candidate_keys:
            raise ContractError(
                f"{field} selected root is absent from its candidates"
            )
        if (
            complete_candidates
            and complete_visibility
            and sum(candidate_rendered) != visible_candidate_count
        ):
            raise ContractError(
                f"{field} visible candidate count is inconsistent"
            )
    copied = _strict_json_copy(source, field=field)
    assert isinstance(copied, dict)
    if compact:
        copied = _compact_root_selection(copied, field=field)
    return copied, selected_root_key


def _canonical_busy_evidence(
    value: object,
    *,
    selected_root_key: tuple[str, str, str],
    field: str,
    compact: bool = False,
) -> dict[str, object]:
    source = _mapping(value, field=field)
    copied = _strict_json_copy(source, field=field)
    assert isinstance(copied, dict)
    if _root_key(source.get("selected_root")) != selected_root_key:
        raise ContractError(f"{field} is bound to a different root")
    root_selection, root_key = _canonical_root_selection(
        source.get("root_selection"),
        field=f"{field}.root_selection",
        compact=True,
    )
    if root_key != selected_root_key:
        raise ContractError(f"{field} root selection is inconsistent")
    copied["selected_root"] = _compact_selected_root(
        copied.get("selected_root"),
        field=f"{field}.selected_root",
        include_visibility=False,
    )
    copied["root_selection"] = root_selection
    if source.get("scope") != "selected_visible_ai_overview_root":
        raise ContractError(f"{field} has an invalid evidence scope")
    if source.get("trusted_root_present") is not True:
        raise ContractError(f"{field} lacks a trusted selected root")
    if source.get("evidence_available") is not True:
        raise ContractError(f"{field} is not available")
    candidate_count = _nonnegative_int(
        source.get("candidate_count"),
        field=f"{field}.candidate_count",
    )
    active_count = source.get("active_candidate_count")
    if (
        isinstance(active_count, bool)
        or not isinstance(active_count, int)
        or active_count != 0
    ):
        raise ContractError(f"{field} contains active terminal-state busy proof")
    if not _empty_optional_text(source.get("busy_reason")):
        raise ContractError(f"{field} contains a terminal-state busy reason")
    matches = source.get("matches")
    if not isinstance(matches, Sequence) or isinstance(
        matches, (str, bytes, bytearray)
    ):
        raise ContractError(f"{field}.matches must be a sequence")
    if len(matches) > MAX_GAIO_BUSY_MATCHES:
        raise ContractError(f"{field}.matches exceeds the busy bound")
    truncated_count = _nonnegative_int(
        source.get("truncated_candidate_count"),
        field=f"{field}.truncated_candidate_count",
    )
    decision_match = source.get("decision_match")
    expected_retained_count = min(
        candidate_count,
        MAX_GAIO_BUSY_MATCHES,
    )
    if (
        len(matches) != expected_retained_count
        or truncated_count
        != candidate_count - expected_retained_count
    ):
        raise ContractError(
            f"{field} candidate retention counts are inconsistent"
        )
    if (decision_match is None) != (expected_retained_count == 0):
        raise ContractError(
            f"{field} decision match is inconsistent with candidates"
        )
    def reject_active_match(
        match: Mapping[str, Any],
        *,
        match_field: str,
    ) -> None:
        match_visibility = _mapping(
            match.get("visibility"),
            field=f"{match_field}.visibility",
        )
        generation = match.get("generation_binding")
        generation_mapping = (
            _mapping(
                generation,
                field=f"{match_field}.generation_binding",
            )
            if generation is not None
            else {}
        )
        contradicted_binding = (
            match_visibility.get("rendered") is True
            and (
                generation_mapping.get("labelled_generation") is True
                or generation_mapping.get("selected_surface_state") is True
            )
        )
        if (
            match_visibility.get("active") is True
            or contradicted_binding
        ):
            raise ContractError(
                f"{field} contradicts its idle terminal-state proof"
            )

    canonical_matches: list[Mapping[str, Any]] = []
    for index, match in enumerate(matches):
        match_source = _mapping(
            match,
            field=f"{field}.matches[{index}]",
        )
        reject_active_match(
            match_source,
            match_field=f"{field}.matches[{index}]",
        )
        candidate_index = match_source.get("candidate_index")
        if (
            isinstance(candidate_index, bool)
            or not isinstance(candidate_index, int)
            or candidate_index != index
        ):
            raise ContractError(
                f"{field}.matches[{index}] has an invalid candidate index"
            )
        canonical_matches.append(match_source)
    decision_source: Optional[Mapping[str, Any]] = None
    if decision_match is not None:
        decision_source = _mapping(
            decision_match,
            field=f"{field}.decision_match",
        )
        reject_active_match(
            decision_source,
            match_field=f"{field}.decision_match",
        )
        if not canonical_matches or decision_source != canonical_matches[0]:
            raise ContractError(
                f"{field} decision match differs from its retained candidate"
            )
    copied["active_candidate_count"] = 0
    copied["busy_reason"] = None
    if compact:
        def compact_match(match: Mapping[str, Any]) -> dict[str, object]:
            visibility = _mapping(
                match.get("visibility"),
                field=f"{field}.compact_match.visibility",
            )
            generation = match.get("generation_binding")
            generation_source = (
                _mapping(
                    generation,
                    field=f"{field}.compact_match.generation_binding",
                )
                if generation is not None
                else {}
            )
            rejected_by = visibility.get("rejected_by")
            if not isinstance(rejected_by, Sequence) or isinstance(
                rejected_by,
                (str, bytes, bytearray),
            ):
                raise ContractError(
                    f"{field} match visibility rejections are invalid"
                )
            return {
                "candidate_index": match["candidate_index"],
                "selector": str(match.get("selector") or ""),
                "visibility": {
                    "rendered": visibility.get("rendered") is True,
                    "active": False,
                    "rejected_by": _strict_json_copy(
                        rejected_by,
                        field=f"{field}.compact_match.rejected_by",
                    ),
                },
                "generation_binding": {
                    "labelled_generation": (
                        generation_source.get("labelled_generation") is True
                    ),
                    "selected_surface_state": (
                        generation_source.get("selected_surface_state") is True
                    ),
                },
            }

        compact_matches = [
            compact_match(match) for match in canonical_matches
        ]
        result: dict[str, object] = {
            "active_candidate_count": 0,
            "busy_reason": None,
            "decision_match": (
                compact_matches[0] if decision_source is not None else None
            ),
            "matches": compact_matches,
            "selected_root": copied["selected_root"],
            "root_selection": root_selection,
        }
        for key in (
            "scope",
            "trusted_root_present",
            "candidate_count",
            "evidence_available",
        ):
            if key in copied:
                result[key] = copied[key]
        result["truncated_candidate_count"] = truncated_count
        return result
    return copied


def _canonical_marker(
    value: object,
    *,
    selected_root_key: tuple[str, str, str],
    accepted: bool,
    field: str,
    compact: bool = False,
) -> dict[str, object]:
    source = _mapping(value, field=field)
    marker = str(source.get("exact_normalized_marker") or "")
    if marker not in GAIO_NOT_SHOWN_MARKERS:
        raise ContractError(f"{field} lacks an exact supported marker")
    if source.get("accepted") is not accepted:
        raise ContractError(f"{field}.accepted is inconsistent")
    if source.get("selector") != '[role="status"]':
        raise ContractError(f"{field} must use the exact status selector")
    matched = _mapping(source.get("matched_node"), field=f"{field}.matched_node")
    _required_text(
        matched.get("dom_path"),
        field=f"{field}.matched_node.dom_path",
    )
    _required_text(matched.get("tag"), field=f"{field}.matched_node.tag")
    if str(matched.get("normalized_text") or "") != marker:
        raise ContractError(
            f"{field} marker text is not bound to the matched node"
        )
    attributes = _mapping(
        source.get("relevant_attributes"),
        field=f"{field}.relevant_attributes",
    )
    if str(attributes.get("role") or "").casefold() != "status":
        raise ContractError(f"{field} marker node must have role=status")
    visibility = _mapping(
        source.get("rendered_visibility"),
        field=f"{field}.rendered_visibility",
    )
    if visibility.get("rendered") is not True:
        raise ContractError(f"{field} marker is not rendered")
    rejected = visibility.get("rejected_by")
    if not isinstance(rejected, Sequence) or isinstance(
        rejected, (str, bytes, bytearray)
    ) or len(rejected) != 0:
        raise ContractError(f"{field} marker has visibility rejections")
    bounds = _mapping(
        visibility.get("rendered_bounds"),
        field=f"{field}.rendered_visibility.rendered_bounds",
    )
    if (
        bounds.get("positive") is not True
        or _finite_number(
            bounds.get("width"),
            field=f"{field}.rendered_visibility.rendered_bounds.width",
        )
        <= 0
        or _finite_number(
            bounds.get("height"),
            field=f"{field}.rendered_visibility.rendered_bounds.height",
        )
        <= 0
    ):
        raise ContractError(f"{field} marker bounds are not positive")
    if _root_key(source.get("selected_root")) != selected_root_key:
        raise ContractError(f"{field} is bound to a different root")
    nested_root_selection, nested_root_key = _canonical_root_selection(
        source.get("root_selection"),
        field=f"{field}.root_selection",
        compact=True,
    )
    if nested_root_key != selected_root_key:
        raise ContractError(f"{field} root selection is inconsistent")
    # Top-level marker evidence may carry the two full probes for correlation.
    # Validate those separately below; copying them recursively here would
    # duplicate the whole proof tree and defeat the evidence-size bound.
    marker_fields = {
        key: item
        for key, item in source.items()
        if key
        not in {
            "stability_probes",
            "query_binding",
            "browser_target_identity_binding",
        }
    }
    copied = _strict_json_copy(marker_fields, field=field)
    assert isinstance(copied, dict)
    copied["selected_root"] = _compact_selected_root(
        copied.get("selected_root"),
        field=f"{field}.selected_root",
        include_visibility=False,
    )
    copied["root_selection"] = nested_root_selection
    if compact:
        copied_matched = _mapping(
            copied.get("matched_node"),
            field=f"{field}.matched_node",
        )
        copied_visibility = _mapping(
            copied.get("rendered_visibility"),
            field=f"{field}.rendered_visibility",
        )
        copied_bounds = _mapping(
            copied_visibility.get("rendered_bounds"),
            field=f"{field}.rendered_visibility.rendered_bounds",
        )
        result: dict[str, object] = {
            "accepted": accepted,
            "exact_normalized_marker": marker,
            "selector": '[role="status"]',
            "matched_node": {
                "dom_path": _required_text(
                    copied_matched.get("dom_path"),
                    field=f"{field}.matched_node.dom_path",
                ),
                "tag": _required_text(
                    copied_matched.get("tag"),
                    field=f"{field}.matched_node.tag",
                ),
                "normalized_text": marker,
            },
            "relevant_attributes": {"role": "status"},
            "rendered_visibility": {
                "rendered": True,
                "rendered_bounds": {
                    "width": _finite_number(
                        copied_bounds.get("width"),
                        field=(
                            f"{field}.rendered_visibility."
                            "rendered_bounds.width"
                        ),
                    ),
                    "height": _finite_number(
                        copied_bounds.get("height"),
                        field=(
                            f"{field}.rendered_visibility."
                            "rendered_bounds.height"
                        ),
                    ),
                    "positive": True,
                },
                "rejected_by": [],
            },
            "selected_root": copied["selected_root"],
            "root_selection": nested_root_selection,
        }
        for key in ("selected_root_scope", "captured_at_epoch_ms"):
            if key in copied:
                result[key] = copied[key]
        return result
    return copied


def _probe_summary(
    probe: Mapping[str, object],
) -> dict[str, object]:
    query = _mapping(probe["query_binding"], field="probe.query_binding")
    identity = _mapping(
        probe["browser_target_identity_binding"],
        field="probe.browser_target_identity_binding",
    )
    return {
        "probe_index": probe["probe_index"],
        "captured_at_epoch_ms": probe["captured_at_epoch_ms"],
        "capture_state": probe["capture_state"],
        "query_sha256": query["query_sha256"],
        "browser_target_reference": identity[
            "browser_target_reference"
        ],
    }


def _canonical_nested_probe_summary(
    value: object,
    *,
    field: str,
) -> dict[str, object]:
    source = _mapping(value, field=field)
    query_binding = source.get("query_binding")
    identity_binding = source.get("browser_target_identity_binding")
    if query_binding is not None and identity_binding is not None:
        query = _canonical_query_binding(query_binding)
        identity = _canonical_identity(identity_binding)
        query_sha256 = query["query_sha256"]
        target_reference = identity["browser_target_reference"]
    else:
        query_sha256 = _required_sha256(
            source.get("query_sha256"),
            field=f"{field}.query_sha256",
        )
        target_reference = _required_text(
            source.get("browser_target_reference"),
            field=f"{field}.browser_target_reference",
        )
    probe_index = source.get("probe_index")
    if (
        isinstance(probe_index, bool)
        or not isinstance(probe_index, int)
        or probe_index not in {1, 2}
    ):
        raise ContractError(f"{field}.probe_index is invalid")
    return {
        "probe_index": probe_index,
        "captured_at_epoch_ms": _finite_number(
            source.get("captured_at_epoch_ms"),
            field=f"{field}.captured_at_epoch_ms",
        ),
        "capture_state": _required_text(
            source.get("capture_state"),
            field=f"{field}.capture_state",
        ),
        "query_sha256": query_sha256,
        "browser_target_reference": target_reference,
    }


def canonical_gaio_terminal_evidence(
    value: object,
    *,
    expected_state: str,
    expected_prompt_sha256: Optional[str] = None,
    expected_identity: Optional[Mapping[str, object]] = None,
    expected_response_sha256: Optional[str] = None,
) -> dict[str, object]:
    """Validate and defensively canonicalize one successful GAIO capture."""

    if expected_state not in {"shown", "not_shown"}:
        raise ContractError("expected GAIO terminal state is invalid")
    source = _mapping(value, field="GAIO terminal evidence")
    if source.get("schema_version") != GAIO_TERMINAL_CAPTURE_EVIDENCE_VERSION:
        raise ContractError("GAIO terminal evidence schema is invalid")
    if source.get("capture_state") != expected_state:
        raise ContractError("GAIO terminal evidence state is inconsistent")
    if not _empty_optional_text(source.get("failure_classification")):
        raise ContractError("successful GAIO evidence contains a failure")
    if not _empty_optional_text(source.get("busy_reason")):
        raise ContractError("successful GAIO evidence remains busy")

    root_selection, selected_root_key = _canonical_root_selection(
        source.get("root_selection"),
        field="root_selection",
    )
    busy_evidence = _canonical_busy_evidence(
        source.get("busy_evidence"),
        selected_root_key=selected_root_key,
        field="busy_evidence",
    )
    query_binding = _canonical_query_binding(source.get("query_binding"))
    prompt_sha256 = str(query_binding["query_sha256"])
    if (
        expected_prompt_sha256 is not None
        and prompt_sha256
        != _required_sha256(
            expected_prompt_sha256,
            field="expected_prompt_sha256",
        )
    ):
        raise ContractError("GAIO evidence is bound to a different prompt")
    identity_binding = _canonical_identity(
        source.get("browser_target_identity_binding")
    )
    if expected_identity is not None:
        expected_canonical_identity = _canonical_identity(expected_identity)
        if identity_binding != expected_canonical_identity:
            raise ContractError(
                "GAIO evidence is bound to a different browser target"
            )
    if (
        identity_binding["conversation_reference_sha256"]
        != query_binding["document_url_sha256"]
    ):
        raise ContractError(
            "GAIO target conversation is not the captured search URL"
        )

    raw_probes = source.get("stability_probes")
    if not isinstance(raw_probes, Sequence) or isinstance(
        raw_probes, (str, bytes, bytearray)
    ) or len(raw_probes) != 2:
        raise ContractError("GAIO terminal evidence requires exactly two probes")
    probes: list[dict[str, object]] = []
    shown_response_sha256: Optional[str] = None
    marker_key: Optional[tuple[str, str, str]] = None
    for index, raw_probe in enumerate(raw_probes, start=1):
        probe_source = _mapping(
            raw_probe,
            field=f"stability_probes[{index - 1}]",
        )
        if probe_source.get("probe_index") != index:
            raise ContractError("GAIO stability probes are not ordered")
        if probe_source.get("capture_state") != expected_state:
            raise ContractError("GAIO stability probe state is inconsistent")
        if not _empty_optional_text(
            probe_source.get("failure_classification")
        ):
            raise ContractError(
                "successful GAIO stability probe contains a failure"
            )
        captured_at = _finite_number(
            probe_source.get("captured_at_epoch_ms"),
            field=f"stability_probes[{index - 1}].captured_at_epoch_ms",
        )
        if probe_source.get("busy") is not False:
            raise ContractError("GAIO terminal probe remains busy")
        if not _empty_optional_text(probe_source.get("busy_reason")):
            raise ContractError("GAIO terminal probe has a busy reason")
        probe_root, probe_root_key = _canonical_root_selection(
            probe_source.get("root_selection"),
            field=f"stability_probes[{index - 1}].root_selection",
            compact=True,
        )
        if probe_root_key != selected_root_key:
            raise ContractError("GAIO stability probe changed roots")
        probe_busy = _canonical_busy_evidence(
            probe_source.get("busy_evidence"),
            selected_root_key=selected_root_key,
            field=f"stability_probes[{index - 1}].busy_evidence",
            compact=True,
        )
        probe_query = _canonical_query_binding(
            probe_source.get("query_binding")
        )
        if probe_query != query_binding:
            raise ContractError("GAIO stability probe changed query binding")
        probe_identity = _canonical_identity(
            probe_source.get("browser_target_identity_binding")
        )
        if probe_identity != identity_binding:
            raise ContractError("GAIO stability probe changed browser target")

        probe: dict[str, object] = {
            "probe_index": index,
            "captured_at_epoch_ms": captured_at,
            "capture_state": expected_state,
            "busy": False,
            "busy_reason": None,
            "busy_evidence": probe_busy,
            "root_selection": probe_root,
            "query_binding": probe_query,
            "browser_target_identity_binding": probe_identity,
            "failure_classification": None,
        }
        for optional_number in (
            "checked_at_monotonic_seconds",
            "probe_ordinal",
        ):
            if optional_number in probe_source:
                probe[optional_number] = _finite_number(
                    probe_source[optional_number],
                    field=(
                        f"stability_probes[{index - 1}].{optional_number}"
                    ),
                )
        for optional_bool in (
            "identity_valid",
            "expected_query_present",
        ):
            if optional_bool in probe_source:
                if probe_source[optional_bool] is not True:
                    raise ContractError(
                        f"stability probe {optional_bool} must be true"
                    )
                probe[optional_bool] = True
        for optional_mapping in (
            "response_baseline",
            "terminal_evidence",
        ):
            if probe_source.get(optional_mapping) is not None:
                probe[optional_mapping] = _strict_json_copy(
                    probe_source[optional_mapping],
                    field=(
                        f"stability_probes[{index - 1}]."
                        f"{optional_mapping}"
                    ),
                )
        if expected_state == "shown":
            response_sha256 = _required_sha256(
                probe_source.get("response_sha256"),
                field=f"stability_probes[{index - 1}].response_sha256",
            )
            if (
                shown_response_sha256 is not None
                and response_sha256 != shown_response_sha256
            ):
                raise ContractError("GAIO shown probes changed response text")
            shown_response_sha256 = response_sha256
            probe["response_sha256"] = response_sha256
            stale_marker = probe_source.get("not_shown_evidence")
            if stale_marker is not None:
                probe["not_shown_evidence"] = _canonical_marker(
                    stale_marker,
                    selected_root_key=selected_root_key,
                    accepted=False,
                    field=(
                        f"stability_probes[{index - 1}]."
                        "not_shown_evidence"
                    ),
                    compact=True,
                )
        else:
            marker = _canonical_marker(
                probe_source.get("not_shown_evidence"),
                selected_root_key=selected_root_key,
                accepted=True,
                field=(
                    f"stability_probes[{index - 1}].not_shown_evidence"
                ),
                compact=True,
            )
            current_marker_key = _root_key(marker.get("matched_node"))
            if marker_key is not None and current_marker_key != marker_key:
                raise ContractError("GAIO not-shown marker changed between probes")
            marker_key = current_marker_key
            probe["not_shown_evidence"] = marker
        probes.append(probe)

    first_monotonic = probes[0].get("checked_at_monotonic_seconds")
    second_monotonic = probes[1].get("checked_at_monotonic_seconds")
    if first_monotonic is not None and second_monotonic is not None:
        first_timestamp = float(first_monotonic)
        second_timestamp = float(second_monotonic)
    else:
        first_timestamp = float(probes[0]["captured_at_epoch_ms"])
        second_timestamp = float(probes[1]["captured_at_epoch_ms"])
    if second_timestamp <= first_timestamp:
        raise ContractError("GAIO stability probe timestamps are unordered")

    if expected_state == "shown":
        expected_digest = (
            _required_sha256(
                expected_response_sha256,
                field="expected_response_sha256",
            )
            if expected_response_sha256 is not None
            else shown_response_sha256
        )
        if shown_response_sha256 != expected_digest:
            raise ContractError("GAIO evidence changed the shown response")
        top_marker = source.get("not_shown_evidence")
        not_shown_evidence = (
            _canonical_marker(
                top_marker,
                selected_root_key=selected_root_key,
                accepted=False,
                field="not_shown_evidence",
            )
            if top_marker is not None
            else None
        )
    else:
        not_shown_evidence = _canonical_marker(
            source.get("not_shown_evidence"),
            selected_root_key=selected_root_key,
            accepted=True,
            field="not_shown_evidence",
        )
        nested_query = source["not_shown_evidence"].get("query_binding")
        nested_identity = source["not_shown_evidence"].get(
            "browser_target_identity_binding"
        )
        if (
            nested_query is not None
            and _canonical_query_binding(nested_query) != query_binding
        ):
            raise ContractError("GAIO marker changed query binding")
        if (
            nested_identity is not None
            and _canonical_identity(nested_identity) != identity_binding
        ):
            raise ContractError("GAIO marker changed browser target")
        nested_probes = source["not_shown_evidence"].get(
            "stability_probes"
        )
        if not isinstance(nested_probes, Sequence) or isinstance(
            nested_probes, (str, bytes, bytearray)
        ) or len(nested_probes) != 2:
            raise ContractError(
                "GAIO marker evidence requires both stability probes"
            )
        expected_nested_probes = [
            _probe_summary(probe) for probe in probes
        ]
        observed_nested_probes = [
            _canonical_nested_probe_summary(
                probe,
                field=f"not_shown_evidence.stability_probes[{index}]",
            )
            for index, probe in enumerate(nested_probes)
        ]
        if observed_nested_probes != expected_nested_probes:
            raise ContractError(
                "GAIO marker stability probes differ from terminal probes"
            )
        not_shown_evidence["stability_probes"] = expected_nested_probes
        not_shown_evidence["query_binding"] = query_binding
        not_shown_evidence[
            "browser_target_identity_binding"
        ] = identity_binding

    canonical = {
        "schema_version": GAIO_TERMINAL_CAPTURE_EVIDENCE_VERSION,
        "capture_state": expected_state,
        "failure_classification": None,
        "busy_reason": None,
        "busy_evidence": busy_evidence,
        "not_shown_evidence": not_shown_evidence,
        "root_selection": root_selection,
        "stability_probes": probes,
        "query_binding": query_binding,
        "browser_target_identity_binding": identity_binding,
    }
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_GAIO_TERMINAL_EVIDENCE_BYTES:
        raise ContractError("GAIO terminal evidence exceeds its byte bound")
    return canonical


__all__ = [
    "GAIO_GENERATED_ANSWER_CAPTURE_VERSION",
    "GAIO_NOT_SHOWN_MARKERS",
    "GAIO_TERMINAL_ERROR_MARKERS",
    "GAIO_TERMINAL_CAPTURE_EVIDENCE_VERSION",
    "MAX_GAIO_RECONCILIATION_EVIDENCE_BYTES",
    "MAX_GAIO_TERMINAL_EVIDENCE_BYTES",
    "build_gaio_query_binding",
    "canonical_gaio_identity_binding",
    "canonical_gaio_reconciliation_evidence",
    "canonical_gaio_retry_authorization_evidence",
    "canonical_gaio_terminal_evidence",
]
