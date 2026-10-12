"""Plan, start, inspect, continue and cancel visibility measurements."""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from ..config import (
    COPILOT_BROWSER_TURN_TIMEOUT_SECONDS,
    MeasurementConfig,
    Platform,
    ProfileIdentity,
    RetryPolicy,
    RuntimeConfig,
)
from ..errors import (
    ArtifactHashMismatchError,
    ArtifactIntegrityError,
    ConfigurationError,
    IntegrityError,
    RunNotFoundError,
    StateTransitionError,
    UnknownArtifactReferenceError,
)
from ..events import EventKind, EventSink, RunnerEvent, discard_event
from ..plan_serialization import (
    _run_plan_from_mapping as _plan_from_mapping,
    load_run_plan,
    validate_run_plan,
)
from ..models import (
    ArtifactReference,
    Issue,
    PacketKind,
    PacketPlan,
    PacketResult,
    RunPlan,
    RunResult,
    SubmissionBudget,
)
from ..planning.hashing import canonical_sha256, json_value
from ..result_acceptance import accepted_result
from ..platform_availability import require_active_execution, require_active_selection
from ..websites import (
    ChatGPTProvider,
    CopilotProvider,
    GeminiProvider,
    ProviderRegistry,
)
from ..websites.chat_actions import (
    _await_call,
    _abandon_background_browser_operations_for_loop,
    _BrowserOperationDeadline,
)
from ..status import (
    IssueSeverity,
    PacketStatus,
    RunStatus,
)
from ..storage.send_attempts import (
    SendAttempt, SendAttemptState, SendAttemptStore,
)
from ..storage.measurement_files import MeasurementFiles
from ..storage.progress_projection import (
    project_result, progress_fields, _observation_result, _measurement_group_attempts,
    _counts_from_records, _packet_owns_observation_result, _run_status,
    _safe_capability_issue_detail,
)
from .scheduler import ScheduledFailure, schedule_packets
from .run_measurement_group import (
    MeasurementGroupWorker,
    MeasurementGroupOutcome,
    _abandon_cancelled_provider_tasks_for_loop,
    _CancellationProbeFailed,
    _CancellationRequested,
    _RunCancellationWatcher,
    _safe_failure_context,
)


def quarantine_unconfirmed_cleanup(lease_manager, profile, *, platform, owner, recovery):
    """Write this pair's cleanup marker, unless the pair owns no tab.

    A marker whose ``resource_references`` list is empty can never be matched
    by a later pass (``_matching_containment_marker`` requires a nonempty list),
    so it refuses the pair for ever. When the recovery journal holds no owned
    tab for the pair there is nothing to protect: a creation without a
    trustworthy reply is already covered by the registry's own marker
    (``_quarantine_unconfirmed_owned_tabs``), and a tab this process never
    registered cannot be named in a marker at all. Without a recovery journal
    the references are unknown, and the marker is still written.
    """

    references = (
        ()
        if recovery is None
        else tuple(recovery.owned_resource_references(platform, (profile.id,)))
    )
    if recovery is not None and not references:
        return ()
    return lease_manager.quarantine(
        (profile,),
        owner=owner,
        provider=platform,
        resource_references=references,
        detail="runner could not confirm owned-tab cleanup",
    )


def page_open_limit(runtime: RuntimeConfig) -> Optional[asyncio.Semaphore]:
    """One pass-wide limit on page openings in flight, or ``None`` for none.

    ``max_concurrent_page_opens`` of 0 means no limit, which is the behaviour
    before this limit existed. A limit is created once per pass and handed to
    the group worker and to the recovery adapter, so every ``open`` and every
    ``reopen`` of that pass waits on the same counter.
    """

    value = int(runtime.max_concurrent_page_opens)
    if value <= 0:
        return None
    return asyncio.Semaphore(value)


class Runner:
    """Synchronous, explicit, in-process visibility measurement runner."""

    def __init__(
        self,
        runtime: RuntimeConfig,
        *,
        providers: Optional[
            ProviderRegistry | Mapping[str, object]
        ] = None,
        event_sink: EventSink = discard_event,
        browser_runtime_factory: Optional[object] = None,
        copilot_source_factory: Optional[object] = None,
    ):
        if not isinstance(runtime, RuntimeConfig):
            raise TypeError("runtime must be a RuntimeConfig")
        self.runtime = runtime
        if providers is None or isinstance(providers, ProviderRegistry):
            self._providers = providers
        elif isinstance(providers, Mapping):
            registry = ProviderRegistry()
            for driver in providers.values():
                registry.register(driver)  # type: ignore[arg-type]
            self._providers = registry
        else:
            raise TypeError("providers must be a ProviderRegistry or mapping")
        self.event_sink = event_sink
        self.browser_runtime_factory = browser_runtime_factory
        self.copilot_source_factory = copilot_source_factory
        self.repository = MeasurementFiles(runtime.artifact_root)
        self._cancel_events: dict[str, threading.Event] = {}
        self._active_browser_runtimes: dict[str, object] = {}
        self._pending_attachment_tasks: set[asyncio.Task] = set()
        self._guard = threading.Lock()

    # Compatibility views for browser restart and verification helpers. The
    # session is the only runtime binding; these hold no independent state.
    @property
    def _owners(self):
        return self.repository.execution_owners

    @property
    def _execution_thread_id(self):
        session = self.repository.active_session
        return session.thread_id if session is not None else None

    @property
    def _browser_startup_factory(self):
        session = self.repository.active_session
        return session.startup_factory if session is not None else None

    @property
    def _browser_startup_in_progress(self):
        session = self.repository.active_session
        return session.startup_in_progress if session is not None else False

    @contextmanager
    def execution(self, run_id: str, *, execution_id=None):
        """Own one stable run inode through provider and host-output cleanup."""
        from .run_ownership import RunOwnership
        thread_id = threading.get_ident()
        with self._guard:
            session = self.repository.active_session
            if session is not None and (
                session.thread_id != thread_id or session.run_id != run_id or session.owner is None
            ):
                raise StateTransitionError("runner_execution_already_owned")
            current = session.owner if session is not None else None
            if session is None:
                session = self.repository.begin_run_session(run_id, thread_id=thread_id)
        if current is not None:
            current.assert_owned()
            if execution_id is not None and current.execution_id != execution_id:
                raise StateTransitionError("different_execution_already_owned")
            yield current
            return
        try:
            # Inspect pre-anchor journal bindings before acquisition replaces the
            # prior owner. A legacy owner is independent durable evidence that
            # a recovery pass already existed, even when recovery/ was removed.
            from ..recovery.api import RecoveryLedger
            ledger = RecoveryLedger(self.repository.run_dir(run_id), watch_changes=False)
            ledger.read()
            with RunOwnership(self.repository.run_dir(run_id), execution_id=execution_id) as owner:
                session.owner = owner
                heartbeat_stop=threading.Event()
                def heartbeat_loop():
                    while not heartbeat_stop.wait(2):
                        try:
                            owner.heartbeat(progress=False)
                        except Exception:
                            return
                heartbeat_thread=threading.Thread(target=heartbeat_loop,daemon=True,name="visibility-owner-heartbeat")
                heartbeat_thread.start()
                try:
                    yield owner
                finally:
                    heartbeat_stop.set()
                    heartbeat_thread.join(timeout=1)
                    with self._guard:
                        session.owner = None
        finally:
            with self._guard:
                self.repository.end_run_session(session)

    def _existing_run_dir(self, run_id: str) -> Path:
        root = self.repository.run_dir(run_id)
        from stat import S_ISDIR
        try:
            mode = root.lstat().st_mode
        except FileNotFoundError:
            raise RunNotFoundError(f"measurement run does not exist: {run_id}") from None
        if not S_ISDIR(mode):
            raise IntegrityError("measurement run path is not a directory")
        return root

    def ownership(self, run_id: str):
        from .run_ownership import inspect_run_ownership
        import fcntl, os
        root = self._existing_run_dir(run_id)
        observed = inspect_run_ownership(root)
        try:
            progress=json.loads((root / "execution-heartbeat.json").read_text())
            if progress.get("execution_id")==observed.get("execution_id"):
                observed["progress"]={key:value for key,value in progress.items() if key in {"execution_id","pass_id","sampled_at","monotonic","progress_monotonic","operations","boot_id"}}
        except (OSError,ValueError,TypeError):
            observed["progress"] = None
        lock = root / ".run.lock"
        if not lock.exists():
            return {**observed, "state": "available"}
        fd = os.open(lock, os.O_RDONLY)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {**observed, "state": "owned"}
            fcntl.flock(fd, fcntl.LOCK_UN)
            state = "available" if observed.get("owner_state") == "released" or observed.get("status") in {"dead","reused"} else "unknown"
            return {**observed, "state":state}
        finally:
            os.close(fd)

    def release_stale_ownership(self, run_id: str, *, reason: str):
        """Audited operator release of an owner record that blocks a run.

        Refused while any process holds the run lock or the recorded process
        is verified alive; the old record is archived under execution-history.
        """
        from .run_ownership import release_stale_ownership
        with self._guard:
            if self.repository.session_for(run_id) is not None:
                raise StateTransitionError("runner_execution_already_owned")
        return release_stale_ownership(self.repository.run_dir(run_id), reason=reason)

    def verification_control(self, run_id: str, **command):
        """Explicit show/check/close of an existing verification episode only."""
        self._existing_run_dir(run_id)
        if set(command) != {"episode_id", "revision", "execution_id", "request_id", "action"}:
            raise ValueError("invalid_verification_command")
        from ..browser.verification_controls import control
        return control(self, run_id, **command)

    @contextmanager
    def defer_browser_startup(self, factory):
        """Bind host browser startup to this owned execution's source admission.

        The factory returns a context manager. Enter it before attachment and
        retain it through provider cleanup; a failed SDK admission never calls it.
        """
        session = self.repository.active_session
        if (session is None or session.owner is None or session.thread_id != threading.get_ident()
                or session.startup_factory is not None):
            raise StateTransitionError("deferred_browser_startup_requires_exclusive_execution")
        if not callable(factory):
            raise TypeError("browser startup factory must be callable")
        session.owner.assert_owned()
        session.startup_factory = factory
        try:
            yield
        finally:
            session.startup_factory = None

    def prepare_browser_restart(self, *, request_id, recovery_request=None):
        """Dispose proven retained tabs before an explicitly authorized restart.

        The caller must hold exclusive ownership of the Chrome lifecycle. This
        admits no provider effect and does not clear ambiguous-send fences.
        """
        from ..browser.restart import prepare
        return prepare(self, request_id=request_id, recovery_request=recovery_request)

    def enable_automatic_verification(self, run_id: str, *, request_id, request_sha256, plan=None, account_emails=None):
        """Authorize native verification for this owned Start/Refetch only.

        Historical runtime, execution policy and send fences remain unchanged.
        The host binds this opt-in to its immutable, explicit execution request.
        An initial plan also covers retrying a legacy job that never started.
        """
        from .verification_policy import enable
        return enable(self, run_id, request_id=request_id, request_sha256=request_sha256,
            plan=plan, account_emails=account_emails)

    @contextmanager
    def browser_restart(self, *, request_id, recovery_request=None):
        """Hold selected profile maintenance leases through the host restart.

        The host also holds its exclusive Chrome lifecycle lease. Release these
        leases before normal provider reservations begin in the fresh browser.
        """
        outcome = self.prepare_browser_restart(request_id=request_id, recovery_request=recovery_request)
        from ..browser.verification_controls import store_for_runtime
        store = store_for_runtime(self.runtime, "restart-inspection", "")
        with store.manager.acquire(tuple(store.profiles.values()),
                owner="visibility-runner:browser-restart:" + request_id,
                provider=None, timeout_seconds=0):
            yield outcome

    def plan_refetch(self, run_id: str, *, request_id=None):
        self._existing_run_dir(run_id)
        try:
            plan = self.repository.read_plan(run_id)
        except RunNotFoundError as exc:
            # The run exists; a missing immutable plan is corruption, not a
            # lookup miss. Keep the established read/refetch error contract.
            raise IntegrityError(str(exc)) from exc
        require_active_execution(plan)
        from ..recovery_adapters.visibility import plan_refetch
        return plan_refetch(self.repository, run_id, request_id=request_id or str(uuid.uuid4()),runtime=self.runtime)

    def failure_readout(self, run_id: str, *, worker_observation=None):
        from ..recovery_adapters.visibility import snapshot_for
        from ..recovery.api import build_failure_readout
        owner_observation=self.ownership(run_id)
        if worker_observation is None:
            worker_observation={k:v for k,v in owner_observation.items() if k in {"status","sampled_at","execution_id","reason_code"}}
        else:
            worker_observation=dict(worker_observation)
        worker_observation['ownership_status']=owner_observation.get('state','unknown')
        # A readout is read-only. A run whose frozen execution settings differ
        # from this runtime is still described; only plan_refetch/refetch/
        # resume/run keep refusing to act on it.
        policy_state = self._execution_policy_state(run_id)
        snapshot, verified = snapshot_for(self.repository,run_id,worker_observation=worker_observation,
                                   runtime=None if policy_state["frozen"] else self.runtime,
                                   verification_runtime=self.runtime)
        result = build_failure_readout(snapshot)
        readout = result.to_dict() if hasattr(result,"to_dict") else dict(result)
        readout["execution_policy"] = policy_state
        from ..browser.verification_controls import store_for_runtime
        verification_store = store_for_runtime(self.runtime, run_id, verified.plan["plan_sha256"])
        readout["verification"] = verification_store.public()
        observations = {row["observation_id"]: row for row in verified.plan["observation_plan"]["observations"]}
        from ..websites.usage_limits import project_usage_limit_issue
        for row in readout.get("items", ()):
            observation = observations.get(row.get("item_id"), {})
            if row["status"] != "succeeded":
                for packet in verified.plan["packets"]:
                    if row["item_id"] not in packet["observation_ids"]:
                        continue
                    for issue in reversed(verified.group_progress[packet["packet_id"]].get("issues", ())):
                        if issue.get("observation_id") not in {None, row["item_id"]}:
                            detail = issue.get("detail")
                            if not isinstance(detail, Mapping) or not detail.get("source_packet_id"):
                                continue
                        limit = project_usage_limit_issue(issue, profile_id=str(packet["profile_id"]), provider=str(packet["platform"]))
                        if limit is not None:
                            row.update(limit)
                            break
            if observation.get("platform") != "github_copilot":
                continue
            config = observation["github_copilot"]
            records = [r for r in verified.attempt_records if r.observation_id == row["item_id"]]
            saved = any(r.action == "copilot_answer" and r.state is SendAttemptState.ACKNOWLEDGED for r in records)
            source_uncertain = any(r.action == "copilot_answer" and r.state in {SendAttemptState.SEND_STARTED, SendAttemptState.RESPONSE_CAPTURED} for r in records)
            parser_uncertain = any(r.action == "gemini_parse" and r.state in {SendAttemptState.SEND_STARTED, SendAttemptState.RESPONSE_CAPTURED} for r in records)
            row.update(source_saved=saved, github_account_id=config["github_account_id"],
                       account_ref=config["account_ref"], source_method=config["source_method"], model=config["model"],
                       stage="accepted" if row["status"] == "succeeded" else "parser_inflight" if parser_uncertain
                             else "source_saved" if saved else "source_inflight" if source_uncertain else "source_pending")
            if row["status"] == "succeeded":
                continue
            for packet in verified.plan["packets"]:
                if row["item_id"] not in packet["observation_ids"]:
                    continue
                issues = verified.group_progress[packet["packet_id"]].get("issues", ())
                for issue in reversed(issues):
                    if issue.get("observation_id") not in {None, row["item_id"]}:
                        continue
                    detail = _safe_capability_issue_detail(issue)
                    if detail:
                        row["capability_detail"] = detail
                    break
        return readout

    def _execution_policy_state(self, run_id: str) -> dict:
        """Compare the run's frozen execution settings with this runtime."""
        from ..recovery_adapters.execution_policy import FILENAME, read_policy
        frozen = (self.repository.run_dir(run_id) / FILENAME).exists()
        state = {"frozen": frozen, "policy_sha256": None, "matches_current_runtime": True, "mismatch": None}
        if not frozen:
            return state
        try:
            plan = self.repository.read_plan(run_id)
        except RunNotFoundError as exc:
            # The caller already established that this run directory exists.
            # A missing frozen plan is corruption, not safely unstarted work.
            raise IntegrityError(str(exc)) from exc
        try:
            policy = read_policy(self.repository, run_id, plan, runtime=self.runtime)
            state["policy_sha256"] = policy.get("policy_sha256") if policy else None
        except StateTransitionError as exc:
            state["matches_current_runtime"] = False
            state["mismatch"] = str(exc)
            policy = read_policy(self.repository, run_id, plan, runtime=None)
            state["policy_sha256"] = policy.get("policy_sha256") if policy else None
        return state

    def failure_readout_for_plan(self, plan, *, worker_observation=None):
        """Project a frozen never-executed plan without creating run artifacts."""
        from ..recovery.api import RecoverySnapshot,ItemState,WorkerObservation,build_failure_readout
        from ..recovery_adapters.visibility import manifest_for
        import dataclasses
        typed=plan if isinstance(plan,RunPlan) else load_run_plan(plan)
        self._validate_plan(typed)
        payload=json_value(typed)
        from ..recovery_adapters.execution_policy import make_policy
        manifest=manifest_for(payload,execution_policy=make_policy(self.runtime,payload,source='new_run'))
        worker=None
        if worker_observation is not None:
            fields={field.name for field in dataclasses.fields(WorkerObservation)}
            worker=WorkerObservation(**{key:value for key,value in worker_observation.items() if key in fields})
        snapshot=RecoverySnapshot(manifest,canonical_sha256({'plan':typed.plan_sha256,
            'results':[],'attempts':[],'events':[],'cancel':False}),
            tuple(ItemState(item.item_id,status='never_started',reason_code='never_started') for item in manifest.items),
            worker=worker)
        readout=build_failure_readout(snapshot)
        return readout.to_dict() if hasattr(readout,'to_dict') else readout

    def resolve_recovery_link(self, run_id, reference):
        from ..recovery.api import RecoveryLedger
        from ..planning.hashing import canonical_sha256
        from ..websites.conversation_binding import canonical_conversation_url
        reference=str(reference)
        verified=self.repository.read_verified_snapshot(run_id)
        for record in verified.attempt_records:
            if record.response_reference==reference and record.response_sha256:
                path=self.repository.run_dir(run_id)/reference
                payload=path.read_bytes()
                from ..planning.hashing import bytes_sha256
                if bytes_sha256(payload)!=record.response_sha256:
                    raise IntegrityError("recovery_evidence_hash_mismatch")
                return {"kind":"artifact","reference":{"path":reference,"sha256":record.response_sha256,"byte_length":len(payload),"media_type":"text/plain; charset=utf-8"}}
        for event in reversed(RecoveryLedger(self.repository.run_dir(run_id)).read()):
            row=event["payload"]
            if event["kind"]!="correlation" or reference!="conversations/"+canonical_sha256({"attempt":row["source_attempt_id"]}):
                continue
            url=canonical_conversation_url(row["binding"]["identity"].get("conversation_reference"),
                ("https://chatgpt.com/c/","https://chatgpt.com/g/","https://gemini.google.com/app/","https://copilot.microsoft.com/chats/","https://copilot.com/chats/","https://copilot.com/chat/conversation/"))
            return {"kind":"conversation","url":url}
        raise UnknownArtifactReferenceError("unknown_recovery_link")

    def refetch(self, request, *, replace_checked_sends=True):
        """Run one recovery pass, optionally keeping uncertain sends read-only.

        Automatic recovery rounds pass ``False`` so checking an uncertain send
        never becomes permission to send it again on a later round.
        """
        from ..recovery.api import RecoveryRequest
        _require_sync_caller()
        request = request if isinstance(request,RecoveryRequest) else RecoveryRequest.from_dict(request)
        refreshed = getattr(self, "_restart_recovery_request", None)
        if refreshed is not None and request.to_dict() == refreshed[0]:
            request = RecoveryRequest.from_dict(refreshed[1])
            self._restart_recovery_request = None
        if request.parser_repairs:
            raise StateTransitionError("visibility_parser_repair_implementation_not_registered")
        run_id=request.run_id
        require_active_execution(self.repository.read_plan(run_id))
        with self.execution(run_id) as owner:
            with self._recovery_scope(run_id, owner, request=request,
                                      replace_checked_sends=replace_checked_sends) as context:
                if context is None:
                    return self.inspect(run_id)
                def reopen_state(state):
                    # Another automatic round cannot withdraw a Stop for this
                    # execution. A newly authorized execution may recover an
                    # older cancelled job through the same public boundary.
                    if (not replace_checked_sends and state.get("execution_id") == owner.execution_id
                            and (state.get("cancel_requested") or state.get("status") == "cancelled")):
                        return
                    state.update(cancel_requested=False, status="planned", execution_id=owner.execution_id,
                                 recovery_pass_id=context.pass_id, issues=[])
                reopened = self.repository.update_state(run_id, reopen_state)
                if reopened.get("cancel_requested") or reopened.get("status") == "cancelled":
                    context.coordinator.cancel_pass(context.pass_id, owner)
                    return self.inspect(run_id)
                self._cancel_events[run_id] = threading.Event()
                # Reset only packets selected by this pass. Repair completed
                # peers without executing them or changing cancellation.
                selected_packet_ids = []
                for packet in context.verified.plan["packets"]:
                    packet_id = str(packet["packet_id"])
                    observation_ids = tuple(str(value) for value in packet["observation_ids"])
                    if any(value in context.selected for value in observation_ids):
                        selected_packet_ids.append(packet_id)
                        def reset_packet(state):
                            state.update(status="planned", issues=[], completed_observation_ids=[
                                value for value in state.get("completed_observation_ids", ())
                                if value not in context.selected])
                        self.repository.update_packet_state(run_id, packet_id, reset_packet)
                    else:
                        self._repair_complete_packet(run_id, packet_id, observation_ids)
                if selected_packet_ids:
                    owner.heartbeat(progress=True)
                    self._execute_owned(run_id, tuple(selected_packet_ids), resume=True)
                context.settle()
                if not selected_packet_ids:
                    self._persist_durable_run_state(run_id)
            return self.inspect(run_id)

    def _repair_complete_packet(self, run_id, packet_id, observation_ids) -> bool:
        """Repair a stale packet projection after all required results committed.

        A late storage exception can mark a packet failed even when its
        immutable result revision committed. Verified complete results repair
        that derived status without provider effects; explicit cancellation
        remains unchanged. Returns True when the state was written.
        """
        repairable={"planned","running","failed","partial","needs_attention"}
        state=self.repository.read_group_progress(run_id,packet_id)
        if str(state.get("status") or "").casefold() not in repairable:
            return False
        results={str(row.get("observation_id") or ""):row
                 for row in self.repository.iter_observation_results(run_id,observation_ids=tuple(observation_ids))}
        if not observation_ids or not all(accepted_result(results.get(value)) for value in observation_ids):
            return False
        def finish(state):
            if str(state.get("status") or "").casefold() in repairable:
                state["status"]="succeeded"
                state["completed_observation_ids"]=list(observation_ids)
        self.repository.update_packet_state(run_id,packet_id,finish)
        return True

    def plan(
        self,
        config: MeasurementConfig | Mapping[str, object],
        *,
        run_id: Optional[str] = None,
    ) -> RunPlan:
        """Build an immutable authorized plan without filesystem/browser I/O."""

        from ..planning.build_measurement_plan import build_run_plan

        from ..client_research import ClientIdentificationConfig, ClientRecommendationConfig
        if isinstance(config, (ClientIdentificationConfig, ClientRecommendationConfig)):
            config = config.to_mapping()
        typed = (
            config
            if isinstance(config, MeasurementConfig)
            else MeasurementConfig.from_mapping(config)
        )
        require_active_selection(typed.platforms)
        if not self.runtime.selected_profiles:
            raise ConfigurationError(
                "pure planning requires selected_profiles with each exact raw "
                "Chrome directory; profile IDs alone can be resolved only by "
                "the explicit legacy environment/browser adapter"
            )
        resolved_id = run_id or f"visibility-{uuid.uuid4().hex[:16]}"
        return build_run_plan(
            typed,
            run_id=resolved_id,
            selected_profile_ids=self.runtime.selected_profile_ids,
            profile_manifest_sha256=self.runtime.profile_manifest_sha256,
            selected_profiles=self.runtime.selected_profiles,
            retry_policy=self.runtime.retry_policy,
            tabs_per_provider=self.runtime.tabs_per_provider,
        )

    def create(self, plan: RunPlan) -> RunResult:
        """Persist a new immutable plan and send nothing."""

        self._validate_plan(plan)
        self._verify_profile_manifest()
        from ..recovery_adapters.execution_policy import make_policy
        self.repository.create(plan,execution_policy=make_policy(self.runtime,json_value(plan),source='new_run'))
        self._emit(EventKind.RUN_CREATED, plan.run_id)
        return self.inspect(plan.run_id)

    def run(self, plan: RunPlan) -> RunResult:
        """Create and synchronously execute a new plan; never imply resume."""

        _require_sync_caller()
        require_active_execution(plan)
        with self.execution(plan.run_id):
            self.create(plan)
            return self._execute(
                plan.run_id,
                tuple(packet.packet_id for packet in plan.packets),
                resume=False,
            )

    def run_packet(self, run_id: str, packet_id: str) -> PacketResult:
        """Execute one frozen, still-planned measurement group independently."""

        _require_sync_caller()
        require_active_execution(self.repository.read_plan(run_id))
        with self.execution(run_id):
            return self._run_packet_owned(run_id, packet_id)

    def _run_packet_owned(self, run_id: str, packet_id: str) -> PacketResult:
        self.repository.read_measurement_group(run_id, packet_id)
        runner_state = self.repository.read_progress(run_id)
        if bool(runner_state.get("cancel_requested")) or str(
            runner_state.get("status") or ""
        ).casefold() == "cancelled":
            raise StateTransitionError(
                "the run was explicitly cancelled and cannot execute packets"
            )
        state = self.repository.read_group_progress(run_id, packet_id)
        if str(state.get("status") or "").casefold() != "planned":
            raise StateTransitionError(
                "run_packet() executes only a planned packet and never implies "
                "resume"
            )
        self._execute(run_id, (packet_id,), resume=False)
        result = self.inspect(run_id)
        return next(
            value for value in result.packets if value.packet_id == packet_id
        )

    def inspect(self, run_id: str) -> RunResult:
        """Read and verify durable state without browser/website interaction."""

        self._existing_run_dir(run_id)
        verified = self.repository.read_verified_snapshot(run_id)
        ownership = self.ownership(run_id)
        return project_result(
            load_run_plan(verified.plan), verified.observation_results,
            verified.attempt_records, verified.runner_state, verified.group_progress,
            ownership=ownership,
        )

    def read_artifact(
        self,
        run_id: str,
        reference: ArtifactReference | Mapping[str, object],
    ) -> bytes:
        """Read one hash-bound run artifact and verify its stored bytes."""

        if isinstance(reference, ArtifactReference):
            stored_reference: Mapping[str, object] = {
                "path": reference.relative_path,
                "sha256": reference.sha256,
                "byte_length": reference.byte_length,
                "media_type": reference.media_type,
            }
        elif isinstance(reference, Mapping):
            path_value = reference.get("path") or reference.get(
                "relative_path"
            )
            if not isinstance(path_value, str) or not path_value.strip():
                raise UnknownArtifactReferenceError(
                    "artifact reference path is missing"
                )
            relative_path = Path(path_value)
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise UnknownArtifactReferenceError(
                    "artifact reference path is invalid"
                )
            expected_sha256 = reference.get("sha256")
            if (
                not isinstance(expected_sha256, str)
                or len(expected_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in expected_sha256.casefold()
                )
            ):
                raise ArtifactHashMismatchError(
                    "artifact reference requires a valid SHA-256"
                )
            byte_length = reference.get("byte_length")
            if (
                isinstance(byte_length, bool)
                or not isinstance(byte_length, int)
                or byte_length < 0
            ):
                raise ArtifactIntegrityError(
                    "artifact reference requires a non-negative byte length"
                )
            stored_reference = {
                **reference,
                "path": path_value,
                "sha256": expected_sha256.casefold(),
                "byte_length": byte_length,
            }
        else:
            raise UnknownArtifactReferenceError(
                "artifact reference must be an ArtifactReference or mapping"
            )
        return self.repository.read_evidence(run_id, stored_reference)

    def resume(self, run_id: str) -> RunResult:
        """Resume only work proven safe by durable no-send/acknowledged send attempts."""

        _require_sync_caller()
        require_active_execution(self.repository.read_plan(run_id))
        with self.execution(run_id):
            return self._resume_owned(run_id)

    def _resume_owned(self, run_id: str) -> RunResult:
        try:
            verified = self.repository.read_verified_snapshot(run_id)
        except IntegrityError as exc:
            self._mark_attention(run_id, str(exc))
            return self.inspect(run_id)
        plan_payload = verified.plan
        runner_state = verified.runner_state
        if bool(runner_state.get("cancel_requested")) or str(
            runner_state.get("status") or ""
        ).casefold() == "cancelled":
            return self.inspect(run_id)
        results = {
            str(row.get("observation_id") or "")
            for row in verified.observation_results
        }
        fence_records = verified.attempt_records
        if (
            str(runner_state.get("status") or "").casefold() == "planned"
            and not results
            and not fence_records
        ):
            # create()+resume() must not become a second spelling of run().
            return self.inspect(run_id)
        # MeasurementGroupWorker reconstructs durable blocked access to a profile and website pair from
        # unresolved send attempts. It refuses every peer on that same profile and website pair while
        # still allowing truly independent profile and website pairs to resume.
        pending_packets: list[str] = []
        repaired = False
        for packet in plan_payload.get("packets") or ():
            packet_id = str(packet["packet_id"])
            state = verified.group_progress[packet_id]
            completed = {
                str(value)
                for value in state.get("completed_observation_ids") or ()
            }
            observation_ids = tuple(
                str(value) for value in packet.get("observation_ids") or ()
            )
            if any(value not in completed for value in observation_ids):
                pending_packets.append(packet_id)
            elif str(state.get("status") or "").casefold() in {"planned", "running"}:
                # Every prompt measurement of this packet is complete but the
                # worker died before it wrote the packet's terminal state.
                # Resume never executes such a packet, so give it its state
                # here instead of leaving the run "partial" for good.
                repaired = self._repair_complete_packet(run_id, packet_id, observation_ids) or repaired
        packet_ids = tuple(pending_packets)
        if not packet_ids:
            if repaired:
                self._persist_durable_run_state(run_id)
            return self.inspect(run_id)
        return self._execute(run_id, packet_ids, resume=True, _verified=verified)

    def cancel(self, run_id: str, *, execution_id: str | None = None) -> RunResult:
        """Cancel only module-owned work/tabs; persistent Chrome is untouched."""

        # Resolve first so a miss cannot create state.
        plan = self.repository.read_plan(run_id)

        target_execution_id = None
        terminal = {"succeeded", "failed", "needs_attention"}

        def request_durable_cancel(state: dict[str, Any]) -> None:
            nonlocal target_execution_id
            target_execution_id = state.get("execution_id")
            if execution_id is not None and target_execution_id != execution_id:
                raise StateTransitionError("stale_execution_cancellation")
            state["cancel_requested"] = True
            self.repository.write_execution_control(run_id,state)
            if str(state.get("status") or "").casefold() not in terminal:
                state["status"] = "cancelled"
            current=state.get('packet_statuses') or {}
            state['packet_statuses']={str(packet['packet_id']):
                current.get(str(packet['packet_id'])) if current.get(str(packet['packet_id'])) in terminal else 'cancelled'
                for packet in plan.get('packets',())}

        # Publish the authoritative barrier first. Every following mutation is
        # serialized against execution-epoch publication by that same inode.
        self.repository.update_state(run_id, request_durable_cancel)
        browser_runtime = None
        with self.repository._locked(self.repository.run_dir(run_id) / 'runner_state.json'):
            current=self.repository.read_progress(run_id)
            if current.get('execution_id') != target_execution_id:
                raise StateTransitionError('stale_execution_cancellation')
            with self._guard:
                owner=self.repository.owner_for(run_id)
                if owner is not None and owner.execution_id != target_execution_id:
                    raise StateTransitionError('stale_execution_cancellation')
                event=self._cancel_events.setdefault(run_id,threading.Event())
                browser_runtime=self._active_browser_runtimes.get(run_id)
                event.set()
            for packet in plan.get('packets',()):
                def cancel_packet(state):
                    if str(state.get('status') or '').casefold() not in terminal:
                        state['status']='cancelled'
                self.repository.update_packet_state(run_id,str(packet['packet_id']),cancel_packet)
        # Capture the exact runtime while its execution matched. A delayed
        # callback cannot look up and cancel a later runtime for this run ID.
        if browser_runtime is not None:
            cancel=getattr(browser_runtime,'request_cancel',None)
            if callable(cancel):
                cancel()
        if self.repository.read_progress(run_id).get('execution_id') == target_execution_id:
            self._emit(EventKind.CANCEL_REQUESTED,run_id)
        return self.inspect(run_id)

    @contextmanager
    def _recovery_scope(self, run_id, owner, *, request=None, resume=False, verified=None,
                        replace_checked_sends=True):
        """Admit one pass and own its policy, read indexes and cleanup."""
        from ..recovery.api import RecoveryLedger, RecoveryCoordinator, RecoveryRequest
        from ..recovery_adapters.visibility import snapshot_for, VisibilityExecution
        from ..recovery_adapters.execution_policy import read_policy, publish_policy

        if self.repository.recovery_execution is not None:
            raise StateTransitionError("execution_has_active_pass")
        ledger = RecoveryLedger(self.repository.run_dir(run_id))
        try:
            if request is None:
                events = ledger.read()
                terminal = {e["payload"]["pass_id"] for e in events
                            if e["kind"] in {"pass_completed", "pass_cancelled"}}
                active = [e["payload"] for e in events
                          if e["kind"] == "pass_authorized" and e["payload"]["pass_id"] not in terminal]
                if events and not active:
                    # Start/Resume cannot reopen a completed authorization.
                    yield None
                    return

            snapshot, verified = snapshot_for(
                self.repository, run_id, verified=verified, runtime=self.runtime,
            )
            coordinator = RecoveryCoordinator(ledger)
            if request is not None:
                recovery_pass = coordinator.authorize(request, snapshot, owner)
                if recovery_pass.duplicate:
                    yield None
                    return
                initial = False
            else:
                manifests = [e["payload"]["manifest"] for e in events if e["kind"] == "manifest"]
                if manifests and manifests[-1]["scope_digest"] != snapshot.manifest.scope_digest:
                    raise StateTransitionError("frozen_execution_policy_mismatch")
                if active:
                    recovery_pass = coordinator.resume_pass(active[-1]["pass_id"], owner)
                    initial = active[-1]["request"]["request_id"].startswith(("initial:", "original:"))
                elif verified.attempt_records or resume:
                    policy = (RetryPolicy.from_mapping(verified.plan["retry_policy"])
                              if verified.plan.get("retry_policy") else RetryPolicy.disabled())
                    recovery_pass = coordinator.import_original_authorization(
                        snapshot, owner,
                        attempt_limits={op.operation_id: policy.authorized_max_attempts
                                        for op in snapshot.manifest.operations},
                    )
                    initial = True
                else:
                    initial_request = RecoveryRequest.from_preview(coordinator.plan(snapshot), f"initial:{run_id}")
                    recovery_pass = coordinator.authorize(initial_request, snapshot, owner)
                    initial = True

            policy = read_policy(self.repository, run_id, verified.plan,
                                 runtime=self.runtime, state=verified.runner_state)
            publish_policy(self.repository, run_id, policy, owner)
            context = VisibilityExecution(
                self.repository, snapshot, verified, recovery_pass, coordinator, owner,
                initial=initial, page_open_limit=page_open_limit(self.runtime),
                replace_checked_sends=replace_checked_sends,
            )
            self.repository.recovery_execution = context
            try:
                yield context
            finally:
                try:
                    context.close()
                finally:
                    self.repository.recovery_execution = None
        finally:
            ledger.close()

    def _execute(self, run_id, packet_ids, *, resume, _verified=None):
        require_active_execution(_verified.plan if _verified is not None else self.repository.read_plan(run_id))
        with self.execution(run_id) as owner:
            owner.heartbeat(progress=True)
            self.repository.update_state(run_id, lambda state: state.update(execution_id=owner.execution_id))
            with self._recovery_scope(run_id, owner, resume=resume, verified=_verified) as context:
                if context is None:
                    return self.inspect(run_id)
                result = self._execute_owned(run_id, packet_ids, resume=resume)
                all_packets = {packet["packet_id"] for packet in context.verified.plan["packets"]}
                complete = (set(packet_ids) == all_packets
                            or context.selected <= context.finished | context.failed)
                if complete and not context.preserve_resumable_preparation():
                    context.settle()
                return result

    def _execute_owned(
        self,
        run_id: str,
        packet_ids: Sequence[str],
        *,
        resume: bool,
    ) -> RunResult:
        context = self.repository.recovery_execution
        persisted = context.verified.runner_state if context is not None and context.initial else self.repository.read_progress(run_id)
        if bool(persisted.get("cancel_requested")) or str(
            persisted.get("status") or ""
        ).casefold() == "cancelled":
            return self.inspect(run_id)
        self._verify_profile_manifest()
        plan = context.verified.plan if context is not None else self.repository.read_plan(run_id)
        packet_by_id={str(packet['packet_id']):packet for packet in plan['packets']}
        packets = [packet_by_id[packet_id] for packet_id in packet_ids]
        cancellation = self._cancel_events.setdefault(
            run_id,
            threading.Event(),
        )

        def is_cancelled() -> bool:
            if cancellation.is_set():
                return True
            if self.repository.cancellation_requested(run_id):
                cancellation.set()
                return True
            return False

        if not resume and cancellation.is_set():
            raise StateTransitionError("the run is already cancelled")

        def running(state: dict[str, Any]) -> None:
            if not bool(state.get("cancel_requested")) and str(
                state.get("status") or ""
            ).casefold() != "cancelled":
                state["status"] = "running"

        started_state = self.repository.update_state(run_id, running)
        if bool(started_state.get("cancel_requested")) or str(
            started_state.get("status") or ""
        ).casefold() == "cancelled":
            cancellation.set()
            return self.inspect(run_id)
        self._emit(EventKind.RUN_STARTED, run_id)
        # The recovery adapter of this pass already holds the pass's page-open
        # limit; the worker must wait on that same counter, so a reopen in
        # `prepare_item` and an `open` in a packet share it.
        opening_limit = (
            context.page_open_limit
            if context is not None
            else page_open_limit(self.runtime)
        )

        startup_error = None
        synchronous_contexts = _SynchronousContexts()

        async def execute_all():
            nonlocal startup_error
            watcher = _RunCancellationWatcher(is_cancelled, cancellation)
            async with AsyncExitStack() as stack:
                worker = MeasurementGroupWorker(
                    self.repository,
                    ProviderRegistry(),
                    cancel_event=cancellation,
                    cancelled=is_cancelled,
                    cancellation_watcher=watcher,
                    event_sink=self.event_sink,
                    cancellation_settlement_seconds=(
                        self.runtime.cancellation_settlement_seconds
                    ),
                    page_open_limit=opening_limit,
                )
                worker.copilot_runtime = getattr(self.runtime, "github_copilot", None)
                worker.copilot_source_factory = self.copilot_source_factory
                await stack.enter_async_context(worker.run_scope(run_id))
                await self._admit_copilot_source(worker, run_id, plan, packets, stack)
                if watcher.is_cancelled():
                    raise _CancellationRequested("cancelled_before_browser_startup")
                session = self.repository.session_for(run_id)
                if session is not None and session.startup_factory is not None:
                    session.startup_in_progress = True
                    try:
                        await synchronous_contexts.enter_context(stack, session.startup_factory)
                    except Exception as exc:
                        startup_error = exc
                        raise
                    finally:
                        session.startup_in_progress = False
                if watcher._check_now():
                    raise _CancellationRequested("cancelled_after_browser_startup")
                worker.bind_providers(await stack.enter_async_context(self._provider_scope(
                    run_id, packets, _cancelled=watcher.is_cancelled,
                )))
                # Resolve original SDK sends before admitting any new account
                # work on another parser profile. Queue order alone cannot prove
                # that a historical in-flight account request has settled.
                if worker.recovery is not None and not worker.recovery.initial:
                    uncertain_sources = {record.observation_id for record in worker.recovery.verified.attempt_records
                        if record.action == "copilot_answer" and record.state in {SendAttemptState.SEND_STARTED, SendAttemptState.RESPONSE_CAPTURED}}
                    rows = {row["observation_id"]: row for row in plan["observation_plan"]["observations"]}
                    for packet in packets:
                        for item_id in packet["observation_ids"]:
                            if item_id in uncertain_sources and item_id in worker.recovery.selected:
                                token = worker._begin_operation_scope(run_id, packet["packet_id"], item_id)
                                try:
                                    await worker.recovery.prepare_item(worker, item_id, rows[item_id], packet)
                                finally:
                                    worker._finish_operation_scope(run_id, packet["packet_id"], item_id, token)

                # One owned-tab slot per measurement group, numbered within its
                # (profile, website, kind) group in frozen plan order.
                slot_by_packet, _ = _packet_tab_slots(plan.get("packets") or ())

                async def one(packet):
                    packet_id = str(packet["packet_id"])
                    # Open this pair's tabs in waves: a measurement group waits its own
                    # slot's share of the stagger before its first website
                    # page load, so the tabs of one profile/website pair do
                    # not navigate at the same moment.
                    stagger = self.runtime.packet_start_stagger_seconds
                    slot = slot_by_packet.get(packet_id, 0)
                    if stagger > 0 and slot > 0 and not watcher.is_cancelled():
                        try:
                            await worker._await_with_cancellation(
                                run_id, asyncio.sleep(stagger * slot),
                                detail="module cancellation interrupted packet staggering",
                            )
                        except _CancellationRequested:
                            pass
                    if watcher.is_cancelled():
                        return MeasurementGroupOutcome(
                            packet_id=packet_id,
                            status="cancelled",
                            completed_observation_ids=(),
                            issues=(),
                            transport_counts={},
                        )

                    def mark_packet(state: dict[str, Any]) -> None:
                        if (
                            str(state.get("status") or "").casefold()
                            != "cancelled"
                            and not watcher.is_cancelled()
                        ):
                            state["status"] = "running"

                    packet_state = self.repository.update_packet_state(
                        run_id,
                        packet_id,
                        mark_packet,
                    )
                    if watcher.is_cancelled() or str(
                        packet_state.get("status") or ""
                    ).casefold() == "cancelled":
                        return MeasurementGroupOutcome(
                            packet_id=packet_id,
                            status="cancelled",
                            completed_observation_ids=(),
                            issues=(),
                            transport_counts={},
                        )
                    self._emit(EventKind.PACKET_STARTED, run_id, packet_id)
                    outcome = await asyncio.wait_for(
                        worker.execute(
                            run_id=run_id,
                            plan=plan,
                            packet=packet,
                            resume=resume,
                        ),
                        timeout=_packet_worker_timeout_seconds(
                            configured_base_seconds=(
                                self.runtime.worker_timeout_seconds
                            ),
                            plan=plan,
                            packet=packet,
                        ),
                    )
                    self._emit(EventKind.PACKET_COMPLETED, run_id, packet_id)
                    return outcome

                # Deferred Google plans are rejected before execution. Active
                # groups share one scheduling phase, including SDK + parser.
                if any(
                    str(packet.get("kind") or "measurement") not in {
                        PacketKind.MEASUREMENT.value, PacketKind.SOURCE_PARSER.value,
                    }
                    for packet in packets
                ):
                    raise IntegrityError("run contains an unsupported packet kind")

                def measurement_group_resource_key(packet):
                    profile_id = str(packet.get("profile_id") or "")
                    provider = _packet_browser_provider(packet)
                    slot = slot_by_packet.get(
                        str(packet.get("packet_id") or ""),
                        0,
                    )
                    # One key per owned tab: measurement groups of the same
                    # (profile, website) pair overlap only across slots.
                    return (profile_id, provider, slot)

                outcomes = await schedule_packets(
                    packets,
                    one,
                    max_concurrency=self.runtime.max_concurrency,
                    resource_key=measurement_group_resource_key,
                )
            # Provider teardown may await while the watcher still owns the
            # run signal. Observe failures after every resource scope exits;
            # an exception from teardown itself keeps its original precedence.
            watcher.raise_if_failed()
            return outcomes

        try:
            settled = _run_sync(
                execute_all(),
                synchronous_contexts=synchronous_contexts,
                shutdown_timeout_seconds=(
                    self.runtime.cancellation_settlement_seconds
                ),
            )
        except Exception as exc:
            probe_failed = isinstance(exc, _CancellationProbeFailed)
            cancelled = cancellation.is_set() if probe_failed else is_cancelled()
            requires_attention = not cancelled and (
                isinstance(exc, IntegrityError)
                or "cleanup" in type(exc).__name__.casefold()
                or "quarantin" in type(exc).__name__.casefold()
            )
            failure_status = (
                "cancelled"
                if cancelled
                else "needs_attention"
                if requires_attention
                else "failed"
            )
            issue = {
                "code": (
                    "cancelled"
                    if cancelled
                    else "runtime_needs_attention"
                    if requires_attention
                    else "runtime_execution_failed"
                ),
                "message": (
                    "module cancellation interrupted browser execution"
                    if cancelled
                    else f"{type(exc).__name__}: {exc}"
                ),
                "recoverable": cancelled or not requires_attention,
            }
            from ..providers.github_copilot import CopilotSourceError
            if isinstance(exc, CopilotSourceError) and not cancelled:
                issue.update(code=exc.code, message="GitHub Copilot source admission failed before browser startup.")
                if exc.capability_detail is not None:
                    issue["capability_detail"] = exc.capability_detail
            for packet in packets:
                packet_id = str(packet["packet_id"])

                def fail_requested_packet(
                    state: dict[str, Any],
                    issue=issue,
                ) -> None:
                    if (
                        str(state.get("status") or "").casefold()
                        == "cancelled"
                        or (
                            not probe_failed
                            and self.repository.cancellation_requested(run_id)
                        )
                    ):
                        return
                    if str(state.get("status") or "").casefold() != "succeeded":
                        state["status"] = failure_status
                    state.setdefault("issues", []).append(issue)

                self.repository.update_packet_state(
                    run_id,
                    packet_id,
                    fail_requested_packet,
                )

            if probe_failed and not cancelled:
                def stop_for_probe_failure(state: dict[str, Any]) -> None:
                    if not bool(state.get("cancel_requested")) and str(
                        state.get("status") or ""
                    ).casefold() != "cancelled":
                        # Completed packet evidence remains valid, but a run
                        # whose safety signal failed during teardown cannot
                        # be projected as fully successful from those packets.
                        state["status"] = "needs_attention"

                self.repository.update_state(run_id, stop_for_probe_failure)
            self._persist_durable_run_state(run_id, issues=(issue,))
            if requires_attention:
                self._emit(EventKind.NEEDS_ATTENTION, run_id)
            self._emit(EventKind.RUN_COMPLETED, run_id)
            if exc is startup_error:
                raise
            return self.inspect(run_id)
        unexpected: list[dict[str, object]] = []
        for packet, outcome in zip(packets, settled):
            packet_id = str(packet["packet_id"])
            if isinstance(outcome, ScheduledFailure):
                observation_ids = {
                    str(value)
                    for value in packet.get("observation_ids") or ()
                }
                fence_records = SendAttemptStore(
                    self.repository.run_dir(run_id) / "fences"
                ).iter_records()
                # Under a Refetch pass only this pass's sends can explain the
                # packet error. Unresolved sends of earlier passes are the
                # work the pass was selected to handle; counting them relabels
                # every packet error as an uncertain send and hides its cause.
                unresolved = tuple(
                    record
                    for record in fence_records
                    if record.observation_id in observation_ids
                    and record.state
                    in {
                        SendAttemptState.SEND_STARTED,
                        SendAttemptState.RESPONSE_CAPTURED,
                    }
                    and (context is None or context.includes(record))
                )
                needs_attention = bool(unresolved)
                typed_plan = load_run_plan(plan)
                typed_packet = next(
                    candidate
                    for candidate in typed_plan.packets
                    if candidate.packet_id == packet_id
                )
                useful_evidence = _packet_has_useful_durable_evidence(
                    packet=typed_packet,
                    plan=typed_plan,
                    observation_results=(
                        self.repository.iter_observation_results(run_id)
                    ),
                    fence_records=fence_records,
                )
                issue = {
                    "packet_id": packet_id,
                    "code": (
                        "ambiguous_send"
                        if needs_attention
                        else "packet_worker_failed"
                    ),
                    "message": (
                        "packet worker stopped with unresolved sent "
                        "attempt(s): "
                        + ", ".join(
                            record.fence_id for record in unresolved
                        )
                        if needs_attention
                        else (
                            f"{type(outcome.error).__name__}: "
                            f"{outcome.error}"
                        )
                    ),
                    "recoverable": not needs_attention,
                    # Type names only; the message above may be replaced, so
                    # the packet's real error type is always kept here.
                    "detail": {
                        "failure_context": _safe_failure_context(
                            outcome.error,
                            phase="packet_execution",
                        ),
                    },
                }
                unexpected.append(issue)

                def fail_packet(
                    state: dict[str, Any],
                    issue=issue,
                    useful_evidence=useful_evidence,
                ) -> None:
                    if (
                        str(state.get("status") or "").casefold()
                        == "cancelled"
                        or self.repository.cancellation_requested(run_id)
                    ):
                        return
                    state["status"] = (
                        "needs_attention"
                        if issue["code"] == "ambiguous_send"
                        else "partial"
                        if useful_evidence
                        else "failed"
                    )
                    state.setdefault("issues", []).append(issue)

                self.repository.update_packet_state(
                    run_id,
                    packet_id,
                    fail_packet,
                )
        self._persist_durable_run_state(run_id, issues=unexpected)
        self._emit(EventKind.RUN_COMPLETED, run_id)
        return self.inspect(run_id)

    def _persist_durable_run_state(
        self,
        run_id: str,
        *,
        issues: Sequence[Mapping[str, object]] = (),
    ) -> None:
        """Project every durable measurement group/result/send attempt into runner state.

        ``run_packet()`` executes only one measurement group, so its outcome alone cannot
        determine the run status or cumulative transport counts.  Reuse the
        verified inspection projection after measurement group state has settled, then
        persist that complete view for readers of ``runner_state.json``.
        """

        def finish(state: dict[str, Any]) -> None:
            if bool(state.get("cancel_requested")) or str(
                state.get("status") or ""
            ).casefold() == "cancelled":
                return
            # Compute while holding the runner-state update lock. Otherwise an
            # earlier worker can calculate PARTIAL, a peer can persist
            # SUCCEEDED, and the earlier stale projection can overwrite it.
            durable = self.inspect(run_id)
            state.update(progress_fields(durable))
            state.setdefault("issues", []).extend(issues)

        self.repository.update_state(run_id, finish)

    async def _admit_copilot_source(self, worker, run_id, plan, packets, stack):
        """Check new SDK source work before Chrome and reuse its owned worker.

        Saved answers need only their parser. Uncertain original SDK sends are
        reconciled by recovery before fresh account work, without a probe session.
        """
        selected = {str(item) for packet in packets for item in packet["observation_ids"]}
        if worker.recovery is not None:
            selected &= worker.recovery.selected
        rows = [row for row in plan["observation_plan"]["observations"]
                if row["observation_id"] in selected and row.get("platform") == "github_copilot"]
        if not rows:
            return
        with SendAttemptStore(self.repository.run_dir(run_id) / "fences", watch_changes=False) as store:
            records = [record for record in store.snapshot().records if record.action == "copilot_answer"]
        if any(record.state in {SendAttemptState.SEND_STARTED, SendAttemptState.RESPONSE_CAPTURED} for record in records):
            return
        saved = {record.observation_id for record in records if record.state is SendAttemptState.ACKNOWLEDGED}
        observation = next((row for row in rows if row["observation_id"] not in saved), None)
        if observation is None:
            return
        from .run_source_parser_observation import _config, _source
        config = _config(plan, observation)
        source = _source(worker, run_id, config)
        worker._copilot_admitted_source = source
        async def close_unclaimed_source():
            if getattr(worker, "_copilot_admitted_source", None) is source:
                worker._copilot_admitted_source = None
                await source.close()
        stack.push_async_callback(close_unclaimed_source)
        await worker._await_with_cancellation(run_id,
            asyncio.wait_for(source.initialize(), config["source_timeout_seconds"]),
            detail="github_copilot_preflight_cancelled")

    @asynccontextmanager
    async def _provider_scope(
        self,
        run_id: str,
        packets: Sequence[Mapping[str, Any]],
        *,
        _cancelled: Optional[Callable[[], bool]] = None,
    ):
        if self._providers is not None:
            yield self._providers
            return
        recovery=self.repository.recovery_execution
        if (recovery is not None and not recovery.requires_attachment_containment
                and all(selection.action in {"reconstruct","blocked","wait"} for selection in recovery.pass_record.selections)):
            yield ProviderRegistry()
            return

        from ..browser.runtime import BrowserRuntime
        from ..browser.desktop_actions import DesktopActionQueue
        from ..browser.copilot_verification import CopilotVerificationService, NativeVerificationSession

        factory = self.browser_runtime_factory or BrowserRuntime
        browser_runtime = factory(self.runtime)
        cancellation = self._cancel_events.setdefault(
            run_id,
            threading.Event(),
        )

        def durable_cancelled() -> bool:
            if _cancelled is not None:
                return _cancelled()
            if cancellation.is_set():
                return True
            if self.repository.cancellation_requested(run_id):
                cancellation.set()
                return True
            return False

        with self._guard:
            self._active_browser_runtimes[run_id] = browser_runtime
        leases = []
        reserved_profile_websites: list[tuple[str, tuple[object, ...]]] = []
        attachment = None
        attachment_task = None
        attachment_resource = None
        attachment_reference = None
        desktop_actions = DesktopActionQueue(session_factory=NativeVerificationSession,
            close_session=lambda session: session.close())
        verification_holds = None

        async def close_desktop_actions():
            settled = await desktop_actions.aclose(timeout_s=self.runtime.cleanup_timeout_seconds)
            if settled and attachment is not None and durable_cancelled():
                attachment.session.registry.close_retained = True
            return settled

        bind_desktop_cleanup = getattr(browser_runtime, "set_desktop_cleanup", None)
        if callable(bind_desktop_cleanup):
            bind_desktop_cleanup(close_desktop_actions)
        if recovery is not None:
            recovery.resource_tracking=True
            attachment_resource='attachment:'+recovery.owner.execution_id
            attachment_reference={'resource_kind':'local_attachment',
                'execution_id':recovery.owner.execution_id,
                'process_identity':recovery.owner.identity.to_dict(),
                'resource_scopes':tuple(sorted({scope for operation in recovery.snapshot.manifest.operations
                    for scope in operation.resource_scopes}))}
            recovery.record_resource(attachment_resource,'owned',recovery.owner,reference=attachment_reference)
        try:
            await desktop_actions.__aenter__()
            attach = getattr(browser_runtime, "attach", None)
            if not callable(attach):
                raise TypeError("BrowserRuntime must expose async attach()")
            attachment_task = asyncio.create_task(attach())
            self._pending_attachment_tasks.add(attachment_task)
            attachment_task.add_done_callback(self._pending_attachment_tasks.discard)
            deadline = asyncio.get_running_loop().time() + (
                self.runtime.browser.launch_timeout_seconds
                + self.runtime.browser.connect_timeout_seconds * 3
                + self.runtime.browser.health_timeout_seconds * 2
            )
            while not attachment_task.done():
                if durable_cancelled() or asyncio.get_running_loop().time() >= deadline:
                    attachment_task.cancel()
                    done, _ = await asyncio.wait((attachment_task,), timeout=self.runtime.cleanup_timeout_seconds)
                    if not done:
                        raise IntegrityError("browser_attachment_cleanup_unconfirmed")
                    if durable_cancelled():
                        raise _CancellationRequested("attachment cancelled")
                    raise TimeoutError("browser_attachment_deadline_exceeded")
                await asyncio.wait((attachment_task,), timeout=0.05)
            attachment = await attachment_task
            recovery = self.repository.recovery_execution
            plan = self.repository.read_plan(run_id)
            from .verification_policy import effective_policy
            verification_policy = effective_policy(self, run_id)
            if verification_policy in {"manual_hold_v1", "automatic_hold_v1"}:
                from ..browser.verification_holds import VerificationHoldStore, CopilotVerificationHolds
                if recovery is None:
                    raise IntegrityError("verification_hold_requires_durable_execution")
                hold_store = VerificationHoldStore(attachment.lease_manager,
                    attachment.manifest.selected_profiles, run_id=run_id,
                    plan_sha256=plan["plan_sha256"], capacity=self.runtime.tabs_per_provider)
                verification_holds = CopilotVerificationHolds(hold_store, attachment, recovery,
                    desktop_actions, cancelled=durable_cancelled)
            if recovery is not None:
                recovery.resource_tracking=True
                if not recovery.initial:
                    await recovery.contain_resources(attachment, cancelled=durable_cancelled,
                        timeout_seconds=self.runtime.profiles.lease_timeout_seconds)
                elif recovery.requires_attachment_containment:
                    recovery.contain_local_attachments(cancelled=durable_cancelled)
            plan = self.repository.read_plan(run_id)
            website_profiles: dict[str, set[str]] = {}
            for packet in packets:
                platform = "gemini" if packet.get("kind") == "source_parser" else str(packet.get("platform") or "")
                profile_id = str(packet.get("profile_id") or "")
                if recovery is not None and f"{profile_id}:{platform}" in recovery.blocked_scopes:
                    continue
                website_profiles.setdefault(platform, set()).add(profile_id)
            if (plan.get("scope_manifest") or {}).get("scope_kind") == "client_research_identification":
                rows = (plan.get("observation_plan") or {}).get("observations") or ()
                for row in rows:
                    website_profiles.setdefault(row["judge_provider"], set()).add(row["judge_profile_id"])
            for platform in sorted(website_profiles):
                selected = tuple(
                    attachment.manifest.profile(profile_id)
                    for profile_id in self.runtime.selected_profile_ids
                    if profile_id in website_profiles[platform]
                )
                lease = await asyncio.to_thread(
                    attachment.lease_manager.acquire,
                    selected,
                    owner=f"visibility-runner:{run_id}:{platform}",
                    provider=platform,
                    timeout_seconds=(
                        self.runtime.profiles.lease_timeout_seconds
                    ),
                    cancelled=durable_cancelled,
                )
                leases.append(lease)
                reserved_profile_websites.append((platform, selected))

            # One owned tab per (platform, profile, slot).  The slot comes from
            # the frozen measurement group layout: every measurement group of a (profile, platform)
            # pair owns its own tab, so its prompt measurements map to that slot.
            _, slot_by_page = _packet_tab_slots(plan.get("packets") or ())
            page_cache: dict[tuple[str, str, int], object] = {}
            page_open_locks: dict[tuple[str, str, int], asyncio.Lock] = {}

            async def page_factory(
                platform: str,
                profile_id: str,
                observation_id: str,
                fresh: bool,
                identity,
            ):
                if identity is not None:
                    # Route restoration through the attachment so a new worker
                    # can safely re-adopt the one uniquely tagged owned tab
                    # left behind by an interrupted turn-1 process.  Opening a
                    # replacement name listing chat is never permitted.
                    return await attachment.provider_page_factory(
                        platform,
                        profile_id,
                        observation_id,
                        fresh,
                        identity,
                    )
                slot = slot_by_page.get(
                    (str(platform), str(observation_id)),
                    0,
                )
                key = (str(platform), str(profile_id), slot)
                open_lock = page_open_locks.setdefault(key, asyncio.Lock())
                async with open_lock:
                    page = page_cache.get(key)
                    if page is not None:
                        closed=getattr(page,"is_closed",None)
                        if callable(closed) and await _await_call(closed,_deadline=_BrowserOperationDeadline.after(5,timeout_message="owned slot state unavailable")):
                            page_cache.pop(key,None)
                            page=None
                    if page is None:
                        page = await attachment.open_page(profile_id, platform)
                        if recovery is not None:
                            owned=next((tab for tab in attachment.session.registry.tabs if tab.page is page),None)
                            if owned is None:
                                raise IntegrityError("opened_tab_ownership_not_recorded")
                            fields=("profile_id","chrome_directory","browser_context_reference","tab_reference","browser_target_reference","conversation_reference")
                            binding={field:str(getattr(owned.identity,field,"") or "") for field in fields}
                            recovery.record_resource("tab:"+binding["tab_reference"],"owned",recovery.owner,
                                reference={"identity":binding,"item_id":observation_id,"platform":platform,
                                           "resource_scopes":(f"{profile_id}:{platform}",)})
                        page_cache[key] = page
                return page

            page_factory._restart_profiles = getattr(self, "_browser_restart_profiles", frozenset())
            self._browser_restart_profiles = frozenset()
            registry = ProviderRegistry()
            def desktop_progress(observation_id, detail):
                try:
                    self.event_sink(RunnerEvent(kind=EventKind.DESKTOP_ACTION,
                        run_id=run_id, observation_id=observation_id, detail=detail))
                except Exception:
                    pass
            desktop_verification = CopilotVerificationService(desktop_actions,
                getattr(attachment, "browser", None), notify=desktop_progress,
                background_only=self.runtime.browser.background_only, holds=verification_holds,
                automatic_verification=verification_policy == "automatic_hold_v1")
            from ..browser.copilot_blockers import CopilotBlockerService
            from .verification_policy import effective_accounts
            copilot_blockers = CopilotBlockerService(desktop_verification,
                holds=verification_holds, accounts=effective_accounts(self, run_id))
            frozen_retry_policy = (
                RetryPolicy.from_mapping(plan["retry_policy"])
                if isinstance(plan.get("retry_policy"), Mapping)
                else RetryPolicy.disabled()
            )
            response_timeouts = frozen_retry_policy.response_timeouts
            pacing = {
                "interaction_min_delay_seconds": (
                    self.runtime.browser.interaction_min_delay_seconds
                ),
                "interaction_max_delay_seconds": (
                    self.runtime.browser.interaction_max_delay_seconds
                ),
                "cancelled": durable_cancelled,
            }
            registry.register(
                ChatGPTProvider(
                    page_factory,
                    response_timeout_seconds=(
                        response_timeouts.chatgpt_turn_1_seconds
                    ),
                    extraction_timeout_seconds=(
                        response_timeouts.chatgpt_turn_2_seconds
                    ),
                    **pacing,
                )
            )
            registry.register(
                GeminiProvider(
                    page_factory,
                    response_timeout_seconds=(
                        response_timeouts.gemini_turn_1_seconds
                    ),
                    extraction_timeout_seconds=(
                        response_timeouts.gemini_turn_2_seconds
                    ),
                    **pacing,
                )
            )
            registry.register(
                CopilotProvider(
                    page_factory,
                    response_timeout_seconds=COPILOT_BROWSER_TURN_TIMEOUT_SECONDS,
                    extraction_timeout_seconds=COPILOT_BROWSER_TURN_TIMEOUT_SECONDS,
                    human_verification_handler=copilot_blockers if verification_holds is not None else desktop_verification,
                    **pacing,
                )
            )
            yield registry
        finally:
            close = getattr(browser_runtime, "close", None)
            close_error = None
            try:
                if not await close_desktop_actions():
                    close_error = IntegrityError("desktop_action_cleanup_unconfirmed")
            except BaseException as exc:
                close_error = exc
            if callable(close) and close_error is None:
                try:
                    cleanup_report = await _await_call(
                        close,
                        _deadline=_BrowserOperationDeadline.after(
                            self.runtime.cleanup_timeout_seconds,
                            timeout_message=(
                                "module-owned browser cleanup did not settle"
                            ),
                        ),
                    )
                    if verification_holds is not None:
                        verification_holds.confirmed_cleanup(cleanup_report)
                except BaseException as exc:
                    close_error = exc
            if close_error is not None and attachment is not None:
                for platform, selected in reserved_profile_websites:
                    for profile in selected:
                        try:
                            quarantine_unconfirmed_cleanup(
                                attachment.lease_manager,
                                profile,
                                platform=platform,
                                owner=f"visibility-runner:{run_id}:cleanup",
                                recovery=recovery,
                            )
                        except BaseException:
                            # Durable unresolved resource history remains authoritative
                            # even if the temporary lane marker could not be written.
                            pass
            if attachment_task is not None and not attachment_task.done():
                close_error = close_error or IntegrityError('browser_attachment_cleanup_unconfirmed')
            if recovery is not None and attachment_resource is not None:
                recovery.record_resource(attachment_resource,'released' if close_error is None else 'blocked',
                    recovery.owner,reference=attachment_reference)
            if recovery is not None and recovery.resource_tracking:
                recovery.record_cleanup(close_error is None)
            for lease in reversed(leases):
                await asyncio.to_thread(lease.release)
            with self._guard:
                self._active_browser_runtimes.pop(run_id, None)
            if close_error is not None:
                raise IntegrityError(
                    "module-owned tab cleanup could not be confirmed; affected "
                    "profile/provider lanes were quarantined"
                ) from close_error

    def _validate_plan(self, plan: RunPlan) -> None:
        if not isinstance(plan, RunPlan):
            raise TypeError("plan must be a RunPlan returned by plan()")
        source_config = plan.measurement_config.get("github_copilot") if isinstance(plan.measurement_config, Mapping) else getattr(plan.measurement_config, "github_copilot", None)
        if source_config is not None:
            source_config = json_value(source_config)
            runtime_source = getattr(self.runtime, "github_copilot", None)
            if runtime_source is None or any(source_config.get(key) != getattr(runtime_source, key) for key in ("account_ref", "github_account_id")):
                raise IntegrityError("github_copilot_runtime_account_mismatch")
        if plan.selected_profile_ids != self.runtime.selected_profile_ids:
            raise IntegrityError(
                "plan selected profile IDs differ from RuntimeConfig"
            )
        if (
            plan.profile_manifest_sha256
            != self.runtime.profile_manifest_sha256
        ):
            raise IntegrityError(
                "plan profile manifest hash differs from RuntimeConfig"
            )
        if plan.selected_profiles != self.runtime.selected_profiles:
            raise IntegrityError(
                "plan raw profile identities differ from RuntimeConfig"
            )
        validate_run_plan(plan)
        retry_policy_mismatch = (
            plan.retry_policy != self.runtime.retry_policy
        )
        plan_has_retry_authorization = (
            plan.retry_policy.authorized_max_attempts > 1
            or any(
                packet.submission_budget.retry_authorized_ceiling
                != packet.submission_budget.initial_topology
                for packet in plan.packets
            )
            or (
                plan.submission_budget.retry_authorized_ceiling
                != plan.submission_budget.initial_topology
            )
        )
        if retry_policy_mismatch and plan_has_retry_authorization:
            raise IntegrityError(
                "plan retry policy differs from RuntimeConfig"
            )

    def _verify_profile_manifest(self) -> None:
        path = self.runtime.profile_manifest_path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IntegrityError(f"cannot read pinned profile manifest {path}") from exc
        if canonical_sha256(payload) != self.runtime.profile_manifest_sha256:
            raise IntegrityError("pinned profile manifest hash changed")
        if not isinstance(payload, list):
            raise IntegrityError("profile manifest must be a JSON array")
        by_id = {
            str(row.get("id") or ""): row
            for row in payload
            if isinstance(row, Mapping)
        }
        selected: list[ProfileIdentity] = []
        for profile_id in self.runtime.selected_profile_ids:
            row = by_id.get(profile_id)
            if row is None:
                raise IntegrityError(
                    f"selected profile {profile_id!r} is absent from manifest"
                )
            selected.append(ProfileIdentity.from_mapping(row))
        if tuple(selected) != self.runtime.selected_profiles:
            raise IntegrityError(
                "profile manifest identities/directories differ from the "
                "explicit RuntimeConfig"
            )

    def _mark_attention(self, run_id: str, detail: str) -> None:
        def update(state: dict[str, Any]) -> None:
            if bool(state.get("cancel_requested")) or str(
                state.get("status") or ""
            ).casefold() == "cancelled":
                return
            state["status"] = "needs_attention"
            state.setdefault("issues", []).append(
                {
                    "code": "integrity_or_ambiguous_send",
                    "message": detail,
                    "recoverable": False,
                }
            )

        self.repository.update_state(run_id, update)
        self._emit(EventKind.NEEDS_ATTENTION, run_id)

    def _emit(
        self,
        kind: EventKind,
        run_id: str,
        packet_id: Optional[str] = None,
    ) -> None:
        owner = self.repository.owner_for(run_id)
        if owner is not None:
            owner.heartbeat(progress=True,operation_id=packet_id)
        try:
            self.event_sink(
                RunnerEvent(
                    kind=kind,
                    run_id=run_id,
                    packet_id=packet_id,
                )
            )
        except Exception:
            # Event sinks are observational and cannot authorize, cancel, or
            # alter durable measurement state.
            return


async def _settle_loop_tasks(
    tasks: set[asyncio.Future[Any]],
    timeout_seconds: float,
) -> tuple[
    set[asyncio.Future[Any]],
    set[asyncio.Future[Any]],
]:
    done, pending = await asyncio.wait(
        tasks,
        timeout=timeout_seconds,
        return_when=asyncio.ALL_COMPLETED,
    )
    return set(done), set(pending)


def _consume_loop_task(task: asyncio.Future[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        pass


def _quietly_abandon_loop_task(task: asyncio.Future[Any]) -> None:
    task.add_done_callback(_consume_loop_task)
    task.cancel()
    if task.done():
        _consume_loop_task(task)
    if hasattr(task, "_log_destroy_pending"):
        task._log_destroy_pending = False  # type: ignore[attr-defined]


def _close_private_loop(
    loop: asyncio.AbstractEventLoop,
    *,
    timeout_seconds: float,
) -> None:
    """Close a runner-owned loop without waiting forever on hostile cleanup."""

    already_abandoned = (
        _abandon_cancelled_provider_tasks_for_loop(loop)
        | _abandon_background_browser_operations_for_loop(loop)
    )
    tracked_abandoned: set[asyncio.Future[Any]] = set(
        already_abandoned
    )
    pending = {
        task
        for task in asyncio.all_tasks(loop)
        if not task.done() and task not in already_abandoned
    }
    for task in pending:
        task.cancel()
    if pending:
        try:
            done, still_pending = loop.run_until_complete(
                _settle_loop_tasks(pending, timeout_seconds)
            )
        except BaseException:
            done, still_pending = set(), pending
        for task in done:
            _consume_loop_task(task)
        tracked_abandoned.update(still_pending)
        for task in still_pending:
            _quietly_abandon_loop_task(task)

    # Website scopes are closed by execute_all().  This bounded final pass is
    # only for any asynchronous generators they registered with the loop.
    shutdown_task = loop.create_task(loop.shutdown_asyncgens())
    try:
        done, still_pending = loop.run_until_complete(
            _settle_loop_tasks({shutdown_task}, timeout_seconds)
        )
    except BaseException:
        done, still_pending = set(), {shutdown_task}
    for task in done:
        _consume_loop_task(task)
    tracked_abandoned.update(still_pending)
    for task in still_pending:
        _quietly_abandon_loop_task(task)

    # Async-generator finalizers may schedule one last task.  Nothing is
    # allowed to outlive this runner-owned loop, so consume completed outcomes
    # and silence only tasks that remain cancellation-hostile.
    final_pending = set(asyncio.all_tasks(loop))
    tracked_abandoned.update(final_pending)
    for task in final_pending:
        _quietly_abandon_loop_task(task)
    if tracked_abandoned:
        try:
            loop.run_until_complete(asyncio.sleep(0))
        except BaseException:
            pass
    for task in tracked_abandoned:
        if task.done():
            _consume_loop_task(task)
        else:
            _quietly_abandon_loop_task(task)

    loop.close()


def _packet_browser_provider(packet: Mapping[str, object]) -> str:
    """Resolve the browser used by one frozen packet for scheduling and tabs."""
    kind = str(packet.get("kind") or PacketKind.MEASUREMENT.value)
    if kind in {PacketKind.GAIO_EXTRACTION.value, "source_parser"}:
        return Platform.GEMINI.value
    return str(packet.get("platform") or "")


def _packet_tab_slots(
    plan_packets: Sequence[object],
) -> tuple[dict[str, int], dict[tuple[str, str], int]]:
    """Assign each frozen measurement group its owned-tab slot within its pair.

    Measurement groups are grouped by ``(profile_id, tab platform, kind)`` in plan order
    and numbered from 0.  A Google AI Overview name listing measurement group counts on
    the Gemini tab.  Returns ``slot by packet_id`` and ``slot by (tab
    platform, observation_id)``; the second lets the page factory pick the
    tab for a page request without changing the five-argument protocol.
    Nothing here is written to the plan, so historical plans keep their hash.
    """

    next_slot: dict[tuple[str, str, str], int] = {}
    slot_by_packet: dict[str, int] = {}
    slot_by_page: dict[tuple[str, str], int] = {}
    for candidate in plan_packets:
        if not isinstance(candidate, Mapping):
            continue
        packet_id = str(candidate.get("packet_id") or "")
        kind = str(candidate.get("kind") or PacketKind.MEASUREMENT.value)
        profile_id = str(candidate.get("profile_id") or "")
        tab_platform = _packet_browser_provider(candidate)
        # Parser groups reuse Gemini slots under identical scheduler keys,
        # while each kind numbers its own frozen group order.
        group = (profile_id, tab_platform, kind)
        slot = next_slot.get(group, 0)
        next_slot[group] = slot + 1
        slot_by_packet[packet_id] = slot
        for observation_id in candidate.get("observation_ids") or ():
            slot_by_page[(tab_platform, str(observation_id))] = slot
    return slot_by_packet, slot_by_page


class _SynchronousContexts:
    """Enter/exit host contexts on the owning thread between private-loop turns.

    Source admission and provider resources remain on one asyncio loop. Host
    startup can use synchronous restart/inspection APIs without nesting that
    loop or handing execution ownership to another thread.
    """

    def __init__(self):
        self.pending = None

    async def _call(self, callback, *, required=False):
        if self.pending is not None:
            raise RuntimeError("synchronous host context is already pending")
        loop = asyncio.get_running_loop()
        result = loop.create_future()
        self.pending = (callback, result, required)
        loop.stop()
        return await result

    async def enter_context(self, stack, factory):
        def enter():
            manager = factory()
            result = manager.__enter__()
            async def exit_context(*exception):
                return await self._call(lambda: manager.__exit__(*exception), required=True)
            stack.push_async_exit(exit_context)
            return result
        return await self._call(enter)

    def complete_pending(self):
        callback, result, required = self.pending
        self.pending = None
        if result.cancelled() and not required:
            return
        try:
            value = callback()
        except BaseException as error:
            if result.done():
                raise
            result.set_exception(error)
        else:
            if not result.done():
                result.set_result(value)


def _run_sync(
    awaitable,
    *,
    shutdown_timeout_seconds: float = 0.25,
    synchronous_contexts=None,
):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            if synchronous_contexts is None:
                return loop.run_until_complete(awaitable)
            task = loop.create_task(awaitable)
            task.add_done_callback(lambda _: loop.stop())
            while not task.done():
                loop.run_forever()
                if synchronous_contexts.pending is not None:
                    synchronous_contexts.complete_pending()
                    # A synchronous restart may have used its own temporary
                    # loop. Restore this execution's default before resuming.
                    asyncio.set_event_loop(loop)
                elif not task.done():
                    raise RuntimeError("private runner loop stopped unexpectedly")
            return task.result()
        finally:
            try:
                _close_private_loop(
                    loop,
                    timeout_seconds=shutdown_timeout_seconds,
                )
            finally:
                asyncio.set_event_loop(None)
    raise RuntimeError(
        "Runner is synchronous; call it from a host worker outside an active "
        "asyncio event loop"
    )


def _require_sync_caller() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        "Runner is synchronous; call it from a host worker outside an active "
        "asyncio event loop"
    )


def _packet_worker_timeout_seconds(
    *,
    configured_base_seconds: float,
    plan: Mapping[str, Any],
    packet: Mapping[str, Any],
) -> float:
    """Return one finite measurement group deadline that cannot pre-empt authorized waits.

    ``worker_timeout_seconds`` remains the bounded allowance for browser setup,
    exact-input verification, evidence persistence, and other measurement execution
    work.  On top of that base, reserve every response timeout, reconciliation
    grace period, and retry backoff authorized by the hash-bound plan.  Measurement group
    prompt measurements execute serially, so their maximum wait envelopes add.

    The resulting value is deliberately conservative.  A successful response
    normally avoids reconciliation grace, and native retries may avoid fresh
    chat setup, but neither optimization is assumed when setting the safety
    deadline.
    """

    frozen = plan.get("retry_policy")
    policy = (
        RetryPolicy.from_mapping(frozen)
        if isinstance(frozen, Mapping)
        else RetryPolicy.disabled()
    )
    attempts = policy.authorized_max_attempts
    grace = policy.late_response_grace_seconds
    backoff = policy.backoff_seconds
    timeouts = policy.response_timeouts

    def logical_turn(timeout_seconds: float) -> float:
        return (
            attempts * (timeout_seconds + grace)
            + max(0, attempts - 1) * backoff
        )

    observations = {
        str(value.get("observation_id") or ""): str(
            value.get("platform") or ""
        )
        for value in (
            (plan.get("observation_plan") or {}).get("observations") or ()
        )
        if isinstance(value, Mapping)
    }
    split_gaio_ids = {
        str(observation_id)
        for candidate in plan.get("packets") or ()
        if isinstance(candidate, Mapping)
        and str(candidate.get("kind") or "measurement")
        == PacketKind.GAIO_EXTRACTION.value
        for observation_id in candidate.get("observation_ids") or ()
    }
    packet_kind = str(packet.get("kind") or "measurement")
    if packet_kind == "source_parser":
        config = (plan.get("measurement_config") or {}).get("github_copilot") or (plan.get("scope_manifest") or {}).get("github_copilot") or {}
        per_item = sum(float(config.get(stage + "_timeout_seconds", 240)) * int(config.get("max_" + stage + "_attempts", 2)) for stage in ("source", "parser"))
        return float(configured_base_seconds) + len(packet.get("observation_ids") or ()) * (per_item + 120)
    packet_platform = str(packet.get("platform") or "")
    authorized_wait_seconds = 0.0
    for value in packet.get("observation_ids") or ():
        observation_id = str(value)
        if packet_kind == PacketKind.GAIO_EXTRACTION.value:
            authorized_wait_seconds += logical_turn(
                timeouts.gaio_gemini_parser_seconds
            )
            continue
        platform = observations.get(observation_id) or packet_platform
        if platform == Platform.CHATGPT.value:
            authorized_wait_seconds += logical_turn(
                timeouts.chatgpt_turn_1_seconds
            )
            authorized_wait_seconds += logical_turn(
                timeouts.chatgpt_turn_2_seconds
            )
        elif platform == Platform.GEMINI.value:
            authorized_wait_seconds += logical_turn(
                timeouts.gemini_turn_1_seconds
            )
            authorized_wait_seconds += logical_turn(
                timeouts.gemini_turn_2_seconds
            )
        elif platform == Platform.COPILOT.value:
            authorized_wait_seconds += logical_turn(COPILOT_BROWSER_TURN_TIMEOUT_SECONDS) * 2
        elif platform == Platform.GOOGLE_AI_OVERVIEW.value:
            authorized_wait_seconds += logical_turn(
                timeouts.google_ai_overview_seconds
            )
            if observation_id not in split_gaio_ids:
                authorized_wait_seconds += logical_turn(
                    timeouts.gaio_gemini_parser_seconds
                )
    return float(configured_base_seconds) + authorized_wait_seconds


def _packet_has_useful_durable_evidence(
    *,
    packet: PacketPlan,
    plan: RunPlan,
    observation_results: Sequence[Mapping[str, Any]],
    fence_records: Sequence[SendAttempt],
) -> bool:
    """Whether a failed worker already made useful measurement group-owned progress."""

    for result in observation_results:
        if (
            str(result.get("observation_id") or "")
            not in packet.observation_ids
        ):
            continue
        if not _packet_owns_observation_result(packet, result, plan=plan):
            continue
        if (
            bool(result.get("artifacts"))
            or str(result.get("status") or "").casefold()
            in {"succeeded", "partial"}
            or str(result.get("collection_status") or "").casefold()
            == "not_shown"
        ):
            return True
    return any(
        record.response_reference is not None
        for record in _measurement_group_attempts(packet, fence_records, plan=plan)
    )


__all__ = ["Runner"]
