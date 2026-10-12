"""Prepare messages, send them and read responses on supported websites."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import random
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import urlsplit

from ..browser.input import (
    ComposerPreSendFailure,
    read_copilot_composer_value,
    read_typed_composer_value,
    write_exact_composer_input,
)
from ..browser.shared_actions import SharedBrowserActions
from ..browser.profile_matching import browser_target_id
from ..browser.tabs import retire_closed_owned_page
from ..errors import (
    BrowserInteractionError,
    HumanVerificationRequiredError,
    IdentityMismatchError,
    ProviderAccessBlockedError,
)
from .conversation_binding import (
    ConversationCorrespondenceError,
    canonical_conversation_url,
    is_temporary_message_id,
    logical_response_baseline,
    verify_conversation_history,
)
from .usage_limits import parse_chatgpt_usage_limit_notice

from ..browser.operations import (
    _BrowserOperationDeadlineExceeded,
    _BrowserOperationDeadline,
    _await_call,
    _cancel_background_browser_operation,
    _consume_background_task_result,
    _abandon_background_browser_operations_for_loop,
    _PENDING_BROWSER_OPERATIONS,
)
from .contracts import (
    ProviderStatus,
    ResponseBaseline,
    ReconciliationDisposition,
    BrowserIdentity,
    ProviderAnswer,
    ReconciliationResult,
    Submission,
    ProviderDriver,
    TransportCounter,
    ProviderRegistry,
    PageProviderSpec,
    VisibleAnswerSnapshot,
    _ResponseScope,
    PageFactory,
)
from .page_readers import (
    _conversation_history,
    _read_conversation,
    _remove_leading_dom_label,
    _without_rendered_answer_labels,
    _text_sha256,
    _matching_marker,
    _is_authentication_url,
    _access_block_error,
    _visible_access_block_marker,
    visible_items,
    _visible_items_from_locator_owner,
    visible_items_within_latest_response,
    visible_text_values,
    visible_text_values_within_latest_response,
    first_visible,
    locator_count,
    visible_answer_snapshot,
    _response_content_text,
    last_visible_text,
    any_visible,
    response_turn_is_generating,
    current_turn_notices,
    _is_native_playwright_timeout,
)


_OWNED_PAGE_IDENTITY_ATTRIBUTE = "_visibility_runner_owned_tab_identity"
_OWNED_PAGE_CONTEXT_ATTRIBUTE = "_visibility_runner_owned_tab_context"
_OWNED_TAB_REFERENCE_PREFIX = "visibility-runner-owned-tab:"


# Shared with the execution supervisor: these are separate provider phases,
# each with its own existing browser deadline.
PROVIDER_PREPARATION_TIMEOUT_SECONDS = 30.0
# Budget for preparing a message in a chat that already holds an answer
# (``same_chat_turn_2``) or a long source-bound prompt (the Google AI Overview
# parser message): reading the whole conversation back and the exact write
# take longer than a first message.  Passed by the caller as
# ``prepare_exact(..., timeout_seconds=...)``; the pre-send pacing pause
# (``interaction_pause``) runs before either budget starts.
PROVIDER_SECOND_MESSAGE_PREPARATION_TIMEOUT_SECONDS = 60.0
PROVIDER_PRE_SEND_IDENTITY_TIMEOUT_SECONDS = 20.0


def _canonical_reference(reference: str, spec: PageProviderSpec) -> str:
    """Ignore query/fragment changes only on registered conversation routes."""
    try:
        return canonical_conversation_url(reference, spec.conversation_url_prefixes)
    except ConversationCorrespondenceError:
        return reference


def _is_conversation_url(reference: str, spec: PageProviderSpec) -> bool:
    try:
        canonical_conversation_url(reference, spec.conversation_url_prefixes)
    except ConversationCorrespondenceError:
        return False
    return True


@dataclass(frozen=True)
class _ReconciliationProbe:
    evidence: Mapping[str, object]
    identity_valid: bool
    prompt_exact: bool
    answer_text: str
    new_response: bool
    busy: bool
    retryable_failure: Optional[str]
    nonretryable_failure: Optional[str]
    native_retry_count: int
    native_retry_control: Optional[object] = None
    website_failure: Optional[str] = None


def _current_turn_failure(notices: Iterable[str]) -> Optional[str]:
    """Classify website-owned turn notices independently of answer content."""

    labels = tuple(" ".join(value.split()).casefold() for value in notices)
    if any(
        (limit := parse_chatgpt_usage_limit_notice(label)) is not None
        and limit["blocking"] is True
        for label in labels
    ):
        return "provider_access_blocked:usage_limit"
    if any("unusual activity" in label and "try again later" in label for label in labels):
        return "provider_access_blocked:unusual_activity"
    if any(label.startswith("failed to fetch") for label in labels):
        return "provider_transport_failure"
    if any(label.startswith("connection interrupted") and "waiting for the complete answer" in label for label in labels):
        return "provider_connection_interrupted"
    return None


def _current_usage_limit(notices: Iterable[str]) -> Optional[dict[str, object]]:
    limits = [limit for notice in notices
        if (limit := parse_chatgpt_usage_limit_notice(notice)) is not None]
    return next((limit for limit in limits
        if limit["blocking"] is True),
        limits[0] if limits else None)


class _FailureTransportObservation:
    """Bounded, passive metadata from this page during one admitted send."""

    _LIMIT = 64
    _ERRORS = frozenset({
        "net::ERR_FAILED", "net::ERR_ABORTED", "net::ERR_CONNECTION_CLOSED",
        "net::ERR_CONNECTION_RESET", "net::ERR_CONNECTION_ABORTED",
        "net::ERR_CONNECTION_REFUSED", "net::ERR_CONNECTION_TIMED_OUT",
        "net::ERR_TIMED_OUT", "net::ERR_INTERNET_DISCONNECTED",
        "net::ERR_NETWORK_CHANGED", "net::ERR_NAME_NOT_RESOLVED",
        "net::ERR_ADDRESS_UNREACHABLE", "net::ERR_BLOCKED_BY_CLIENT",
        "net::ERR_BLOCKED_BY_RESPONSE", "net::ERR_HTTP_RESPONSE_CODE_FAILURE",
        "net::ERR_HTTP2_PROTOCOL_ERROR", "net::ERR_QUIC_PROTOCOL_ERROR",
        "net::ERR_CERT_AUTHORITY_INVALID", "net::ERR_SSL_PROTOCOL_ERROR",
        "net::ERR_EMPTY_RESPONSE", "net::ERR_CONTENT_LENGTH_MISMATCH",
        "net::ERR_INCOMPLETE_CHUNKED_ENCODING", "net::ERR_TUNNEL_CONNECTION_FAILED",
    })
    _METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})

    def __init__(self, page, provider_url):
        self.page = page
        self._origin = self._url_origin(provider_url)
        self._started = time.monotonic()
        self._events = deque(maxlen=self._LIMIT)
        self._requests = OrderedDict()
        self._ordinal = self._request_ordinal = 0
        self._listeners = []
        self._active = self.available = False

    @staticmethod
    def _url_origin(url):
        parsed = urlsplit(url)
        return parsed.scheme, parsed.hostname, parsed.port

    def _record(self, event, **metadata):
        if self._active:
            self._ordinal += 1
            self._events.append({
                "ordinal": self._ordinal,
                "elapsed_ms": round((time.monotonic() - self._started) * 1000),
                "event": event, **metadata,
            })

    def _request(self, request):
        if (request.frame is not self.page.main_frame
                or request.resource_type not in {"fetch", "xhr"}
                or self._url_origin(request.url) != self._origin):
            return None
        saved = self._requests.get(request)
        if saved is None:
            path = urlsplit(request.url).path.rstrip("/")
            endpoint = (
                "conversation" if path in {"/backend-api/conversation", "/backend-api/f/conversation"}
                else "conversation_prepare" if path == "/backend-api/conversation/prepare"
                else "provider_api" if path.startswith("/backend-api/")
                else "provider_fetch"
            )
            self._request_ordinal += 1
            saved = {
                "request_ordinal": self._request_ordinal, "endpoint": endpoint,
                "method": request.method if request.method in self._METHODS else "OTHER",
                "resource_type": request.resource_type,
            }
            self._requests[request] = saved
            if len(self._requests) > self._LIMIT:
                self._requests.popitem(last=False)
        return saved

    def start(self):
        on, remove = getattr(self.page, "on", None), getattr(self.page, "remove_listener", None)
        if not callable(on) or not callable(remove):
            return

        def network(event, value):
            try:
                if not self._active:
                    return
                request = value.request if event == "response" else value
                metadata = self._request(request)
                if metadata is None:
                    return
                detail = {}
                if event == "response":
                    status = value.status
                    if type(status) is int and 100 <= status <= 599:
                        detail["status"] = status
                elif event == "requestfailed":
                    failure = request.failure
                    detail["error"] = failure if isinstance(failure, str) and failure in self._ERRORS else "unavailable"
                self._record(event, **metadata, **detail)
                if event in {"requestfailed", "requestfinished"}:
                    self._requests.pop(request, None)
            except Exception:
                pass  # Optional diagnostics cannot affect an input or response.

        def navigated(frame):
            try:
                if frame is self.page.main_frame:
                    self._record("navigation", destination="provider" if self._url_origin(frame.url) == self._origin else "other")
            except Exception:
                pass

        self._active = True
        try:
            for event in ("request", "response", "requestfailed", "requestfinished"):
                callback = lambda value, event=event: network(event, value)
                self._listeners.append((event, callback))
                on(event, callback)
            for event, callback in (
                ("framenavigated", navigated),
                ("close", lambda *_: self._record("close")),
                ("crash", lambda *_: self._record("crash")),
            ):
                self._listeners.append((event, callback))
                on(event, callback)
            self.available = True
        except Exception:
            self.finish()

    def finish(self):
        self._active = False
        for event, callback in self._listeners:
            try:
                self.page.remove_listener(event, callback)
            except Exception:
                pass
        self._listeners.clear()
        self._requests.clear()

    def snapshot(self):
        return {
            "available": self.available, "events_seen": self._ordinal,
            "events": [dict(event) for event in self._events],
        }


class PageSubmission:
    """One fresh or restored page that accepts exactly one pending message."""

    def __init__(
        self,
        *,
        page: object,
        profile_id: str,
        chrome_directory: str,
        observation_id: str,
        spec: PageProviderSpec,
        identity: BrowserIdentity,
        composer_attempts: int = 3,
        extraction_timeout_seconds: Optional[float] = None,
        interaction_min_delay_seconds: float = 0.0,
        interaction_max_delay_seconds: float = 0.0,
        cancelled: Optional[Callable[[], bool]] = None,
        require_fresh_blank_start: bool = False,
        human_verification_handler: Optional[Callable] = None,
    ):
        self.page = page
        self.profile_id = profile_id
        self.chrome_directory = chrome_directory
        self.observation_id = observation_id
        self.spec = spec
        self._identity = identity
        self.composer_attempts = composer_attempts
        self.extraction_timeout_seconds = float(
            extraction_timeout_seconds
            if extraction_timeout_seconds is not None
            else spec.response_timeout_seconds
        )
        (
            self.interaction_min_delay_seconds,
            self.interaction_max_delay_seconds,
        ) = _delay_bounds(
            interaction_min_delay_seconds,
            interaction_max_delay_seconds,
        )
        self._current_response_timeout_seconds = (
            spec.response_timeout_seconds
        )
        self.cancelled = cancelled or (lambda: False)
        self.human_verification_handler = human_verification_handler
        self._previous_turn_binding = None
        self._verification_attempts = 0
        self._require_fresh_blank_start = bool(
            require_fresh_blank_start
        )
        self._composer: Optional[object] = None
        self._send_control: Optional[object] = None
        self._native_retry_control: Optional[object] = None
        self._prepared = False
        self._submitted = False
        self._native_retry_prepared = False
        self._native_retry_sent = False
        self._prompt = ""
        self._before_answer_count = 0
        self._before_answer_text = ""
        self._response_baseline: Optional[ResponseBaseline] = None
        self._logical_response_baseline: Optional[ResponseBaseline] = None
        self._native_answer_captured = False
        self._confirmed_failure_signature: Optional[str] = None
        self._correlation_checkpoint: Optional[Callable] = None
        self._correlation_task: Optional[asyncio.Task] = None
        self._correlation_error: Optional[BaseException] = None
        self._last_correlation_payload: Optional[str] = None
        self._last_correlation_value: Optional[dict] = None
        self._history_before: Mapping[str, object] = {}
        self._recovery_binding: Optional[Mapping[str, object]] = None
        self._last_history_read_monotonic: Optional[float] = None
        self._actions = SharedBrowserActions(self)
        self._composer_target = None
        self._send_target = None
        self._native_retry_target = None
        self._native_retry_signature = None
        self._dispatch_deadline = None
        self._failure_transport = None
        self._usage_limit = None

    def finish_failure_observation(self) -> None:
        if self._failure_transport is not None:
            self._failure_transport.finish()

    def failure_transport_snapshot(self):
        return self._failure_transport.snapshot() if self._failure_transport is not None else None

    def usage_limit_snapshot(self):
        return dict(self._usage_limit) if self._usage_limit is not None else None

    def _record_usage_limit(self, notice):
        self._usage_limit = dict(notice)

    @property
    def identity(self) -> BrowserIdentity:
        return self._identity

    @property
    def response_baseline(self) -> Optional[ResponseBaseline]:
        return self._response_baseline

    def set_correlation_checkpoint(self, callback: Callable) -> None:
        """Bind a mandatory durable writer before entering the send boundary."""
        if not callable(callback) or self._submitted:
            raise BrowserInteractionError("invalid conversation checkpoint binding")
        self._correlation_checkpoint = callback
        self._correlation_error = None
        self._last_correlation_payload = None
        self._last_correlation_value = None
        self._last_history_read_monotonic = None

    # The conversation URL is a local Playwright property (no browser round
    # trip), so it is sampled every CORRELATION_URL_POLL_SECONDS and saved as
    # soon as it appears. Reading the whole transcript costs several browser
    # calls per message, so it runs when the URL is first saved, then at most
    # every CORRELATION_HISTORY_INTERVAL_SECONDS, and once after the answer.
    CORRELATION_URL_POLL_SECONDS = 0.1
    CORRELATION_HISTORY_INTERVAL_SECONDS = 2.0

    async def _checkpoint_conversation(self, *, read_history: bool = True) -> None:
        if self._correlation_checkpoint is None or self._response_baseline is None:
            return
        try:
            url = canonical_conversation_url(getattr(self.page, "url", None), self.spec.conversation_url_prefixes)
        except ConversationCorrespondenceError:
            return
        if self._last_correlation_value is not None and self._last_correlation_value["identity"]["conversation_reference"] != url:
            raise IdentityMismatchError("conversation changed during correlation checkpoint")
        # This records observed correlation, not acceptance. The pre-send
        # identity already bound this exact page/target to the admitted effect.
        identity = replace(self._identity, conversation_reference=url)
        baseline = self._response_baseline
        logical = self._logical_response_baseline or baseline
        transcript = {
            "prior_user_sha256": list(self._history_before.get("user_sha256", ())),
            "prior_assistant_sha256": list(self._history_before.get("assistant_sha256", ())),
            "user_ids": list(self._history_before.get("user_ids", ())),
            "assistant_ids": list(self._history_before.get("assistant_ids", ())),
            "branch_id": self._history_before.get("branch_id"),
        }
        if self._last_correlation_value is not None:
            transcript.update(self._last_correlation_value["transcript"])
        value = {"schema_version": "browser-conversation-binding-v1", "identity": identity.to_mapping(),
                 "prompt_sha256": _text_sha256(self._prompt), "expected_turn": logical.visible_response_count + 1,
                 "response_baseline": baseline.to_mapping(), "transcript": transcript}
        if self._logical_response_baseline is not None:
            value["logical_response_baseline"] = logical.to_mapping()
        first_publication = self._last_correlation_payload is None
        await self._publish_correlation(value)
        if not read_history and not first_publication:
            return
        # Enrich the early URL checkpoint with message/branch identity as soon
        # as the UI exposes it. A history read cannot delay the first write.
        self._last_history_read_monotonic = time.monotonic()
        try:
            history = await _conversation_history(self.page, self.spec, _deadline=_BrowserOperationDeadline.after(2, timeout_message="conversation checkpoint history timed out"))
        except (BrowserInteractionError, TimeoutError, AttributeError):
            return
        for key in ("user_ids", "assistant_ids"):
            prior, current = list(transcript.get(key) or ()), list(history.get(key) or ())
            if key == "assistant_ids" and not self._native_answer_captured:
                # Streaming fragments and failed regeneration wrappers can be
                # replaced. Bind prior answers now, the awaited one on capture.
                current = current[:logical.visible_response_count]
            for index, known in enumerate(prior):
                if known is not None and index < len(current) and current[index] not in (None, known):
                    raise IdentityMismatchError("conversation message changed during checkpoint")
            if len(current) >= len(prior):
                transcript[key] = [value if value is not None else prior[index] if index < len(prior) else None for index, value in enumerate(current)]
        branch = history.get("branch_id")
        if transcript.get("branch_id") is not None and branch not in (None, transcript["branch_id"]):
            raise IdentityMismatchError("conversation branch changed during checkpoint")
        if branch is not None:
            transcript["branch_id"] = branch
        await self._publish_correlation(value)

    async def _publish_correlation(self, value: Mapping[str, object]) -> None:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        if encoded != self._last_correlation_payload:
            result = self._correlation_checkpoint(value)
            if inspect.isawaitable(result):
                await result
            self._last_correlation_payload = encoded
            self._last_correlation_value = json.loads(encoded)

    async def _watch_conversation(self) -> None:
        try:
            while self._submitted and not self.cancelled():
                last_read = self._last_history_read_monotonic
                due = last_read is None or time.monotonic() - last_read >= self.CORRELATION_HISTORY_INTERVAL_SECONDS
                await self._checkpoint_conversation(read_history=due)
                await asyncio.sleep(self.CORRELATION_URL_POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._correlation_error = exc

    async def _stop_conversation_watch(self) -> None:
        task, self._correlation_task = getattr(self, "_correlation_task", None), None
        if task is not None and not task.done():
            _cancel_background_browser_operation(task)
            await asyncio.wait((task,), timeout=.25)

    def restore_recovery_attempt(self, *, prompt: str, response_baseline: Mapping[str, object], binding: Mapping[str, object]) -> None:
        """Restore an uncertain sent turn without granting another submission."""
        if self._submitted or self._prepared or _text_sha256(prompt) != binding.get("prompt_sha256"):
            raise BrowserInteractionError("recovery prompt binding mismatch")
        baseline = ResponseBaseline.from_mapping(response_baseline)
        if baseline.to_mapping() != binding.get("response_baseline"):
            raise BrowserInteractionError("recovery baseline binding mismatch")
        self._prompt, self._response_baseline = prompt, baseline
        logical = logical_response_baseline(binding)
        self._logical_response_baseline = (
            ResponseBaseline.from_mapping(logical)
            if "logical_response_baseline" in binding else None
        )
        self._native_retry_sent = self._logical_response_baseline is not None
        self._before_answer_count = baseline.visible_response_count
        self._before_answer_text = ""
        rebound = getattr(self, "_restart_recovery_binding", None)
        self._recovery_binding = dict(rebound[1] if rebound is not None and rebound[0] == binding else binding)
        self._submitted = True
        self._require_fresh_blank_start = False
        if binding.get("expected_turn") == 2:
            self._current_response_timeout_seconds = self.extraction_timeout_seconds

    def preparation_timeout_seconds(
        self, prompt: str, timeout_seconds: Optional[float] = None,
    ) -> float:
        """Include intentional per-character pacing in the preparation budget."""

        budget = (
            PROVIDER_PREPARATION_TIMEOUT_SECONDS
            if timeout_seconds is None else float(timeout_seconds)
        )
        if self.spec.type_composer_input:
            budget += (
                len(prompt) * self.spec.composer_typing_max_delay_ms
                * self.composer_attempts / 1000.0
            )
        return budget

    async def prepare_exact(
        self,
        prompt: str,
        *,
        on_pre_send_failure: Optional[
            Callable[[ComposerPreSendFailure], object]
        ] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        """Pace, then prepare within the base budget plus deliberate typing time.

        The pre-send pause is pacing, not work: it runs before the deadline is
        created, so it never consumes the preparation budget.  ``None`` means
        ``PROVIDER_PREPARATION_TIMEOUT_SECONDS``; a second message in an
        existing chat is given
        ``PROVIDER_SECOND_MESSAGE_PREPARATION_TIMEOUT_SECONDS`` by its caller.
        Sequential-keyboard providers add the known per-character delays for
        all bounded composer attempts; browser operations remain timed out.
        """
        # The same two refusals as in ``_prepare_exact``, checked before the
        # pause so a refused call still raises at once.
        if self._submitted:
            raise BrowserInteractionError("cannot prepare after submission started")
        if self._native_retry_prepared:
            raise BrowserInteractionError(
                "native retry is already prepared; do not add a user message"
            )
        await interaction_pause(
            self.interaction_min_delay_seconds,
            self.interaction_max_delay_seconds,
            cancelled=self.cancelled,
        )
        budget = self.preparation_timeout_seconds(prompt, timeout_seconds)
        deadline = _BrowserOperationDeadline.after(budget, timeout_message="provider preparation timed out")
        for attempt in range(3):
            try:
                await _await_call(self._prepare_exact, prompt, on_pre_send_failure=on_pre_send_failure,
                    operation_deadline=deadline, _deadline=deadline)
                return
            except Exception as error:
                if (attempt == 2 or deadline.expired() or getattr(error, "access_blocker_handled", False)
                        or not self._handles_access_blockers()):
                    raise
                marker = await _visible_access_block_marker(self.page, self.spec, _deadline=deadline)
                if marker is None:
                    raise
                await self._assert_access_available(_deadline=deadline)

    async def _prepare_exact(self, prompt: str, *, on_pre_send_failure=None, operation_deadline=None) -> None:
        if self._submitted:
            raise BrowserInteractionError("cannot prepare after submission started")
        if self._native_retry_prepared:
            raise BrowserInteractionError(
                "native retry is already prepared; do not add a user message"
            )
        await self._actions.release_all()
        self._composer_target = self._send_target = self._native_retry_target = None
        self._dispatch_deadline = None
        self._composer = None
        self._send_control = None
        self._native_retry_control = None
        self._prepared = False
        self._native_retry_prepared = False
        self._native_retry_sent = False
        self._prompt = ""
        self._before_answer_count = 0
        self._before_answer_text = ""
        self._response_baseline = None
        self._confirmed_failure_signature = None
        await self._assert_access_available(_deadline=operation_deadline if self._handles_access_blockers() else None)
        operation_deadline = operation_deadline or _BrowserOperationDeadline.after(
            self.preparation_timeout_seconds(prompt), timeout_message="provider preparation timed out")
        composer = await self._first_action_with_access(self.spec.composer_selectors,
            timeout_ms=20_000, deadline=operation_deadline)
        composer_target = await self._actions.retain(composer, operation_deadline.expires_at)
        self._composer_target = composer_target
        last_access_probe = 0.

        async def verify_preparation(until):
            nonlocal last_access_probe
            if self._submitted:
                raise BrowserInteractionError("cannot prepare after submission started")
            await self._actions.assert_owned(until)
            live = str(getattr(self.page, "url", "") or "")
            if self._require_fresh_blank_start:
                if not _is_blank_provider_start_url(live, self.spec.start_url,
                        aliases=self.spec.blank_start_url_aliases):
                    raise IdentityMismatchError("fresh provider route changed during input")
            elif _canonical_reference(live, self.spec) != _canonical_reference(
                    self._identity.conversation_reference, self.spec):
                raise IdentityMismatchError("provider conversation changed during input")
            immediate = getattr(self.page, "_visibility_runner_blocking_dialog", None) is not None or _is_authentication_url(
                live, self.spec.authentication_url_prefixes)
            if immediate or time.monotonic() - last_access_probe >= .2:
                last_access_probe = time.monotonic()
                marker = await _visible_access_block_marker(self.page, self.spec, _deadline=operation_deadline)
                if marker is not None:
                    raise _access_block_error(self.spec.platform, marker)
            return True

        async def clear_value(_target):
            await self._actions.clear(composer_target, operation_deadline.expires_at, verify_preparation)

        async def write_value(_target, value):
            await self._actions.write(composer_target, value, operation_deadline.expires_at,
                verify_preparation, sequential=self.spec.type_composer_input,
                min_delay_ms=self.spec.composer_typing_min_delay_ms if self.spec.type_composer_input else 0.,
                max_delay_ms=self.spec.composer_typing_max_delay_ms if self.spec.type_composer_input else 0.)

        async def read_value(target):
            await self._actions.validate(composer_target, operation_deadline.expires_at)
            reader = read_copilot_composer_value if self.spec.copilot_lexical_readback else read_typed_composer_value
            return await _await_call(reader, target, _deadline=operation_deadline)
        await write_exact_composer_input(
            composer,
            prompt,
            max_attempts=self.composer_attempts,
            send_started=lambda: self._submitted,
            record_failure=on_pre_send_failure,
            clear=clear_value, write=write_value, read=read_value,
        )
        # A wall may have appeared while typing. Recheck before the trial click.
        if self._handles_access_blockers():
            await self._assert_access_available(_deadline=operation_deadline)
        try:
            control = await self._first_action_with_access(self.spec.send_selectors,
                timeout_ms=4_000, deadline=operation_deadline)
        except TimeoutError:
            # Some website variants submit exclusively through Enter.
            pass
        else:
            click = getattr(control, "click", None)
            if click is None:
                raise BrowserInteractionError(
                    "provider send control is not clickable"
                )
            try:
                await _await_call(lambda: click(trial=True,
                    timeout=max(1, min(4_000, int(operation_deadline.remaining() * 1000)))),
                    _deadline=operation_deadline)
            except TypeError as exc:
                raise BrowserInteractionError(
                    f"{self.spec.platform} send control does not support "
                    "a safe Playwright trial click"
                ) from exc
            self._send_control = control
            self._send_target = await self._actions.retain(control, operation_deadline.expires_at)
        self._composer = composer
        self._prompt = prompt
        await self._capture_pre_send_answer(_deadline=operation_deadline)
        self._prepared = True

    def _handles_access_blockers(self):
        return callable(getattr(self.human_verification_handler, "resolve", None))

    async def _first_action_with_access(self, selectors, *, timeout_ms, deadline):
        """Resolve one action locator without promoting an arbitrary nth node."""
        expires = min(deadline.expires_at, time.monotonic() + timeout_ms / 1000)
        while time.monotonic() < expires:
            if self._handles_access_blockers():
                await self._assert_access_available(_deadline=deadline)
            for selector in selectors:
                locator = self.page.locator(selector)
                count = int(await _await_call(locator.count, _deadline=deadline) or 0)
                if count > 1:
                    raise BrowserInteractionError("provider action target is not unique")
                if count == 1 and await _await_call(locator.is_visible, _deadline=deadline):
                    return locator
            await asyncio.sleep(min(.05, max(0., expires - time.monotonic())))
        if deadline.expired():
            deadline.raise_expired()
        raise TimeoutError("provider action control did not become available")

    async def _first_visible_with_access(self, selectors, *, timeout_ms, deadline=None):
        if not self._handles_access_blockers():
            return await first_visible(self.page, selectors, timeout_ms=timeout_ms)
        deadline = deadline or _BrowserOperationDeadline.after(timeout_ms / 1000,
            timeout_message="Copilot controls did not become available")
        expires = time.monotonic() + timeout_ms / 1000
        while not deadline.expired() and time.monotonic() < expires:
            await self._assert_access_available(_deadline=deadline)
            items = await visible_items(self.page, selectors, _deadline=deadline)
            if items:
                return items[0]
            await asyncio.sleep(min(.2, max(0, deadline.remaining())))
        raise TimeoutError("Copilot controls did not become available")

    async def _capture_pre_send_answer(
        self,
        *,
        _deadline: Optional[_BrowserOperationDeadline] = None,
    ) -> None:
        snapshot = await visible_answer_snapshot(
            self.page,
            self.spec.response_node_selectors or self.spec.answer_selectors,
            label_selectors=self.spec.rendered_answer_label_selectors,
            content_selectors=self.spec.response_content_selectors,
            collapse_nested=self.spec.collapse_nested_message_aliases,
            turn_owner_selector=self.spec.current_turn_owner_selector,
            _deadline=_deadline,
        )
        if _deadline is not None and _deadline.expired():
            _deadline.raise_expired()
        self._before_answer_count = snapshot.count
        self._before_answer_text = snapshot.text
        self._response_baseline = ResponseBaseline.from_snapshot(snapshot)
        if snapshot.count:
            self._history_before = await _conversation_history(self.page, self.spec, _deadline=_deadline or _BrowserOperationDeadline.after(10, timeout_message="conversation baseline history timed out"))
        else:
            self._history_before = {"user_sha256": (), "assistant_sha256": (), "user_ids": (), "assistant_ids": ()}

    async def _assert_access_available(
        self,
        *,
        _deadline: Optional[_BrowserOperationDeadline] = None,
    ) -> None:
        if not (
            self.spec.access_wall_selectors
            or self.spec.human_verification_selectors
            or self.spec.human_verification_text_selectors
            or self.spec.authentication_url_prefixes
            or self.spec.unauthenticated_control_selectors
            or self.spec.other_blocker_selectors
            or self.spec.cookie_notice_selectors
        ):
            return
        deadline = _deadline or _BrowserOperationDeadline.after(
            20.0,
            timeout_message="provider pre-send access check timed out",
        )
        watch = getattr(self.human_verification_handler, "watch_page", None)
        if callable(watch):
            await watch(self.page, deadline)
        marker = await _visible_access_block_marker(
            self.page,
            self.spec,
            _deadline=deadline,
        )
        if marker is None:
            gate = getattr(self.human_verification_handler, "gate", None)
            if callable(gate) and await gate(self, deadline):
                marker = await _visible_access_block_marker(self.page, self.spec, _deadline=deadline)
        if marker is not None:
            if self._handles_access_blockers():
                await self.human_verification_handler.resolve(self, marker, deadline)
                return
            if marker == "human_verification_required" and self.human_verification_handler is not None:
                await self._resolve_human_verification(deadline)
                return
            raise _access_block_error(self.spec.platform, marker)

    async def _resolve_human_verification(self, deadline) -> None:
        try:
            return await self._resolve_human_verification_inner(deadline)
        except BaseException as error:
            failed = getattr(self.human_verification_handler, "failed", None)
            if callable(failed):
                failed(self, error, deadline)
            raise

    async def _resolve_human_verification_inner(self, deadline) -> None:
        """Handle one visible challenge without repeating the pending message."""
        if self.human_verification_handler is None or self._verification_attempts >= 2:
            raise HumanVerificationRequiredError("human verification still requires user action")
        if self.cancelled():
            raise BrowserInteractionError("verification action was cancelled")
        self._verification_attempts += 1
        try:
            try:
                await self.human_verification_handler(self, deadline)
            except HumanVerificationRequiredError as exc:
                # A guard can refuse input as the challenge disappears. Re-read
                # once, without dispatching another action or repeating a send.
                if getattr(exc, "verification_input_attempted", None) is not False:
                    raise
                marker = await _visible_access_block_marker(self.page, self.spec, _deadline=deadline)
                if marker == "human_verification_required":
                    raise
                if marker is not None:
                    raise _access_block_error(self.spec.platform, marker)
            # A native click is not proof of success. Only the original page
            # clearing its deterministic access marker allows work to resume.
            while not deadline.expired():
                if self.cancelled():
                    raise BrowserInteractionError("verification action was cancelled")
                poll = getattr(self.human_verification_handler, "poll_commands", None)
                if callable(poll):
                    await _await_call(poll, self, deadline, _deadline=deadline)
                marker = await _visible_access_block_marker(self.page, self.spec, _deadline=deadline)
                if marker is None:
                    observed = await strict_page_identity(self.page, profile_id=self.profile_id,
                        chrome_directory=self.chrome_directory, _deadline=deadline)
                    if not self._submitted:
                        self._identity.assert_same_tab(observed, require_same_conversation=True,
                            canonical=lambda ref: _canonical_reference(ref, self.spec))
                        if self._prepared and self._composer is not None:
                            value = await _await_call(read_copilot_composer_value,
                                self._composer, _deadline=deadline)
                            if value != self._prompt:
                                raise BrowserInteractionError("prepared prompt changed during human verification")
                    elif not self._accept_live_identity(observed):
                        raise IdentityMismatchError("conversation changed during human verification")
                    validated = getattr(self.human_verification_handler, "validated", None)
                    if callable(validated):
                        await _await_call(validated, self, deadline, _deadline=deadline)
                    return
                if marker != "human_verification_required":
                    raise _access_block_error(self.spec.platform, marker)
                await asyncio.sleep(min(.2, max(0, deadline.remaining())))
        except TimeoutError as exc:
            raise HumanVerificationRequiredError("human verification did not clear before the operation deadline") from exc
        raise HumanVerificationRequiredError("human verification did not clear before the operation deadline")

    async def validate_pre_send_identity(self) -> None:
        """Re-prove access, live tab and conversation at the send boundary."""

        if not self._prepared:
            raise BrowserInteractionError(
                "cannot validate provider identity before exact preparation"
            )
        deadline = _BrowserOperationDeadline.after(
            PROVIDER_PRE_SEND_IDENTITY_TIMEOUT_SECONDS,
            timeout_message="provider pre-send identity check timed out",
        )
        self._dispatch_deadline = deadline
        if self._handles_access_blockers():
            await self._assert_access_available(_deadline=deadline)
        elif _is_authentication_url(
            getattr(self.page, "url", ""),
            self.spec.authentication_url_prefixes,
        ):
            raise _access_block_error(self.spec.platform, "authentication_redirect")
        observed = await strict_page_identity(
            self.page,
            profile_id=self.profile_id,
            chrome_directory=self.chrome_directory,
            _deadline=deadline,
        )
        if self._require_fresh_blank_start:
            self._identity.assert_same_tab(observed)
            if not _is_blank_provider_start_url(
                observed.conversation_reference,
                self.spec.start_url,
                aliases=self.spec.blank_start_url_aliases,
            ):
                raise IdentityMismatchError(
                    f"{self.spec.platform} fresh chat changed identity "
                    "during pre-send preparation: observed "
                    f"{_provider_url_diagnostic(observed.conversation_reference)}"
                )
        else:
            self._identity.assert_same_tab(
                observed,
                require_same_conversation=True,
                canonical=lambda ref: _canonical_reference(ref, self.spec),
            )
        await self._assert_access_available(_deadline=deadline)
        self._identity = observed

    async def _verify_shared_send(self, until: float) -> bool:
        """Read the original source facts again immediately before input."""
        deadline = _BrowserOperationDeadline(until, "provider send verification timed out")
        observed = await strict_page_identity(self.page, profile_id=self.profile_id,
            chrome_directory=self.chrome_directory, _deadline=deadline)
        self._identity.assert_same_tab(observed)
        if self._require_fresh_blank_start:
            if not _is_blank_provider_start_url(observed.conversation_reference,
                    self.spec.start_url, aliases=self.spec.blank_start_url_aliases):
                raise IdentityMismatchError("fresh provider route changed before input")
        else:
            self._identity.assert_same_tab(observed, require_same_conversation=True,
                canonical=lambda ref: _canonical_reference(ref, self.spec))
        marker = await _visible_access_block_marker(self.page, self.spec, _deadline=deadline)
        if marker is not None:
            raise _access_block_error(self.spec.platform, marker)
        if self._native_retry_sent:
            probe = await self._reconciliation_probe(include_retry_control=True, _deadline=deadline,
                _baseline_override=self._logical_response_baseline, _resolve_access=False)
            if (not probe.identity_valid or not probe.prompt_exact or probe.new_response
                    or probe.busy is not False or probe.retryable_failure != self._native_retry_signature
                    or probe.native_retry_count != 1 or probe.native_retry_control is None):
                raise BrowserInteractionError("native retry state changed before input")
            # Core validation also proves the exact retained node still matches
            # its original latest-response-scoped locator.
            await self._actions.validate(self._native_retry_target, until)
            current = await _await_call(probe.native_retry_control.element_handle, _deadline=deadline)
            try:
                same = await _await_call(self._native_retry_target.native.handle.evaluate,
                    "(element, current) => element === current", current, _deadline=deadline)
                if same is not True:
                    raise BrowserInteractionError("native retry response target changed before input")
            finally:
                if current is not None:
                    await _await_call(current.dispose, _deadline=deadline)
        else:
            if self._composer_target is None:
                raise BrowserInteractionError("prepared composer target is unavailable")
            await self._actions.validate(self._composer_target, until)
            reader = read_copilot_composer_value if self.spec.copilot_lexical_readback else read_typed_composer_value
            value = await _await_call(reader, self._composer_target.locator, _deadline=deadline)
            if value != self._prompt:
                raise BrowserInteractionError("prepared prompt changed before input")
        conversation = await _read_conversation(self.page, self.spec, _deadline=deadline)
        if ResponseBaseline.from_snapshot(conversation.answer) != self._response_baseline:
            raise BrowserInteractionError("provider response baseline changed before input")
        history = conversation.history
        if not self._native_retry_sent and history.get("generating"):
            raise BrowserInteractionError("provider response became active before input")
        for key in ("user_sha256", "assistant_sha256"):
            prior = list(self._history_before.get(key, ()))
            current = list(history.get(key, ()))
            if self._native_retry_sent:
                if len(current) != len(prior) + 1:
                    raise BrowserInteractionError("native retry conversation turn count changed before input")
                current = current[:len(prior)]
            if current != prior:
                raise BrowserInteractionError("provider prior conversation changed before input")
        for key in ("user_ids", "assistant_ids"):
            current = list(history.get(key, ()))
            for index, known in enumerate(self._history_before.get(key, ())):
                if (known is not None and not is_temporary_message_id(known, self.spec.temporary_message_id_prefixes)
                        and (index >= len(current) or current[index] != known)):
                    raise BrowserInteractionError("provider prior message identity changed before input")
        branch = self._history_before.get("branch_id")
        if branch is not None and history.get("branch_id") != branch:
            raise BrowserInteractionError("provider conversation branch changed before input")
        return True

    async def submit_once(self) -> None:
        if not self._prepared or (
            self._composer is None and not self._native_retry_prepared
        ):
            raise BrowserInteractionError("submission was not prepared exactly")
        if self._submitted:
            raise BrowserInteractionError("submission action may run only once")
        if self.cancelled():
            raise BrowserInteractionError(
                "module cancellation interrupted provider submission"
            )
        if self._response_baseline is None:
            raise BrowserInteractionError(
                "provider response baseline was not captured before send"
            )
        # Mark before the first irreversible browser event. Any exception from
        # this point is intentionally ambiguous and must not trigger a retry.
        deadline = self._dispatch_deadline or _BrowserOperationDeadline.after(
            PROVIDER_PRE_SEND_IDENTITY_TIMEOUT_SECONDS, timeout_message="provider submission timed out")
        self._submitted = True
        if self._handles_access_blockers():
            await self._assert_access_available(_deadline=deadline)
        if self._correlation_checkpoint is not None:
            self._correlation_task = asyncio.create_task(self._watch_conversation())
        if self.spec.platform == "chatgpt":
            try:
                self.finish_failure_observation()
                self._failure_transport = _FailureTransportObservation(self.page, self.spec.start_url)
                self._failure_transport.start()
            except Exception:
                pass
        if self._native_retry_prepared:
            control = self._native_retry_control
            self._native_retry_prepared = False
            self._native_retry_control = None
            self._confirmed_failure_signature = None
            self._native_retry_sent = True
            if control is None:
                raise BrowserInteractionError(
                    "provider native retry control is unavailable"
                )
            click = getattr(control, "click", None)
            if click is None:
                raise BrowserInteractionError(
                    "provider native retry control is not clickable"
                )
            if self._native_retry_target is None:
                raise BrowserInteractionError("provider native retry retained target is unavailable")
            await self._actions.click(self._native_retry_target, deadline.expires_at, self._verify_shared_send)
            return
        if self._send_control is None:
            assert self._composer is not None
            press = getattr(self._composer, "press", None)
            if press is None:
                raise BrowserInteractionError(
                    f"{self.spec.platform} has no usable send control"
                ) from None
            await self._actions.press(self._composer_target, "Enter", deadline.expires_at, self._verify_shared_send)
            return
        click = getattr(self._send_control, "click", None)
        if click is None:
            raise BrowserInteractionError("provider send control is not clickable")
        if self._send_target is None:
            raise BrowserInteractionError("provider send retained target is unavailable")
        await self._actions.click(self._send_target, deadline.expires_at, self._verify_shared_send)

    async def wait_response(self) -> ProviderAnswer:
        task = asyncio.create_task(self._wait_response())
        try:
            watcher = self._correlation_task
            if watcher is not None:
                await asyncio.wait((task, watcher), return_when=asyncio.FIRST_COMPLETED)
                if self._correlation_error is not None:
                    _cancel_background_browser_operation(task)
                    raise self._correlation_error
            answer = await task
            self._native_answer_captured = True
            await self._checkpoint_conversation()
            if self._correlation_error is not None:
                raise self._correlation_error
            return answer
        except BaseException:
            if not task.done():
                _cancel_background_browser_operation(task)
            raise
        finally:
            await self._stop_conversation_watch()

    async def _wait_response(self) -> ProviderAnswer:
        if not self._submitted:
            raise BrowserInteractionError("cannot await an unsent provider turn")
        deadline = _BrowserOperationDeadline.after(
            self._current_response_timeout_seconds,
            timeout_message=(
                f"{self.spec.platform} response did not settle"
            ),
        )
        text = await wait_for_stable_answer(
            self.page,
            self.spec,
            before_count=self._before_answer_count,
            before_answer=self._before_answer_text,
            expected_prompt=self._prompt,
            cancelled=self.cancelled,
            allow_same_node_replacement=self._native_retry_sent,
            _deadline=deadline,
            resolve_human_verification=(self._resolve_human_verification
                if self.human_verification_handler is not None else None),
            resolve_access_blocker=(self._assert_access_available
                if self._handles_access_blockers() else None),
            usage_limit_observer=self._record_usage_limit,
        )
        observed = await strict_page_identity(
            self.page,
            profile_id=self.profile_id,
            chrome_directory=self.chrome_directory,
            _deadline=deadline,
        )
        if not self._accept_live_identity(observed):
            raise IdentityMismatchError(
                "provider identity changed before response capture"
            )
        return ProviderAnswer(
            status=ProviderStatus.COLLECTED,
            text=text,
            identity=self._identity,
            capture_evidence=({"usage_limit": self.usage_limit_snapshot()}
                if self._usage_limit is not None else {}),
        )

    async def reconcile_no_response(
        self,
        grace_seconds: float,
    ) -> ReconciliationResult:
        """Inspect the exact sent turn; a timeout alone never authorizes retry."""

        grace = float(grace_seconds)
        if not math.isfinite(grace) or grace < 0:
            raise ValueError("grace_seconds must be finite and non-negative")
        if not self._submitted:
            raise BrowserInteractionError(
                "cannot reconcile a provider turn before send start"
            )
        if not self._prompt or self._response_baseline is None:
            return self._ambiguous_reconciliation(
                "missing_pre_send_evidence",
                (),
            )

        deadline = _BrowserOperationDeadline.after(
            grace,
            timeout_message=(
                f"{self.spec.platform} reconciliation did not settle"
            ),
        )
        checks: list[dict[str, object]] = []
        terminal_signature: Optional[str] = None
        consecutive_terminal_checks = 0
        late_signature: Optional[str] = None
        consecutive_late_checks = 0
        minimum_checks = 2

        while True:
            if deadline.expired() and len(checks) >= minimum_checks:
                break
            try:
                probe = await self._reconciliation_probe(
                    _deadline=deadline,
                )
            except _BrowserOperationDeadlineExceeded as exc:
                return self._ambiguous_reconciliation(
                    (checks[-1].get("website_failure") if checks else None)
                    or "reconciliation_deadline_exceeded",
                    checks,
                    detail=str(exc),
                    grace_seconds=grace,
                )
            except Exception as exc:
                return self._ambiguous_reconciliation(
                    "identity_or_dom_unavailable",
                    checks,
                    detail=str(exc),
                    grace_seconds=grace,
                )
            checks.append(probe.evidence)

            if probe.website_failure in {
                "provider_access_blocked:unusual_activity",
                "provider_access_blocked:usage_limit",
            }:
                return self._ambiguous_reconciliation(
                    probe.website_failure,
                    checks,
                    grace_seconds=grace,
                )
            if not probe.identity_valid:
                return self._ambiguous_reconciliation(
                    probe.website_failure or "browser_identity_changed",
                    checks,
                    grace_seconds=grace,
                )
            if not probe.prompt_exact:
                return self._ambiguous_reconciliation(
                    "expected_user_message_unproven",
                    checks,
                    grace_seconds=grace,
                )
            if probe.nonretryable_failure is not None:
                self._confirmed_failure_signature = None
                return ReconciliationResult(
                    disposition=(
                        ReconciliationDisposition.TERMINAL_NONRETRYABLE
                    ),
                    identity=self._identity,
                    evidence=_reconciliation_evidence(
                        grace,
                        checks,
                        reason="terminal_nonretryable",
                    ),
                    failure_classification=probe.nonretryable_failure,
                    native_retry_available=False,
                )

            if (
                probe.new_response
                and probe.answer_text
                and probe.busy is False
                and probe.retryable_failure is None
            ):
                signature = _text_sha256(probe.answer_text)
                if signature == late_signature:
                    consecutive_late_checks += 1
                else:
                    late_signature = signature
                    consecutive_late_checks = 1
                if consecutive_late_checks >= minimum_checks:
                    answer = ProviderAnswer(
                        status=ProviderStatus.COLLECTED,
                        text=probe.answer_text,
                        identity=self._identity,
                    )
                    self._confirmed_failure_signature = None
                    return ReconciliationResult(
                        disposition=(
                            ReconciliationDisposition.LATE_RESPONSE_CAPTURED
                        ),
                        identity=self._identity,
                        evidence=_reconciliation_evidence(
                            grace,
                            checks,
                            reason="late_response_captured",
                        ),
                        answer=answer,
                        native_retry_available=False,
                    )
            else:
                late_signature = None
                consecutive_late_checks = 0

            if (
                probe.retryable_failure is not None
                and not probe.new_response
                and probe.busy is False
            ):
                if probe.retryable_failure == terminal_signature:
                    consecutive_terminal_checks += 1
                else:
                    terminal_signature = probe.retryable_failure
                    consecutive_terminal_checks = 1
            else:
                terminal_signature = None
                consecutive_terminal_checks = 0

            if deadline.expired() and len(checks) >= minimum_checks:
                break
            remaining = max(0.0, deadline.remaining())
            await asyncio.sleep(min(0.25, remaining) if remaining else 0)

        final = checks[-1]
        if (
            terminal_signature is not None
            and consecutive_terminal_checks >= minimum_checks
            and final.get("busy") is False
            and final.get("new_response") is False
        ):
            self._confirmed_failure_signature = terminal_signature
            return ReconciliationResult(
                disposition=(
                    ReconciliationDisposition.CONFIRMED_TERMINAL_NO_RESPONSE
                ),
                identity=self._identity,
                evidence=_reconciliation_evidence(
                    grace,
                    checks,
                    reason="stable_terminal_no_response",
                    stable_terminal_checks=consecutive_terminal_checks,
                ),
                failure_classification=terminal_signature,
                native_retry_available=bool(
                    final.get("native_retry_available")
                ),
            )
        self._confirmed_failure_signature = None
        return self._ambiguous_reconciliation(
            final.get("website_failure") or "no_stable_terminal_failure",
            checks,
            grace_seconds=grace,
        )

    async def prepare_native_retry(self) -> bool:
        """Prepare one scoped native regeneration without adding a user turn."""

        expected_signature = self._confirmed_failure_signature
        if not expected_signature or not self._submitted:
            return False
        deadline = _BrowserOperationDeadline.after(
            self._current_response_timeout_seconds,
            timeout_message=(
                f"{self.spec.platform} native retry preparation did not settle"
            ),
        )
        probe = await self._reconciliation_probe(
            include_retry_control=True,
            _deadline=deadline,
        )
        if (
            not probe.identity_valid
            or not probe.prompt_exact
            or probe.new_response
            or probe.busy is not False
            or probe.retryable_failure != expected_signature
        ):
            raise BrowserInteractionError(
                "native retry state could not be re-proved safely"
            )
        if probe.native_retry_count == 0:
            return False
        if (
            probe.native_retry_count != 1
            or probe.native_retry_control is None
        ):
            raise BrowserInteractionError(
                "native retry control is not uniquely identifiable"
            )
        click = getattr(probe.native_retry_control, "click", None)
        if click is None:
            return False
        await _await_call(
            lambda: click(trial=True, timeout=max(1, min(4_000, int(deadline.remaining() * 1000)))),
            _deadline=deadline,
        )
        logical = self._logical_response_baseline or self._response_baseline
        assert logical is not None
        saved = self._last_correlation_value or self._recovery_binding
        history_before = self._history_before
        await self._capture_pre_send_answer(
            _deadline=deadline,
        )
        history = self._history_before
        if saved is not None:
            verify_conversation_history(saved,
                {"identity": self._identity.to_mapping(), "transcript": history},
                prefixes=self.spec.conversation_url_prefixes,
                prompt_sha256=_text_sha256(self._prompt),
                expected_profile_id=self.profile_id,
                expected_chrome_directory=self.chrome_directory,
                temporary_message_id_prefixes=self.spec.temporary_message_id_prefixes)
        count = logical.visible_response_count
        def prior_ids(key, limit):
            current = list(history.get(key, ()))[:limit]
            for index, known in enumerate(list(history_before.get(key, ()))[:limit]):
                if index >= len(current):
                    current.append(known)
                elif known is not None:
                    current[index] = known
            return current
        # Keep the user turn and all earlier history, excluding the failed answer.
        # The recaptured response snapshot remains the physical send-fence proof.
        prior = {
            **history,
            "user_sha256": list(history_before.get("user_sha256", history["user_sha256"]))[:count],
            "assistant_sha256": list(history_before.get("assistant_sha256", history["assistant_sha256"]))[:count],
            "user_ids": prior_ids("user_ids", count + 1),
            "assistant_ids": prior_ids("assistant_ids", count),
            "branch_id": history_before.get("branch_id") or history.get("branch_id"),
        }
        candidate = {"schema_version": "browser-conversation-binding-v1",
            "identity": self._identity.to_mapping(), "prompt_sha256": _text_sha256(self._prompt),
            "expected_turn": count + 1, "response_baseline": self._response_baseline.to_mapping(),
            "logical_response_baseline": logical.to_mapping(),
            "transcript": {"prior_user_sha256": prior["user_sha256"],
                "prior_assistant_sha256": prior["assistant_sha256"],
                "user_ids": prior["user_ids"], "assistant_ids": prior["assistant_ids"],
                "branch_id": prior.get("branch_id")}}
        verify_conversation_history(candidate,
            {"identity": self._identity.to_mapping(), "transcript": history},
            prefixes=self.spec.conversation_url_prefixes, prompt_sha256=_text_sha256(self._prompt),
            expected_profile_id=self.profile_id, expected_chrome_directory=self.chrome_directory,
            temporary_message_id_prefixes=self.spec.temporary_message_id_prefixes)
        self._logical_response_baseline = logical
        self._history_before = prior
        self._native_answer_captured = False
        # Any previous checkpoint belongs to the failed attempt, whose current
        # answer ID may be replaced. Its other identities were proved above.
        self._last_correlation_payload = None
        self._last_correlation_value = None
        self._recovery_binding = None
        self._native_retry_control = probe.native_retry_control
        await self._actions.release_all()
        self._composer_target = self._send_target = None
        self._native_retry_target = await self._actions.retain(probe.native_retry_control, deadline.expires_at)
        self._native_retry_signature = expected_signature
        self._dispatch_deadline = None
        self._native_retry_prepared = True
        self._require_fresh_blank_start = False
        self._composer = None
        self._send_control = None
        self._prepared = False
        self._submitted = False
        self._prepared = True
        return True

    def restore_confirmed_attempt(
        self,
        *,
        prompt: str,
        response_baseline: Mapping[str, object],
        failure_classification: str,
        native_retry: bool = False,
        binding: Optional[Mapping[str, object]] = None,
    ) -> None:
        """Rehydrate one durable sent attempt for a strict native re-probe.

        This performs no browser action. ``prepare_native_retry`` still has to
        prove the exact prompt, response baseline, live identity, terminal
        state, and unique scoped control before it can return true.
        """

        if self._prepared or self._submitted:
            raise BrowserInteractionError(
                "cannot restore a confirmed attempt over active submission "
                "state"
            )
        prompt_text = str(prompt)
        classification = str(failure_classification or "").strip()
        if not prompt_text or not classification:
            raise BrowserInteractionError(
                "durable prompt and failure classification are required"
            )
        baseline = ResponseBaseline.from_mapping(response_baseline)
        binding = binding if binding is not None else getattr(self, "_reopened_binding", None)
        if native_retry and binding is None:
            raise BrowserInteractionError("native retry conversation binding unavailable")
        if binding is not None:
            self.restore_recovery_attempt(prompt=prompt_text,
                response_baseline=response_baseline, binding=binding)
        self._prompt = prompt_text
        self._response_baseline = baseline
        self._before_answer_count = baseline.visible_response_count
        self._before_answer_text = ""
        self._confirmed_failure_signature = classification
        self._native_retry_sent = bool(native_retry)
        # The original attempt is durably SEND_STARTED/confirmed. This flag
        # enables only the read-only native retry probe; a successful probe
        # resets it before the successor's send attempt begins.
        self._submitted = True

    async def _reconciliation_probe(
        self,
        *,
        include_retry_control: bool = False,
        _deadline: Optional[_BrowserOperationDeadline] = None,
        _baseline_override: Optional[ResponseBaseline] = None,
        _resolve_access: bool = True,
    ) -> "_ReconciliationProbe":
        if _resolve_access and self._handles_access_blockers() and _deadline is not None and not _deadline.expired():
            await self._assert_access_available(_deadline=_deadline)
        observed = await strict_page_identity(
            self.page,
            profile_id=self.profile_id,
            chrome_directory=self.chrome_directory,
            _deadline=_deadline,
        )
        identity_valid = self._accept_live_identity(observed)
        if self._recovery_binding is not None:
            conversation = await _read_conversation(self.page, self.spec, _deadline=_deadline)
            correspondence = verify_conversation_history(self._recovery_binding,
                {"identity": observed.to_mapping(), "transcript": conversation.history},
                prefixes=self.spec.conversation_url_prefixes, prompt_sha256=_text_sha256(self._prompt),
                expected_profile_id=self.profile_id, expected_chrome_directory=self.chrome_directory,
                temporary_message_id_prefixes=self.spec.temporary_message_id_prefixes)
            identity_valid = identity_valid and correspondence["correspondence_verified"] is True
            prompt_text = conversation.prompt_text
            snapshot = conversation.answer
            response_scope = conversation.response_scope
            busy = correspondence["generating"]
        else:
            prompt_text = await last_visible_text(
                self.page,
                self.spec.user_message_selectors,
                label_selectors=self.spec.rendered_user_label_selectors,
                collapse_nested=self.spec.collapse_nested_message_aliases,
                _deadline=_deadline,
            )
            response_scope = _ResponseScope()
            snapshot = await visible_answer_snapshot(
                self.page,
                self.spec.response_node_selectors or self.spec.answer_selectors,
                label_selectors=self.spec.rendered_answer_label_selectors,
                content_selectors=self.spec.response_content_selectors,
                collapse_nested=self.spec.collapse_nested_message_aliases,
                turn_owner_selector=self.spec.current_turn_owner_selector,
                _scope=response_scope,
                _deadline=_deadline,
            )
            busy = (
                bool(self.spec.stop_selectors) and await any_visible(self.page, self.spec.stop_selectors, _deadline=_deadline)
            ) or await response_turn_is_generating(self.page, self.spec, _scope=response_scope, _deadline=_deadline)
        prompt_exact = bool(self.spec.user_message_selectors) and (
            prompt_text == self._prompt
        )
        current_digest = _text_sha256(snapshot.text)
        baseline = _baseline_override or self._response_baseline
        assert baseline is not None
        response_changed_from_baseline = (
            snapshot.count > baseline.visible_response_count
            or (
                self._native_retry_sent
                and snapshot.count == baseline.visible_response_count
                and current_digest
                != baseline.newest_response_sha256
            )
        )
        new_response = bool(snapshot.text) and (
            response_changed_from_baseline
        )
        notices = await current_turn_notices(
            self.page, self.spec, self._prompt,
            _scope=response_scope, _deadline=_deadline,
        )
        usage_limit = _current_usage_limit(notices)
        if usage_limit is not None:
            self._record_usage_limit(usage_limit)
        website_failure = _current_turn_failure(notices)
        if website_failure is not None:
            new_response = False
        if website_failure == "provider_connection_interrupted":
            busy = True
        blocked_marker = await _visible_access_block_marker(
            self.page,
            self.spec,
            _deadline=_deadline,
        )
        terminal_texts = await visible_text_values_within_latest_response(
            self.page,
            (
                self.spec.response_node_selectors
                or self.spec.answer_selectors
            ),
            self.spec.terminal_error_within_response_selectors,
            _scope=response_scope,
            _deadline=_deadline,
        )
        if blocked_marker is None and response_changed_from_baseline:
            blocked_marker = next(
                (
                    marker
                    for value in terminal_texts
                    if (marker := _matching_marker(value, self.spec.blocked_markers))
                    is not None
                ),
                None,
            )
        visible_terminal_marker = next(
            (
                marker
                for value in terminal_texts
                for marker in self.spec.terminal_error_markers
                if marker.casefold() in value.casefold()
            ),
            None,
        )
        terminal_marker = (
            visible_terminal_marker
            if (
                visible_terminal_marker is not None
                and response_changed_from_baseline
            )
            else None
        )
        if terminal_marker is not None:
            # The newest post-send wrapper owns this visible website error.
            # It may be a sibling of partial/empty model content, so neither
            # failure proof nor answer suppression may depend on model text.
            new_response = False
        retry_controls = (
            await visible_items_within_latest_response(
                self.page,
                (
                    self.spec.response_node_selectors
                    or self.spec.answer_selectors
                ),
                self.spec.native_retry_within_response_selectors,
                _scope=response_scope,
                _deadline=_deadline,
            )
            if terminal_marker is not None and website_failure is None
            else ()
        )
        native_retry_control = (
            retry_controls[0]
            if include_retry_control and len(retry_controls) == 1
            else None
        )
        retryable_failure = (
            f"provider_terminal_error:{terminal_marker.casefold()}"
            if terminal_marker is not None and website_failure is None
            else None
        )
        evidence: dict[str, object] = {
            "checked_at_monotonic_seconds": time.monotonic(),
            "identity_valid": identity_valid,
            "identity": observed.to_mapping(),
            "expected_user_message_present": prompt_exact,
            "expected_prompt_sha256": _text_sha256(self._prompt),
            "response_baseline": baseline.to_mapping(),
            "visible_response_count": snapshot.count,
            "newest_response_sha256": current_digest,
            "response_changed_from_baseline": (
                response_changed_from_baseline
            ),
            "new_response": new_response,
            "busy": busy,
            "website_failure": website_failure,
            "usage_limit": usage_limit,
            "terminal_error": retryable_failure,
            "nonretryable_error": (
                f"provider_access_blocked:{blocked_marker.casefold()}"
                if blocked_marker is not None
                else None
            ),
            "native_retry_count": len(retry_controls),
            "native_retry_available": len(retry_controls) == 1,
        }
        return _ReconciliationProbe(
            evidence=evidence,
            identity_valid=identity_valid,
            prompt_exact=prompt_exact,
            answer_text=snapshot.text,
            new_response=new_response,
            busy=busy,
            retryable_failure=retryable_failure,
            nonretryable_failure=(
                str(evidence["nonretryable_error"])
                if evidence["nonretryable_error"] is not None
                else None
            ),
            native_retry_count=len(retry_controls),
            native_retry_control=native_retry_control,
            website_failure=website_failure,
        )

    def _accept_live_identity(self, observed: BrowserIdentity) -> bool:
        expected = self._identity
        try:
            expected.assert_same_tab(observed)
        except IdentityMismatchError:
            return False
        canonical = lambda ref: _canonical_reference(ref, self.spec)
        checkpoint = self._last_correlation_value
        if checkpoint is not None and canonical(checkpoint["identity"]["conversation_reference"]) != canonical(observed.conversation_reference):
            return False
        if canonical(expected.conversation_reference) == canonical(observed.conversation_reference):
            self._identity = observed
            return True
        may_bind = (
            _is_blank_provider_start_url(
                expected.conversation_reference,
                self.spec.start_url,
                aliases=self.spec.blank_start_url_aliases,
            )
            and bool(observed.conversation_reference)
            and _is_conversation_url(observed.conversation_reference, self.spec)
        )
        if may_bind:
            self._identity = observed
            return True
        return False

    def _ambiguous_reconciliation(
        self,
        reason: str,
        checks: Iterable[Mapping[str, object]],
        *,
        detail: Optional[str] = None,
        grace_seconds: float = 0.0,
    ) -> ReconciliationResult:
        return ReconciliationResult(
            disposition=ReconciliationDisposition.AMBIGUOUS,
            identity=self._identity,
            evidence=_reconciliation_evidence(
                grace_seconds,
                list(checks),
                reason=reason,
                detail=detail,
            ),
            failure_classification=reason,
            native_retry_available=False,
        )

    def next_turn(self) -> "PageSubmission":
        """Reuse this exact tab/conversation for the one permitted follow-up."""

        if not self._submitted:
            raise BrowserInteractionError(
                "cannot advance a provider conversation before its response"
            )
        self.finish_failure_observation()
        self._failure_transport = None
        self._usage_limit = None
        self._previous_turn_binding = self._last_correlation_value or self._recovery_binding
        self._composer = None
        self._send_control = None
        self._native_retry_control = None
        self._prepared = False
        self._submitted = False
        self._verification_attempts = 0
        self._native_retry_prepared = False
        self._native_retry_sent = False
        self._prompt = ""
        self._before_answer_count = 0
        self._before_answer_text = ""
        self._response_baseline = None
        self._logical_response_baseline = None
        self._native_answer_captured = False
        self._confirmed_failure_signature = None
        self._current_response_timeout_seconds = (
            self.extraction_timeout_seconds
        )
        self._require_fresh_blank_start = False
        self._recovery_binding = None
        self._correlation_checkpoint = None
        self._reopened_binding = None
        self._last_correlation_payload = None
        self._last_correlation_value = None
        self._last_history_read_monotonic = None
        return self

    def use_extraction_timeout(
        self,
        seconds: Optional[float] = None,
    ) -> None:
        """Apply the configured name listing timeout to a fresh one-turn chat."""

        if self._submitted:
            raise BrowserInteractionError(
                "cannot change the response timeout after submission started"
            )
        timeout = (
            self.extraction_timeout_seconds
            if seconds is None
            else float(seconds)
        )
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("extraction timeout must be finite and positive")
        self._current_response_timeout_seconds = timeout

    async def close(self) -> None:
        self.finish_failure_observation()
        await self._stop_conversation_watch()
        await self._actions.release_all()
        close = getattr(self.page, "close", None)
        if close is not None:
            deadline = _BrowserOperationDeadline.after(5, timeout_message="owned conversation cleanup timed out")
            desktop_close = getattr(self.human_verification_handler, "close_page", None)
            if callable(desktop_close):
                await desktop_close(self)
            else:
                await _await_call(close, _deadline=deadline)
            await _await_call(retire_closed_owned_page, self.page, _deadline=deadline)


class PageProvider:
    """Reusable exact-input browser driver for conversational websites."""

    platform: str

    def canonical_conversation_reference(self, reference: str) -> str:
        return _canonical_reference(reference, self.spec)

    def __init__(
        self,
        page_factory: PageFactory,
        spec: PageProviderSpec,
        *,
        composer_attempts: int = 3,
        response_timeout_seconds: Optional[float] = None,
        extraction_timeout_seconds: Optional[float] = None,
        interaction_min_delay_seconds: float = 0.0,
        interaction_max_delay_seconds: float = 0.0,
        cancelled: Optional[Callable[[], bool]] = None,
        human_verification_handler: Optional[Callable] = None,
    ):
        self.page_factory = page_factory
        timeout = (
            spec.response_timeout_seconds
            if response_timeout_seconds is None
            else float(response_timeout_seconds)
        )
        if timeout <= 0:
            raise ValueError("response_timeout_seconds must be positive")
        self.spec = replace(spec, response_timeout_seconds=timeout)
        self.platform = spec.platform
        self.composer_attempts = composer_attempts
        self.extraction_timeout_seconds = (
            timeout
            if extraction_timeout_seconds is None
            else float(extraction_timeout_seconds)
        )
        if self.extraction_timeout_seconds <= 0:
            raise ValueError("extraction_timeout_seconds must be positive")
        (
            self.interaction_min_delay_seconds,
            self.interaction_max_delay_seconds,
        ) = _delay_bounds(
            interaction_min_delay_seconds,
            interaction_max_delay_seconds,
        )
        self.cancelled = cancelled or (lambda: False)
        self.human_verification_handler = human_verification_handler

    async def open(
        self,
        *,
        profile_id: str,
        chrome_directory: str,
        observation_id: str,
        fresh: bool = True,
    ) -> PageSubmission:
        deadline = _BrowserOperationDeadline.after(PROVIDER_PREPARATION_TIMEOUT_SECONDS, timeout_message="provider navigation/preparation timed out")
        page = await _await_call(self.page_factory,
            self.platform,
            profile_id,
            observation_id,
            fresh,
            None,
            _deadline=deadline,
        )
        watch = getattr(self.human_verification_handler, "watch_page", None)
        if callable(watch):
            await watch(page, deadline)
        if fresh:
            await _await_call(
                getattr(page, "goto"),
                self.spec.start_url,
                wait_until="domcontentloaded",
                _deadline=deadline,
            )
        identity = await _await_call(page_identity,
            page,
            profile_id=profile_id,
            chrome_directory=chrome_directory,
            _deadline=deadline,
        )
        submission = PageSubmission(
            page=page, profile_id=profile_id, chrome_directory=chrome_directory,
            observation_id=observation_id, spec=self.spec, identity=identity,
            composer_attempts=self.composer_attempts, extraction_timeout_seconds=self.extraction_timeout_seconds,
            interaction_min_delay_seconds=self.interaction_min_delay_seconds,
            interaction_max_delay_seconds=self.interaction_max_delay_seconds,
            cancelled=self.cancelled, require_fresh_blank_start=fresh,
            human_verification_handler=self.human_verification_handler,
        )
        if submission._handles_access_blockers():
            if fresh:
                submission._identity = replace(identity, conversation_reference=self.spec.start_url)
            await submission._assert_access_available(_deadline=deadline)
            identity = await strict_page_identity(page, profile_id=profile_id,
                chrome_directory=chrome_directory, _deadline=deadline)
            submission._identity = identity
        if fresh and not _is_blank_provider_start_url(
            identity.conversation_reference,
            self.spec.start_url,
            aliases=self.spec.blank_start_url_aliases,
        ):
            raise IdentityMismatchError(
                f"{self.platform} fresh chat did not remain on the blank "
                "provider start before send: expected "
                f"{_provider_url_diagnostic(self.spec.start_url)}, observed "
                f"{_provider_url_diagnostic(identity.conversation_reference)}"
            )
        return submission

    async def restore(
        self,
        identity: BrowserIdentity,
        *,
        observation_id: str,
    ) -> Optional[PageSubmission]:
        deadline = _BrowserOperationDeadline.after(30, timeout_message="provider tab adoption timed out")
        page = await _await_call(self.page_factory,
            self.platform,
            identity.profile_id,
            observation_id,
            False,
            identity,
            _deadline=deadline,
        )
        if page is None:
            return None
        observed = await strict_page_identity(
            page,
            profile_id=identity.profile_id,
            chrome_directory=identity.chrome_directory,
            _deadline=deadline,
        )
        identity.assert_same_tab(observed,
            require_same_conversation=not callable(getattr(self.human_verification_handler, "resolve", None)),
            canonical=self.canonical_conversation_reference)
        submission = PageSubmission(
            page=page,
            profile_id=identity.profile_id,
            chrome_directory=identity.chrome_directory,
            observation_id=observation_id,
            spec=self.spec,
            identity=identity if callable(getattr(self.human_verification_handler, "resolve", None)) else observed,
            composer_attempts=self.composer_attempts,
            extraction_timeout_seconds=self.extraction_timeout_seconds,
            interaction_min_delay_seconds=(
                self.interaction_min_delay_seconds
            ),
            interaction_max_delay_seconds=(
                self.interaction_max_delay_seconds
            ),
            cancelled=self.cancelled,
            require_fresh_blank_start=False,
            human_verification_handler=self.human_verification_handler,
        )
        if submission._handles_access_blockers():
            await submission._assert_access_available(_deadline=deadline)
            observed = await strict_page_identity(page, profile_id=identity.profile_id,
                chrome_directory=identity.chrome_directory, _deadline=deadline)
            identity.assert_same_tab(observed, require_same_conversation=True,
                canonical=self.canonical_conversation_reference)
            submission._identity = observed
        return submission

    async def reopen(self, binding: Mapping[str, object], *, observation_id: str) -> PageSubmission:
        """Open a validated saved conversation in a new, exactly owned tab.

        The caller has already admitted this retrieval effect and contained
        the prior executor/resources. This method never submits a prompt.
        """
        if binding.get("schema_version") != "browser-conversation-binding-v1":
            raise ConversationCorrespondenceError("conversation_binding_unsupported")
        identity = BrowserIdentity.from_mapping(binding.get("identity", {}))
        url = canonical_conversation_url(identity.conversation_reference, self.spec.conversation_url_prefixes)
        expected_turn = logical_response_baseline(binding)["visible_response_count"] + 1
        deadline = _BrowserOperationDeadline.after(30, timeout_message="original conversation reopening timed out")
        page = await _await_call(self.page_factory, self.platform, identity.profile_id, observation_id, True, None, _deadline=deadline)
        submission = None
        try:
            observed = await strict_page_identity(page, profile_id=identity.profile_id, chrome_directory=identity.chrome_directory, _deadline=deadline)
            if (observed.profile_id != identity.profile_id or observed.chrome_directory != identity.chrome_directory):
                raise ConversationCorrespondenceError("conversation_profile_mismatch")
            effective_binding = binding
            if observed.browser_context_reference != identity.browser_context_reference:
                permitted = getattr(self.page_factory, "_restart_profiles", frozenset())
                if (not identity.browser_context_reference or not observed.browser_context_reference
                        or (identity.profile_id, identity.chrome_directory) not in permitted):
                    raise ConversationCorrespondenceError("conversation_account_context_mismatch")
                # Only the verified owned-profile factory after explicit restart
                # may replace a process-local context. Keep the saved evidence
                # unchanged and still prove URL, transcript, branch and turns.
                effective_binding = {**binding, "identity": {**binding["identity"],
                    "browser_context_reference": observed.browser_context_reference}}
            if callable(getattr(self.human_verification_handler, "resolve", None)):
                submission = PageSubmission(page=page, profile_id=identity.profile_id,
                    chrome_directory=identity.chrome_directory, observation_id=observation_id,
                    spec=self.spec, identity=replace(observed, conversation_reference=url),
                    composer_attempts=self.composer_attempts, extraction_timeout_seconds=self.extraction_timeout_seconds,
                    interaction_min_delay_seconds=self.interaction_min_delay_seconds,
                    interaction_max_delay_seconds=self.interaction_max_delay_seconds,
                    cancelled=self.cancelled, human_verification_handler=self.human_verification_handler)
                submission._previous_turn_binding = effective_binding
                submission._retrieval_only = True
                await self.human_verification_handler.watch_page(page, deadline)
            await _await_call(getattr(page, "goto"), url, wait_until="domcontentloaded", _deadline=deadline)
            if submission is not None:
                await submission._assert_access_available(_deadline=deadline)
            while True:
                if self.cancelled():
                    raise BrowserInteractionError("original conversation reopening was cancelled")
                observed = await strict_page_identity(page, profile_id=identity.profile_id, chrome_directory=identity.chrome_directory, _deadline=deadline)
                history = await _conversation_history(page, self.spec, _deadline=deadline)
                try:
                    verify_conversation_history(effective_binding, {"identity": observed.to_mapping(), "transcript": history},
                        prefixes=self.spec.conversation_url_prefixes, prompt_sha256=str(binding.get("prompt_sha256") or ""),
                        expected_profile_id=identity.profile_id, expected_chrome_directory=identity.chrome_directory,
                        temporary_message_id_prefixes=self.spec.temporary_message_id_prefixes)
                    break
                except ConversationCorrespondenceError as exc:
                    # DOMContentLoaded can precede the website's transcript.
                    # Wait for missing expected turns without accepting them.
                    if str(exc) not in {"conversation_extra_or_missing_turns", "conversation_message_or_branch_changed"}:
                        raise
                    saved_transcript = binding["transcript"]
                    if str(exc) == "conversation_message_or_branch_changed":
                        for key in ("user_ids", "assistant_ids"):
                            current = history[key]
                            for index, known in enumerate(saved_transcript.get(key, ())):
                                if (known is not None and not is_temporary_message_id(known, self.spec.temporary_message_id_prefixes)
                                        and index < len(current) and current[index] != known):
                                    raise
                    required_answers = expected_turn - 1
                    for index, known in enumerate(saved_transcript.get("assistant_ids", ())):
                        if known is not None and not is_temporary_message_id(known, self.spec.temporary_message_id_prefixes):
                            required_answers = max(required_answers, index + 1)
                    users = len(history["user_sha256"])
                    answers = len(history["assistant_sha256"])
                    if (users > expected_turn or answers > expected_turn
                            or (users >= expected_turn and answers >= required_answers)):
                        raise
                    await _await_call(asyncio.sleep, min(.2, max(0., deadline.remaining())), _deadline=deadline)
            if submission is not None:
                submission._identity = observed
                submission._retrieval_only = False
                submission._restart_recovery_binding = (binding, effective_binding)
                submission._reopened_binding = effective_binding
                return submission
            submission = PageSubmission(page=page, profile_id=identity.profile_id, chrome_directory=identity.chrome_directory,
                observation_id=observation_id, spec=self.spec, identity=observed, composer_attempts=self.composer_attempts,
                extraction_timeout_seconds=self.extraction_timeout_seconds,
                interaction_min_delay_seconds=self.interaction_min_delay_seconds,
                interaction_max_delay_seconds=self.interaction_max_delay_seconds,
                cancelled=self.cancelled, require_fresh_blank_start=False,
                human_verification_handler=self.human_verification_handler)
            await submission._assert_access_available(_deadline=deadline)
            submission._reopened_binding = effective_binding
            if effective_binding is not binding:
                submission._restart_recovery_binding = (dict(binding), effective_binding)
            return submission
        except BaseException:
            close = getattr(page, "close", None)
            if close is not None:
                try:
                    if submission is not None:
                        await submission.close()
                    else:
                        await _await_call(close, _deadline=_BrowserOperationDeadline.after(5, timeout_message="reopened conversation cleanup timed out"))
                        await retire_closed_owned_page(page)
                except BaseException:
                    pass  # runtime owns the durable unresolved-cleanup record
            raise


def _delay_bounds(
    minimum: float,
    maximum: float,
) -> tuple[float, float]:
    minimum_value = float(minimum)
    maximum_value = float(maximum)
    if minimum_value < 0 or maximum_value < minimum_value:
        raise ValueError(
            "interaction delays must be non-negative and ordered"
        )
    return minimum_value, maximum_value


def _reconciliation_evidence(
    grace_seconds: float,
    checks: Iterable[Mapping[str, object]],
    *,
    reason: str,
    detail: Optional[str] = None,
    stable_terminal_checks: int = 0,
) -> dict[str, object]:
    value: dict[str, object] = {
        "grace_seconds": float(grace_seconds),
        "reason": reason,
        "checks": [dict(check) for check in checks],
        "stable_terminal_checks": int(stable_terminal_checks),
    }
    if detail:
        value["detail"] = detail
    return value


async def strict_page_identity(
    page: object,
    *,
    profile_id: str,
    chrome_directory: str,
    _deadline: Optional[_BrowserOperationDeadline] = None,
) -> BrowserIdentity:
    """Read a live identity without accepting an unowned replacement Page."""

    is_closed = getattr(page, "is_closed", None)
    if callable(is_closed) and bool(
        await _await_call(
            is_closed,
            _deadline=_deadline,
        )
    ):
        raise IdentityMismatchError("provider tab is closed")
    if isinstance(getattr(page, "closed", None), bool) and bool(
        getattr(page, "closed")
    ):
        raise IdentityMismatchError("provider tab is closed")

    bound = _runner_owned_page_identity(
        page,
        profile_id=profile_id,
        chrome_directory=chrome_directory,
    )
    target_reference = await _page_target_reference(
        page,
        bound,
        _deadline=_deadline,
    )
    context = getattr(page, "context", None)
    context_reference = (
        _identity_value(bound, "browser_context_reference")
        if bound is not None
        else str(
            getattr(page, "browser_context_reference", "")
            or getattr(page, "browser_context_id", "")
            or getattr(context, "browser_context_reference", "")
            or getattr(context, "browser_context_id", "")
            or (f"context:{id(context)}" if context is not None else "")
        )
    )
    if not context_reference:
        raise IdentityMismatchError(
            "provider browser context identity is unavailable"
        )
    evaluate = getattr(page, "evaluate", None)
    if bound is None and not callable(evaluate):
        raise IdentityMismatchError("provider tab identity is unreadable")
    window_reference = ""
    if callable(evaluate):
        try:
            window_reference = str(
                await _await_call(
                    evaluate,
                    "() => window.name || ''",
                    _deadline=_deadline,
                )
                or ""
            )
        except _BrowserOperationDeadlineExceeded:
            raise
        except Exception as exc:
            if bound is None:
                raise IdentityMismatchError(
                    "provider tab identity read failed"
                ) from exc
    if bound is not None:
        tab_reference = _identity_value(bound, "tab_reference")
        if (
            not target_reference
            and window_reference
            and window_reference != tab_reference
        ):
            raise IdentityMismatchError(
                "provider window marker conflicts with its runner-owned tab"
            )
    else:
        tab_reference = window_reference
        if not tab_reference:
            raise IdentityMismatchError("provider tab identity is empty")
        if tab_reference.startswith(_OWNED_TAB_REFERENCE_PREFIX):
            raise IdentityMismatchError(
                "an unbound provider page cannot claim a runner-owned tab"
            )
    try:
        conversation_reference = str(getattr(page, "url", "") or "")
    except Exception as exc:
        raise IdentityMismatchError(
            "provider conversation identity read failed"
        ) from exc
    if not conversation_reference:
        raise IdentityMismatchError(
            "provider conversation identity is unavailable"
        )
    return BrowserIdentity(
        profile_id=profile_id,
        chrome_directory=chrome_directory,
        browser_context_reference=context_reference,
        tab_reference=tab_reference,
        browser_target_reference=target_reference,
        conversation_reference=conversation_reference,
    )


async def interaction_pause(
    minimum_seconds: float,
    maximum_seconds: float,
    *,
    cancelled: Optional[Callable[[], bool]] = None,
) -> None:
    """Apply explicit pre-send pacing while the send is still retry-safe."""

    minimum, maximum = _delay_bounds(
        minimum_seconds,
        maximum_seconds,
    )
    is_cancelled = cancelled or (lambda: False)
    if is_cancelled():
        raise BrowserInteractionError(
            "module cancellation interrupted pre-send pacing"
        )
    if maximum == 0:
        return
    delay = (
        minimum
        if minimum == maximum
        else random.uniform(minimum, maximum)
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + delay
    while True:
        if is_cancelled():
            raise BrowserInteractionError(
                "module cancellation interrupted pre-send pacing"
            )
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.1, remaining))


async def wait_for_stable_answer(
    page: object,
    spec: PageProviderSpec,
    *,
    before_count: int,
    before_answer: str,
    timeout_seconds: Optional[float] = None,
    cancelled: Optional[Callable[[], bool]] = None,
    allow_same_node_replacement: bool = False,
    expected_prompt: Optional[str] = None,
    _deadline: Optional[_BrowserOperationDeadline] = None,
    resolve_human_verification: Optional[Callable] = None,
    resolve_access_blocker: Optional[Callable] = None,
    usage_limit_observer: Optional[Callable] = None,
) -> str:
    deadline = _deadline or _BrowserOperationDeadline.after(
        (
            spec.response_timeout_seconds
            if timeout_seconds is None
            else float(timeout_seconds)
        ),
        timeout_message=(
            f"{spec.platform} response did not settle"
        ),
    )
    previous = ""
    stable_since: Optional[float] = None
    while not deadline.expired():
        if cancelled is not None and cancelled():
            raise BrowserInteractionError(
                f"{spec.platform} response wait was cancelled"
            )
        try:
            blocked_marker = await _visible_access_block_marker(
                page,
                spec,
                _deadline=deadline,
            )
            if blocked_marker is not None:
                if resolve_access_blocker is not None:
                    await resolve_access_blocker(_deadline=deadline)
                    previous, stable_since = "", None
                    continue
                if blocked_marker == "human_verification_required" and resolve_human_verification is not None:
                    await resolve_human_verification(deadline)
                    previous, stable_since = "", None
                    continue
                raise _access_block_error(spec.platform, blocked_marker)
            response_scope = _ResponseScope()
            notices = (
                await current_turn_notices(
                    page, spec, expected_prompt,
                    _scope=response_scope, _deadline=deadline,
                ) if expected_prompt is not None else ()
            )
            usage_limit = _current_usage_limit(notices)
            if usage_limit is not None and usage_limit_observer is not None:
                usage_limit_observer(usage_limit)
            website_failure = _current_turn_failure(notices)
            if website_failure == "provider_access_blocked:usage_limit":
                raise ProviderAccessBlockedError(
                    f"{spec.platform} displayed a usage limit blocking this request",
                    reason="usage_limit", usage_limit=usage_limit,
                )
            if website_failure == "provider_access_blocked:unusual_activity":
                raise ProviderAccessBlockedError(
                    f"{spec.platform} displayed an unusual activity block",
                    reason="unusual_activity",
                )
            if website_failure == "provider_transport_failure":
                # A failed fetch proves neither delivery nor a retry-safe
                # rejection. Inspect the existing grace window without input.
                raise TimeoutError(
                    f"{spec.platform} displayed a transport failure"
                )
            snapshot = await visible_answer_snapshot(
                page,
                spec.response_node_selectors or spec.answer_selectors,
                label_selectors=spec.rendered_answer_label_selectors,
                content_selectors=spec.response_content_selectors,
                collapse_nested=spec.collapse_nested_message_aliases,
                turn_owner_selector=spec.current_turn_owner_selector,
                _scope=response_scope,
                _deadline=deadline,
            )
            busy = (
                bool(spec.stop_selectors) and await any_visible(page, spec.stop_selectors, _deadline=deadline)
            ) or await response_turn_is_generating(page, spec, _scope=response_scope, _deadline=deadline)
            if website_failure == "provider_connection_interrupted":
                busy = True
            terminal_texts = await visible_text_values_within_latest_response(
                page,
                spec.response_node_selectors or spec.answer_selectors,
                spec.terminal_error_within_response_selectors,
                _scope=response_scope,
                _deadline=deadline,
            )
        except Exception as exc:
            if not _is_native_playwright_timeout(exc):
                raise
            # A locator's own timeout is shorter than the measurement budget.
            # Re-probe only this already-sent response under the SAME absolute
            # deadline. A failed read cannot count toward answer stability.
            previous = ""
            stable_since = None
            remaining = max(0.0, deadline.remaining())
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.2, remaining))
            continue
        count = snapshot.count
        answer = snapshot.text
        response_evidence = count > before_count or (
            allow_same_node_replacement
            and count == before_count
            and bool(answer)
            and answer != before_answer
        )
        if response_evidence:
            blocked_marker = next(
                (
                    marker
                    for value in terminal_texts
                    if (marker := _matching_marker(value, spec.blocked_markers))
                    is not None
                ),
                None,
            )
            if blocked_marker is not None:
                raise _access_block_error(spec.platform, blocked_marker)
        terminal_marker = next(
            (
                marker
                for value in terminal_texts
                for marker in spec.terminal_error_markers
                if marker.casefold() in value.casefold()
            ),
            None,
        )
        if (
            terminal_marker is not None
            and response_evidence
            and not busy
        ):
            # Website error nodes are scoped to the newest post-send wrapper,
            # independently of the model-content root selected for capture.
            raise TimeoutError(
                f"{spec.platform} displayed a terminal no-response state"
            )
        now = time.monotonic()
        if response_evidence and answer and not busy:
            if answer == previous:
                stable_since = stable_since or now
                if now - stable_since >= spec.stable_seconds:
                    return answer
            else:
                previous = answer
                stable_since = now
        else:
            stable_since = None
        remaining = max(0.0, deadline.remaining())
        if remaining <= 0:
            break
        await asyncio.sleep(min(0.2, remaining))
    raise TimeoutError(deadline.timeout_message)


async def page_identity(
    page: object,
    *,
    profile_id: str,
    chrome_directory: str,
    fallback_tab: str = "",
) -> BrowserIdentity:
    bound = _runner_owned_page_identity(
        page,
        profile_id=profile_id,
        chrome_directory=chrome_directory,
    )
    target_reference = await _page_target_reference(page, bound)
    context = getattr(page, "context", None)
    context_reference = (
        _identity_value(bound, "browser_context_reference")
        if bound is not None
        else str(
            getattr(page, "browser_context_reference", "")
            or getattr(page, "browser_context_id", "")
            or getattr(context, "browser_context_reference", "")
            or getattr(context, "browser_context_id", "")
            or f"context:{id(context)}"
        )
    )
    tab_reference = (
        _identity_value(bound, "tab_reference")
        if bound is not None
        else str(
            getattr(page, "tab_reference", "")
            or fallback_tab
            or f"visibility-runner:{uuid.uuid4().hex}"
        )
    )
    evaluate = getattr(page, "evaluate", None)
    if evaluate is not None:
        try:
            existing = await _await_call(evaluate, "() => window.name || ''")
            if existing:
                if (
                    bound is not None
                    and not target_reference
                    and str(existing) != tab_reference
                ):
                    raise IdentityMismatchError(
                        "provider window marker conflicts with its "
                        "runner-owned tab"
                    )
                if bound is None:
                    tab_reference = str(existing)
            else:
                await _await_call(
                    evaluate,
                    "(value) => { window.name = value; }",
                    tab_reference,
                )
        except IdentityMismatchError:
            raise
        except Exception:
            pass
    return BrowserIdentity(
        profile_id=profile_id,
        chrome_directory=chrome_directory,
        browser_context_reference=context_reference,
        tab_reference=tab_reference,
        browser_target_reference=target_reference,
        conversation_reference=str(getattr(page, "url", "") or ""),
    )


def _identity_value(identity: object, name: str) -> str:
    if isinstance(identity, Mapping):
        return str(identity.get(name) or "")
    return str(getattr(identity, name, "") or "")


async def _page_target_reference(
    page: object,
    bound: Optional[object],
    *,
    _deadline: Optional[_BrowserOperationDeadline] = None,
) -> str:
    expected = (
        _identity_value(bound, "browser_target_reference")
        if bound is not None
        else ""
    )
    if expected:
        observed = await _await_call(
            browser_target_id,
            page,
            _deadline=_deadline,
        )
        live = f"cdp-target:{observed}" if observed else ""
        if live != expected:
            raise IdentityMismatchError(
                "runner-owned provider tab changed CDP target identity"
            )
        return expected
    raw = str(
        getattr(page, "browser_target_reference", "")
        or getattr(page, "browser_target_id", "")
        or ""
    )
    if raw and not raw.startswith("cdp-target:"):
        return f"cdp-target:{raw}"
    return raw


def _runner_owned_page_identity(
    page: object,
    *,
    profile_id: str,
    chrome_directory: str,
) -> Optional[object]:
    """Return only a binding attached by ``OwnedTabRegistry``."""

    identity = getattr(page, _OWNED_PAGE_IDENTITY_ATTRIBUTE, None)
    if identity is None:
        return None
    for field_name, expected in (
        ("profile_id", profile_id),
        ("chrome_directory", chrome_directory),
    ):
        if _identity_value(identity, field_name) != expected:
            raise IdentityMismatchError(
                f"runner-owned provider tab changed {field_name}"
            )
    tab_reference = _identity_value(identity, "tab_reference")
    context_reference = _identity_value(
        identity,
        "browser_context_reference",
    )
    if (
        not tab_reference.startswith(_OWNED_TAB_REFERENCE_PREFIX)
        or not context_reference
    ):
        raise IdentityMismatchError(
            "runner-owned provider tab binding is incomplete"
        )
    missing = object()
    bound_context = getattr(
        page,
        _OWNED_PAGE_CONTEXT_ATTRIBUTE,
        missing,
    )
    if (
        bound_context is missing
        or bound_context is not getattr(page, "context", None)
    ):
        raise IdentityMismatchError(
            "runner-owned provider tab changed browser context"
        )
    live_context_reference = str(
        getattr(page, "browser_context_reference", "")
        or getattr(page, "browser_context_id", "")
    )
    if (
        live_context_reference
        and live_context_reference != context_reference
    ):
        raise IdentityMismatchError(
            "runner-owned provider tab changed browser context identity"
        )
    return identity


def _is_blank_provider_start_url(
    live_url: str,
    start_url: str,
    *,
    aliases: tuple[str, ...] = (),
) -> bool:
    """Match an HTTPS provider origin and new-chat route, ignoring URL extras."""

    def route(url: str) -> Optional[tuple[str, str]]:
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme.casefold() != "https"
                or not parsed.hostname
                or parsed.port not in (None, 443)
                or parsed.username is not None
                or parsed.password is not None
            ):
                return None
        except (TypeError, ValueError):
            return None
        # Remove just one optional trailing slash; a nested or double-slash
        # path must not become an allowed route through broader normalization.
        return parsed.hostname.casefold(), parsed.path.removesuffix("/")

    live = route(live_url)
    expected = route(start_url)
    if live is None or expected is None or live[0] != expected[0]:
        return False
    return live == expected or any(live == route(alias) for alias in aliases)


def _provider_url_diagnostic(url: str) -> str:
    """Describe rejected routes without logging credentials or URL extras."""

    try:
        parsed = urlsplit(url)
        scheme, host, port, path = (
            parsed.scheme, parsed.hostname, parsed.port, parsed.path,
        )
    except (TypeError, ValueError):
        return "<invalid URL>"
    return (
        f"scheme={scheme[:32]!r} host={(host or '')[:255]!r} "
        f"port={port!r} path={path[:256]!r}"
    )


__all__ = [
    "BrowserIdentity",
    "PageFactory",
    "PageProvider",
    "PageProviderSpec",
    "PageSubmission",
    "ProviderAnswer",
    "ProviderDriver",
    "ProviderRegistry",
    "ProviderStatus",
    "ReconciliationDisposition",
    "ReconciliationResult",
    "ResponseBaseline",
    "Submission",
    "TransportCounter",
    "VisibleAnswerSnapshot",
    "any_visible",
    "first_visible",
    "last_visible_text",
    "page_identity",
    "strict_page_identity",
    "visible_items",
    "visible_items_within_latest_response",
    "visible_text_values",
    "visible_text_values_within_latest_response",
    "visible_answer_snapshot",
]
