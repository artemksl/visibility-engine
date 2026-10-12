"""Bounded macOS vnode notifications reusable by verified file indexes.

Notifications are an optimization, never a mutation authority. Unsupported
platforms, missing notifications, descriptor exhaustion or uncertain ownership
fall back to the caller's complete metadata refresh. Directory timestamps alone
are insufficient: legacy writers may modify an existing inode in place.
"""

from __future__ import annotations

import os
import select
import stat
import threading
from pathlib import Path
from typing import Optional


MAX_WATCHED_ATTEMPT_FILES = 4096
# Descriptors that must stay free for everything else in the process: the
# browser connection, lock files, journal publication and evidence writes.
RESERVED_DESCRIPTORS = 256
# Below this many usable descriptors the watcher is not worth its cost.
MINIMUM_WATCHED_ATTEMPT_FILES = 64


def descriptor_limit() -> Optional[int]:
    """The process's soft descriptor limit, or None when it cannot be read."""
    try:
        import resource
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ImportError, OSError, ValueError):
        return None
    if soft in (resource.RLIM_INFINITY, -1) or soft <= 0:
        return None
    return int(soft)


def ensure_descriptor_limit(target: int = 8192) -> Optional[int]:
    """Raise the soft descriptor limit towards ``target`` (never above the hard
    limit) and return the effective soft limit, or None when unknown.

    The macOS launchd default is 256, which a run with a few hundred send
    attempt files plus the browser connection can exhaust. Failure to raise
    the limit is not an error: the watcher adapts to whatever remains.
    """
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ImportError, OSError, ValueError):
        return None
    wanted = target if hard in (resource.RLIM_INFINITY, -1) else min(target, hard)
    if soft in (resource.RLIM_INFINITY, -1) or soft >= wanted:
        return descriptor_limit()
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
    except (OSError, ValueError):
        return descriptor_limit()
    return descriptor_limit()


def open_descriptor_count() -> Optional[int]:
    """Sample existing descriptors once per tracker, including other trackers."""
    for directory in ("/dev/fd", "/proc/self/fd"):
        try:
            # The enumeration descriptor may be included; keeping that extra
            # slot reserved is conservative and avoids a platform assumption.
            return len(os.listdir(directory))
        except OSError:
            continue
    return None


def available_watch_descriptors() -> Optional[int]:
    """Additional descriptors available after current usage and the reserve."""
    soft = descriptor_limit()
    if soft is None:
        return None
    opened = open_descriptor_count()
    # An unknown inventory cannot prove that native watches fit safely.
    return 0 if opened is None else max(0, soft - RESERVED_DESCRIPTORS - opened)


# Every tracker shares the same allocation budget. A new tracker refreshes the
# existing non-watcher usage, then each allocation/deallocation adjusts the
# count under the lock, without enumerating descriptors for every journal event.
# Path conversion/allocation may run GC while this lock is held. An older
# watcher's finalizer releases descriptors on this same thread, so bookkeeping
# must permit that nested release without deadlocking the event loop.
_descriptor_lock = threading.RLock()
_descriptor_count = 0
_descriptor_capacity: Optional[int] = None


def bounded_watch_limit(limit: int = MAX_WATCHED_ATTEMPT_FILES) -> int:
    """Cap the watched files so RESERVED_DESCRIPTORS always stay available."""
    available = available_watch_descriptors()
    if available is None:
        return limit
    return min(limit, available)


class ChangeTrackingUnavailable(Exception):
    """The caller must discard the optimization and refresh every file."""


class WatchCapacityReached(ChangeTrackingUnavailable):
    """The configured descriptor quota is full; existing watches remain valid."""


class FileChanges:
    """Track one directory and every attempt inode with bounded descriptors."""

    def __init__(self, root: Path, *, limit: int = MAX_WATCHED_ATTEMPT_FILES):
        self.root = root
        self.limit = limit
        self._queue = None
        self._root_fd: Optional[int] = None
        self._files: dict[Path, int] = {}
        self._names: dict[str, Path] = {}
        self._unwatched_names: set[str] = set()
        self._paths_by_fd: dict[int, Path] = {}
        self._identities: dict[Path, tuple[int, int]] = {}
        self._fresh = True
        global _descriptor_capacity
        with _descriptor_lock:
            available = available_watch_descriptors()
            _descriptor_capacity = None if available is None else available + _descriptor_count
        if available is not None:
            self.limit = min(limit, max(0, available - 2))  # queue + directory
        if available is not None and available < MINIMUM_WATCHED_ATTEMPT_FILES:
            # The process itself is short of descriptors; an explicit small
            # ``limit`` from a caller is a different matter and is honoured.
            raise ChangeTrackingUnavailable("too few descriptors available for attempt notifications")
        if not hasattr(select, "kqueue") or not hasattr(os, "O_EVTONLY"):
            raise ChangeTrackingUnavailable("vnode notifications are unavailable")
        try:
            self._queue = self._allocate(select.kqueue)
            self._root_fd = self._allocate(lambda: self._open(root))
            initial = os.fstat(self._root_fd)
            if not stat.S_ISDIR(initial.st_mode):
                raise ChangeTrackingUnavailable("attempt root is not a directory")
            self._root_identity = (initial.st_dev, initial.st_ino)
            self._register(self._root_fd)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _allocate(opener):
        global _descriptor_count
        with _descriptor_lock:
            if _descriptor_capacity is not None and _descriptor_count >= _descriptor_capacity:
                raise WatchCapacityReached("shared watch descriptor bound reached")
            descriptor = opener()
            _descriptor_count += 1
            return descriptor

    @staticmethod
    def _release(closer) -> None:
        global _descriptor_count
        with _descriptor_lock:
            try:
                closer()
            except OSError:
                pass
            finally:
                _descriptor_count -= 1

    @staticmethod
    def _open(path: Path) -> int:
        # O_NOFOLLOW prevents an unwatched symlink target from entering the
        # index. The caller falls back to stat-based validation for symlinks.
        return os.open(path, os.O_EVTONLY | os.O_NOFOLLOW | os.O_CLOEXEC)

    def _register(self, descriptor: int) -> None:
        assert self._queue is not None
        flags = (
            select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_ATTRIB
            | select.KQ_NOTE_LINK | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME
            | select.KQ_NOTE_REVOKE
        )
        self._queue.control([
            select.kevent(descriptor, filter=select.KQ_FILTER_VNODE,
                          flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR, fflags=flags),
        ], 0, 0)

    def watch(self, path: Path) -> tuple[int, int]:
        """Watch before reading so a simultaneous replacement remains visible."""

        if path in self._files:
            return self._identities[path]
        if len(self._files) >= self.limit:
            raise WatchCapacityReached("attempt watch descriptor bound reached")
        descriptor = self._allocate(lambda: self._open(path))
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ChangeTrackingUnavailable("attempt is not a regular file")
            self._register(descriptor)
        except BaseException:
            self._release(lambda: os.close(descriptor))
            raise
        self._unwatched_names.discard(path.name)
        self._files[path] = descriptor
        self._names[path.name] = path
        self._paths_by_fd[descriptor] = path
        self._identities[path] = (metadata.st_dev, metadata.st_ino)
        return self._identities[path]

    def remember_unwatched(self, path: Path) -> None:
        """Avoid rediscovering names whose metadata the caller verifies itself."""
        self._unwatched_names.add(path.name)

    def forget(self, path: Path) -> None:
        self._unwatched_names.discard(path.name)
        descriptor = self._files.pop(path, None)
        self._names.pop(path.name, None)
        if descriptor is not None:
            self._paths_by_fd.pop(descriptor, None)
            self._identities.pop(path, None)
            self._release(lambda: os.close(descriptor))  # Closing unregisters the vnode.

    def changed_paths(self) -> Optional[set[Path]]:
        """Return changed names, or None for the first complete refresh."""

        if self._queue is None or self._root_fd is None:
            raise ChangeTrackingUnavailable("attempt change tracker is closed")
        current = self.root.stat()
        if (current.st_dev, current.st_ino) != self._root_identity:
            raise ChangeTrackingUnavailable("attempt directory was replaced")
        dirty: set[Path] = set()
        replaced: set[Path] = set()
        directory_changed = False
        # Do not let continuous concurrent writes create an unbounded event
        # drain inside a synchronous scheduling decision.
        for _ in range(8):
            events = self._queue.control(None, self.limit + 1, 0)
            if not events:
                break
            for event in events:
                if event.flags & select.KQ_EV_ERROR or event.fflags & select.KQ_NOTE_REVOKE:
                    raise ChangeTrackingUnavailable("vnode notification failed")
                if event.ident == self._root_fd:
                    if event.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME):
                        raise ChangeTrackingUnavailable("attempt directory ownership changed")
                    directory_changed = True
                    continue
                path = self._paths_by_fd.get(event.ident)
                if path is None:
                    raise ChangeTrackingUnavailable("unknown vnode notification")
                dirty.add(path)
                if event.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME):
                    replaced.add(path)
        else:
            raise ChangeTrackingUnavailable("vnode notification drain bound reached")
        if directory_changed:
            # Enumerate names only, never stat all old files. Watching each old
            # inode covers replacements at an unchanged name and in-place writes.
            with os.scandir(self.root) as entries:
                names = {entry.name for entry in entries if entry.name.endswith(".json")}
            # Construct paths only for changed names. Building one Path for
            # every unchanged record on every append is avoidable quadratic
            # allocation, even though the directory enumeration is necessary.
            dirty.update(self.root / name for name in names.symmetric_difference(self._names.keys() | self._unwatched_names))
        for path in replaced:
            self.forget(path)
        if self._fresh:
            self._fresh = False
            return None
        return dirty

    def close(self) -> None:
        for descriptor in self._files.values():
            self._release(lambda: os.close(descriptor))
        self._files.clear()
        self._names.clear()
        self._unwatched_names.clear()
        self._paths_by_fd.clear()
        self._identities.clear()
        if self._root_fd is not None:
            self._release(lambda: os.close(self._root_fd))
            self._root_fd = None
        if self._queue is not None:
            self._release(self._queue.close)
            self._queue = None

    def __del__(self) -> None:
        self.close()

# Compatibility name for the send-attempt storage facade.
AttemptChanges = FileChanges
