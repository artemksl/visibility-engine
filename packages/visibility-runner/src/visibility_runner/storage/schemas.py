"""Versioned schemas owned by :mod:`visibility_runner`."""

from ..contract_primitives import (
    RUN_PLAN_SCHEMA_VERSION as PLAN_SCHEMA_VERSION,
    WORKER_REQUEST_SCHEMA_VERSION,
)

PACKET_SCHEMA_VERSION = "visibility_runner.packet.v1"
RUN_STATE_SCHEMA_VERSION = "visibility_runner.runner_state.v1"
PACKET_STATE_SCHEMA_VERSION = "visibility_runner.packet_state.v1"
OBSERVATION_RESULT_SCHEMA_VERSION = "visibility_runner.observation_result.v1"
FENCE_SCHEMA_VERSION = "visibility_runner.send_fence.v1"
WORKER_RESULT_SCHEMA_VERSION = "visibility_runner.worker_result.v1"
EVENT_SCHEMA_VERSION = "visibility_runner.event.v1"

# The evidence contracts predate this package and deliberately retain their
# established names so archived artifacts remain readable without rewriting.
SAME_CHAT_SCHEMA_VERSION = "free_snapshot.same_chat_name_extraction.v1"
GAIO_EXTRACTION_SCHEMA_VERSION = "free_snapshot.gaio_gemini_name_extraction.v1"

__all__ = [name for name in globals() if name.endswith("_VERSION")]
