"""Public recovery facade. Importing it never initializes a transport."""
from .contracts import (
    SCHEMA_VERSION, AllowanceExceeded, ArtifactRef, AttemptRecord, EffectCertainty,
    EffectRecord, ItemState, OperationReference, OperationSpec, ParserRepair,
    RecoveryBlocked, RecoveryError, RecoveryIntegrityError, RecoveryPass, RecoveryPreview,
    RecoveryRequest, RecoverySelection, RecoverySnapshot, RunManifest, StaleRecoveryPreview,
    StepObservation, WorkItem, WorkerObservation, digest, json_value,
)
from .ledger import RecoveryLedger
from .coordinator import RecoveryCoordinator, next_action, plan_recovery
from .readout import FailureReadout, build_failure_readout
from .service import AttemptContext, OperationAdapter, RecoveryService
from ..execution.run_ownership import (
    ProcessIdentity, ProcessObservation, RunOwnership, capture_process_identity,
    inspect_process, inspect_run_ownership, inspect_worker_identity, process_identity,
)

__all__ = [name for name in globals() if not name.startswith("_")]
