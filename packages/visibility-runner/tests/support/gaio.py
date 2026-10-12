"""Strict offline fixtures for Google AI Overview terminal evidence."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Literal

from visibility_runner.gaio_evidence import (
    GAIO_TERMINAL_CAPTURE_EVIDENCE_VERSION,
    build_gaio_query_binding,
    canonical_gaio_identity_binding,
    canonical_gaio_terminal_evidence,
)
from visibility_runner.websites.chat_actions import BrowserIdentity
from visibility_runner.gaio_compatibility import google_search_url
from visibility_runner.gaio_compatibility import (
    _google_reconciliation_evidence,
)


def gaio_response_identity(
    identity: BrowserIdentity,
    prompt: str,
) -> BrowserIdentity:
    """Bind a fake tab to the exact post-navigation search target."""

    return replace(
        identity,
        browser_target_reference=(
            identity.browser_target_reference
            or f"target:{identity.tab_reference}"
        ),
        conversation_reference=google_search_url(prompt),
    )


def fake_gaio_terminal_evidence(
    identity: BrowserIdentity,
    prompt: str,
    *,
    state: Literal["shown", "not_shown"],
    text: str = "",
) -> dict[str, object]:
    """Return validator-produced evidence for isolated website fakes."""

    root_visibility = {
        "connected": True,
        "rendered": True,
        "rendered_bounds": {
            "x": 0,
            "y": 0,
            "width": 640,
            "height": 240,
            "positive": True,
        },
        "client_rect_count": 1,
        "in_viewport": True,
        "rejected_by": [],
    }
    root = {
        "dom_path": "html > body > div",
        "tag": "div",
        "selector": '[data-attrid*="ai-overview"]',
        "attributes": {"data_attrid": "ai-overview"},
        "visibility": root_visibility,
    }
    root_selection = {
        "status": "selected",
        "reason": "unique_visible_exact_heading_root",
        "candidate_count": 1,
        "visible_candidate_count": 1,
        "exact_visible_heading_count": 1,
        "selected_root": root,
        "candidates": [root],
        "truncated_candidate_count": 0,
    }
    busy_evidence = {
        "scope": "selected_visible_ai_overview_root",
        "trusted_root_present": True,
        "candidate_count": 0,
        "active_candidate_count": 0,
        "decision_match": None,
        "matches": [],
        "truncated_candidate_count": 0,
        "busy_reason": None,
        "selected_root": root,
        "root_selection": root_selection,
        "evidence_available": True,
    }
    marker = {
        "accepted": state == "not_shown",
        "exact_normalized_marker": (
            "an ai overview is not available for this search"
        ),
        "selector": '[role="status"]',
        "matched_node": {
            "dom_path": "html > body > div > div",
            "tag": "div",
            "normalized_text": (
                "an ai overview is not available for this search"
            ),
        },
        "relevant_attributes": {"role": "status"},
        "rendered_visibility": {
            "rendered": True,
            "rendered_bounds": {
                "width": 320,
                "height": 24,
                "positive": True,
            },
            "rejected_by": [],
        },
        "selected_root": root,
        "selected_root_scope": "unique_visible_exact_heading_root",
        "root_selection": root_selection,
        "captured_at_epoch_ms": 1_000,
    }
    expected_url = google_search_url(prompt)
    query_binding = build_gaio_query_binding(
        prompt=prompt,
        expected_url=expected_url,
        document_url=expected_url,
        document_url_source="capture_script",
        hl="en",
        gl="gb",
    )
    identity_binding = canonical_gaio_identity_binding(
        identity.to_mapping()
    )
    response_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    probes: list[dict[str, object]] = []
    for ordinal in (1, 2):
        probe: dict[str, object] = {
            "probe_index": ordinal,
            "probe_ordinal": ordinal,
            "capture_state": state,
            "checked_at_monotonic_seconds": float(ordinal),
            "captured_at_epoch_ms": 1_000 + ordinal,
            "busy": False,
            "busy_reason": None,
            "busy_evidence": busy_evidence,
            "root_selection": root_selection,
            "query_binding": query_binding,
            "browser_target_identity_binding": identity_binding,
            "failure_classification": None,
        }
        if state == "shown":
            probe["response_sha256"] = response_sha256
        else:
            probe["not_shown_evidence"] = marker
        probes.append(probe)
    raw = {
        "schema_version": GAIO_TERMINAL_CAPTURE_EVIDENCE_VERSION,
        "capture_state": state,
        "failure_classification": None,
        "busy_reason": None,
        "busy_evidence": busy_evidence,
        "not_shown_evidence": (
            {
                **marker,
                "stability_probes": probes,
                "query_binding": query_binding,
                "browser_target_identity_binding": identity_binding,
            }
            if state == "not_shown"
            else None
        ),
        "root_selection": root_selection,
        "stability_probes": probes,
        "query_binding": query_binding,
        "browser_target_identity_binding": identity_binding,
    }
    return canonical_gaio_terminal_evidence(
        raw,
        expected_state=state,
        expected_prompt_sha256=hashlib.sha256(
            prompt.encode("utf-8")
        ).hexdigest(),
        expected_identity=identity.to_mapping(),
        expected_response_sha256=(
            response_sha256 if state == "shown" else None
        ),
    )


def fake_gaio_retry_authorization_evidence(
    identity: BrowserIdentity,
    prompt: str,
    *,
    response_baseline: dict[str, object],
    marker: str = "something went wrong",
) -> dict[str, object]:
    """Return repeated rendered terminal-error proof for an offline retry."""

    terminal_shape = fake_gaio_terminal_evidence(
        identity,
        prompt,
        state="shown",
        text="terminal-error-placeholder",
    )
    selected_root = terminal_shape["root_selection"]["selected_root"]
    terminal_failure = f"google_terminal_error:{marker}"
    checks: list[dict[str, object]] = []
    for ordinal in (1, 2):
        terminal_evidence = {
            "selector": '[role="alert"], .g-error-card',
            "matched_node": {
                "dom_path": (
                    f"{selected_root['dom_path']} > div[role=alert]"
                ),
                "tag": "div",
                "attributes": {"role": "alert"},
                "normalized_text": marker,
            },
            "rendered_visibility": {
                "rendered": True,
                "rendered_bounds": {
                    "width": 320,
                    "height": 24,
                    "positive": True,
                },
                "rejected_by": [],
            },
            "selected_root": selected_root,
            "captured_at_epoch_ms": 1_000 + ordinal,
        }
        checks.append(
            {
                "checked_at_monotonic_seconds": float(ordinal),
                "captured_at_epoch_ms": 1_000 + ordinal,
                "probe_ordinal": ordinal,
                "identity_valid": True,
                "expected_query_present": True,
                "expected_prompt_sha256": hashlib.sha256(
                    prompt.encode("utf-8")
                ).hexdigest(),
                "query_binding": terminal_shape["query_binding"],
                "browser_target_identity_binding": terminal_shape[
                    "browser_target_identity_binding"
                ],
                "response_baseline": dict(response_baseline),
                "capture_state": "terminal_error",
                "busy": False,
                "busy_reason": None,
                "busy_evidence": terminal_shape["busy_evidence"],
                "failure_classification": terminal_failure,
                "root_selection": terminal_shape["root_selection"],
                "terminal_evidence": terminal_evidence,
            }
        )
    return _google_reconciliation_evidence(
        1.25,
        checks,
        "stable_google_terminal_error",
        stable_terminal_checks=2,
    )


__all__ = [
    "fake_gaio_retry_authorization_evidence",
    "fake_gaio_terminal_evidence",
    "gaio_response_identity",
]
