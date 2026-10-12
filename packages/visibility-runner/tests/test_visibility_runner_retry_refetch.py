"""Immediate confirmed-failure Refetch remains within frozen allowance."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from visibility_runner.config import RetryPolicy
from visibility_runner.gaio_compatibility import GAIO_GENERATED_ANSWER_CAPTURE_VERSION
from visibility_runner.names.parse_source_answer import source_parser_prompt
from visibility_runner.events import EventKind
from visibility_runner.websites.chat_actions import ReconciliationDisposition
from visibility_runner.storage.send_attempts import SendAttemptState, SendAttemptStore
from support.retries import (
    _ScriptedDriver,
    _answer,
    _execute_raw,
    _observation,
    _prepare_failure,
    _raw_plan,
    _registry,
    _result,
    _timeout,
)


class RetryRefetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_strict_json_rejection_is_refetched_in_a_fresh_chat(self):
        # Founder decision 2026-09-10: a confirmed rejection starts the chat
        # again from scratch for the same prompt while the frozen limit
        # allows it. The rejected attempt stays on disk as evidence.
        with tempfile.TemporaryDirectory() as temporary:
            observation = _observation("strict-rejection", "chatgpt")
            driver = _ScriptedDriver(
                "chatgpt",
                {
                    "strict-rejection": (
                        (
                            _answer("Zeta Clinic is nearby."),
                            _answer('["Zeta Clinic"] trailing prose'),
                        ),
                        (
                            _answer("Zeta Clinic is nearby."),
                            _answer('["Zeta Clinic"]'),
                        ),
                    )
                },
            )
            repository, outcome = await _execute_raw(
                Path(temporary),
                _raw_plan("strict-rejection-run", [observation]),
                _registry(driver),
            )

            self.assertEqual(outcome.status, "succeeded")
            self.assertEqual(len(driver.opens), 2)
            self.assertEqual(len(driver.sent), 4)
            self.assertEqual(
                [row[3] for row in driver.sent],
                [
                    "Recommend a clinic",
                    source_parser_prompt("Zeta Clinic is nearby."),
                    "Recommend a clinic",
                    source_parser_prompt("Zeta Clinic is nearby."),
                ],
            )
            result = _result(
                repository,
                "strict-rejection-run",
                "strict-rejection",
            )
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["extraction_status"], "complete")
            self.assertEqual(
                [row["name"] for row in result["grounded_occurrences"]],
                ["Zeta Clinic"],
            )
            states = {
                row["attempt_id"]: (
                    row["state"],
                    row["observation_attempt_ordinal"],
                    row["retried_from"],
                )
                for row in result["attempts"]
            }
            self.assertEqual(
                states,
                {
                    "strict-rejection:turn-1": ("acknowledged", 1, None),
                    "strict-rejection:turn-2": ("rejected", 1, None),
                    "strict-rejection:turn-1:attempt-2": (
                        "acknowledged",
                        2,
                        "strict-rejection:turn-1",
                    ),
                    "strict-rejection:turn-2:attempt-2": (
                        "acknowledged",
                        2,
                        "strict-rejection:turn-2",
                    ),
                },
            )
            self.assertEqual(
                result["accepted_attempt_ids"],
                {
                    "measurement_turn_1": "strict-rejection:turn-1:attempt-2",
                    "same_chat_turn_2": "strict-rejection:turn-2:attempt-2",
                },
            )

    async def test_grounding_rejection_is_refetched_in_a_fresh_chat(self):
        with tempfile.TemporaryDirectory() as temporary:
            observation = _observation(
                "grounding-rejection",
                "gemini",
            )
            driver = _ScriptedDriver(
                "gemini",
                {
                    "grounding-rejection": (
                        (
                            _answer("Real Clinic is nearby."),
                            _answer('["Invented Clinic"]'),
                        ),
                        (
                            _answer("Real Clinic is nearby."),
                            _answer('["Real Clinic"]'),
                        ),
                    )
                },
            )
            repository, outcome = await _execute_raw(
                Path(temporary),
                _raw_plan("grounding-rejection-run", [observation]),
                _registry(driver),
            )

            self.assertEqual(outcome.status, "succeeded")
            self.assertEqual(len(driver.opens), 2)
            self.assertEqual(len(driver.sent), 4)
            result = _result(
                repository,
                "grounding-rejection-run",
                "grounding-rejection",
            )
            self.assertEqual(result["extraction_status"], "complete")
            self.assertEqual(
                [row["name"] for row in result["grounded_occurrences"]],
                ["Real Clinic"],
            )
            self.assertEqual(len(result["attempt_ids"]), 4)

    async def test_rejection_refetch_stops_at_the_frozen_ceiling(self):
        with tempfile.TemporaryDirectory() as temporary:
            observation = _observation("twice-rejected", "chatgpt")
            driver = _ScriptedDriver(
                "chatgpt",
                {
                    "twice-rejected": (
                        (
                            _answer("Zeta Clinic is nearby."),
                            _answer('["Zeta Clinic"] trailing prose'),
                        ),
                        (
                            _answer("Zeta Clinic is nearby."),
                            _answer('["Somewhere Else"]'),
                        ),
                        (
                            _answer("Zeta Clinic is nearby."),
                            _answer('["Zeta Clinic"]'),
                        ),
                    )
                },
            )
            repository, outcome = await _execute_raw(
                Path(temporary),
                _raw_plan("twice-rejected-run", [observation]),
                _registry(driver),
            )

            self.assertEqual(outcome.status, "partial")
            self.assertEqual(
                len(driver.opens),
                2,
                "max_attempts=2 permits exactly one fresh chat after a rejection",
            )
            self.assertEqual(len(driver.sent), 4)
            result = _result(
                repository,
                "twice-rejected-run",
                "twice-rejected",
            )
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["extraction_status"], "rejected")
            self.assertEqual(
                [issue["code"] for issue in result["issues"]],
                ["extraction_rejected"],
            )
            self.assertEqual(len(result["attempt_ids"]), 4)
            self.assertEqual(
                result["accepted_attempt_ids"],
                {"measurement_turn_1": "twice-rejected:turn-1:attempt-2"},
            )

    async def test_rejection_is_not_refetched_when_retries_are_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            observation = _observation("disabled-rejection", "chatgpt")
            driver = _ScriptedDriver(
                "chatgpt",
                {
                    "disabled-rejection": (
                        (
                            _answer("Zeta Clinic is nearby."),
                            _answer('["Zeta Clinic"] trailing prose'),
                        ),
                        (
                            _answer("Zeta Clinic is nearby."),
                            _answer('["Zeta Clinic"]'),
                        ),
                    )
                },
            )
            repository, outcome = await _execute_raw(
                Path(temporary),
                _raw_plan(
                    "disabled-rejection-run",
                    [observation],
                    retry_policy=RetryPolicy(enabled=False),
                ),
                _registry(driver),
            )

            self.assertEqual(outcome.status, "partial")
            self.assertEqual(len(driver.opens), 1)
            self.assertEqual(len(driver.sent), 2)
            result = _result(
                repository,
                "disabled-rejection-run",
                "disabled-rejection",
            )
            self.assertEqual(result["extraction_status"], "rejected")
            self.assertEqual(len(result["attempt_ids"]), 2)

    async def test_rejected_result_is_kept_when_the_refetch_chat_cannot_open(
        self,
    ):
        # The fresh chat for the refetch fails to open. The worker enters the
        # prompt measurement once more (the last entry the limit permits) and that
        # entry returns the durable rejected result instead of losing it.
        with tempfile.TemporaryDirectory() as temporary:
            observation = _observation("refetch-open-fails", "chatgpt")
            driver = _ScriptedDriver(
                "chatgpt",
                {
                    "refetch-open-fails": (
                        (
                            _answer("Zeta Clinic is nearby."),
                            _answer('["Zeta Clinic"] trailing prose'),
                        ),
                        (),
                        (
                            _answer("Zeta Clinic is nearby."),
                            _answer('["Zeta Clinic"]'),
                        ),
                    )
                },
                open_failures={"refetch-open-fails": (2,)},
            )
            events: list = []
            repository, outcome = await _execute_raw(
                Path(temporary),
                _raw_plan("refetch-open-fails-run", [observation]),
                _registry(driver),
                events=events,
            )

            self.assertEqual(outcome.status, "partial")
            self.assertEqual(driver.failed_opens, [("refetch-open-fails", 2)])
            self.assertEqual(len(driver.opens), 1)
            self.assertEqual(len(driver.sent), 2)
            result = _result(
                repository,
                "refetch-open-fails-run",
                "refetch-open-fails",
            )
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["extraction_status"], "rejected")
            self.assertEqual(len(result["attempt_ids"]), 2)
            self.assertEqual(
                [
                    event.kind
                    for event in events
                    if event.kind
                    in {
                        EventKind.OBSERVATION_STARTED,
                        EventKind.OBSERVATION_REFETCHED,
                        EventKind.OBSERVATION_COMPLETED,
                    }
                ],
                [
                    EventKind.OBSERVATION_STARTED,
                    EventKind.OBSERVATION_REFETCHED,
                    EventKind.OBSERVATION_STARTED,
                    EventKind.OBSERVATION_COMPLETED,
                ],
            )

    async def test_pre_send_failure_is_refetched_in_the_same_run(self):
        # Exact preparation fails before anything is sent: the website
        # received nothing, so the same prompt is fetched again at once in a
        # fresh chat. The re-armed send attempt keeps its id (attempt 1) because no
        # send was consumed.
        with tempfile.TemporaryDirectory() as temporary:
            observation = _observation("pre-send-refetch", "chatgpt")
            driver = _ScriptedDriver(
                "chatgpt",
                {
                    "pre-send-refetch": (
                        (_prepare_failure(),),
                        (
                            _answer("Zeta Clinic is nearby."),
                            _answer('["Zeta Clinic"]'),
                        ),
                    )
                },
            )
            events: list = []
            repository, outcome = await _execute_raw(
                Path(temporary),
                _raw_plan("pre-send-refetch-run", [observation]),
                _registry(driver),
                events=events,
            )

            self.assertEqual(outcome.status, "succeeded")
            self.assertEqual(outcome.issues, ())
            self.assertEqual(
                driver.prepare_failures,
                [("pre-send-refetch", 1, 0)],
            )
            self.assertEqual(len(driver.opens), 2)
            self.assertEqual(len(driver.sent), 2)
            self.assertEqual(
                [row[1] for row in driver.sent],
                [2, 2],
                "both sends happen in the fresh second chat",
            )
            result = _result(
                repository,
                "pre-send-refetch-run",
                "pre-send-refetch",
            )
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(
                [row["name"] for row in result["grounded_occurrences"]],
                ["Zeta Clinic"],
            )
            self.assertEqual(
                result["attempt_ids"],
                ["pre-send-refetch:turn-1", "pre-send-refetch:turn-2"],
            )
            self.assertEqual(
                [
                    event.kind
                    for event in events
                    if event.kind
                    in {
                        EventKind.OBSERVATION_STARTED,
                        EventKind.OBSERVATION_REFETCHED,
                        EventKind.OBSERVATION_COMPLETED,
                    }
                ],
                [
                    EventKind.OBSERVATION_STARTED,
                    EventKind.OBSERVATION_REFETCHED,
                    EventKind.OBSERVATION_STARTED,
                    EventKind.OBSERVATION_COMPLETED,
                ],
            )
            self.assertEqual(
                outcome.transport_counts["measurement_turn_1"]["retry_count"],
                0,
                "a re-armed pre-send failure consumes no submission",
            )
            packet_state = repository.read_group_progress(
                "pre-send-refetch-run",
                "packet-1",
            )
            failure_context = packet_state["in_run_refetches"][0]["failure_context"]
            self.assertEqual(
                packet_state["in_run_refetches"],
                [
                    {
                        "observation_id": "pre-send-refetch",
                        "failed_entry": 1,
                        "failure_context": {
                            "phase": "observation_execution",
                            "exception_type": "BrowserInteractionError",
                            "origin_file": "retries.py",
                            "origin_module": "support.retries",
                            "origin_function": "prepare_exact",
                            "origin_line": failure_context["origin_line"],
                        },
                    }
                ],
            )
            self.assertGreater(int(failure_context["origin_line"]), 0)

    async def test_pre_send_refetch_is_bounded_by_the_frozen_ceiling(self):
        with tempfile.TemporaryDirectory() as temporary:
            observation = _observation("pre-send-twice", "chatgpt")
            driver = _ScriptedDriver(
                "chatgpt",
                {
                    "pre-send-twice": (
                        (_prepare_failure(),),
                        (_prepare_failure(),),
                        (
                            _answer("Zeta Clinic is nearby."),
                            _answer('["Zeta Clinic"]'),
                        ),
                    )
                },
            )
            repository, outcome = await _execute_raw(
                Path(temporary),
                _raw_plan("pre-send-twice-run", [observation]),
                _registry(driver),
            )

            self.assertEqual(outcome.status, "failed")
            self.assertEqual(
                len(driver.opens),
                2,
                "max_attempts=2 permits exactly one in-run refetch",
            )
            self.assertEqual(len(driver.sent), 0)
            self.assertEqual(
                [issue["code"] for issue in outcome.issues],
                ["observation_failed"],
            )
            self.assertTrue(outcome.issues[0]["recoverable"])
            fences = SendAttemptStore(
                repository.run_dir("pre-send-twice-run") / "fences"
            )
            records = list(fences.iter_records())
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].state, SendAttemptState.PRE_SEND_FAILED)
            self.assertEqual(
                list(repository.iter_observation_results("pre-send-twice-run")),
                [],
            )

    async def test_pre_send_refetch_is_skipped_when_retries_are_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            observation = _observation("pre-send-disabled", "chatgpt")
            driver = _ScriptedDriver(
                "chatgpt",
                {
                    "pre-send-disabled": (
                        (_prepare_failure(),),
                        (
                            _answer("Zeta Clinic is nearby."),
                            _answer('["Zeta Clinic"]'),
                        ),
                    )
                },
            )
            repository, outcome = await _execute_raw(
                Path(temporary),
                _raw_plan(
                    "pre-send-disabled-run",
                    [observation],
                    retry_policy=RetryPolicy(enabled=False),
                ),
                _registry(driver),
            )

            self.assertEqual(outcome.status, "failed")
            self.assertEqual(len(driver.opens), 1)
            self.assertEqual(len(driver.sent), 0)
            self.assertEqual(
                [issue["code"] for issue in outcome.issues],
                ["observation_failed"],
            )



    async def test_ambiguous_send_is_never_refetched_in_the_same_run(self):
        # A send whose outcome is unknown must not be repeated automatically;
        # that rule is unchanged by in-run refetching of confirmed failures.
        with tempfile.TemporaryDirectory() as temporary:
            observation = _observation("ambiguous-no-refetch", "chatgpt")
            driver = _ScriptedDriver(
                "chatgpt",
                {
                    "ambiguous-no-refetch": (
                        (
                            _timeout(
                                disposition=(
                                    ReconciliationDisposition.AMBIGUOUS
                                ),
                            ),
                        ),
                        (
                            _answer("Zeta Clinic is nearby."),
                            _answer('["Zeta Clinic"]'),
                        ),
                    )
                },
            )
            repository, outcome = await _execute_raw(
                Path(temporary),
                _raw_plan("ambiguous-no-refetch-run", [observation]),
                _registry(driver),
            )

            self.assertEqual(len(driver.opens), 1)
            self.assertEqual(len(driver.sent), 1)
            self.assertEqual(
                [issue["code"] for issue in outcome.issues],
                ["ambiguous_send"],
            )
            fences = SendAttemptStore(
                repository.run_dir("ambiguous-no-refetch-run") / "fences"
            )
            records = list(fences.iter_records())
            self.assertEqual(len(records), 1)
            self.assertIn(
                records[0].state,
                {SendAttemptState.SEND_STARTED, SendAttemptState.RESPONSE_CAPTURED},
            )
