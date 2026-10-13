"""Mandatory correlation checkpoints cannot be downgraded to diagnostics."""
import asyncio
import time
from types import SimpleNamespace
import pytest
from visibility_runner.websites import chat_actions as chat
from support.conversation import Page, SPEC, identity
from visibility_runner.websites.conversation_binding import text_digest


class _CheckpointBrowserActions:
    """Dispatch only the synthetic control; this test isolates durable writes."""

    def __init__(self, submission):
        self.submission = submission
        self.targets = []

    async def retain(self, locator, deadline):
        assert time.monotonic() < deadline
        target = SimpleNamespace(locator=locator)
        self.targets.append(target)
        return target

    async def click(self, target, deadline, verify):
        assert time.monotonic() < deadline
        assert any(retained is target for retained in self.targets)
        assert not self.submission.cancelled()
        return await target.locator.click()

    async def release_all(self):
        self.targets.clear()


def test_failed_correlation_flush_prevents_answer_acceptance(monkeypatch):
    monkeypatch.setattr(chat, 'SharedBrowserActions', _CheckpointBrowserActions)
    async def exercise():
        page=Page()
        page.url='https://chatgpt.com/c/checkpoint-failure'
        submission=chat.PageSubmission(page=page,profile_id='fixture',chrome_directory='Profile 99',
            observation_id='obs',spec=SPEC,identity=identity(page))
        submission._prepared=True
        submission._composer=object()
        async def click(): pass
        submission._send_control=type('Control',(),{'click':staticmethod(click)})()
        submission._send_target=await submission._actions.retain(
            submission._send_control, time.monotonic()+1)
        submission._prompt='measurement'
        submission._response_baseline=chat.ResponseBaseline(0,text_digest(''))
        def failed_write(value): raise OSError('synthetic disk-full')
        submission.set_correlation_checkpoint(failed_write)
        await submission.submit_once()
        with pytest.raises(OSError,match='synthetic disk-full'):
            await submission.wait_response()
        assert submission._submitted  # Failed storage never means nothing sent.
        await submission.close()
    asyncio.run(exercise())


def test_collection_metadata_is_independent_immutable_and_timestamp_validated():
    from visibility_runner.models import ObservationResult
    from visibility_runner.errors import ContractError
    values={'captured_at':'2026-09-09T08:00:00Z','retrieved_at':'2026-09-14T09:00:00Z','recovery_pass_id':'pass-2'}
    row=ObservationResult('o','p','chatgpt','fixture','failed','failed',collection_metadata=values)
    values['captured_at']='changed'
    assert row.collection_metadata['captured_at']=='2026-09-09T08:00:00Z'
    assert row.capture_evidence=={}
    with pytest.raises(TypeError): row.collection_metadata['captured_at']='changed'
    for metadata in ({'captured_at':'2026-09-14'}, {'raw_exception':'unsafe'}, {'captured_at':None}):
        with pytest.raises(ContractError):
            ObservationResult('o','p','chatgpt','fixture','failed','failed',collection_metadata=metadata)
