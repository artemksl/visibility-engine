"""Strict, content-free correlation for revisiting an existing conversation.

URL recognition belongs to the website's registered specification. Google
search does not supply a conversation prefix and cannot pass this contract.
The transport supplies inspected message digests and optional message/branch
identities; this module never navigates, sends or treats absence as no send.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit


class ConversationCorrespondenceError(ValueError):
    """A safe reason code for an unverifiable or changed conversation."""


def canonical_conversation_url(value: object, prefixes: Sequence[str]) -> str:
    if not isinstance(value, str):
        raise ConversationCorrespondenceError("conversation_id_missing")
    try:
        parsed = urlsplit(value)
        if parsed.username or parsed.password or parsed.scheme != "https" or parsed.port not in (None, 443):
            raise ValueError
        for prefix in prefixes:
            allowed = urlsplit(prefix)
            boundary = allowed.path.rstrip("/") + "/"
            suffix = parsed.path[len(boundary):] if parsed.path.startswith(boundary) else ""
            if parsed.hostname == allowed.hostname and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", suffix):
                return urlunsplit(("https", parsed.hostname, parsed.path, "", ""))
    except ValueError:
        pass
    raise ConversationCorrespondenceError("conversation_id_missing_or_invalid")


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digests(value: object, *, reason: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(row, str) or not re.fullmatch(r"[0-9a-f]{64}", row) for row in value):
        raise ConversationCorrespondenceError(reason)
    return tuple(value)


def is_temporary_message_id(value: object, prefixes: Sequence[str]) -> bool:
    """True when ``value`` is a message id the website had not settled yet.

    Such an id was shown only while the answer was being written and was
    replaced afterwards. It identifies nothing durable, so it counts as unknown.
    """
    if not isinstance(value, str) or not value:
        return False
    return any(isinstance(prefix, str) and prefix and value.startswith(prefix) for prefix in prefixes)


def logical_response_baseline(binding: Mapping[str, object]) -> Mapping[str, object]:
    """Keep the polling snapshot separate from a native retry's logical turn.

    Older bindings use their physical snapshot for both purposes. Regeneration
    may recapture the failed wrapper but adds no user message; its optional
    logical snapshot still describes the history before that user message.
    """
    physical = binding.get("response_baseline")
    if not isinstance(physical, Mapping):
        raise ConversationCorrespondenceError("conversation_baseline_missing")
    logical = binding.get("logical_response_baseline", physical)
    for baseline in (physical, logical):
        if not isinstance(baseline, Mapping):
            raise ConversationCorrespondenceError("conversation_baseline_invalid")
        count = baseline.get("visible_response_count")
        digest = baseline.get("newest_response_sha256")
        if (type(count) is not int or count < 0 or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or (count == 0 and digest != text_digest(""))):
            raise ConversationCorrespondenceError("conversation_baseline_invalid")
    count = logical["visible_response_count"]
    if physical["visible_response_count"] not in {count, count + 1}:
        raise ConversationCorrespondenceError("conversation_baseline_invalid")
    if physical["visible_response_count"] == count and physical != logical:
        raise ConversationCorrespondenceError("conversation_baseline_invalid")
    if type(binding.get("expected_turn")) is not int or binding["expected_turn"] != count + 1:
        raise ConversationCorrespondenceError("conversation_expected_turn_invalid")
    return logical


def verify_conversation_history(
    binding: Mapping[str, object],
    observed: Mapping[str, object],
    *,
    prefixes: Sequence[str],
    prompt_sha256: str,
    expected_profile_id: str,
    expected_chrome_directory: str,
    temporary_message_id_prefixes: Sequence[str] = (),
) -> dict[str, object]:
    """Prove the exact turn before accepting a response or continuing a turn.

    Both mappings contain identity and transcript checkpoints. Transcript keys
    are user_sha256, assistant_sha256, user_ids, assistant_ids, branch_id,
    history_complete and generating. Baseline assistant digests exclude the
    awaited answer. Newly observed partial answer text is never an accepted
    result. Missing/inaccessible/extra history is an explicit outcome.

    A saved message id that starts with one of ``temporary_message_id_prefixes``
    was recorded while the website was still writing that message; it is treated
    as unknown (like ``None``) and not compared. Every other saved id must match
    the observed id at the same position.
    """
    if binding.get("schema_version") != "browser-conversation-binding-v1":
        raise ConversationCorrespondenceError("conversation_binding_unsupported")
    expected = binding.get("identity")
    actual = observed.get("identity")
    if not isinstance(expected, Mapping) or not isinstance(actual, Mapping):
        raise ConversationCorrespondenceError("conversation_identity_unavailable")
    for key, configured in (("profile_id", expected_profile_id), ("chrome_directory", expected_chrome_directory)):
        if expected.get(key) != configured or actual.get(key) != configured:
            raise ConversationCorrespondenceError("conversation_profile_mismatch")
    if not expected.get("browser_context_reference") or actual.get("browser_context_reference") != expected.get("browser_context_reference"):
        raise ConversationCorrespondenceError("conversation_account_context_mismatch")
    saved_url = canonical_conversation_url(expected.get("conversation_reference"), prefixes)
    current_url = canonical_conversation_url(actual.get("conversation_reference"), prefixes)
    if saved_url != current_url:
        raise ConversationCorrespondenceError("conversation_identity_mismatch")
    if binding.get("prompt_sha256") != prompt_sha256 or not re.fullmatch(r"[0-9a-f]{64}", prompt_sha256):
        raise ConversationCorrespondenceError("conversation_prompt_binding_mismatch")
    baseline = logical_response_baseline(binding)
    count = baseline["visible_response_count"]
    transcript = observed.get("transcript")
    saved = binding.get("transcript")
    if not isinstance(transcript, Mapping) or not isinstance(saved, Mapping) or transcript.get("history_complete") is not True:
        raise ConversationCorrespondenceError("conversation_history_unavailable")
    users = _digests(transcript.get("user_sha256"), reason="conversation_user_history_unavailable")
    assistants = _digests(transcript.get("assistant_sha256"), reason="conversation_answer_history_unavailable")
    if len(users) != count + 1 or len(assistants) not in {count, count + 1}:
        raise ConversationCorrespondenceError("conversation_extra_or_missing_turns")
    if not users or users[-1] != prompt_sha256:
        raise ConversationCorrespondenceError("conversation_prompt_mismatch")
    prior_users = _digests(saved.get("prior_user_sha256", ()), reason="conversation_baseline_invalid")
    prior_assistants = _digests(saved.get("prior_assistant_sha256", ()), reason="conversation_baseline_invalid")
    if count and (len(prior_users) != count or len(prior_assistants) != count):
        raise ConversationCorrespondenceError("conversation_baseline_incomplete")
    if users[:count] != prior_users or assistants[:count] != prior_assistants:
        raise ConversationCorrespondenceError("conversation_prior_history_changed")
    if count and assistants[count - 1] != baseline.get("newest_response_sha256"):
        raise ConversationCorrespondenceError("conversation_baseline_changed")
    for key in ("user_ids", "assistant_ids"):
        stored_ids = saved.get(key, ())
        current_ids = transcript.get(key, ())
        if not isinstance(stored_ids, (tuple, list)) or not isinstance(current_ids, (tuple, list)):
            raise ConversationCorrespondenceError("conversation_message_identity_invalid")
        for index, identity in enumerate(stored_ids):
            if identity is None or is_temporary_message_id(identity, temporary_message_id_prefixes):
                continue
            if not isinstance(identity, str) or not identity or index >= len(current_ids) or current_ids[index] != identity:
                raise ConversationCorrespondenceError("conversation_message_or_branch_changed")
    branch = saved.get("branch_id")
    if branch is not None and transcript.get("branch_id") != branch:
        raise ConversationCorrespondenceError("conversation_branch_changed")
    if transcript.get("branch_changed") is True or transcript.get("history_ambiguous") is True:
        raise ConversationCorrespondenceError("conversation_branch_or_history_ambiguous")
    return {
        "schema_version": "browser-conversation-correspondence-v1",
        "conversation_url": saved_url,
        "expected_turn": count + 1,
        "correspondence_verified": True,
        "response_present": len(assistants) == count + 1,
        "generating": transcript.get("generating") is True,
        "new_tab_reference": actual.get("tab_reference"),
        "new_target_reference": actual.get("browser_target_reference"),
    }
