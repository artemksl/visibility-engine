"""Gemini preparation sequences on isolated, traffic-denied HTML fixtures."""
from __future__ import annotations

import hashlib
import json
from html import escape

import pytest

from support.dom import (
    DOCUMENT_PREFIX,
    bound_submission,
    evaluate_main_world,
    isolated_browser,
    run_dom_case,
)
from visibility_runner.browser.input import read_typed_composer_value
from visibility_runner.execution.run_measurement_group import MeasurementGroupWorker
from visibility_runner.storage.measurement_files import MeasurementFiles
from visibility_runner.websites.chat_actions import ProviderRegistry
from visibility_runner.websites.conversation_binding import ConversationCorrespondenceError

pytestmark = pytest.mark.local_browser

URL = "https://gemini.google.com/app/synthetic-map-conversation"
SOURCE_PROMPT = "Recommend Synthetic Clinic."
SOURCE_ANSWER = "Synthetic Clinic is an example."
FOLLOWUP = "Return the clinic names from the captured answer as a JSON array."


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def document(*, source_prompt=SOURCE_PROMPT):
    return DOCUMENT_PREFIX + (
        '<user-query data-query-id="u1"><p>' + escape(source_prompt) + '</p></user-query>'
        '<model-response data-response-id="a1"><div class="markdown">'
        + escape(SOURCE_ANSWER) + '</div></model-response>'
        '<rich-textarea><div class="ql-editor" contenteditable="true" '
        'role="textbox"><p>Existing unsent draft.</p></div></rich-textarea>'
        '<button id="send" aria-label="Send message">Send</button>'
        '<map-local-immersive-panel id="map" style="display:none">'
        '<div class="maps-container" aria-hidden="false"><div class="action-bar">'
        '<gem-icon-button class="close-button"><button aria-label="Close" '
        'onclick="sessionStorage.setItem(\'map-closed\', \'1\'); '
        'document.getElementById(\'map\').remove()">Close</button></gem-icon-button>'
        '</div></div></map-local-immersive-panel>'
        '<script>window.sends=[]; window.editorKeys=[]; window.editorInputs=[]; '
        'const editor=document.querySelector(\'.ql-editor\'); '
        'editor.addEventListener(\'keydown\', e=>window.editorKeys.push(e.key)); '
        'editor.addEventListener(\'input\', e=>window.editorInputs.push(e.isTrusted)); '
        'document.getElementById(\'send\').onclick=()=>window.sends.push(editor.innerText);'
        '</script>'
    )


def first_turn_binding(submission):
    return {
        "schema_version": "browser-conversation-binding-v1",
        "identity": submission.identity.to_mapping(),
        "prompt_sha256": digest(SOURCE_PROMPT),
        "expected_turn": 1,
        "response_baseline": {
            "visible_response_count": 0,
            "newest_response_sha256": digest(""),
        },
        "transcript": {
            "prior_user_sha256": [], "prior_assistant_sha256": [],
            "user_ids": ["u1"], "assistant_ids": ["a1"], "branch_id": None,
        },
    }


def test_native_fill_replaces_nonempty_quill_draft_without_keyboard_clear(isolated_browser):
    # Quill's native Fill replaces paragraphs. Preserve literal line/Unicode
    # readback while removing the two preparatory native keyboard operations.
    prompt = "\n  Café Łódź 😀 e\u0301\u00a0\u200b\n\n" + "long source " * 500 + "\n"

    async def check(page):
        submission = await bound_submission(page, "gemini")
        assert await read_typed_composer_value(page.locator(".ql-editor")) == "Existing unsent draft."
        await submission.prepare_exact(prompt)
        assert await read_typed_composer_value(page.locator(".ql-editor")) == prompt
        inputs = await evaluate_main_world(page, "window.editorInputs")
        assert inputs and all(inputs)
        assert "Backspace" not in await evaluate_main_world(page, "window.editorKeys")
        assert await evaluate_main_world(page, "window.sends") == []
        assert submission._submitted is False
        await submission._actions.release_all()

    run_dom_case(isolated_browser, document(), check)


@pytest.mark.parametrize("changed_history", (False, True), ids=("same_history", "changed_user"))
def test_map_close_restores_canonical_history_before_preparing_second_turn(
    isolated_browser, changed_history,
):
    loop, browser = isolated_browser
    documents = [document(), document(source_prompt="Different original prompt." if changed_history else SOURCE_PROMPT)]

    async def exercise():
        async with browser.context({URL: lambda: documents.pop(0)}) as guarded:
            page = await guarded.context.new_page()
            await page.goto(URL)
            submission = await bound_submission(page, "gemini")
            submission._last_correlation_value = first_turn_binding(submission)
            submission._submitted = True
            submission.next_turn()
            await page.evaluate("""() => {
                history.pushState({}, '', '?canvas-id=synthetic-map');
                document.querySelector('.ql-editor').style.display='none';
                document.getElementById('map').style.display='block';
            }""")
            if changed_history:
                with pytest.raises(ConversationCorrespondenceError, match="conversation_prompt_mismatch"):
                    await submission.prepare_exact(FOLLOWUP)
                assert await evaluate_main_world(page, "window.editorInputs") == []
                assert await read_typed_composer_value(page.locator(".ql-editor")) == "Existing unsent draft."
                assert submission._prepared is False
            else:
                await submission.prepare_exact(FOLLOWUP)
                assert await read_typed_composer_value(page.locator(".ql-editor")) == FOLLOWUP
                assert submission.response_baseline.visible_response_count == 1
                assert submission.identity.conversation_reference == URL
            assert page.url == URL
            assert await evaluate_main_world(page, "sessionStorage.getItem('map-closed')") == "1"
            assert guarded.fulfilled == [URL, URL]
            assert await evaluate_main_world(page, "window.sends") == []
            assert submission._submitted is False
            await submission._actions.release_all()

    loop.run_until_complete(exercise())


@pytest.mark.parametrize("notice", ("known", "quoted", "unknown"))
def test_cookie_dismissal_is_limited_to_known_notice_outside_conversation(
    isolated_browser, notice,
):
    cookie = (
        '<div role="dialog" id="cookie-notice"><cookie-banner>'
        '<button data-test-id="reject-button" onclick="window.rejected += 1; '
        'document.getElementById(\'cookie-notice\').remove()">Reject all</button>'
        '<button onclick="window.accepted += 1">Accept all</button>'
        '</cookie-banner></div>'
    )
    if notice == "quoted":
        cookie = '<model-response><div class="markdown">' + cookie + '</div></model-response>'
    elif notice == "unknown":
        cookie = cookie.replace("<cookie-banner>", "<div>").replace("</cookie-banner>", "</div>")
    markup = '<script>window.rejected=0; window.accepted=0;</script>' + cookie

    async def check(page):
        submission = await bound_submission(page, "gemini")
        await submission._assert_access_available()
        assert await evaluate_main_world(page, "window.rejected") == (1 if notice == "known" else 0)
        assert await evaluate_main_world(page, "window.accepted") == 0
        assert await page.locator("#cookie-notice").count() == (0 if notice == "known" else 1)
        assert submission._submitted is False
        await submission._actions.release_all()

    run_dom_case(isolated_browser, markup, check)


@pytest.mark.parametrize("answer_state", ("rendered", "empty", "ambiguous"))
def test_original_failure_capture_records_rendered_transcript_without_input(
    isolated_browser, tmp_path, answer_state,
):
    prompt = "You said literal user text. Café 🦷 " + "source " * 1500 + "END"
    content = "Synthetic Clinic"
    if answer_state == "empty":
        content = ""
    roots = '<div class="markdown">' + escape(content) + '</div>'
    if answer_state == "ambiguous":
        roots += '<message-content style="display:block">Another unrelated root.</message-content>'
    markup = (
        '<user-query data-query-id="u1"><h5 class="screen-reader-user-query-label">'
        'You said ' + escape(prompt) + '</h5><p>' + escape(prompt) + '</p></user-query>'
        '<model-response data-response-id="a1" style="min-height:32px">' + roots + '</model-response>'
        '<script>window.interactions={input:0,click:0,keydown:0}; '
        'for (const type of Object.keys(window.interactions)) '
        'document.addEventListener(type,()=>window.interactions[type]++);</script>'
    )

    async def check(page):
        submission = await bound_submission(page, "gemini")
        repository = MeasurementFiles(tmp_path)
        worker = MeasurementGroupWorker(repository, ProviderRegistry())
        references = await worker._capture_send_failure(
            "synthetic-failure", "packet-1", None, submission,
            RuntimeError("synthetic original response error"),
            phase="response_wait", observation_id="observation-1",
        )
        assert references and "html" in references
        metadata = json.loads(repository.read_evidence("synthetic-failure", references["metadata"]))
        assert metadata["html_status"] == "complete"
        assert metadata["coherence"] == "same_document"
        transcript = metadata["transcript"]
        assert transcript["document_coherence"] == "same_document"
        if answer_state == "ambiguous":
            assert transcript["status"] == "unavailable"
            assert transcript["failure_context"]["exception_type"] == "BrowserInteractionError"
        else:
            assert transcript["status"] == "captured"
            assert transcript["rendered_user_text"] == prompt[:8192]
            assert transcript["rendered_user_sha256"] == digest(prompt)
            assert transcript["rendered_user_character_count"] == len(prompt)
            assert transcript["rendered_user_truncated"] is True
            assert transcript["logical_user_count"] == transcript["logical_assistant_count"] == 1
            assert transcript["latest_user_id"] == "u1"
            assert transcript["latest_assistant_id"] == "a1"
            assert transcript["answer_readability"] == ("empty" if answer_state == "empty" else "nonempty")
            assert transcript["latest_answer_sha256"] == digest(content)
            assert transcript["generating"] is False
        assert await evaluate_main_world(page, "window.interactions") == {"input": 0, "click": 0, "keydown": 0}
        for reference in references.values():
            assert (repository.run_dir("synthetic-failure") / reference["path"]).stat().st_mode & 0o777 == 0o600
        await submission._actions.release_all()

    run_dom_case(isolated_browser, markup, check)
