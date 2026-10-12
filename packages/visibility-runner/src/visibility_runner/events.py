"""Structured progress events for synchronous runner hosts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Optional, Protocol

from .models import Issue
from .status import StringEnum


class EventKind(StringEnum):
    RUN_PLANNED = "run_planned"
    RUN_CREATED = "run_created"
    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    PACKET_STARTED = "packet_started"
    PACKET_COMPLETED = "packet_completed"
    OBSERVATION_STARTED = "observation_started"
    OBSERVATION_PREPARED = "observation_prepared"
    SEND_STARTED = "send_started"
    SEND_ACKNOWLEDGED = "send_acknowledged"
    RESPONSE_CAPTURED = "response_captured"
    EXTRACTION_STARTED = "extraction_started"
    EXTRACTION_REJECTED = "extraction_rejected"
    OBSERVATION_REFETCHED = "observation_refetched"
    OBSERVATION_COMPLETED = "observation_completed"
    OPERATION_COMPLETED = "operation_completed"
    NEEDS_ATTENTION = "needs_attention"
    CANCEL_REQUESTED = "cancel_requested"
    DESKTOP_ACTION = "desktop_action"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_value(item) for item in value)
    return value


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    frozen: dict[str, object] = {}
    for key, item in value.items():
        frozen[str(key)] = _freeze_value(item)
    return MappingProxyType(frozen)


@dataclass(frozen=True)
class RunnerEvent:
    kind: EventKind
    run_id: str
    packet_id: Optional[str] = None
    observation_id: Optional[str] = None
    issue: Optional[Issue] = None
    detail: Mapping[str, object] = field(default_factory=dict)
    occurred_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        try:
            kind = (
                self.kind
                if isinstance(self.kind, EventKind)
                else EventKind(str(self.kind))
            )
        except ValueError as exc:
            raise ValueError(f"unknown event kind: {self.kind!r}") from exc
        run_id = str(self.run_id or "").strip()
        if not run_id:
            raise ValueError("event run_id is required")
        if self.issue is not None and not isinstance(self.issue, Issue):
            raise TypeError("event issue must be an Issue")
        if not isinstance(self.detail, Mapping):
            raise TypeError("event detail must be an object")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(
            self,
            "packet_id",
            str(self.packet_id or "").strip() or None,
        )
        object.__setattr__(
            self,
            "observation_id",
            str(self.observation_id or "").strip() or None,
        )
        object.__setattr__(self, "detail", _freeze_mapping(self.detail))
        object.__setattr__(
            self,
            "occurred_at",
            str(self.occurred_at or "").strip() or _utc_now(),
        )


class EventSink(Protocol):
    def __call__(self, event: RunnerEvent) -> object:
        """Consume one progress event."""


EventCallback = Callable[[RunnerEvent], object]


def discard_event(_event: RunnerEvent) -> None:
    """Default no-op event sink."""


__all__ = [
    "EventCallback",
    "EventKind",
    "EventSink",
    "RunnerEvent",
    "discard_event",
]
