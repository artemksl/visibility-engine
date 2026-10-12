"""Interpret text from an already proven ChatGPT provider notice."""

from __future__ import annotations

import re
from typing import Any, Mapping


_LIMIT_REJECTION = re.compile(
    r"\b(?:hit|reached|exceeded)\s+(?:(?:your|the|a)\s+)?"
    r"(?:[\w-]+\s+){0,4}limit\b",
    re.IGNORECASE,
)
_LIMIT = re.compile(
    _LIMIT_REJECTION.pattern
    + r"|\b(?:usage|messages?|text|files?|images?|uploads?|data analysis)\s+limit\b"
    r"|\b(?:usage|limit|quota|allowance)\s+(?:will\s+)?resets?\b",
    re.IGNORECASE,
)
_TOOLS = re.compile(r"\b(?:files?|images?|uploads?|data analysis)\b", re.IGNORECASE)
_TEXT_LIMIT = re.compile(
    r"\b(?:messages?|text(?:\s+messages?)?|chat(?:ting)?)\s+(?:usage\s+)?limit\b"
    r"|\blimit\s+(?:for|on)\s+(?:messages?|text|chatting)\b",
    re.IGNORECASE,
)
_TEXT_ALLOWED = re.compile(
    r"(?:^|[.!?]\s+)(?:continue|keep)\s+chatting\s+(?:with|using)\s+text\b"
    r"|\byou\s+(?:can|may)\s+(?:continue|keep)\s+chatting\s+(?:with|using)\s+text\b"
    r"|\btext(?:[- ]only)?\s+(?:chatting|messages?|chat)\s+(?:is|are)\s+"
    r"(?:still\s+)?(?:available|allowed)\b",
    re.IGNORECASE,
)
_RESET_DISPLAY_PATTERN = (
    r"(?:(?P<day_before>today|tomorrow)\s+)?(?:at\s+)?"
    r"(?P<clock>\d{1,2}(?::\d{2})?(?:\s*[ap]m)?)"
    r"(?![\w:])(?:\s+(?P<day_after>today|tomorrow))?"
)
_RESET_DISPLAY = re.compile(_RESET_DISPLAY_PATTERN, re.IGNORECASE)
_RESET = re.compile(
    r"\b(?:resets?|try\s+again)\s+(?P<display>" + _RESET_DISPLAY_PATTERN + ")",
    re.IGNORECASE,
)
_CLOCK = re.compile(r"(\d{1,2})(?::(\d{2}))?(?:\s*([ap]m))?", re.IGNORECASE)


def _normalized_clock(display: str) -> str | None:
    match = _CLOCK.fullmatch(display)
    if match is None:
        return None
    hour = int(match[1])
    minute = int(match[2] or 0)
    meridiem = (match[3] or "").lower()
    if minute > 59:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    elif match[2] is None or hour > 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def parse_usage_limit_reset_time(display: str) -> dict[str, object] | None:
    """Validate a displayed reset phrase; this does not classify a notice."""
    if not isinstance(display, str) or len(display) > 2000:
        return None
    match = _RESET_DISPLAY.fullmatch(display)
    if match is None:
        return None
    clock = _normalized_clock(match["clock"])
    before, after = match["day_before"], match["day_after"]
    if clock is None or (before and after and before.lower() != after.lower()):
        return None
    day = before or after
    return {
        "reset_day": day.lower() if day else None,
        "reset_clock": clock,
        "reset_display": display,
        "reset_timezone": None,
    }


def parse_chatgpt_usage_limit_notice(text: str) -> dict[str, object] | None:
    """Return display-clock metadata, without inferring a timezone or deadline.

    The caller must establish that the text belongs to a provider notice.
    Generic limits retain unknown scope. An explicit rejection with a try-again
    instruction is blocking without inferring which quota was exhausted.
    """
    if not isinstance(text, str) or not text or len(text) > 2000:
        return None
    matching_text = " ".join(text.split())
    if _LIMIT.search(matching_text) is None:
        return None

    allows_text = _TEXT_ALLOWED.search(matching_text) is not None
    if allows_text:
        scope = "tools" if _TOOLS.search(matching_text) else "unknown"
        text_allowed: bool | None = True
    elif _TEXT_LIMIT.search(matching_text):
        scope, text_allowed = "text", False
    elif _TOOLS.search(matching_text):
        scope, text_allowed = "tools", None
    else:
        scope, text_allowed = "unknown", None

    blocking = text_allowed is False or (
        text_allowed is not True
        and scope == "unknown"
        and _LIMIT_REJECTION.search(matching_text) is not None
        and re.search(r"\btry\s+again\b", matching_text, re.IGNORECASE) is not None
    )
    reset = {
        "reset_day": None,
        "reset_clock": None,
        "reset_display": None,
        "reset_timezone": None,
    }
    for match in _RESET.finditer(text):
        parsed = parse_usage_limit_reset_time(match["display"])
        if parsed is not None:
            reset = parsed
            break

    return {
        "notice_text": text,
        "scope": scope,
        "text_allowed": text_allowed,
        "blocking": blocking,
        **reset,
    }


def project_blocking_usage_limit(value: Any) -> dict[str, object] | None:
    """Expose validated provider clock facts, never notice text or inferred time."""
    if not isinstance(value, Mapping) or value.get("scope") not in ("text", "unknown") or value.get("blocking") is not True or value.get("text_allowed") is True:
        return None
    reset = None
    display = value.get("reset_display")
    if isinstance(display, str) and len(display) <= 100:
        parsed = parse_usage_limit_reset_time(display)
        if parsed is not None and all(parsed[key] == value.get(key) for key in ("reset_clock", "reset_day")):
            reset = parsed
    return {
        "scope": value["scope"], "blocking": True,
        "text_allowed": value.get("text_allowed") if type(value.get("text_allowed")) is bool else None,
        "reset_day": reset["reset_day"] if reset else None,
        "reset_clock": reset["reset_clock"] if reset else None,
        "reset_display": reset["reset_display"] if reset else None,
        "reset_timezone": None,
    }


def project_usage_limit_issue(issue: Any, *, profile_id: str, provider: str) -> dict[str, object] | None:
    """Project only a durable blocking usage limit for this profile/provider lane."""
    if not isinstance(issue, Mapping) or issue.get("code") not in ("needs_attention", "ambiguous_send"):
        return None
    detail = issue.get("detail")
    if not isinstance(detail, Mapping) or detail.get("user_action_required") is not True:
        return None
    block = detail.get("access_block")
    if not isinstance(block, Mapping) or block.get("profile_id") != profile_id or block.get("provider") != provider or block.get("reason") != "usage_limit":
        return None
    limit = project_blocking_usage_limit(block.get("usage_limit"))
    if limit is None:
        if isinstance(block.get("usage_limit"), Mapping):
            if block["usage_limit"].get("scope") == "tools" or block["usage_limit"].get("text_allowed") is True or block["usage_limit"].get("blocking") is False:
                return None
        limit = project_blocking_usage_limit({"scope": "unknown", "blocking": True})
    return {"limit_reason": "usage_limit", "usage_limit": limit}

