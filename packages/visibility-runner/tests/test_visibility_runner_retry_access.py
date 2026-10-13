"""Access blocks preserve provider-lane isolation and send certainty."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from visibility_runner.errors import (
    HumanVerificationRequiredError,
    ProviderAccessBlockedError,
    StateTransitionError,
)
from visibility_runner.execution.run_measurement_group import MeasurementGroupWorker
from visibility_runner.websites.chat_actions import ReconciliationDisposition
from visibility_runner.storage.send_attempts import SendAttemptState, SendAttemptStore
from visibility_runner.storage.measurement_files import MeasurementFiles
from support.retries import (
    _ScriptedDriver,
    _ScriptedSubmission,
    _answer,
    _execute_raw,
    _observation,
    _raw_plan,
    _registry,
    _result,
    _timeout,
)


class RetryAccessTests(unittest.IsolatedAsyncioTestCase):
    def _assert_access_stop(self, outcome, *, human=False, code="needs_attention", reason=None):
        self.assertEqual(outcome.status, "needs_attention")
        self.assertEqual(len(outcome.issues), 1)
        issue = outcome.issues[0]
        self.assertEqual(issue["code"], code)
        self.assertFalse(issue["recoverable"])
        self.assertIs(issue["detail"]["user_action_required"], True)
        block = issue["detail"]["access_block"]
        self.assertEqual(block["profile_id"], "profile_a")
        self.assertEqual(block["provider"], "gemini")
        self.assertEqual(
            block["reason"],
            reason or ("human_verification_required" if human else "authentication_required"),
        )
        self.assertEqual(
            block["exception_type"],
            "HumanVerificationRequiredError" if human else "ProviderAccessBlockedError",
        )

    async def test_pre_send_access_blocks_never_send_or_open_a_peer(self):
        private_detail = "synthetic-private-provider-access-text"
        for error_type in (ProviderAccessBlockedError, HumanVerificationRequiredError):
            for stage in ("open", "prepare_exact", "validate_pre_send_identity"):
                with (
                    self.subTest(error=error_type.__name__, stage=stage),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    first = _observation("blocked-access", "gemini")
                    peer = _observation("forbidden-access-peer", "gemini")
                    driver = _ScriptedDriver(
                        "gemini",
                        {"blocked-access": ((_answer("Unused synthetic answer."),),)},
                    )
                    attempts = []

                    async def blocked(subject, *args, **kwargs):
                        del args
                        attempts.append(
                            kwargs["observation_id"]
                            if stage == "open"
                            else subject.observation_id
                        )
                        raise error_type(private_detail)

                    owner = _ScriptedDriver if stage == "open" else _ScriptedSubmission
                    with patch.object(owner, stage, blocked, create=True):
                        repository, outcome = await _execute_raw(
                            Path(temporary),
                            _raw_plan("pre-send-access-run", [first, peer]),
                            _registry(driver),
                        )

                    self._assert_access_stop(
                        outcome,
                        human=error_type is HumanVerificationRequiredError,
                    )
                    self.assertEqual(attempts, ["blocked-access"])
                    self.assertEqual(driver.sent, [])
                    self.assertEqual(driver.native_retries, [])
                    records = SendAttemptStore(
                        repository.run_dir("pre-send-access-run") / "fences"
                    ).iter_records()
                    self.assertEqual(len(records), 0 if stage == "open" else 1)
                    for record in records:
                        self.assertIs(record.state, SendAttemptState.PRE_SEND_FAILED)
                        self.assertIsNone(record.send_started_at)
                        self.assertIsNone(record.retried_by)
                        self.assertIs(record.pre_send_failure["user_action_required"], True)
                    for path in repository.run_dir("pre-send-access-run").rglob("*.json"):
                        self.assertNotIn(private_detail, path.read_text(encoding="utf-8"))

    async def test_access_block_after_send_start_preserves_ambiguity_and_safe_type(self):
        private_detail = "synthetic-private-post-send-access-text"
        for error_type in (ProviderAccessBlockedError, HumanVerificationRequiredError):
            for stage in ("submit_once", "wait_response"):
                with (
                    self.subTest(error=error_type.__name__, stage=stage),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    driver = _ScriptedDriver(
                        "gemini",
                        {"sent-access": ((_answer("Unused synthetic answer."),),)},
                    )

                    async def blocked(_submission):
                        raise error_type(private_detail)

                    with patch.object(_ScriptedSubmission, stage, blocked):
                        repository, outcome = await _execute_raw(
                            Path(temporary),
                            _raw_plan(
                                "post-send-access-run",
                                [
                                    _observation("sent-access", "gemini"),
                                    _observation("forbidden-sent-peer", "gemini"),
                                ],
                            ),
                            _registry(driver),
                        )

                    self._assert_access_stop(
                        outcome,
                        human=error_type is HumanVerificationRequiredError,
                        code="ambiguous_send",
                    )
                    self.assertEqual(len(driver.opens), 1)
                    self.assertEqual(driver.native_retries, [])
                    records = SendAttemptStore(
                        repository.run_dir("post-send-access-run") / "fences"
                    ).iter_records()
                    self.assertEqual(len(records), 1)
                    self.assertIs(records[0].state, SendAttemptState.SEND_STARTED)
                    self.assertIsNone(records[0].retry_decision)
                    self.assertIsNone(records[0].retried_by)
                    self.assertEqual(
                        outcome.transport_counts["measurement_turn_1"]["send_started"],
                        1,
                    )
                    self.assertEqual(
                        outcome.issues[0]["detail"]["failure_context"]["cause_type"],
                        error_type.__name__,
                    )
                    for path in repository.run_dir("post-send-access-run").rglob("*.json"):
                        self.assertNotIn(private_detail, path.read_text(encoding="utf-8"))

    async def test_proven_reconciled_access_block_stops_without_peer_or_retry(self):
        for classification, human in (
            ("provider_access_blocked:authentication_redirect", False),
            ("provider_access_blocked:human_verification_required", True),
            ("provider_access_blocked:verify you are human", True),
        ):
            with (
                self.subTest(classification=classification),
                tempfile.TemporaryDirectory() as temporary,
            ):
                driver = _ScriptedDriver(
                    "gemini",
                    {
                        "reconciled-access": ((
                            _timeout(
                                disposition=ReconciliationDisposition.TERMINAL_NONRETRYABLE,
                                failure_classification=classification,
                            ),
                        ),),
                    },
                )
                repository, outcome = await _execute_raw(
                    Path(temporary),
                    _raw_plan(
                        "reconciled-access-run",
                        [
                            _observation("reconciled-access", "gemini"),
                            _observation("forbidden-reconciled-peer", "gemini"),
                        ],
                    ),
                    _registry(driver),
                )

                self._assert_access_stop(outcome, human=human)
                self.assertEqual(len(driver.opens), 1)
                self.assertEqual(len(driver.sent), 1)
                self.assertEqual(driver.native_retries, [])
                records = SendAttemptStore(
                    repository.run_dir("reconciled-access-run") / "fences"
                ).iter_records()
                self.assertEqual(len(records), 1)
                self.assertIs(records[0].state, SendAttemptState.CONFIRMED_NO_RESPONSE)
                self.assertEqual(records[0].failure_classification, classification)
                self.assertEqual(records[0].retry_decision, "do_not_retry")
                self.assertIsNone(records[0].retried_by)

    async def test_ordinary_terminal_nonretryable_does_not_poison_peer_lane(self):
        with tempfile.TemporaryDirectory() as temporary:
            driver = _ScriptedDriver(
                "gemini",
                {
                    "ordinary-terminal": ((
                        _timeout(
                            disposition=ReconciliationDisposition.TERMINAL_NONRETRYABLE,
                            failure_classification="ordinary_provider_terminal_failure",
                        ),
                    ),),
                    "allowed-peer": ((
                        _answer("Alpha Clinic is nearby."),
                        _answer('["Alpha Clinic"]'),
                    ),),
                },
            )
            repository, outcome = await _execute_raw(
                Path(temporary),
                _raw_plan(
                    "ordinary-nonretryable-run",
                    [
                        _observation("ordinary-terminal", "gemini"),
                        _observation("allowed-peer", "gemini"),
                    ],
                ),
                _registry(driver),
            )

            self.assertEqual(outcome.status, "partial")
            self.assertEqual(len(driver.opens), 2)
            self.assertEqual(len(driver.sent), 3)
            self.assertEqual([issue["code"] for issue in outcome.issues], ["provider_no_response"])
            self.assertEqual(_result(repository, "ordinary-nonretryable-run", "allowed-peer")["status"], "succeeded")

    async def test_access_stop_is_durable_and_scoped_to_profile_provider(self):
        with tempfile.TemporaryDirectory() as temporary:
            direct = _observation("blocked-direct", "gemini")
            peer = _observation("blocked-peer", "gemini")
            unrelated = _observation("allowed-chatgpt", "chatgpt")
            other_profile = _observation("allowed-profile", "gemini")
            other_profile["profile_id"] = "profile_b"
            other_profile["chrome_directory"] = "Profile 8"
            plan = _raw_plan("scoped-access-run", [direct])
            plan["selected_profiles"].append({
                "id": "profile_b", "display_name": "Account B",
                "chrome_directory": "Profile 8", "project_url": "",
            })
            plan["observation_plan"]["observations"] += [peer, unrelated, other_profile]
            plan["packets"] += [
                {"packet_id": "peer-packet", "kind": "measurement",
                 "platform": "gemini", "profile_id": "profile_a",
                 "observation_ids": ["blocked-peer"]},
                {"packet_id": "chatgpt-packet", "kind": "measurement",
                 "platform": "chatgpt", "profile_id": "profile_a",
                 "observation_ids": ["allowed-chatgpt"]},
                {"packet_id": "other-profile-packet", "kind": "measurement",
                 "platform": "gemini", "profile_id": "profile_b",
                 "observation_ids": ["allowed-profile"]},
            ]
            gemini = _ScriptedDriver("gemini", {
                "allowed-profile": ((
                    _answer("Alpha Clinic is nearby."), _answer('["Alpha Clinic"]'),
                ),),
            })
            chatgpt = _ScriptedDriver("chatgpt", {
                "allowed-chatgpt": ((
                    _answer("Alpha Clinic is nearby."), _answer('["Alpha Clinic"]'),
                ),),
            })
            providers = _registry(gemini, chatgpt)

            async def blocked(_driver, **_kwargs):
                raise HumanVerificationRequiredError("synthetic private gate text")

            with patch.object(_ScriptedDriver, "open", blocked):
                repository, first = await _execute_raw(Path(temporary), plan, providers)
            self._assert_access_stop(first, human=True)
            run_id = "scoped-access-run"

            async def execute(packet_id):
                return await MeasurementGroupWorker(repository, providers).execute(
                    run_id=run_id,
                    plan=repository.read_plan(run_id),
                    packet=repository.read_measurement_group(run_id, packet_id),
                )

            peer_outcome = await execute("peer-packet")
            self._assert_access_stop(peer_outcome, human=True)
            self.assertEqual(peer_outcome.issues[0]["detail"]["source_packet_id"], "packet-1")
            self.assertEqual(gemini.opens, [])
            self.assertEqual(SendAttemptStore(repository.run_dir(run_id) / "fences").iter_records(), ())
            self.assertEqual((await execute("chatgpt-packet")).status, "succeeded")
            self.assertEqual((await execute("other-profile-packet")).status, "succeeded")
            self.assertEqual([row[0] for row in gemini.opens], ["allowed-profile"])
            self.assertEqual([row[0] for row in chatgpt.opens], ["allowed-chatgpt"])

    async def test_historical_google_packets_remain_readable_without_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_id = "historical-google-access-run"
            plan = _raw_plan(run_id, [_observation("historical-google", "google_ai_overview")])
            plan["packets"].append({
                "packet_id": "historical-parser", "kind": "gaio_extraction",
                "platform": "gemini", "profile_id": "profile_a",
                "observation_ids": ["historical-google"],
            })
            repository = MeasurementFiles(Path(temporary))
            run_dir = repository.create(plan)
            frozen = {path.relative_to(run_dir): path.read_bytes()
                      for path in run_dir.rglob("*") if path.is_file()}
            stored_plan = repository.read_plan(run_id)
            self.assertEqual(stored_plan["observation_plan"], plan["observation_plan"])
            self.assertEqual(stored_plan["packets"], plan["packets"])
            google = _ScriptedDriver("google_ai_overview", {})
            gemini = _ScriptedDriver("gemini", {})
            worker = MeasurementGroupWorker(repository, _registry(google, gemini))
            for packet in plan["packets"]:
                with self.subTest(packet=packet["packet_id"]):
                    saved_packet = repository.read_measurement_group(run_id, packet["packet_id"])
                    self.assertEqual(saved_packet, packet)
                    with self.assertRaisesRegex(StateTransitionError, "google_ai_overview_deferred"):
                        await worker.execute(run_id=run_id, plan=stored_plan, packet=saved_packet)
            self.assertEqual(google.opens, [])
            self.assertEqual(gemini.opens, [])
            self.assertEqual(google.sent, [])
            self.assertEqual(gemini.sent, [])
            self.assertEqual(frozen, {path.relative_to(run_dir): path.read_bytes()
                                      for path in run_dir.rglob("*") if path.is_file()})

    async def test_access_reason_retains_sent_fence_and_durable_peer_stop(self):
        for reason in ("unusual_activity", "unknown_dialog"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                run_id = "access-reason-run"
                plan = _raw_plan(run_id, [_observation("blocked-activity", "chatgpt")])
                plan["observation_plan"]["observations"].append(
                    _observation("blocked-activity-peer", "chatgpt")
                )
                plan["packets"].append({
                    "packet_id": "peer-packet", "kind": "measurement",
                    "platform": "chatgpt", "profile_id": "profile_a",
                    "observation_ids": ["blocked-activity-peer"],
                })
                driver = _ScriptedDriver("chatgpt", {
                    "blocked-activity": ((_answer("Unused synthetic answer."),),),
                })
                providers = _registry(driver)

                async def blocked(_submission):
                    raise ProviderAccessBlockedError(
                        "synthetic private access label", reason=reason
                    )

                with patch.object(_ScriptedSubmission, "wait_response", blocked):
                    repository, first = await _execute_raw(Path(temporary), plan, providers)
                self.assertEqual(first.issues[0]["detail"]["access_block"]["reason"], reason)
                peer = await MeasurementGroupWorker(repository, providers).execute(
                    run_id=run_id,
                    plan=repository.read_plan(run_id),
                    packet=repository.read_measurement_group(run_id, "peer-packet"),
                )
                self.assertEqual(peer.status, "needs_attention")
                self.assertEqual(peer.issues[0]["detail"]["access_block"]["reason"], reason)
                self.assertEqual(peer.issues[0]["detail"]["source_packet_id"], "packet-1")
                records = SendAttemptStore(repository.run_dir(run_id) / "fences").iter_records()
                self.assertEqual(len(records), 1)
                self.assertIs(records[0].state, SendAttemptState.SEND_STARTED)
                self.assertIsNone(records[0].retried_by)
                self.assertEqual(len(driver.opens), 1)
                self.assertEqual(len(driver.sent), 1)
                self.assertEqual(driver.native_retries, [])

    async def test_usage_limit_preserves_ambiguity_clock_and_durable_peer_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_id = "usage-limit-run"
            plan = _raw_plan(run_id, [_observation("limited", "chatgpt")])
            plan["observation_plan"]["observations"].append(_observation("limited-peer", "chatgpt"))
            plan["packets"].append({
                "packet_id": "peer-packet", "kind": "measurement", "platform": "chatgpt",
                "profile_id": "profile_a", "observation_ids": ["limited-peer"],
            })
            limit = {"scope": "text", "text_allowed": False, "blocking": True,
                "reset_day": "tomorrow", "reset_clock": "02:00", "reset_display": "tomorrow at 2:00\u202fAM",
                "reset_timezone": None, "notice_text": "private notice token-sentinel"}
            driver = _ScriptedDriver("chatgpt", {"limited": ((_answer("Unused answer."),),)})
            providers = _registry(driver)

            async def blocked(_submission):
                raise ProviderAccessBlockedError("private error token-sentinel", reason="usage_limit", usage_limit=limit)

            with patch.object(_ScriptedSubmission, "wait_response", blocked):
                repository, first = await _execute_raw(Path(temporary), plan, providers)
            block = first.issues[0]["detail"]["access_block"]
            self.assertEqual(first.issues[0]["code"], "ambiguous_send")
            self.assertEqual(block["reason"], "usage_limit")
            self.assertEqual(block["usage_limit"], {key: value for key, value in limit.items() if key != "notice_text"})
            peer = await MeasurementGroupWorker(repository, providers).execute(
                run_id=run_id, plan=repository.read_plan(run_id),
                packet=repository.read_measurement_group(run_id, "peer-packet"),
            )
            self.assertEqual(peer.status, "needs_attention")
            self.assertEqual(peer.issues[0]["detail"]["access_block"], block)
            self.assertEqual(peer.issues[0]["detail"]["source_packet_id"], "packet-1")
            records = SendAttemptStore(repository.run_dir(run_id) / "fences").iter_records()
            self.assertEqual(len(records), 1)
            self.assertIs(records[0].state, SendAttemptState.SEND_STARTED)
            self.assertIsNone(records[0].retry_decision)
            self.assertIsNone(records[0].retried_by)
            self.assertEqual(len(driver.opens), 1)
            self.assertEqual(len(driver.sent), 1)
            self.assertEqual(driver.native_retries, [])

    async def test_pre_send_usage_limit_stops_without_send_or_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            driver = _ScriptedDriver("gemini", {"limited": ((_answer("Unused answer."),),)})
            limit = {"scope": "unknown", "text_allowed": None, "blocking": True}

            async def blocked(_submission, *_args, **_kwargs):
                raise ProviderAccessBlockedError(reason="usage_limit", usage_limit=limit)

            with patch.object(_ScriptedSubmission, "prepare_exact", blocked):
                repository, outcome = await _execute_raw(Path(temporary),
                    _raw_plan("unsent-limit", [_observation("limited", "gemini"), _observation("peer", "gemini")]),
                    _registry(driver))
            self._assert_access_stop(outcome, reason="usage_limit")
            self.assertEqual(outcome.issues[0]["detail"]["access_block"]["usage_limit"]["scope"], "unknown")
            records = SendAttemptStore(repository.run_dir("unsent-limit") / "fences").iter_records()
            self.assertEqual(len(records), 1)
            self.assertIs(records[0].state, SendAttemptState.PRE_SEND_FAILED)
            self.assertIsNone(records[0].send_started_at)
            self.assertEqual(len(driver.opens), 1)
            self.assertEqual(driver.sent, [])
            self.assertEqual(driver.native_retries, [])

    async def test_reconciled_usage_limit_reads_last_blocking_check_without_resend(self):
        with tempfile.TemporaryDirectory() as temporary:
            limit = {"scope": "text", "text_allowed": False, "blocking": True,
                "reset_day": None, "reset_clock": "02:00", "reset_display": "2am", "reset_timezone": None}
            driver = _ScriptedDriver("gemini", {"limited": ((_timeout(
                disposition=ReconciliationDisposition.AMBIGUOUS,
                failure_classification="provider_access_blocked:usage_limit",
                reconciliation_evidence={"checks": [
                    {"usage_limit": {**limit, "reset_clock": "01:00", "reset_display": "1am"}},
                    {"usage_limit": limit},
                ]},
            ),),)})
            repository, outcome = await _execute_raw(Path(temporary),
                _raw_plan("reconciled-limit", [_observation("limited", "gemini"), _observation("peer", "gemini")]),
                _registry(driver))
            self._assert_access_stop(outcome, code="ambiguous_send", reason="usage_limit")
            self.assertEqual(outcome.issues[0]["detail"]["access_block"]["usage_limit"], limit)
            records = SendAttemptStore(repository.run_dir("reconciled-limit") / "fences").iter_records()
            self.assertIs(records[0].state, SendAttemptState.SEND_STARTED)
            self.assertEqual(records[0].failure_classification, "provider_access_blocked:usage_limit")
            self.assertIsNone(records[0].retried_by)
            self.assertEqual(len(driver.sent), 1)
            self.assertEqual(len(driver.opens), 1)
            self.assertEqual(driver.native_retries, [])
