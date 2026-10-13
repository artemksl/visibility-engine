from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import time
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from visibility_runner.websites import chat_actions as provider_base
from visibility_runner.websites.chat_actions import (
    PageProviderSpec,
    VisibleAnswerSnapshot,
    last_visible_text,
    visible_answer_snapshot,
    wait_for_stable_answer,
)
from visibility_runner.websites.chatgpt import CHATGPT_SPEC
from visibility_runner.websites.gemini import GEMINI_SPEC


def test_gemini_dom_label_removal_preserves_identical_model_prefix() -> None:
    label_selector = "h2.cdk-visually-hidden"

    class Locator:
        def __init__(self, nodes):
            self.nodes = list(nodes)

        async def count(self):
            return len(self.nodes)

        def nth(self, index):
            return self.nodes[index]

    class Node:
        def __init__(self, text, descendants=None):
            self.text = text
            self.descendants = descendants or {}

        async def is_visible(self):
            return True

        async def inner_text(self):
            return self.text

        async def text_content(self):
            return self.text

        def locator(self, selector):
            return Locator(self.descendants.get(selector, ()))

    accessibility_label = Node("Gemini said")
    response = Node(
        "Gemini said\n\nGemini said this exact phrase is model content.",
        {label_selector: (accessibility_label,)},
    )

    class Page:
        def locator(self, selector):
            return Locator((response,) if selector == "answer" else ())

    assert GEMINI_SPEC.rendered_answer_label_selectors == (label_selector,)
    captured = asyncio.run(
        last_visible_text(
            Page(),
            ("answer",),
            label_selectors=GEMINI_SPEC.rendered_answer_label_selectors,
        )
    )

    assert captured == "Gemini said this exact phrase is model content."


def test_gemini_phrase_without_dom_label_is_byte_lossless() -> None:
    expected = "Gemini said\n\nthis is legitimate model content."

    class Node:
        async def is_visible(self):
            return True

        async def inner_text(self):
            return expected

        def locator(self, _selector):
            return Locator(())

    class Locator:
        def __init__(self, nodes):
            self.nodes = tuple(nodes)

        async def count(self):
            return len(self.nodes)

        def nth(self, index):
            return self.nodes[index]

    class Page:
        def locator(self, selector):
            return Locator((Node(),) if selector == "answer" else ())

    captured = asyncio.run(
        last_visible_text(
            Page(),
            ("answer",),
            label_selectors=GEMINI_SPEC.rendered_answer_label_selectors,
        )
    )

    assert captured == expected


def test_last_visible_text_keeps_an_empty_newest_response_node() -> None:
    class Node:
        def __init__(self, text):
            self.text = text

        async def is_visible(self):
            return True

        async def inner_text(self):
            return self.text

    class Locator:
        def __init__(self, nodes):
            self.nodes = tuple(nodes)

        async def count(self):
            return len(self.nodes)

        def nth(self, index):
            return self.nodes[index]

    class Page:
        def locator(self, selector):
            assert selector == "answer"
            return Locator((Node("turn-one response"), Node("")))

    captured = asyncio.run(last_visible_text(Page(), ("answer",)))

    assert captured == ""


def test_visible_answer_snapshot_uses_one_unique_selector_union() -> None:
    locator_calls = []

    class Node:
        async def is_visible(self):
            return True

        async def inner_text(self):
            return "one response matched by both selectors"

    class Locator:
        async def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return Node()

    class Page:
        def locator(self, selector):
            locator_calls.append(selector)
            assert selector == "answer, .answer"
            return Locator()

    snapshot = asyncio.run(
        visible_answer_snapshot(Page(), ("answer", ".answer"))
    )

    assert snapshot == VisibleAnswerSnapshot(
        count=1,
        text="one response matched by both selectors",
    )
    assert locator_calls == ["answer, .answer"]


def test_hidden_new_node_cannot_authorize_old_visible_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from visibility_runner.websites import chat_actions as provider_base

    states = (
        (False, "", False),
        (False, "", False),
        (False, "", True),
        (True, "", True),
        (True, "turn-two response", True),
        (True, "turn-two response", False),
        (True, "turn-two response", False),
    )
    state_index = 0

    class Node:
        def __init__(self, text, visible):
            self.text = text
            self.visible = visible

        async def is_visible(self):
            return self.visible

        async def inner_text(self):
            return self.text

    class Locator:
        def __init__(self, nodes):
            self.nodes = tuple(nodes)

        async def count(self):
            return len(self.nodes)

        def nth(self, index):
            return self.nodes[index]

    class Body:
        async def inner_text(self):
            return ""

    class Page:
        def locator(self, selector):
            if selector == "body":
                return Body()
            assert selector in {"answer", ".answer", "answer, .answer"}
            new_visible, new_text, _busy = states[state_index]
            return Locator(
                (
                    Node("turn-one response", True),
                    Node(new_text, new_visible),
                )
            )

    async def is_busy(*_args, **_kwargs):
        return states[state_index][2]

    async def advance_poll(_delay):
        nonlocal state_index
        state_index = min(state_index + 1, len(states) - 1)

    monkeypatch.setattr(provider_base, "any_visible", is_busy)
    monkeypatch.setattr(provider_base.asyncio, "sleep", advance_poll)
    spec = PageProviderSpec(
        platform="gemini",
        start_url="https://gemini.google.com/app",
        composer_selectors=("composer",),
        send_selectors=("send",),
        answer_selectors=("answer", ".answer"),
        stop_selectors=("stop",),
        response_timeout_seconds=1.0,
        stable_seconds=0.0,
    )

    captured = asyncio.run(
        wait_for_stable_answer(
            Page(),
            spec,
            before_count=1,
            before_answer="turn-one response",
        )
    )

    assert captured == "turn-two response"
    assert state_index == len(states) - 1


def test_stable_answer_waits_for_post_send_mutation_before_turn_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from visibility_runner.websites import chat_actions as provider_base

    states = (
        (1, "turn-one response", False),
        (1, "turn-one response", False),
        (2, "", False),
        (2, "", True),
        (2, "turn-two response", True),
        (2, "turn-two response", False),
        (2, "turn-two response", False),
    )
    state_index = 0

    class Body:
        async def inner_text(self):
            return ""

    class Page:
        def locator(self, selector):
            assert selector == "body"
            return Body()

    async def answer_snapshot(*_args, **_kwargs):
        count, text, _busy = states[state_index]
        return VisibleAnswerSnapshot(count=count, text=text)

    async def is_busy(*_args, **_kwargs):
        return states[state_index][2]

    async def advance_poll(_delay):
        nonlocal state_index
        state_index = min(state_index + 1, len(states) - 1)

    monkeypatch.setattr(
        provider_base,
        "visible_answer_snapshot",
        answer_snapshot,
    )
    monkeypatch.setattr(provider_base, "any_visible", is_busy)
    monkeypatch.setattr(provider_base.asyncio, "sleep", advance_poll)
    spec = PageProviderSpec(
        platform="gemini",
        start_url="https://gemini.google.com/app",
        composer_selectors=("composer",),
        send_selectors=("send",),
        answer_selectors=("answer",),
        stop_selectors=("stop",),
        response_timeout_seconds=1.0,
        stable_seconds=0.0,
    )

    captured = asyncio.run(
        wait_for_stable_answer(
            Page(),
            spec,
            before_count=1,
            before_answer="turn-one response",
        )
    )

    assert captured == "turn-two response"
    assert state_index == len(states) - 1


def test_response_deadline_bounds_never_returning_scoped_wrapper_count() -> None:
    response_timeout = 0.02
    external_watchdog = 0.15

    class Responses:
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
            raise AssertionError("the never-returning CDP read resumed")

    class Page:
        def __init__(self) -> None:
            self.responses = Responses()
            self.submit_calls = 0
            self.retry_calls = 0

        def locator(self, selector: str) -> Responses:
            assert selector == "answer"
            return self.responses

        async def submit_once(self) -> None:
            self.submit_calls += 1

        async def prepare_native_retry(self) -> bool:
            self.retry_calls += 1
            return False

    page = Page()
    spec = PageProviderSpec(
        platform="chatgpt",
        start_url="https://chatgpt.com",
        composer_selectors=("composer",),
        send_selectors=("send",),
        answer_selectors=("answer",),
        response_timeout_seconds=response_timeout,
        stable_seconds=0.0,
    )

    async def exercise() -> tuple[float, bool, BaseException]:
        started = time.monotonic()
        response_wait = asyncio.create_task(
            wait_for_stable_answer(
                page,
                spec,
                before_count=0,
                before_answer="",
            )
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(response_wait),
                timeout=external_watchdog,
            )
        except TimeoutError as exc:
            elapsed = time.monotonic() - started
            watchdog_fired = not response_wait.done()
            if watchdog_fired:
                response_wait.cancel()
                try:
                    await response_wait
                except asyncio.CancelledError:
                    pass
            return elapsed, watchdog_fired, exc
        raise AssertionError("response wait unexpectedly completed")

    elapsed, watchdog_fired, error = asyncio.run(exercise())

    assert watchdog_fired is False, (
        f"external watchdog fired after {elapsed:.3f}s"
    )
    assert str(error) == "chatgpt response did not settle"
    assert response_timeout / 2 <= elapsed < external_watchdog / 2
    assert page.responses.read_calls == 1
    assert page.responses.cancelled is True
    assert page.submit_calls == 0
    assert page.retry_calls == 0


def test_response_deadline_rejects_late_result_after_delayed_cancellation() -> None:
    response_timeout = 0.02
    external_watchdog = 0.15

    class Responses:
        def __init__(self) -> None:
            self.cancelled = False
            self.returned_after_cancellation = False
            self.finished = asyncio.Event()

        async def count(self) -> int:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                await asyncio.sleep(0.04)
                self.returned_after_cancellation = True
                self.finished.set()
                return 1

    class Page:
        def __init__(self) -> None:
            self.responses = Responses()

        def locator(self, selector: str) -> Responses:
            assert selector == "answer"
            return self.responses

    page = Page()
    spec = PageProviderSpec(
        platform="chatgpt",
        start_url="https://chatgpt.com",
        composer_selectors=("composer",),
        send_selectors=("send",),
        answer_selectors=("answer",),
        response_timeout_seconds=response_timeout,
        stable_seconds=0.0,
    )

    async def exercise() -> tuple[float, BaseException]:
        started = time.monotonic()
        response_wait = asyncio.create_task(
            wait_for_stable_answer(
                page,
                spec,
                before_count=0,
                before_answer="",
            )
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(response_wait),
                timeout=external_watchdog,
            )
        except TimeoutError as exc:
            elapsed = time.monotonic() - started
            assert response_wait.done()
            await asyncio.wait_for(
                page.responses.finished.wait(),
                timeout=external_watchdog,
            )
            return elapsed, exc
        raise AssertionError("response wait unexpectedly completed")

    elapsed, error = asyncio.run(exercise())

    assert str(error) == "chatgpt response did not settle"
    assert response_timeout / 2 <= elapsed < external_watchdog / 2
    assert page.responses.cancelled is True
    assert page.responses.returned_after_cancellation is True


def test_response_browser_await_preserves_caller_cancellation() -> None:
    class Responses:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = False

        async def count(self) -> int:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    class Page:
        def __init__(self) -> None:
            self.responses = Responses()

        def locator(self, selector: str) -> Responses:
            assert selector == "answer"
            return self.responses

    page = Page()
    spec = PageProviderSpec(
        platform="chatgpt",
        start_url="https://chatgpt.com",
        composer_selectors=("composer",),
        send_selectors=("send",),
        answer_selectors=("answer",),
        response_timeout_seconds=10.0,
        stable_seconds=0.0,
    )

    async def exercise() -> None:
        response_wait = asyncio.create_task(
            wait_for_stable_answer(
                page,
                spec,
                before_count=0,
                before_answer="",
            )
        )
        await page.responses.started.wait()
        response_wait.cancel()
        with pytest.raises(asyncio.CancelledError):
            await response_wait
        await asyncio.sleep(0)

    asyncio.run(exercise())

    assert page.responses.cancelled is True


@pytest.mark.parametrize(
    "hung_operation",
    (
        "answer_count",
        "answer_visibility",
        "answer_text",
        "label_count",
        "label_text",
        "busy_count",
        "busy_visibility",
        "terminal_count",
        "terminal_visibility",
        "terminal_text",
    ),
)
def test_response_deadline_bounds_every_dom_read(
    hung_operation: str,
) -> None:
    response_timeout = 0.02
    external_watchdog = 0.15
    cancelled: list[str] = []

    async def browser_call(name: str, value):
        if name != hung_operation:
            return value
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(name)
            raise
        raise AssertionError("the never-returning DOM read resumed")

    class Locator:
        def __init__(self, name: str, nodes: tuple[object, ...]):
            self.name = name
            self.nodes = nodes

        async def count(self):
            return await browser_call(
                f"{self.name}_count",
                len(self.nodes),
            )

        def nth(self, index: int):
            return self.nodes[index]

    class Node:
        def __init__(
            self,
            name: str,
            text: str,
            descendants: dict[str, tuple[object, ...]] | None = None,
        ):
            self.name = name
            self.text = text
            self.descendants = descendants or {}

        async def is_visible(self):
            return await browser_call(
                f"{self.name}_visibility",
                True,
            )

        async def inner_text(self):
            return await browser_call(
                f"{self.name}_text",
                self.text,
            )

        async def text_content(self):
            return await self.inner_text()

        def locator(self, selector: str):
            name = "label" if selector == "label" else "terminal"
            return Locator(name, self.descendants.get(selector, ()))

    class Body:
        async def inner_text(self):
            return ""

    label = Node("label", "Assistant said")
    terminal = Node("terminal", "")
    answer = Node(
        "answer",
        "Assistant said\n\npartial answer",
        {
            "label": (label,),
            "terminal": (terminal,),
        },
    )
    busy = Node("busy", "")

    class Page:
        def locator(self, selector: str):
            if selector == "body":
                return Body()
            if selector == "answer":
                return Locator("answer", (answer,))
            if selector == "stop":
                return Locator("busy", (busy,))
            raise AssertionError(f"unexpected selector: {selector}")

    spec = PageProviderSpec(
        platform="chatgpt",
        start_url="https://chatgpt.com",
        composer_selectors=("composer",),
        send_selectors=("send",),
        answer_selectors=("answer",),
        response_node_selectors=("answer",),
        stop_selectors=("stop",),
        terminal_error_within_response_selectors=("terminal",),
        terminal_error_markers=("something went wrong",),
        rendered_answer_label_selectors=("label",),
        response_timeout_seconds=response_timeout,
        stable_seconds=0.0,
    )

    async def exercise() -> tuple[float, bool, BaseException]:
        started = time.monotonic()
        response_wait = asyncio.create_task(
            wait_for_stable_answer(
                Page(),
                spec,
                before_count=0,
                before_answer="",
            )
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(response_wait),
                timeout=external_watchdog,
            )
        except TimeoutError as exc:
            elapsed = time.monotonic() - started
            watchdog_fired = not response_wait.done()
            if watchdog_fired:
                response_wait.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await response_wait
            return elapsed, watchdog_fired, exc
        raise AssertionError("response wait unexpectedly completed")

    elapsed, watchdog_fired, error = asyncio.run(exercise())

    assert watchdog_fired is False
    assert str(error) == "chatgpt response did not settle"
    assert response_timeout / 2 <= elapsed < external_watchdog / 2
    assert cancelled == [hung_operation]


def test_provider_specs_keep_generation_errors_out_of_access_markers() -> None:
    # ChatGPT briefly exposes a visible fallback textarea while the app
    # hydrates, then hides it. Selecting a bare textarea can strand a
    # fresh-chat retry on a locator that becomes non-actionable before exact
    # write/readback completes.
    assert "textarea" not in CHATGPT_SPEC.composer_selectors
    assert CHATGPT_SPEC.user_message_selectors
    assert CHATGPT_SPEC.terminal_error_within_response_selectors
    assert CHATGPT_SPEC.native_retry_within_response_selectors
    assert GEMINI_SPEC.user_message_selectors
    assert GEMINI_SPEC.terminal_error_within_response_selectors
    assert GEMINI_SPEC.native_retry_within_response_selectors
    assert "something went wrong" not in GEMINI_SPEC.blocked_markers
