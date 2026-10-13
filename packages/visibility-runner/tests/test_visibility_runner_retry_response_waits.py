"""Response uncertainty, deadlines, cancellation and durable retry evidence."""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch
from automation_core.diagnostics.failure_capture import HTML_SAMPLE
from visibility_runner.errors import IdentityMismatchError
from visibility_runner.config import ProviderResponseTimeouts, RetryPolicy
from visibility_runner.execution.run_measurement_group import (
    MeasurementGroupWorker,
    _safe_failure_context,
)
from visibility_runner.websites.chat_actions import (
    _FailureTransportObservation,
    PageProviderSpec,
    ProviderAnswer,
    ReconciliationDisposition,
    wait_for_stable_answer,
)
from visibility_runner.storage.send_attempts import SendAttemptState, SendAttemptStore
from visibility_runner.storage.measurement_files import MeasurementFiles
from support.retries import (
    _Outcome,
    _ScriptedDriver,
    _ScriptedSubmission,
    _answer,
    _execute_raw,
    _observation,
    _policy,
    _raw_plan,
    _registry,
    _result,
    _timeout,
)


class _FailurePage:
    """Only the two read-only diagnostics are supported; input is impossible."""

    def __init__(self, *, coherence="same_document", read_error=False, delay=0):
        self.coherence, self.read_error, self.delay = coherence, read_error, delay
        self.calls = []
        self.html = "<!doctype html><html><body>synthetic-private-page</body></html>"

    def locator(self, selector):
        assert selector == "html"
        return self

    async def evaluate(self, expression, *, timeout):
        assert 0 < timeout <= 3000
        self.calls.append(expression)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.read_error:
            raise RuntimeError("synthetic-private-capture-error")
        sample = {"url": "https://chatgpt.com/c/synthetic", "timeOrigin": 123.0}
        if expression == HTML_SAMPLE:
            return {**sample, "html": self.html, "title": "private-title"}
        assert expression == "element => ({url: location.href, timeOrigin: performance.timeOrigin})"
        if self.coherence == "unconfirmed":
            return {}
        if self.coherence == "changed":
            sample["timeOrigin"] += 1
        return sample


class RetryResponseWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_open_failure_captures_original_page_without_a_send_fence(self):
        from visibility_runner.errors import ProviderAccessBlockedError
        from visibility_runner.websites import chat_actions as chat
        from visibility_runner.websites.copilot import CopilotProvider

        page = _FailurePage()
        page.url = "https://copilot.com/"
        opened = []
        identity = chat.BrowserIdentity(
            profile_id="profile_a", chrome_directory="Default",
            browser_context_reference="synthetic-context", tab_reference="synthetic-tab",
            browser_target_reference="synthetic-target", conversation_reference=page.url,
        )
        async def factory(*args):
            opened.append(page)
            return page
        async def navigate(url, **kwargs):
            page.url = url
        async def identify(*args, **kwargs):
            return identity
        async def blocked(submission, **kwargs):
            assert submission.page is page and not submission._submitted
            page.url = "https://login.live.com/synthetic-authentication"
            raise ProviderAccessBlockedError("synthetic-private-sign-in-error", reason="sign_in")
        page.goto = navigate
        provider = CopilotProvider(factory, human_verification_handler=SimpleNamespace(resolve=blocked))
        plan = _raw_plan("open-failure-run", [_observation("open-failure", "copilot")])
        plan["scope_manifest"] = {"platform_data_modes": {"copilot": "copilot_browser_same_chat_v1"}}
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(chat, "page_identity", identify), \
                    patch.object(chat, "_runner_owned_page_identity", return_value=identity), \
                    patch.object(chat.PageSubmission, "_assert_access_available", blocked):
                repository, outcome = await _execute_raw(
                    Path(temporary), plan, _registry(provider),
                )
            assert outcome.status == "needs_attention" and opened == [page]
            assert len(page.calls) == 2
            issue = outcome.issues[0]
            assert "synthetic-private" not in json.dumps(issue)
            references = issue["detail"]["failure_evidence"]
            assert len(references) == 1
            metadata = json.loads(repository.read_evidence("open-failure-run", references[0]["metadata"]))
            assert metadata["phase"] == metadata["action"] == "provider_open"
            assert metadata["fence_id"] is None and metadata["response_baseline"] is None
            assert metadata["observation_id"] == "open-failure" and metadata["coherence"] == "same_document"
            assert "synthetic-private" not in json.dumps(metadata)
            assert repository.read_evidence("open-failure-run", references[0]["html"]).decode() == page.html
            assert SendAttemptStore(repository.run_dir("open-failure-run") / "fences").iter_records() == ()

    async def test_transport_snapshot_survives_failure_capture_and_listener_cleanup(self):
        from test_failure_transport_observation import EventPage, Request

        class FailurePage(_FailurePage, EventPage):
            def __init__(self):
                _FailurePage.__init__(self)
                EventPage.__init__(self)

        page = FailurePage()
        original_submit = _ScriptedSubmission.submit_once
        original_init = _ScriptedSubmission.__init__
        observed = []

        def initialize(submission, *args, **kwargs):
            original_init(submission, *args, **kwargs)
            submission.page = page

            async def owned(deadline):
                return deadline > time.monotonic()

            submission._actions = SimpleNamespace(assert_owned=owned)

        async def submit(submission):
            observer = _FailureTransportObservation(page, "https://chatgpt.com/")
            submission.failure_transport_snapshot = observer.snapshot
            submission.finish_failure_observation = observer.finish
            observed.append(observer)
            observer.start()
            await original_submit(submission)
            request = Request(page)
            page.emit("request", request)
            page.emit("response", SimpleNamespace(request=request, status=429))
            page.emit("requestfailed", request)

        with tempfile.TemporaryDirectory() as temporary:
            driver = _ScriptedDriver("chatgpt", {"transport-failure": ((_Outcome(wait_error="synthetic-private-original-error"),),)})
            with patch.object(_ScriptedSubmission, "__init__", initialize), patch.object(_ScriptedSubmission, "submit_once", submit):
                repository, outcome = await _execute_raw(Path(temporary), _raw_plan("transport-failure-run", [_observation("transport-failure", "chatgpt")]), _registry(driver))
            assert outcome.status == "needs_attention"
            assert len(driver.sent) == 1 and driver.native_retries == []
            assert not any(page.listeners.values())
            reference = outcome.issues[0]["detail"]["failure_evidence"][0]["metadata"]
            metadata = json.loads(repository.read_evidence("transport-failure-run", reference))
            assert metadata["transport"] == observed[0].snapshot()
            assert metadata["transport"]["events"][1]["status"] == 429
            assert "synthetic-private" not in json.dumps(metadata["transport"])
            fence = SendAttemptStore(repository.run_dir("transport-failure-run") / "fences").load("transport-failure:turn-1")
            assert fence.state is SendAttemptState.SEND_STARTED
            assert fence.retry_decision is None and fence.retried_by is None

    async def test_failure_dom_is_private_correlated_and_never_resends(self):
        original_init = _ScriptedSubmission.__init__
        original_write = MeasurementFiles.write_evidence
        for mode in ("same_document", "changed", "unconfirmed", "read_failed", "ownership_lost", "storage_failed"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                page = _FailurePage(coherence=mode, read_error=mode == "read_failed")
                dialog_message = "original-provider-dialog-" + "x" * 5000
                marker = "browser_dialog" + "x" * 100
                def forbidden_dialog_input():
                    raise AssertionError("diagnostic capture must not act on a browser dialog")
                page._visibility_runner_blocking_dialog = SimpleNamespace(
                    type="alert" + "x" * 50, message=dialog_message,
                    accept=forbidden_dialog_input, dismiss=forbidden_dialog_input,
                )

                def initialize(submission, *args, **kwargs):
                    original_init(submission, *args, **kwargs)
                    submission.page = page
                    submission._last_access_block_marker = marker

                    async def owned(deadline):
                        assert deadline > time.monotonic()
                        if mode == "ownership_lost":
                            raise IdentityMismatchError("synthetic-private-owner-error")
                        return True

                    submission._actions = SimpleNamespace(assert_owned=owned)

                def write(repository, *args, **kwargs):
                    if mode == "storage_failed" and kwargs.get("suffix", "").startswith(".failure"):
                        raise OSError("synthetic-private-storage-error")
                    return original_write(repository, *args, **kwargs)

                driver = _ScriptedDriver("chatgpt", {"dom-failure": ((_Outcome(wait_error="synthetic-private-original-error"),),)})
                with patch.object(_ScriptedSubmission, "__init__", initialize), patch.object(MeasurementFiles, "write_evidence", write):
                    repository, outcome = await _execute_raw(
                        Path(temporary), _raw_plan("dom-failure-run", [_observation("dom-failure", "chatgpt")]), _registry(driver),
                    )
                self.assertEqual(outcome.status, "needs_attention")
                self.assertEqual(len(driver.sent), 1)
                self.assertEqual(len(driver.closed), 0)  # Uncertain targets stay retained.
                fence = SendAttemptStore(repository.run_dir("dom-failure-run") / "fences").load("dom-failure:turn-1")
                self.assertIs(fence.state, SendAttemptState.SEND_STARTED)
                self.assertIsNone(fence.retry_decision)
                self.assertIsNone(fence.response_reference)
                issue = outcome.issues[0]
                self.assertEqual(issue["detail"]["failure_context"]["cause_type"], "RuntimeError")
                self.assertNotIn("synthetic-private", json.dumps(issue))
                self.assertNotIn("original-provider-dialog-", json.dumps(issue))
                references = issue["detail"].get("failure_evidence", [])
                if mode in {"ownership_lost", "storage_failed"}:
                    self.assertEqual(references, [])
                    if mode == "ownership_lost":
                        self.assertEqual(page.calls, [])
                    continue
                self.assertEqual(len(references), 1)
                metadata = json.loads(repository.read_evidence("dom-failure-run", references[0]["metadata"]))
                self.assertEqual(metadata["fence_id"], "dom-failure:turn-1")
                self.assertEqual(metadata["packet_id"], "packet-1")
                self.assertEqual(metadata["phase"], "response_wait")
                self.assertNotIn("synthetic-private", json.dumps(metadata))
                self.assertEqual(metadata["browser_dialog"], {
                    "type": ("alert" + "x" * 50)[:40], "message": dialog_message[:4000],
                })
                self.assertEqual(metadata["last_access_block_marker"], marker[:80])
                if mode == "read_failed":
                    self.assertNotIn("html", references[0])
                    self.assertEqual(metadata["coherence"], "unconfirmed")
                    self.assertEqual(metadata["html_status"], "unavailable")
                else:
                    self.assertEqual(metadata["coherence"], mode)
                    self.assertEqual(repository.read_evidence("dom-failure-run", references[0]["html"]).decode(), page.html)
                for reference in references[0].values():
                    path = repository.run_dir("dom-failure-run") / reference["path"]
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    async def test_timeout_capture_precedes_reconciliation_and_consumes_original_grace(self):
        original_init = _ScriptedSubmission.__init__
        original_reconcile = _ScriptedSubmission.reconcile_no_response
        page = _FailurePage(delay=0.01)
        observed_grace = []

        def initialize(submission, *args, **kwargs):
            original_init(submission, *args, **kwargs)
            submission.page = page

            async def owned(deadline):
                return deadline > time.monotonic()

            submission._actions = SimpleNamespace(assert_owned=owned)

        async def reconcile(submission, grace_seconds):
            self.assertEqual(len(page.calls), 2)
            observed_grace.append(grace_seconds)
            return await original_reconcile(submission, grace_seconds)

        with tempfile.TemporaryDirectory() as temporary:
            driver = _ScriptedDriver("chatgpt", {"capture-timeout": ((_timeout(disposition=ReconciliationDisposition.AMBIGUOUS),),)})
            policy = RetryPolicy(enabled=True, max_attempts=2, late_response_grace_seconds=0.2, backoff_seconds=0)
            with patch.object(_ScriptedSubmission, "__init__", initialize), patch.object(_ScriptedSubmission, "reconcile_no_response", reconcile):
                repository, outcome = await _execute_raw(
                    Path(temporary), _raw_plan("capture-timeout-run", [_observation("capture-timeout", "chatgpt")], retry_policy=policy), _registry(driver),
                )
            self.assertEqual(outcome.status, "needs_attention")
            self.assertEqual(len(driver.sent), 1)
            self.assertEqual(len(driver.reconciled), 1)
            self.assertGreater(observed_grace[0], 0)
            self.assertLess(observed_grace[0], 0.18)
            phases = [json.loads(repository.read_evidence("capture-timeout-run", ref["metadata"]))["phase"] for ref in outcome.issues[0]["detail"]["failure_evidence"]]
            self.assertEqual(phases, ["response_wait", "response_reconciliation"])

    async def test_gemini_first_no_response_then_fresh_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            observation = _observation("gemini-retry", "gemini")
            driver = _ScriptedDriver(
                "gemini",
                {
                    "gemini-retry": (
                        (_timeout(),),
                        (
                            _answer("Beta Clinic is nearby."),
                            _answer('["Beta Clinic"]'),
                        ),
                    )
                },
            )
            repository, outcome = await _execute_raw(
                Path(temporary),
                _raw_plan("gemini-retry-run", [observation]),
                _registry(driver),
            )

            self.assertEqual(outcome.status, "succeeded")
            self.assertEqual(len(driver.opens), 2)
            self.assertEqual(len(driver.sent), 3)
            result = _result(
                repository,
                "gemini-retry-run",
                "gemini-retry",
            )
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(
                result["accepted_attempt_ids"]["measurement_turn_1"],
                "gemini-retry:turn-1:attempt-2",
            )

    async def test_late_response_is_captured_without_resending_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            observation = _observation("late-response", "chatgpt")
            driver = _ScriptedDriver(
                "chatgpt",
                {
                    "late-response": (
                        (
                            _timeout(
                                disposition=(
                                    ReconciliationDisposition.LATE_RESPONSE_CAPTURED
                                ),
                                failure_classification="late_response",
                                late_text="Late Clinic eventually appeared.",
                            ),
                            _answer('["Late Clinic"]'),
                        ),
                    )
                },
            )
            repository, outcome = await _execute_raw(
                Path(temporary),
                _raw_plan("late-response-run", [observation]),
                _registry(driver),
            )

            self.assertEqual(outcome.status, "succeeded")
            self.assertEqual(len(driver.opens), 1)
            self.assertEqual(len(driver.sent), 2)
            measurement_sends = [
                row
                for row in driver.sent
                if row[3] == "Recommend a clinic"
            ]
            self.assertEqual(len(measurement_sends), 1)
            fences = SendAttemptStore(
                repository.run_dir("late-response-run") / "fences"
            )
            first = fences.load("late-response:turn-1")
            self.assertIs(first.state, SendAttemptState.ACKNOWLEDGED)
            self.assertEqual(first.failure_classification, "late_response")
            self.assertIsNotNone(first.reconciliation_evidence)

    async def test_visible_terminal_error_persists_retry_authorization(self):
        with tempfile.TemporaryDirectory() as temporary:
            observation = _observation("terminal-error", "chatgpt")
            driver = _ScriptedDriver(
                "chatgpt",
                {
                    "terminal-error": (
                        (
                            _timeout(
                                failure_classification=(
                                    "visible_provider_terminal_error"
                                )
                            ),
                        ),
                        (
                            _answer("Gamma Clinic responded."),
                            _answer('["Gamma Clinic"]'),
                        ),
                    )
                },
            )
            repository, outcome = await _execute_raw(
                Path(temporary),
                _raw_plan("terminal-error-run", [observation]),
                _registry(driver),
            )

            self.assertEqual(outcome.status, "succeeded")
            first = SendAttemptStore(
                repository.run_dir("terminal-error-run") / "fences"
            ).load("terminal-error:turn-1")
            self.assertIs(first.state, SendAttemptState.CONFIRMED_NO_RESPONSE)
            self.assertEqual(
                first.failure_classification,
                "visible_provider_terminal_error",
            )
            self.assertEqual(first.retry_decision, "retry_authorized")
            self.assertEqual(
                first.retried_by,
                "terminal-error:turn-1:attempt-2",
            )

    async def test_changed_or_lost_tab_remains_needs_attention(self):
        with tempfile.TemporaryDirectory() as temporary:
            observation = _observation("lost-tab", "gemini")
            driver = _ScriptedDriver(
                "gemini",
                {
                    "lost-tab": (
                        (
                            _timeout(
                                changed_identity=True,
                                failure_classification=(
                                    "browser_identity_changed"
                                ),
                            ),
                        ),
                    )
                },
            )
            repository, outcome = await _execute_raw(
                Path(temporary),
                _raw_plan("lost-tab-run", [observation]),
                _registry(driver),
            )

            self.assertEqual(outcome.status, "needs_attention")
            self.assertEqual(len(driver.sent), 1)
            self.assertEqual(
                repository.iter_observation_results("lost-tab-run"),
                [],
            )
            fence = SendAttemptStore(
                repository.run_dir("lost-tab-run") / "fences"
            ).load("lost-tab:turn-1")
            self.assertIs(fence.state, SendAttemptState.SEND_STARTED)
            self.assertIsNone(fence.retried_by)

    async def test_never_returning_browser_wait_is_bounded_and_not_retried(
        self,
    ):
        response_timeout = 0.02
        external_watchdog = 0.15

        class NeverReturningResponse:
            def __init__(self) -> None:
                self.read_calls = 0
                self.cancelled = False

            async def count(self) -> int:
                self.read_calls += 1
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
                raise AssertionError(
                    "the never-returning browser read resumed"
                )

        class NeverReturningPage:
            def __init__(self) -> None:
                self.response = NeverReturningResponse()

            def locator(self, selector: str) -> NeverReturningResponse:
                self.assert_response_selector(selector)
                return self.response

            @staticmethod
            def assert_response_selector(selector: str) -> None:
                if selector != "answer":
                    raise AssertionError(
                        f"unexpected locator after hung response read: {selector}"
                    )

        page = NeverReturningPage()
        spec = PageProviderSpec(
            platform="chatgpt",
            start_url="https://chatgpt.com",
            composer_selectors=("composer",),
            send_selectors=("send",),
            answer_selectors=("answer",),
            response_timeout_seconds=response_timeout,
            stable_seconds=0.0,
        )

        class NeverReturningSubmission(_ScriptedSubmission):
            async def wait_response(self) -> ProviderAnswer:
                if not self.submitted:
                    raise AssertionError(
                        "scripted response requested before send"
                    )
                self.driver.waited.append(
                    (
                        self.observation_id,
                        self.session_ordinal,
                        self.step,
                    )
                )
                started = time.monotonic()
                try:
                    await wait_for_stable_answer(
                        page,
                        spec,
                        before_count=0,
                        before_answer="",
                    )
                finally:
                    self.driver.response_wait_elapsed = (
                        time.monotonic() - started
                    )
                raise AssertionError(
                    "never-returning response wait unexpectedly completed"
                )

        class NeverReturningDriver(_ScriptedDriver):
            response_wait_elapsed: Optional[float] = None

            async def open(
                self,
                *,
                profile_id: str,
                chrome_directory: str,
                observation_id: str,
                fresh: bool = True,
            ) -> NeverReturningSubmission:
                base = await super().open(
                    profile_id=profile_id,
                    chrome_directory=chrome_directory,
                    observation_id=observation_id,
                    fresh=fresh,
                )
                submission = NeverReturningSubmission(
                    self,
                    observation_id,
                    base.identity,
                    base.steps,
                    base.session_ordinal,
                )
                self.active[base.identity.tab_reference] = submission
                return submission

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observation = _observation(
                "never-returning-browser-wait",
                "chatgpt",
            )
            policy = _policy(
                max_attempts=2,
                response_timeouts=ProviderResponseTimeouts(
                    chatgpt_turn_1_seconds=response_timeout,
                ),
            )
            plan = _raw_plan(
                "never-returning-browser-wait-run",
                [observation],
                retry_policy=policy,
            )
            driver = NeverReturningDriver(
                "chatgpt",
                {
                    "never-returning-browser-wait": (
                        (
                            _timeout(
                                disposition=(
                                    ReconciliationDisposition.AMBIGUOUS
                                ),
                                failure_classification=(
                                    "provider_state_uncertain"
                                ),
                            ),
                        ),
                    )
                },
            )
            providers = _registry(driver)
            execution = asyncio.create_task(
                _execute_raw(root, plan, providers)
            )
            watchdog_fired = False
            try:
                repository, first = await asyncio.wait_for(
                    asyncio.shield(execution),
                    timeout=external_watchdog,
                )
            except TimeoutError:
                watchdog_fired = not execution.done()
                if watchdog_fired:
                    execution.cancel()
                    try:
                        await execution
                    except asyncio.CancelledError:
                        pass
                raise

            self.assertFalse(watchdog_fired)
            self.assertEqual(first.status, "needs_attention")
            self.assertIsNotNone(driver.response_wait_elapsed)
            self.assertGreaterEqual(
                driver.response_wait_elapsed,
                response_timeout / 2,
            )
            self.assertLess(
                driver.response_wait_elapsed,
                external_watchdog / 2,
            )
            self.assertEqual(page.response.read_calls, 1)
            self.assertTrue(page.response.cancelled)
            self.assertEqual(len(driver.prepared), 1)
            self.assertEqual(len(driver.sent), 1)
            self.assertEqual(len(driver.opens), 1)
            self.assertEqual(driver.native_retry_probes, [])
            self.assertEqual(driver.native_retries, [])
            self.assertEqual(driver.next_turns, [])

            counts = first.transport_counts["measurement_turn_1"]
            self.assertEqual(
                counts,
                {
                    "prepared": 1,
                    "send_started": 1,
                    "responses": 0,
                    "acknowledged": 0,
                    "retry_count": 0,
                    "exhausted_retry_count": 0,
                },
            )
            fence = SendAttemptStore(
                repository.run_dir(
                    "never-returning-browser-wait-run"
                )
                / "fences"
            ).load("never-returning-browser-wait:turn-1")
            self.assertIsNotNone(fence)
            self.assertIs(fence.state, SendAttemptState.SEND_STARTED)
            self.assertEqual(fence.attempt_ordinal, 1)
            self.assertEqual(
                fence.retry_decision,
                "needs_attention",
            )
            self.assertEqual(
                fence.failure_classification,
                "provider_state_uncertain",
            )
            self.assertIsNotNone(fence.reconciliation_evidence)
            self.assertIsNone(fence.response_reference)
            self.assertIsNone(fence.retried_from)
            self.assertIsNone(fence.retried_by)

            sent_before_resume = tuple(driver.sent)
            prepared_before_resume = tuple(driver.prepared)
            _, resumed = await _execute_raw(
                root,
                plan,
                providers,
                resume=True,
            )
            self.assertEqual(resumed.status, "needs_attention")
            self.assertEqual(tuple(driver.sent), sent_before_resume)
            self.assertEqual(
                tuple(driver.prepared),
                prepared_before_resume,
            )
            self.assertEqual(
                resumed.transport_counts["measurement_turn_1"],
                counts,
            )

    async def test_durable_cancellation_bounds_suppressing_provider_task(
        self,
    ):
        started = asyncio.Event()
        cancellation_received = asyncio.Event()
        release = asyncio.Event()
        detached_done = asyncio.Event()
        late_loop_errors: list[dict[str, object]] = []

        class SuppressingSubmission(_ScriptedSubmission):
            async def prepare_exact(
                self,
                prompt,
                *,
                on_pre_send_failure=None,
                timeout_seconds=None,
            ):
                del on_pre_send_failure, timeout_seconds
                self.current_prompt = str(prompt)
                self.driver.prepared.append(
                    (
                        self.observation_id,
                        self.session_ordinal,
                        self.step,
                        prompt,
                        self._identity.tab_reference,
                    )
                )
                started.set()
                try:
                    while not release.is_set():
                        try:
                            await release.wait()
                        except asyncio.CancelledError:
                            cancellation_received.set()
                            continue
                    raise RuntimeError(
                        "late provider failure after cancellation"
                    )
                finally:
                    detached_done.set()

        class SuppressingDriver(_ScriptedDriver):
            async def open(
                self,
                *,
                profile_id: str,
                chrome_directory: str,
                observation_id: str,
                fresh: bool = True,
            ) -> SuppressingSubmission:
                base = await super().open(
                    profile_id=profile_id,
                    chrome_directory=chrome_directory,
                    observation_id=observation_id,
                    fresh=fresh,
                )
                submission = SuppressingSubmission(
                    self,
                    observation_id,
                    base.identity,
                    base.steps,
                    base.session_ordinal,
                )
                self.active[base.identity.tab_reference] = submission
                return submission

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_id = "bounded-suppressing-cancellation"
            observations = [
                _observation("cancel-suppress-first", "chatgpt"),
                _observation("cancel-suppress-peer", "chatgpt"),
            ]
            plan = _raw_plan(
                run_id,
                observations,
                retry_policy=RetryPolicy.disabled(),
            )
            driver = SuppressingDriver(
                "chatgpt",
                {
                    observation["observation_id"]: (
                        (
                            _answer("unused"),
                            _answer('["unused"]'),
                        ),
                    )
                    for observation in observations
                },
            )
            repository = MeasurementFiles(root)
            repository.create(plan)
            worker = MeasurementGroupWorker(
                repository,
                _registry(driver),
                cancellation_settlement_seconds=0.02,
            )
            stored_plan = repository.read_plan(run_id)
            packet = repository.read_measurement_group(
                run_id,
                str(stored_plan["packets"][0]["packet_id"]),
            )
            loop = asyncio.get_running_loop()
            prior_handler = loop.get_exception_handler()
            loop.set_exception_handler(
                lambda _loop, context: late_loop_errors.append(
                    dict(context)
                )
            )
            try:
                execution = asyncio.create_task(
                    worker.execute(
                        run_id=run_id,
                        plan=stored_plan,
                        packet=packet,
                    )
                )
                await asyncio.wait_for(started.wait(), timeout=0.2)

                def cancel(state):
                    state["cancel_requested"] = True
                    state["status"] = "cancelled"
                    state["issues"] = [{"code": "cancel_sentinel"}]

                repository.update_state(run_id, cancel)
                cancel_started = time.monotonic()
                await asyncio.wait_for(
                    cancellation_received.wait(),
                    timeout=0.15,
                )
                settlement_observed = time.monotonic()
                outcome = await asyncio.wait_for(
                    execution,
                    timeout=0.2,
                )
                cancellation_elapsed = time.monotonic() - cancel_started
                settlement_elapsed = (
                    time.monotonic() - settlement_observed
                )

                # Total latency includes the worker's 50 ms durable-cancel
                # poll. Once cancellation reaches the website coroutine,
                # settlement itself is bounded by the configured allowance
                # plus only a small scheduler tolerance.
                self.assertLess(
                    cancellation_elapsed,
                    0.05
                    + worker.cancellation_settlement_seconds
                    + 0.08,
                )
                self.assertLess(
                    settlement_elapsed,
                    worker.cancellation_settlement_seconds + 0.03,
                )
                self.assertEqual(outcome.status, "cancelled")
                self.assertEqual(
                    [row[0] for row in driver.opens],
                    ["cancel-suppress-first"],
                )
                self.assertEqual(
                    [row[0] for row in driver.prepared],
                    ["cancel-suppress-first"],
                )
                self.assertEqual(driver.sent, [])
                records = SendAttemptStore(
                    repository.run_dir(run_id) / "fences"
                ).iter_records()
                self.assertEqual(len(records), 1)
                self.assertEqual(
                    records[0].observation_id,
                    "cancel-suppress-first",
                )
                self.assertIs(
                    records[0].state,
                    SendAttemptState.PRE_SEND_FAILED,
                )
                state = repository.read_progress(run_id)
                self.assertEqual(state["status"], "cancelled")
                self.assertEqual(
                    state["issues"],
                    [{"code": "cancel_sentinel"}],
                )
                packet_state = repository.read_group_progress(
                    run_id,
                    str(packet["packet_id"]),
                )
                self.assertEqual(packet_state["status"], "planned")

                release.set()
                await asyncio.wait_for(detached_done.wait(), timeout=0.2)
                await asyncio.sleep(0)
                self.assertEqual(late_loop_errors, [])
                self.assertEqual(driver.sent, [])
                self.assertEqual(
                    len(
                        SendAttemptStore(
                            repository.run_dir(run_id) / "fences"
                        ).iter_records()
                    ),
                    1,
                )
            finally:
                release.set()
                loop.set_exception_handler(prior_handler)

    async def test_post_send_response_wait_error_remains_needs_attention(self):
        with tempfile.TemporaryDirectory() as temporary:
            observation = _observation(
                "response-wait-error",
                "gemini",
            )
            driver = _ScriptedDriver(
                "gemini",
                {
                    "response-wait-error": (
                        (
                            _Outcome(
                                wait_error="provider DOM became unreadable"
                            ),
                        ),
                    )
                },
            )
            repository, outcome = await _execute_raw(
                Path(temporary),
                _raw_plan("response-wait-error-run", [observation]),
                _registry(driver),
            )

            self.assertEqual(outcome.status, "needs_attention")
            self.assertEqual(len(driver.sent), 1)
            fence = SendAttemptStore(
                repository.run_dir("response-wait-error-run") / "fences"
            ).load("response-wait-error:turn-1")
            self.assertIs(fence.state, SendAttemptState.SEND_STARTED)
            self.assertIsNone(fence.retry_decision)
            self.assertIsNone(fence.retried_by)

    async def test_post_send_failure_metadata_is_structural_and_fail_closed(self):
        private_detail = "synthetic-private-prompt-and-response-detail"
        for method, phase in (
            ("submit_once", "provider_submission"),
            ("wait_response", "response_wait"),
        ):
            with (
                self.subTest(phase=phase),
                tempfile.TemporaryDirectory() as temporary,
            ):
                observation_id = "private-failure"
                run_id = "private-failure-run"
                observations = [
                    _observation(observation_id, "gemini"),
                    _observation("forbidden-peer", "gemini"),
                ]
                driver = _ScriptedDriver(
                    "gemini",
                    {observation_id: ((_answer("Synthetic answer."),),)},
                )
                original = getattr(_ScriptedSubmission, method)

                async def fail_after_action(submission):
                    await original(submission)
                    raise RuntimeError(private_detail, {"private": private_detail})

                with patch.object(
                    _ScriptedSubmission,
                    method,
                    fail_after_action,
                ):
                    repository, outcome = await _execute_raw(
                        Path(temporary),
                        _raw_plan(run_id, observations),
                        _registry(driver),
                    )

                self.assertEqual(outcome.status, "needs_attention")
                self.assertEqual(outcome.completed_observation_ids, ())
                self.assertEqual(len(driver.sent), 1)
                self.assertEqual(len(driver.opens), 1)
                issue = outcome.issues[0]
                self.assertEqual(issue["code"], "ambiguous_send")
                self.assertFalse(issue["recoverable"])
                self.assertEqual(
                    issue["detail"]["failure_context"],
                    {
                        "phase": phase,
                        "exception_type": "AmbiguousSendError",
                        "cause_type": "RuntimeError",
                        "origin_file": Path(__file__).name,
                        "origin_module": __name__,
                        "origin_function": "fail_after_action",
                        "origin_line": str(fail_after_action.__code__.co_firstlineno + 2),
                    },
                )
                state = repository.read_group_progress(run_id, "packet-1")
                self.assertEqual(state["issues"], list(outcome.issues))
                self.assertNotIn(private_detail, json.dumps(state))
                records = SendAttemptStore(
                    repository.run_dir(run_id) / "fences"
                ).iter_records()
                self.assertEqual(len(records), 1)
                self.assertIs(records[0].state, SendAttemptState.SEND_STARTED)
                self.assertIsNone(records[0].failure_classification)
                self.assertIsNone(records[0].response_reference)
                self.assertIsNone(records[0].retry_decision)
                self.assertIsNone(records[0].retried_by)
                counts = outcome.transport_counts["measurement_turn_1"]
                self.assertEqual(counts["send_started"], 1)
                self.assertEqual(counts["responses"], 0)
                self.assertEqual(counts["acknowledged"], 0)
                self.assertEqual(counts["retry_count"], 0)

    def test_failure_metadata_does_not_serialize_unusual_error_class_names(self):
        private_error = type("synthetic private detail", (Exception,), {})
        error = private_error("more synthetic private detail")
        self.assertEqual(
            _safe_failure_context(error, phase="response_capture"),
            {
                "phase": "response_capture",
                "exception_type": "UnclassifiedError",
            },
        )

    async def test_packet_deadline_during_submit_propagates_after_send_start(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observations = [
                _observation("submit-deadline", "chatgpt"),
                _observation("submit-deadline-peer", "chatgpt"),
            ]
            plan = _raw_plan(
                "submit-deadline-run",
                observations,
                retry_policy=RetryPolicy.disabled(),
            )
            driver = _ScriptedDriver(
                "chatgpt",
                {
                    "submit-deadline": (
                        (
                            _answer(
                                "The click completed before the call hung.",
                                submit_delay_seconds=0.2,
                            ),
                        ),
                    ),
                    "submit-deadline-peer": (
                        (
                            _answer("Peer Clinic answered."),
                            _answer('["Peer Clinic"]'),
                        ),
                    ),
                },
            )
            repository = MeasurementFiles(root)
            repository.create(plan)

            worker_task = asyncio.create_task(
                MeasurementGroupWorker(
                    repository,
                    _registry(driver),
                ).execute(
                    run_id="submit-deadline-run",
                    plan=repository.read_plan(
                        "submit-deadline-run"
                    ),
                    packet=repository.read_measurement_group(
                        "submit-deadline-run",
                        "packet-1",
                    ),
                )
            )

            async def wait_until_submit_started() -> None:
                while not driver.sent:
                    await asyncio.sleep(0)

            await asyncio.wait_for(
                wait_until_submit_started(),
                timeout=1,
            )
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    worker_task,
                    timeout=0.01,
                )

            self.assertEqual(
                [row[0] for row in driver.sent],
                ["submit-deadline"],
            )
            fence = SendAttemptStore(
                repository.run_dir("submit-deadline-run") / "fences"
            ).load("submit-deadline:turn-1")
            self.assertIs(fence.state, SendAttemptState.SEND_STARTED)
            self.assertIsNone(fence.retry_decision)
            self.assertIsNone(fence.retried_by)

    async def test_packet_deadline_during_reconciliation_propagates_ambiguity(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observations = [
                _observation("reconcile-deadline", "gemini"),
                _observation("reconcile-deadline-peer", "gemini"),
            ]
            plan = _raw_plan(
                "reconcile-deadline-run",
                observations,
                retry_policy=RetryPolicy.disabled(),
            )
            driver = _ScriptedDriver(
                "gemini",
                {
                    "reconcile-deadline": (
                        (
                            _timeout(
                                reconciliation_delay_seconds=0.2,
                            ),
                        ),
                    ),
                    "reconcile-deadline-peer": (
                        (
                            _answer("Peer Clinic answered."),
                            _answer('["Peer Clinic"]'),
                        ),
                    ),
                },
            )
            repository = MeasurementFiles(root)
            repository.create(plan)

            worker_task = asyncio.create_task(
                MeasurementGroupWorker(
                    repository,
                    _registry(driver),
                ).execute(
                    run_id="reconcile-deadline-run",
                    plan=repository.read_plan(
                        "reconcile-deadline-run"
                    ),
                    packet=repository.read_measurement_group(
                        "reconcile-deadline-run",
                        "packet-1",
                    ),
                )
            )

            async def wait_until_reconciliation_started() -> None:
                while not driver.waited:
                    await asyncio.sleep(0)

            await asyncio.wait_for(
                wait_until_reconciliation_started(),
                timeout=1,
            )
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    worker_task,
                    timeout=0.01,
                )

            self.assertEqual(
                [row[0] for row in driver.sent],
                ["reconcile-deadline"],
            )
            fence = SendAttemptStore(
                repository.run_dir("reconcile-deadline-run") / "fences"
            ).load("reconcile-deadline:turn-1")
            self.assertIs(fence.state, SendAttemptState.SEND_STARTED)
            self.assertEqual(fence.retry_decision, "needs_attention")
            self.assertEqual(
                fence.failure_classification,
                "reconciliation_cancelled",
            )
            self.assertIsNone(fence.retried_by)

    async def test_missing_durable_response_baseline_remains_needs_attention(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            observation = _observation(
                "missing-baseline",
                "chatgpt",
            )
            driver = _ScriptedDriver(
                "chatgpt",
                {
                    "missing-baseline": (
                        (_timeout(),),
                    )
                },
                provide_response_baseline=False,
            )
            repository, outcome = await _execute_raw(
                Path(temporary),
                _raw_plan("missing-baseline-run", [observation]),
                _registry(driver),
            )

            self.assertEqual(outcome.status, "needs_attention")
            self.assertEqual(len(driver.sent), 1)
            self.assertEqual(len(driver.reconciled), 0)
            fence = SendAttemptStore(
                repository.run_dir("missing-baseline-run") / "fences"
            ).load("missing-baseline:turn-1")
            self.assertIs(fence.state, SendAttemptState.SEND_STARTED)
            self.assertEqual(fence.retry_decision, "needs_attention")
            self.assertEqual(
                fence.failure_classification,
                "response_baseline_unproven",
            )
