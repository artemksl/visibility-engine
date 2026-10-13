"""Execute the ordered prompt measurements assigned to one group."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, Callable, Collection, Mapping, Optional
from urllib.parse import urlsplit

from automation_core.diagnostics.failure_capture import HTML_SAMPLE

from ..browser.operations import _await_call, _BrowserOperationDeadline
from ..config import COPILOT_BROWSER_TURN_TIMEOUT_SECONDS, RetryPolicy
from ..errors import (
    AmbiguousSendError,
    BrowserInteractionError,
    HumanVerificationRequiredError,
    IdentityMismatchError,
    IntegrityError,
    ProviderAccessBlockedError,
    StateConflictError,
    StateTransitionError,
)
from ..platform_availability import (
    GOOGLE_AI_OVERVIEW_DEFERRED, require_active_execution, require_active_action,
)
from ..names.check_chat_names import (
    MentionExtractionError,
    find_name_positions,
    parse_closed_name_array,
)
from ..names.prompts import same_chat_extraction_prompt
from ..events import EventKind, EventSink, RunnerEvent, discard_event
from ..gaio_evidence import (
    GAIO_GENERATED_ANSWER_CAPTURE_VERSION,
    canonical_gaio_terminal_evidence,
)
from ..websites.chat_actions import (
    BrowserIdentity,
    ProviderAnswer,
    ProviderRegistry,
    ProviderStatus,
    ReconciliationDisposition,
    ReconciliationResult,
    ResponseBaseline,
    Submission,
    PROVIDER_PREPARATION_TIMEOUT_SECONDS,
    PROVIDER_PRE_SEND_IDENTITY_TIMEOUT_SECONDS,
    PROVIDER_SECOND_MESSAGE_PREPARATION_TIMEOUT_SECONDS,
)
from ..storage.send_attempts import (
    AttemptSnapshot, SendAttempt, SendAttemptState, SendAttemptStore,
    attempt_allows_retry, count_attempt_milestones,
)
from ..storage.measurement_files import MeasurementFiles
from ..websites.usage_limits import project_blocking_usage_limit


DEFAULT_CANCELLATION_SETTLEMENT_SECONDS = 0.25
_PENDING_CANCELLED_PROVIDER_TASKS: set[asyncio.Future[Any]] = set()

# Send-attempt actions whose message is prepared with
# ``PROVIDER_SECOND_MESSAGE_PREPARATION_TIMEOUT_SECONDS``: the name listing
# message sent into a chat that already holds the first answer, and the Google
# AI Overview parser message, whose prompt embeds the whole captured Overview.
_SECOND_MESSAGE_ACTIONS = frozenset({"same_chat_turn_2", "gaio_gemini_extractor"})


def _preparation_step_seconds(
    submission: object,
    timeout_seconds: Optional[float],
    *,
    prompt: str,
) -> float:
    """Seconds the whole ``prepare_exact`` step may take, for its watchdog.

    ``prepare_exact`` runs the pre-send pacing pause (at most the submission's
    ``interaction_max_delay_seconds``) before its budget starts, so the
    advertised deadline is the pause plus the budget; a submission without a
    pause contributes nothing for it. Typed-input providers compute their
    budget from this exact message, so the watchdog uses the same calculation
    as ``prepare_exact`` rather than stopping a long message mid-typing.
    """

    pause = getattr(submission, "interaction_max_delay_seconds", 0.0)
    try:
        pause = max(0.0, float(pause))
    except (TypeError, ValueError):
        pause = 0.0
    preparation_budget = getattr(submission, "preparation_timeout_seconds", None)
    if callable(preparation_budget):
        budget = float(preparation_budget(prompt, timeout_seconds=timeout_seconds))
    else:
        budget = (
            PROVIDER_PREPARATION_TIMEOUT_SECONDS
            if timeout_seconds is None
            else float(timeout_seconds)
        )
    return pause + budget


@dataclass
class _OperationActivity:
    run_id: str
    packet_id: str
    observation_id: str
    tasks: set[asyncio.Future[Any]] = field(default_factory=set)
    active: bool = False


# Context-local because one worker drives independent observations concurrently.
_OPERATION_ACTIVITY: ContextVar[Optional[_OperationActivity]] = ContextVar(
    "visibility_operation_activity", default=None,
)


def _consume_cancelled_provider_task(
    task: asyncio.Future[Any],
) -> None:
    """Retrieve a detached website task's eventual terminal outcome."""

    _PENDING_CANCELLED_PROVIDER_TASKS.discard(task)
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        pass


def _abandon_cancelled_provider_tasks_for_loop(
    loop: asyncio.AbstractEventLoop,
) -> set[asyncio.Future[Any]]:
    """Forget already-settled detached tasks before a private loop closes.

    A synchronous ``Runner`` owns and closes its event loop.  Once the worker
    has spent its configured cancellation-settlement budget, keeping that
    private loop alive would restore the very unbounded wait the budget is
    intended to prevent.  These tasks have already been cancelled twice and
    their outcomes are forbidden from entering durable state.
    """

    abandoned: set[asyncio.Future[Any]] = set()
    for task in tuple(_PENDING_CANCELLED_PROVIDER_TASKS):
        if task.get_loop() is not loop:
            continue
        _PENDING_CANCELLED_PROVIDER_TASKS.discard(task)
        if task.done():
            _consume_cancelled_provider_task(task)
            continue
        task.cancel()
        # Keep the done callback attached.  The final cancellation can make a
        # task raise while the loop is performing its last bounded cleanup
        # turn, and that late exception still has to be retrieved.
        # There is no public asyncio API for abandoning a coroutine that
        # deliberately suppresses cancellation.  The private loop is about to
        # close, so prevent a misleading "Task was destroyed" diagnostic; no
        # task code can run after the loop is closed.
        if hasattr(task, "_log_destroy_pending"):
            task._log_destroy_pending = False  # type: ignore[attr-defined]
        abandoned.add(task)
    return abandoned


@dataclass(frozen=True)
class MeasurementGroupOutcome:
    packet_id: str
    status: str
    completed_observation_ids: tuple[str, ...]
    issues: tuple[Mapping[str, object], ...]
    transport_counts: Mapping[str, Mapping[str, int]]


@dataclass(frozen=True)
class _ConfirmedNoResponse(Exception):
    """Internal signal for one durably reconciled sent attempt."""

    record: SendAttempt
    terminal_nonretryable: bool = False

    def __str__(self) -> str:
        classification = (
            self.record.failure_classification or "confirmed_no_response"
        )
        return f"{self.record.fence_id}: {classification}"


class _CancellationRequested(Exception):
    """Internal signal emitted by durable cancellation polling."""


class _PageOpenSlot:
    """One held place under the pass-wide page-opening limit.

    ``release`` gives the place back exactly once; with no limit configured
    there is nothing to hold and ``release`` does nothing.
    """

    __slots__ = ("_limit", "_held")

    def __init__(self, limit: Optional[asyncio.Semaphore], *, held: bool):
        self._limit = limit
        self._held = bool(held and limit is not None)

    @property
    def held(self) -> bool:
        return self._held

    def release(self) -> None:
        if self._held:
            self._held = False
            assert self._limit is not None
            self._limit.release()


class _CancellationProbeFailed(IntegrityError):
    """Cancellation state became unreadable; no further action is admissible."""


class _RunCancellationWatcher:
    """One durable probe for all asynchronous waits in one run.

    Scopes are reference counted so standalone worker/helper callers receive
    the same cleanup guarantees as Runner. The task itself is the shared
    completion signal; waiters create no additional polling tasks.
    """

    def __init__(self, probe: Callable[[], bool], event: Event):
        self._probe = probe
        self._event = event
        self._failure: Optional[Exception] = None
        self._users = 0
        self.task: Optional[asyncio.Task[None]] = None

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise _CancellationProbeFailed(
                "durable cancellation state could not be verified"
            ) from self._failure

    def is_cancelled(self) -> bool:
        self.raise_if_failed()
        return self._event.is_set()

    def _check_now(self) -> bool:
        self.raise_if_failed()
        try:
            cancelled = self._event.is_set() or self._probe()
        except Exception as exc:
            self._failure = exc
            self.raise_if_failed()
        if cancelled:
            self._event.set()
        return bool(cancelled)

    async def _monitor(self) -> None:
        while not self._event.is_set():
            await asyncio.sleep(0.05)
            try:
                if self._check_now():
                    return
            except _CancellationProbeFailed:
                # Store the cause once, and wake every wait without producing
                # an unobserved task exception or pretending the user cancelled.
                return

    @asynccontextmanager
    async def watching(self):
        if self._users == 0:
            self._check_now()
            self.task = asyncio.create_task(
                self._monitor(), name="visibility-runner-cancellation-watcher",
            )
        elif self.task is not None and self.task.get_loop() is not asyncio.get_running_loop():
            raise RuntimeError("a cancellation watcher cannot span event loops")
        self._users += 1
        try:
            self.raise_if_failed()
            yield self
        finally:
            self._users -= 1
            if self._users == 0:
                task, self.task = self.task, None
                if task is not None:
                    task.cancel()
                    interrupted = False
                    owner = asyncio.current_task()
                    cancellation_count = owner.cancelling() if owner is not None else 0
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            if owner is not None and owner.cancelling() > cancellation_count:
                                interrupted = True
                    if not task.cancelled():
                        task.result()
                    if interrupted:
                        raise asyncio.CancelledError


class _CancellableSendAttemptStore(SendAttemptStore):
    """Send attempt store whose new-action mutations share the run cancel lock."""

    def __init__(
        self,
        root: Path,
        *,
        admission: Callable[[], Any],
        cancelled: Callable[[], bool],
        recovery=None,
    ):
        super().__init__(root)
        self.recovery = recovery
        self.record_filter = recovery.includes if recovery is not None else None
        self._admission = admission
        self._cancelled = cancelled

    def _admit(self, method: Callable[..., SendAttempt], *args, **kwargs):
        with self._admission() as active:
            if not active or self._cancelled():
                raise _CancellationRequested(
                    "durable cancellation forbids a new provider action"
                )
            return method(self, *args, **kwargs)

    def prepare(self, *args, **kwargs) -> SendAttempt:
        if self.recovery is not None:
            self.recovery.before_prepare(kwargs)
        return self._admit(SendAttemptStore.prepare, *args, **kwargs)

    def prepare_retry(self, *args, **kwargs) -> SendAttempt:
        if self.recovery is not None:
            self.recovery.before_prepare(kwargs)
        return self._admit(SendAttemptStore.prepare_retry, *args, **kwargs)

    def retry_pre_send_failure(self, *args, **kwargs) -> SendAttempt:
        return self._admit(
            SendAttemptStore.retry_pre_send_failure,
            *args,
            **kwargs,
        )

    def admit_send(self, fence_id: str) -> SendAttempt:
        try:
            return self._admit(
                SendAttemptStore.admit_send, fence_id,
                record_admission=self.recovery._admit_send if self.recovery is not None else None,
            )
        except _CancellationRequested:
            self.fail_before_send(fence_id, {
                "kind": "cancelled_pre_send", "phase": "pre_send",
                "detail": "durable cancellation won admission immediately before send start",
            })
            raise


class MeasurementGroupWorker:
    """Execute one frozen measurement group's prompt measurements serially.

    Several measurement groups of the same ``(platform, profile)`` pair may run at the
    same time on this worker, each on its own owned tab.  Their in-flight
    sends are tracked in ``_active_observations`` so the unresolved-action
    check for one measurement group does not mistake a sibling's live send for an
    abandoned one; a send that ends ambiguous still blocks the whole pair.
    """

    def __init__(
        self,
        repository: MeasurementFiles,
        providers: ProviderRegistry,
        *,
        cancel_event: Optional[Event] = None,
        cancelled: Optional[Callable[[], bool]] = None,
        cancellation_watcher: Optional[_RunCancellationWatcher] = None,
        event_sink: EventSink = discard_event,
        cancellation_settlement_seconds: float = (
            DEFAULT_CANCELLATION_SETTLEMENT_SECONDS
        ),
        page_open_limit: Optional[asyncio.Semaphore] = None,
    ):
        self.repository = repository
        self._copilot_source_limit = asyncio.Semaphore(1)
        self._client_identification_lock = asyncio.Lock()
        self._copilot_account_failure = None
        self._copilot_account_failure_detail = None
        self.copilot_runtime = None
        self.copilot_source_factory = None
        self.recovery = getattr(repository, "recovery_execution", None)
        # Pass-wide limit on page openings in flight (``RuntimeConfig.
        # max_concurrent_page_opens``); ``None`` means no limit.  Runner
        # creates one per pass and gives the same object to the recovery
        # adapter, so ``open`` here and ``reopen`` there wait on one counter.
        self.page_open_limit = page_open_limit
        self.bind_providers(providers)
        self.cancel_event = cancel_event or Event()
        self.cancelled = cancelled
        self._cancellation_watcher = cancellation_watcher
        self._cancellation_watchers: dict[str, _RunCancellationWatcher] = {}
        self._run_scope_users: dict[str, int] = {}
        self.event_sink = event_sink
        self.cancellation_settlement_seconds = float(
            cancellation_settlement_seconds
        )
        if (
            not math.isfinite(self.cancellation_settlement_seconds)
            or self.cancellation_settlement_seconds <= 0
        ):
            raise ValueError(
                "cancellation_settlement_seconds must be finite and positive"
            )
        self._late_reconciliations: dict[str, ReconciliationResult] = {}
        # Diagnostics only: a phase never changes send attempt state or authorizes
        # another send. Keep website exception text out of durable issues.
        self._send_phases: dict[str, str] = {}
        self._send_failure_evidence: dict[str, list[dict[str, Any]]] = {}
        self._observation_providers: dict[str, str] = {}
        # Prompt measurements whose website actions this process is driving right
        # now (sibling measurement groups of one profile/website pair on other tabs).
        self._active_observations: set[str] = set()
        self._operation_activities: dict[str, _OperationActivity] = {}
        # Keep the checked metadata index across observations in this worker.
        # Each store lookup still refreshes file versions; send admission still
        # enters the durable cancellation lock and reloads the selected fence.
        self._attempt_stores: dict[str, _CancellableSendAttemptStore] = {}

    def bind_providers(self, providers: ProviderRegistry) -> None:
        """Attach drivers after admission, retaining recovery's guarded adapters."""
        if self.recovery is None:
            self.providers = providers
            return
        from ..recovery_adapters.visibility import VisibilityDriver
        wrapped = ProviderRegistry()
        for platform in ("chatgpt", "gemini", "copilot", "google_ai_overview"):
            try:
                driver = providers.require(platform)
            except Exception:
                continue
            wrapped.register(VisibilityDriver(driver, self.recovery, worker=self))
        self.providers = wrapped

    async def execute(
        self,
        *,
        run_id: str,
        plan: Mapping[str, Any],
        packet: Mapping[str, Any],
        resume: bool = False,
    ) -> MeasurementGroupOutcome:
        require_active_execution({**plan, "packets": (*plan.get("packets", ()), packet)})
        async with self.run_scope(run_id):
            return await self._execute(
                run_id=run_id, plan=plan, packet=packet, resume=resume,
            )

    async def _execute(
        self,
        *,
        run_id: str,
        plan: Mapping[str, Any],
        packet: Mapping[str, Any],
        resume: bool = False,
    ) -> MeasurementGroupOutcome:
        if packet.get("kind") == "source_parser":
            from .run_source_parser_observation import execute_packet
            return await execute_packet(self, run_id=run_id, plan=plan, packet=packet, resume=resume)
        packet_id = str(packet["packet_id"])
        packet_kind = str(packet.get("kind") or "measurement")
        if packet_kind not in {"measurement", "gaio_extraction"}:
            raise IntegrityError(
                f"packet {packet_id} has unsupported kind {packet_kind!r}"
            )
        observation_ids = tuple(str(value) for value in packet["observation_ids"])
        observations = {
            str(row["observation_id"]): row
            for row in (plan.get("observation_plan") or {}).get("observations") or ()
            if isinstance(row, Mapping)
        }
        if any(value not in observations for value in observation_ids):
            raise IntegrityError(
                f"packet {packet_id} contains an observation outside the frozen plan"
            )
        packet_platform = str(packet.get("platform") or "")
        if packet_kind == "measurement":
            mismatched = [
                observation_id
                for observation_id in observation_ids
                if str(
                    observations[observation_id].get("platform") or ""
                )
                != packet_platform
            ]
            if mismatched:
                raise IntegrityError(
                    f"measurement packet {packet_id} platform does not match "
                    "its frozen observation(s): "
                    + ", ".join(mismatched)
                )
        elif (
            packet_platform != "gemini"
            or any(
                str(observations[value].get("platform") or "")
                != "google_ai_overview"
                for value in observation_ids
            )
        ):
            raise IntegrityError(
                f"GAIO extraction packet {packet_id} must bind Gemini to "
                "Google AI Overview source observations"
            )
        packet_state = self.repository.read_group_progress(run_id, packet_id)
        completed_before = {
            str(value)
            for value in packet_state.get("completed_observation_ids") or ()
            if str(value) in observation_ids
        }
        if self.recovery is not None:
            completed_before.difference_update(self.recovery.selected)
            completed_before.update(value for value in observation_ids if value not in self.recovery.selected)
        stored_results = {
            str(row.get("observation_id") or "")
            : row
            for row in self.repository.iter_observation_results(
                run_id, observation_ids=observation_ids,
            )
        }
        issues: list[dict[str, object]] = []
        # Durable trace of every in-run refetch after a confirmed pre-send
        # failure (structural failure context only, never exception text).
        refetches: list[dict[str, object]] = []
        completed: list[str] = [
            value for value in observation_ids if value in completed_before
        ]
        evidence_exists = bool(
            any(
                bool(
                    (stored_results.get(value) or {}).get("artifacts")
                )
                for value in observation_ids
            )
            or any(
                record.observation_id in observation_ids
                and record.response_reference is not None
                for record in self._send_attempts(run_id).snapshot(
                    observation_ids=observation_ids,
                ).records
            )
        )
        if completed_before == set(observation_ids):
            if not resume:
                raise StateConflictError(
                    f"packet {packet_id} is already complete; use resume() "
                    "only for incomplete work"
                )
            existing_counts = self._transport_counts(
                run_id,
                observation_ids,
                packet_kind=packet_kind,
                conditional_split=any(
                    _has_conditional_gaio_packet(plan, value)
                    for value in observation_ids
                ),
            )
            return MeasurementGroupOutcome(
                packet_id=packet_id,
                status=str(packet_state.get("status") or "failed").casefold(),
                completed_observation_ids=tuple(
                    value
                    for value in observation_ids
                    if value in completed_before
                ),
                issues=tuple(
                    row
                    for row in packet_state.get("issues") or ()
                    if isinstance(row, Mapping)
                ),
                transport_counts=existing_counts,
            )

        website_send_attempts = self._send_attempts(run_id)
        stored_terminal_ids: tuple[str, ...] = ()
        if packet_kind == "gaio_extraction":
            stored_terminal_ids = tuple(
                observation_id
                for observation_id in observation_ids
                if (
                    observation_id not in completed_before
                    and stored_results.get(observation_id) is not None
                )
            )
            for observation_id in stored_terminal_ids:
                if observation_id not in completed:
                    completed.append(observation_id)
        unresolved_sources = (
            _unresolved_google_search_attempts(
                website_send_attempts,
                packet,
            )
            if packet_kind == "gaio_extraction"
            else ()
        )
        if unresolved_sources:
            issues.append(
                _ambiguous_gaio_source_issue(unresolved_sources)
            )
        else:
            for observation_id in stored_terminal_ids:
                stored_result = stored_results[observation_id]
                if (
                    str(stored_result.get("status") or "").casefold()
                    == "succeeded"
                ):
                    continue
                stored_issues = [
                    dict(row)
                    for row in stored_result.get("issues") or ()
                    if isinstance(row, Mapping)
                ]
                if stored_issues:
                    issues.extend(stored_issues)
                else:
                    issues.append(
                        _issue(
                            observation_id,
                            "conditional_source_failed",
                            (
                                "conditional GAIO extraction could not run "
                                "because source collection did not complete"
                            ),
                            recoverable=False,
                        )
                    )
        executable_observation_ids = (
            ()
            if unresolved_sources
            else tuple(
                observation_id
                for observation_id in observation_ids
                if observation_id not in set(stored_terminal_ids)
            )
        )
        blocked_website_issue = _recorded_website_access_issue(
            self.repository,
            run_id,
            plan,
            packet,
        )
        policy = _retry_policy(plan)
        for observation_id in executable_observation_ids:
            if self._durable_is_cancelled(run_id):
                issues.append(
                    _issue(
                        observation_id,
                        "cancelled",
                        "packet cancellation was requested",
                        recoverable=True,
                    )
                )
                break
            if self.recovery is not None and observation_id not in completed_before:
                token = self._begin_operation_scope(run_id, packet_id, observation_id)
                try:
                    prepared = await self.recovery.prepare_item(self, observation_id, observations[observation_id], packet)
                except Exception as exc:
                    # The coordinator refused this one item before any send.
                    # Record its fixed code for the item and keep the packet's
                    # other items; every other error propagates unchanged.
                    refusal = self.recovery.item_refusal(observation_id, exc)
                    if refusal is None:
                        raise
                    issues.append(_issue(observation_id, "recovery_refused", refusal, recoverable=True))
                    continue
                finally:
                    self._finish_operation_scope(run_id, packet_id, observation_id, token)
                if not prepared:
                    reason = self.recovery.item_failure_reason(observation_id) or "original_response_unresolved"
                    issues.append(_issue(observation_id, reason,
                        f"Recovery did not complete: {reason}", recoverable=False))
                    continue
            unresolved_website_attempts = _unresolved_website_attempts(
                website_send_attempts,
                plan,
                packet,
                observations,
                in_flight=self._active_observations,
            )
            if unresolved_website_attempts:
                issue = _uncertain_website_send_issue(unresolved_website_attempts[0])
                if blocked_website_issue is not None:
                    issue["detail"].update(blocked_website_issue["detail"])
                issues.append(issue)
                break
            if blocked_website_issue is not None:
                issues.append(blocked_website_issue)
                break
            if observation_id in completed_before:
                if not resume:
                    raise StateConflictError(
                        f"packet {packet_id} already has results; use resume() explicitly"
                    )
                continue
            stored_result = stored_results.get(observation_id)
            if stored_result is not None:
                # Conditional name listing measurement groups intentionally share the
                # source prompt measurement IDs. A terminal not-shown/failed source,
                # or a result durably written just before a crash, makes this
                # phase complete without another website action.
                if packet_kind == "gaio_extraction" or resume:
                    completed.append(observation_id)
                    artifacts = stored_result.get("artifacts") or {}
                    evidence_exists = evidence_exists or bool(artifacts)
                    if (
                        str(stored_result.get("status") or "").casefold()
                        != "succeeded"
                    ):
                        stored_issues = [
                            dict(row)
                            for row in stored_result.get("issues") or ()
                            if isinstance(row, Mapping)
                        ]
                        if stored_issues:
                            issues.extend(stored_issues)
                        else:
                            issues.append(
                                _issue(
                                    observation_id,
                                    "conditional_source_failed",
                                    (
                                        "conditional GAIO extraction could "
                                        "not run because source collection "
                                        "did not complete"
                                    ),
                                    recoverable=False,
                                )
                            )
                    continue
                raise StateConflictError(
                    f"packet {packet_id} already has a durable result; "
                    "use resume() explicitly"
                )
            observation = observations[observation_id]
            tries = 0
            resume_this = resume
            stop_packet = False
            while True:
                tries += 1
                token = self._begin_operation_scope(run_id, packet_id, observation_id)
                self._emit(
                    EventKind.OBSERVATION_STARTED,
                    run_id,
                    packet_id,
                    observation_id,
                )
                self._active_observations.add(observation_id)
                try:
                    if str(observation.get("platform") or "") == "google_ai_overview":
                        if packet_kind == "gaio_extraction":
                            google_mode = "extract_only"
                        elif _has_conditional_gaio_packet(plan, observation_id):
                            google_mode = "search_only"
                        else:
                            # Read compatibility for early package plans that
                            # authorized both actions in one measurement measurement group.
                            google_mode = "inline"
                        result = await self._google(
                            run_id,
                            observation,
                            plan=plan,
                            packet=packet,
                            resume=resume_this,
                            mode=google_mode,
                            allow_restart=_in_run_refetch_allowed(
                                policy,
                                tries,
                            ),
                        )
                    else:
                        if packet_kind != "measurement":
                            raise IntegrityError(
                                "a GAIO extraction packet may contain only "
                                "Google AI Overview source observations"
                            )
                        if observation.get("client_research_capability") == "client_research.identification.v1":
                            from .run_client_identification import execute_observation
                            async with self._client_identification_lock:
                                result = await execute_observation(self, run_id, observation, plan=plan, packet=packet, resume=resume_this)
                        else:
                            result = await self._conversational(
                                run_id,
                                observation,
                                plan=plan,
                                packet=packet,
                                resume=resume_this,
                                allow_restart=_in_run_refetch_allowed(
                                    policy,
                                    tries,
                                ),
                            )
                    if result is not None:
                        deferred = bool(result.pop("_deferred", False))
                        if not deferred:
                            self._assert_execution()
                            self.repository.write_observation_result(
                                run_id,
                                observation_id,
                                result,
                            )
                        completed.append(observation_id)
                        evidence_exists = evidence_exists or bool(result.get("artifacts"))
                        if (
                            not deferred
                            and str(result.get("status")).casefold() != "succeeded"
                        ):
                            issues.extend(result.get("issues") or ())
                        self._emit(
                            EventKind.OBSERVATION_COMPLETED,
                            run_id,
                            packet_id,
                            observation_id,
                        )
                    break
                except asyncio.CancelledError:
                    # Scheduler timeouts/cancellation are measurement group-scoped. They must
                    # not set the shared user-cancellation event or cancel peers.
                    raise
                except BaseException as exc:
                    # This prompt measurement's own actions are no longer in flight, so
                    # an ambiguous send attempt it left behind must be reported below.
                    self._active_observations.discard(observation_id)
                    open_failure_evidence = []
                    failed_open = getattr(exc, "_visibility_open_submission", None)
                    if getattr(failed_open, "observation_id", None) == observation_id:
                        references = await self._capture_send_failure(
                            run_id, packet_id, None, failed_open, exc,
                            phase="provider_open", observation_id=observation_id,
                        )
                        if references:
                            open_failure_evidence.append(references)
                    access_error = _provider_access_error(exc)
                    access_detail = None
                    if access_error is not None:
                        profile_id, _ = _profile_identity(
                            plan,
                            packet,
                            observation,
                        )
                        access_detail = _access_block_detail(
                            access_error,
                            profile_id=profile_id,
                            provider=self._observation_providers.get(
                                observation_id,
                                packet_platform,
                            ),
                        )
                    unresolved_website_attempts = _unresolved_website_attempts(
                        website_send_attempts,
                        plan,
                        packet,
                        observations,
                        in_flight=self._active_observations,
                    )
                    if unresolved_website_attempts:
                        unresolved = unresolved_website_attempts[0]
                        issue = _uncertain_website_send_issue(
                            unresolved,
                            failure_context=_safe_failure_context(
                                exc,
                                phase=self._send_phases.get(
                                    unresolved.fence_id,
                                    "observation_execution",
                                ),
                            ),
                            failure_evidence=[
                                *self._send_failure_evidence.get(unresolved.fence_id, []),
                                *open_failure_evidence,
                            ],
                        )
                        if access_detail is not None:
                            issue["detail"].update(access_detail)
                        issues.append(issue)
                        stop_packet = True
                        break
                    if access_detail is not None:
                        if open_failure_evidence:
                            access_detail["failure_evidence"] = open_failure_evidence
                        issues.append(
                            _website_access_issue(
                                observation_id,
                                access_detail,
                            )
                        )
                        stop_packet = True
                        break
                    if isinstance(
                        exc,
                        (
                            AmbiguousSendError,
                            IdentityMismatchError,
                            IntegrityError,
                        ),
                    ):
                        issues.append(
                            _issue(
                                observation_id,
                                "needs_attention",
                                str(exc),
                                recoverable=False,
                            )
                        )
                    else:
                        # A confirmed failure before any send: the website
                        # received nothing, so the prompt measurement is fetched
                        # again at once in a fresh chat (founder decision
                        # 2026-09-10), bounded by the frozen attempt limit.
                        # The failed provider action is over before retry backoff.
                        # Its old deadline must not supervise an idle retry pause.
                        self._complete_operation(run_id, packet_id, observation_id)
                        refetch = False
                        if _in_run_refetch_allowed(
                            policy,
                            tries,
                        ) and not self._is_cancelled(run_id):
                            try:
                                await _retry_backoff(
                                    policy,
                                    cancelled=lambda: self._is_cancelled(
                                        run_id
                                    ),
                                )
                                refetch = True
                            except BrowserInteractionError:
                                # Cancellation arrived during the pause; fall
                                # through and record the failure as it stands.
                                refetch = False
                        if refetch:
                            refetches.append(
                                {
                                    "observation_id": observation_id,
                                    "failed_entry": tries,
                                    "failure_context": _safe_failure_context(
                                        exc,
                                        phase="observation_execution",
                                    ),
                                }
                            )
                            if open_failure_evidence:
                                refetches[-1]["failure_evidence"] = open_failure_evidence
                            self._emit(
                                EventKind.OBSERVATION_REFETCHED,
                                run_id,
                                packet_id,
                                observation_id,
                            )
                            resume_this = True
                            continue
                        # Retry-safe failures remain prompt measurement-scoped.
                        issues.append(
                            _issue(
                                observation_id,
                                "observation_failed",
                                f"{type(exc).__name__}: {exc}",
                                recoverable=True,
                            )
                        )
                    if open_failure_evidence:
                        issues[-1].setdefault("detail", {})["failure_evidence"] = open_failure_evidence
                    break
                finally:
                    self._active_observations.discard(observation_id)
                    self._finish_operation_scope(run_id, packet_id, observation_id, token)
            if stop_packet:
                break

        counts = self._transport_counts(
            run_id,
            observation_ids,
            packet_kind=packet_kind,
            conditional_split=any(
                _has_conditional_gaio_packet(plan, value)
                for value in observation_ids
            ),
        )
        if self._is_cancelled(run_id):
            status = "cancelled"
        elif any(
            row["code"] in {"needs_attention", "ambiguous_send"}
            for row in issues
        ):
            status = "needs_attention"
        elif issues or len(completed) != len(observation_ids):
            status = "partial" if evidence_exists else "failed"
        else:
            status = "succeeded"

        def update_packet(state: dict[str, Any]) -> None:
            self._assert_execution()
            if (
                str(state.get("status") or "").casefold() == "cancelled"
                or self._is_cancelled(run_id)
            ):
                return
            state["status"] = status
            state["issues"] = issues
            state["transport_counts"] = counts
            state["completed_observation_ids"] = [
                value for value in observation_ids if value in set(completed)
            ]
            if refetches:
                state["in_run_refetches"] = [
                    *list(state.get("in_run_refetches") or ()),
                    *refetches,
                ]

        self._assert_execution()
        self.repository.update_packet_state(run_id, packet_id, update_packet)
        return MeasurementGroupOutcome(
            packet_id=packet_id,
            status=status,
            completed_observation_ids=tuple(completed),
            issues=tuple(issues),
            transport_counts=counts,
        )

    def _is_cancelled(self, run_id: str) -> bool:
        watcher = self._cancellation_watchers.get(run_id) or self._cancellation_watcher
        if watcher is not None and watcher._users:
            return watcher.is_cancelled()
        return self._durable_is_cancelled(run_id)

    def _durable_is_cancelled(self, run_id: str) -> bool:
        watcher = self._cancellation_watchers.get(run_id) or self._cancellation_watcher
        if watcher is not None:
            watcher.raise_if_failed()
        if self.cancel_event.is_set():
            return True
        # An injected callback is the authoritative durable probe. Runner's
        # callback already reads cancellation_requested; never read it twice.
        cancelled = (
            self.cancelled()
            if self.cancelled is not None
            else self.repository.cancellation_requested(run_id)
        )
        if cancelled:
            self.cancel_event.set()
            return True
        return False

    @asynccontextmanager
    async def run_scope(self, run_id: str):
        """Own run resources until every participating packet has settled."""

        self._run_scope_users[run_id] = self._run_scope_users.get(run_id, 0) + 1
        try:
            async with self._watch_cancellation(run_id):
                yield
        finally:
            users = self._run_scope_users[run_id] - 1
            if users:
                self._run_scope_users[run_id] = users
            else:
                del self._run_scope_users[run_id]
                store = self._attempt_stores.pop(run_id, None)
                if store is not None:
                    store.close()

    @asynccontextmanager
    async def _watch_cancellation(self, run_id: str):
        watcher = self._cancellation_watchers.get(run_id)
        if watcher is None:
            watcher = self._cancellation_watcher or _RunCancellationWatcher(
                lambda: self._durable_is_cancelled(run_id), self.cancel_event,
            )
            self._cancellation_watchers[run_id] = watcher
        try:
            async with watcher.watching():
                yield watcher
        finally:
            if watcher._users == 0 and watcher._failure is None:
                self._cancellation_watchers.pop(run_id, None)

    def _assert_execution(self):
        if self.recovery is not None:
            self.recovery.assert_owned()

    def _require_active_run(self, run_id: str) -> None:
        if self._durable_is_cancelled(run_id):
            raise _CancellationRequested(
                "durable cancellation forbids a new provider action"
            )

    def _send_attempts(self, run_id: str) -> _CancellableSendAttemptStore:
        store = self._attempt_stores.get(run_id)
        if store is None:
            store = _CancellableSendAttemptStore(
                self.repository.run_dir(run_id) / "fences",
                admission=lambda: self.repository.submission_admission(run_id),
                cancelled=lambda: self._durable_is_cancelled(run_id),
                recovery=self.recovery,
            )
            owner = self.repository.execution_owners.get(run_id)
            store.ownership_guard = self.recovery.assert_owned if self.recovery is not None else owner.assert_owned if owner is not None else None
            self._attempt_stores[run_id] = store
        return store

    async def _await_with_cancellation(
        self,
        run_id: str,
        awaitable,
        *,
        detail: str,
    ):
        """Race one website await against the shared run cancellation signal."""

        task = asyncio.ensure_future(awaitable)
        activity = _OPERATION_ACTIVITY.get()
        if activity is not None:
            activity.tasks.add(task)
        settled = False
        try:
            async with self._watch_cancellation(run_id) as watcher:
                try:
                    if watcher.is_cancelled():
                        raise _CancellationRequested(detail)
                    if not task.done():
                        assert watcher.task is not None
                        await asyncio.wait(
                            (task, watcher.task),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    if watcher.is_cancelled():
                        raise _CancellationRequested(detail)
                    return task.result()
                except BaseException:
                    settled = True
                    await self._settle_cancelled_task(task)
                    raise
        except BaseException:
            if not settled:
                await self._settle_cancelled_task(task)
            raise

    async def _acquire_page_open_slot(
        self,
        run_id: str,
        limit: Optional[asyncio.Semaphore],
    ) -> _PageOpenSlot:
        """Wait for a place under ``limit``; race run cancellation.

        The wait happens before the caller admits its navigation effect and
        before any opening deadline starts, so a packet whose turn has not
        come consumes none of its own budget.  Cancellation while waiting is
        raised exactly as for any other website await
        (``_await_with_cancellation``), and a place that was obtained at the
        same moment is given back.  With ``limit`` ``None`` the returned slot
        holds nothing.  The recovery adapter calls this for ``reopen`` with
        the limit it was given, which Runner makes the same object as
        ``self.page_open_limit``.
        """

        if limit is None:
            return _PageOpenSlot(None, held=False)
        acquired = False

        async def acquire() -> None:
            nonlocal acquired
            await limit.acquire()
            acquired = True

        try:
            await self._await_with_cancellation(
                run_id,
                acquire(),
                detail=(
                    "module cancellation interrupted the wait for a "
                    "page-opening slot"
                ),
            )
        except BaseException:
            if acquired:
                limit.release()
            raise
        return _PageOpenSlot(limit, held=True)

    async def _open_website_page(
        self,
        run_id: str,
        open_call: Callable[[], Any],
    ):
        """Run one ``open`` under the page-opening limit.

        With a recovery journal every provider here is wrapped by
        ``VisibilityDriver``, whose ``open`` waits for the place itself, after
        it has started the attempt and before it admits the ``navigate``
        effect, so the wait is visible in the journal.  Without a journal the
        wait happens here.  Either way the place is released as soon as
        ``open`` returns or raises; it is never held through preparation or a
        send.
        """

        if self.recovery is not None:
            return await open_call()
        slot = await self._acquire_page_open_slot(run_id, self.page_open_limit)
        try:
            return await open_call()
        finally:
            slot.release()

    async def _settle_cancelled_task(
        self,
        task: asyncio.Future[Any],
    ) -> None:
        """Bound cancellation settlement and discard every late outcome."""

        task.cancel()
        try:
            done, _ = await asyncio.wait(
                (task,),
                timeout=self.cancellation_settlement_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            self._detach_cancelled_task(task)
            raise
        if task in done:
            _consume_cancelled_provider_task(task)
            return
        # A second cancellation settles the common case where a coroutine
        # suppressed the first CancelledError while still ensuring the worker
        # never waits beyond its configured bound.
        task.cancel()
        self._detach_cancelled_task(task)

    @staticmethod
    def _detach_cancelled_task(task: asyncio.Future[Any]) -> None:
        if task.done():
            _consume_cancelled_provider_task(task)
            return
        if task not in _PENDING_CANCELLED_PROVIDER_TASKS:
            _PENDING_CANCELLED_PROVIDER_TASKS.add(task)
            task.add_done_callback(_consume_cancelled_provider_task)

    def _begin_operation_scope(self, run_id, packet_id, observation_id):
        activity = _OperationActivity(run_id, packet_id, observation_id)
        self._operation_activities[observation_id] = activity
        return _OPERATION_ACTIVITY.set(activity)

    def _complete_operation(self, run_id, packet_id, observation_id, *, activity=None):
        activity = activity or _OPERATION_ACTIVITY.get()
        if activity is None or self._operation_activities.get(observation_id) is not activity:
            return True
        if any(not task.done() for task in activity.tasks):
            # Bounded cancellation may detach a provider coroutine that ignores
            # cancellation. Keep its deadline available to external containment.
            return False
        if activity.active or (self.recovery is not None and observation_id in self.recovery.deadlines):
            self._emit(EventKind.OPERATION_COMPLETED, run_id, packet_id, observation_id)
            activity.active = False
        return True

    def _settle_detached_operation(self, activity):
        if self._operation_activities.get(activity.observation_id) is not activity:
            return  # A later operation segment owns this observation now.
        try:
            # The callback cannot mutate a later execution/pass or a released
            # owner. A failed ownership check retains fail-closed progress.
            self._assert_execution()
            if self._complete_operation(activity.run_id, activity.packet_id,
                                        activity.observation_id, activity=activity):
                self._operation_activities.pop(activity.observation_id, None)
        except Exception:
            return

    def _finish_operation_scope(self, run_id, packet_id, observation_id, token):
        activity = _OPERATION_ACTIVITY.get()
        try:
            if self._complete_operation(run_id, packet_id, observation_id):
                if self._operation_activities.get(observation_id) is activity:
                    self._operation_activities.pop(observation_id, None)
            elif activity is not None:
                for task in activity.tasks:
                    if not task.done():
                        task.add_done_callback(lambda _done, scope=activity: self._settle_detached_operation(scope))
        finally:
            _OPERATION_ACTIVITY.reset(token)

    def _emit(
        self,
        kind: EventKind,
        run_id: str,
        packet_id: str,
        observation_id: str,
    ) -> None:
        detail = {}
        activity = _OPERATION_ACTIVITY.get()
        if activity is not None and activity.observation_id == observation_id:
            activity.active = kind not in {EventKind.OBSERVATION_COMPLETED, EventKind.OPERATION_COMPLETED}
        if self.recovery is not None:
            self.recovery.progress(observation_id, complete=kind in {
                EventKind.OBSERVATION_COMPLETED, EventKind.OPERATION_COMPLETED,
            })
            deadline=self.recovery.deadlines.get(observation_id)
            if deadline is not None:
                detail["deadline_monotonic"]=deadline
            detail.update(execution_id=self.recovery.owner.execution_id,recovery_pass_id=self.recovery.pass_id)
        try:
            self.event_sink(
                RunnerEvent(
                    kind=kind,
                    run_id=run_id,
                    packet_id=packet_id,
                    observation_id=observation_id,
                    detail=detail,
                )
            )
        except Exception:
            # Progress reporting is observational and must never alter durable
            # send topology or cancel an otherwise independent prompt measurement.
            return

    def _logical_id(self, observation_id, suffix):
        if self.recovery is not None:
            return self.recovery.logical_id(observation_id, suffix)
        return f"{observation_id}:{suffix}"

    async def _await_send_admission(self, run_id, observation_id, platform, profile_id):
        if self.recovery is not None and platform == "copilot":
            reason = await self.recovery.wait_for_active_blocker(self, profile_id, observation_id)
            holds = self.recovery.verification_holds
            retained = next((row for row in holds.store.rows(own_run=False)
                if row['profile_id'] == profile_id
                and observation_id not in row.get('active_item_ids', ())), None) if holds is not None else None
            if retained is not None:
                error_type = (HumanVerificationRequiredError
                    if retained.get('blocker_kind', 'verification') == 'verification'
                    else ProviderAccessBlockedError)
                raise error_type("copilot_profile_blocker_retained",
                    reason=reason or retained.get('reason') or 'copilot_profile_blocker_retained')
        # No await follows this check before the fence's synchronous hold guard.
        self._require_active_run(run_id)

    async def _conversational(
        self,
        run_id: str,
        observation: Mapping[str, Any],
        *,
        plan: Mapping[str, Any],
        packet: Mapping[str, Any],
        resume: bool,
        allow_restart: bool = True,
    ) -> Optional[dict[str, Any]]:
        # ``allow_restart`` is False on the last in-run entry the frozen
        # limit permits: a confirmed rejection is then returned as it
        # stands instead of opening one more fresh chat whose failure would
        # lose the durable partial result.
        from ..client_research import RECOMMENDATION_EXTRACTION_PROMPT
        research = observation.get("client_research_capability") == "client_research.recommendations.v1"
        extraction_prompt = RECOMMENDATION_EXTRACTION_PROMPT if research else None
        if research:
            observation = {**observation, "client_research_context": (plan.get("scope_manifest") or {}).get("client_research") or {}}
        observation_id = str(observation["observation_id"])
        packet_id = str(packet["packet_id"])
        platform = str(observation["platform"])
        self._observation_providers[observation_id] = platform
        prompt = str(observation["prompt_text"])
        profile_id, chrome_directory = _profile_identity(plan, packet, observation)
        driver = None
        canonical_reference = None
        fences = self._send_attempts(run_id)
        policy = _retry_policy(plan)
        turn_1_logical_id = self._logical_id(observation_id, "turn-1")
        turn_2_logical_id = self._logical_id(observation_id, "turn-2")
        chat_ordinal = 1

        while True:
            # One fresh snapshot for this synchronous action-selection decision.
            # Never carry its admission authority across the browser awaits below.
            attempt_snapshot = fences.snapshot(observation_ids=(observation_id,))
            turn_1_attempts = _logical_attempts(attempt_snapshot, turn_1_logical_id)
            turn_2_attempts = _logical_attempts(attempt_snapshot, turn_2_logical_id)
            chat_ordinal = max(
                chat_ordinal,
                _latest_observation_attempt(attempt_snapshot, observation_id),
            )
            turn_1 = _latest_for_observation_attempt(
                turn_1_attempts,
                chat_ordinal,
            )
            turn_2 = _latest_for_observation_attempt(
                turn_2_attempts,
                chat_ordinal,
            )

            if turn_2 is not None and turn_2.state in {
                SendAttemptState.ACKNOWLEDGED,
                SendAttemptState.REJECTED,
            }:
                accepted_turn_1 = _accepted_for_observation_attempt(
                    turn_1_attempts,
                    chat_ordinal,
                )
                if accepted_turn_1 is None:
                    raise IntegrityError(
                        "accepted same-chat extraction lacks its measurement "
                        "attempt"
                    )
                if (
                    turn_2.state is SendAttemptState.REJECTED
                    and allow_restart
                    and _can_restart_conversation(
                        turn_1_attempts,
                        turn_2_attempts,
                        policy,
                    )
                ):
                    # A exact name matching rejection is a confirmed failure of this
                    # attempt: the chat is started again from scratch for
                    # the same prompt while the frozen limit allows it
                    # (founder decision 2026-09-10). The rejected attempt's
                    # send attempts and evidence stay on disk.
                    await _retry_backoff(
                        policy,
                        cancelled=lambda: self._is_cancelled(run_id),
                    )
                    chat_ordinal += 1
                    continue
                answer_text, answer_reference = _read_attempt_response(
                    self.repository,
                    run_id,
                    accepted_turn_1,
                )
                extraction_text, extraction_reference = _read_attempt_response(
                    self.repository,
                    run_id,
                    turn_2,
                )
                return _conversational_result(
                    observation_id,
                    platform,
                    answer_text,
                    answer_reference,
                    extraction_text,
                    extraction_reference,
                    turn_2,
                    fences=fences,
                    accepted_turn_1=accepted_turn_1,
                    observation=observation,
                )

            if driver is None:
                driver = self.providers.require(platform)
                canonical_reference = getattr(driver, "canonical_conversation_reference", None)
            if turn_1 is not None and turn_1.state in {
                SendAttemptState.SEND_STARTED,
                SendAttemptState.RESPONSE_CAPTURED,
            }:
                raise AmbiguousSendError(
                    f"{turn_1.fence_id} cannot be recovered automatically"
                )
            if turn_2 is not None and turn_2.state in {
                SendAttemptState.SEND_STARTED,
                SendAttemptState.RESPONSE_CAPTURED,
            }:
                raise AmbiguousSendError(
                    f"{turn_2.fence_id} cannot be recovered automatically"
                )

            # A durable confirmed decision may continue on resume. Native
            # retry is re-probed against the restored page; if that proof is
            # unavailable, a complete fresh-chat prompt measurement is the only
            # clean fallback.
            if (
                turn_2 is not None
                and turn_2.state is SendAttemptState.CONFIRMED_NO_RESPONSE
            ):
                accepted_turn_1 = _accepted_for_observation_attempt(
                    turn_1_attempts,
                    chat_ordinal,
                )
                if accepted_turn_1 is None:
                    raise IntegrityError(
                        "turn-2 failure lacks an accepted turn-1 attempt"
                    )
                if not _record_authorizes_retry(turn_2):
                    return _conversational_no_response_result(
                        observation_id,
                        platform,
                        self.repository,
                        run_id,
                        fences,
                        accepted_turn_1=accepted_turn_1,
                        failed_turn=turn_2,
                    )
                if not research:
                    source_text, _ = _read_attempt_response(self.repository, run_id, accepted_turn_1)
                    extraction_prompt = same_chat_extraction_prompt(
                        source_text, platform=platform, expected_prompt_sha256=turn_2.prompt_sha256,
                    )
                await _retry_backoff(
                    policy,
                    cancelled=lambda: self._is_cancelled(run_id),
                )
                failed_submission = (
                    await driver.restore(
                        BrowserIdentity.from_mapping(
                            _reconciled_identity(turn_2)
                        ),
                        observation_id=observation_id,
                    )
                    if turn_2.native_retry_available is True
                    else None
                )
                if failed_submission is not None:
                    _use_same_chat_extraction_timeout(
                        failed_submission,
                        policy,
                        platform,
                    )
                    native = await _restore_and_prepare_native_retry(
                        failed_submission,
                        turn_2,
                        extraction_prompt,
                        binding=self.recovery.correlation(turn_2.fence_id) if self.recovery is not None else None,
                    )
                    if native:
                        await self._await_send_admission(run_id, observation_id, platform, profile_id)
                        successor = fences.prepare_retry(
                            confirmed_fence_id=turn_2.fence_id,
                            successor_fence_id=_send_attempt_id(
                                turn_2_logical_id,
                                _attempt_ordinal(turn_2) + 1,
                            ),
                            identity=failed_submission.identity.to_mapping(),
                            action="same_chat_turn_2",
                            prompt_sha256=_text_sha(
                                extraction_prompt
                            ),
                            logical_turn_id=turn_2_logical_id,
                            attempt_ordinal=_attempt_ordinal(turn_2) + 1,
                            observation_attempt_ordinal=chat_ordinal,
                            native_retry=True,
                        )
                        turn_2 = successor
                        submission = failed_submission
                        prepared_native = True
                    else:
                        turn_2 = None
                        submission = None
                        prepared_native = False
                else:
                    turn_2 = None
                    submission = None
                    prepared_native = False
                if turn_2 is None:
                    if not _can_restart_conversation(
                        turn_1_attempts,
                        turn_2_attempts,
                        policy,
                    ):
                        failed_turn = _latest_for_observation_attempt(
                            turn_2_attempts,
                            chat_ordinal,
                        )
                        if (
                            failed_turn is not None
                            and _record_authorizes_retry(failed_turn)
                        ):
                            failed_turn = fences.exhaust_authorized_retry(
                                failed_turn.fence_id,
                                detail=(
                                    "native retry could not be re-proved and "
                                    "the full-observation retry ceiling was "
                                    "already exhausted"
                                ),
                            )
                        return _conversational_no_response_result(
                            observation_id,
                            platform,
                            self.repository,
                            run_id,
                            fences,
                            accepted_turn_1=accepted_turn_1,
                            failed_turn=failed_turn,
                        )
                    chat_ordinal += 1
                    continue
            else:
                submission = None
                prepared_native = False

            if (
                turn_1 is not None
                and turn_1.state is SendAttemptState.CONFIRMED_NO_RESPONSE
            ):
                if not _record_authorizes_retry(turn_1):
                    return _turn_1_no_response_result(
                        observation_id,
                        platform,
                        self.repository,
                        run_id,
                        fences,
                        turn_1,
                    )
                await _retry_backoff(
                    policy,
                    cancelled=lambda: self._is_cancelled(run_id),
                )
                failed_submission = (
                    await driver.restore(
                        BrowserIdentity.from_mapping(
                            _reconciled_identity(turn_1)
                        ),
                        observation_id=observation_id,
                    )
                    if turn_1.native_retry_available is True
                    else None
                )
                if failed_submission is not None:
                    native = await _restore_and_prepare_native_retry(
                        failed_submission,
                        turn_1,
                        prompt,
                        binding=self.recovery.correlation(turn_1.fence_id) if self.recovery is not None else None,
                    )
                    if native:
                        await self._await_send_admission(run_id, observation_id, platform, profile_id)
                        turn_1 = fences.prepare_retry(
                            confirmed_fence_id=turn_1.fence_id,
                            successor_fence_id=_send_attempt_id(
                                turn_1_logical_id,
                                _attempt_ordinal(turn_1) + 1,
                            ),
                            identity=failed_submission.identity.to_mapping(),
                            action="measurement_turn_1",
                            prompt_sha256=_text_sha(prompt),
                            logical_turn_id=turn_1_logical_id,
                            attempt_ordinal=_attempt_ordinal(turn_1) + 1,
                            observation_attempt_ordinal=chat_ordinal,
                            native_retry=True,
                        )
                        submission = failed_submission
                        prepared_native = True
                    else:
                        submission = None
                if submission is None:
                    if _next_attempt_ordinal(turn_1_attempts) > (
                        policy.authorized_max_attempts
                    ):
                        return _turn_1_no_response_result(
                            observation_id,
                            platform,
                            self.repository,
                            run_id,
                            fences,
                            turn_1,
                        )
                    chat_ordinal += 1
                    turn_1 = None
                    turn_2 = None

            if turn_1 is None:
                submission = await self._open_website_page(
                    run_id,
                    lambda: driver.open(
                        profile_id=profile_id,
                        chrome_directory=chrome_directory,
                        observation_id=observation_id,
                        fresh=True,
                    ),
                )
                turn_1_attempts = _logical_attempts(
                    fences,
                    turn_1_logical_id,
                )
                attempt_ordinal = _next_attempt_ordinal(turn_1_attempts)
                predecessor = (
                    turn_1_attempts[-1] if turn_1_attempts else None
                )
                if (
                    predecessor is not None
                    and predecessor.state
                    is SendAttemptState.CONFIRMED_NO_RESPONSE
                ):
                    await self._await_send_admission(run_id, observation_id, platform, profile_id)
                    turn_1 = fences.prepare_retry(
                        confirmed_fence_id=predecessor.fence_id,
                        successor_fence_id=_send_attempt_id(
                            turn_1_logical_id,
                            attempt_ordinal,
                        ),
                        identity=submission.identity.to_mapping(),
                        action="measurement_turn_1",
                        prompt_sha256=_text_sha(prompt),
                        logical_turn_id=turn_1_logical_id,
                        attempt_ordinal=attempt_ordinal,
                        observation_attempt_ordinal=chat_ordinal,
                    )
                else:
                    await self._await_send_admission(run_id, observation_id, platform, profile_id)
                    turn_1 = fences.prepare(
                        fence_id=_send_attempt_id(
                            turn_1_logical_id,
                            attempt_ordinal,
                        ),
                        observation_id=observation_id,
                        action="measurement_turn_1",
                        prompt_sha256=_text_sha(prompt),
                        identity=submission.identity.to_mapping(),
                        logical_turn_id=turn_1_logical_id,
                        attempt_ordinal=attempt_ordinal,
                        observation_attempt_ordinal=chat_ordinal,
                        retried_from=(
                            predecessor.fence_id
                            if predecessor is not None
                            else None
                        ),
                    )
                prepared_native = False
            elif (
                turn_1.state
                in {
                    SendAttemptState.PREPARING,
                    SendAttemptState.PREPARED,
                    SendAttemptState.PRE_SEND_FAILED,
                }
                and not prepared_native
            ):
                _require_resume(resume, turn_1.fence_id)
                if turn_1.native_retry:
                    parent = (
                        fences.load(turn_1.retried_from)
                        if turn_1.retried_from
                        else None
                    )
                    if parent is None:
                        raise IntegrityError(
                            "native retry successor lacks its confirmed "
                            "predecessor"
                        )
                    submission = await driver.restore(
                        BrowserIdentity.from_mapping(
                            _reconciled_identity(parent)
                        ),
                        observation_id=observation_id,
                    )
                    if (
                        submission is None
                        or not await _restore_and_prepare_native_retry(
                            submission,
                            parent,
                            prompt,
                            binding=self.recovery.correlation(parent.fence_id) if self.recovery is not None else None,
                        )
                    ):
                        raise AmbiguousSendError(
                            "the prepared native retry could not be "
                            f"re-adopted safely ({turn_1.fence_id})"
                        )
                    prepared_native = True
                else:
                    submission = await self._open_website_page(
                        run_id,
                        lambda: driver.open(
                            profile_id=profile_id,
                            chrome_directory=chrome_directory,
                            observation_id=observation_id,
                            fresh=True,
                        ),
                    )
                    self._require_active_run(run_id)
                    turn_1 = fences.retry_pre_send_failure(
                        turn_1.fence_id,
                        identity=submission.identity.to_mapping(),
                    )
                    prepared_native = False

            if turn_1.state not in {
                SendAttemptState.ACKNOWLEDGED,
                SendAttemptState.REJECTED,
            }:
                assert submission is not None
                while True:
                    next_ordinal = _attempt_ordinal(turn_1) + 1
                    try:
                        (
                            answer_text,
                            answer_reference,
                            answer_identity,
                        ) = await self._send(
                            run_id,
                            fences,
                            turn_1,
                            submission,
                            prompt,
                            packet_id=packet_id,
                            policy=policy,
                            allow_fresh_retry=(
                                next_ordinal
                                <= policy.authorized_max_attempts
                            ),
                            prepared_native=prepared_native,
                        )
                        break
                    except _ConfirmedNoResponse as failed:
                        turn_1 = failed.record
                        if not _record_authorizes_retry(turn_1):
                            return _turn_1_no_response_result(
                                observation_id,
                                platform,
                                self.repository,
                                run_id,
                                fences,
                                turn_1,
                            )
                        await _retry_backoff(
                            policy,
                            cancelled=lambda: self._is_cancelled(run_id),
                        )
                        native = (
                            turn_1.native_retry_available is True
                            and await _prepare_native_retry(submission)
                        )
                        if native:
                            await self._await_send_admission(run_id, observation_id, platform, profile_id)
                            turn_1 = fences.prepare_retry(
                                confirmed_fence_id=turn_1.fence_id,
                                successor_fence_id=_send_attempt_id(
                                    turn_1_logical_id,
                                    next_ordinal,
                                ),
                                identity=submission.identity.to_mapping(),
                                action="measurement_turn_1",
                                prompt_sha256=_text_sha(prompt),
                                logical_turn_id=turn_1_logical_id,
                                attempt_ordinal=next_ordinal,
                                observation_attempt_ordinal=chat_ordinal,
                                native_retry=True,
                            )
                            prepared_native = True
                            continue
                        chat_ordinal += 1
                        turn_1 = None
                        break
                if turn_1 is None:
                    continue
            else:
                answer_text, answer_reference = _read_attempt_response(
                    self.repository,
                    run_id,
                    turn_1,
                )
                answer_identity = BrowserIdentity.from_mapping(
                    turn_1.ack_identity or turn_1.identity
                )

            assert answer_text is not None
            assert answer_reference is not None
            assert answer_identity is not None
            accepted_turn_1 = fences.load(turn_1.fence_id) or turn_1
            if not research:
                extraction_prompt = same_chat_extraction_prompt(
                    answer_text,
                    platform=platform,
                    expected_prompt_sha256=turn_2.prompt_sha256 if turn_2 is not None else None,
                )

            if turn_2 is None:
                continuation=(self.recovery.continuations.pop(observation_id,None)
                    if self.recovery is not None else None)
                if continuation is not None:
                    self.recovery.assert_owned()
                    verified_submission,verified_identity=continuation
                    for field in ('profile_id','chrome_directory','browser_context_reference','conversation_reference'):
                        expected_value, actual_value = getattr(answer_identity, field), getattr(verified_identity, field)
                        if field == 'conversation_reference' and canonical_reference is not None:
                            expected_value, actual_value = canonical_reference(expected_value), canonical_reference(actual_value)
                        if not expected_value or expected_value != actual_value:
                            raise IdentityMismatchError('recovered continuation identity mismatch')
                    submission=verified_submission
                    answer_identity=verified_identity
                if submission is not None:
                    submission = submission.next_turn()
                    answer_identity.assert_same_tab(
                        submission.identity,
                        require_same_conversation=True,
                        canonical=canonical_reference,
                    )
                else:
                    submission = await driver.restore(
                        answer_identity,
                        observation_id=observation_id,
                    )
                    if submission is None:
                        raise IdentityMismatchError(
                            "the original provider tab is unavailable; no "
                            "replacement extraction chat was opened"
                        )
                    answer_identity.assert_same_tab(
                        submission.identity,
                        require_same_conversation=True,
                        canonical=canonical_reference,
                    )
                _use_same_chat_extraction_timeout(
                    submission,
                    policy,
                    platform,
                )
                turn_2_attempts = _logical_attempts(
                    fences,
                    turn_2_logical_id,
                )
                attempt_ordinal = _next_attempt_ordinal(turn_2_attempts)
                predecessor = (
                    turn_2_attempts[-1] if turn_2_attempts else None
                )
                if (
                    predecessor is not None
                    and predecessor.state
                    is SendAttemptState.CONFIRMED_NO_RESPONSE
                    and _observation_attempt_ordinal(predecessor) == chat_ordinal
                ):
                    await self._await_send_admission(run_id, observation_id, platform, profile_id)
                    turn_2 = fences.prepare_retry(
                        confirmed_fence_id=predecessor.fence_id,
                        successor_fence_id=_send_attempt_id(
                            turn_2_logical_id,
                            attempt_ordinal,
                        ),
                        identity=submission.identity.to_mapping(),
                        action="same_chat_turn_2",
                        prompt_sha256=_text_sha(
                            extraction_prompt
                        ),
                        logical_turn_id=turn_2_logical_id,
                        attempt_ordinal=attempt_ordinal,
                        observation_attempt_ordinal=chat_ordinal,
                    )
                else:
                    await self._await_send_admission(run_id, observation_id, platform, profile_id)
                    turn_2 = fences.prepare(
                        fence_id=_send_attempt_id(
                            turn_2_logical_id,
                            attempt_ordinal,
                        ),
                        observation_id=observation_id,
                        action="same_chat_turn_2",
                        prompt_sha256=_text_sha(
                            extraction_prompt
                        ),
                        identity=submission.identity.to_mapping(),
                        logical_turn_id=turn_2_logical_id,
                        attempt_ordinal=attempt_ordinal,
                        observation_attempt_ordinal=chat_ordinal,
                        retried_from=(
                            predecessor.fence_id
                            if predecessor is not None
                            else None
                        ),
                    )
                prepared_native = False
            elif (
                turn_2.state
                in {
                    SendAttemptState.PREPARING,
                    SendAttemptState.PREPARED,
                    SendAttemptState.PRE_SEND_FAILED,
                }
                and not prepared_native
            ):
                _require_resume(resume, turn_2.fence_id)
                if turn_2.native_retry:
                    parent = (
                        fences.load(turn_2.retried_from)
                        if turn_2.retried_from
                        else None
                    )
                    if parent is None:
                        raise IntegrityError(
                            "native retry successor lacks its confirmed "
                            "predecessor"
                        )
                    submission = await driver.restore(
                        BrowserIdentity.from_mapping(
                            _reconciled_identity(parent)
                        ),
                        observation_id=observation_id,
                    )
                    if submission is not None:
                        _use_same_chat_extraction_timeout(
                            submission,
                            policy,
                            platform,
                        )
                    if (
                        submission is None
                        or not await _restore_and_prepare_native_retry(
                            submission,
                            parent,
                            extraction_prompt,
                            binding=self.recovery.correlation(parent.fence_id) if self.recovery is not None else None,
                        )
                    ):
                        raise AmbiguousSendError(
                            "the prepared native retry could not be "
                            f"re-adopted safely ({turn_2.fence_id})"
                        )
                    answer_identity.assert_same_tab(
                        submission.identity,
                        require_same_conversation=True,
                        canonical=canonical_reference,
                    )
                    prepared_native = True
                else:
                    if submission is None:
                        submission = await driver.restore(
                            answer_identity,
                            observation_id=observation_id,
                        )
                    if submission is None:
                        raise IdentityMismatchError(
                            "the original provider tab is unavailable; no "
                            "replacement extraction chat was opened"
                        )
                    _use_same_chat_extraction_timeout(
                        submission,
                        policy,
                        platform,
                    )
                    answer_identity.assert_same_tab(
                        submission.identity,
                        require_same_conversation=True,
                        canonical=canonical_reference,
                    )
                    self._require_active_run(run_id)
                    turn_2 = fences.retry_pre_send_failure(
                        turn_2.fence_id,
                        identity=submission.identity.to_mapping(),
                    )
                    prepared_native = False

            assert submission is not None
            restart_full_observation = False
            while True:
                attempt_snapshot = fences.snapshot(observation_ids=(observation_id,))
                turn_1_attempts = _logical_attempts(attempt_snapshot, turn_1_logical_id)
                turn_2_attempts = _logical_attempts(attempt_snapshot, turn_2_logical_id)
                next_turn_2_ordinal = _attempt_ordinal(turn_2) + 1
                can_restart = _can_restart_conversation(
                    turn_1_attempts,
                    turn_2_attempts,
                    policy,
                )
                try:
                    (
                        extraction_text,
                        extraction_reference,
                        extraction_identity,
                    ) = await self._send(
                        run_id,
                        fences,
                        turn_2,
                        submission,
                        extraction_prompt,
                        packet_id=packet_id,
                        acknowledge=False,
                        policy=policy,
                        allow_fresh_retry=can_restart,
                        prepared_native=prepared_native,
                    )
                except _ConfirmedNoResponse as failed:
                    turn_2 = failed.record
                    if not _record_authorizes_retry(turn_2):
                        return _conversational_no_response_result(
                            observation_id,
                            platform,
                            self.repository,
                            run_id,
                            fences,
                            accepted_turn_1=accepted_turn_1,
                            failed_turn=turn_2,
                        )
                    await _retry_backoff(
                        policy,
                        cancelled=lambda: self._is_cancelled(run_id),
                    )
                    native = (
                        turn_2.native_retry_available is True
                        and await _prepare_native_retry(submission)
                    )
                    if native:
                        await self._await_send_admission(run_id, observation_id, platform, profile_id)
                        turn_2 = fences.prepare_retry(
                            confirmed_fence_id=turn_2.fence_id,
                            successor_fence_id=_send_attempt_id(
                                turn_2_logical_id,
                                next_turn_2_ordinal,
                            ),
                            identity=submission.identity.to_mapping(),
                            action="same_chat_turn_2",
                            prompt_sha256=_text_sha(
                                extraction_prompt
                            ),
                            logical_turn_id=turn_2_logical_id,
                            attempt_ordinal=next_turn_2_ordinal,
                            observation_attempt_ordinal=chat_ordinal,
                            native_retry=True,
                        )
                        prepared_native = True
                        continue
                    if not can_restart:
                        turn_2 = fences.exhaust_authorized_retry(
                            turn_2.fence_id,
                            detail=(
                                "native retry could not be prepared and the "
                                "full-observation retry ceiling was already "
                                "exhausted"
                            ),
                        )
                        return _conversational_no_response_result(
                            observation_id,
                            platform,
                            self.repository,
                            run_id,
                            fences,
                            accepted_turn_1=accepted_turn_1,
                            failed_turn=turn_2,
                        )
                    restart_full_observation = True
                    break

                answer_identity.assert_same_tab(
                    extraction_identity,
                    require_same_conversation=True,
                    canonical=canonical_reference,
                )
                try:
                    grounded = _validate_chat_extraction(observation, answer_text, extraction_text)
                except MentionExtractionError as exc:
                    terminal = fences.acknowledge(
                        turn_2.fence_id,
                        validation_status="rejected",
                        detail=str(exc),
                        identity=extraction_identity.to_mapping(),
                    )
                    await self._capture_send_failure(
                        run_id, packet_id, terminal, submission, exc,
                        phase="extraction_validation",
                    )
                    attempt_snapshot = fences.snapshot(observation_ids=(observation_id,))
                    if allow_restart and _can_restart_conversation(
                        _logical_attempts(attempt_snapshot, turn_1_logical_id),
                        _logical_attempts(attempt_snapshot, turn_2_logical_id),
                        policy,
                    ):
                        # Confirmed name listing failure: start the chat again
                        # from scratch for the same prompt (founder decision
                        # 2026-09-10). The rejected attempt stays on disk.
                        await _retry_backoff(
                            policy,
                            cancelled=lambda: self._is_cancelled(run_id),
                        )
                        restart_full_observation = True
                        break
                    return _conversational_result(
                        observation_id,
                        platform,
                        answer_text,
                        answer_reference,
                        extraction_text,
                        extraction_reference,
                        terminal,
                        fences=fences,
                        accepted_turn_1=accepted_turn_1,
                        observation=observation,
                    )
                terminal = fences.acknowledge(
                    turn_2.fence_id,
                    validation_status="accepted",
                    identity=extraction_identity.to_mapping(),
                )
                return {
                    "observation_id": observation_id,
                    "platform": platform,
                    "status": "succeeded",
                    "collection_status": "collected",
                    "extraction_status": "complete",
                    "grounded_occurrences": grounded,
                    "artifacts": {
                        "turn_1_response": answer_reference,
                        "turn_2_response": extraction_reference,
                    },
                    "issues": [],
                    **_client_recommendation_fields(observation, answer_text, extraction_text, plan),
                    **_attempt_result_metadata(
                        fences,
                        observation_id,
                        accepted_attempt=terminal,
                        accepted_attempts={
                            "measurement_turn_1": accepted_turn_1.fence_id,
                            "same_chat_turn_2": terminal.fence_id,
                        },
                    ),
                }

            if restart_full_observation:
                chat_ordinal += 1
                continue

    async def _google(self, *args, **kwargs):
        raise StateTransitionError(GOOGLE_AI_OVERVIEW_DEFERRED)

    async def _send(
        self,
        run_id: str,
        fences: SendAttemptStore,
        fence: SendAttempt,
        submission: Submission,
        prompt: str,
        *,
        packet_id: str,
        acknowledge: bool = True,
        return_answer: bool = False,
        policy: RetryPolicy,
        allow_fresh_retry: bool = False,
        prepared_native: bool = False,
    ):
        try:
            return await self._send_observed(
                run_id, fences, fence, submission, prompt, packet_id=packet_id,
                acknowledge=acknowledge, return_answer=return_answer, policy=policy,
                allow_fresh_retry=allow_fresh_retry, prepared_native=prepared_native,
            )
        finally:
            try:
                finish = getattr(submission, "finish_failure_observation", None)
                if callable(finish):
                    finish()
            except Exception:
                pass  # Passive observer cleanup cannot replace the send outcome.

    async def _send_observed(
        self,
        run_id: str,
        fences: SendAttemptStore,
        fence: SendAttempt,
        submission: Submission,
        prompt: str,
        *,
        packet_id: str,
        acknowledge: bool = True,
        return_answer: bool = False,
        policy: RetryPolicy,
        allow_fresh_retry: bool = False,
        prepared_native: bool = False,
    ):
        require_active_action(fence.action)
        if (
            _attempt_ordinal(fence) > policy.authorized_max_attempts
            or _observation_attempt_ordinal(fence)
            > policy.authorized_max_attempts
        ):
            raise IntegrityError(
                f"{fence.fence_id} exceeds the frozen retry ceiling "
                f"of {policy.authorized_max_attempts} attempts"
            )

        async def failed(detail: object) -> None:
            current = fences.load(fence.fence_id)
            if current is not None and current.state in {
                SendAttemptState.PREPARING,
                SendAttemptState.PREPARED,
            }:
                fences.fail_before_send(fence.fence_id, detail)

        try:
            if not prepared_native:
                # A second message in an existing chat (``same_chat_turn_2``)
                # and the Google AI Overview parser message get the longer
                # preparation budget; a first message keeps the default.
                preparation_timeout = (
                    PROVIDER_SECOND_MESSAGE_PREPARATION_TIMEOUT_SECONDS
                    if fence.action in _SECOND_MESSAGE_ACTIONS
                    else None
                )
                if self.recovery is not None:
                    # Exact input preparation has its own provider deadline;
                    # it must not inherit a nearly spent navigation deadline.
                    # The advertised deadline covers the whole step: the
                    # pre-send pacing pause, which runs before the budget
                    # starts, plus the budget itself.
                    self.recovery.progress(fence.observation_id,
                        deadline=time.monotonic()+_preparation_step_seconds(
                            submission, preparation_timeout, prompt=prompt))
                await self._await_with_cancellation(
                    run_id,
                    submission.prepare_exact(
                        prompt,
                        on_pre_send_failure=failed,
                        timeout_seconds=preparation_timeout,
                    ),
                    detail=(
                        "module cancellation interrupted exact provider "
                        "preparation"
                    ),
                )
            baseline = _response_baseline_mapping(submission)
            fences.mark_prepared(
                fence.fence_id,
                response_baseline=baseline,
            )
            self._emit(
                EventKind.OBSERVATION_PREPARED,
                run_id,
                packet_id,
                fence.observation_id,
            )
        except BaseException as exc:
            await failed(
                _pre_send_failure_detail("provider_prepare_failed", exc)
            )
            if isinstance(exc, Exception):
                await self._capture_send_failure(
                    run_id, packet_id, fence, submission, exc,
                    phase="provider_preparation",
                )
            raise
        if self._durable_is_cancelled(run_id):
            fences.fail_before_send(
                fence.fence_id,
                {
                    "kind": "cancelled_pre_send",
                    "phase": "pre_send",
                    "detail": (
                        "module cancellation was observed after exact input "
                        "verification and before send start"
                    ),
                },
            )
            raise BrowserInteractionError(
                "module cancellation interrupted the prepared send"
            )
        validate_identity = getattr(
            submission,
            "validate_pre_send_identity",
            None,
        )
        if callable(validate_identity):
            try:
                if self.recovery is not None:
                    self.recovery.progress(fence.observation_id,
                        deadline=time.monotonic()+PROVIDER_PRE_SEND_IDENTITY_TIMEOUT_SECONDS)
                await self._await_with_cancellation(
                    run_id,
                    validate_identity(),
                    detail=(
                        "module cancellation interrupted pre-send identity "
                        "verification"
                    ),
                )
            except BaseException as exc:
                await failed(
                    _pre_send_failure_detail("provider_identity_changed", exc)
                )
                if isinstance(exc, Exception):
                    await self._capture_send_failure(
                        run_id, packet_id, fence, submission, exc,
                        phase="pre_send_identity",
                    )
                raise
        initial_identity = submission.identity
        self._send_phases[fence.fence_id] = "send_admission"
        try:
            fences.admit_send(fence.fence_id)
        except _CancellationRequested:
            self.cancel_event.set()
            raise BrowserInteractionError(
                "module cancellation interrupted the prepared send"
            )
        self._emit(
            EventKind.SEND_STARTED,
            run_id,
            packet_id,
            fence.observation_id,
        )
        if self._durable_is_cancelled(run_id):
            raise AmbiguousSendError(
                f"{fence.fence_id} was cancelled after send start and "
                "before provider submission"
            )
        self._send_phases[fence.fence_id] = "provider_submission"
        checkpoint = getattr(submission, "set_correlation_checkpoint", None)
        if callable(checkpoint) and self.recovery is not None:
            checkpoint(lambda binding: self.recovery.checkpoint(fence, binding))
        try:
            await self._await_with_cancellation(
                run_id,
                submission.submit_once(),
                detail=(
                    "module cancellation interrupted provider submission "
                    "after send start"
                ),
            )
        except asyncio.CancelledError:
            # The irreversible browser action was entered, so SEND_STARTED
            # remains the durable ambiguity proof. Propagate measurement group
            # cancellation so asyncio.wait_for cannot lose its deadline.
            raise
        except BaseException as exc:
            # The irreversible browser action was entered. Whether the
            # website received it is unknowable, so this send attempt deliberately
            # remains SEND_STARTED and may never authorize an automatic resend.
            if isinstance(exc, Exception):
                await self._capture_send_failure(
                    run_id, packet_id, fence, submission, exc,
                    phase="provider_submission",
                )
            if _provider_access_error(exc) is not None:
                raise AmbiguousSendError(
                    f"{fence.fence_id} encountered a provider access gate "
                    "after send start"
                ) from exc
            raise AmbiguousSendError(
                f"{fence.fence_id} failed after send start: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        self._send_phases[fence.fence_id] = "response_wait"
        try:
            answer = await self._await_with_cancellation(
                run_id,
                submission.wait_response(),
                detail=(
                    "module cancellation interrupted the sent response wait"
                ),
            )
        except TimeoutError as exc:
            # Preserve the timed-out view before reconciliation reads it. The
            # diagnostic read consumes this grace window; it never renews it.
            grace_deadline = time.monotonic() + policy.late_response_grace_seconds
            await self._capture_send_failure(
                run_id, packet_id, fence, submission, exc,
                phase="response_wait", deadline=grace_deadline,
            )
            self._send_phases[fence.fence_id] = "response_reconciliation"
            try:
                answer = await self._reconcile_timed_out_send(
                    run_id,
                    fences,
                    fence,
                    submission,
                    initial_identity,
                    packet_id=packet_id,
                    policy=policy,
                    allow_fresh_retry=allow_fresh_retry,
                    grace_deadline=grace_deadline,
                )
            except Exception as reconciliation_error:
                await self._capture_send_failure(
                    run_id, packet_id, fence, submission, reconciliation_error,
                    phase="response_reconciliation",
                )
                raise
            late_response = True
        except (AmbiguousSendError, IdentityMismatchError) as exc:
            await self._capture_send_failure(
                run_id, packet_id, fence, submission, exc, phase="response_wait",
            )
            raise
        except Exception as exc:
            # The send already started. A website/DOM failure while waiting
            # does not prove whether a response exists, so it must never be
            # downgraded to an ordinary retry-safe prompt measurement failure.
            await self._capture_send_failure(
                run_id, packet_id, fence, submission, exc, phase="response_wait",
            )
            if _provider_access_error(exc) is not None:
                raise AmbiguousSendError(
                    f"{fence.fence_id} encountered a provider access gate "
                    "while awaiting its response"
                ) from exc
            raise AmbiguousSendError(
                f"{fence.fence_id} response wait failed after send start: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        else:
            late_response = False
            try:
                initial_identity.assert_same_tab(answer.identity)
            except IdentityMismatchError as exc:
                await self._capture_send_failure(
                    run_id, packet_id, fence, submission, exc,
                    phase="response_wait",
                )
                raise
        self._send_phases[fence.fence_id] = "response_capture"
        response_evidence = _provider_response_evidence(
            answer,
            action=fence.action,
            expected_prompt_sha256=fence.prompt_sha256,
        )
        response_payload = answer.text
        response_media_type = "text/plain; charset=utf-8"
        response_suffix = {
            "measurement_turn_1": ".turn-1.txt",
            "same_chat_turn_2": ".turn-2.txt",
            "google_search": ".google-source.txt",
            "gaio_gemini_extractor": ".gemini-extractor.txt",
        }.get(fence.action, ".txt")
        if (
            fence.action == "google_search"
            and answer.status is ProviderStatus.NOT_SHOWN
        ):
            if response_evidence is None:
                raise IntegrityError(
                    "Google NOT_SHOWN cannot persist an empty source artifact"
                )
            response_payload = (
                json.dumps(
                    response_evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            response_media_type = "application/json"
            response_suffix = ".google-not-shown-evidence.json"
        self._assert_execution()
        reference = self.repository.write_evidence(
            run_id,
            response_payload,
            media_type=response_media_type,
            suffix=response_suffix,
        )
        if late_response:
            # ProviderAnswer is frozen and intentionally carries no mutable
            # measurement execution state. The reconciliation was cached on the worker
            # immediately before returning the late answer.
            result = self._late_reconciliations.pop(fence.fence_id)
            fences.capture_late_response(
                fence.fence_id,
                response_reference=str(reference["path"]),
                response_sha256=str(reference["sha256"]),
                reconciliation_evidence=result.evidence,
                response_evidence=response_evidence,
                failure_classification=(
                    result.failure_classification or "late_response"
                ),
                native_retry_available=result.native_retry_available,
                identity=result.identity.to_mapping(),
            )
        else:
            fences.capture_response(
                fence.fence_id,
                response_reference=str(reference["path"]),
                response_sha256=str(reference["sha256"]),
                response_evidence=response_evidence,
            )
        if self.recovery is not None:
            self.recovery.captured(fences.load(fence.fence_id) or fence)
        self._emit(
            EventKind.RESPONSE_CAPTURED,
            run_id,
            packet_id,
            fence.observation_id,
        )
        if acknowledge:
            self._send_phases[fence.fence_id] = "response_acknowledgement"
            fences.acknowledge(
                fence.fence_id,
                validation_status=answer.status.value,
                identity=answer.identity.to_mapping(),
            )
            self._emit(
                EventKind.SEND_ACKNOWLEDGED,
                run_id,
                packet_id,
                fence.observation_id,
            )
            self._send_phases.pop(fence.fence_id, None)
        else:
            self._send_phases[fence.fence_id] = "response_validation"
        base = (answer.text, reference, answer.identity)
        return (*base, answer) if return_answer else base

    async def _reconcile_timed_out_send(
        self,
        run_id: str,
        fences: SendAttemptStore,
        fence: SendAttempt,
        submission: Submission,
        initial_identity: BrowserIdentity,
        *,
        packet_id: str,
        policy: RetryPolicy,
        allow_fresh_retry: bool,
        grace_deadline: Optional[float] = None,
    ) -> ProviderAnswer:
        durable = fences.load(fence.fence_id)
        try:
            if (
                durable is None
                or durable.state is not SendAttemptState.SEND_STARTED
                or durable.response_baseline is None
            ):
                raise ValueError("missing durable pre-send response baseline")
            ResponseBaseline.from_mapping(durable.response_baseline)
        except (TypeError, ValueError) as exc:
            evidence = {
                "reason": "missing_or_invalid_durable_response_baseline",
                "checks": [],
                "response_baseline": (
                    durable.response_baseline
                    if durable is not None
                    else None
                ),
                "detail": str(exc),
            }
            record = fences.mark_reconciliation_ambiguous(
                fence.fence_id,
                failure_classification="response_baseline_unproven",
                reconciliation_evidence=evidence,
                detail=(
                    "automatic reconciliation requires a valid response "
                    "baseline captured before send start"
                ),
            )
            raise AmbiguousSendError(
                f"{record.fence_id} timed out without durable response "
                "baseline proof"
            ) from exc
        fence = durable
        reconcile = getattr(submission, "reconcile_no_response", None)
        if not callable(reconcile):
            record = fences.mark_reconciliation_ambiguous(
                fence.fence_id,
                failure_classification="reconciliation_unavailable",
                reconciliation_evidence={
                    "reason": "provider_reconciliation_unavailable",
                    "checks": [],
                    "response_baseline": fence.response_baseline,
                },
                detail=(
                    "the provider submission does not expose a safe "
                    "late-response reconciliation seam"
                ),
            )
            raise AmbiguousSendError(
                f"{record.fence_id} timed out without a safe reconciliation "
                "seam"
            )
        try:
            result = await self._await_with_cancellation(
                run_id,
                reconcile(
                    policy.late_response_grace_seconds
                    if grace_deadline is None
                    else max(0.0, grace_deadline - time.monotonic())
                ),
                detail=(
                    "durable cancellation interrupted sent-action "
                    "reconciliation"
                ),
            )
        except asyncio.CancelledError as exc:
            fences.mark_reconciliation_ambiguous(
                fence.fence_id,
                failure_classification="reconciliation_cancelled",
                reconciliation_evidence={
                    "reason": "provider_reconciliation_cancelled",
                    "checks": [],
                    "response_baseline": fence.response_baseline,
                    "exception_type": type(exc).__name__,
                    "detail": (
                        "packet execution was cancelled during provider "
                        "reconciliation"
                    ),
                },
                detail=(
                    "packet execution was cancelled during provider "
                    "reconciliation"
                ),
            )
            # Keep the sent send attempt fail-closed, but do not consume the measurement group
            # deadline cancellation.
            raise
        except _CancellationRequested as exc:
            # Cancellation does not rewrite the original irreversible send attempt.
            # SEND_STARTED remains the exact ambiguity proof.
            raise AmbiguousSendError(
                f"{fence.fence_id} reconciliation stopped by durable "
                "cancellation"
            ) from exc
        except BaseException as exc:
            record = fences.mark_reconciliation_ambiguous(
                fence.fence_id,
                failure_classification="reconciliation_failed",
                reconciliation_evidence={
                    "reason": "provider_reconciliation_failed",
                    "checks": [],
                    "response_baseline": fence.response_baseline,
                    "exception_type": type(exc).__name__,
                    "detail": str(exc) or repr(exc),
                },
                detail=str(exc) or repr(exc),
            )
            raise AmbiguousSendError(
                f"{record.fence_id} reconciliation failed safely closed"
            ) from exc
        if not isinstance(result, ReconciliationResult):
            record = fences.mark_reconciliation_ambiguous(
                fence.fence_id,
                failure_classification="invalid_reconciliation_result",
                reconciliation_evidence={
                    "reason": "invalid_provider_reconciliation_result",
                    "checks": [],
                    "response_baseline": fence.response_baseline,
                },
            )
            raise AmbiguousSendError(
                f"{record.fence_id} returned an invalid reconciliation result"
            )
        try:
            initial_identity.assert_same_tab(result.identity)
        except IdentityMismatchError as exc:
            record = fences.mark_reconciliation_ambiguous(
                fence.fence_id,
                failure_classification="browser_identity_changed",
                reconciliation_evidence=dict(result.evidence),
                detail=str(exc),
                identity=result.identity.to_mapping(),
            )
            raise AmbiguousSendError(
                f"{record.fence_id} changed browser identity during "
                "reconciliation"
            ) from exc

        if (
            result.disposition
            is ReconciliationDisposition.LATE_RESPONSE_CAPTURED
        ):
            if result.answer is None:
                record = fences.mark_reconciliation_ambiguous(
                    fence.fence_id,
                    failure_classification="late_response_missing_answer",
                    reconciliation_evidence=dict(result.evidence),
                )
                raise AmbiguousSendError(
                    f"{record.fence_id} reported a late response without "
                    "capturable answer evidence"
                )
            initial_identity.assert_same_tab(result.answer.identity)
            self._late_reconciliations[fence.fence_id] = result
            return result.answer

        if (
            result.disposition
            is ReconciliationDisposition.CONFIRMED_TERMINAL_NO_RESPONSE
        ):
            current_ordinal = _attempt_ordinal(
                fences.load(fence.fence_id) or fence
            )
            capacity = (
                current_ordinal < policy.authorized_max_attempts
            )
            authorized = bool(
                policy.enabled
                and capacity
                and (
                    result.native_retry_available
                    or allow_fresh_retry
                )
            )
            decision = (
                "retry_authorized"
                if authorized
                else "retry_disabled"
                if not policy.enabled
                else "retry_exhausted"
            )
            record = fences.confirm_no_response(
                fence.fence_id,
                failure_classification=(
                    result.failure_classification
                    or "provider_terminal_no_response"
                ),
                reconciliation_evidence=dict(result.evidence),
                retry_decision=decision,
                native_retry_available=result.native_retry_available,
                identity=result.identity.to_mapping(),
            )
            raise _ConfirmedNoResponse(record)

        if (
            result.disposition
            is ReconciliationDisposition.TERMINAL_NONRETRYABLE
        ):
            record = fences.confirm_no_response(
                fence.fence_id,
                failure_classification=(
                    result.failure_classification
                    or "provider_terminal_nonretryable"
                ),
                reconciliation_evidence=dict(result.evidence),
                retry_decision="do_not_retry",
                native_retry_available=False,
                identity=result.identity.to_mapping(),
            )
            access_error = _reconciled_access_error(
                record.failure_classification,
                result.evidence,
            )
            if access_error is not None:
                raise access_error
            raise _ConfirmedNoResponse(
                record,
                terminal_nonretryable=True,
            )

        record = fences.mark_reconciliation_ambiguous(
            fence.fence_id,
            failure_classification=(
                result.failure_classification
                or "ambiguous_no_response"
            ),
            reconciliation_evidence=dict(result.evidence),
            native_retry_available=result.native_retry_available,
            identity=result.identity.to_mapping(),
        )
        self._emit(
            EventKind.NEEDS_ATTENTION,
            run_id,
            packet_id,
            fence.observation_id,
        )
        access_error = _reconciled_access_error(record.failure_classification, result.evidence)
        raise AmbiguousSendError(
            f"{record.fence_id} remains ambiguous after late-response "
            "reconciliation"
        ) from access_error

    async def _capture_send_failure(
        self, run_id: str, packet_id: str, fence: Optional[SendAttempt],
        submission: Submission, error: Exception, *, phase: str,
        deadline: Optional[float] = None,
        observation_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Best-effort private evidence from the original owned page; no input."""

        until = time.monotonic() + 3.0
        if deadline is not None:
            until = min(until, deadline)
        budget = _BrowserOperationDeadline(until, "failure DOM capture timed out")
        try:
            page = getattr(submission, "page", None)
            assert_owned = getattr(getattr(submission, "_actions", None), "assert_owned", None)
            if page is None or not callable(assert_owned):
                return
            self._assert_execution()
            if await _await_call(assert_owned, until, _deadline=budget) is not True:
                return
        except (Exception, asyncio.CancelledError):
            return  # Lost/unproven ownership forbids even a diagnostic page read.
        try:
            durable = (self._send_attempts(run_id).load(fence.fence_id) or fence) if fence else None
            observation_id = fence.observation_id if fence else observation_id
            if not observation_id:
                return
            metadata = {
                "schema_version": "visibility_runner.failure_dom.v1",
                "run_id": run_id, "packet_id": packet_id,
                "observation_id": observation_id, "fence_id": fence.fence_id if fence else None,
                "action": fence.action if fence else "provider_open", "phase": phase,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "response_baseline": durable.response_baseline if durable else None,
                "failure_context": _safe_failure_context(error, phase=phase),
                "coherence": "unconfirmed", "html_status": "unavailable",
            }
            if phase in {"provider_preparation", "provider_open"}:
                metadata["preparation_stage"] = getattr(submission, "_preparation_phase", None)
            marker = getattr(submission, "_last_access_block_marker", None)
            if isinstance(marker, str):
                metadata["last_access_block_marker"] = marker[:80]
            dialog = getattr(page, "_visibility_runner_blocking_dialog", None)
            if dialog is not None:
                # Browser dialogs are outside the document HTML. Preserve the
                # original notice privately without accepting or dismissing it.
                metadata["browser_dialog"] = {
                    "type": str(getattr(dialog, "type", "unknown"))[:40],
                    "message": str(getattr(dialog, "message", ""))[:4000],
                }
        except Exception:
            return
        try:
            usage_snapshot = getattr(submission, "usage_limit_snapshot", None)
            usage_limit = usage_snapshot() if callable(usage_snapshot) else None
            if isinstance(usage_limit, Mapping):
                metadata["usage_limit"] = _bounded_usage_limit_metadata(usage_limit)
        except Exception:
            pass  # Optional cached notice detail cannot affect capture or send certainty.
        try:
            snapshot = getattr(submission, "failure_transport_snapshot", None)
            if callable(snapshot):
                transport = snapshot()
                if transport is not None:
                    metadata["transport"] = transport
        except Exception:
            pass  # Transport metadata is optional; keep the original DOM capture.
        html = None
        try:
            locator = page.locator("html")
            sample = await _await_call(
                locator.evaluate, HTML_SAMPLE,
                timeout=max(1, int(budget.remaining() * 1000)), _deadline=budget,
            )
            if not isinstance(sample, dict) or not isinstance(sample.get("html"), str):
                raise TypeError("invalid failure HTML sample")
            html = sample["html"]
            metadata["page"] = {key: sample.get(key) for key in ("url", "timeOrigin")}
            metadata["html_status"] = "complete"
            transcript_snapshot = getattr(submission, "failure_transcript_snapshot", None)
            if callable(transcript_snapshot):
                try:
                    if await _await_call(assert_owned, until, _deadline=budget) is not True:
                        raise IdentityMismatchError("failure transcript ownership unavailable")
                    transcript = await _await_call(
                        transcript_snapshot, deadline=until, _deadline=budget,
                    )
                    metadata["transcript"] = _bounded_failure_transcript(transcript)
                    if durable is not None:
                        metadata["transcript"]["matches_admitted_prompt"] = (
                            metadata["transcript"]["rendered_user_sha256"] == durable.prompt_sha256
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as read_error:
                    metadata["transcript"] = {
                        "status": "unavailable",
                        "failure_context": _safe_failure_context(read_error, phase="failure_transcript"),
                    }
            checked = await _await_call(
                locator.evaluate,
                "element => ({url: location.href, timeOrigin: performance.timeOrigin})",
                timeout=max(1, int(budget.remaining() * 1000)), _deadline=budget,
            )
            if isinstance(checked, dict):
                metadata["checked_page"] = {key: checked.get(key) for key in ("url", "timeOrigin")}
                if all(
                    isinstance(value.get("url"), str) and value["url"]
                    and isinstance(value.get("timeOrigin"), (int, float))
                    and not isinstance(value["timeOrigin"], bool)
                    and math.isfinite(value["timeOrigin"])
                    for value in (metadata["page"], metadata["checked_page"])
                ):
                    metadata["coherence"] = (
                        "same_document" if metadata["page"] == metadata["checked_page"] else "changed"
                    )
        except asyncio.CancelledError:
            return
        except Exception as capture_error:
            metadata["capture_error"] = _safe_failure_context(capture_error, phase="failure_capture")
        if "transcript" in metadata:
            # HTML and rendered transcript are successive bounded reads,
            # never a claim that the DOM stayed unchanged between them.
            metadata["transcript"]["document_coherence"] = metadata["coherence"]
        try:
            self._assert_execution()
            references = {}
            if html is not None:
                references["html"] = self.repository.write_evidence(
                    run_id, html, media_type="text/html; charset=utf-8", suffix=".failure.html",
                )
            references["metadata"] = self.repository.write_evidence(
                run_id, json.dumps(metadata, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n",
                media_type="application/json", suffix=".failure.json",
            )
            if fence:
                self._send_failure_evidence.setdefault(fence.fence_id, []).append(references)
            return references
        except Exception:
            pass  # Diagnostics never replace the failure or interfere with cleanup.

    def _transport_counts(
        self,
        run_id: str,
        observation_ids: tuple[str, ...],
        *,
        packet_kind: str,
        conditional_split: bool,
    ) -> dict[str, dict[str, int]]:
        grouped: dict[str, list[SendAttempt]] = {}
        fences = self._send_attempts(run_id)
        for record in fences.snapshot(observation_ids=observation_ids).records:
            if packet_kind == "gaio_extraction" and record.action != "gaio_gemini_extractor":
                continue
            if packet_kind == "measurement" and conditional_split and record.action == "gaio_gemini_extractor":
                continue
            grouped.setdefault(record.action, []).append(record)
        return {action: count_attempt_milestones(records) for action, records in grouped.items()}



def _retry_policy(plan: Mapping[str, Any]) -> RetryPolicy:
    """Read only the hash-bound policy frozen into the persisted run plan."""

    value = plan.get("retry_policy")
    if value is None:
        # Pre-retry plans must never acquire retry authority from a newer
        # RuntimeConfig during inspect/resume.
        return RetryPolicy.disabled()
    if not isinstance(value, Mapping):
        raise IntegrityError("frozen retry_policy must be an object")
    return RetryPolicy.from_mapping(value)


def _response_baseline_mapping(
    submission: Submission,
) -> Optional[dict[str, Any]]:
    value = getattr(submission, "response_baseline", None)
    if value is None:
        # Compatibility for old fake websites and already-frozen test plans.
        # A real timed-out send without this proof still fails closed during
        # reconciliation.
        return None
    to_mapping = getattr(value, "to_mapping", None)
    if callable(to_mapping):
        mapped = to_mapping()
    elif isinstance(value, Mapping):
        mapped = value
    else:
        raise IntegrityError(
            "provider response_baseline is not serializable"
        )
    if not isinstance(mapped, Mapping):
        raise IntegrityError(
            "provider response_baseline must serialize as an object"
        )
    return {str(key): item for key, item in mapped.items()}


def _provider_response_evidence(
    answer: ProviderAnswer,
    *,
    action: str,
    expected_prompt_sha256: str,
) -> Optional[dict[str, Any]]:
    value = answer.capture_evidence
    if not isinstance(value, Mapping):
        raise IntegrityError("provider capture_evidence must be an object")
    evidence = {str(key): item for key, item in value.items()}
    if action != "google_search":
        return evidence or None
    if answer.status is ProviderStatus.NOT_SHOWN:
        expected_state = "not_shown"
        expected_response_sha256 = None
    elif answer.status is ProviderStatus.COLLECTED:
        if (
            answer.capture_contract_version
            != GAIO_GENERATED_ANSWER_CAPTURE_VERSION
        ):
            raise IntegrityError(
                "Google COLLECTED lacks the generated-answer contract"
            )
        expected_state = "shown"
        expected_response_sha256 = hashlib.sha256(
            answer.text.encode("utf-8")
        ).hexdigest()
    else:
        raise IntegrityError(
            "Google search returned an unsupported terminal status"
        )
    try:
        return canonical_gaio_terminal_evidence(
            evidence,
            expected_state=expected_state,
            expected_prompt_sha256=expected_prompt_sha256,
            expected_identity=answer.identity.to_mapping(),
            expected_response_sha256=expected_response_sha256,
        )
    except ValueError as exc:
        raise IntegrityError(
            f"Google terminal evidence failed validation: {exc}"
        ) from exc


def _logical_attempts(
    fences: SendAttemptStore | AttemptSnapshot,
    logical_turn_id: str,
) -> tuple[SendAttempt, ...]:
    return fences.attempts_for(logical_turn_id)


def _attempt_ordinal(record: SendAttempt) -> int:
    """Compatibility wrapper for the storage-normalized legacy identity."""

    return record.normalized_attempt_ordinal


def _observation_attempt_ordinal(record: SendAttempt) -> int:
    return record.observation_attempt_ordinal or 1


def _send_attempt_id(logical_turn_id: str, ordinal: int) -> str:
    if ordinal <= 0:
        raise ValueError("attempt ordinal must be positive")
    return (
        logical_turn_id
        if ordinal == 1
        else f"{logical_turn_id}:attempt-{ordinal}"
    )


def _next_attempt_ordinal(
    attempts: tuple[SendAttempt, ...],
) -> int:
    return max((_attempt_ordinal(value) for value in attempts), default=0) + 1


def _latest_observation_attempt(
    fences: SendAttemptStore | AttemptSnapshot,
    observation_id: str,
) -> int:
    snapshot = (
        fences.snapshot(observation_ids=(observation_id,))
        if isinstance(fences, SendAttemptStore)
        else fences
    )
    return max(
        (
            _observation_attempt_ordinal(record)
            for record in snapshot.for_observation(observation_id)
            if record.action in {"measurement_turn_1", "same_chat_turn_2"}
        ),
        default=1,
    )


def _latest_for_observation_attempt(
    attempts: tuple[SendAttempt, ...],
    observation_attempt_ordinal: int,
) -> Optional[SendAttempt]:
    values = tuple(
        record
        for record in attempts
        if _observation_attempt_ordinal(record)
        == observation_attempt_ordinal
    )
    return values[-1] if values else None


def _accepted_for_observation_attempt(
    attempts: tuple[SendAttempt, ...],
    observation_attempt_ordinal: int,
) -> Optional[SendAttempt]:
    values = tuple(
        record
        for record in attempts
        if (
            _observation_attempt_ordinal(record)
            == observation_attempt_ordinal
            and record.state
            in {SendAttemptState.ACKNOWLEDGED, SendAttemptState.REJECTED}
            and record.response_reference is not None
        )
    )
    return values[-1] if values else None


def _record_authorizes_retry(record: SendAttempt) -> bool:
    return attempt_allows_retry(record)


def _reconciled_identity(record: SendAttempt) -> Mapping[str, str]:
    return (
        record.reconciliation_identity
        or record.ack_identity
        or record.identity
    )


def _can_restart_conversation(
    turn_1_attempts: tuple[SendAttempt, ...],
    turn_2_attempts: tuple[SendAttempt, ...],
    policy: RetryPolicy,
) -> bool:
    return bool(
        policy.enabled
        and _next_attempt_ordinal(turn_1_attempts)
        <= policy.authorized_max_attempts
        and _next_attempt_ordinal(turn_2_attempts)
        <= policy.authorized_max_attempts
    )


def _in_run_refetch_allowed(policy: RetryPolicy, tries: int) -> bool:
    """One prompt measurement may be fetched at most ``max_attempts`` times per run.

    This bounds refetches after confirmed pre-send failures, which consume no
    website send and therefore leave the frozen submission limit untouched.
    """

    return bool(policy.enabled and tries < policy.authorized_max_attempts)


async def _retry_backoff(
    policy: RetryPolicy,
    *,
    cancelled: Callable[[], bool],
) -> None:
    if cancelled():
        raise BrowserInteractionError(
            "module cancellation interrupted retry backoff"
        )
    if policy.backoff_seconds <= 0:
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + policy.backoff_seconds
    while True:
        if cancelled():
            raise BrowserInteractionError(
                "module cancellation interrupted retry backoff"
            )
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.1, remaining))


async def _prepare_native_retry(submission: Submission) -> bool:
    prepare = getattr(submission, "prepare_native_retry", None)
    if not callable(prepare):
        return False
    try:
        value = prepare()
        if asyncio.iscoroutine(value):
            value = await value
        return value is True
    except Exception as exc:
        raise AmbiguousSendError(
            "native retry preparation could not be proved safely"
        ) from exc


async def _restore_and_prepare_native_retry(
    submission: Submission,
    record: SendAttempt,
    prompt: str,
    *,
    binding: Optional[Mapping[str, object]] = None,
) -> bool:
    restore = getattr(submission, "restore_confirmed_attempt", None)
    if (
        not callable(restore)
        or record.response_baseline is None
        or not record.failure_classification
    ):
        return False
    try:
        options = {"binding": binding} if binding is not None else {}
        value = restore(
            prompt=prompt,
            response_baseline=record.response_baseline,
            failure_classification=record.failure_classification,
            native_retry=bool(record.native_retry),
            **options,
        )
        if asyncio.iscoroutine(value):
            await value
    except Exception:
        return False
    return await _prepare_native_retry(submission)


def _use_same_chat_extraction_timeout(
    submission: Submission,
    policy: RetryPolicy,
    platform: str,
) -> None:
    use_timeout = getattr(submission, "use_extraction_timeout", None)
    if not callable(use_timeout):
        return
    if platform == "chatgpt":
        seconds = policy.response_timeouts.chatgpt_turn_2_seconds
    elif platform == "gemini":
        seconds = policy.response_timeouts.gemini_turn_2_seconds
    elif platform == "copilot":
        seconds = COPILOT_BROWSER_TURN_TIMEOUT_SECONDS
    else:
        raise IntegrityError(
            f"unsupported same-chat extraction platform: {platform}"
        )
    try:
        use_timeout(seconds)
    except TypeError:
        # Compatibility for pre-policy fakes; real PageSubmission accepts the
        # frozen website-specific override.
        use_timeout()


def _attempt_result_metadata(
    fences: SendAttemptStore,
    observation_id: str,
    *,
    accepted_attempt: Optional[SendAttempt],
    accepted_attempts: Mapping[str, str],
) -> dict[str, Any]:
    attempts = tuple(
        sorted(
            (
                record
                for record in fences.snapshot(observation_ids=(observation_id,)).records
            ),
            key=lambda record: (
                record.prepared_at,
                record.action,
                _attempt_ordinal(record),
                record.fence_id,
            ),
        )
    )
    return {
        "accepted_attempt_id": (
            accepted_attempt.fence_id
            if accepted_attempt is not None
            else None
        ),
        "accepted_attempt_ids": {
            str(key): str(value)
            for key, value in accepted_attempts.items()
        },
        "attempt_ids": [record.fence_id for record in attempts],
        "attempts": [
            {
                "attempt_id": record.fence_id,
                "logical_turn_id": record.normalized_logical_turn_id,
                "attempt_ordinal": _attempt_ordinal(record),
                "observation_attempt_ordinal": (
                    _observation_attempt_ordinal(record)
                ),
                "action": record.action,
                "state": record.state.value,
                "response_reference": record.response_reference,
                "response_evidence": record.response_evidence,
                "failure_classification": (
                    record.failure_classification
                ),
                "reconciliation_evidence": (
                    record.reconciliation_evidence
                ),
                "retry_decision": record.retry_decision,
                "retried_from": record.retried_from,
                "retried_by": record.retried_by,
                "native_retry": record.native_retry,
            }
            for record in attempts
        ],
    }


def _failure_issue(
    observation_id: str,
    record: SendAttempt,
) -> dict[str, object]:
    exhausted = record.retry_decision == "retry_exhausted"
    return _issue(
        observation_id,
        "retry_exhausted" if exhausted else "provider_no_response",
        (
            "automatic retry attempts were exhausted after a positively "
            "confirmed provider no-response failure"
            if exhausted
            else (
                "provider ended without a response and the terminal state "
                "does not authorize another send"
            )
        ),
        recoverable=False,
    )


def _turn_1_no_response_result(
    observation_id: str,
    platform: str,
    repository: MeasurementFiles,
    run_id: str,
    fences: SendAttemptStore,
    failed_turn: SendAttempt,
) -> dict[str, Any]:
    prior_measurements = tuple(
        record
        for record in fences.snapshot(observation_ids=(observation_id,)).records
        if record.action == "measurement_turn_1"
        and record.fence_id != failed_turn.fence_id
        and record.response_reference is not None
        and record.state in {
            SendAttemptState.ACKNOWLEDGED,
            SendAttemptState.REJECTED,
        }
    )
    accepted = (
        max(
            prior_measurements,
            key=lambda record: (
                _observation_attempt_ordinal(record),
                _attempt_ordinal(record),
                record.acknowledged_at or "",
                record.fence_id,
            ),
        )
        if prior_measurements
        else None
    )
    artifacts: dict[str, Any] = {}
    accepted_attempts: dict[str, str] = {}
    if accepted is not None:
        _, reference = _read_attempt_response(
            repository,
            run_id,
            accepted,
        )
        artifacts["turn_1_response"] = reference
        accepted_attempts["measurement_turn_1"] = accepted.fence_id
    return {
        "observation_id": observation_id,
        "platform": platform,
        "status": "partial" if accepted is not None else "failed",
        "collection_status": (
            "collected" if accepted is not None else "failed"
        ),
        "extraction_status": (
            "failed" if accepted is not None else "not_started"
        ),
        "grounded_occurrences": [],
        "artifacts": artifacts,
        "issues": [_failure_issue(observation_id, failed_turn)],
        **_attempt_result_metadata(
            fences,
            observation_id,
            accepted_attempt=accepted,
            accepted_attempts=accepted_attempts,
        ),
    }


def _conversational_no_response_result(
    observation_id: str,
    platform: str,
    repository: MeasurementFiles,
    run_id: str,
    fences: SendAttemptStore,
    *,
    accepted_turn_1: SendAttempt,
    failed_turn: Optional[SendAttempt],
) -> dict[str, Any]:
    if failed_turn is None:
        raise IntegrityError("missing failed same-chat extraction attempt")
    _, answer_reference = _read_attempt_response(
        repository,
        run_id,
        accepted_turn_1,
    )
    return {
        "observation_id": observation_id,
        "platform": platform,
        "status": "partial",
        "collection_status": "collected",
        "extraction_status": "failed",
        "grounded_occurrences": [],
        "artifacts": {
            "turn_1_response": answer_reference,
        },
        "issues": [_failure_issue(observation_id, failed_turn)],
        **_attempt_result_metadata(
            fences,
            observation_id,
            accepted_attempt=accepted_turn_1,
            accepted_attempts={
                "measurement_turn_1": accepted_turn_1.fence_id,
            },
        ),
    }


def _profile_identity(
    plan: Mapping[str, Any],
    packet: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> tuple[str, str]:
    profile_id = str(
        observation.get("profile_id")
        or packet.get("profile_id")
        or ""
    )
    chrome_directory = str(
        observation.get("chrome_directory")
        or packet.get("chrome_directory")
        or ""
    )
    profiles = plan.get("selected_profiles")
    if profiles is None:
        # Compatibility for early package-owned/legacy worker requests that
        # used the shorter key before RunPlan froze exact selected identities.
        profiles = plan.get("profiles")
    for row in profiles or ():
        if isinstance(row, Mapping) and str(row.get("id") or "") == profile_id:
            chrome_directory = chrome_directory or str(
                row.get("chrome_directory") or ""
            )
    if not profile_id or not chrome_directory:
        raise IntegrityError(
            "the frozen observation is missing exact profile ID/directory identity"
        )
    return profile_id, chrome_directory


def _unresolved_website_attempts(
    fences: SendAttemptStore,
    plan: Mapping[str, Any],
    packet: Mapping[str, Any],
    observations: Mapping[str, Mapping[str, Any]],
    *,
    in_flight: Collection[str] = (),
) -> tuple[SendAttempt, ...]:
    """Return unresolved irreversible actions on every profile and website pair this measurement group owns.

    ``in_flight`` names prompt measurements whose actions this process is driving
    right now on other owned tabs of the same pair; their SEND_STARTED and
    RESPONSE_CAPTURED send attempts are live, not abandoned, and are skipped.  Once
    such an prompt measurement finishes it leaves ``in_flight``, so any send attempt it
    left unresolved blocks the pair exactly as before.
    """

    profile_id = str(packet.get("profile_id") or "")
    providers = _measurement_group_websites(plan, packet)
    unresolved: list[SendAttempt] = []
    snapshot = fences.unresolved_snapshot(
        profile_id=profile_id,
        platforms=providers,
        observation_platforms={
            observation_id: str(row.get("platform") or "")
            for observation_id, row in observations.items()
        },
    )
    for record in snapshot.records:
        if record.state not in {
            SendAttemptState.SEND_STARTED,
            SendAttemptState.RESPONSE_CAPTURED,
        }:
            continue
        if str(record.identity.get("profile_id") or "") != profile_id:
            continue
        if record.observation_id in in_flight:
            continue
        if record.action == "google_search":
            record_provider = "google_ai_overview"
        elif record.action == "gaio_gemini_extractor":
            record_provider = "gemini"
        else:
            observation = observations.get(record.observation_id) or {}
            record_provider = str(observation.get("platform") or "")
        if record_provider in providers:
            unresolved.append(record)
    return tuple(unresolved)


def _unresolved_google_search_attempts(
    fences: SendAttemptStore,
    packet: Mapping[str, Any],
) -> tuple[SendAttempt, ...]:
    """Return blocked Google prerequisites for one split name listing measurement group."""

    profile_id = str(packet.get("profile_id") or "")
    observation_ids = {
        str(value)
        for value in packet.get("observation_ids") or ()
    }
    unresolved = [
        record
        for record in fences.unresolved_snapshot(
            observation_ids=observation_ids, profile_id=profile_id,
            platforms=("google_ai_overview",), observation_platforms={},
        ).records
        if (
            record.action == "google_search"
            and record.observation_id in observation_ids
            and str(record.identity.get("profile_id") or "") == profile_id
            and record.state in {
                SendAttemptState.SEND_STARTED,
                SendAttemptState.RESPONSE_CAPTURED,
            }
        )
    ]
    return tuple(sorted(unresolved, key=lambda record: record.fence_id))


def _measurement_group_websites(
    plan: Mapping[str, Any],
    packet: Mapping[str, Any],
) -> frozenset[str]:
    packet_kind = str(packet.get("kind") or "measurement")
    if packet_kind == "gaio_extraction":
        return frozenset({"gemini"})
    provider = str(packet.get("platform") or "")
    providers = {provider}
    if provider == "google_ai_overview" and any(
        not _has_conditional_gaio_packet(plan, str(observation_id))
        for observation_id in packet.get("observation_ids") or ()
    ):
        # The supported legacy inline measurement group owns both its Google source and
        # fresh Gemini parser profile and website pairs for the duration of the measurement group.
        providers.add("gemini")
    return frozenset(providers)


def _provider_access_error(
    error: BaseException,
) -> Optional[ProviderAccessBlockedError]:
    """Recognize typed access failures without inspecting exception text."""

    current: Optional[BaseException] = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, ProviderAccessBlockedError):
            return current
        seen.add(id(current))
        current = current.__cause__
    return None


def _bounded_usage_limit_metadata(value: Mapping[str, Any]) -> dict[str, object]:
    """Copy only cached notice fields into private failure evidence."""
    result: dict[str, object] = {}
    for key in ("scope", "reset_day", "reset_clock", "reset_display", "reset_timezone"):
        field = value.get(key)
        if field is None or isinstance(field, str) and len(field) <= 100:
            result[key] = field
    for key in ("text_allowed", "blocking"):
        field = value.get(key)
        if field is None or type(field) is bool:
            result[key] = field
    notice = value.get("notice_text")
    if isinstance(notice, str):
        result["notice_text"] = notice[:2000]
    return result


def _bounded_failure_transcript(snapshot: object) -> dict[str, object]:
    """Describe the existing rendered reader's result in private diagnostics."""
    history = snapshot.history
    prompt = snapshot.prompt_text
    answer = snapshot.answer
    result = {
        "status": "captured",
        "rendered_user_text": prompt[:8192],
        "rendered_user_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "rendered_user_character_count": len(prompt),
        "rendered_user_truncated": len(prompt) > 8192,
        "logical_user_count": len(history.get("user_sha256", ())),
        "logical_assistant_count": answer.count,
        "answer_readability": "nonempty" if answer.text else "empty",
        "latest_answer_sha256": hashlib.sha256(answer.text.encode("utf-8")).hexdigest(),
        "history_complete": history.get("history_complete") is True,
        "history_ambiguous": history.get("history_ambiguous") is True,
        "generating": history.get("generating") is True,
    }
    for role in ("user", "assistant"):
        identities = history.get(role + "_ids", ())
        identifier = identities[-1] if identities else None
        result["latest_" + role + "_id"] = identifier[:200] if isinstance(identifier, str) else None
    branch = history.get("branch_id")
    result["branch_id"] = branch[:200] if isinstance(branch, str) else None
    return result


def _reconciled_access_error(
    classification: Optional[str],
    evidence: Optional[Mapping[str, Any]] = None,
) -> Optional[ProviderAccessBlockedError]:
    """Recognize only the websites' explicit nonretryable access category."""

    if classification in {
        "google_access_blocked",
        "provider_access_blocked:human_verification_required",
        "provider_access_blocked:verify you are human",
    }:
        return HumanVerificationRequiredError(
            "provider human verification requires operator action"
        )
    if isinstance(classification, str) and (
        classification == "provider_access_blocked"
        or classification.startswith("provider_access_blocked:")
    ):
        usage_limit = None
        if classification == "provider_access_blocked:usage_limit" and isinstance(evidence, Mapping):
            checks = evidence.get("checks")
            if isinstance(checks, (tuple, list)):
                usage_limit = next((dict(check["usage_limit"]) for check in reversed(checks)
                    if isinstance(check, Mapping) and isinstance(check.get("usage_limit"), Mapping)
                    and project_blocking_usage_limit(check["usage_limit"]) is not None), None)
        reason = classification.partition(":")[2]
        if reason in {"", "authentication_redirect"}:
            reason = "authentication_required"
        return ProviderAccessBlockedError(
            "provider access requires operator action",
            reason=reason,
            usage_limit=usage_limit,
        )
    return None


def _access_block_detail(
    error: ProviderAccessBlockedError,
    *,
    profile_id: str,
    provider: str,
) -> dict[str, object]:
    human = isinstance(error, HumanVerificationRequiredError)
    reason = (
        "human_verification_required" if human
        else error.reason or "provider_access_blocked"
    )
    detail = {
        "user_action_required": True,
        "access_block": {
            "profile_id": profile_id,
            "provider": provider,
            "reason": reason,
            "exception_type": (
                "HumanVerificationRequiredError"
                if human
                else "ProviderAccessBlockedError"
            ),
        },
    }
    if reason == "usage_limit":
        limit = project_blocking_usage_limit(getattr(error, "usage_limit", None))
        if limit is not None:
            detail["access_block"]["usage_limit"] = limit
    return detail


def _website_access_issue(
    observation_id: str,
    detail: Mapping[str, object],
) -> dict[str, object]:
    return _issue(
        observation_id,
        "needs_attention",
        "provider access requires user action; the profile/provider lane was stopped",
        recoverable=False,
        detail=dict(detail),
    )


def _recorded_website_access_issue(
    repository: MeasurementFiles,
    run_id: str,
    plan: Mapping[str, Any],
    packet: Mapping[str, Any],
) -> Optional[dict[str, object]]:
    """Honor a proven access stop across later measurement groups on the same profile and website pair."""

    profile_id = str(packet.get("profile_id") or "")
    providers = _measurement_group_websites(plan, packet)
    for candidate in plan.get("packets") or ():
        if (
            not isinstance(candidate, Mapping)
            or str(candidate.get("profile_id") or "") != profile_id
        ):
            continue
        state = repository.read_group_progress(
            run_id,
            str(candidate["packet_id"]),
        )
        for issue in state.get("issues") or ():
            if not isinstance(issue, Mapping) or issue.get("code") not in {
                "needs_attention", "ambiguous_send"
            }:
                continue
            detail = issue.get("detail")
            if (
                not isinstance(detail, Mapping)
                or detail.get("user_action_required") is not True
            ):
                continue
            block = detail.get("access_block")
            if (
                not isinstance(block, Mapping)
                or block.get("profile_id") != profile_id
                or block.get("provider") not in providers
                or not isinstance(block.get("reason"), str)
                or not block["reason"]
            ):
                continue
            if block["reason"] == "usage_limit" and isinstance(block.get("usage_limit"), Mapping) and (
                block["usage_limit"].get("scope") == "tools" or block["usage_limit"].get("text_allowed") is True or block["usage_limit"].get("blocking") is False
            ):
                continue
            error_type = (
                HumanVerificationRequiredError
                if block["reason"] == "human_verification_required"
                else ProviderAccessBlockedError
            )
            safe_detail = _access_block_detail(
                error_type(reason=str(block["reason"]), usage_limit=block.get("usage_limit") if isinstance(block.get("usage_limit"), Mapping) else None),
                profile_id=profile_id,
                provider=str(block["provider"]),
            )
            safe_detail["source_packet_id"] = str(candidate["packet_id"])
            return _website_access_issue(
                str(issue.get("observation_id") or ""),
                safe_detail,
            )
    return None


def _pre_send_failure_detail(
    kind: str,
    error: BaseException,
) -> dict[str, object]:
    access_error = _provider_access_error(error)
    if access_error is not None:
        human = isinstance(access_error, HumanVerificationRequiredError)
        return {
            "kind": kind,
            "phase": "pre_send",
            "exception_type": (
                "HumanVerificationRequiredError"
                if human
                else "ProviderAccessBlockedError"
            ),
            "user_action_required": True,
            "detail": "provider access requires user action before any send",
        }
    return {
        "kind": kind,
        "phase": "pre_send",
        "exception_type": type(error).__name__,
        "detail": str(error) or repr(error),
    }


def _safe_failure_context(
    error: BaseException,
    *,
    phase: str,
) -> dict[str, str]:
    """Retain causal type and source location without messages, args or locals."""

    def error_type(value: BaseException) -> str:
        name = type(value).__name__
        return (
            name
            if name.isascii() and name.isidentifier() and len(name) <= 80
            else "UnclassifiedError"
        )

    context = {"phase": phase, "exception_type": error_type(error)}
    if error.__cause__ is not None:
        context["cause_type"] = error_type(error.__cause__)
    origin = error
    seen = {id(origin)}
    while origin.__cause__ is not None and id(origin.__cause__) not in seen:
        origin = origin.__cause__
        seen.add(id(origin))
    traceback = origin.__traceback__
    if traceback is not None:
        while traceback.tb_next is not None:
            traceback = traceback.tb_next
        frame = traceback.tb_frame
        locations = {
            "origin_file": Path(frame.f_code.co_filename).name,
            "origin_module": frame.f_globals.get("__name__"),
            "origin_function": frame.f_code.co_name,
        }
        for key, value in locations.items():
            if (isinstance(value, str) and value.isascii() and len(value) <= 160
                    and all(part.isidentifier() for part in value.split("."))):
                context[key] = value
        context["origin_line"] = str(traceback.tb_lineno)
    return context


def _uncertain_website_send_issue(
    record: SendAttempt,
    *,
    failure_context: Optional[Mapping[str, str]] = None,
    failure_evidence: Optional[list[dict[str, Any]]] = None,
) -> dict[str, object]:
    detail: dict[str, object] = {
        "fence_id": record.fence_id,
        "failure_classification": record.failure_classification,
        "reconciliation_evidence": record.reconciliation_evidence,
    }
    if failure_context is not None:
        detail["failure_context"] = dict(failure_context)
    if failure_evidence:
        detail["failure_evidence"] = list(failure_evidence)
    return _issue(
        record.observation_id,
        "ambiguous_send",
        (
            f"{record.fence_id} has an unresolved irreversible action; "
            "the profile/provider lane was fail-stopped"
        ),
        recoverable=False,
        detail=detail,
    )


def _ambiguous_gaio_source_issue(
    records: tuple[SendAttempt, ...],
) -> dict[str, object]:
    first = records[0]
    fence_ids = ", ".join(record.fence_id for record in records)
    return _issue(
        first.observation_id,
        "ambiguous_send",
        (
            "conditional GAIO extraction was skipped because its Google "
            f"source lane has unresolved irreversible action(s): {fence_ids}"
        ),
        recoverable=False,
        detail={
            "source_fences": [
                {
                    "fence_id": record.fence_id,
                    "failure_classification": (
                        record.failure_classification
                    ),
                    "reconciliation_evidence": (
                        record.reconciliation_evidence
                    ),
                }
                for record in records
            ],
        },
    )


def _has_conditional_gaio_packet(
    plan: Mapping[str, Any],
    observation_id: str,
) -> bool:
    for packet in plan.get("packets") or ():
        if not isinstance(packet, Mapping):
            continue
        if str(packet.get("kind") or "measurement") != "gaio_extraction":
            continue
        if observation_id in {
            str(value) for value in packet.get("observation_ids") or ()
        }:
            return True
    return False


def _read_attempt_response(
    repository: MeasurementFiles,
    run_id: str,
    fence: SendAttempt,
) -> tuple[str, dict[str, Any]]:
    if not fence.response_reference or not fence.response_sha256:
        raise IntegrityError(f"{fence.fence_id} has incomplete response evidence")
    path = repository.run_dir(run_id) / fence.response_reference
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise IntegrityError(f"missing response evidence: {path}") from exc
    digest = hashlib.sha256(payload).hexdigest()
    if digest != fence.response_sha256:
        raise IntegrityError(f"response evidence hash mismatch: {path}")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntegrityError(f"response evidence is not UTF-8: {path}") from exc
    return text, {
        "path": fence.response_reference,
        "sha256": digest,
        "byte_length": len(payload),
        "media_type": (
            "application/json"
            if fence.response_reference.endswith(".json")
            else "text/plain; charset=utf-8"
        ),
    }


def _conversational_result(
    observation_id: str,
    platform: str,
    answer: str,
    answer_reference: Mapping[str, Any],
    extraction: str,
    extraction_reference: Mapping[str, Any],
    fence: SendAttempt,
    *,
    fences: SendAttemptStore,
    accepted_turn_1: SendAttempt,
    observation: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    rejected = fence.state is SendAttemptState.REJECTED
    metadata = _attempt_result_metadata(
        fences,
        observation_id,
        accepted_attempt=accepted_turn_1 if rejected else fence,
        accepted_attempts=(
            {"measurement_turn_1": accepted_turn_1.fence_id}
            if rejected
            else {
                "measurement_turn_1": accepted_turn_1.fence_id,
                "same_chat_turn_2": fence.fence_id,
            }
        ),
    )
    if fence.state is SendAttemptState.REJECTED:
        issue = _issue(
            observation_id,
            "extraction_rejected",
            str(fence.detail or "strict extraction validation rejected the response"),
            recoverable=False,
        )
        return {
            "observation_id": observation_id,
            "platform": platform,
            "status": "partial",
            "collection_status": "collected",
            "extraction_status": "rejected",
            "grounded_occurrences": [],
            "artifacts": {
                "turn_1_response": dict(answer_reference),
                "turn_2_response": dict(extraction_reference),
            },
            "issues": [issue],
            **metadata,
        }
    grounded = _validate_chat_extraction(observation or {}, answer, extraction)
    return {
        "observation_id": observation_id,
        "platform": platform,
        "status": "succeeded",
        "collection_status": "collected",
        "extraction_status": "complete",
        "grounded_occurrences": grounded,
        "artifacts": {
            "turn_1_response": dict(answer_reference),
            "turn_2_response": dict(extraction_reference),
        },
        "issues": [],
        **_client_recommendation_fields(observation or {}, answer, extraction),
        **metadata,
    }


def _validate_chat_extraction(observation, answer, extraction):
    if observation.get("client_research_capability") == "client_research.recommendations.v1":
        from ..client_research import validate_recommendation_extraction
        value = validate_recommendation_extraction(extraction, answer)
        return find_name_positions(answer, [row["name"] for row in value["recommendations"]])
    return find_name_positions(answer, parse_closed_name_array(extraction))


def _client_recommendation_fields(observation, answer, extraction, plan=None):
    if observation.get("client_research_capability") != "client_research.recommendations.v1":
        return {}
    from ..client_research import validate_recommendation_extraction
    config = ((plan or {}).get("scope_manifest") or {}).get("client_research") or observation.get("client_research_context") or {}
    return {"client_research":{
        **{key:config[key] for key in ("workspace_id","directory_revision","config_revision") if key in config},
        **{key:observation[key] for key in ("cell_id","area_id","treatment_id","query_fingerprint","profile_id")},
        "capability":observation["client_research_capability"],"source_prompt_sha256":_text_sha(observation["prompt_text"]),
        "source_answer_sha256":_text_sha(answer),"extraction":validate_recommendation_extraction(extraction, answer)}}


def _issue(
    observation_id: str,
    code: str,
    message: str,
    *,
    recoverable: bool,
    detail: Optional[Mapping[str, object]] = None,
) -> dict[str, object]:
    issue: dict[str, object] = {
        "observation_id": observation_id,
        "code": code,
        "message": message,
        "recoverable": recoverable,
    }
    if detail is not None:
        issue["detail"] = dict(detail)
    return issue


def _require_resume(resume: bool, fence_id: str) -> None:
    if not resume:
        raise StateConflictError(
            f"{fence_id} already exists; run_packet() never implies resume"
        )


def _text_sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _blank_gemini_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.hostname in {"gemini.google.com", "www.gemini.google.com"}
        and parsed.path.rstrip("/") == "/app"
        and not parsed.query
        and not parsed.fragment
    )


__all__ = ["MeasurementGroupWorker", "MeasurementGroupOutcome"]
