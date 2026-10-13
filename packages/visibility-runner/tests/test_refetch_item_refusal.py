"""Synthetic regressions for item refusal and recovery packet isolation.

1. An observation's turn 2 was
   sent with no captured answer. A later pass checked it, then the replacement
   page failed to open (`preparation_failed`). In every pass after that,
   `begin_attempt(..., action="reconcile")` raised
   `RecoveryBlocked("dependency_incomplete")` before any attempt was recorded,
   the error ended the whole packet, and its text was replaced by
   "packet worker stopped with unresolved sent attempt(s)".
2. Pair profile_a:chatgpt was refused by the old-tab check with no record of
   the cause.

No test here uses a browser, a saved profile or a historical run file.
"""
from __future__ import annotations
import sys
from dataclasses import replace
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
sys.path.insert(0,str(ROOT/'tests'))
from support.execution import FakeDriver, AnswerSpec, _runtime, _typed_plan
from visibility_runner.config import Platform
from visibility_runner.execution.manage_measurements import Runner
from visibility_runner.recovery.api import RecoveryLedger


def _events(runner, run_id, kind):
    journal=RecoveryLedger(runner.repository.run_dir(run_id),watch_changes=False)
    try:
        return [e for e in journal.read() if e['kind']==kind]
    finally:
        journal.close()


def _uncertain_second_turn(tmp_path):
    runtime,digest=_runtime(tmp_path)
    runtime=replace(runtime,profiles=replace(runtime.profiles,lease_lock_root=tmp_path/'leases'))
    plan=_typed_plan(Platform.CHATGPT,run_id='refetch-item-refusal',manifest_sha256=digest)
    item_id=plan.observation_ids[0]
    # Turn 1 is answered. Turn 2 is sent and its answer is lost.
    driver=FakeDriver('chatgpt',{item_id:(AnswerSpec('Alpha Clinic'),)})
    runner=Runner(runtime,providers={'chatgpt':driver})
    runner.run(plan)
    assert len(driver.sent)==2
    return runner,plan,driver,item_id


def test_failed_turn_one_replacement_does_not_block_the_next_pass(tmp_path):
    runner,plan,driver,item_id=_uncertain_second_turn(tmp_path)
    check=runner.plan_refetch(plan.run_id,request_id='check')
    assert check['selections'][0]['action']=='reconcile'
    runner.refetch(check)
    assert len(driver.sent)==2

    # Pass 2 may replace, but the page opening of the replacement fails.
    # (This fixture's frozen retry policy permits one entry per pass.)
    replace=runner.plan_refetch(plan.run_id,request_id='replace-1')
    assert replace['selections'][0]['action']=='reconcile_or_replace'
    driver.open_failures[item_id]=1
    runner.refetch(replace)
    assert driver.open_failures[item_id]==0
    assert len(driver.sent)==2
    finished=[e['payload'] for e in _events(runner,plan.run_id,'attempt_finished')]
    assert [p['reason_code'] for p in finished if p['operation_id'].endswith(':measurement_turn_1')][-1]=='preparation_failed'

    # Pass 3 must check the old send again and then replace it.
    again=runner.plan_refetch(plan.run_id,request_id='replace-2')
    assert again['selections'][0]['action']=='reconcile_or_replace'
    started_before=len(_events(runner,plan.run_id,'attempt_started'))
    driver.answers[item_id]=(AnswerSpec('Alpha Clinic'),AnswerSpec('["Alpha Clinic"]'))
    result=runner.refetch(again)
    assert len(_events(runner,plan.run_id,'attempt_started'))>started_before
    assert result.status.value=='succeeded'
    assert len(driver.sent)==4


def test_refused_item_does_not_end_the_packet_and_keeps_its_code(tmp_path,monkeypatch):
    """Any RecoveryBlocked from prepare_item is recorded for that item only."""
    from visibility_runner.recovery.api import RecoveryBlocked
    from visibility_runner.recovery_adapters import visibility
    runner,plan,driver,item_id=_uncertain_second_turn(tmp_path)
    check=runner.plan_refetch(plan.run_id,request_id='check')

    async def refuse(self,worker,item,observation,packet):
        raise RecoveryBlocked('dependency_incomplete')
    monkeypatch.setattr(visibility.VisibilityExecution,'prepare_item',refuse)
    runner.refetch(check)
    reasons=[e['payload']['reason_code'] for e in _events(runner,plan.run_id,'failure')
             if e['payload']['item_id']==item_id]
    assert reasons[-1]=='dependency_incomplete'
    packet_id=plan.packets[0].packet_id
    state=runner.repository.read_group_progress(plan.run_id,packet_id)
    codes=[issue.get('code') for issue in state.get('issues') or ()]
    assert 'recovery_refused' in codes
    assert not any('packet worker stopped' in str(issue.get('message')) for issue in state.get('issues') or ())


def test_packet_error_under_refetch_keeps_its_real_cause(tmp_path,monkeypatch):
    """An earlier pass's unresolved send must not relabel an unrelated packet error."""
    from visibility_runner.recovery_adapters import visibility
    runner,plan,driver,item_id=_uncertain_second_turn(tmp_path)
    check=runner.plan_refetch(plan.run_id,request_id='check')

    async def broken(self,worker,item,observation,packet):
        raise RuntimeError('injected packet error')
    monkeypatch.setattr(visibility.VisibilityExecution,'prepare_item',broken)
    runner.refetch(check)
    state=runner.repository.read_group_progress(plan.run_id,plan.packets[0].packet_id)
    issue=[row for row in state.get('issues') or () if row.get('packet_id')==plan.packets[0].packet_id][-1]
    assert issue['code']=='packet_worker_failed'
    assert issue['message']=='RuntimeError: injected packet error'
    assert issue['detail']['failure_context']['exception_type']=='RuntimeError'


def test_false_recovery_result_keeps_recorded_item_reason_without_sending(tmp_path,monkeypatch):
    from visibility_runner.recovery_adapters import visibility
    runner,plan,driver,item_id=_uncertain_second_turn(tmp_path)
    check=runner.plan_refetch(plan.run_id,request_id='check')

    async def refuse(self,worker,item,observation,packet):
        self.failure(item,'copilot_verification_required')
        return False
    monkeypatch.setattr(visibility.VisibilityExecution,'prepare_item',refuse)
    runner.refetch(check)
    state=runner.repository.read_group_progress(plan.run_id,plan.packets[0].packet_id)
    codes=[issue.get('code') for issue in state.get('issues') or ()]
    assert 'copilot_verification_required' in codes
    assert 'original_response_unresolved' not in codes
    assert len(driver.sent)==2


def test_refusal_after_an_attempt_started_closes_that_attempt(tmp_path,monkeypatch):
    from visibility_runner.recovery.api import AllowanceExceeded
    from visibility_runner.recovery_adapters import visibility
    runner,plan,driver,item_id=_uncertain_second_turn(tmp_path)
    check=runner.plan_refetch(plan.run_id,request_id='check')

    async def refuse_late(self,worker,item,observation,packet):
        self.coordinator.begin_attempt(self.pass_id,f'{item}:same_chat_turn_2',self.owner,
            action='reconcile',predecessor_ids=(f'{item}:turn-2',))
        raise AllowanceExceeded('effect_allowance_exhausted')
    monkeypatch.setattr(visibility.VisibilityExecution,'prepare_item',refuse_late)
    runner.refetch(check)
    # The initial pass keeps its unfinished turn-2 attempt: that send is uncertain.
    pass_id=_events(runner,plan.run_id,'pass_authorized')[-1]['payload']['pass_id']
    started={e['payload']['attempt_id'] for e in _events(runner,plan.run_id,'attempt_started')
             if e['payload']['pass_id']==pass_id}
    finished={e['payload']['attempt_id']:e['payload'] for e in _events(runner,plan.run_id,'attempt_finished')
              if e['payload']['pass_id']==pass_id}
    assert len(started)==1 and started==set(finished)
    assert [row['reason_code'] for row in finished.values()][-1]=='effect_allowance_exhausted'
    reasons=[e['payload']['reason_code'] for e in _events(runner,plan.run_id,'failure') if e['payload']['item_id']==item_id]
    assert reasons[-1]=='effect_allowance_exhausted'


def test_blocked_pair_with_only_released_tabs_records_its_cause():
    """A synthetic profile pair was refused with no recorded cause."""
    import asyncio
    from types import SimpleNamespace
    from visibility_runner.recovery_adapters.visibility import _contain_resources
    from visibility_runner.websites.chat_actions import BrowserIdentity
    identity=BrowserIdentity('profile_a','Default','context:a','old:a').to_mapping()
    committed={'kind':'resource_observed','payload':{'resource_id':'tab:old:a','status':'released',
        'reference':{'identity':identity,'platform':'chatgpt','item_id':'item','resource_scopes':('profile_a:chatgpt',)}}}
    history=[]
    class MarkerMismatch(RuntimeError):
        pass
    def acquire(*args,**kwargs):
        raise MarkerMismatch('text that must not be stored: /private/tmp/some/path')
    context=SimpleNamespace(coordinator=SimpleNamespace(ledger=SimpleNamespace(read=lambda:(committed,),
        append=lambda kind,payload,owner:history.append((kind,payload)))),owner=SimpleNamespace(execution_id='owned'),
        pass_id='finite',assert_owned=lambda:None,blocked_scopes=set(),
        record_resource=lambda *args,**kwargs:history.append(('resource',kwargs)),
        snapshot=SimpleNamespace(manifest=SimpleNamespace(run_id='synthetic',operations=())),
        verified=SimpleNamespace(attempt_records=(),plan={'observation_plan':{'observations':[{'observation_id':'item','platform':'chatgpt'}]}}))
    attachment=SimpleNamespace(lease_manager=SimpleNamespace(acquire_containment=acquire,resolve_containment=None,
        containment_requirements=lambda *a,**k:()),
        manifest=SimpleNamespace(profile=lambda value:value),provider_page_factory=None)
    asyncio.run(_contain_resources(context,attachment))
    assert context.blocked_scopes=={'profile_a:chatgpt'}
    assert history==[('resource_containment_blocked',{'pass_id':'finite','resource_scope':'profile_a:chatgpt',
        'resource_references':('tab:old:a',),
        'failure_context':{'step':'acquire_or_resolve_pair_lock','exception_type':'MarkerMismatch'}})]
    assert 'private' not in repr(history)
