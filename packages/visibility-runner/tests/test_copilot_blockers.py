"""Durable blocker holds and request-bound identities; fake browser only."""
import asyncio
import json
import time
from types import SimpleNamespace
from contextlib import nullcontext

import pytest

from visibility_runner.browser.copilot_blockers import CopilotBlockerService, account_emails
from visibility_runner.errors import HumanVerificationRequiredError, ProviderAccessBlockedError, IntegrityError
from visibility_runner.execution.verification_policy import effective_accounts
from visibility_runner.browser.profile_access import ProfileVerificationHeld
from visibility_runner.recovery_adapters.visibility import VisibilityExecution
from visibility_runner.execution.run_measurement_group import MeasurementGroupWorker, _CancellableSendAttemptStore
from test_automatic_verification_policy import make_runner
from test_copilot_verification_holds import case, deadline


@pytest.mark.parametrize('marker, kind', [('authentication_redirect', 'sign_in'), ('unknown_dialog', 'other'), ('browser_dialog', 'other')])
def test_blocker_deadline_retains_exact_tab_and_kind_without_native_input(tmp_path, monkeypatch, marker, kind):
    async def run():
        c = case(tmp_path, monkeypatch)
        c.page.marker = marker
        handler = CopilotBlockerService(c.service, holds=c.holds)
        c.submission.human_verification_handler = handler
        with pytest.raises((ProviderAccessBlockedError, TimeoutError)):
            await handler.resolve(c.submission, marker, deadline(.06))
        row = c.store.rows()[0]
        assert row['retained'] and row['status'] == 'expired_held'
        assert c.store.public()[0]['blocker_kind'] == kind
        report = await c.registry.close_all()
        assert report.retained_tab_references == ('synthetic-tab',)
        assert not c.page.closed and c.native == [] and c.page.focus_count == 0
        with pytest.raises(ProfileVerificationHeld):
            c.manager.acquire((c.profile,), owner='other', provider='copilot', timeout_seconds=.1)
        assert await c.queue.aclose()
    asyncio.run(run())


def test_authentication_hold_never_persists_oauth_query_values(tmp_path, monkeypatch):
    async def run():
        c = case(tmp_path, monkeypatch)
        c.page.url = 'https://login.live.com/oauth20_authorize.srf?code=synthetic-secret&state=synthetic-state'
        await c.holds.begin(c.submission, deadline(), blocker_kind='sign_in')
        body = c.store.path(c.profile.id).read_text()
        assert 'synthetic-secret' not in body and 'synthetic-state' not in body and 'login.live.com' not in body
        row = c.store.rows()[0]
        assert row['identity']['conversation_reference'].startswith('https://copilot.com/')
        before = row['deadline_monotonic']
        await c.holds.begin(c.submission, deadline(), blocker_kind='other')
        assert len(c.store.rows()) == 1 and c.store.rows()[0]['deadline_monotonic'] == before
        assert await c.queue.aclose()
    asyncio.run(run())


def test_account_opt_in_is_execution_local_hash_bound_and_preserves_frozen_inputs(tmp_path):
    runner, plan = make_runner(tmp_path)
    profile = runner.runtime.selected_profile_ids[0]
    root = runner.repository.run_dir(plan.run_id)
    frozen = {name: (root / name).read_bytes() for name in ('plan.json', 'execution-policy.json', 'runner_state.json')}
    with runner.execution(plan.run_id, execution_id='blocker-execution'):
        receipt = runner.enable_automatic_verification(plan.run_id,
            request_id='explicit-refetch', request_sha256='ab'*32, account_emails={profile:'Expected@example.test'})
        assert receipt['account_emails'] == {profile:'expected@example.test'}
        assert effective_accounts(runner, plan.run_id) == receipt['account_emails']
        assert {name:(root/name).read_bytes() for name in frozen} == frozen
        path = runner._automatic_verification_execution[2]
        changed = json.loads(path.read_text()); changed['account_emails'][profile] = 'other@example.test'
        path.write_text(json.dumps(changed))
        with pytest.raises(IntegrityError, match='policy_changed'):
            effective_accounts(runner, plan.run_id)
    with runner.execution(plan.run_id, execution_id='another-execution'):
        assert effective_accounts(runner, plan.run_id) == {}


@pytest.mark.parametrize('bindings', [{'unknown':'a@example.test'}, {'selected':'not-an-email'}, ['a@example.test']])
def test_invalid_or_unselected_account_bindings_are_refused(bindings):
    with pytest.raises(ValueError):
        account_emails(bindings, ['selected'])


def test_profile_gate_waits_under_its_own_deadline_without_input(tmp_path, monkeypatch):
    async def run():
        c = case(tmp_path, monkeypatch)
        handler = CopilotBlockerService(c.service, holds=c.holds)
        lock = handler.locks[c.profile.id] = asyncio.Lock()
        await lock.acquire()
        handler.active_profiles[c.profile.id] = 'other-owned-observation'
        with pytest.raises(TimeoutError):
            await handler.gate(c.submission, deadline(.03))
        assert lock.locked() and c.native == [] and c.page.focus_count == 0
        lock.release()
        assert await c.queue.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('kind, reason, error_type, foreign', [
    ('sign_in', 'copilot_sign_in_required', ProviderAccessBlockedError, False),
    ('other', 'unknown_dialog', ProviderAccessBlockedError, False),
    ('verification', 'human_verification_required', HumanVerificationRequiredError, False),
    ('sign_in', 'copilot_sign_in_required', ProviderAccessBlockedError, True),
])
def test_failed_profile_blocker_keeps_its_reason_and_peer_fence(tmp_path, monkeypatch, kind, reason, error_type, foreign):
    async def run():
        c = case(tmp_path, monkeypatch)
        handler = CopilotBlockerService(c.service, holds=c.holds)
        await c.holds.begin(c.submission, deadline(), blocker_kind=kind)
        c.holds.update(c.submission, reason=reason)
        c.holds.failed(c.submission, ProviderAccessBlockedError('synthetic blocker failure'), deadline())
        with pytest.raises(error_type, match='profile_blocker_retained') as raised:
            await handler.gate(SimpleNamespace(profile_id=c.profile.id), deadline())
        assert type(raised.value) is error_type
        assert raised.value.reason == c.store.rows()[0]['reason']
        if foreign:
            with c.store.transaction(c.profile.id) as marker:
                for episode in marker['episodes'].values():
                    episode['run_id'] = 'another-synthetic-run'
            assert c.store.rows() == ()
        async def no_active_wait(*args):
            return None
        worker = object.__new__(MeasurementGroupWorker)
        worker.recovery = SimpleNamespace(verification_holds=c.holds,
            wait_for_active_blocker=no_active_wait)
        worker._require_active_run = lambda run_id: None
        with pytest.raises(error_type, match='profile_blocker_retained') as refused:
            await worker._await_send_admission('synthetic-run', 'queued', 'copilot', c.profile.id)
        assert type(refused.value) is error_type
        assert refused.value.reason == raised.value.reason
        assert c.store.rows(own_run=False)[0]['retained'] and c.native == [] and c.page.focus_count == 0
        assert await c.queue.aclose()
    asyncio.run(run())


def test_visible_blocker_is_retained_before_a_peer_gate_can_refuse(tmp_path, monkeypatch):
    async def run():
        c = case(tmp_path, monkeypatch)
        c.page.marker = 'unknown_dialog'
        handler = c.submission.human_verification_handler = CopilotBlockerService(c.service, holds=c.holds)
        async def refused(*args):
            raise ProviderAccessBlockedError('synthetic peer gate')
        handler.gate = refused
        with pytest.raises((ProviderAccessBlockedError, TimeoutError)):
            await c.submission._assert_access_available(_deadline=deadline(.1))
        assert c.store.rows()[0]['blocker_kind'] == 'other'
        assert (await c.registry.close_all()).retained_tab_references == ('synthetic-tab',)
        assert not c.page.closed and c.native == []
        assert await c.queue.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('kind, stage, outcome', [
    ('sign_in', 'queued', 'cleared'), ('sign_in', 'queued', 'unsupported'),
    ('verification', 'queued', 'cleared'), ('verification', 'queued', 'unsupported'),
    ('verification', 'admission', 'cleared'), ('verification', 'admission', 'unsupported'),
    ('sign_in', 'admission', 'unsupported'),
    ('verification', 'admission', 'expired'), ('verification', 'admission', 'cancelled'),
])
def test_active_peer_blocker_waits_before_work_or_admission_without_resetting_its_deadline(
        tmp_path, monkeypatch, kind, stage, outcome):
    async def run():
        c = case(tmp_path, monkeypatch)
        context = object.__new__(VisibilityExecution)
        context.__dict__.update(vars(c.recovery))
        context.verification_holds = c.holds
        context.snapshot = SimpleNamespace(manifest=SimpleNamespace(run_id='synthetic-run',
            operations=(SimpleNamespace(item_id='queued', resource_scopes=(f'{c.profile.id}:copilot',)),)))
        context.initial = True
        context.adopted = set()
        context.blocked_scopes = set()
        failures = []
        context.failure = lambda item, reason: failures.append((item, reason))
        context.progress = lambda *args, **kwargs: None
        c.holds.recovery = context
        handler = CopilotBlockerService(c.service, holds=c.holds)
        lock = handler.locks[c.profile.id] = asyncio.Lock()
        await lock.acquire()
        handler.active_profiles[c.profile.id] = id(c.submission)
        budget = deadline(.15 if outcome == 'expired' else .5)
        await c.holds.begin(c.submission, budget, blocker_kind=kind)
        expires = c.store.rows()[0]['deadline_monotonic']
        observed_deadlines = []
        gate = handler.gate
        async def observe_gate(submission, bounded):
            observed_deadlines.append(bounded.expires_at)
            return await gate(submission, bounded)
        handler.gate = observe_gate
        async def wait(run_id, awaitable, **kwargs):
            return await awaitable
        worker = object.__new__(MeasurementGroupWorker)
        worker.providers = SimpleNamespace(drivers={
            'copilot':SimpleNamespace(human_verification_handler=handler)})
        worker._await_with_cancellation = wait
        worker._require_active_run = lambda run_id: None
        worker.recovery = context
        context.operations = {'queued:measurement_turn_1': SimpleNamespace(resource_scopes=(f'{c.profile.id}:copilot',))}
        context.selections = {'queued': None}
        context.resource_tracking = False
        context.pending = {('queued', 'measurement_turn_1'): SimpleNamespace(
            attempt_id='queued-attempt', operation_id='queued:measurement_turn_1')}
        context.assert_owned = lambda: None
        context.coordinator.ledger.transaction = lambda owner: nullcontext()
        context.coordinator.ledger.append = lambda *args: None
        fences = _CancellableSendAttemptStore(tmp_path/'fences',
            admission=lambda: nullcontext(True), cancelled=lambda: False, recovery=context)
        async def admit():
            await worker._await_send_admission('synthetic-run', 'queued', 'copilot', c.profile.id)
            return fences.prepare(fence_id='queued:turn-1', observation_id='queued',
                action='measurement_turn_1', prompt_sha256='ab'*32, identity=c.identity.to_mapping())
        queued = asyncio.create_task(admit() if stage == 'admission'
            else context.prepare_item(worker, 'queued', {}, {}))
        await asyncio.sleep(.01)
        assert not queued.done() and failures == [] and fences.iter_records() == ()
        if outcome == 'cancelled':
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
        elif outcome == 'expired':
            with pytest.raises(HumanVerificationRequiredError, match='profile_blocker_retained') as refused:
                await queued
            assert refused.value.reason == 'copilot_blocker_deadline_expired'
        elif outcome == 'cleared':
            c.page.marker = None
            c.holds.live[id(c.submission)]['history'] = c.history
            await c.holds.validated(c.submission, budget)
        else:
            c.holds.update(c.submission, reason='sign_in_requires_manual_step')
            c.holds.failed(c.submission, ProviderAccessBlockedError('synthetic unsupported sign-in'), budget)
        handler.active_profiles.pop(c.profile.id)
        lock.release()
        clear = outcome == 'cleared'
        if stage == 'queued':
            assert await queued is clear
            assert failures == ([] if clear else [('queued', c.store.rows()[0]['reason'])])
        elif outcome in {'cleared', 'unsupported'}:
            if clear:
                assert (await queued).observation_id == 'queued'
            else:
                error_type = HumanVerificationRequiredError if kind == 'verification' else ProviderAccessBlockedError
                with pytest.raises(error_type, match='profile_blocker_retained') as refused:
                    await queued
                assert type(refused.value) is error_type
                assert refused.value.reason == c.store.rows()[0]['reason']
        if stage == 'admission':
            assert len(fences.iter_records()) == int(clear)
        assert observed_deadlines == [expires]
        assert c.store.scope_held(c.profile.id, item_id='queued') is not clear
        if not clear:
            with pytest.raises(HumanVerificationRequiredError, match='verification_pair_held'):
                context.before_prepare({'observation_id':'queued', 'action':'measurement_turn_1'})
        assert c.native == [] and c.page.focus_count == 0
        assert await c.queue.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('marker', ['authentication_redirect', 'human_verification_required', 'unknown_dialog'])
def test_expired_hold_cannot_reset_deadline_or_clear_with_a_new_budget(tmp_path, monkeypatch, marker):
    async def run():
        c = case(tmp_path, monkeypatch)
        c.page.marker = marker
        handler = CopilotBlockerService(c.service, holds=c.holds)
        c.submission.human_verification_handler = handler
        await c.holds.begin(c.submission, deadline(), blocker_kind='sign_in' if marker == 'authentication_redirect' else 'other')
        c.holds.update(c.submission, deadline_monotonic=time.monotonic()-.01)
        original_deadline = c.store.rows()[0]['deadline_monotonic']
        c.page.marker = None
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await handler.resolve(c.submission, marker, deadline(10))
        assert time.monotonic() - started < 1
        row = c.store.rows()[0]
        assert row['deadline_monotonic'] == original_deadline and row['status'] == 'expired_held'
        assert row['retained'] and c.native == [] and c.page.focus_count == 0
        with pytest.raises(TimeoutError):
            await c.holds.validated(c.submission, deadline(10))
        assert await c.queue.aclose()
    asyncio.run(run())
