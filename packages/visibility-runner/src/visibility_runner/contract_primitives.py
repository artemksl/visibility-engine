"""Shared, dependency-free names and serialization for public contracts.

Persistence uses strict canonical JSON.  The historical model hash exports
intentionally retain their older JSON-only input contract (including NaN);
they must not be used to validate persisted evidence or rewrite its hashes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


RUN_PLAN_SCHEMA_VERSION = "visibility_runner.run_plan.v1"
WORKER_REQUEST_SCHEMA_VERSION = "visibility_runner.worker_request.v1"

# Accept the union of the established configuration and planning aliases at
# both boundaries.  Canonical values and therefore existing hashes are stable.
PLATFORM_ALIASES = MappingProxyType({
    "chatgpt": "chatgpt",
    "gpt": "chatgpt",
    "openai": "chatgpt",
    "chat_gpt": "chatgpt",
    "gemini": "gemini",
    "google_gemini": "gemini",
    "google_ai_overview": "google_ai_overview",
    "google_ai_overviews": "google_ai_overview",
    "google_aio": "google_ai_overview",
    "google": "google_ai_overview",
    "google_ai": "google_ai_overview",
    "google_search_ai": "google_ai_overview",
    "google_ai_search": "google_ai_overview",
    "ai_overview": "google_ai_overview",
    "aio": "google_ai_overview",
    "github_copilot": "github_copilot",
    "github_copilot_sdk": "github_copilot",
    "copilot": "copilot",
    "ms_copilot": "copilot",
    "microsoft_copilot": "copilot",
})


def platform_key(value: object, *, legacy_planning: bool = False) -> str:
    """Resolve shared aliases, preserving each boundary's lexical contract.

    Legacy raw-scope planning accepts punctuation as separators and strips
    leading/trailing separators. Configuration only folds spaces and hyphens.
    The callers retain their public return types and error types/messages.
    """

    key = str(value or "").strip().casefold()
    if legacy_planning:
        key = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
    else:
        key = re.sub(r"[\s-]+", "_", key)
    return PLATFORM_ALIASES[key]


def _json_value(value: Any, *, legacy_model: bool) -> Any:
    if not legacy_model:
        if isinstance(value, Enum):
            return value.value
        projection = getattr(value, "__json_value__", None)
        if callable(projection):
            return _json_value(projection(), legacy_model=False)
        if is_dataclass(value):
            return {
                field.name: _json_value(
                    getattr(value, field.name), legacy_model=False
                )
                for field in fields(value)
            }
        if isinstance(value, Path):
            return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item, legacy_model=legacy_model)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item, legacy_model=legacy_model) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(
            _json_value(item, legacy_model=legacy_model) for item in value
        )
    return value


def json_value(value: Any) -> Any:
    return _json_value(value, legacy_model=False)


def _canonical_json(value: Any, *, legacy_model: bool) -> str:
    return json.dumps(
        _json_value(value, legacy_model=legacy_model),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=legacy_model,
    )


def canonical_json(value: Any) -> str:
    """Return the exact strict Unicode JSON used by legacy v4 hashes."""

    return _canonical_json(value, legacy_model=False)


def canonical_json_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_sha256(value: Any) -> str:
    return bytes_sha256(canonical_json_bytes(value))


def text_sha256(value: str) -> str:
    return bytes_sha256(value.encode("utf-8"))


def legacy_model_canonical_json_bytes(value: object) -> bytes:
    """Compatibility only: allow NaN and reject Path/dataclass/ordinary Enum."""

    return _canonical_json(value, legacy_model=True).encode("utf-8")


def legacy_model_canonical_sha256(value: object) -> str:
    return bytes_sha256(legacy_model_canonical_json_bytes(value))
