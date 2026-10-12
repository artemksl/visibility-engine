"""Deterministic JSON serialization and SHA-256 helpers.

These functions define the byte contract shared by persisted artifacts and
content-addressed identifiers.  Changing the serialization settings changes
existing identities, so callers should treat this module as a compatibility
boundary.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, Mapping


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(item) for item in value)
    return value


def canonical_json(value: Any) -> str:
    """Return the canonical JSON text used by immutable artifact hashes."""

    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Return canonical JSON encoded as UTF-8 without a trailing newline."""

    return canonical_json(value).encode("utf-8")


def canonical_bytes(value: Any) -> bytes:
    """Concise alias for :func:`canonical_json_bytes`."""

    return canonical_json_bytes(value)


def sha256_bytes(value: bytes) -> str:
    """Return the lowercase hexadecimal SHA-256 digest of exact bytes."""

    return hashlib.sha256(value).hexdigest()


def canonical_sha256(value: Any) -> str:
    """Return the SHA-256 digest of a value's canonical JSON bytes."""

    return sha256_bytes(canonical_json_bytes(value))


__all__ = [
    "canonical_bytes",
    "canonical_json",
    "canonical_json_bytes",
    "canonical_sha256",
    "sha256_bytes",
]
