"""Neutral identity and data-layout contract for supported AI platforms."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional


CHATGPT = "chatgpt"
GEMINI = "gemini"
GOOGLE_AI_OVERVIEW = "google_ai_overview"
COPILOT = "copilot"
GITHUB_COPILOT = "github_copilot"


PLATFORM_LABELS = {
    CHATGPT: "ChatGPT",
    GEMINI: "Gemini",
    GOOGLE_AI_OVERVIEW: "Google AI Overview",
    COPILOT: "Copilot",
    GITHUB_COPILOT: "GitHub Copilot (SDK)",
}


PLATFORM_ALIASES = {
    "gpt": CHATGPT,
    "openai": CHATGPT,
    "chat_gpt": CHATGPT,
    "chat-gpt": CHATGPT,
    "google": GOOGLE_AI_OVERVIEW,
    "google_ai": GOOGLE_AI_OVERVIEW,
    "google-ai": GOOGLE_AI_OVERVIEW,
    "google_search_ai": GOOGLE_AI_OVERVIEW,
    "google-search-ai": GOOGLE_AI_OVERVIEW,
    "google_ai_search": GOOGLE_AI_OVERVIEW,
    "google-ai-search": GOOGLE_AI_OVERVIEW,
    "ai_overview": GOOGLE_AI_OVERVIEW,
    "ai-overview": GOOGLE_AI_OVERVIEW,
    "aio": GOOGLE_AI_OVERVIEW,
    "ms_copilot": COPILOT,
    "microsoft_copilot": COPILOT,
}


DEFAULT_AUDIT_PLATFORMS = (CHATGPT,)
FULL_AUDIT_PLATFORMS = (CHATGPT, GEMINI, GOOGLE_AI_OVERVIEW, COPILOT)
RETRYABLE_STATUSES = {"error", "timeout", "partial", "overview_empty"}


def normalize_platform(value: str) -> str:
    key = re.sub(r"[\s-]+", "_", str(value or "").strip().lower())
    key = PLATFORM_ALIASES.get(key, key)
    if key not in PLATFORM_LABELS:
        raise ValueError(
            f"Unknown audit platform '{value}'. Known: {', '.join(PLATFORM_LABELS)}"
        )
    return key


def parse_platforms(
    raw: Optional[object],
    *,
    default: Iterable[str] = DEFAULT_AUDIT_PLATFORMS,
) -> list[str]:
    if raw is None or raw == "":
        parts = list(default)
    elif isinstance(raw, str):
        parts = re.split(r"[,;\s]+", raw.strip())
    else:
        parts = list(raw)
    out: list[str] = []
    for part in parts:
        if part is None or str(part).strip() == "":
            continue
        key = normalize_platform(str(part))
        if key not in out:
            out.append(key)
    return out or list(default)


def platform_data_dir(
    base_data_dir: Path,
    platform: str,
    *,
    legacy_chatgpt: bool = True,
) -> Path:
    base_data_dir = Path(base_data_dir)
    if platform == CHATGPT and legacy_chatgpt:
        return base_data_dir
    return base_data_dir / platform


__all__ = [
    "CHATGPT",
    "GEMINI",
    "GOOGLE_AI_OVERVIEW",
    "COPILOT",
    "GITHUB_COPILOT",
    "PLATFORM_LABELS",
    "PLATFORM_ALIASES",
    "DEFAULT_AUDIT_PLATFORMS",
    "FULL_AUDIT_PLATFORMS",
    "RETRYABLE_STATUSES",
    "normalize_platform",
    "parse_platforms",
    "platform_data_dir",
]
