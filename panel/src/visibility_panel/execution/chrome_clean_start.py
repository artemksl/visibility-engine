"""Suppress tab restoration in saved profiles, without deleting profile data.

The launcher validates before shutdown and applies only after Chrome is gone.
Run this file directly so the shell launcher needs no installed host package.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile


def preferences(root: Path, directories: list[str]):
    rows = []
    for directory in directories:
        profile = root / directory
        if Path(directory).name != directory or not profile.is_dir():
            raise ValueError("Selected saved Chrome profile is unavailable")
        path = profile / "Preferences"
        body = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(body, dict) or any(
                key in body and not isinstance(body[key], dict) for key in ("session", "profile")):
            raise ValueError("Selected Chrome preferences cannot be verified")
        rows.append((path, body))
    return rows


def prepare(root: Path, directories: list[str], *, validate_only=False):
    rows = preferences(root, directories)
    if validate_only:
        return
    for path, body in rows:
        # Chrome value 5 opens a new tab instead of restoring the old session.
        body.setdefault("session", {})["restore_on_startup"] = 5
        state = body.setdefault("profile", {})
        state.update(exit_type="Normal", exited_cleanly=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                    prefix=".clean-start-", delete=False) as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, path.stat().st_mode & 0o777 if path.exists() else 0o600)
                json.dump(body, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        prepare(Path(sys.argv[2]), sys.argv[3:], validate_only=sys.argv[1] == "validate")
    except (OSError, ValueError, TypeError):
        print("Selected Chrome preferences could not be prepared; measurement was not started", file=sys.stderr)
        raise SystemExit(1)
