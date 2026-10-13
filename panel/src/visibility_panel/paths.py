"""Repository path helpers for packaged workflow modules."""

from __future__ import annotations

from pathlib import Path
import os
import sys


def repo_root() -> Path:
    """Return the checked-out repository root."""
    configured = os.environ.get("VISIBILITY_ENGINE_ROOT")
    if configured:
        root = Path(configured).expanduser()
        if not root.is_absolute():
            raise ValueError("VISIBILITY_ENGINE_ROOT must be an absolute checkout path")
        return root.resolve()
    module = Path(__file__).resolve()
    source_root = module.parents[3]
    if source_root / "panel" / "src" / "visibility_panel" == module.parent:
        return source_root
    # A normal installation lives under this checkout's dedicated environment,
    # with different site-packages depths on Windows and macOS.
    environment = Path(sys.prefix).resolve()
    root = environment.parent
    if environment.name == ".venv" and (root / "scripts" / "run_visibility_measurement.py").is_file():
        return root
    raise RuntimeError("Launch the panel with run.cmd or run.sh from its checkout")


def repo_path(*parts: str) -> Path:
    """Build an absolute path under the repository root."""
    return repo_root().joinpath(*parts)


def root_or_default(root: Path | str | None = None) -> Path:
    """Return an explicitly supplied root, falling back to the repository root."""
    return Path(root) if root else repo_root()


def resolve_repo_relative(path: Path | str, *, base: Path | None = None) -> Path:
    """Resolve a path, treating relative values as repository-relative by default."""
    value = Path(path).expanduser()
    if value.is_absolute():
        return value
    return ((base or repo_root()) / value).resolve()
