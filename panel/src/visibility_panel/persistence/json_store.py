"""Small JSON/JSONL persistence helpers with deterministic formatting."""

from __future__ import annotations

import json
from pathlib import Path


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_json_or_none(path):
    """Read JSON, returning None for a missing or malformed file."""
    try:
        return read_json(path)
    except Exception:
        return None


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_jsonl(path, *, missing_ok: bool = False):
    """Read newline-delimited JSON, skipping blank lines.

    `missing_ok` returns [] for an absent file, which several pipeline stages rely on to
    run against a database that has not been produced yet.
    """
    path = Path(path)
    if missing_ok and not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
