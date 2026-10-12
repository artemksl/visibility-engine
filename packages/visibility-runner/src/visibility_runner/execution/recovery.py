"""Typed explicit recovery decisions."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from ..storage.send_attempts import SendAttempt, attempt_resume_decision


class RecoveryAction(str, Enum):
    START_NEW = "start_new"
    RECONSTRUCT_PRE_SEND = "reconstruct_pre_send"
    CONTINUE_NEXT_TURN = "continue_next_turn"
    COMPLETE = "complete"
    REJECTED = "rejected"
    RETRY_CONFIRMED = "retry_confirmed"
    EXHAUSTED = "exhausted"
    NEEDS_ATTENTION = "needs_attention"


def recovery_action(
    current: Optional[SendAttempt],
    *,
    has_next_turn: bool = False,
) -> RecoveryAction:
    decision = attempt_resume_decision(current)
    if decision == "complete" and has_next_turn:
        return RecoveryAction.CONTINUE_NEXT_TURN
    return {
        "prepare": RecoveryAction.START_NEW,
        "prepare_input": RecoveryAction.RECONSTRUCT_PRE_SEND,
        "send": RecoveryAction.RECONSTRUCT_PRE_SEND,
        "retry_prepare": RecoveryAction.RECONSTRUCT_PRE_SEND,
        "refuse_ambiguous": RecoveryAction.NEEDS_ATTENTION,
        "complete": RecoveryAction.COMPLETE,
        "rejected": RecoveryAction.REJECTED,
        "retry_confirmed": RecoveryAction.RETRY_CONFIRMED,
        "confirmed_exhausted": RecoveryAction.EXHAUSTED,
    }[decision]



__all__ = ["RecoveryAction", "recovery_action"]
