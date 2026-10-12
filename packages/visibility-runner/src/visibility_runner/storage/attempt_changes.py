"""Compatibility facade for shared bounded native file notifications."""
from .._file_changes import (
    AttemptChanges, ChangeTrackingUnavailable, FileChanges,
    MAX_WATCHED_ATTEMPT_FILES, MINIMUM_WATCHED_ATTEMPT_FILES, RESERVED_DESCRIPTORS,
    available_watch_descriptors, bounded_watch_limit, descriptor_limit, ensure_descriptor_limit,
)
