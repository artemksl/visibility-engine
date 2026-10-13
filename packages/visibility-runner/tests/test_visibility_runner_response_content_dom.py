"""Synthetic DOM-only response boundaries, with no website or profile access."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import html
import json

import pytest

pytestmark = pytest.mark.local_browser

from support.dom import (
    DOCUMENT_PREFIX,
    bound_submission,
    isolated_browser,
    run_dom_case,
)
from visibility_runner.errors import BrowserInteractionError, IdentityMismatchError
from visibility_runner.names.check_chat_names import (
    MentionExtractionError,
    parse_closed_name_array,
)
from visibility_runner.names.parse_source_answer import source_parser_prompt
from visibility_runner.websites.chat_actions import (
    visible_answer_snapshot,
    wait_for_stable_answer,
)
from visibility_runner.websites.gemini import GEMINI_SPEC
from visibility_runner.websites.contracts import ReconciliationDisposition
from visibility_runner.websites.page_readers import _read_conversation


ARRAY_TEXT = json.dumps(["Synthetic Clinic"], ensure_ascii=False)
COMPLETION_URL = "https://gemini.google.com/app/synthetic-completion"
ROOT_MARKUP = (
    '<div class="markdown" id="content">{body}</div>',
    '<message-content id="content" style="display:block">{body}</message-content>',
    '<structured-content-container class="model-response-text" id="content" '
    'style="display:block">{body}</structured-content-container>',
)


def wrapper(body, *, alternate=False):
    if alternate:
        return f'<div data-test-id="model-response">{body}</div>'
    # Matching both wrapper selectors must not double-count the same element.
    return f'<model-response data-test-id="model-response">{body}</model-response>'


def user_message(prompt):
    return (
        '<user-query><div class="query-text">'
        '<h5 class="cdk-visually-hidden screen-reader-user-query-label">'
        f'<span>You said</span> {html.escape(prompt)}</h5>'
        f'<p class="query-text-line">{html.escape(prompt)}</p>'
        '</div></user-query>'
    )


def captured_gemini_user_message(prompt):
    # 0.2.77 failure HTML: the accessibility preview is truncated, while each
    # original prompt line remains in its own paragraph under query-text.
    lines = "".join(
        f'<p class="query-text-line"> {html.escape(line)} </p>'
        for line in prompt.split("\n")
    )
    return (
        '<user-query><user-query-content><div class="query-content">'
        '<span class="user-query-bubble-with-background luminous-collapsed">'
        '<div class="query-text collapsed">'
        '<h5 class="cdk-visually-hidden screen-reader-user-query-label">'
        f'<span>You said</span> {html.escape(prompt[:99])}… </h5>'
        + lines + '</div></span><div class="luminous-toggle-container">'
        '<button aria-label="Expand">Expand</button></div></div>'
        '<div class="luminous-actions-container"><button>Copy prompt</button>'
        '<button>Edit</button></div></user-query-content></user-query>'
    )


def completed_message(text):
    return wrapper(f'<div class="markdown">{html.escape(text)}</div>')


def run_completion_case(isolated_browser, markup, check):
    loop, browser = isolated_browser

    async def run():
        async with browser.context({COMPLETION_URL: DOCUMENT_PREFIX + markup}) as guarded:
            page = await guarded.context.new_page()
            await page.goto(COMPLETION_URL)
            await check(page)
            assert guarded.fulfilled == [COMPLETION_URL]
            assert guarded.denied == []

    loop.run_until_complete(run())


@pytest.mark.parametrize("turn", ("source", "same_chat_extraction", "github_sdk_fresh_parser"))
def test_gemini_captured_full_prompt_lines_are_the_completion_identity(isolated_browser, turn):
    source_prompt = "You said literal content. Name two clinics in Marylebone, London. " + ("Full clinic names. " * 8).rstrip()
    source_answer = "Synthetic Clinic\n\nCafé Clinic 🦷"
    prompt = source_prompt if turn == "source" else source_parser_prompt(source_answer)
    prior = captured_gemini_user_message(source_prompt) + completed_message(source_answer) if turn == "same_chat_extraction" else ""

    async def check(page):
        submission = await bound_submission(page, "gemini",
            spec=replace(GEMINI_SPEC, stable_seconds=0., response_timeout_seconds=2.))
        await submission._capture_pre_send_answer()
        submission._prompt = prompt
        submission._submitted = True
        await page.locator("body").evaluate("(node, markup) => node.insertAdjacentHTML('beforeend', markup)",
            captured_gemini_user_message(prompt) + completed_message(ARRAY_TEXT))
        try:
            conversation = await _read_conversation(page, GEMINI_SPEC)
            assert conversation.prompt_text == prompt
            assert conversation.history["user_sha256"][-1] == hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            assert (await submission.wait_response()).text == ARRAY_TEXT
        finally:
            await submission.close()

    run_completion_case(isolated_browser, prior, check)


def test_gemini_completed_answer_rejects_another_prompt_without_checkpoint_writer(isolated_browser):
    async def check(page):
        submission = await bound_submission(page, "gemini",
            spec=replace(GEMINI_SPEC, stable_seconds=0., response_timeout_seconds=2.))
        await submission._capture_pre_send_answer()
        submission._prompt = "Expected admitted prompt."
        submission._submitted = True
        await page.locator("body").evaluate("(node, markup) => node.insertAdjacentHTML('beforeend', markup)",
            captured_gemini_user_message("Different prompt.") + completed_message(ARRAY_TEXT))
        try:
            with pytest.raises(IdentityMismatchError, match="conversation_prompt_mismatch"):
                await submission.wait_response()
            assert submission._native_answer_captured is False
        finally:
            await submission.close()

    run_completion_case(isolated_browser, "", check)


@pytest.mark.parametrize("turn", ("measurement", "same_chat_extraction", "github_sdk_fresh_parser", "same_node_native_retry"))
def test_gemini_completion_proves_the_logical_turn_and_preserves_exact_text(isolated_browser, turn):
    prior = user_message("Source prompt.") + completed_message("Synthetic Clinic") if turn == "same_chat_extraction" else ""
    prompt = "Extract the ordered names from the admitted source." if "parser" in turn or "extraction" in turn else "Measurement prompt."

    async def check(page):
        submission = await bound_submission(page, "gemini",
            spec=replace(GEMINI_SPEC, stable_seconds=0., response_timeout_seconds=2.))
        await submission._capture_pre_send_answer()
        logical, history = submission.response_baseline, submission._history_before
        rows = []
        submission.set_correlation_checkpoint(rows.append)
        submission._prompt = prompt
        if turn == "same_node_native_retry":
            await page.locator("body").evaluate("(node, markup) => node.insertAdjacentHTML('beforeend', markup)",
                user_message(prompt) + completed_message("Failed placeholder."))
            await submission._capture_pre_send_answer()
            submission._logical_response_baseline = logical
            submission._history_before = history
            submission._native_retry_sent = True
            await page.locator(".markdown").last.evaluate("(node, text) => node.textContent = text", ARRAY_TEXT)
        else:
            await page.locator("body").evaluate("(node, markup) => node.insertAdjacentHTML('beforeend', markup)",
                user_message(prompt) + completed_message(ARRAY_TEXT))
        # This test starts at the admitted-send state; no website input is performed.
        submission._submitted = True
        try:
            answer = await submission.wait_response()
            assert answer.text == ARRAY_TEXT
            assert parse_closed_name_array(answer.text) == ["Synthetic Clinic"]
            assert rows[-1]["expected_turn"] == (2 if turn == "same_chat_extraction" else 1)
            assert len(await page.locator("user-query").all()) == rows[-1]["expected_turn"]
            if turn == "same_node_native_retry":
                assert rows[-1]["response_baseline"]["visible_response_count"] == 1
                assert rows[-1]["logical_response_baseline"]["visible_response_count"] == 0
        finally:
            await submission.close()

    run_completion_case(isolated_browser, prior, check)


@pytest.mark.parametrize("interruption", ("hidden_content", "native_locator_timeout"))
def test_gemini_final_read_waits_for_transient_content_under_same_deadline(isolated_browser, monkeypatch, interruption):
    from visibility_runner.websites import chat_actions
    from playwright.async_api import TimeoutError as LocatorTimeoutError

    async def check(page):
        submission = await bound_submission(page, "gemini",
            spec=replace(GEMINI_SPEC, stable_seconds=0., response_timeout_seconds=2.))
        await submission._capture_pre_send_answer()
        submission._prompt = "Expected admitted prompt."
        submission._submitted = True
        await page.locator("body").evaluate("(node, markup) => node.insertAdjacentHTML('beforeend', markup)",
            user_message(submission._prompt) + completed_message(ARRAY_TEXT))
        read = chat_actions._read_conversation
        hidden_once = False
        deadlines = []

        async def transient_read(*args, **kwargs):
            nonlocal hidden_once
            deadlines.append(kwargs["_deadline"].expires_at)
            if hidden_once:
                return await read(*args, **kwargs)
            hidden_once = True
            if interruption == "native_locator_timeout":
                raise LocatorTimeoutError("Synthetic final transcript locator timeout")
            await page.locator("model-response").evaluate("node => node.style.minHeight = '48px'")
            await page.locator(".markdown").evaluate("node => node.style.display = 'none'")
            try:
                conversation = await read(*args, **kwargs)
                assert conversation.answer.count == 1 and conversation.answer.text == ""
                return conversation
            finally:
                await page.locator(".markdown").evaluate("node => node.style.display = 'block'")

        monkeypatch.setattr(chat_actions, "_read_conversation", transient_read)
        try:
            answer = await submission.wait_response()
            assert hidden_once and answer.text == ARRAY_TEXT
            assert len(set(deadlines)) == 1
        finally:
            await submission.close()

    run_completion_case(isolated_browser, "", check)


def test_restored_gemini_native_retry_cannot_accept_the_unchanged_failed_wrapper(isolated_browser):
    async def check(page):
        submission = await bound_submission(page, "gemini",
            spec=replace(GEMINI_SPEC, stable_seconds=0., response_timeout_seconds=.65))
        await submission._capture_pre_send_answer()
        logical, history = submission.response_baseline, submission._history_before
        prompt = "Expected admitted prompt."
        await page.locator("body").evaluate("(node, markup) => node.insertAdjacentHTML('beforeend', markup)",
            user_message(prompt) + completed_message("Unchanged failed placeholder."))
        await submission._capture_pre_send_answer()
        submission._prompt = prompt
        submission._logical_response_baseline = logical
        submission._history_before = history
        binding = submission._conversation_binding(submission.identity)
        submission.restore_recovery_attempt(prompt=prompt,
            response_baseline=submission.response_baseline.to_mapping(), binding=binding)
        assert submission._before_answer_text == ""
        try:
            with pytest.raises(TimeoutError, match="response did not settle"):
                await submission.wait_response()
            assert submission._native_answer_captured is False
            assert await page.locator("user-query").count() == 1
        finally:
            await submission.close()

    run_completion_case(isolated_browser, "", check)


async def snapshot(page):
    return await visible_answer_snapshot(
        page,
        GEMINI_SPEC.response_node_selectors,
        label_selectors=GEMINI_SPEC.rendered_answer_label_selectors,
        content_selectors=GEMINI_SPEC.response_content_selectors,
        decoration_selectors=GEMINI_SPEC.response_content_decoration_selectors,
    )


@pytest.mark.parametrize("prompt", (
    "Recommend good clinics for laser hair removal in Marylebone.",
    "You said literal user text must remain.",
), ids=("duplicate_accessibility_heading", "literal_user_prefix"))
def test_gemini_user_heading_excludes_duplicated_prompt_without_stripping_user_text(
    isolated_browser, prompt
):
    # Captured Gemini user-query: the h5 repeats the full prompt for readers.
    markup = (
        '<user-query><div class="query-text">'
        '<h5 class="cdk-visually-hidden screen-reader-user-query-label">'
        f'<span>You said</span> {html.escape(prompt)} </h5>'
        f'<p class="query-text-line"> {html.escape(prompt)} </p>'
        '</div></user-query>'
    )

    async def check(page):
        conversation = await _read_conversation(page, GEMINI_SPEC)
        assert conversation.prompt_text == prompt
        assert conversation.history["user_sha256"] == [
            hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        ]

    run_dom_case(isolated_browser, markup, check)


def test_gemini_user_lines_preserve_inline_breaks_and_empty_logical_lines(isolated_browser):
    prompt = "You said actual content.\nSecond part.\n\nFinal line."
    markup = (
        '<user-query><div class="query-text">'
        '<h5 class="screen-reader-user-query-label">You said truncated…</h5>'
        '<p class="query-text-line">You said actual content.<br>Second part.</p>'
        '<p class="query-text-line"><br></p>'
        '<p class="query-text-line">Final line.</p></div></user-query>'
    )

    async def check(page):
        conversation = await _read_conversation(page, GEMINI_SPEC)
        assert conversation.prompt_text == prompt
        assert conversation.history["user_sha256"] == [hashlib.sha256(prompt.encode("utf-8")).hexdigest()]

    run_dom_case(isolated_browser, markup, check)


@pytest.mark.parametrize("inside_conversation", (False, True),
                         ids=("optional_location_notice", "model_content_untouched"))
@pytest.mark.parametrize("nested_labels", (False, True),
                         ids=("direct_labels", "nested_material_labels"))
def test_gemini_location_notice_dismisses_only_known_optional_ui(
    isolated_browser, inside_conversation, nested_labels
):
    def label(text):
        if not nested_labels:
            return text
        return (
            '<span class="mat-mdc-button-persistent-ripple mdc-button__ripple"></span>'
            f'<span class="mdc-button__label">{text}</span>'
            '<span class="mat-focus-indicator"></span>'
            '<span class="mat-mdc-button-touch-target"></span>'
        )

    notice = (
        '<div role="dialog" id="location-notice">'
        '<button id="precise-location" onclick="window.locationGranted += 1">'
        f'{label("Use precise location")}</button>'
        '<button id="dismiss-location" onclick="window.locationDismissed += 1; '
        'document.getElementById(\'location-notice\').remove()">'
        f'{label("Dismiss")}</button></div>'
    )
    markup = (
        '<script>window.locationGranted = 0; window.locationDismissed = 0; '
        'window.otherDismissed = 0;</script>'
        + (wrapper(notice) if inside_conversation else notice)
        + '<div role="dialog" id="other-notice"><button '
        f'onclick="window.otherDismissed += 1">{label("Dismiss")}</button></div>'
    )

    async def check(page):
        submission = await bound_submission(page, "gemini")
        await submission._assert_access_available()
        assert await page.evaluate("window.locationGranted") == 0
        assert await page.evaluate("window.otherDismissed") == 0
        assert await page.evaluate("window.locationDismissed") == (0 if inside_conversation else 1)
        assert await page.locator("#location-notice").count() == (1 if inside_conversation else 0)
        assert submission._submitted is False
        await submission._actions.release_all()

    run_dom_case(isolated_browser, markup, check)


@pytest.mark.parametrize(
    ("alternate", "root_markup", "label_markup"),
    (
        (False, ROOT_MARKUP[0], '<h2 class="cdk-visually-hidden">Gemini said</h2>'),
        (False, ROOT_MARKUP[1], '<div role="heading" aria-level="2">Gemini said</div>'),
        (True, ROOT_MARKUP[2], '<h2 class="cdk-visually-hidden">Gemini said</h2>'),
        (True, ROOT_MARKUP[0], '<div role="heading" aria-level="2">Gemini said</div>'),
    ),
    ids=("tag-markdown-known", "tag-message-content-other", "test-id-structured-known", "test-id-markdown-other"),
)
def test_content_boundary_excludes_wrapper_labels_without_text_stripping(
    isolated_browser, alternate, root_markup, label_markup
):
    markup = wrapper(
        label_markup + root_markup.format(body=html.escape(ARRAY_TEXT)),
        alternate=alternate,
    )

    async def check(page):
        captured = await snapshot(page)
        assert captured.count == 1
        assert captured.text == ARRAY_TEXT
        assert parse_closed_name_array(captured.text) == ["Synthetic Clinic"]

    run_dom_case(isolated_browser, markup, check)


def test_content_boundary_preserves_literal_prefix_and_nested_label_nodes(
    isolated_browser,
):
    content = (
        '<message-content id="content" style="display:block">'
        '<div class="markdown" style="white-space:pre-wrap">'
        'Gemini said this is literal assistant prose.\n'
        '<h2 class="cdk-visually-hidden">Gemini said</h2>'
        'Unicode α and repeated Synthetic Clinic / Synthetic Clinic stay intact.'
        '</div></message-content>'
    )
    markup = wrapper('<h2 class="cdk-visually-hidden">Gemini said</h2>' + content)

    async def check(page):
        expected = await page.locator("#content").inner_text()
        captured = await snapshot(page)
        assert captured.count == 1
        assert captured.text == expected
        assert captured.text.startswith("Gemini said this is literal assistant prose.")
        assert captured.text.count("Gemini said") == 2
        with pytest.raises(MentionExtractionError):
            parse_closed_name_array(captured.text)

    run_dom_case(isolated_browser, markup, check)


def test_nested_root_aliases_capture_the_full_outer_content_not_a_partial_array(
    isolated_browser,
):
    content = (
        '<structured-content-container class="model-response-text" id="content" '
        'style="display:block"><message-content style="display:block">'
        'Actual assistant introduction.'
        f'<div class="markdown">{html.escape(ARRAY_TEXT)}</div>'
        'Actual assistant conclusion.'
        '</message-content></structured-content-container>'
    )

    async def check(page):
        expected = await page.locator("#content").inner_text()
        captured = await snapshot(page)
        assert captured.count == 1
        assert captured.text == expected
        assert "Actual assistant introduction." in captured.text
        assert "Actual assistant conclusion." in captured.text
        with pytest.raises(MentionExtractionError):
            parse_closed_name_array(captured.text)

    run_dom_case(isolated_browser, wrapper(content), check)


@pytest.mark.parametrize(
    "outer_style",
    (
        "display:contents",
        "display:block;width:0;height:0;overflow:visible",
    ),
    ids=("display_contents", "zero_box_overflow"),
)
def test_outer_alias_without_its_own_box_never_becomes_a_partial_child_answer(
    isolated_browser, outer_style
):
    content = (
        f'<message-content id="content" style="{outer_style}">'
        'Visible assistant introduction.'
        f'<div class="markdown" style="width:240px">{html.escape(ARRAY_TEXT)}</div>'
        'Visible assistant conclusion.'
        '</message-content>'
    )

    async def check(page):
        expected = await page.locator("#content").inner_text()
        captured = await snapshot(page)
        assert captured.count == 1
        assert captured.text in ("", expected)
        if captured.text:
            assert "Visible assistant introduction." in captured.text
            assert "Visible assistant conclusion." in captured.text

    run_dom_case(
        isolated_browser,
        wrapper('<div role="heading">Gemini said</div>' + content),
        check,
    )


@pytest.mark.parametrize("model_prefix", ("", "JSON is literal model prose."),
                         ids=("website_language_header", "literal_model_prefix"))
def test_gemini_code_toolbar_requires_dom_proven_leading_position(
    isolated_browser, model_prefix
):
    prefix = f"<p>{model_prefix}</p>" if model_prefix else ""
    content = (
        '<message-content id="content" style="display:block"><div class="markdown">'
        + prefix
        + '<code-block><div class="code-block"><div class="formatted-code-block-internal-container">'
        '<div class="animated-opacity"><div class="code-block-decoration header-formatted gds-emphasized-body-m">'
        '<span>JSON</span><div class="buttons"><button aria-label="Download code"></button>'
        '<button aria-label="Copy code"></button></div></div>'
        f'<pre><code role="text" data-test-id="code-content">{html.escape(ARRAY_TEXT)}</code></pre>'
        '</div></div></div></code-block></div></message-content>'
    )

    async def check(page):
        captured = await snapshot(page)
        assert captured.count == 1
        assert (await _read_conversation(page, GEMINI_SPEC)).answer == captured
        if model_prefix:
            assert captured.text == await page.locator("#content").inner_text()
            assert captured.text.startswith(model_prefix)
        else:
            assert captured.text.strip() == ARRAY_TEXT
            assert parse_closed_name_array(captured.text) == ["Synthetic Clinic"]

    run_dom_case(isolated_browser, wrapper(content), check)


@pytest.mark.parametrize(
    "newest_content",
    (
        "",
        '<div class="markdown" style="min-height:1px"></div>',
        f'<div class="markdown" style="display:none">{html.escape(ARRAY_TEXT)}</div>',
    ),
    ids=("missing", "empty", "hidden"),
)
def test_empty_or_missing_newest_content_keeps_wrapper_count_without_old_fallback(
    isolated_browser, newest_content
):
    prior = wrapper(f'<div class="markdown">{html.escape(ARRAY_TEXT)}</div>')
    newest = wrapper('<div role="heading">Gemini said</div>' + newest_content)

    async def check(page):
        captured = await snapshot(page)
        assert captured.count == 2
        assert captured.text == ""

    run_dom_case(isolated_browser, prior + newest, check)


@pytest.mark.parametrize(
    "same_selector", (False, True), ids=("different_selectors", "same_selector")
)
def test_disjoint_content_roots_fail_closed_without_combining_or_choosing_text(
    isolated_browser, same_selector
):
    private_marker = "private synthetic content must not enter diagnostics"
    second = (
        f'<div class="markdown">{private_marker}</div>'
        if same_selector
        else f'<message-content style="display:block">{private_marker}</message-content>'
    )
    markup = wrapper(f'<div class="markdown">{html.escape(ARRAY_TEXT)}</div>' + second)

    async def check(page):
        with pytest.raises(BrowserInteractionError) as raised:
            await snapshot(page)
        assert str(raised.value) == "provider_response_content_ambiguous"
        assert private_marker not in str(raised.value)

    run_dom_case(isolated_browser, markup, check)


def test_older_ambiguous_content_is_not_read_when_newest_response_is_unambiguous(
    isolated_browser,
):
    prior = wrapper('<div class="markdown">earlier</div><div class="markdown">text</div>')
    newest = wrapper(f'<div class="markdown">{html.escape(ARRAY_TEXT)}</div>')

    async def check(page):
        captured = await snapshot(page)
        assert captured.count == 2
        assert captured.text == ARRAY_TEXT

    run_dom_case(isolated_browser, prior + newest, check)


def test_stable_wait_uses_content_root_without_altering_parser_contract(
    isolated_browser,
):
    markup = wrapper(
        '<div role="heading">Gemini said</div>'
        f'<message-content style="display:block">{html.escape(ARRAY_TEXT)}</message-content>'
    )

    async def check(page):
        spec = replace(GEMINI_SPEC, stable_seconds=0.0, response_timeout_seconds=2.0)
        captured = await wait_for_stable_answer(
            page, spec, before_count=0, before_answer=""
        )
        assert captured == ARRAY_TEXT
        assert parse_closed_name_array(captured) == ["Synthetic Clinic"]

    run_dom_case(isolated_browser, markup, check)


@pytest.mark.parametrize("matching_prompt", (True, False), ids=("exact_multiline_prompt", "different_prompt_refused"))
def test_baseline_and_reconciliation_use_the_same_content_boundary(isolated_browser, matching_prompt):
    prior_text = json.dumps(["Prior Synthetic Clinic"])
    prompt = source_parser_prompt(prior_text)
    prior = captured_gemini_user_message("Prior source prompt.") + wrapper(
        '<div role="heading">Gemini said</div>'
        f'<div class="markdown">{html.escape(prior_text)}</div>'
    )

    async def check(page):
        submission = await bound_submission(page, "gemini")
        await submission._capture_pre_send_answer()
        assert submission.response_baseline.visible_response_count == 1
        assert submission._before_answer_text == prior_text
        assert submission.response_baseline.newest_response_sha256 == hashlib.sha256(
            prior_text.encode("utf-8")
        ).hexdigest()
        submission._prompt = prompt
        submission._submitted = True
        await page.locator("body").evaluate(
            "(node, markup) => node.insertAdjacentHTML('beforeend', markup)",
            captured_gemini_user_message(prompt if matching_prompt else prompt + " Different request.") + wrapper(
                '<div role="heading">Gemini said</div>'
                f'<div class="markdown">{html.escape(ARRAY_TEXT)}</div>'
            ),
        )
        probe = await submission._reconciliation_probe()
        assert probe.answer_text == ARRAY_TEXT
        assert probe.new_response is True
        assert probe.prompt_exact is matching_prompt
        result = await submission.reconcile_no_response(0)
        assert result.native_retry_available is False
        if matching_prompt:
            assert result.disposition is ReconciliationDisposition.LATE_RESPONSE_CAPTURED
            assert result.answer.text == ARRAY_TEXT
        else:
            assert result.disposition is ReconciliationDisposition.AMBIGUOUS
            assert result.evidence["reason"] == "expected_user_message_unproven"
            assert result.answer is None
        assert await page.locator("user-query").count() == 2
        await submission.close()

    run_completion_case(isolated_browser, prior, check)
