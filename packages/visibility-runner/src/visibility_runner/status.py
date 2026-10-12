"""Typed lifecycle and send attempt states.

String enum values are stable JSON spellings; callers should compare enum
members rather than ad-hoc lifecycle strings.
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable, Type, TypeVar


class StringEnum(str, Enum):
    """A JSON-friendly string enum that works on Python 3.10."""

    def __str__(self) -> str:
        return self.value


class RunStatus(StringEnum):
    PLANNED = "planned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PacketStatus(StringEnum):
    PLANNED = "planned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ObservationStatus(StringEnum):
    PLANNED = "planned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NOT_SHOWN = "not_shown"
    REJECTED = "rejected"
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExtractionStatus(StringEnum):
    NOT_REQUIRED = "not_required"
    PLANNED = "planned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SendFenceStatus(StringEnum):
    """Durable transport send attempt for one irreversible browser submission."""

    NOT_STARTED = "not_started"
    PREPARED = "prepared"
    SEND_STARTED = "send_started"
    ACKNOWLEDGED = "acknowledged"
    RESPONSE_CAPTURED = "response_captured"
    AMBIGUOUS = "ambiguous"


class IssueSeverity(StringEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


_StatusT = TypeVar("_StatusT", RunStatus, PacketStatus)


def aggregate_status(
    statuses: Iterable[ObservationStatus | PacketStatus],
    *,
    target: Type[_StatusT],
    useful_evidence: bool = False,
) -> _StatusT:
    """Derive a run/measurement group status without treating function return as success.

    ``NEEDS_ATTENTION`` dominates because an ambiguous send or integrity
    mismatch cannot be retried automatically.  Any failed/rejected/incomplete
    child alongside useful immutable evidence is ``PARTIAL``. A ``PARTIAL``
    child already establishes useful evidence without a second caller flag.
    ``SUCCEEDED`` is
    returned only when every child completed collection and required
    name listing (an exact Google ``NOT_SHOWN`` is terminally complete).
    """

    values = tuple(statuses)
    if not values:
        return target.PLANNED
    serialized = {str(value) for value in values}
    if "needs_attention" in serialized:
        return target.NEEDS_ATTENTION
    if serialized <= {"succeeded", "not_shown"}:
        return target.SUCCEEDED
    if serialized == {"cancelled"}:
        return target.CANCELLED
    if serialized <= {"planned"}:
        return target.PLANNED
    if serialized <= {"planned", "running"} and "running" in serialized:
        return target.RUNNING
    evidence_exists = bool(serialized & {"succeeded", "not_shown", "partial"})
    if useful_evidence or evidence_exists:
        return target.PARTIAL
    if "cancelled" in serialized and serialized <= {
        "cancelled",
        "planned",
        "running",
    }:
        return target.CANCELLED
    return target.FAILED


def reduce_run_status(
    statuses: Iterable[PacketStatus], *, persisted: str = "",
) -> RunStatus:
    """Reduce run progress while preserving cancellation and failure precedence.

    Packet completion supersedes a transient persisted failure or partial state.
    The result projection separately requires every frozen accepted result.
    """
    values = {str(status) for status in statuses}
    persisted = persisted.casefold()
    if persisted == "needs_attention" or "needs_attention" in values:
        return RunStatus.NEEDS_ATTENTION
    if values == {"succeeded"}:
        return RunStatus.SUCCEEDED
    if persisted == "cancelled" and not values & {"running", "succeeded"}:
        return RunStatus.CANCELLED
    if persisted == "failed":
        return RunStatus.PARTIAL if values & {"succeeded", "partial"} else RunStatus.FAILED
    if persisted == "partial":
        return RunStatus.PARTIAL
    if values == {"planned"}:
        return RunStatus.PLANNED
    if values == {"cancelled"}:
        return RunStatus.CANCELLED
    if values & {"succeeded", "partial"}:
        return RunStatus.PARTIAL
    if "running" in values:
        return RunStatus.RUNNING
    return RunStatus.FAILED


__all__ = [
    "ExtractionStatus",
    "IssueSeverity",
    "ObservationStatus",
    "PacketStatus",
    "RunStatus",
    "SendFenceStatus",
    "StringEnum",
    "aggregate_status",
    "reduce_run_status",
]
