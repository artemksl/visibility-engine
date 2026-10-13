from __future__ import annotations
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
sys.path.insert(0,str(ROOT/'tests'))
import pytest
from dataclasses import replace
from support.execution import FakeDriver, AnswerSpec, _runtime, _typed_plan
from visibility_runner.config import Platform
from visibility_runner.execution.manage_measurements import Runner
from visibility_runner.recovery.api import StaleRecoveryPreview


def fixture(tmp_path):
    runtime,digest=_runtime(tmp_path)
    runtime=replace(runtime, profiles=replace(runtime.profiles, lease_lock_root=tmp_path / "profile-locks"),
                    browser=replace(runtime.browser, user_data_dir=tmp_path / "synthetic-user-data"))
    plan=_typed_plan(Platform.CHATGPT,run_id='refetch-original',manifest_sha256=digest)
    driver=FakeDriver('chatgpt',{plan.observation_ids[0]:(AnswerSpec('Alpha Clinic'),AnswerSpec('bad json'))})
    runner=Runner(runtime,providers={'chatgpt':driver})
    return runner,plan,driver


@pytest.mark.parametrize('replace_checked_sends', [True, False])
def test_refetch_saved_failure_retains_original_and_request_is_idempotent(tmp_path, replace_checked_sends):
    runner,plan,driver=fixture(tmp_path)
    result=runner.run(plan)
    assert result.status.value != 'succeeded'
    original=runner.repository.run_dir(plan.run_id)/'observations'/f'{plan.observation_ids[0]}.json'
    original_bytes=original.read_bytes()
    original_collection=result.observations[0].collection_metadata
    request=runner.plan_refetch(plan.run_id,request_id='recover-once')
    assert len(request['selections']) == 1
    driver.answers[plan.observation_ids[0]]=(AnswerSpec('Alpha Clinic'),AnswerSpec('["Alpha Clinic"]'))
    recovered=runner.refetch(request, replace_checked_sends=replace_checked_sends)
    assert recovered.status.value == 'succeeded'
    collection=recovered.observations[0].collection_metadata
    assert collection['original_captured_at']==original_collection['captured_at']
    assert collection['captured_at']>original_collection['captured_at']
    assert 'retrieved_at' not in collection and 'recovered_at' not in collection
    assert len(driver.sent)==4
    assert original.read_bytes()==original_bytes
    runner.refetch(request, replace_checked_sends=replace_checked_sends)
    assert len(driver.sent)==4
    assert runner.plan_refetch(plan.run_id,request_id='nothing-left')['selections']==[]


def test_refetch_cleanup_error_releases_scope_before_duplicate_request(tmp_path, monkeypatch):
    from visibility_runner.recovery_adapters.visibility import VisibilityExecution
    runner, plan, driver = fixture(tmp_path)
    assert runner.run(plan).status.value != 'succeeded'
    request = runner.plan_refetch(plan.run_id, request_id='cleanup-error')
    driver.answers[plan.observation_ids[0]] = (AnswerSpec('Alpha Clinic'), AnswerSpec('["Alpha Clinic"]'))
    close = VisibilityExecution.close
    error = OSError('synthetic read-index cleanup failure')
    context_ledger = []

    def fail_close(context):
        context_ledger.append(context.coordinator.ledger)
        close(context)
        raise error

    with monkeypatch.context() as patch:
        patch.setattr(VisibilityExecution, 'close', fail_close)
        with pytest.raises(OSError) as caught:
            runner.refetch(request)
    assert caught.value is error
    assert runner.repository.recovery_execution is None
    assert runner.ownership(plan.run_id)['state'] == 'available'
    assert len(context_ledger) == 1 and context_ledger[0]._changes is None
    authorizations = context_ledger[0].events('pass_authorized')
    sent = tuple(driver.sent)
    assert runner.refetch(request).status.value == 'succeeded'
    assert tuple(driver.sent) == sent
    assert context_ledger[0].events('pass_authorized') == authorizations


def test_preview_read_only_and_stale_rejected_before_send(tmp_path):
    runner,plan,driver=fixture(tmp_path)
    runner.create(plan)
    before={p.relative_to(runner.repository.run_dir(plan.run_id)):p.read_bytes() for p in runner.repository.run_dir(plan.run_id).rglob('*') if p.is_file()}
    request=runner.plan_refetch(plan.run_id,request_id='stale')
    after={p.relative_to(runner.repository.run_dir(plan.run_id)):p.read_bytes() for p in runner.repository.run_dir(plan.run_id).rglob('*') if p.is_file()}
    assert before==after
    runner.cancel(plan.run_id)
    with pytest.raises(StaleRecoveryPreview):
        runner.refetch(request)
    assert not driver.sent


def test_cancelled_unstarted_work_gets_finite_refetch(tmp_path):
    runner,plan,driver=fixture(tmp_path)
    runner.create(plan)
    runner.cancel(plan.run_id)
    request=runner.plan_refetch(plan.run_id,request_id='cancelled-pass')
    driver.answers[plan.observation_ids[0]]=(AnswerSpec('Alpha Clinic'),AnswerSpec('["Alpha Clinic"]'))
    result=runner.refetch(request)
    assert result.status.value == 'succeeded'
    assert len(driver.sent)==2


def test_unresolved_check_fails_then_next_pass_replaces_only_checked_send(tmp_path):
    from visibility_runner.storage.send_attempts import SendAttemptStore
    from visibility_runner.websites.chat_actions import BrowserIdentity
    import hashlib
    runner,plan,driver=fixture(tmp_path)
    runner.create(plan)
    store=SendAttemptStore(runner.repository.run_dir(plan.run_id)/'fences',watch_changes=False)
    item_id=plan.observation_ids[0]
    old=store.prepare(fence_id=f'{item_id}:turn-1',observation_id=item_id,action='measurement_turn_1',
        prompt_sha256=hashlib.sha256(b'Recommend a clinic').hexdigest(),
        identity=BrowserIdentity('profile_a','Default','context:profile_a','old-tab',conversation_reference='https://chatgpt.com/c/old').to_mapping())
    store.mark_prepared(old.fence_id)
    store.begin_send(old.fence_id)
    old_bytes=store.path(old.fence_id).read_bytes()
    first=runner.plan_refetch(plan.run_id,request_id='check-original')
    assert first['selections'][0]['action']=='reconcile'
    runner.refetch(first)
    assert not driver.sent
    assert store.path(old.fence_id).read_bytes()==old_bytes
    second=runner.plan_refetch(plan.run_id,request_id='replace-checked')
    assert second['selections'][0]['replacement_attempt_ids']==[old.fence_id]
    driver.answers[item_id]=(AnswerSpec('Alpha Clinic'),AnswerSpec('["Alpha Clinic"]'))
    result=runner.refetch(second)
    assert result.status.value=='succeeded'
    assert len(driver.sent)==2
    assert store.path(old.fence_id).read_bytes()==old_bytes


@pytest.mark.parametrize('pending_turn', [0, 1])
def test_automatic_refetch_rounds_only_read_uncertain_send_and_late_reply(tmp_path, pending_turn):
    import hashlib
    from dataclasses import replace
    from support.execution import FakeSubmission
    from visibility_runner.storage.send_attempts import SendAttemptStore
    from visibility_runner.websites.chat_actions import (
        BrowserIdentity, ProviderAnswer, ProviderStatus, ReconciliationDisposition,
        ReconciliationResult, ResponseBaseline,
    )

    class Submission(FakeSubmission):
        checkpoint = None

        @property
        def response_baseline(self):
            return ResponseBaseline(self.turn, hashlib.sha256(b'').hexdigest())

        def set_correlation_checkpoint(self, callback):
            self.checkpoint = callback

        async def submit_once(self):
            await super().submit_once()
            if self.checkpoint:
                self.checkpoint({
                    'identity': self.identity.to_mapping(),
                    'prompt_sha256': hashlib.sha256(self.current_prompt.encode()).hexdigest(),
                    'response_baseline': self.response_baseline.to_mapping(),
                    'expected_turn': self.turn + 1,
                    'schema_version': 'browser-conversation-binding-v1',
                })

        async def wait_response(self):
            if self.driver.fail_first and self.turn == pending_turn:
                self.driver.fail_first = False
                raise RuntimeError('usage limit after admitted send')
            return await super().wait_response()

        def restore_recovery_attempt(self, **kwargs):
            self.submitted = True
            self.turn = kwargs['response_baseline']['visible_response_count']

        async def reconcile_no_response(self, grace_seconds):
            self.driver.checked.append(grace_seconds)
            answer_text = 'Alpha Clinic' if pending_turn == 0 else '["Alpha Clinic"]'
            answer = (ProviderAnswer(ProviderStatus.COLLECTED, answer_text, self.identity)
                      if self.driver.reply_available else None)
            disposition = (ReconciliationDisposition.LATE_RESPONSE_CAPTURED if answer
                           else ReconciliationDisposition.AMBIGUOUS)
            return ReconciliationResult(disposition, self.identity, {}, answer=answer)

    class Driver(FakeDriver):
        fail_first = True
        reply_available = False

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.checked = []

        async def open(self, **kwargs):
            opened = await super().open(**kwargs)
            return Submission(self, opened.observation_id, opened.identity, opened.answers)

        async def reopen(self, binding, *, observation_id):
            identity = replace(BrowserIdentity.from_mapping(binding['identity']),
                               tab_reference='reopened-tab', browser_target_reference='reopened-target')
            return Submission(self, observation_id, identity, self.answers[observation_id])

        async def restore(self, identity, *, observation_id):
            raise AssertionError('recovery must use the correlated reopened conversation')

    runner, plan, _ = fixture(tmp_path)
    item_id = plan.observation_ids[0]
    driver = Driver('chatgpt', {item_id: (AnswerSpec('Alpha Clinic'), AnswerSpec('["Alpha Clinic"]'))})
    runner = Runner(runner.runtime, providers={'chatgpt': driver})
    assert runner.run(plan).status.value != 'succeeded'
    original_send_count = pending_turn + 1
    assert len(driver.sent) == original_send_count
    store = SendAttemptStore(runner.repository.run_dir(plan.run_id) / 'fences', watch_changes=False)
    action = 'measurement_turn_1' if pending_turn == 0 else 'same_chat_turn_2'
    original = next(record for record in store.snapshot().records if record.action == action)
    original_bytes = store.path(original.fence_id).read_bytes()

    with runner.execution(plan.run_id, execution_id='automatic-worker'):
        for round_number in range(1, 4):
            request = runner.plan_refetch(plan.run_id, request_id=f'automatic-check-{round_number}')
            if round_number > 1:
                assert request['selections'][0]['replacement_attempt_ids'] == [original.fence_id]
            result = runner.refetch(request, replace_checked_sends=False)
            assert result.status.value != 'succeeded'
            assert len(driver.sent) == original_send_count
            assert store.path(original.fence_id).read_bytes() == original_bytes

        driver.reply_available = True
        request = runner.plan_refetch(plan.run_id, request_id='automatic-check-4')
        result = runner.refetch(request, replace_checked_sends=False)
    assert result.status.value == 'succeeded'
    assert len(driver.sent) == 2
    assert driver.sent[-1][1] == 1  # The original or previously unsent extraction.
    assert len(driver.checked) == 4
    assert store.path(original.fence_id).read_bytes() == original_bytes
    assert runner.failure_readout(plan.run_id)['accounting']['unknown_effects'] == 0


def test_automatic_refetch_keeps_stop_at_round_transition_and_new_execution_can_recover(tmp_path, monkeypatch):
    runner, plan, driver = fixture(tmp_path)
    runner.run(plan)
    with runner.execution(plan.run_id, execution_id='automatic-worker') as owner:
        request = runner.plan_refetch(plan.run_id, request_id='automatic-round-1')
        runner.refetch(request, replace_checked_sends=False)
        request = runner.plan_refetch(plan.run_id, request_id='automatic-round-2')
        sent = tuple(driver.sent)
        update_state = runner.repository.update_state

        def stop_before_reopen(run_id, mutate, **kwargs):
            if mutate.__name__ == 'reopen_state':
                runner.cancel(run_id, execution_id=owner.execution_id)
            return update_state(run_id, mutate, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(runner.repository, 'update_state', stop_before_reopen)
            result = runner.refetch(request, replace_checked_sends=False)
        assert result.status.value != 'succeeded'
        assert runner.repository.read_progress(plan.run_id)['cancel_requested'] is True
        assert tuple(driver.sent) == sent

    # A later deliberate action has a new execution and may clear the old Stop.
    driver.answers[plan.observation_ids[0]] = (AnswerSpec('Alpha Clinic'), AnswerSpec('["Alpha Clinic"]'))
    request = runner.plan_refetch(plan.run_id, request_id='new-explicit-action')
    with runner.execution(plan.run_id, execution_id='new-automatic-worker'):
        result = runner.refetch(request, replace_checked_sends=False)
    assert result.status.value == 'succeeded'
    assert runner.repository.read_progress(plan.run_id)['cancel_requested'] is False
    assert len(driver.sent) == len(sent) + 2


@pytest.mark.parametrize('crash_after_recovery',[False,True])
def test_recovered_turn_one_continues_only_unsent_second_turn(tmp_path,crash_after_recovery):
    import hashlib
    from dataclasses import replace
    from support.execution import FakeSubmission
    from visibility_runner.websites.chat_actions import ProviderAnswer,ProviderStatus,ReconciliationResult,ReconciliationDisposition
    class Submission(FakeSubmission):
        checkpoint=None
        @property
        def response_baseline(self):
            from visibility_runner.websites.chat_actions import ResponseBaseline
            return ResponseBaseline(self.turn,hashlib.sha256(b'').hexdigest())
        def set_correlation_checkpoint(self,callback): self.checkpoint=callback
        async def submit_once(self):
            await super().submit_once()
            if self.checkpoint:
                self.checkpoint({'identity':self.identity.to_mapping(),'prompt_sha256':hashlib.sha256(self.current_prompt.encode()).hexdigest(),'response_baseline':self.response_baseline.to_mapping(),'expected_turn':self.turn+1,'schema_version':'browser-conversation-binding-v1'})
        async def wait_response(self):
            if self.driver.fail_first and self.turn==0:
                self.driver.fail_first=False
                raise RuntimeError('connection lost after sent')
            return await super().wait_response()
        def restore_recovery_attempt(self,**kwargs): self.submitted=True
        async def reconcile_no_response(self,grace_seconds):
            self.driver.checked.append(grace_seconds)
            return ReconciliationResult(ReconciliationDisposition.LATE_RESPONSE_CAPTURED,self.identity,{},answer=ProviderAnswer(ProviderStatus.COLLECTED,'Alpha Clinic',self.identity))
    class Driver(FakeDriver):
        fail_first=True
        checked=[]
        async def open(self,**kwargs):
            old=await super().open(**kwargs)
            return Submission(self,old.observation_id,old.identity,old.answers)
        async def reopen(self,binding,*,observation_id):
            from visibility_runner.websites.chat_actions import BrowserIdentity
            identity=replace(BrowserIdentity.from_mapping(binding['identity']),tab_reference='new-owned-tab',browser_target_reference='new-target')
            return Submission(self,observation_id,identity,self.answers[observation_id])
        async def restore(self,identity,*,observation_id):
            raise AssertionError('recovery must continue its verified submission, never restore the stale tab')
    runner,plan,_=fixture(tmp_path)
    driver=Driver('chatgpt',{plan.observation_ids[0]:(AnswerSpec('Alpha Clinic'),AnswerSpec('["Alpha Clinic"]'))})
    runner=Runner(runner.runtime,providers={'chatgpt':driver})
    runner.run(plan)
    assert len(driver.sent)==1
    request=runner.plan_refetch(plan.run_id,request_id='late-source')
    if crash_after_recovery:
        import asyncio
        from support.execution import _registry
        from visibility_runner.recovery.api import RecoveryLedger,RecoveryCoordinator,RecoveryRequest
        from visibility_runner.recovery_adapters.visibility import snapshot_for,VisibilityExecution
        from visibility_runner.execution.run_measurement_group import MeasurementGroupWorker
        with runner.execution(plan.run_id) as owner:
            snapshot,verified=snapshot_for(runner.repository,plan.run_id,runtime=runner.runtime)
            coordinator=RecoveryCoordinator(RecoveryLedger(runner.repository.run_dir(plan.run_id)))
            recovery_pass=coordinator.authorize(RecoveryRequest.from_dict(request),snapshot,owner)
            context=VisibilityExecution(runner.repository,snapshot,verified,recovery_pass,coordinator,owner)
            runner.repository.recovery_execution=context
            runner.repository.update_state(plan.run_id,lambda state:state.update(status='running',execution_id=owner.execution_id,recovery_pass_id=recovery_pass.pass_id,cancel_requested=False))
            worker=MeasurementGroupWorker(runner.repository,_registry(driver))
            item_id=plan.observation_ids[0]
            assert asyncio.run(context.prepare_item(worker,item_id,verified.plan['observation_plan']['observations'][0],verified.plan['packets'][0]))
            alias=runner.repository.run_dir(plan.run_id)/'fences'
            saved={str(path):path.read_bytes() for path in alias.glob('*.json')}
            runner.repository.recovery_execution=None
        driver.checked=[]
        result=runner.resume(plan.run_id)
        assert all(Path(path).read_bytes()==value for path,value in saved.items())
    else:
        result=runner.refetch(request)
    assert result.status.value=='succeeded'
    assert len(driver.sent)==2
    assert driver.sent[-1][1]==1
    collection=result.observations[0].collection_metadata
    assert collection['retrieved_at']==collection['captured_at']==collection['recovered_at']
    assert 'original_captured_at' not in collection
    assert driver.checked==[plan.retry_policy.response_timeouts.chatgpt_turn_1_seconds+plan.retry_policy.late_response_grace_seconds]
    readout=runner.failure_readout(plan.run_id)
    assert readout['items'][0]['external_outcome_unknown'] is False
    assert readout['accounting']['unknown_effects']==0




def test_strict_resume_preserves_authorized_pass_and_prepared_allowance(tmp_path):
    import hashlib
    from visibility_runner.recovery.api import RecoveryLedger,RecoveryCoordinator,RecoveryRequest
    from visibility_runner.recovery_adapters.visibility import snapshot_for,VisibilityExecution
    from visibility_runner.storage.send_attempts import SendAttemptStore
    from visibility_runner.websites.chat_actions import BrowserIdentity
    runner,plan,driver=fixture(tmp_path)
    runner.create(plan)
    item_id=plan.observation_ids[0]
    with runner.execution(plan.run_id) as owner:
        snapshot,verified=snapshot_for(runner.repository,plan.run_id)
        coordinator=RecoveryCoordinator(RecoveryLedger(runner.repository.run_dir(plan.run_id)))
        request=RecoveryRequest.from_preview(coordinator.plan(snapshot),'initial:'+plan.run_id)
        recovery_pass=coordinator.authorize(request,snapshot,owner)
        context=VisibilityExecution(runner.repository,snapshot,verified,recovery_pass,coordinator,owner,initial=True)
        values=dict(fence_id=f'{item_id}:turn-1',observation_id=item_id,action='measurement_turn_1',prompt_sha256=hashlib.sha256(b'Recommend a clinic').hexdigest(),identity=BrowserIdentity('profile_a','Default','context:profile_a','prepared-tab',conversation_reference='https://chatgpt.com/c/saved').to_mapping())
        context.before_prepare(values)
        store=SendAttemptStore(runner.repository.run_dir(plan.run_id)/'fences',watch_changes=False)
        store.prepare(**values)
        store.mark_prepared(values['fence_id'])
        runner.repository.update_state(plan.run_id,lambda state:state.update(status='running'))
        pass_id=recovery_pass.pass_id
    driver.answers[item_id]=(AnswerSpec('Alpha Clinic'),AnswerSpec('["Alpha Clinic"]'))
    result=runner.resume(plan.run_id)
    assert result.status.value=='succeeded'
    events=RecoveryLedger(runner.repository.run_dir(plan.run_id)).read()
    assert [e['payload']['pass_id'] for e in events if e['kind']=='pass_authorized']==[pass_id]
    assert len([e for e in events if e['kind']=='attempt_started'])==2
    assert len(driver.sent)==2


def test_reconstruct_accepted_chat_outputs_without_browser_attachment(tmp_path):
    import asyncio
    from support.execution import _registry
    from visibility_runner.execution.run_measurement_group import MeasurementGroupWorker
    runner,plan,driver=fixture(tmp_path)
    runner.create(plan)
    driver.answers[plan.observation_ids[0]]=(AnswerSpec('Alpha Clinic'),AnswerSpec('["Alpha Clinic"]'))
    stored=runner.repository.read_plan(plan.run_id)
    asyncio.run(MeasurementGroupWorker(runner.repository,_registry(driver)).execute(run_id=plan.run_id,plan=stored,packet=stored['packets'][0]))
    result_path=runner.repository.run_dir(plan.run_id)/'observations'/f'{plan.observation_ids[0]}.json'
    result_path.unlink() # Synthetic fixture: crash after accepted capture, before final output.
    no_browser=Runner(runner.runtime,browser_runtime_factory=lambda config: (_ for _ in ()).throw(AssertionError('browser forbidden')))
    request=no_browser.plan_refetch(plan.run_id,request_id='rebuild-output')
    assert request['selections'][0]['action']=='reconstruct'
    result=no_browser.refetch(request)
    assert result.status.value=='succeeded'
    assert len(driver.sent)==2


def test_runner_execution_reentrancy_is_same_thread_and_run_only(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from visibility_runner.errors import StateTransitionError
    runner,plan,driver=fixture(tmp_path)
    runner.create(plan)
    request=runner.plan_refetch(plan.run_id,request_id='concurrent-request')
    with runner.execution(plan.run_id,execution_id='healthy-execution') as owner:
        with runner.execution(plan.run_id) as nested:
            assert nested is owner
        with pytest.raises(StateTransitionError,match='runner_execution_already_owned'):
            with runner.execution('different-run'):
                pytest.fail('cross-run repository context was shared')
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(runner.refetch,request)
            with pytest.raises(StateTransitionError,match='runner_execution_already_owned'):
                future.result(timeout=2)
        assert runner._owners[plan.run_id] is owner
        owner.assert_owned()
    assert not driver.sent
    from visibility_runner.recovery.api import RecoveryLedger
    assert not RecoveryLedger(runner.repository.run_dir(plan.run_id)).read()


def test_terminal_original_pass_is_not_reauthorized_by_resume(tmp_path):
    from visibility_runner.recovery.api import RecoveryLedger
    runner,plan,driver=fixture(tmp_path)
    runner.run(plan)
    ledger=RecoveryLedger(runner.repository.run_dir(plan.run_id))
    before=tuple((event['kind'],event['checksum']) for event in ledger.read())
    sends=len(driver.sent)
    fresh=Runner(runner.runtime,providers={'chatgpt':driver})
    fresh.resume(plan.run_id)
    assert len(driver.sent)==sends
    assert tuple((event['kind'],event['checksum']) for event in ledger.read())==before


def test_containment_holds_exact_lane_and_blocks_only_failed_scope():
    import asyncio
    from types import SimpleNamespace
    from visibility_runner.recovery_adapters.visibility import _contain_resources
    from visibility_runner.websites.chat_actions import BrowserIdentity
    events=[]
    held=set()
    identities={profile:BrowserIdentity(profile,'Default','context:'+profile,'old:'+profile,
        browser_target_reference='target:'+profile,conversation_reference='https://chatgpt.com/c/'+profile).to_mapping()
        for profile in ('profile_a','profile_b')}
    class Ledger:
        def read(self): return ()
        def append(self,kind,payload,owner): events.append((kind,payload))
    class Lease:
        def __init__(self,profile): self.profile=profile
        def release(self): held.remove(self.profile)
    class Leases:
        def acquire_containment(self,profiles,**kwargs):
            profile=profiles[0]
            assert kwargs['resource_references']==('tab:old:'+profile,)
            if profile=='profile_a': raise RuntimeError('unrelated owner still active')
            held.add(profile)
            return Lease(profile)
        def containment_requirements(self,profile,*,provider):
            return ()
        def resolve_containment(self,lease,*,resource_references,record_resolution):
            assert lease.profile in held
            record_resolution({'resource_references':resource_references})
    class Page:
        def __init__(self,profile): self.profile,self.closed=profile,False
        def close(self):
            assert self.profile in held
            self.closed=True
        def is_closed(self): return self.closed
    def factory(platform,profile,item,fresh,identity):
        assert profile in held
        return Page(profile)
    context=SimpleNamespace(
        coordinator=SimpleNamespace(ledger=Ledger()),owner=SimpleNamespace(execution_id='exclusive'),pass_id='finite-pass',
        blocked_scopes=set(),assert_owned=lambda:None,
        record_resource=lambda resource,status,owner,**kwargs:events.append(('resource',{'resource_id':resource,'status':status})),
        snapshot=SimpleNamespace(manifest=SimpleNamespace(run_id='synthetic',operations=())),
        verified=SimpleNamespace(attempt_records=tuple(SimpleNamespace(identity=identity,ack_identity=None,
            observation_id=profile,action='measurement_turn_1') for profile,identity in identities.items()),
            plan={'observation_plan':{'observations':[{'observation_id':profile,'platform':'chatgpt'} for profile in identities]}}))
    attachment=SimpleNamespace(lease_manager=Leases(),manifest=SimpleNamespace(profile=lambda profile:profile),provider_page_factory=factory)
    asyncio.run(_contain_resources(context,attachment))
    assert context.blocked_scopes=={'profile_a:chatgpt'}
    assert not held
    assert ('resource',{'resource_id':'tab:old:profile_b','status':'released'}) in events
    assert not any(kind=='resource' and row['resource_id']=='tab:old:profile_a' and row['status']=='released' for kind,row in events)
    assert any(kind=='resource_containment_resolved' for kind,_ in events)


def test_generic_cleanup_preserves_unresolved_prior_resources(tmp_path):
    from visibility_runner.recovery.api import RecoveryLedger,RecoveryCoordinator,RecoveryRequest
    from visibility_runner.recovery_adapters.visibility import snapshot_for,VisibilityExecution
    runner,plan,_=fixture(tmp_path)
    runner.create(plan)
    with runner.execution(plan.run_id) as owner:
        snapshot,verified=snapshot_for(runner.repository,plan.run_id)
        coordinator=RecoveryCoordinator(RecoveryLedger(runner.repository.run_dir(plan.run_id)))
        recovery_pass=coordinator.authorize(RecoveryRequest.from_preview(coordinator.plan(snapshot),'cleanup'),snapshot,owner)
        context=VisibilityExecution(runner.repository,snapshot,verified,recovery_pass,coordinator,owner)
        context.record_resource('old-blocked','blocked',owner,reference={'item_id':plan.observation_ids[0]})
        context.record_resource('new-owned','owned',owner,reference={'item_id':plan.observation_ids[0]})
        context.record_cleanup(True)
        latest={event['payload']['resource_id']:event['payload']['status'] for event in coordinator.ledger.read() if event['kind']=='resource_observed'}
        assert latest=={'old-blocked':'blocked','new-owned':'released'}




def test_durable_containment_proof_resolves_leftover_marker_without_reopening():
    import asyncio
    from types import SimpleNamespace
    from visibility_runner.recovery_adapters.visibility import _contain_resources
    from visibility_runner.websites.chat_actions import BrowserIdentity
    identity=BrowserIdentity('profile_a','Default','context:a','old:a').to_mapping()
    committed={'kind':'resource_observed','payload':{'resource_id':'tab:old:a','status':'released',
        'reference':{'identity':identity,'platform':'chatgpt','item_id':'item','resource_scopes':('profile_a:chatgpt',)}}}
    history=[]
    held=[]
    lease=SimpleNamespace(release=lambda:held.pop())
    def acquire(*args,**kwargs):
        held.append(True)
        assert kwargs['resource_references']==('tab:old:a',)
        return lease
    def resolve(owned,*,resource_references,record_resolution):
        assert held and owned is lease
        record_resolution({'resource_references':resource_references})
    context=SimpleNamespace(coordinator=SimpleNamespace(ledger=SimpleNamespace(read=lambda:(committed,),
        append=lambda kind,payload,owner:history.append((kind,payload)))),owner=SimpleNamespace(execution_id='owned'),
        pass_id='finite',assert_owned=lambda:None,blocked_scopes=set(),
        record_resource=lambda *args,**kwargs:pytest.fail('committed closure was altered'),
        snapshot=SimpleNamespace(manifest=SimpleNamespace(run_id='synthetic',operations=())),
        verified=SimpleNamespace(attempt_records=(),plan={'observation_plan':{'observations':[{'observation_id':'item','platform':'chatgpt'}]}}))
    attachment=SimpleNamespace(lease_manager=SimpleNamespace(acquire_containment=acquire,resolve_containment=resolve,
        containment_requirements=lambda *a,**k:()),
        manifest=SimpleNamespace(profile=lambda value:value),provider_page_factory=lambda *args:pytest.fail('closed tab reopened'))
    asyncio.run(_contain_resources(context,attachment))
    assert not context.blocked_scopes and not held
    assert history[0][0]=='resource_containment_resolved'


def test_failure_readout_separates_process_liveness_from_released_ownership(tmp_path,monkeypatch):
    runner,plan,_=fixture(tmp_path)
    runner.create(plan)
    monkeypatch.setattr(runner,'ownership',lambda run_id:{'status':'alive','state':'available','execution_id':'released-process'})
    readout=runner.failure_readout(plan.run_id)
    assert readout['worker']['status']=='alive'
    assert readout['worker']['ownership_status']=='available'
    assert readout['items'][0]['status']=='never_started'


def test_crash_after_effect_admission_before_send_fence_retains_uncertainty(tmp_path, monkeypatch):
    import hashlib
    from visibility_runner.recovery.api import RecoveryLedger,RecoveryCoordinator,RecoveryRequest
    from visibility_runner.recovery_adapters.visibility import snapshot_for,VisibilityExecution
    from visibility_runner.storage.send_attempts import SendAttemptStore
    from visibility_runner.storage import send_attempts
    from visibility_runner.errors import AmbiguousSendError
    from visibility_runner.websites.chat_actions import BrowserIdentity
    runner,plan,driver=fixture(tmp_path)
    runner.create(plan)
    item_id=plan.observation_ids[0]
    with runner.execution(plan.run_id) as owner:
        snapshot,verified=snapshot_for(runner.repository,plan.run_id)
        coordinator=RecoveryCoordinator(RecoveryLedger(runner.repository.run_dir(plan.run_id)))
        authorized=coordinator.authorize(RecoveryRequest.from_preview(coordinator.plan(snapshot),'initial:'+plan.run_id),snapshot,owner)
        context=VisibilityExecution(runner.repository,snapshot,verified,authorized,coordinator,owner,initial=True)
        values=dict(fence_id=f'{item_id}:turn-1',observation_id=item_id,action='measurement_turn_1',
            prompt_sha256=hashlib.sha256(b'Recommend a clinic').hexdigest(),
            identity=BrowserIdentity('profile_a','Default','context:profile_a','admitted-tab').to_mapping())
        context.before_prepare(values)
        store=SendAttemptStore(runner.repository.run_dir(plan.run_id)/'fences',watch_changes=False)
        store.prepare(**values)
        fence=store.mark_prepared(values['fence_id'])
        write = send_attempts.atomic_write_json
        def fail_send_fence(path, value, **kwargs):
            if value.get('state') == 'send_started':
                raise OSError('synthetic interruption before the detailed send fence')
            return write(path, value, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(send_attempts, 'atomic_write_json', fail_send_fence)
            with pytest.raises(AmbiguousSendError):
                store.admit_send(fence.fence_id, record_admission=context._admit_send)
            with pytest.raises(AmbiguousSendError):
                store.admit_send(fence.fence_id, record_admission=context._admit_send)
        assert len([event for event in coordinator.ledger.events('effect_admitted')
                    if event['payload']['action'] == 'submission']) == 1
    original=store.path(values['fence_id']).read_bytes()
    first=runner.plan_refetch(plan.run_id,request_id='check-admission')
    assert first['selections'][0]['action']=='reconcile'
    runner.refetch(first)
    assert not driver.sent
    assert store.path(values['fence_id']).read_bytes()==original
    second=runner.plan_refetch(plan.run_id,request_id='replace-checked-admission')
    assert second['selections'][0]['replacement_attempt_ids']==[values['fence_id']]
    driver.answers[item_id]=(AnswerSpec('Alpha Clinic'),AnswerSpec('["Alpha Clinic"]'))
    assert runner.refetch(second).status.value=='succeeded'
    assert len(driver.sent)==2
    assert runner.failure_readout(plan.run_id)['accounting']['unknown_effects']>=1


@pytest.mark.parametrize('fault', ['before_effect', 'after_effect', 'before_fence', 'after_fence'])
def test_send_admission_failure_never_repeats_a_durable_effect(tmp_path, monkeypatch, fault):
    from visibility_runner.recovery import ledger as ledger_module
    from visibility_runner.recovery.api import RecoveryCoordinator, RecoveryLedger
    from visibility_runner.storage import send_attempts
    runner, plan, driver = fixture(tmp_path)
    admit_effect = RecoveryCoordinator.admit_effect
    publish = ledger_module.publish_json
    write = send_attempts.atomic_write_json

    def fail_before_effect(self, *args, **kwargs):
        if args[2] == 'submission':
            raise OSError('synthetic failure before effect admission')
        return admit_effect(self, *args, **kwargs)

    def fail_after_effect(path, value, **kwargs):
        result = publish(path, value, **kwargs)
        if (value.get('kind') == 'effect_admitted'
                and value['payload']['action'] == 'submission'):
            raise OSError('synthetic interruption after effect publication')
        return result

    def fail_fence(path, value, **kwargs):
        if value.get('state') == 'send_started':
            if fault == 'after_fence':
                write(path, value, **kwargs)
            raise OSError('synthetic interruption at the detailed send fence')
        return write(path, value, **kwargs)

    if fault == 'before_effect':
        monkeypatch.setattr(RecoveryCoordinator, 'admit_effect', fail_before_effect)
    elif fault == 'after_effect':
        monkeypatch.setattr(ledger_module, 'publish_json', fail_after_effect)
    else:
        monkeypatch.setattr(send_attempts, 'atomic_write_json', fail_fence)
    result = runner.run(plan)
    ledger = RecoveryLedger(runner.repository.run_dir(plan.run_id), watch_changes=False)
    try:
        submissions = [event for event in ledger.events('effect_admitted')
                       if event['payload']['action'] == 'submission']
    finally:
        ledger.close()
    assert not driver.sent
    if fault == 'before_effect':
        assert result.status.value == 'failed'
        assert len(driver.opens) == 1  # This fixture freezes one attempt.
        assert not submissions
    else:
        assert result.status.value == 'needs_attention', [issue.message for issue in result.issues]
        assert runner.inspect(plan.run_id).status.value == 'needs_attention'
        assert len(driver.opens) == len(submissions) == 1
        request = runner.plan_refetch(plan.run_id, request_id='reconcile-admission')
        assert request['selections'][0]['action'] == 'reconcile'


def test_failed_replacement_does_not_recheck_superseded_unknown_forever(tmp_path):
    import hashlib
    from visibility_runner.storage.send_attempts import SendAttemptStore,SendAttemptState
    from visibility_runner.websites.chat_actions import BrowserIdentity
    runner,plan,driver=fixture(tmp_path)
    runner.create(plan)
    item_id=plan.observation_ids[0]
    store=SendAttemptStore(runner.repository.run_dir(plan.run_id)/'fences',watch_changes=False)
    old=store.prepare(fence_id=f'{item_id}:turn-1',observation_id=item_id,action='measurement_turn_1',
        prompt_sha256=hashlib.sha256(b'Recommend a clinic').hexdigest(),
        identity=BrowserIdentity('profile_a','Default','context:profile_a','old-tab').to_mapping())
    store.mark_prepared(old.fence_id)
    store.begin_send(old.fence_id)
    runner.refetch(runner.plan_refetch(plan.run_id,request_id='check-old'))
    driver.answers[item_id]=() # New replacement sends, then loses its response.
    runner.refetch(runner.plan_refetch(plan.run_id,request_id='replace-old'))
    assert len(driver.sent)==1
    unknowns=[record for record in store.iter_records() if record.state is SendAttemptState.SEND_STARTED]
    new=next(record for record in unknowns if record.fence_id!=old.fence_id)
    check_new=runner.plan_refetch(plan.run_id,request_id='check-new')
    assert check_new['selections'][0]['action']=='reconcile'
    assert check_new['selections'][0]['replacement_attempt_ids']==[]
    runner.refetch(check_new)
    assert len(driver.sent)==1
    replace_new=runner.plan_refetch(plan.run_id,request_id='replace-new')
    assert replace_new['selections'][0]['replacement_attempt_ids']==[new.fence_id]
    driver.answers[item_id]=(AnswerSpec('Alpha Clinic'),AnswerSpec('["Alpha Clinic"]'))
    assert runner.refetch(replace_new).status.value=='succeeded'
    assert len(driver.sent)==3
    item=runner.failure_readout(plan.run_id)['items'][0]
    assert item['external_outcome_unknown'] is True
    assert set(item['historical_unresolved_attempt_ids'])=={old.fence_id,new.fence_id}



def test_unstarted_plan_readout_is_pure(tmp_path):
    runner,plan,driver=fixture(tmp_path)
    run_dir=runner.repository.run_dir(plan.run_id)
    assert not run_dir.exists()
    readout=runner.failure_readout_for_plan(plan)
    assert readout['items'][0]['status']=='never_started'
    assert readout['items'][0]['next_action']=='execute'
    assert not run_dir.exists() and not driver.sent


def test_cancel_cannot_mutate_packets_after_execution_changes(tmp_path,monkeypatch):
    from visibility_runner.errors import StateTransitionError
    runner,plan,driver=fixture(tmp_path)
    runner.create(plan)
    runner.repository.update_state(plan.run_id,lambda state:state.update(execution_id='old'))
    group_path=runner.repository.run_dir(plan.run_id)/'packets'/plan.packet_ids[0]/'state.json'
    before=group_path.read_bytes()
    update=runner.repository.update_state
    def change_epoch_after_first_barrier(run_id,mutate,**kwargs):
        result=update(run_id,mutate,**kwargs)
        update(run_id,lambda state:state.update(execution_id='new',cancel_requested=False,status='running'))
        return result
    monkeypatch.setattr(runner.repository,'update_state',change_epoch_after_first_barrier)
    with pytest.raises(StateTransitionError,match='stale_execution_cancellation'):
        runner.cancel(plan.run_id,execution_id='old')
    assert group_path.read_bytes()==before
    assert runner.repository.read_progress(plan.run_id)['cancel_requested'] is False
    assert not driver.sent


@pytest.mark.parametrize('recovered_text,expected_sends',[( '["Alpha Clinic"]',2),('invalid extraction',4)])
def test_second_turn_recovery_validates_before_commit_or_unit_restart(tmp_path,recovered_text,expected_sends):
    import hashlib
    from dataclasses import replace
    from support.execution import FakeSubmission
    from support.retries import _policy, _typed_plan as retry_plan
    from visibility_runner.websites.chat_actions import ProviderAnswer,ProviderStatus,ReconciliationResult,ReconciliationDisposition,ResponseBaseline,BrowserIdentity
    class Submission(FakeSubmission):
        checkpoint=None
        @property
        def response_baseline(self):
            return ResponseBaseline(self.turn,hashlib.sha256(('Alpha Clinic' if self.turn else '').encode()).hexdigest())
        def set_correlation_checkpoint(self,callback):self.checkpoint=callback
        async def submit_once(self):
            await super().submit_once()
            if self.checkpoint:
                self.checkpoint({'identity':self.identity.to_mapping(),'prompt_sha256':hashlib.sha256(self.current_prompt.encode()).hexdigest(),
                    'response_baseline':self.response_baseline.to_mapping(),'expected_turn':self.turn+1,'schema_version':'browser-conversation-binding-v1'})
        async def wait_response(self):
            if self.turn==1 and self.driver.lose_second:
                self.driver.lose_second=False
                raise RuntimeError('second response lost after send')
            return await super().wait_response()
        def restore_recovery_attempt(self,**kwargs):self.submitted=True
        async def reconcile_no_response(self,grace_seconds):
            return ReconciliationResult(ReconciliationDisposition.LATE_RESPONSE_CAPTURED,self.identity,{},
                answer=ProviderAnswer(ProviderStatus.COLLECTED,recovered_text,self.identity))
    class Driver(FakeDriver):
        lose_second=True
        async def open(self,**kwargs):
            original=await super().open(**kwargs)
            return Submission(self,original.observation_id,original.identity,original.answers)
        async def reopen(self,binding,*,observation_id):
            identity=replace(BrowserIdentity.from_mapping(binding['identity']),tab_reference='verified-reopened',browser_target_reference='verified-target')
            return Submission(self,observation_id,identity,self.answers[observation_id])
    runtime,digest=_runtime(tmp_path)
    policy=_policy()
    runtime=replace(runtime,retry_policy=policy,
        profiles=replace(runtime.profiles, lease_lock_root=tmp_path / "profile-locks"),
        browser=replace(runtime.browser, user_data_dir=tmp_path / "synthetic-user-data"))
    base=_typed_plan(Platform.CHATGPT,run_id='recover-second',manifest_sha256=digest)
    plan=retry_plan(Platform.CHATGPT,run_id=base.run_id,manifest_sha256=digest,profile=base.selected_profiles[0],retry_policy=policy)
    item_id=plan.observation_ids[0]
    driver=Driver('chatgpt',{item_id:(AnswerSpec('Alpha Clinic'),AnswerSpec('["Alpha Clinic"]'))})
    runner=Runner(runtime,providers={'chatgpt':driver})
    runner.run(plan)
    assert len(driver.sent)==2
    assert runner.refetch(runner.plan_refetch(plan.run_id,request_id='recover-second')).status.value=='succeeded'
    assert len(driver.sent)==expected_sends
    assert runner.failure_readout(plan.run_id)['items'][0]['external_outcome_unknown'] is False


def test_created_run_freezes_runtime_and_rejects_changed_execution(tmp_path):
    from dataclasses import replace
    from visibility_runner.errors import StateTransitionError
    runner,plan,driver=fixture(tmp_path)
    runner.create(plan)
    policy=runner.repository.run_dir(plan.run_id)/'execution-policy.json'
    original=policy.read_bytes()
    changed=Runner(replace(runner.runtime,packet_start_stagger_seconds=runner.runtime.packet_start_stagger_seconds+1),providers={'chatgpt':driver})
    with pytest.raises(StateTransitionError,match='frozen_execution_policy_mismatch'):
        changed.run_packet(plan.run_id,plan.packets[0].packet_id)
    assert policy.read_bytes()==original
    assert not driver.sent


def test_legacy_preview_binds_runtime_before_any_policy_publication(tmp_path):
    from dataclasses import replace
    runner,plan,driver=fixture(tmp_path)
    runner.repository.create(plan)
    policy=runner.repository.run_dir(plan.run_id)/'execution-policy.json'
    original_plan=(runner.repository.run_dir(plan.run_id)/'plan.json').read_bytes()
    request=runner.plan_refetch(plan.run_id,request_id='legacy-runtime')
    assert not policy.exists()
    changed=Runner(replace(runner.runtime,packet_start_stagger_seconds=runner.runtime.packet_start_stagger_seconds+1),providers={'chatgpt':driver})
    with pytest.raises(StaleRecoveryPreview):
        changed.refetch(request)
    assert not policy.exists() and not driver.sent
    driver.answers[plan.observation_ids[0]]=(AnswerSpec('Alpha Clinic'),AnswerSpec('["Alpha Clinic"]'))
    assert runner.refetch(request).status.value=='succeeded'
    import json
    assert json.loads(policy.read_text())['source']=='legacy_runtime_attestation'
    assert (runner.repository.run_dir(plan.run_id)/'plan.json').read_bytes()==original_plan


@pytest.mark.parametrize('damage',['missing','corrupt'])
def test_missing_or_corrupt_required_execution_policy_blocks(tmp_path,damage):
    from visibility_runner.errors import IntegrityError
    runner,plan,driver=fixture(tmp_path)
    runner.create(plan)
    path=runner.repository.run_dir(plan.run_id)/'execution-policy.json'
    if damage=='missing':path.unlink()
    else:path.write_text('{')
    with pytest.raises(IntegrityError,match='frozen_execution_policy'):
        runner.plan_refetch(plan.run_id,request_id='damaged-policy')
    assert not driver.sent


def test_unconfirmed_attachment_stays_blocked_without_temporary_marker(tmp_path):
    from visibility_runner.recovery.api import RecoveryLedger,RecoveryCoordinator
    runner,plan,driver=fixture(tmp_path)
    runner.create(plan)
    with runner.execution(plan.run_id) as owner:
        coordinator=RecoveryCoordinator(RecoveryLedger(runner.repository.run_dir(plan.run_id)))
        coordinator.record_resource('attachment:'+owner.execution_id,'blocked',owner,
            reference={'resource_kind':'local_attachment','execution_id':owner.execution_id,
                       'process_identity':owner.identity.to_dict(),'resource_scopes':('profile_a:chatgpt',)})
    request=runner.plan_refetch(plan.run_id,request_id='attachment-blocked')
    assert request['selections'][0]['action']=='blocked'
    assert request['selections'][0]['reason_code']=='previous_attachment_cleanup_unconfirmed'
    runner.refetch(request)
    assert not driver.sent
    assert runner.failure_readout(plan.run_id)['items'][0]['reason_code']=='previous_attachment_cleanup_unconfirmed'


@pytest.mark.parametrize('damage',['missing','corrupt'])
def test_completed_progress_rebuild_is_read_only_and_never_resends(tmp_path,damage):
    runner,plan,driver=fixture(tmp_path)
    driver.answers[plan.observation_ids[0]]=(AnswerSpec('Alpha Clinic'),AnswerSpec('["Alpha Clinic"]'))
    assert runner.run(plan).status.value=='succeeded'
    root=runner.repository.run_dir(plan.run_id)
    paths=(root/'runner_state.json',root/'packets'/plan.packets[0].packet_id/'state.json')
    for path in paths:
        if damage=='missing':path.unlink()
        else:path.write_text('{')
    before={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert runner.inspect(plan.run_id).status.value=='succeeded'
    assert runner.plan_refetch(plan.run_id,request_id='rebuild-index')['selections']==[]
    assert before=={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert len(driver.sent)==2


def test_cancel_control_survives_lost_projection_and_failed_flush(tmp_path,monkeypatch):
    from visibility_runner.recovery.api import RecoveryLedger,RecoveryCoordinator,RecoveryRequest
    from visibility_runner.recovery_adapters.visibility import snapshot_for
    import visibility_runner.storage.measurement_files as files
    runner,plan,driver=fixture(tmp_path)
    runner.create(plan)
    with runner.execution(plan.run_id) as owner:
        snapshot,_=snapshot_for(runner.repository,plan.run_id,runtime=runner.runtime)
        coordinator=RecoveryCoordinator(RecoveryLedger(runner.repository.run_dir(plan.run_id)))
        recovery_pass=coordinator.authorize(RecoveryRequest.from_preview(coordinator.plan(snapshot),'initial:'+plan.run_id),snapshot,owner)
        runner.repository.update_state(plan.run_id,lambda state:state.update(status='running',execution_id=owner.execution_id,recovery_pass_id=recovery_pass.pass_id))
    original_write=files.atomic_write_json
    def fail_projection(path,*args,**kwargs):
        if Path(path).name=='runner_state.json':raise OSError('synthetic disk full after cancellation control')
        return original_write(path,*args,**kwargs)
    monkeypatch.setattr(files,'atomic_write_json',fail_projection)
    with pytest.raises(OSError):runner.cancel(plan.run_id,execution_id=owner.execution_id)
    monkeypatch.setattr(files,'atomic_write_json',original_write)
    root=runner.repository.run_dir(plan.run_id)
    (root/'runner_state.json').unlink()
    (root/'packets'/plan.packets[0].packet_id/'state.json').unlink()
    assert runner.repository.read_progress(plan.run_id)['cancel_requested'] is True
    assert runner.resume(plan.run_id).status.value=='cancelled'
    assert not driver.sent


def test_progress_rebuild_does_not_ignore_corrupt_critical_ledger(tmp_path):
    from visibility_runner.errors import IntegrityError
    runner,plan,driver=fixture(tmp_path)
    runner.run(plan)
    root=runner.repository.run_dir(plan.run_id)
    (root/'runner_state.json').unlink()
    next((root/'recovery'/'events').glob('*.json')).write_text('{')
    from visibility_runner.recovery.api import RecoveryIntegrityError
    with pytest.raises((IntegrityError,RecoveryIntegrityError)):runner.inspect(plan.run_id)




def test_known_turn_one_inaccessible_chat_restarts_only_after_prior_explicit_check(tmp_path):
    import hashlib
    import asyncio
    from support.execution import FakeSubmission,_registry
    from visibility_runner.execution.run_measurement_group import MeasurementGroupWorker
    from visibility_runner.storage.send_attempts import SendAttemptStore
    from visibility_runner.websites.chat_actions import BrowserIdentity
    from visibility_runner.recovery.api import RecoveryLedger
    runner,plan,driver=fixture(tmp_path)
    runner.create(plan)
    item_id=plan.observation_ids[0]
    root=runner.repository.run_dir(plan.run_id)
    store=SendAttemptStore(root/'fences',watch_changes=False)
    identity=BrowserIdentity('profile_a','Default','context:profile_a','original-tab',conversation_reference='https://chatgpt.com/c/known-source')
    attempt=store.prepare(fence_id=item_id+':turn-1',observation_id=item_id,action='measurement_turn_1',
        prompt_sha256=hashlib.sha256(b'Recommend a clinic').hexdigest(),identity=identity.to_mapping())
    store.mark_prepared(attempt.fence_id)
    store.begin_send(attempt.fence_id)
    ref=runner.repository.write_evidence(plan.run_id,'Alpha Clinic',media_type='text/plain; charset=utf-8')
    store.capture_response(attempt.fence_id,response_reference=ref['path'],response_sha256=ref['sha256'])
    store.acknowledge(attempt.fence_id,validation_status='collected',identity=identity.to_mapping())
    original=store.path(attempt.fence_id).read_bytes()
    driver.answers[item_id]=(AnswerSpec('Alpha Clinic'),AnswerSpec('["Alpha Clinic"]'))
    first=runner.plan_refetch(plan.run_id,request_id='known-source-first-check')
    runner.refetch(first)
    assert not driver.sent
    first_readout=runner.failure_readout(plan.run_id)
    assert first_readout['items'][0]['external_outcome_unknown'] is False
    second=runner.plan_refetch(plan.run_id,request_id='known-source-new-pass')
    assert runner.refetch(second).status.value=='succeeded'
    assert len(driver.sent)==2
    assert store.path(attempt.fence_id).read_bytes()==original
    readout=runner.failure_readout(plan.run_id)
    assert readout['items'][0]['external_outcome_unknown'] is False
    events=RecoveryLedger(root).read()
    restarts=[event['payload'] for event in events if event['kind']=='visibility_known_unit_restart']
    assert len(restarts)==1 and restarts[0]['source_attempt_id']==attempt.fence_id
    assert restarts[0]['original_effect_outcome']=='succeeded'
    runner.refetch(second)
    assert len(driver.sent)==2


def test_projection_preserves_latest_durable_pass_cancellation(tmp_path):
    from visibility_runner.storage.progress_projection import project_progress
    plan={'run_id':'cancelled-pass','plan_sha256':'f'*64,'packets':[{'packet_id':'packet','observation_ids':['item'],'platform':'chatgpt'}]}
    events=({'kind':'pass_authorized','payload':{'pass_id':'pass-one','execution_id':'epoch'}},
            {'kind':'pass_cancelled','payload':{'pass_id':'pass-one'}})
    run,groups=project_progress(plan,(),(),events)
    assert run['cancel_requested'] and run['status']=='cancelled'
    assert groups['packet']['status']=='cancelled'


def test_readout_describes_a_run_whose_frozen_settings_differ_but_execution_stays_refused(tmp_path):
    from dataclasses import replace
    from visibility_runner.errors import StateTransitionError
    runner,plan,driver=fixture(tmp_path)
    driver.answers[plan.observation_ids[0]]=(AnswerSpec('Alpha Clinic'),AnswerSpec('["Alpha Clinic"]'))
    assert runner.run(plan).status.value=='succeeded'
    same=runner.failure_readout(plan.run_id)
    assert same['execution_policy']['frozen'] is True
    assert same['execution_policy']['matches_current_runtime'] is True
    assert same['execution_policy']['policy_sha256']
    changed=Runner(replace(runner.runtime,packet_start_stagger_seconds=runner.runtime.packet_start_stagger_seconds+1),providers={'chatgpt':driver})
    readout=changed.failure_readout(plan.run_id)
    assert readout['items'][0]['status']=='succeeded'
    assert readout['execution_policy']['matches_current_runtime'] is False
    assert readout['execution_policy']['mismatch']=='frozen_execution_policy_mismatch'
    assert readout['execution_policy']['policy_sha256']==same['execution_policy']['policy_sha256']
    with pytest.raises(StateTransitionError,match='frozen_execution_policy_mismatch'):
        changed.plan_refetch(plan.run_id,request_id='refused')
    # A finished run has nothing to resume, so resume returns without touching
    # the policy; run_packet on remaining work is refused (see the test above).
    assert changed.resume(plan.run_id).status.value=='succeeded'
    assert len(driver.sent)==2
