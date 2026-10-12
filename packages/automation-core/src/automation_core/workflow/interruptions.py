from __future__ import annotations

from dataclasses import dataclass, field, replace

import math

import re

from uuid import uuid4

from automation_core.contracts import (AuthObservation, BlockerRuleRef, DispatchReceipt, EvidenceRef,
                        ExpectedScope, Outcome, Recording, StopReason, ViewRef)

_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z", re.ASCII)

_OPAQUE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z", re.ASCII)

def _id(value, *, optional=False, opaque=False):
    return optional and value is None or type(value) is str and ( _OPAQUE if opaque else _ID).fullmatch(value) is not None

def _document(value):
    return type(value) is int and 0 <= value <= MAX_INTEGER

class _RuntimeOnly:
    def __reduce__(self):
        raise TypeError("Runtime interruption proof cannot be serialized.")

@dataclass(frozen=True, repr=False)
class NavigationFailureProof(_RuntimeOnly):
    """Settled failure correlated by the owning driver, not an exception string."""
    view_ref: ViewRef
    document_generation: int
    operation_id: str
    attempt_id: str
    destination_id: str
    receipt: DispatchReceipt
    cause_code: str

    def __post_init__(self):
        if (type(self.view_ref) is not ViewRef or not _document(self.document_generation)
                or not all(_id(value, opaque=True) for value in
                           (self.operation_id, self.attempt_id, self.destination_id))
                or type(self.receipt) is not DispatchReceipt
                or self.receipt.status != "returned" or self.receipt.returned_at is None
                or self.cause_code not in {"navigation_failed", "network_failed"}
                or self.receipt.reason != self.cause_code
                or any(dialog.response == "unresolved" or dialog.entered_at is not None
                       and dialog.returned_at is None for dialog in self.receipt.dialogs)):
            raise ValueError("Invalid settled navigation failure proof.")
