"""Match selected Chrome profiles to their actual browser tabs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Optional, Sequence, TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from ..errors import (
    ProfileIdentityMismatchError as _CoreProfileIdentityMismatchError,
    ProfileManifestError as _CoreProfileManifestError,
)
from ..planning.hashing import canonical_json_bytes

if TYPE_CHECKING:  # Imported only for static checking; configuration stays lazy.
    from ..config import ProfileConfig


DEFAULT_BROWSER_CONTEXT_ID = "__chrome_default_browser_context__"
PROFILE_ID_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
PROFILE_MARKER_KEYS = ("visibility-runner-profile", "citeup-profile")


class ProfileManifestError(_CoreProfileManifestError):
    """The frozen profile identity contract could not be verified."""


class ProfileAnchorError(_CoreProfileIdentityMismatchError):
    """The requested persistent Chrome verified profile tabs could not be matched."""


@dataclass(frozen=True)
class BrowserProfile:
    """One stable application identity and its exact raw Chrome directory."""

    id: str
    display_name: str
    chrome_directory: str
    project_url: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not PROFILE_ID_RE.fullmatch(self.id):
            raise ProfileManifestError(
                "profile id must contain lowercase letters/numbers separated "
                "only by underscores or hyphens"
            )
        if (
            not isinstance(self.chrome_directory, str)
            or not self.chrome_directory
            or self.chrome_directory != self.chrome_directory.strip()
        ):
            raise ProfileManifestError(
                "chrome_directory must be non-empty exact text without outer whitespace"
            )
        if not isinstance(self.display_name, str) or not self.display_name:
            raise ProfileManifestError("display_name must be non-empty text")
        if not isinstance(self.project_url, str):
            raise ProfileManifestError("project_url must be text")

    @property
    def profile_id(self) -> str:
        """Explicit alias used by transport evidence."""

        return self.id


# The legacy name is intentionally local to this dependency-free package.
LoggedInProfile = BrowserProfile


@dataclass(frozen=True)
class ProfileManifest:
    """A verified manifest plus its exact, ordered frozen selection."""

    path: Path
    canonical_sha256: str
    source_sha256: str
    profiles: tuple[BrowserProfile, ...]
    selected_profile_ids: tuple[str, ...]
    selected_chrome_directories: tuple[str, ...]
    selected_profiles: tuple[BrowserProfile, ...]

    def profile(self, profile_id: str) -> BrowserProfile:
        for row in self.selected_profiles:
            if row.id == profile_id:
                return row
        raise KeyError(profile_id)


@dataclass(frozen=True)
class VerifiedProfileTab:
    """A configured profile paired with a persistent page in its real context."""

    profile: BrowserProfile
    page: object
    browser_context_id: str

    @property
    def identity(self) -> BrowserProfile:
        return self.profile


def _canonical_manifest_value(value: object) -> object:
    """Return the value covered by the manifest's canonical hash.

    A mapping may carry its own declared hash.  That declaration is metadata
    and cannot hash itself, so it is excluded.  All other keys remain covered.
    A historical top-level list is covered in full.
    """

    if not isinstance(value, Mapping):
        return value
    return {
        str(key): item
        for key, item in value.items()
        if str(key) not in {"canonical_sha256", "manifest_sha256"}
    }


def canonical_profile_manifest_sha256(value: object) -> str:
    return hashlib.sha256(
        canonical_json_bytes(_canonical_manifest_value(value))
    ).hexdigest()


def _exact_text(row: Mapping[str, object], key: str, *, row_number: int) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ProfileManifestError(
            f"profile manifest row {row_number} requires non-empty {key}"
        )
    if value != value.strip():
        raise ProfileManifestError(
            f"profile manifest row {row_number} {key} has outer whitespace"
        )
    return value


def _manifest_rows(payload: object) -> Sequence[object]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        rows = payload.get("profiles")
        if isinstance(rows, list):
            return rows
    raise ProfileManifestError(
        "profile manifest must be a JSON list or an object containing a profiles list"
    )


def _parse_profiles(payload: object) -> tuple[BrowserProfile, ...]:
    rows: list[BrowserProfile] = []
    seen_ids: set[str] = set()
    seen_directories: set[str] = set()
    for index, raw_row in enumerate(_manifest_rows(payload), start=1):
        if not isinstance(raw_row, Mapping):
            raise ProfileManifestError(
                f"profile manifest row {index} must be a JSON object"
            )
        profile_id = _exact_text(raw_row, "id", row_number=index)
        directory_value = raw_row.get("chrome_directory")
        if directory_value is None:
            # Read compatibility is safe because the raw value is still exact;
            # IDs are never inferred.
            directory_value = raw_row.get("profile")
        if not isinstance(directory_value, str) or not directory_value:
            raise ProfileManifestError(
                f"profile manifest row {index} requires chrome_directory"
            )
        if directory_value != directory_value.strip():
            raise ProfileManifestError(
                f"profile manifest row {index} chrome_directory has outer whitespace"
            )
        display_value = raw_row.get("display_name")
        if display_value is None:
            display_value = raw_row.get("label")
        if display_value is None:
            display_value = directory_value
        if not isinstance(display_value, str) or not display_value:
            raise ProfileManifestError(
                f"profile manifest row {index} display_name must be text"
            )
        project_url = raw_row.get("project_url", "")
        if not isinstance(project_url, str):
            raise ProfileManifestError(
                f"profile manifest row {index} project_url must be text"
            )
        profile = BrowserProfile(
            id=profile_id,
            display_name=display_value,
            chrome_directory=directory_value,
            project_url=project_url,
        )
        if profile.id in seen_ids:
            raise ProfileManifestError(
                f"profile manifest repeats profile id {profile.id!r}"
            )
        if profile.chrome_directory in seen_directories:
            raise ProfileManifestError(
                "profile manifest repeats raw Chrome directory "
                f"{profile.chrome_directory!r}"
            )
        seen_ids.add(profile.id)
        seen_directories.add(profile.chrome_directory)
        rows.append(profile)
    if not rows:
        raise ProfileManifestError("profile manifest contains no profiles")
    return tuple(rows)


def read_profile_manifest(
    path: Path | str,
    *,
    selected_profile_ids: Iterable[str],
    expected_canonical_sha256: Optional[str] = None,
    expected_chrome_directories: Optional[Iterable[str]] = None,
) -> ProfileManifest:
    """Load and verify an exact manifest selection.

    Selection is by stable ID, preserves caller order, and never falls back to
    a count or mutable display name.  If raw directories were frozen alongside
    the IDs, their values and order must also match exactly.
    """

    resolved = Path(path).expanduser().resolve()
    try:
        source = resolved.read_bytes()
    except OSError as exc:
        raise ProfileManifestError(
            f"could not read profile manifest {resolved}: {exc}"
        ) from exc
    try:
        payload = json.loads(source.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileManifestError(
            f"profile manifest {resolved} is not valid UTF-8 JSON: {exc}"
        ) from exc

    try:
        canonical_hash = canonical_profile_manifest_sha256(payload)
    except (TypeError, ValueError) as exc:
        raise ProfileManifestError(
            f"profile manifest {resolved} is not canonically hashable: {exc}"
        ) from exc
    declared_hash = (
        str(
            payload.get("canonical_sha256")
            or payload.get("manifest_sha256")
            or ""
        )
        if isinstance(payload, Mapping)
        else ""
    )
    if declared_hash and declared_hash != canonical_hash:
        raise ProfileManifestError(
            "profile manifest declared canonical hash does not match its content"
        )
    if (
        expected_canonical_sha256 is not None
        and expected_canonical_sha256 != canonical_hash
    ):
        raise ProfileManifestError(
            "profile manifest canonical hash does not match the frozen configuration"
        )

    profiles = _parse_profiles(payload)
    requested = tuple(selected_profile_ids)
    if not requested:
        raise ProfileManifestError(
            "selected_profile_ids must freeze at least one exact profile ID"
        )
    if any(not isinstance(item, str) or not item for item in requested):
        raise ProfileManifestError("selected_profile_ids must contain non-empty text")
    if len(set(requested)) != len(requested):
        raise ProfileManifestError("selected_profile_ids contains a duplicate ID")

    by_id = {profile.id: profile for profile in profiles}
    unknown = [profile_id for profile_id in requested if profile_id not in by_id]
    if unknown:
        raise ProfileManifestError(
            "profile manifest does not contain selected ID(s): "
            + ", ".join(repr(item) for item in unknown)
        )
    selected = tuple(by_id[profile_id] for profile_id in requested)
    selected_directories = tuple(row.chrome_directory for row in selected)
    if expected_chrome_directories is not None:
        frozen_directories = tuple(expected_chrome_directories)
        if frozen_directories != selected_directories:
            raise ProfileManifestError(
                "selected raw Chrome directories or their order changed"
            )

    return ProfileManifest(
        path=resolved,
        canonical_sha256=canonical_hash,
        source_sha256=hashlib.sha256(source).hexdigest(),
        profiles=profiles,
        selected_profile_ids=requested,
        selected_chrome_directories=selected_directories,
        selected_profiles=selected,
    )


_MISSING = object()


def _attribute(value: object, names: Sequence[str], default: Any = _MISSING) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
    else:
        for name in names:
            if hasattr(value, name):
                return getattr(value, name)
    if default is _MISSING:
        raise ProfileManifestError(
            "profile configuration is missing " + "/".join(names)
        )
    return default


def read_selected_profiles(config: "ProfileConfig | object") -> ProfileManifest:
    """Load a core ``ProfileConfig`` without duplicating its contract here.

    A few explicit aliases are accepted for versioned worker payloads.  There
    is intentionally no environment lookup and no count-based selection.
    """

    path = _attribute(config, ("manifest_path", "profile_manifest_path"))
    selected = _attribute(
        config,
        ("selected_profile_ids", "profile_ids"),
    )
    expected_hash = _attribute(
        config,
        (
            "manifest_sha256",
            "profile_manifest_sha256",
            "canonical_manifest_sha256",
        ),
        None,
    )
    expected_directories = _attribute(
        config,
        (
            "selected_chrome_directories",
            "chrome_directories",
            "raw_chrome_directories",
        ),
        None,
    )
    if expected_directories is not None:
        expected_directories = tuple(expected_directories)
        if not expected_directories:
            expected_directories = None
    if expected_directories is None:
        frozen_profiles = _attribute(config, ("selected_profiles",), ())
        if frozen_profiles:
            expected_directories = tuple(
                _attribute(
                    profile,
                    ("chrome_directory", "profile"),
                )
                for profile in frozen_profiles
            )
    return read_profile_manifest(
        path,
        selected_profile_ids=selected,
        expected_canonical_sha256=expected_hash,
        expected_chrome_directories=expected_directories,
    )


def profile_marker_from_url(url: object) -> Optional[str]:
    """Return the exact raw Chrome directory carried by a launcher anchor."""

    try:
        parsed = urlsplit(str(url))
    except Exception:
        return None
    for component in (parsed.fragment, parsed.query):
        values = parse_qs(component, keep_blank_values=True)
        for key in PROFILE_MARKER_KEYS:
            candidates = values.get(key, ())
            if not candidates:
                continue
            value = candidates[0]
            if value:
                return value
    return None


def _is_profile_tab_candidate(page: object) -> bool:
    try:
        url = str(getattr(page, "url"))
    except Exception:
        return False
    return profile_marker_from_url(url) is not None or "chatgpt.com" in url.casefold()


_PENDING_IDENTITY_CALLS: set[asyncio.Future] = set()
IDENTITY_CALL_TIMEOUT_SECONDS = 10.0
IDENTITY_CLEANUP_TIMEOUT_SECONDS = 0.25


def _retain_identity_call(task: asyncio.Future) -> None:
    """Keep an abandoned read/cleanup referenced until its outcome is known."""
    _PENDING_IDENTITY_CALLS.add(task)
    def finished(done: asyncio.Future) -> None:
        _PENDING_IDENTITY_CALLS.discard(done)
        try:
            done.exception()
        except BaseException:
            pass
    task.add_done_callback(finished)


async def _identity_call(call, *, deadline: float, late_session: bool = False):
    loop = asyncio.get_running_loop()
    if loop.time() >= deadline:
        raise TimeoutError("profile identity deadline exceeded")
    task = asyncio.ensure_future(call())
    try:
        done, _ = await asyncio.wait((task,), timeout=max(0.0, deadline - loop.time()))
        if not done:
            raise TimeoutError("profile identity deadline exceeded")
        return task.result()
    except BaseException:
        task.cancel()
        _retain_identity_call(task)
        if late_session:
            def close_late(done):
                if done.cancelled():
                    return
                try:
                    session = done.result()
                except BaseException:
                    return
                cleanup = asyncio.create_task(_detach_identity_session(session))
                _retain_identity_call(cleanup)
            task.add_done_callback(close_late)
        raise


async def _detach_identity_session(session: object) -> None:
    detach = getattr(session, "detach", None)
    if detach is not None:
        try:
            await _identity_call(detach, deadline=asyncio.get_running_loop().time() + IDENTITY_CLEANUP_TIMEOUT_SECONDS)
        except Exception:
            pass


async def _browser_target_info(page: object, *, deadline: Optional[float]) -> Optional[Mapping]:
    context = getattr(page, "context", None)
    new_session = getattr(context, "new_cdp_session", None)
    if new_session is None:
        return None
    if deadline is None:
        deadline = asyncio.get_running_loop().time() + IDENTITY_CALL_TIMEOUT_SECONDS
    session = None
    try:
        session = await _identity_call(lambda: new_session(page), deadline=deadline, late_session=True)
        response = await _identity_call(lambda: session.send("Target.getTargetInfo"), deadline=deadline)
        target = response.get("targetInfo") if isinstance(response, Mapping) else None
        if not isinstance(target, Mapping) or not target.get("targetId"):
            return None
        return target
    except Exception:
        return None
    finally:
        if session is not None:
            await _detach_identity_session(session)


async def browser_context_id(page: object, *, deadline: Optional[float] = None) -> Optional[str]:
    """Read an exact context identity with bounded CDP work and cleanup."""
    for attribute_name in ("browser_context_id", "profile_context_id"):
        value = getattr(page, attribute_name, None)
        if value:
            return str(value)
    target = await _browser_target_info(page, deadline=deadline)
    if target is None:
        return None
    value = target.get("browserContextId")
    return str(value) if value else DEFAULT_BROWSER_CONTEXT_ID


async def browser_target_id(page: object, *, deadline: Optional[float] = None) -> Optional[str]:
    """Read the navigation-stable CDP target identity within a deadline."""
    for attribute_name in ("browser_target_id", "target_id"):
        value = getattr(page, attribute_name, None)
        if value:
            return str(value)
    target = await _browser_target_info(page, deadline=deadline)
    return str(target["targetId"]) if target else None


def _all_pages(browser: object) -> list[object]:
    pages: list[object] = []
    for context in list(getattr(browser, "contexts", ()) or ()):
        pages.extend(list(getattr(context, "pages", ()) or ()))
    return pages


async def match_profile_tabs(
    browser: object,
    profiles: Iterable[BrowserProfile],
    *,
    wait_seconds: float = 10.0,
    poll_seconds: float = 0.2,
    allow_untagged_legacy: bool = False,
) -> tuple[VerifiedProfileTab, ...]:
    """Match selected profiles to distinct persistent Chrome anchors.

    Tagged anchors are matched only by the exact raw Chrome directory.  An
    explicitly requested legacy mode can positionally pair untagged ChatGPT
    anchors, but it never reattributes an anchor tagged for another profile.
    """

    requested = tuple(profiles)
    if not requested:
        raise ProfileAnchorError("no selected profiles were supplied")
    if len({item.chrome_directory for item in requested}) != len(requested):
        raise ProfileAnchorError("selected profiles repeat a Chrome directory")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, float(wait_seconds))
    discovered: list[tuple[object, str, Optional[str]]] = []
    matches: dict[str, tuple[object, str]] = {}

    while True:
        by_context: dict[str, tuple[object, str, Optional[str]]] = {}
        for page in _all_pages(browser):
            if not _is_profile_tab_candidate(page):
                continue
            marker = profile_marker_from_url(getattr(page, "url", ""))
            if marker is None and not allow_untagged_legacy:
                continue
            context_id = await browser_context_id(page, deadline=deadline)
            if not context_id:
                continue
            previous = by_context.get(context_id)
            if previous is not None and previous[2] is not None and marker is not None and previous[2] != marker:
                raise ProfileAnchorError("one browser context claims conflicting Chrome directories")
            if previous is None or (marker is not None and previous[2] is None):
                by_context[context_id] = (page, context_id, marker)
        discovered = list(by_context.values())

        tagged: dict[str, tuple[object, str]] = {}
        duplicate_markers: set[str] = set()
        for page, context_id, marker in discovered:
            if marker is None:
                continue
            if marker in tagged and tagged[marker][1] != context_id:
                duplicate_markers.add(marker)
            tagged[marker] = (page, context_id)
        if duplicate_markers:
            raise ProfileAnchorError(
                "multiple persistent contexts claim raw Chrome director"
                + ("ies " if len(duplicate_markers) != 1 else "y ")
                + ", ".join(repr(item) for item in sorted(duplicate_markers))
            )

        matches = {}
        used_contexts: set[str] = set()
        for profile in requested:
            candidate = tagged.get(profile.chrome_directory)
            if candidate is not None and candidate[1] not in used_contexts:
                matches[profile.chrome_directory] = candidate
                used_contexts.add(candidate[1])

        if allow_untagged_legacy:
            legacy = [
                (page, context_id)
                for page, context_id, marker in discovered
                if marker is None and context_id not in used_contexts
            ]
            unmatched = [
                profile
                for profile in requested
                if profile.chrome_directory not in matches
            ]
            for profile, candidate in zip(unmatched, legacy):
                matches[profile.chrome_directory] = candidate
                used_contexts.add(candidate[1])

        if len(matches) == len(requested):
            break
        if loop.time() >= deadline:
            missing = [
                profile.chrome_directory
                for profile in requested
                if profile.chrome_directory not in matches
            ]
            raise ProfileAnchorError(
                "persistent Chrome profile pool is incomplete; missing exact "
                "tagged anchor(s) for " + ", ".join(repr(item) for item in missing)
            )
        await asyncio.sleep(min(max(0.0, float(poll_seconds)), max(0.0, deadline - loop.time())))

    return tuple(
        VerifiedProfileTab(
            profile=profile,
            page=matches[profile.chrome_directory][0],
            browser_context_id=matches[profile.chrome_directory][1],
        )
        for profile in requested
    )


match_profile_anchors = match_profile_tabs


__all__ = [
    "BrowserProfile",
    "DEFAULT_BROWSER_CONTEXT_ID",
    "LoggedInProfile",
    "VerifiedProfileTab",
    "ProfileAnchorError",
    "ProfileManifest",
    "ProfileManifestError",
    "browser_context_id",
    "browser_target_id",
    "canonical_profile_manifest_sha256",
    "read_selected_profiles",
    "read_profile_manifest",
    "match_profile_anchors",
    "match_profile_tabs",
    "profile_marker_from_url",
]
