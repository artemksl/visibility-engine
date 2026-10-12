"""One terminal-result predicate for inspection and visibility recovery.

Callers must first validate the result's identity and evidence against its plan.
Missing extraction status retains the original result-format compatibility;
an explicit unfinished or uncertain extraction is never terminal success.
"""

from __future__ import annotations

from typing import Mapping


def accepted_result(row: Mapping[str, object] | None) -> bool:
    if not isinstance(row, Mapping):
        return False
    status = str(row.get("status") or "").casefold()
    extraction = str(row.get("extraction_status") or "accepted").casefold()
    return status in {"succeeded", "not_shown"} and extraction in {
        "accepted", "complete", "succeeded", "not_required",
    }
