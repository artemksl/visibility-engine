"""Durable callback ordering and exact browser-fence correspondence."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import pytest
from support.conversation import Item,Page,SPEC,identity
from support.recovery import authorized
from visibility_runner.websites import chat_actions as chat
from visibility_runner.websites.conversation_binding import text_digest
from visibility_runner.recovery_adapters.visibility import VisibilityExecution
from visibility_runner.errors import IntegrityError
from visibility_runner.planning.hashing import json_value


def submission():
    page=Page();page.url='https://chatgpt.com/c/checkpoint-contract'
    result=chat.PageSubmission(page=page,profile_id='fixture',chrome_directory='Profile 99',observation_id='one',spec=SPEC,identity=identity(page))
    result._prompt='measurement';result._response_baseline=chat.ResponseBaseline(0,text_digest(''))
    return result


@pytest.mark.parametrize("prior_count", [0, 1])
def test_pending_answer_identity_is_bound_only_after_capture(prior_count):
    async def run():
        current = submission()
        prior_answers = [Item("Prior answer", "prior-answer")] if prior_count else []
        current.page.answers = prior_answers + [Item("Streaming answer", "intermediate-answer")]
        current._response_baseline = chat.ResponseBaseline(prior_count, text_digest("Prior answer" if prior_count else ""))
        current._history_before = {
            "assistant_ids": ["prior-answer"] if prior_count else [],
            "assistant_sha256": [text_digest("Prior answer")] if prior_count else [],
        }
        rows = []
        current.set_correlation_checkpoint(lambda value: rows.append(deepcopy(value)))
        await current._checkpoint_conversation()
        assert rows[-1]["transcript"]["assistant_ids"] == (["prior-answer"] if prior_count else [])
        current.page.answers[-1].identity = "final-answer"
        await current._checkpoint_conversation()
        assert rows[-1]["transcript"]["assistant_ids"] == (["prior-answer"] if prior_count else [])
        current._native_answer_captured = True
        await current._checkpoint_conversation()
        assert rows[-1]["transcript"]["assistant_ids"] == (["prior-answer"] if prior_count else []) + ["final-answer"]
        committed = deepcopy(rows)
        current.page.answers[-1].identity = "changed-captured-answer"
        with pytest.raises(chat.IdentityMismatchError):
            await current._checkpoint_conversation()
        assert rows == committed
    asyncio.run(run())


def test_pending_answer_does_not_relax_prior_answer_identity():
    async def run():
        current = submission()
        current._response_baseline = chat.ResponseBaseline(1, text_digest("Prior answer"))
        current._history_before = {"assistant_ids": ["prior-answer"], "assistant_sha256": [text_digest("Prior answer")]}
        current.page.answers = [Item("Prior answer", "prior-answer"), Item("Streaming answer", "intermediate-answer")]
        rows = []
        current.set_correlation_checkpoint(lambda value: rows.append(deepcopy(value)))
        await current._checkpoint_conversation()
        committed = deepcopy(rows)
        current.page.answers[0].identity = "changed-prior-answer"
        with pytest.raises(chat.IdentityMismatchError):
            await current._checkpoint_conversation()
        assert rows == committed
    asyncio.run(run())


def test_durable_url_precedes_stalled_history_and_enrichment_is_idempotent(tmp_path,monkeypatch):
    coordinator,owner,recovery_pass,_,_=authorized(tmp_path)
    async def run():
        entered,release=asyncio.Event(),asyncio.Event();saved_reader=chat._conversation_history
        async def stalled(*args,**kwargs):
            entered.set();await release.wait();return await saved_reader(*args,**kwargs)
        monkeypatch.setattr(chat,'_conversation_history',stalled)
        current=submission()
        current.set_correlation_checkpoint(lambda binding:coordinator.ledger.append('correlation',{'binding':binding},owner))
        task=asyncio.create_task(current._checkpoint_conversation())
        await entered.wait()
        rows=tuple(row for row in coordinator.ledger.read() if row['kind']=='correlation')
        assert len(rows)==1 and rows[0]['payload']['binding']['identity']['conversation_reference'].endswith('/checkpoint-contract')
        assert not task.done()
        early=json_value(rows[0])
        release.set();await task
        assert len(tuple(row for row in coordinator.ledger.read() if row['kind']=='correlation'))==2
        assert json_value(tuple(row for row in coordinator.ledger.read() if row['kind']=='correlation')[0])==early
        await current._checkpoint_conversation()
        assert len(tuple(row for row in coordinator.ledger.read() if row['kind']=='correlation'))==2
    try: asyncio.run(asyncio.wait_for(run(),5))
    finally: owner.release()


@pytest.mark.parametrize('asynchronous',[False,True])
@pytest.mark.parametrize('write_number',[1,2])
def test_sync_and_async_flush_errors_are_not_downgraded(asynchronous,write_number):
    async def run():
        current=submission();calls=[]
        def write(binding):
            calls.append(deepcopy(binding))
            if len(calls)==write_number: raise OSError('fixture full disk')
        async def async_write(binding): write(binding)
        current.set_correlation_checkpoint(async_write if asynchronous else write)
        with pytest.raises(OSError,match='fixture full disk'): await current._checkpoint_conversation()
        assert len(calls)==write_number
        if write_number==2:
            assert current._last_correlation_value['transcript']['user_ids']==[]
    asyncio.run(run())


@pytest.mark.parametrize('change',['url','message','branch'])
def test_known_correlation_change_never_overwrites_committed_binding(change):
    async def run():
        current=submission();rows=[]
        item=current.page.users[0]
        item.branch='original'
        read_attribute=item.get_attribute
        async def attribute(key):
            return item.branch if key=='data-branch-id' else await read_attribute(key)
        item.get_attribute=attribute
        current.set_correlation_checkpoint(lambda binding:rows.append(deepcopy(binding)))
        await current._checkpoint_conversation();before=deepcopy(rows)
        if change=='url': current.page.url='https://chatgpt.com/c/changed'
        elif change=='message': current.page.users[0].identity='changed'
        else:
            current.page.users[0].branch='changed'
        with pytest.raises(chat.IdentityMismatchError): await current._checkpoint_conversation()
        assert rows==before
    asyncio.run(run())


@pytest.mark.parametrize('field',['profile_id','chrome_directory','browser_context_reference','tab_reference','prompt_sha256'])
def test_visibility_checkpoint_rejects_fence_binding_mismatch(tmp_path,field):
    coordinator,owner,recovery_pass,_,_=authorized(tmp_path)
    try:
        context=VisibilityExecution.__new__(VisibilityExecution)
        context.owner=owner;context.pass_id=recovery_pass.pass_id;context.coordinator=coordinator
        saved=identity(Page()).to_mapping()
        fence=SimpleNamespace(identity=saved,prompt_sha256=text_digest('measurement'),observation_id='one',action='measurement_turn_1',fence_id='one:turn1')
        binding={'identity':dict(saved),'prompt_sha256':fence.prompt_sha256}
        if field=='prompt_sha256': binding[field]=text_digest('changed')
        else: binding['identity'][field]='wrong'
        before=coordinator.ledger.read()
        with pytest.raises(IntegrityError,match='conversation_checkpoint_binding_mismatch'): context.checkpoint(fence,binding)
        assert coordinator.ledger.read()==before
    finally: owner.release()


def test_late_checkpoint_cannot_write_under_replacement_owner(tmp_path):
    from visibility_runner.recovery.api import RunOwnership
    coordinator,owner,_,_,_=authorized(tmp_path)
    async def run():
        entered,release=asyncio.Event(),asyncio.Event()
        current=submission()
        async def write(binding):
            entered.set();await release.wait()
            coordinator.ledger.append('correlation',{'binding':binding},owner)
        current.set_correlation_checkpoint(write)
        task=asyncio.create_task(current._checkpoint_conversation())
        await entered.wait();owner.release()
        fresh=RunOwnership(tmp_path).acquire()
        try:
            before=coordinator.ledger.read();release.set()
            from visibility_runner.recovery.api import RecoveryBlocked
            with pytest.raises(RecoveryBlocked,match='execution_not_owned'):
                await task
            assert coordinator.ledger.read()==before
            assert current._last_correlation_payload is None
        finally: fresh.release()
    try: asyncio.run(asyncio.wait_for(run(),5))
    finally: owner.release()


def test_history_timeout_retains_committed_url_and_is_not_write_failure(tmp_path,monkeypatch):
    coordinator,owner,_,_,_=authorized(tmp_path)
    async def run():
        current=submission()
        current.set_correlation_checkpoint(lambda binding:coordinator.ledger.append('correlation',{'binding':binding},owner))
        async def unavailable(*args,**kwargs): raise TimeoutError('synthetic transcript delay')
        monkeypatch.setattr(chat,'_conversation_history',unavailable)
        await current._checkpoint_conversation()
        rows=[r for r in coordinator.ledger.read() if r['kind']=='correlation']
        assert len(rows)==1 and rows[0]['payload']['binding']['identity']['conversation_reference'].endswith('/checkpoint-contract')
        assert current._last_correlation_payload is not None
    try: asyncio.run(run())
    finally: owner.release()


def test_watch_latches_changed_url_and_wait_cannot_acknowledge():
    async def run():
        current=submission();rows=[]
        current.set_correlation_checkpoint(lambda binding:rows.append(deepcopy(binding)))
        await current._checkpoint_conversation();before=deepcopy(rows)
        current._submitted=True;current.page.url='https://chatgpt.com/c/changed'
        await current._watch_conversation()
        assert isinstance(current._correlation_error,chat.IdentityMismatchError)
        with pytest.raises(chat.IdentityMismatchError): await current.wait_response()
        assert rows==before
        await current.close()
    asyncio.run(asyncio.wait_for(run(),5))
