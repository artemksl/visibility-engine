"""Synthetic Copilot DOM only: no live website capture, account or network.

Markup exercises selectors inherited from the old host driver. It proves shared
browser mechanics and deterministic extraction, not live website compatibility.
"""
import hashlib
import asyncio
import html
from dataclasses import replace

import pytest

from support.dom import evaluate_main_world, DOCUMENT_PREFIX, isolated_browser, bound_submission, run_dom_case
from visibility_runner.errors import BrowserInteractionError, HumanVerificationRequiredError, ProviderAccessBlockedError
from visibility_runner.websites.copilot import COPILOT_SPEC, CopilotProvider
from visibility_runner.websites import chat_actions as chat
from visibility_runner.websites.conversation_binding import ConversationCorrespondenceError
from visibility_runner.names.prompts import MENTION_EXTRACTION_PROMPT
from visibility_runner.browser.input import (
    ComposerReadbackMismatch, clear_composer_with_keyboard,
    read_typed_composer_value, type_composer_value, write_exact_composer_input,
)

pytestmark = pytest.mark.local_browser


@pytest.mark.parametrize('element', [
    '<textarea data-testid="composer-input"></textarea>',
    '<div data-testid="composer-input" contenteditable="true" role="textbox"></div>',
])
@pytest.mark.parametrize('prompt', ['  Café 🦷 e\u0301\n\nnext\tline  ', '\n', '\n\n', 'last\n\n'])
def test_keyboard_preparation_preserves_editor_state_and_line_breaks(isolated_browser, element, prompt):
    markup = element + '''<button data-testid="submit-button">Send</button><script>
    window.keys=[];window.inputs=[];window.pastes=0;window.sends=0;window.model='';
    const el=document.querySelector('[data-testid="composer-input"]');
    el.addEventListener('keydown',e=>{
        window.keys.push({key:e.key,shift:e.shiftKey});
        if(e.key==='Enter'&&!e.shiftKey){window.sends++;e.preventDefault();}
    });
    el.addEventListener('input',e=>{
        window.inputs.push({trusted:e.isTrusted,type:e.inputType});
        if(e.isTrusted) window.model=el.value??el.innerText;
    });
    el.addEventListener('paste',()=>window.pastes++);
    </script>'''
    async def check(page):
        submission = await bound_submission(page, 'copilot', spec=replace(
            COPILOT_SPEC, composer_typing_min_delay_ms=1, composer_typing_max_delay_ms=1,
        ))
        # The production path still uses the same exact receipt and explicit
        # send fence afterward; preparation only trial-clicks the button.
        await submission.prepare_exact(prompt)
        composer = page.locator('[data-testid="composer-input"]')
        assert await read_typed_composer_value(composer) == prompt
        state = await evaluate_main_world(page, '({keys,inputs,pastes,sends,model})')
        assert state['pastes'] == state['sends'] == 0
        assert state['inputs'] and all(event['trusted'] for event in state['inputs'])
        assert any(event['key'] == 'Backspace' for event in state['keys'])
        assert all(event['shift'] for event in state['keys'] if event['key'] == 'Enter')
        assert submission._prepared and not submission._submitted
    run_dom_case(isolated_browser, markup, check)


def test_keyboard_readback_does_not_accept_extra_editor_text(isolated_browser):
    async def check(page):
        composer = page.locator('[contenteditable]')
        async def type_then_corrupt(target, text):
            await type_composer_value(target, text, min_delay_ms=1, max_delay_ms=1)
            await target.press_sequentially('EXTRA', delay=1)
        with pytest.raises(ComposerReadbackMismatch) as caught:
            await write_exact_composer_input(
                composer, 'expected\n', 1,
                clear=clear_composer_with_keyboard, write=type_then_corrupt,
                read=read_typed_composer_value,
            )
        assert caught.value.failure.observed_text == 'expected\nEXTRA'
    run_dom_case(isolated_browser, '<div contenteditable="true"></div>', check)


@pytest.mark.parametrize('corruption,observed', [
    ("const extra=document.createElement('span');extra.hidden=true;extra.textContent='EXTRA';el.append(extra);", 'expected promptEXTRA'),
    ("el.style.whiteSpace='normal';el.firstChild.textContent='expected  prompt';", 'expected  prompt'),
])
def test_keyboard_readback_does_not_hide_or_collapse_changed_dom_text(isolated_browser, corruption, observed):
    async def check(page):
        composer = page.locator('[contenteditable]')
        async def type_then_corrupt(target, text):
            await type_composer_value(target, text, min_delay_ms=1, max_delay_ms=1)
            await target.evaluate('el=>{' + corruption + '}')
        with pytest.raises(ComposerReadbackMismatch) as caught:
            await write_exact_composer_input(
                composer, 'expected prompt', 1,
                clear=clear_composer_with_keyboard, write=type_then_corrupt,
                read=read_typed_composer_value,
            )
        assert caught.value.failure.observed_text == observed
    run_dom_case(isolated_browser, '<div contenteditable="true"></div>', check)


@pytest.mark.parametrize('markup,expected', [
    ('<p>first<br>second</p>', 'first\nsecond'),
    ('first<br>second', 'first\nsecond'),
    ('<p>first</p><p>second</p>', 'first\nsecond'),
    ('<p>first</p><div>second</div>', None),
])
def test_keyboard_readback_handles_plain_lines_and_refuses_mixed_blocks(isolated_browser, markup, expected):
    async def check(page):
        assert await read_typed_composer_value(page.locator('[contenteditable]')) == expected
    run_dom_case(isolated_browser, '<div contenteditable="true">' + markup + '</div>', check)


def test_keyboard_input_in_parallel_tabs_stays_on_each_page(isolated_browser):
    loop, browser = isolated_browser
    async def run():
        async with browser.context() as guarded:
            pages = [await guarded.context.new_page() for _ in range(2)]
            for page in pages:
                await page.set_content(DOCUMENT_PREFIX + '<textarea></textarea>')
            prompts = ['First page α\nonly here', 'Second page 🦷\nonly here']
            async def prepare(page, prompt):
                async def write(target, text):
                    await type_composer_value(target, text, min_delay_ms=1, max_delay_ms=1)
                await write_exact_composer_input(
                    page.locator('textarea'), prompt,
                    clear=clear_composer_with_keyboard, write=write,
                    read=read_typed_composer_value,
                )
            await asyncio.gather(*(prepare(page, prompt) for page, prompt in zip(pages, prompts)))
            assert [await page.locator('textarea').input_value() for page in pages] == prompts
    loop.run_until_complete(run())


async def capture(page):
    return await chat.wait_for_stable_answer(
        page, replace(COPILOT_SPEC, response_timeout_seconds=2, stable_seconds=0),
        before_count=0, before_answer="",
    )


@pytest.mark.parametrize('outcome', ['late_route', 'missing_route', 'cancelled'])
def test_completed_source_waits_for_its_canonical_route_under_response_deadline(
        isolated_browser, monkeypatch, outcome):
    loop, browser = isolated_browser
    root = 'https://copilot.com/chat'
    canonical = root + '/conversation/offline-late-source'
    document = DOCUMENT_PREFIX + '''<div data-testid="chatQuestion">You said
        <div data-testid="chatOutput" data-message-id="u1" style="white-space:pre-wrap">Source prompt\n</div></div>
        <div data-content="ai-message" data-message-id="a1">Alpha Clinic.</div>
        <script>window.sends=0;</script>'''
    binding_wait = asyncio.Event()
    deadlines = []
    original = chat.wait_for_stable_answer
    async def read_stable(*args, **kwargs):
        assert args[0].url == canonical
        assert deadlines == [kwargs['_deadline'].expires_at]
        answer = await original(*args, **kwargs)
        return answer
    monkeypatch.setattr(chat, 'wait_for_stable_answer', read_stable)
    async def run():
        async with browser.context({root: document}) as guarded:
            page = await guarded.context.new_page()
            await page.goto(root)
            submission = await bound_submission(page, 'copilot', spec=replace(
                COPILOT_SPEC, stable_seconds=0, response_timeout_seconds=1.2))
            submission._require_fresh_blank_start = True
            submission._prompt = 'Source prompt'
            submission._response_baseline = chat.ResponseBaseline(0, hashlib.sha256(b'').hexdigest())
            submission._history_before = {'user_sha256': [], 'assistant_sha256': [],
                'user_ids': [], 'assistant_ids': []}
            cancelled = False
            submission.cancelled = lambda: cancelled
            checkpoints = []
            submission.set_correlation_checkpoint(checkpoints.append)
            submission._submitted = True
            ownership = submission._actions.assert_owned
            async def owned(until):
                if not deadlines:
                    deadlines.append(until)
                assert deadlines == [until]
                binding_wait.set()
                return await ownership(until)
            submission._actions.assert_owned = owned
            result = asyncio.create_task(submission.wait_response())
            await binding_wait.wait()
            await asyncio.sleep(.05)
            assert not result.done() and page.url == root and checkpoints == []
            if outcome == 'late_route':
                # The canonical render can replace the previously stable text.
                # Read stability after that render, rather than bind old text.
                await evaluate_main_world(page, '''() => {
                    document.querySelector('[data-content="ai-message"]').innerHTML =
                        '<p>Alpha Clinic.</p><p>Beta Clinic.</p>';
                    history.replaceState({}, '', '/chat/conversation/offline-late-source');
                }''')
                answer = await result
                assert answer.text == 'Alpha Clinic.\n\nBeta Clinic.' and answer.identity.conversation_reference == canonical
                assert checkpoints[-1]['identity']['conversation_reference'] == canonical
                assert checkpoints[-1]['transcript']['assistant_ids'] == ['a1']
                submission.next_turn()
                assert submission._previous_turn_binding == checkpoints[-1]
            elif outcome == 'cancelled':
                cancelled = True
                with pytest.raises(BrowserInteractionError, match='binding wait was cancelled'):
                    await result
            else:
                with pytest.raises(TimeoutError, match='response did not settle'):
                    await result
            assert await evaluate_main_world(page, 'window.sends') == 0
            if outcome != 'late_route':
                assert checkpoints == [] and not submission._native_answer_captured
    loop.run_until_complete(run())


@pytest.mark.parametrize('rendered, matches', [('Source  prompt\n', True), ('Source changed prompt\n', False)])
def test_rendered_user_boundary_whitespace_keeps_internal_text_exact(isolated_browser, rendered, matches):
    prompt = 'Source  prompt'
    answer = '\nAlpha Clinic.\n'
    markup = f'''<div data-testid="chatQuestion"><h3>You said</h3>
        <div data-testid="chatOutput" style="white-space:pre-wrap">{rendered}</div></div>
        <div data-testid="markdown-reply" style="white-space:pre-wrap">{answer}</div>'''
    async def check(page):
        observed = await chat._read_conversation(page, COPILOT_SPEC)
        assert observed.prompt_text == rendered.strip()
        assert (observed.history['user_sha256'] == [hashlib.sha256(prompt.encode()).hexdigest()]) is matches
        assert observed.answer.text == answer
        assert observed.history['assistant_sha256'] == [hashlib.sha256(answer.encode()).hexdigest()]
        # The native/frozen prompt hash does not acquire this DOM read rule.
        assert chat._text_sha256(rendered) != chat._text_sha256(rendered.strip())
        submission = await bound_submission(page, 'copilot', spec=COPILOT_SPEC)
        submission._prompt = prompt
        submission._response_baseline = chat.ResponseBaseline(0, hashlib.sha256(b'').hexdigest())
        probe = await submission._reconciliation_probe()
        assert probe.prompt_exact is matches
    run_dom_case(isolated_browser, markup, check)


@pytest.mark.parametrize('structure', ['plain_normal', 'plain_pre_wrap', 'br_only', 'hidden_descendant', 'older_root'])
def test_copilot_plain_user_text_preserves_literal_breaks_for_quota_correlation(isolated_browser, structure):
    prompt = 'Copy exact names.\n\nSource text:\n"Alpha  Clinic\\nBeta Clinic"'
    content = 'Copy exact names.\n<br>\nSource text:\n"Alpha  Clinic\\nBeta Clinic"\n'
    if structure == 'hidden_descendant':
        content += '<span hidden>unexpected hidden content</span>'
    elif structure == 'br_only':
        content = 'Copy exact names.<br><br>Source text:<br>"Alpha  Clinic\\nBeta Clinic"'
    attributes = ('data-content="user-message"' if structure == 'older_root'
        else 'data-testid="chatOutput"')
    whitespace = 'pre-wrap' if structure == 'plain_pre_wrap' else 'normal'
    notice = "You've reached your daily limit. Get more usage now or check back at 1:00am"
    markup = (f'<main><div role="feed"><div data-testid="chatQuestion"><div {attributes} '
        f'style="white-space:{whitespace}">{content}</div></div>'
        '<div data-testid="markdown-reply">Alpha Clinic</div></div>'
        f'<div role="status">{html.escape(notice)}</div></main>')
    async def check(page):
        user = page.locator('[data-testid="chatOutput"], [data-content="user-message"]')
        rendered = (await user.inner_text()).strip()
        assert rendered != prompt
        observed = await chat._read_conversation(page, COPILOT_SPEC)
        matches = structure in ('plain_normal', 'plain_pre_wrap')
        assert observed.prompt_text == (prompt if matches else rendered)
        assert (observed.history['user_sha256'] == [chat._text_sha256(prompt)]) is matches
        assert bool(await chat.current_turn_notices(page, COPILOT_SPEC, prompt)) is matches
        submission = await bound_submission(page, 'copilot', spec=COPILOT_SPEC)
        submission._prompt = prompt
        submission._response_baseline = chat.ResponseBaseline(1, chat._text_sha256('Alpha Clinic'))
        probe = await submission._reconciliation_probe()
        assert probe.prompt_exact is matches
        if matches:
            with pytest.raises(ProviderAccessBlockedError) as caught:
                await chat.wait_for_stable_answer(page, replace(COPILOT_SPEC,
                    stable_seconds=0, response_timeout_seconds=1),
                    before_count=1, before_answer='Alpha Clinic', expected_prompt=prompt)
            assert caught.value.reason == 'usage_limit'
    run_dom_case(isolated_browser, markup, check)


@pytest.mark.parametrize('surface', ['status', 'region', 'quoted', 'different_prompt'])
def test_daily_limit_notice_outside_feed_requires_latest_prompt_and_excludes_quotes(isolated_browser, surface):
    prompt = 'Source prompt'
    notice = "You've reached your daily limit. Get more usage now or check back at 1:00 AM."
    banner = f'<div role="status">{notice}</div>'
    if surface == 'region':
        banner = ('<div role="status" style="display:none">' + notice + '</div>'
            '<div role="region" aria-label="Daily limit"><span role="heading" style="display:block">'
            "You've reached your daily limit</span><span style=\"display:block\">Get more usage now or check back at 1:00am</span></div>")
    response = '<div data-testid="copilot-message-div"><div role="article">' + notice + '</div></div>'
    if surface == 'quoted':
        response = '<div data-testid="markdown-reply">The website can say: ' + banner + '</div>'
        banner = ''
    markup = '<main><div role="feed"><div data-testid="chatQuestion">You said<div '
    markup += 'data-testid="chatOutput" style="white-space:pre-wrap">Source prompt\n</div></div>' + response + '</div>' + banner + '</main>'
    async def check(page):
        expected = 'Another prompt' if surface == 'different_prompt' else prompt
        notices = await chat.current_turn_notices(page, COPILOT_SPEC, expected)
        assert bool(notices) is (surface in ('status', 'region'))
        if notices:
            limits = []
            with pytest.raises(ProviderAccessBlockedError) as caught:
                await chat.wait_for_stable_answer(page, replace(COPILOT_SPEC,
                    stable_seconds=0, response_timeout_seconds=1),
                    before_count=0, before_answer='', expected_prompt=expected,
                    usage_limit_observer=limits.append)
            assert caught.value.reason == 'usage_limit'
            assert limits[-1]['reset_clock'] == '01:00' and limits[-1]['reset_timezone'] is None
            history = await chat._conversation_history(page, COPILOT_SPEC)
            assert len(history['user_sha256']) == 1 and history['assistant_sha256'] == []
        elif surface == 'quoted':
            answer = await chat.wait_for_stable_answer(page, replace(COPILOT_SPEC,
                stable_seconds=0, response_timeout_seconds=1),
                before_count=0, before_answer='', expected_prompt=expected)
            assert 'check back at 1:00 AM' in answer
    run_dom_case(isolated_browser, markup, check)


@pytest.mark.parametrize('variant', ['content', 'testid', 'nested_aliases'])
def test_narrow_assistant_roots_exclude_user_toolbar_and_source_cards(isolated_browser, variant):
    answer = 'Copilot said: Alpha Clinic. Verify you are human is an example phrase.'
    if variant == 'content':
        root = f'<div data-content="ai-message">{answer}</div>'
    elif variant == 'testid':
        root = f'<div data-testid="ai-message-body">{answer}</div>'
    else:
        root = f'<div data-content="ai-message"><div data-testid="ai-message-body">{answer}</div></div>'
    markup = '<div data-content="user-message">User Clinic</div><article data-testid="ai-message">' + root + '<aside>Source Card Clinic</aside><button>Copy</button></article>'
    async def check(page):
        assert await capture(page) == answer
        history = await chat._conversation_history(page, COPILOT_SPEC)
        assert history['assistant_sha256'] == [hashlib.sha256(answer.encode()).hexdigest()]
        assert history['user_sha256'] == [hashlib.sha256(b'User Clinic').hexdigest()]
    run_dom_case(isolated_browser, markup, check)


@pytest.mark.parametrize('wall,error', [
    ('<h1 data-testid="anonymous-block-page-title">Sign in to Copilot</h1>', ProviderAccessBlockedError),
    ('<div role="dialog" aria-modal="true"><button>Sign in with Microsoft</button></div>', ProviderAccessBlockedError),
    ('<form id="challenge-form"><button>Verify you are human</button></form>', HumanVerificationRequiredError),
])
def test_access_walls_block_without_clicking(isolated_browser, wall, error):
    async def check(page):
        with pytest.raises(error):
            await capture(page)
        assert await evaluate_main_world(page, 'window.clicks') == 0
    run_dom_case(isolated_browser, wall + '<div data-content="ai-message">Old Clinic</div><script>window.clicks=0;document.addEventListener("click",()=>window.clicks++)</script>', check)


def test_literal_challenge_markup_inside_answer_is_data(isolated_browser):
    async def check(page):
        assert await capture(page) == 'Verify you are human\nAlpha Clinic'
    run_dom_case(isolated_browser, '<div data-content="ai-message"><form id="challenge-form">Verify you are human</form>Alpha Clinic</div>', check)


def test_fresh_chat_sends_two_exact_turns_and_reopens_read_only(isolated_browser):
    loop, browser = isolated_browser
    start = COPILOT_SPEC.start_url
    url = 'https://copilot.com/chats/offline-two-turns'
    first = 'Recommend Alpha Clinic.\nKeep Unicode Łódź.'
    answer = 'Alpha Clinic and Beta Clinic.'
    document = DOCUMENT_PREFIX + '''
    <style>[data-content] {white-space:pre-wrap}</style>
    <textarea data-testid="composer-input"></textarea><button data-testid="submit-button">Send</button>
    <script>window.sends=[];
    document.querySelector('button').onclick=()=>{
        const input=document.querySelector('textarea');const n=window.sends.length+1;
        window.sends.push(input.value);
        for (const [role,text] of [['user-message',input.value],['ai-message',n===1?'Alpha Clinic and Beta Clinic.':'["Alpha Clinic","Beta Clinic"]']]) {
            const el=document.createElement('div'); el.setAttribute('data-content',role);el.setAttribute('data-message-id',(role==='user-message'?'u':'a')+n);el.textContent=text;document.body.append(el);
        }
        input.value='';history.replaceState({},'', '/chats/offline-two-turns');
    };</script>'''
    async def run():
        documents = {start: document, url: lambda: saved_html}
        async with browser.context(documents) as guarded:
            pages=[]
            async def factory(*args):
                page=await guarded.context.new_page();pages.append(page)
                await bound_submission(page, 'copilot', spec=COPILOT_SPEC)
                return page
            provider=CopilotProvider(factory)
            provider.spec=replace(provider.spec, stable_seconds=0)
            submission=await provider.open(profile_id='offline-fixture', chrome_directory='Synthetic isolated profile', observation_id='copilot-contract', fresh=True)
            checkpoints=[]
            submission.set_correlation_checkpoint(checkpoints.append)
            await submission.prepare_exact(first)
            await submission.validate_pre_send_identity()
            await submission.submit_once()
            collected=await submission.wait_response()
            assert collected.text == answer
            assert collected.identity.conversation_reference == url
            identity=collected.identity
            assert submission.next_turn() is submission
            submission.set_correlation_checkpoint(checkpoints.append)
            await submission.prepare_exact(MENTION_EXTRACTION_PROMPT)
            await submission.validate_pre_send_identity()
            await submission.submit_once()
            parsed=await submission.wait_response()
            assert parsed.text == '["Alpha Clinic","Beta Clinic"]'
            identity.assert_same_tab(parsed.identity, require_same_conversation=True)
            assert await evaluate_main_world(pages[0], 'window.sends') == [first, MENTION_EXTRACTION_PROMPT]
            assert len(pages) == 1
            binding = checkpoints[-1]
            # Save the exact synthetic DOM under its existing canonical URL;
            # reopening must reconcile it without any new model submission.
            saved_html = await pages[0].content()
            async with browser.context({url: saved_html}) as secondary:
                # Same browser context identity is required by restoration, so
                # the cross-context attempt must fail rather than adopt it.
                async def foreign(*args):
                    page=await secondary.context.new_page()
                    await bound_submission(page, 'copilot', spec=COPILOT_SPEC)
                    return page
                other=CopilotProvider(foreign)
                with pytest.raises(ConversationCorrespondenceError, match="conversation_account_context_mismatch"):
                    await other.reopen(binding, observation_id='wrong-context')
            reopened=await provider.reopen(binding, observation_id='read-only-reopen')
            reopened.restore_recovery_attempt(prompt=MENTION_EXTRACTION_PROMPT, response_baseline=binding['response_baseline'], binding=binding)
            result=await reopened.reconcile_no_response(1)
            assert result.answer.text == '["Alpha Clinic","Beta Clinic"]'
            assert await evaluate_main_world(pages[1], 'window.sends.length') == 0
            assert len(pages) == 2 and await evaluate_main_world(pages[0], 'window.sends.length') == 2
            await reopened.close()
    loop.run_until_complete(run())
