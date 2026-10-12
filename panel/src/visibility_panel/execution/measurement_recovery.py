"""Bound automatic recovery to one explicitly authorized host execution.

The runner owns eligibility, attempts and browser effects. The host only repeats
its public recovery operation, retaining a request identity for each round.
"""
from collections.abc import Mapping
from pathlib import Path

from visibility_panel.execution.worker_registry import publish_json, read_record


AUTO_RECOVERY_ROUNDS = 5


def auto_recovery_progress(request: Mapping, directory: Path):
    limit = request["bindings"].get("auto_recovery_rounds")
    if type(limit) is not int or not 1 < limit <= AUTO_RECOVERY_ROUNDS:
        return None
    saved = read_record(directory / "auto-recovery.json", optional=True)
    if (saved is None or saved.get("schema_version") != "host-auto-recovery-v1"
        or saved.get("execution_id") != request["execution_id"]
        or saved.get("request_sha256") != request["request_sha256"]
        or saved.get("limit") != limit
        or type(saved.get("round")) is not int or not 1 <= saved["round"] <= limit
        or saved.get("status") not in {"running", "completed", "exhausted", "cancelled", "blocked", "error"}):
        return None
    return {key: saved[key] for key in ("round", "limit", "status")}


def run_recovery_rounds(component, request: Mapping, directory: Path, *, first_round,
                        cancelled=lambda: False):
    limit = request["bindings"].get("auto_recovery_rounds", 1)
    if type(limit) is not int or not 1 <= limit <= AUTO_RECOVERY_ROUNDS:
        raise ValueError("Invalid automatic recovery round limit")
    if limit == 1:
        return first_round()

    binding = {"execution_id": request["execution_id"],
               "request_sha256": request["request_sha256"]}
    progress_path = directory / "auto-recovery.json"

    def progress(round_number, status):
        publish_json(progress_path, {"schema_version": "host-auto-recovery-v1",
            **binding, "round": round_number, "limit": limit, "status": status}, replace=True)
        print(f"[recovery] Round {round_number}/{limit}: {status}", flush=True)

    for round_number in range(1, limit + 1):
        try:
            if round_number == 1:
                progress(round_number, "running")
                result = first_round()
            else:
                if cancelled():
                    progress(round_number - 1, "cancelled")
                    return component.inspect(request["run_id"])
                path = directory / f"auto-recovery-round-{round_number}.json"
                request_id = f"{request['request_id']}:auto:{round_number}"
                saved = read_record(path, optional=True)
                if saved is None:
                    recovery = component.plan_refetch(request["run_id"],
                        request_id=request_id)
                    if not recovery.get("selections"):
                        progress(round_number - 1, "blocked")
                        return result
                    saved = {**binding, "round": round_number, "request": recovery}
                    # Save before admission. Re-delivery must reuse this exact
                    # preview, even if the recovery journal changed afterwards.
                    publish_json(path, saved)
                if any(saved.get(key) != value for key, value in binding.items()) or saved.get("round") != round_number:
                    raise ValueError("Automatic recovery request binding changed")
                recovery = saved["request"]
                if recovery.get("run_id") != request["run_id"] or recovery.get("request_id") != request_id:
                    raise ValueError("Automatic recovery round identity changed")
                if cancelled():
                    progress(round_number - 1, "cancelled")
                    return component.inspect(request["run_id"])
                progress(round_number, "running")
                result = component.refetch(recovery, replace_checked_sends=False)

            status = str(getattr(result.status, "value", result.status))
            if status == "succeeded":
                progress(round_number, "completed")
                return result
            if status == "cancelled" or cancelled():
                progress(round_number, "cancelled")
                return component.inspect(request["run_id"])
        except BaseException:
            progress(round_number, "cancelled" if cancelled() else "error")
            raise
    progress(limit, "exhausted")
    return result
