"""Measurement group scheduler that never fail-fast cancels independent work."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Hashable, Mapping, Sequence

from .run_measurement_group import MeasurementGroupOutcome


@dataclass(frozen=True)
class ScheduledFailure:
    packet_id: str
    error: BaseException


async def schedule_packets(
    packets: Sequence[Mapping[str, object]],
    execute: Callable[[Mapping[str, object]], Awaitable[MeasurementGroupOutcome]],
    *,
    max_concurrency: int,
    resource_key: Callable[[Mapping[str, object]], Hashable],
) -> tuple[MeasurementGroupOutcome | ScheduledFailure, ...]:
    """Serialize each owned tab and settle every group's result independently."""

    semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))
    resource_locks: dict[Hashable, asyncio.Lock] = {}

    async def one(packet: Mapping[str, object]):
        packet_id = str(packet.get("packet_id") or "")
        lock = resource_locks.setdefault(resource_key(packet), asyncio.Lock())
        # A group waiting for its tab must not occupy capacity needed by an
        # independent tab. Context managers release only acquired resources.
        async with lock:
            async with semaphore:
                try:
                    return await execute(packet)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    return ScheduledFailure(packet_id=packet_id, error=exc)

    # `return_exceptions=True` is deliberate defense-in-depth: `one()` already
    # converts normal failures, and gather must still wait for every peer.
    settled = await asyncio.gather(
        *(one(packet) for packet in packets),
        return_exceptions=True,
    )
    results: list[MeasurementGroupOutcome | ScheduledFailure] = []
    for packet, value in zip(packets, settled):
        packet_id = str(packet.get("packet_id") or "")
        if isinstance(value, (MeasurementGroupOutcome, ScheduledFailure)):
            results.append(value)
        elif isinstance(value, BaseException):
            results.append(ScheduledFailure(packet_id, value))
        else:  # pragma: no cover - defensive protocol validation
            results.append(
                ScheduledFailure(
                    packet_id,
                    TypeError("packet worker returned an unsupported result"),
                )
            )
    return tuple(results)


__all__ = [
    "ScheduledFailure",
    "schedule_packets",
]
