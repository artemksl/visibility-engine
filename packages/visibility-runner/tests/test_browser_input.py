from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import time
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from visibility_runner.browser.input import (
    ComposerFailureKind,
    ComposerReadbackMismatch,
    ComposerSendAlreadyStarted,
    ExactComposerInputFailure,
    clear_composer_with_keyboard,
    type_composer_value,
    write_exact_composer_input,
)
from support.browser_actions import SyntheticBrowserActions
from visibility_runner.browser import input as browser_input
from visibility_runner.errors import (
    BrowserInteractionError,
    IdentityMismatchError,
)
from visibility_runner.websites import chat_actions as provider_base
from visibility_runner.websites.chat_actions import (
    BrowserIdentity,
    PageProviderSpec,
    PageSubmission,
    interaction_pause,
)
from visibility_runner.websites.chatgpt import ChatGPTProvider
from visibility_runner.websites.gemini import GeminiProvider
from visibility_runner.websites.copilot import CopilotProvider




@pytest.fixture
def synthetic_browser_actions(monkeypatch):
    """Explicitly opt legacy locator fixtures into a test-only input seam."""
    async def action_lookup(submission, selectors, *, timeout_ms, deadline):
        return await provider_base.first_visible(
            submission.page, selectors, timeout_ms=timeout_ms,
        )

    monkeypatch.setattr(provider_base, "SharedBrowserActions", SyntheticBrowserActions)
    monkeypatch.setattr(PageSubmission, "_first_action_with_access", action_lookup)


class _BlankLocator:
    async def count(self):
        return 0

    def nth(self, index):
        raise IndexError(index)


class _BlankPage:
    def locator(self, selector):
        return _BlankLocator()


class _FreshOpenPage(_BlankPage):
    def __init__(self, live_url: str) -> None:
        self.url = live_url
        self.browser_context_id = "context-1"
        self.window_name = ""
        self.goto_calls: list[tuple[str, str]] = []

    async def goto(self, url: str, *, wait_until: str) -> None:
        self.goto_calls.append((url, wait_until))
        # Simulate the live URL after browser/website redirects have settled.
        # It may be the requested blank start or an existing conversation.
        return None

    async def evaluate(
        self,
        script: str,
        value: str | None = None,
    ) -> str | None:
        if script == "() => window.name || ''":
            return self.window_name
        if script == "(value) => { window.name = value; }":
            self.window_name = str(value or "")
            return None
        raise AssertionError(f"unexpected evaluation: {script}")

@pytest.mark.parametrize(
    ("provider_type", "blank_url"),
    (
        (ChatGPTProvider, "https://chatgpt.com"),
        (GeminiProvider, "https://gemini.google.com/app/"),
        (CopilotProvider, "https://copilot.com/"),
    ),
    ids=("chatgpt", "gemini", "copilot"),
)
def test_fresh_conversational_open_accepts_only_blank_provider_start(
    provider_type,
    blank_url: str,
) -> None:
    page = _FreshOpenPage(blank_url)
    factory_calls: list[tuple[object, ...]] = []

    async def page_factory(*args: object) -> _FreshOpenPage:
        factory_calls.append(args)
        return page

    provider = provider_type(page_factory)
    submission = asyncio.run(
        provider.open(
            profile_id="first",
            chrome_directory="Profile 7",
            observation_id="obs-fresh",
            fresh=True,
        )
    )

    assert factory_calls == [
        (
            provider.platform,
            "first",
            "obs-fresh",
            True,
            None,
        )
    ]
    assert page.goto_calls == [
        (provider.spec.start_url, "domcontentloaded")
    ]
    assert submission.identity.conversation_reference == blank_url


@pytest.mark.parametrize(
    ("provider_type", "existing_conversation_url"),
    (
        (
            ChatGPTProvider,
            "https://chatgpt.com/c/existing-conversation",
        ),
        (
            GeminiProvider,
            "https://gemini.google.com/app/existing-conversation",
        ),
        (CopilotProvider, "https://copilot.com/chats/existing-conversation"),
    ),
    ids=("chatgpt", "gemini", "copilot"),
)
def test_fresh_conversational_open_rejects_existing_conversation_pre_send(
    provider_type,
    existing_conversation_url: str,
) -> None:
    page = _FreshOpenPage(existing_conversation_url)

    async def page_factory(*_args: object) -> _FreshOpenPage:
        return page

    provider = provider_type(page_factory)

    with pytest.raises(
        IdentityMismatchError,
        match="fresh chat did not remain on the blank provider start",
    ):
        asyncio.run(
            provider.open(
                profile_id="first",
                chrome_directory="Profile 7",
                observation_id="obs-fresh",
                fresh=True,
            )
        )

    assert page.goto_calls == [
        (provider.spec.start_url, "domcontentloaded")
    ]


def test_conversational_restore_keeps_existing_conversation_identity() -> None:
    existing_url = "https://chatgpt.com/c/existing-conversation"
    page = _FreshOpenPage(existing_url)

    async def page_factory(*_args: object) -> _FreshOpenPage:
        return page

    submission = asyncio.run(
        ChatGPTProvider(page_factory).open(
            profile_id="first",
            chrome_directory="Profile 7",
            observation_id="obs-restore",
            fresh=False,
        )
    )

    assert page.goto_calls == []
    assert submission.identity.conversation_reference == existing_url


@pytest.mark.parametrize(
    ("provider_type", "redirected_url"),
    (
        (
            ChatGPTProvider,
            "https://chatgpt.com/c/unexpected-conversation",
        ),
        (
            GeminiProvider,
            "https://gemini.google.com/app/unexpected-conversation",
        ),
    ),
    ids=("chatgpt", "gemini"),
)
def test_fresh_conversational_identity_is_reproved_at_send_boundary(
    provider_type,
    redirected_url: str,
) -> None:
    page = _FreshOpenPage(provider_type(lambda *_args: None).spec.start_url)

    async def page_factory(*_args: object) -> _FreshOpenPage:
        return page

    provider = provider_type(page_factory)
    submission = asyncio.run(
        provider.open(
            profile_id="first",
            chrome_directory="Profile 7",
            observation_id="obs-fresh-boundary",
            fresh=True,
        )
    )
    submission._prepared = True
    page.url = redirected_url

    with pytest.raises(
        IdentityMismatchError,
        match="fresh chat changed identity",
    ):
        asyncio.run(submission.validate_pre_send_identity())


def test_restored_conversation_identity_is_reproved_at_send_boundary() -> None:
    existing_url = "https://chatgpt.com/c/expected-conversation"
    page = _FreshOpenPage(existing_url)

    async def page_factory(*_args: object) -> _FreshOpenPage:
        return page

    provider = ChatGPTProvider(page_factory)
    submission = asyncio.run(
        provider.open(
            profile_id="first",
            chrome_directory="Profile 7",
            observation_id="obs-restored-boundary",
            fresh=False,
        )
    )
    submission._prepared = True
    page.url = "https://chatgpt.com/c/different-conversation"

    with pytest.raises(IdentityMismatchError):
        asyncio.run(submission.validate_pre_send_identity())


class _Composer:
    def __init__(self, readbacks: list[str] | None = None):
        self.value = ""
        self.writes: list[str] = []
        self.readbacks = list(readbacks or [])
        self.read_count = 0

    async def fill(self, value: str) -> None:
        self.value = value
        self.writes.append(value)

    async def input_value(self) -> str:
        self.read_count += 1
        if self.readbacks:
            return self.readbacks.pop(0)
        return self.value


class _ContentEditableComposer:
    def __init__(self):
        self.value = ""
        self.fill_calls = 0

    async def fill(self, value: str) -> None:
        self.fill_calls += 1
        self.value = value.replace("\n", "")

    async def evaluate(self, script: str, argument=None):
        if "Boolean(el && el.isContentEditable)" in script:
            return True
        if argument is not None:
            self.value = argument["value"]
            return None
        if "return el.textContent" in script:
            return self.value
        return None


def test_exact_composer_keeps_large_multiline_unicode_losslessly() -> None:
    prompt = (
        "SOURCE_JSON:\n"
        + ("診療所 🦷 café e\u0301\r\nline\t\u00a0tail\n" * 8_000)
        + "\nEND\u0000VISIBLE"
    )
    composer = _Composer()

    receipt = asyncio.run(
        write_exact_composer_input(
            composer,
            prompt,
            2,
            send_started=lambda: False,
        )
    )

    assert composer.value == prompt
    assert composer.writes == ["", prompt]
    assert receipt.text == prompt
    assert receipt.character_count == len(prompt)
    assert receipt.utf8_byte_count == len(prompt.encode("utf-8"))
    assert receipt.attempts == 1


def test_exact_composer_preserves_multiline_contenteditable_text_node() -> None:
    prompt = "instruction\n\nSOURCE_JSON:\n{\"name\":\"Café 🦷\"}"
    composer = _ContentEditableComposer()

    receipt = asyncio.run(
        write_exact_composer_input(
            composer,
            prompt,
            2,
            send_started=lambda: False,
        )
    )

    assert composer.value == prompt
    assert composer.fill_calls == 0
    assert receipt.text == prompt
    assert receipt.character_count == len(prompt)
    assert receipt.utf8_byte_count == len(prompt.encode("utf-8"))


def test_exact_composer_retries_mismatch_then_succeeds_before_send() -> None:
    prompt = "unchanged\nUnicode 🦷"
    composer = _Composer([prompt + " ", prompt])

    receipt = asyncio.run(
        write_exact_composer_input(
            composer,
            prompt,
            2,
            send_started=lambda: False,
        )
    )

    assert receipt.attempts == 2
    assert composer.writes == ["", prompt, "", prompt]


def test_exact_composer_records_typed_mismatch_and_never_normalizes() -> None:
    prompt = "line one\r\nline two\u00a0"
    composer = _Composer(["line one\nline two ", "line one\nline two "])
    failures = []

    with pytest.raises(ComposerReadbackMismatch) as caught:
        asyncio.run(
            write_exact_composer_input(
                composer,
                prompt,
                2,
                send_started=lambda: False,
                record_failure=failures.append,
            )
        )

    assert failures == [caught.value.failure]
    assert caught.value.failure.kind is ComposerFailureKind.READBACK_MISMATCH
    assert caught.value.failure.expected_text == prompt
    assert caught.value.failure.observed_text == "line one\nline two "
    assert composer.writes == ["", prompt, "", prompt]


def test_exact_composer_never_clears_for_retry_after_send_fence_started() -> None:
    prompt = "full prompt"
    composer = _Composer(["partial"])
    failures = []

    def send_started() -> bool:
        # The first mismatch occurred when read_count becomes one.
        return composer.read_count > 0

    with pytest.raises(ComposerSendAlreadyStarted) as caught:
        asyncio.run(
            write_exact_composer_input(
                composer,
                prompt,
                2,
                send_started=send_started,
                record_failure=failures.append,
            )
        )

    assert caught.value.failure.kind is ComposerFailureKind.SEND_ALREADY_STARTED
    assert composer.writes == ["", prompt]
    assert failures == [caught.value.failure]


def test_replacing_fill_does_not_clear_or_refill_after_send_started() -> None:
    composer = _Composer(["partial"])
    composer.value = "Existing unsent draft."

    def forbidden_clear(_composer):
        raise AssertionError("replacing Fill must not issue a keyboard clear")

    with pytest.raises(ComposerSendAlreadyStarted):
        asyncio.run(write_exact_composer_input(
            composer, "exact replacement", 2,
            clear=forbidden_clear, clear_before_write=False,
            send_started=lambda: composer.read_count > 0,
        ))
    assert composer.value == "exact replacement"
    assert composer.writes == ["exact replacement"]


class _TypedComposer(_Composer):
    def __init__(self, readbacks=None):
        super().__init__(readbacks)
        self.keys = []
        self.typed = []
        self.events = []

    async def press(self, key, **kwargs):
        self.keys.append(key)
        self.events.append(("press", key, kwargs))
        if key == "Backspace":
            self.value = ""
        elif key == "Shift+Enter":
            self.value += "\n"

    async def press_sequentially(self, text, **kwargs):
        self.typed.append((text, kwargs))
        self.events.append(("type", text, kwargs))
        self.value += text


def test_keyboard_input_types_multiline_with_delay_and_no_paste_or_bare_enter(monkeypatch):
    composer = _TypedComposer()
    prompt = "Café 🦷\n\n  next line  \n"
    samples = [(10.0 + (index * 7) % 21) / 2 for index in range(len(prompt) - 1)]
    random_bounds = []

    def sample_delay(low, high):
        random_bounds.append((low, high))
        return samples[len(random_bounds) - 1]

    async def record_sleep(seconds):
        composer.events.append(("sleep", seconds))

    monkeypatch.setattr(browser_input.random, "uniform", sample_delay)
    monkeypatch.setattr(browser_input.asyncio, "sleep", record_sleep)
    receipt = asyncio.run(write_exact_composer_input(
        composer, prompt, clear=clear_composer_with_keyboard, write=type_composer_value,
    ))
    assert receipt.text == prompt
    assert composer.writes == []
    assert composer.keys[0] == ("Meta+A" if sys.platform == "darwin" else "Control+A")
    assert composer.keys[1:] == ["Backspace", "Shift+Enter", "Shift+Enter", "Shift+Enter"]
    assert composer.typed == [
        (char, {"delay": 0, "timeout": 30_000})
        for char in prompt if char != "\n"
    ]
    assert random_bounds == [(5.0, 15.0)] * (len(prompt) - 1)
    expected_events = []
    for index, char in enumerate(prompt):
        if index:
            expected_events.append(("sleep", samples[index - 1] / 1000))
        expected_events.append(
            ("press", "Shift+Enter", {"delay": 0}) if char == "\n"
            else ("type", char, {"delay": 0, "timeout": 30_000})
        )
    # Every adjacent character, including blank lines and Unicode, gets a new
    # sample; no initial/final delay or extra keyboard delay is introduced.
    assert composer.events[2:] == expected_events


@pytest.mark.parametrize("prompt", ["", "🦷", "\n"])
def test_keyboard_input_with_no_character_gap_needs_no_random_delay(monkeypatch, prompt):
    composer = _TypedComposer()

    def unexpected_delay(*_args, **_kwargs):
        raise AssertionError("no delay is needed without adjacent characters")

    monkeypatch.setattr(browser_input.random, "uniform", unexpected_delay)
    monkeypatch.setattr(browser_input.asyncio, "sleep", unexpected_delay)
    asyncio.run(type_composer_value(composer, prompt))
    assert composer.value == prompt


@pytest.mark.parametrize("minimum,maximum", [
    (-1.0, 30.0), (30.0, 10.0), (10.0, -1.0),
    (float("nan"), 30.0), (10.0, float("nan")),
    (float("inf"), 30.0), (10.0, float("inf")),
    (float("-inf"), 30.0), (10.0, float("-inf")),
])
def test_keyboard_input_rejects_invalid_delay_range_before_typing(minimum, maximum):
    composer = _TypedComposer()
    with pytest.raises(ValueError):
        asyncio.run(type_composer_value(
            composer, "exact prompt", min_delay_ms=minimum, max_delay_ms=maximum,
        ))
    assert composer.events == []


def test_keyboard_input_keeps_mismatch_and_send_fence_checks():
    composer = _TypedComposer(["changed prompt"])
    failures = []
    with pytest.raises(ComposerSendAlreadyStarted):
        asyncio.run(write_exact_composer_input(
            composer, "expected prompt", 2,
            clear=clear_composer_with_keyboard, write=type_composer_value,
            send_started=lambda: composer.read_count > 0,
            record_failure=failures.append,
        ))
    assert "".join(char for char, _ in composer.typed) == "expected prompt"
    assert len(failures) == 1
    assert failures[0].observed_text == "changed prompt"


def test_keyboard_input_rejects_carriage_return_before_any_typing():
    composer = _TypedComposer()
    with pytest.raises(ExactComposerInputFailure) as caught:
        asyncio.run(write_exact_composer_input(
            composer, "line\r\nline", 1,
            clear=clear_composer_with_keyboard, write=type_composer_value,
        ))
    assert caught.value.failure.kind is ComposerFailureKind.WRITE_FAILED
    assert composer.typed == []
    assert "Enter" not in composer.keys


def test_page_submission_records_one_typed_composer_failure(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_browser_actions,
) -> None:
    prompt = "exact\nprompt"
    composer = _Composer(["mismatch", "mismatch"])
    failures = []

    async def first_visible(*_args, **_kwargs):
        return composer

    from visibility_runner.websites import chat_actions as provider_base

    monkeypatch.setattr(provider_base, "first_visible", first_visible)
    submission = PageSubmission(
        page=object(),
        profile_id="first",
        chrome_directory="Profile 7",
        observation_id="obs-1",
        spec=PageProviderSpec(
            platform="gemini",
            start_url="https://example.invalid",
            composer_selectors=("composer",),
            send_selectors=("send",),
            answer_selectors=("answer",),
        ),
        identity=BrowserIdentity(
            profile_id="first",
            chrome_directory="Profile 7",
            browser_context_reference="context-1",
            tab_reference="tab-1",
        ),
        composer_attempts=2,
    )

    with pytest.raises(ComposerReadbackMismatch):
        asyncio.run(
            submission.prepare_exact(
                prompt,
                on_pre_send_failure=failures.append,
            )
        )

    assert len(failures) == 1
    assert failures[0].kind is ComposerFailureKind.READBACK_MISMATCH
    assert failures[0].expected_text == prompt


def test_send_control_trial_precedes_and_reuses_for_real_click(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_browser_actions,
) -> None:
    from visibility_runner.websites import chat_actions as provider_base

    composer = _Composer()
    click_calls = []
    send_lookups = 0

    class Control:
        async def click(self, **kwargs):
            click_calls.append(dict(kwargs))

    control = Control()

    async def first_visible(_page, selectors, **_kwargs):
        nonlocal send_lookups
        if tuple(selectors) == ("composer",):
            return composer
        assert tuple(selectors) == ("send",)
        send_lookups += 1
        return control

    monkeypatch.setattr(provider_base, "first_visible", first_visible)
    submission = PageSubmission(
        page=_BlankPage(),
        profile_id="first",
        chrome_directory="Profile 7",
        observation_id="obs-1",
        spec=PageProviderSpec(
            platform="gemini",
            start_url="https://example.invalid",
            composer_selectors=("composer",),
            send_selectors=("send",),
            answer_selectors=("answer",),
        ),
        identity=BrowserIdentity(
            profile_id="first",
            chrome_directory="Profile 7",
            browser_context_reference="context-1",
            tab_reference="tab-1",
        ),
    )

    async def exercise():
        await submission.prepare_exact("exact prompt")
        assert click_calls == [{"trial": True, "timeout": 4_000}]
        await submission.submit_once()

    asyncio.run(exercise())

    assert click_calls == [{"trial": True, "timeout": 4_000}, {}]
    assert send_lookups == 1
    assert submission._submitted is True


def test_intercepted_trial_click_is_pre_send_and_retry_safe(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_browser_actions,
) -> None:
    from visibility_runner.websites import chat_actions as provider_base

    composer = _Composer()

    class Control:
        blocked = True
        trial_calls = 0
        real_clicks = 0

        async def click(self, *, trial=False, timeout=None):
            if trial:
                self.trial_calls += 1
                if self.blocked:
                    raise RuntimeError(
                        "cdk-overlay-backdrop intercepts pointer events"
                    )
                return
            self.real_clicks += 1

    control = Control()

    async def first_visible(_page, selectors, **_kwargs):
        if tuple(selectors) == ("composer",):
            return composer
        return control

    monkeypatch.setattr(provider_base, "first_visible", first_visible)
    submission = PageSubmission(
        page=_BlankPage(),
        profile_id="first",
        chrome_directory="Profile 7",
        observation_id="obs-1",
        spec=PageProviderSpec(
            platform="gemini",
            start_url="https://example.invalid",
            composer_selectors=("composer",),
            send_selectors=("send",),
            answer_selectors=("answer",),
        ),
        identity=BrowserIdentity(
            profile_id="first",
            chrome_directory="Profile 7",
            browser_context_reference="context-1",
            tab_reference="tab-1",
        ),
    )

    with pytest.raises(RuntimeError, match="cdk-overlay-backdrop"):
        asyncio.run(submission.prepare_exact("exact prompt"))

    assert submission._submitted is False
    assert submission._prepared is False
    assert control.real_clicks == 0

    control.blocked = False

    async def retry():
        await submission.prepare_exact("exact prompt")
        await submission.submit_once()

    asyncio.run(retry())

    assert control.trial_calls == 2
    assert control.real_clicks == 1


def test_trial_keyword_type_error_never_falls_back_to_real_click(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_browser_actions,
) -> None:
    from visibility_runner.websites import chat_actions as provider_base

    composer = _Composer()

    class Control:
        clicks = 0

        async def click(self):
            self.clicks += 1

    control = Control()

    async def first_visible(_page, selectors, **_kwargs):
        if tuple(selectors) == ("composer",):
            return composer
        return control

    monkeypatch.setattr(provider_base, "first_visible", first_visible)
    submission = PageSubmission(
        page=object(),
        profile_id="first",
        chrome_directory="Profile 7",
        observation_id="obs-1",
        spec=PageProviderSpec(
            platform="gemini",
            start_url="https://example.invalid",
            composer_selectors=("composer",),
            send_selectors=("send",),
            answer_selectors=("answer",),
        ),
        identity=BrowserIdentity(
            profile_id="first",
            chrome_directory="Profile 7",
            browser_context_reference="context-1",
            tab_reference="tab-1",
        ),
    )

    with pytest.raises(
        BrowserInteractionError,
        match="safe Playwright trial click",
    ):
        asyncio.run(submission.prepare_exact("exact prompt"))

    assert control.clicks == 0
    assert submission._submitted is False
    assert submission._prepared is False


def test_no_send_control_preserves_enter_fallback(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_browser_actions,
) -> None:
    from visibility_runner.websites import chat_actions as provider_base

    class Composer(_Composer):
        def __init__(self):
            super().__init__()
            self.presses = []

        async def press(self, key):
            self.presses.append(key)

    composer = Composer()

    async def first_visible(_page, selectors, **_kwargs):
        if tuple(selectors) == ("composer",):
            return composer
        raise TimeoutError

    monkeypatch.setattr(provider_base, "first_visible", first_visible)
    submission = PageSubmission(
        page=_BlankPage(),
        profile_id="first",
        chrome_directory="Profile 7",
        observation_id="obs-1",
        spec=PageProviderSpec(
            platform="gemini",
            start_url="https://example.invalid",
            composer_selectors=("composer",),
            send_selectors=("send",),
            answer_selectors=("answer",),
        ),
        identity=BrowserIdentity(
            profile_id="first",
            chrome_directory="Profile 7",
            browser_context_reference="context-1",
            tab_reference="tab-1",
        ),
    )

    async def exercise():
        await submission.prepare_exact("exact prompt")
        await submission.submit_once()

    asyncio.run(exercise())

    assert composer.presses == ["Enter"]
    assert submission._submitted is True


def test_interaction_pacing_is_cancel_aware_before_any_send() -> None:
    with pytest.raises(BrowserInteractionError, match="cancellation"):
        asyncio.run(
            interaction_pause(
                10,
                10,
                cancelled=lambda: True,
            )
        )
