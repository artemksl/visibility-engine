"""Event-barrier scheduler interleavings; elapsed time is only an outer watchdog."""
import asyncio
from types import SimpleNamespace
import pytest
from visibility_runner.execution import scheduler
from visibility_runner.execution.run_measurement_group import MeasurementGroupOutcome


def outcome(packet): return MeasurementGroupOutcome(packet['packet_id'],'succeeded',(),(),{})


def test_lane_waiter_does_not_take_global_slot():
    async def run():
        a_started,b_started,c_started,release_a= [asyncio.Event() for _ in range(4)]
        order=[]
        async def execute(p):
            name=p['packet_id'];order.append(name)
            if name=='A': a_started.set();await release_a.wait()
            if name=='B': b_started.set()
            if name=='C': c_started.set()
            return outcome(p)
        packets=[{'packet_id':x,'resource':key} for x,key in [('A','X'),('B','X'),('C','Y')]]
        task=asyncio.create_task(scheduler.schedule_packets(packets,execute,max_concurrency=2,resource_key=lambda p:p['resource']))
        await a_started.wait();await c_started.wait()
        assert not b_started.is_set()
        release_a.set(); results=await task
        assert order==['A','C','B'] and [r.packet_id for r in results]==['A','B','C']
    asyncio.run(asyncio.wait_for(run(),5))


@pytest.mark.parametrize('bad',['raise','unsupported'])
def test_worker_failure_settles_peer_and_preserves_input_order(bad):
    async def run():
        entered,release=asyncio.Event(),asyncio.Event()
        async def execute(p):
            if p['packet_id']=='bad':
                if bad=='raise': raise ValueError('fixture failure')
                return object()
            entered.set();await release.wait();return outcome(p)
        task=asyncio.create_task(scheduler.schedule_packets([{'packet_id':'bad'},{'packet_id':'peer'}],execute,max_concurrency=2,resource_key=lambda p:'shared-tab'))
        await entered.wait();assert not task.done();release.set()
        results=await task
        assert isinstance(results[0],scheduler.ScheduledFailure)
        assert results[0].packet_id=='bad' and results[1].status=='succeeded'
    asyncio.run(asyncio.wait_for(run(),5))


@pytest.mark.parametrize('stage',['resource_lock','semaphore'])
def test_cancelled_waiter_releases_only_its_own_locks(stage,monkeypatch):
    async def run():
        blocker_entered,waiter_blocked,release_blocker= [asyncio.Event() for _ in range(3)]
        tasks={};locks=[];events=[]
        class Lock(asyncio.Lock):
            def __init__(self): super().__init__();self.owner=None;locks.append(self)
            async def acquire(self):
                if asyncio.current_task() is tasks.get('waiter') and self.locked(): waiter_blocked.set()
                await super().acquire();self.owner=asyncio.current_task();events.append(('acquire',id(self),self.owner))
                return True
            def release(self):
                assert self.owner is asyncio.current_task(), 'released another packet lock'
                events.append(('release',id(self),self.owner));self.owner=None;super().release()
        class Semaphore(asyncio.Semaphore):
            async def acquire(self):
                if asyncio.current_task() is tasks.get('waiter') and self.locked(): waiter_blocked.set()
                return await super().acquire()
        proxy=SimpleNamespace(**{name:getattr(asyncio,name) for name in dir(asyncio) if not name.startswith('__')})
        proxy.Lock=Lock;proxy.Semaphore=Semaphore
        monkeypatch.setattr(scheduler,'asyncio',proxy)
        keys={'blocker':'X','waiter':'X','peer':'Y'}
        if stage=='semaphore': keys={'blocker':'A','waiter':'B','peer':'B'}
        def get_key(p): tasks[p['packet_id']]=asyncio.current_task();return keys[p['packet_id']]
        called=[]
        async def execute(p):
            called.append(p['packet_id'])
            if p['packet_id']=='blocker': blocker_entered.set();await release_blocker.wait()
            return outcome(p)
        packets=[{'packet_id':name} for name in keys]
        task=asyncio.create_task(scheduler.schedule_packets(packets,execute,max_concurrency=1,resource_key=get_key))
        await blocker_entered.wait();await waiter_blocked.wait();tasks['waiter'].cancel()
        # Yield through the actual cancelled waiter, not an arbitrary sleep.
        await asyncio.gather(tasks['waiter'],return_exceptions=True)
        release_blocker.set();result=await task
        assert called==['blocker','peer']
        assert isinstance(result[1],scheduler.ScheduledFailure)
        assert not any(lock.locked() for lock in locks)
        assert sum(e[0]=='acquire' for e in events)==sum(e[0]=='release' for e in events)
    asyncio.run(asyncio.wait_for(run(),5))


def test_cancellation_during_stagger_admits_no_staggered_provider_action(tmp_path,monkeypatch):
    from dataclasses import replace
    from support.execution import _runtime,_measurement,FakeDriver,AnswerSpec
    from support.clock import _ModuleProxy
    from visibility_runner.config import Platform
    from visibility_runner.execution import manage_measurements as manage
    runtime,_=_runtime(tmp_path)
    runtime=replace(runtime,tabs_per_provider=2,max_concurrency=2,packet_start_stagger_seconds=10)
    runner=manage.Runner(runtime,providers={})
    plan=runner.plan(_measurement(Platform.CHATGPT),run_id='stagger-cancel-contract')
    waiting=asyncio.Event();stagger_delays=[];opens=[]
    async def stagger(delay,result=None):
        stagger_delays.append(delay);waiting.set();await asyncio.Event().wait()
    monkeypatch.setattr(manage,'asyncio',_ModuleProxy(asyncio,sleep=stagger))
    driver=FakeDriver('chatgpt',{item:(AnswerSpec('Alpha Clinic'),AnswerSpec('["Alpha Clinic"]')) for item in plan.observation_ids})
    original_open=driver.open
    async def opened(**kwargs):
        opens.append(kwargs['observation_id'])
        await waiting.wait()
        runner.cancel(plan.run_id)
        return await original_open(**kwargs)
    driver.open=opened
    runner=manage.Runner(runtime,providers={'chatgpt':driver})
    result=runner.run(plan)
    assert result.status.value=='cancelled'
    assert stagger_delays==[10] and len(opens)==1 and not driver.sent


@pytest.mark.parametrize('completion',['available','expired'])
def test_deadline_is_checked_after_callback_completion(completion):
    from visibility_runner.websites.chat_actions import _await_call,_BrowserOperationDeadlineExceeded
    class Deadline:
        elapsed=False
        timeout_message='fixture exact boundary expired'
        def expired(self): return self.elapsed
        def remaining(self): return 5
        def raise_expired(self): raise _BrowserOperationDeadlineExceeded(self.timeout_message)
    async def run():
        deadline=Deadline();calls=[]
        async def callback():
            calls.append('completed');deadline.elapsed=completion=='expired';return 'response'
        if completion=='available': assert await _await_call(callback,_deadline=deadline)=='response'
        else:
            with pytest.raises(_BrowserOperationDeadlineExceeded): await _await_call(callback,_deadline=deadline)
        assert calls==['completed']
    asyncio.run(run())


def test_seven_profiles_two_slots_three_platforms_share_one_run_watcher():
    from collections import Counter
    from support.planning import config
    from visibility_runner.config import Platform, ProfileIdentity
    from visibility_runner.planning import build_run_plan
    from visibility_runner.planning.hashing import json_value
    from visibility_runner.execution.manage_measurements import _packet_tab_slots
    from visibility_runner.execution.run_measurement_group import MeasurementGroupWorker
    profiles=tuple(ProfileIdentity(f'fixture_{i}',f'Synthetic {i}',f'Synthetic Profile {i}') for i in range(7))
    selected=(Platform.CHATGPT,Platform.GEMINI,Platform.COPILOT)
    plan=build_run_plan(config(platforms=selected),run_id='forty-two-slots',selected_profile_ids=tuple(p.id for p in profiles),selected_profiles=profiles,profile_manifest_sha256='a'*64,tabs_per_provider=2)
    packets=[json_value(p) for p in plan.packets if p.kind.value=='measurement']
    assert len(packets)==42
    slots,_=_packet_tab_slots(packets)
    async def run():
        release,all_entered=asyncio.Event(),asyncio.Event()
        active=set();maximum=0;completed=[]
        worker=MeasurementGroupWorker(SimpleNamespace(cancellation_requested=lambda run_id:False),None)
        def key(p): return (p['profile_id'],p['platform'],slots[p['packet_id']])
        async def execute(p):
            nonlocal maximum
            resource=key(p);assert resource not in active;active.add(resource)
            maximum=max(maximum,len(active))
            assert max(Counter((profile,platform) for profile,platform,slot in active).values())<=2
            if len(active)==42: all_entered.set()
            try:
                await worker._await_with_cancellation('forty-two-slots',release.wait(),detail='synthetic release')
                completed.append(resource);return outcome(p)
            finally: active.remove(resource)
        task=asyncio.create_task(scheduler.schedule_packets(packets,execute,max_concurrency=42,resource_key=key))
        await all_entered.wait()
        assert len(worker._cancellation_watchers)==1
        assert set(Counter((profile,platform) for profile,platform,slot in active).values())=={2}
        assert len({profile for profile,platform,slot in active})==7
        assert {platform for profile,platform,slot in active}=={'chatgpt','gemini','copilot'}
        release.set();results=await task
        assert len(completed)==42 and maximum==42 and not active
        assert all(row.status=='succeeded' for row in results)
        assert not worker._cancellation_watchers
    asyncio.run(asyncio.wait_for(run(),5))
