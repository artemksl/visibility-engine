"""Shared input executes only inside isolated, network-denied runner tabs."""
from dataclasses import replace
from html import escape
import time

import pytest

from automation_core.browser.async_actions import AsyncActionError
from support.dom import DOCUMENT_PREFIX, bound_submission, isolated_browser, evaluate_main_world
from visibility_runner.browser.input import read_typed_composer_value
from visibility_runner.errors import BrowserInteractionError
from visibility_runner.names.parse_source_answer import source_parser_prompt
from visibility_runner.websites.chat_actions import PageProviderSpec

pytestmark = pytest.mark.local_browser

URL = "https://fixture.test/chat"
SPEC = PageProviderSpec(platform="chatgpt", start_url=URL,
    composer_selectors=("#composer",), send_selectors=("#send",),
    answer_selectors=(".answer",), response_node_selectors=(".answer",),
    user_message_selectors=(".user",), conversation_url_prefixes=("https://fixture.test/c/",),
    stable_seconds=0)


def document(editor="textarea"):
    composer = '<textarea id="composer"></textarea>' if editor == "textarea" else '<div id="composer" contenteditable="true"></div>'
    return DOCUMENT_PREFIX + composer + '''<button id="send">Send</button>
    <script>window.sends=[];
    document.querySelector('#send').onclick=()=>{
        const input=document.querySelector('#composer');
        window.sends.push(input.value === undefined ? input.innerText : input.value);
    };</script>'''


@pytest.mark.parametrize("paragraph", [False, True], ids=["native-div-lines", "native-p-lines"])
@pytest.mark.parametrize("prompt", [
    "  leading and trailing  ",
    "\nfirst\n\n  indented  \n",
    "\n\n",
    "Café Łódź 😀 e\u0301\u00a0\u200b\u200c\n  last  \n",
])
def test_native_rich_fill_has_exact_line_readback(isolated_browser, paragraph, prompt):
    loop, browser = isolated_browser
    markup = document("contenteditable")
    if paragraph:
        markup = markup.replace('contenteditable="true"></div>',
            'contenteditable="true"><p><br class="ProseMirror-trailingBreak"></p></div>')
    async def exercise():
        async with browser.context({URL: markup}) as guarded:
            page = await guarded.context.new_page()
            await page.goto(URL)
            await page.evaluate("""() => {
                window.editorInputs = [];
                document.querySelector('#composer').addEventListener('input', event =>
                    window.editorInputs.push(event.isTrusted));
            }""")
            submission = await bound_submission(page, "chatgpt", spec=SPEC)
            await submission.prepare_exact(prompt)
            assert await read_typed_composer_value(page.locator("#composer")) == prompt
            inputs = await evaluate_main_world(page, "() => window.editorInputs")
            assert inputs and all(inputs)
    loop.run_until_complete(exercise())


@pytest.mark.parametrize("contents", [
    "<p>first</p><div>second</div>",
    "<div>first<div>nested</div></div><div>second</div>",
    "<p>first</p><ul><li>second</li></ul>",
    '<p>first</p><p><span><div style="display:inline">second</div></span></p>',
])
def test_rich_line_readback_refuses_unknown_blocks(isolated_browser, contents):
    loop, browser = isolated_browser
    markup = DOCUMENT_PREFIX + '<div id="composer" contenteditable="true">' + contents + '</div>'
    async def exercise():
        async with browser.context({URL: markup}) as guarded:
            page = await guarded.context.new_page()
            await page.goto(URL)
            assert await read_typed_composer_value(page.locator("#composer")) is None
    loop.run_until_complete(exercise())


@pytest.mark.parametrize("graphics_display", ["inline", "block"])
def test_rich_line_readback_preserves_formatted_source_prompt(isolated_browser, graphics_display):
    loop, browser = isolated_browser
    url = "https://clinic.example/care"
    prompt = source_parser_prompt("  Café Clinic\u00a0" + url + "\u200b\n  last  \n")
    formatted_url = ('<span style="display:inline-flex"><span><span aria-hidden="true" '
        'contenteditable="false"><svg width="16" height="16">'
        + f'<g style="display:{graphics_display}"><path style="display:{graphics_display}" d="M0 0"/></g>'
        + '</svg></span></span>'
        + escape(url) + '</span>')
    lines = [escape(line).replace(escape(url), formatted_url) for line in prompt.split("\n")]
    markup = DOCUMENT_PREFIX + '<div id="composer" contenteditable="true">' + ''.join(
        '<p>' + line + '</p>' for line in lines
    ) + '</div>'
    async def exercise():
        async with browser.context({URL: markup}) as guarded:
            page = await guarded.context.new_page()
            await page.goto(URL)
            assert await page.locator("svg g").evaluate("el => getComputedStyle(el).display") == graphics_display
            assert await read_typed_composer_value(page.locator("#composer")) == prompt
    loop.run_until_complete(exercise())


def test_rich_line_readback_keeps_literal_svg_text(isolated_browser):
    loop, browser = isolated_browser
    markup = DOCUMENT_PREFIX + ('<div id="composer" contenteditable="true"><p>first '
        '<svg style="display:none"> EXTRA\u200b\u200c </svg>'
        '</p><p>second</p></div>')
    async def exercise():
        async with browser.context({URL: markup}) as guarded:
            page = await guarded.context.new_page()
            await page.goto(URL)
            assert await read_typed_composer_value(page.locator("#composer")) == "first  EXTRA\u200b\u200c \nsecond"
    loop.run_until_complete(exercise())


@pytest.mark.parametrize("html_child", ["<br>", '<div style="display:inline"><br></div>'])
def test_rich_line_readback_refuses_svg_with_html_line_structure(isolated_browser, html_child):
    loop, browser = isolated_browser
    markup = DOCUMENT_PREFIX + ('<div id="composer" contenteditable="true"><p>first '
        '<svg style="display:inline"><foreignObject style="display:inline">'
        + html_child + '</foreignObject></svg></p><p>second</p></div>')
    async def exercise():
        async with browser.context({URL: markup}) as guarded:
            page = await guarded.context.new_page()
            await page.goto(URL)
            assert await read_typed_composer_value(page.locator("#composer")) is None
    loop.run_until_complete(exercise())


def test_rich_line_readback_keeps_hidden_and_zero_width_text(isolated_browser):
    loop, browser = isolated_browser
    markup = DOCUMENT_PREFIX + ('<div id="composer" contenteditable="true"><p> first </p>'
        '<p>second<span hidden>EXTRA\u200b\u200c</span></p></div>')
    async def exercise():
        async with browser.context({URL: markup}) as guarded:
            page = await guarded.context.new_page()
            await page.goto(URL)
            assert await read_typed_composer_value(page.locator("#composer")) == " first \nsecondEXTRA\u200b\u200c"
    loop.run_until_complete(exercise())


@pytest.mark.parametrize("editor", ["textarea", "contenteditable"])
def test_shared_executor_prepares_and_sends_exact_multiline(isolated_browser, editor):
    loop, browser = isolated_browser
    prompt = "first line\nUnicode Łódź 😀\nlast line"
    async def exercise():
        async with browser.context({URL: document(editor)}) as guarded:
            page = await guarded.context.new_page()
            await page.goto(URL)
            submission = await bound_submission(page, "chatgpt", spec=SPEC)
            await submission.prepare_exact(prompt)
            await submission.validate_pre_send_identity()
            await submission.submit_once()
            assert await evaluate_main_world(page, "() => window.sends") == [prompt]
            with pytest.raises(BrowserInteractionError, match="only once"):
                await submission.submit_once()
    loop.run_until_complete(exercise())


@pytest.mark.parametrize("change", ["prompt", "send_node", "composer_node", "duplicate_send", "duplicate_composer", "identity", "baseline"])
def test_shared_executor_rejects_changed_source_before_send(isolated_browser, change):
    loop, browser = isolated_browser
    async def exercise():
        async with browser.context({URL: document()}) as guarded:
            page = await guarded.context.new_page()
            await page.goto(URL)
            submission = await bound_submission(page, "chatgpt", spec=SPEC)
            await submission.prepare_exact("exact prompt")
            await submission.validate_pre_send_identity()
            if change == "prompt":
                await page.locator("#composer").fill("operator draft")
            elif change in ("send_node", "composer_node"):
                selector = "#send" if change == "send_node" else "#composer"
                await page.locator(selector).evaluate("el => el.replaceWith(el.cloneNode(true))")
            elif change in ("duplicate_send", "duplicate_composer"):
                selector = "#send" if change == "duplicate_send" else "#composer"
                await page.locator(selector).evaluate("el => el.after(el.cloneNode(true))")
            elif change == "identity":
                original = page._visibility_runner_owned_tab_identity
                page._visibility_runner_owned_tab_identity = replace(original, tab_reference="visibility-runner-owned-tab:replacement")
            else:
                await page.evaluate("() => { const answer=document.createElement('div');answer.className='answer';answer.textContent='unexpected answer';document.body.append(answer); }")
            with pytest.raises(AsyncActionError):
                await submission.submit_once()
            assert await evaluate_main_world(page, "() => window.sends") == []
            assert submission._submitted
            with pytest.raises(BrowserInteractionError, match="only once"):
                await submission.submit_once()
    loop.run_until_complete(exercise())


def test_uncertain_native_send_never_replays(isolated_browser):
    loop, browser = isolated_browser
    async def exercise():
        async with browser.context({URL: document()}) as guarded:
            page = await guarded.context.new_page()
            await page.goto(URL)
            submission = await bound_submission(page, "chatgpt", spec=SPEC)
            await submission.prepare_exact("exact prompt")
            native = submission._send_target.native.handle
            original = native.click
            async def uncertain(**kwargs):
                await original(**kwargs)
                raise RuntimeError("synthetic transport disconnect after click")
            native.click = uncertain
            with pytest.raises(AsyncActionError) as caught:
                await submission.submit_once()
            assert caught.value.receipt.possibly_dispatched
            assert await evaluate_main_world(page, "() => window.sends") == ["exact prompt"]
            with pytest.raises(BrowserInteractionError, match="only once"):
                await submission.submit_once()
            assert await evaluate_main_world(page, "() => window.sends") == ["exact prompt"]
    loop.run_until_complete(exercise())


def test_shared_executor_moves_virtual_pointer_before_retained_send(isolated_browser):
    loop, browser = isolated_browser
    markup = document().replace('<textarea', '<style>textarea{position:absolute;left:40px;top:350px;width:300px;height:80px}button{position:absolute;left:500px;top:350px}</style><textarea')
    async def exercise():
        async with browser.context({URL: markup}) as guarded:
            page = await guarded.context.new_page()
            await page.goto(URL)
            await page.evaluate("""() => {
                window.pointerEvents = [];
                document.addEventListener('mousemove', event =>
                    window.pointerEvents.push({kind:'move',trusted:event.isTrusted,x:event.clientX,y:event.clientY}));
                document.querySelector('#send').addEventListener('click', event =>
                    window.pointerEvents.push({kind:'click',trusted:event.isTrusted}));
            }""")
            submission = await bound_submission(page, "chatgpt", spec=SPEC)
            await submission.prepare_exact("exact prompt")
            await submission.validate_pre_send_identity()
            await submission.submit_once()
            events = await evaluate_main_world(page, "() => window.pointerEvents")
            moves = [event for event in events if event['kind'] == 'move']
            assert len(moves) > 3
            assert all(event['trusted'] for event in events)
            assert events[-1]['kind'] == 'click'
            assert len([event for event in events if event['kind'] == 'click']) == 1
            assert await evaluate_main_world(page, "() => window.sends") == ["exact prompt"]
    loop.run_until_complete(exercise())


def test_shared_copilot_typing_keeps_existing_preparation_budget(isolated_browser):
    loop, browser = isolated_browser
    prompt = "a" * 500
    spec = replace(SPEC, platform="copilot", type_composer_input=True,
        composer_typing_min_delay_ms=5, composer_typing_max_delay_ms=15)
    async def exercise():
        async with browser.context({URL: document()}) as guarded:
            page = await guarded.context.new_page()
            await page.goto(URL)
            submission = await bound_submission(page, "copilot", spec=spec)
            started = time.monotonic()
            await submission.prepare_exact(prompt)
            elapsed = time.monotonic() - started
            assert elapsed < submission.preparation_timeout_seconds(prompt)
            return elapsed
    elapsed = loop.run_until_complete(exercise())
    print(f"500-character preparation: {elapsed:.3f}s")


def test_long_rich_draft_keeps_original_preparation_deadline(isolated_browser):
    loop, browser = isolated_browser
    prompt = source_parser_prompt(("Unicode Łódź 😀 " * 120) + "\n" + ("second line " * 120))
    async def exercise():
        async with browser.context({URL: document("contenteditable")}) as guarded:
            page = await guarded.context.new_page()
            await page.goto(URL)
            submission = await bound_submission(page, "chatgpt", spec=SPEC)
            executor = submission._actions.executor
            calls = {"fill": 0, "type_text": 0}
            original_fill, original_type = executor.fill, executor.type_text
            async def counted_fill(*args, **kwargs):
                calls["fill"] += 1
                return await original_fill(*args, **kwargs)
            async def counted_type(*args, **kwargs):
                calls["type_text"] += 1
                return await original_type(*args, **kwargs)
            executor.fill, executor.type_text = counted_fill, counted_type
            started = time.monotonic()
            await submission.prepare_exact(prompt)
            elapsed = time.monotonic() - started
            assert elapsed < 30
            assert calls == {"fill": 1, "type_text": 0}
            await submission.submit_once()
            assert await evaluate_main_world(page, "() => window.sends") == [prompt]
            return elapsed
    elapsed = loop.run_until_complete(exercise())
    print(f"{len(prompt)}-character rich preparation: {elapsed:.3f}s")


@pytest.mark.parametrize("change", [None, "retry_node", "prompt", "prior_history"])
def test_shared_native_retry_keeps_latest_response_target(isolated_browser, change):
    loop, browser = isolated_browser
    spec = replace(SPEC, terminal_error_within_response_selectors=(".error",),
        terminal_error_markers=("Something went wrong",),
        native_retry_within_response_selectors=(".retry",), response_timeout_seconds=2)
    markup = DOCUMENT_PREFIX + '''<div class="user">Previous question</div><div class="answer">Previous answer</div>
    <textarea id="composer"></textarea><button id="send">Send</button>
    <script>window.sends=[];window.retries=0;
    document.querySelector('#send').onclick=()=>{
        const text=document.querySelector('#composer').value;window.sends.push(text);
        const user=document.createElement('div');user.className='user';user.textContent=text;document.body.append(user);
        const answer=document.createElement('div');answer.className='answer';
        answer.innerHTML='<span class="error">Something went wrong</span><button class="retry">Retry</button>';
        answer.querySelector('button').onclick=()=>{window.retries++;answer.textContent='regenerated answer';};
        document.body.append(answer);document.querySelector('#composer').value='';
        history.replaceState({},'', '/c/retry');
    };</script>'''
    async def exercise():
        async with browser.context({URL: markup}) as guarded:
            page = await guarded.context.new_page()
            await page.goto(URL)
            submission = await bound_submission(page, "chatgpt", spec=spec)
            await submission.prepare_exact("exact prompt")
            await submission.submit_once()
            # Represent the runner's already confirmed failed attempt. This
            # fixture exercises native retry input; the existing reconciliation
            # tests own grace-period polling and durable admission.
            probe = await submission._reconciliation_probe(include_retry_control=True)
            assert probe.prompt_exact and probe.identity_valid and not probe.new_response
            assert probe.native_retry_count == 1 and probe.retryable_failure
            submission._confirmed_failure_signature = probe.retryable_failure
            assert await submission.prepare_native_retry()
            if change is not None:
                if change == "retry_node":
                    await page.locator(".retry").evaluate("el => el.replaceWith(el.cloneNode(true))")
                elif change == "prompt":
                    await page.locator(".user").last.evaluate("el => { el.textContent='Changed question'; }")
                else:
                    await page.locator(".user").first.evaluate("el => { el.textContent='Changed history'; }")
                with pytest.raises(AsyncActionError):
                    await submission.submit_once()
                assert await evaluate_main_world(page, "() => window.retries") == 0
            else:
                await submission.submit_once()
                assert (await submission.wait_response()).text == "regenerated answer"
                assert await evaluate_main_world(page, "() => window.retries") == 1
            assert await evaluate_main_world(page, "() => window.sends") == ["exact prompt"]
    loop.run_until_complete(exercise())
