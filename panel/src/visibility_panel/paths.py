"""Repository path helpers for packaged workflow modules."""

from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    """Return the checked-out repository root."""
    return Path(__file__).resolve().parents[3]


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
