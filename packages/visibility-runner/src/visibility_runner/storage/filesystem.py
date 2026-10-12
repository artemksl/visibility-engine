"""Atomic, lossless filesystem primitives."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..planning.hashing import canonical_json_bytes
from ..recovery.durable import publish_bytes


def _replace(path: Path, payload: bytes, *, exclusive: bool = False, full_sync: bool = True) -> None:
    # Shared complete-file publication: never expose an exclusive destination
    # before its contents and critical flush have completed. ``full_sync=False``
    # is reserved for mutable progress projections that are rebuilt from the
    # fully synced journal, attempt and result records.
    from ..recovery.durable import publish_bytes
    publish_bytes(path, payload, exclusive=exclusive, full_sync=full_sync)


def _fsync_directory(path: Path) -> None:
    from ..recovery.durable import sync_directory
    sync_directory(path)


def atomic_write_bytes(
    path: Path | str,
    payload: bytes,
    *,
    exclusive: bool = False,
    full_sync: bool = True,
) -> Path:
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    resolved = Path(path)
    _replace(resolved, payload, exclusive=exclusive, full_sync=full_sync)
    return resolved


def atomic_write_text(
    path: Path | str,
    value: str,
    *,
    exclusive: bool = False,
) -> Path:
    if not isinstance(value, str):
        raise TypeError("value must be text")
    return atomic_write_bytes(
        path,
        value.encode("utf-8"),
        exclusive=exclusive,
    )


def atomic_write_json(
    path: Path | str,
    value: Any,
    *,
    exclusive: bool = False,
    canonical: bool = False,
    full_sync: bool = True,
) -> Path:
    payload = (
        canonical_json_bytes(value) + b"\n"
        if canonical
        else (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    )
    return atomic_write_bytes(path, payload, exclusive=exclusive, full_sync=full_sync)


def read_json(path: Path | str) -> Any:
    with Path(path).open("r", encoding="utf-8", newline="") as source:
        return json.load(source)


def read_text_losslessly(path: Path | str) -> str:
    with Path(path).open("r", encoding="utf-8", newline="") as source:
        return source.read()


__all__ = [
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_text",
    "read_json",
    "read_text_losslessly",
]
