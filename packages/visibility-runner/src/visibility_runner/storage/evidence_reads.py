"""Bounded artifact bytes shared only within one synchronous verification."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path


DEFAULT_VERIFICATION_CACHE_BYTES = 16 * 1024 * 1024
DEFAULT_VERIFICATION_CACHE_ENTRIES = 1024


class EvidenceReads:
    """Memoize disk reads, never the validity of an artifact reference.

    Every caller must still validate its expected digest, length and identity.
    A session belongs to one verification traversal and must not be retained
    on a repository, attempt store used for mutation, or returned snapshot.
    Oversized entries are read normally; retained bytes and entry count are
    bounded independently so even empty evidence cannot grow without limit.
    """

    def __init__(
        self,
        *,
        max_bytes: int = DEFAULT_VERIFICATION_CACHE_BYTES,
        max_entries: int = DEFAULT_VERIFICATION_CACHE_ENTRIES,
    ) -> None:
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("max_bytes must be a non-negative integer")
        if type(max_entries) is not int or max_entries < 0:
            raise ValueError("max_entries must be a non-negative integer")
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self._bytes = 0
        self._payloads: OrderedDict[Path, bytes] = OrderedDict()

    def read_bytes(self, path: Path) -> bytes:
        path = Path(path)
        if path in self._payloads:
            self._payloads.move_to_end(path)
            return self._payloads[path]
        payload = path.read_bytes()
        if len(payload) > self.max_bytes or self.max_entries == 0:
            return payload
        while self._payloads and (
            self._bytes + len(payload) > self.max_bytes
            or len(self._payloads) >= self.max_entries
        ):
            _, removed = self._payloads.popitem(last=False)
            self._bytes -= len(removed)
        self._payloads[path] = payload
        self._bytes += len(payload)
        return payload
