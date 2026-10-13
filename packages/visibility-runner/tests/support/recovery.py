"""Synthetic recovery manifests, snapshots and authorized passes."""
from __future__ import annotations
from visibility_runner.recovery.api import ItemState, OperationSpec, RecoveryCoordinator, RecoveryLedger, RecoveryRequest, RecoverySnapshot, RunManifest, RunOwnership, WorkItem, digest


def manifest(*, local=False, items=("one",), max_effects=8, max_attempts=2, cost_limits=()):
    return RunManifest("run", digest("scope"), "workflow.1", "policy.1",
        tuple(WorkItem(item, (item + ".op",)) for item in items),
        tuple(OperationSpec(item + ".op", item, "fake", "1", digest(item), max_attempts=max_attempts,
                            max_effects=max_effects, local_only=local, cost_limits=cost_limits) for item in items))


def snapshot(m, states=None):
    return RecoverySnapshot(m, digest("revision"), tuple(states or [ItemState(i.item_id) for i in m.items]))


def authorized(tmp_path, *, m=None, states=None):
    m = m or manifest()
    ledger = RecoveryLedger(tmp_path)
    coordinator = RecoveryCoordinator(ledger)
    owner = RunOwnership(tmp_path).acquire()
    snap = snapshot(m, states)
    request = RecoveryRequest.from_preview(coordinator.plan(snap), "request-1")
    recovery_pass = coordinator.authorize(request, snap, owner)
    return coordinator, owner, recovery_pass, snap, request
