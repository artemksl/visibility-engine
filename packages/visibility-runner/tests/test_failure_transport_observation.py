"""Passive provider metadata stays bounded and cannot leak private requests."""
import asyncio
from types import SimpleNamespace

import pytest

from visibility_runner.execution.run_measurement_group import MeasurementGroupWorker
from visibility_runner.websites.chat_actions import (
    BrowserIdentity, PageSubmission, ResponseBaseline, _FailureTransportObservation,
)
from visibility_runner.websites.chatgpt import CHATGPT_SPEC


class EventPage:
    def __init__(self, *, fail_event=None):
        self.main_frame = SimpleNamespace(url="https://chatgpt.com/c/private-chat?secret=value")
        self.listeners = {}
        self.fail_event = fail_event

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)
        if event == self.fail_event:
            raise RuntimeError("private-registration-error")

    def remove_listener(self, event, callback):
        self.listeners[event].remove(callback)

    def emit(self, event, value=None):
        for callback in tuple(self.listeners.get(event, ())):
            callback(value)


class Request:
    def __init__(self, page, *, url="https://chatgpt.com/backend-api/conversation?secret=value", frame=None, resource="fetch", method="POST", failure="net::ERR_CONNECTION_RESET"):
        self.frame = page.main_frame if frame is None else frame
        self.url, self.resource_type, self.method, self.failure = url, resource, method, failure

    @property
    def headers(self):
        raise AssertionError("Headers must never be read")

    @property
    def post_data(self):
        raise AssertionError("Bodies must never be read")


def test_provider_stream_metadata_is_filtered_and_sanitized():
    page = EventPage()
    observer = _FailureTransportObservation(page, "https://chatgpt.com/")
    observer.start()
    for request in (
        Request(page, frame=object()),
        Request(page, url="https://foreign.invalid/backend-api/conversation?private=value"),
        Request(page, resource="document"),
    ):
        page.emit("request", request)
        page.emit("requestfailed", request)
    assert observer.snapshot()["events"] == []
    request = Request(page)
    page.emit("request", request)
    page.emit("response", SimpleNamespace(request=request, status=200))
    page.emit("requestfailed", request)
    events = observer.snapshot()["events"]
    assert [event["event"] for event in events] == ["request", "response", "requestfailed"]
    assert {event["request_ordinal"] for event in events} == {1}
    assert events[1]["status"] == 200
    assert events[2]["error"] == "net::ERR_CONNECTION_RESET"
    assert all(event["endpoint"] == "conversation" for event in events)
    page.emit("requestfailed", Request(page, url="https://chatgpt.com/private-path?secret=value", method="private-method", failure="private-error https://example.invalid/?token=value"))
    last = observer.snapshot()["events"][-1]
    assert last["method"] == "OTHER" and last["error"] == "unavailable"
    assert last["endpoint"] == "provider_fetch"
    assert "private" not in str(observer.snapshot()) and "secret" not in str(observer.snapshot())
    observer.finish()


def test_events_and_pending_requests_are_bounded_and_cleanup_freezes_snapshot():
    page = EventPage()
    observer = _FailureTransportObservation(page, "https://chatgpt.com/")
    observer.start()
    for _ in range(150):
        page.emit("request", Request(page))
    assert len(observer._requests) == 64
    assert len(observer.snapshot()["events"]) == 64
    assert observer.snapshot()["events_seen"] == 150
    page.emit("framenavigated", object())
    page.emit("framenavigated", page.main_frame)
    page.emit("crash")
    page.emit("close")
    assert [event["event"] for event in observer.snapshot()["events"][-3:]] == ["navigation", "crash", "close"]
    saved = observer.snapshot()
    observer.finish()
    observer.finish()
    assert not observer._requests and not any(page.listeners.values())
    page.emit("requestfailed", Request(page))
    assert observer.snapshot() == saved


def test_partial_registration_and_callback_errors_never_escape():
    page = EventPage(fail_event="requestfailed")
    observer = _FailureTransportObservation(page, "https://chatgpt.com/")
    observer.start()
    assert observer.available is False
    assert not any(page.listeners.values())
    page.fail_event = None
    observer.start()
    page.emit("request", object())
    page.emit("response", object())
    page.emit("requestfailed", object())
    page.emit("framenavigated", object())
    assert observer.snapshot()["events"] == []
    observer.finish()
    unavailable = _FailureTransportObservation(object(), "https://chatgpt.com/")
    unavailable.start()
    assert unavailable.snapshot() == {"available": False, "events_seen": 0, "events": []}


def test_observation_is_attached_before_native_dispatch_and_reset_between_turns():
    page = EventPage()
    submission = PageSubmission(page=page, profile_id="synthetic", chrome_directory="Synthetic",
        observation_id="synthetic", spec=CHATGPT_SPEC,
        identity=BrowserIdentity("synthetic", "Synthetic", "context", "tab"))
    submission._prepared = True
    submission._composer = object()
    submission._send_control = SimpleNamespace(click=lambda: None)
    submission._send_target = object()
    submission._response_baseline = ResponseBaseline(0, "0" * 64)
    calls = []

    async def click(*args):
        assert sum(len(value) for value in page.listeners.values()) == 7
        calls.append(args)
        page.emit("requestfailed", Request(page))

    submission._actions = SimpleNamespace(click=click)
    asyncio.run(submission.submit_once())
    assert len(calls) == 1
    assert submission.failure_transport_snapshot()["events"][0]["event"] == "requestfailed"
    submission.next_turn()
    assert not any(page.listeners.values())
    assert submission.failure_transport_snapshot() is None


@pytest.mark.parametrize("outcome", ("success", "failure", "cancelled"))
def test_send_finally_removes_listeners_without_changing_outcome(outcome):
    page = EventPage()
    observer = _FailureTransportObservation(page, "https://chatgpt.com/")
    observer.start()
    worker = object.__new__(MeasurementGroupWorker)
    error = asyncio.CancelledError() if outcome == "cancelled" else RuntimeError("original-failure")

    async def send(*args, **kwargs):
        if outcome != "success":
            raise error
        return "original-result"

    worker._send_observed = send
    submission = SimpleNamespace(finish_failure_observation=observer.finish)
    call = worker._send("run", None, None, submission, "private-prompt", packet_id="packet", policy=None)
    if outcome == "success":
        assert asyncio.run(call) == "original-result"
    else:
        with pytest.raises(type(error)) as caught:
            asyncio.run(call)
        assert caught.value is error
    assert not any(page.listeners.values())
