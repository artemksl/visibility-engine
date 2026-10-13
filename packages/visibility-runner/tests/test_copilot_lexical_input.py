"""Synthetic regressions for the observed Copilot Lexical editor structure.

Only structural facts from inspection are represented here. Fixture text is
invented; the guarded browser has no account, saved profile or provider access.
"""
from __future__ import annotations

from dataclasses import replace
from html import escape
import json

import pytest

from support.dom import evaluate_main_world, DOCUMENT_PREFIX, bound_submission, isolated_browser, run_dom_case
from visibility_runner.browser.input import (
    ComposerReadbackMismatch,
    ComposerReadbackUnavailable,
    read_copilot_composer_value,
    read_typed_composer_value,
    write_exact_composer_input,
)
from visibility_runner.names.prompts import same_chat_extraction_prompt
from visibility_runner.websites.copilot import COPILOT_SPEC, CopilotProvider


pytestmark = pytest.mark.local_browser

EDITOR_ATTRIBUTES = (
    'id="m365-chat-editor-target-element" role="textbox" '
    'aria-label="Message Copilot" contenteditable="true" data-lexical-editor="true"'
)
SENTINEL = '<span aria-hidden="true" data-lexical-text="true">\u200b\u200c</span>'


def text_span(text: str, *, hidden: bool = False) -> str:
    return (
        '<span data-lexical-text="true"' + (' aria-hidden="true"' if hidden else '') +
        '>' + escape(text) + '</span>'
    )


def paragraph(text: str) -> str:
    return '<p>' + (text_span(text) + SENTINEL if text else '<br>') + '</p>'


def editor(contents: str, attributes: str = EDITOR_ATTRIBUTES, tag: str = 'span') -> str:
    return f'<{tag} {attributes}>{contents}</{tag}>'


@pytest.mark.parametrize('prompt', [
    '', 'A short recommendation prompt.', '  Café 🦷 e\u0301\t  ',
    '\n', '\n\n', 'first\nsecond', 'first\n\nlast', '\nfirst\n\n',
    'keep\u200b\u200cthese\u200b\u200c', '  one  \n\t two  \n',
])
def test_known_lexical_structure_preserves_exact_prompt(isolated_browser, prompt):
    markup = editor(''.join(paragraph(line) for line in prompt.split('\n')))
    async def check(page):
        target = page.locator('#m365-chat-editor-target-element')
        assert await read_copilot_composer_value(target) == prompt
        # This reproduces why the existing generic reader cannot prove the
        # current editor: it includes the marker or refuses multiple blocks.
        if prompt:
            assert await read_typed_composer_value(target) != prompt
    run_dom_case(isolated_browser, markup, check)


def test_initial_hidden_break_is_empty_text(isolated_browser):
    async def check(page):
        assert await read_copilot_composer_value(page.locator('[contenteditable]')) == ''
    run_dom_case(isolated_browser, editor('<p><br aria-hidden="true"></p>'), check)


@pytest.mark.parametrize('contents,expected', [
    (text_span('first') + text_span(' second') + SENTINEL, 'first second'),
    (text_span('expected') + text_span('EXTRA', hidden=True) + SENTINEL, 'expectedEXTRA'),
    (text_span('expected') + text_span('\u200b', hidden=True), 'expected\u200b'),
    (text_span('expected') + text_span('\u200b\u200cEXTRA', hidden=True), 'expected\u200b\u200cEXTRA'),
    (text_span('expected') + text_span('\u200c\u200b', hidden=True), 'expected\u200c\u200b'),
    (text_span('expected') + text_span('\u200b\u200c'), 'expected\u200b\u200c'),
    (text_span('expected') + SENTINEL + text_span('more'), 'expected\u200b\u200cmore'),
    (text_span('expected') + SENTINEL + SENTINEL, 'expected\u200b\u200c'),
])
def test_only_exact_terminal_hidden_marker_is_excluded(isolated_browser, contents, expected):
    async def check(page):
        assert await read_copilot_composer_value(page.locator('[contenteditable]')) == expected
    run_dom_case(isolated_browser, editor('<p>' + contents + '</p>'), check)


@pytest.mark.parametrize('contents', [
    '', '<p></p>', '<div>' + text_span('expected') + SENTINEL + '</div>',
    '<p>expected' + SENTINEL + '</p>',
    '<p>' + text_span('expected') + '<span hidden>EXTRA</span>' + SENTINEL + '</p>',
    '<p><span data-lexical-text="true"><b>expected</b></span>' + SENTINEL + '</p>',
    '<p>' + text_span('expected') + '<br>' + SENTINEL + '</p>',
    '<p><br><br></p>',
    '<p>' + text_span('expected') + SENTINEL + '</p>EXTRA',
])
def test_unrecognized_lexical_structure_has_no_provable_readback(isolated_browser, contents):
    async def check(page):
        assert await read_copilot_composer_value(page.locator('[contenteditable]')) is None
    run_dom_case(isolated_browser, editor(contents), check)


@pytest.mark.parametrize('attributes,tag', [
    (EDITOR_ATTRIBUTES.replace('m365-chat-editor-target-element', 'another-editor'), 'span'),
    (EDITOR_ATTRIBUTES.replace('data-lexical-editor="true"', 'data-lexical-editor="false"'), 'span'),
    (EDITOR_ATTRIBUTES.replace(' data-lexical-editor="true"', ''), 'span'),
    (EDITOR_ATTRIBUTES.replace('role="textbox"', 'role="combobox"'), 'span'),
    (EDITOR_ATTRIBUTES.replace('contenteditable="true"', 'contenteditable="false"'), 'span'),
    (EDITOR_ATTRIBUTES.replace('Message Copilot', 'Different input'), 'span'),
    (EDITOR_ATTRIBUTES, 'div'),
])
def test_editor_marker_exception_never_applies_to_other_editor_identity(isolated_browser, attributes, tag):
    async def check(page):
        target = page.locator('[contenteditable]')
        assert await read_copilot_composer_value(target) == 'expected\u200b\u200c'
    run_dom_case(isolated_browser, editor(paragraph('expected'), attributes, tag), check)


@pytest.mark.parametrize('markup,prompt', [
    ('<textarea>  old editor\nnext  </textarea>', '  old editor\nnext  '),
    ('<div contenteditable="true">old<br>editor</div>', 'old\neditor'),
])
def test_older_composer_retains_generic_exact_readback(isolated_browser, markup, prompt):
    async def check(page):
        assert await read_copilot_composer_value(page.locator('textarea, [contenteditable]')) == prompt
    run_dom_case(isolated_browser, markup, check)


@pytest.mark.parametrize('extra,error,observed', [
    (text_span('EXTRA', hidden=True), ComposerReadbackMismatch, 'expectedEXTRA'),
    ('<span hidden>EXTRA</span>', ComposerReadbackUnavailable, None),
    (text_span('\u200b', hidden=True), ComposerReadbackMismatch, 'expected\u200b'),
])
def test_corruption_cannot_receive_exact_pre_send_receipt(isolated_browser, extra, error, observed):
    async def check(page):
        target = page.locator('[contenteditable]')
        async def clear(_target):
            await _target.evaluate("el => el.innerHTML = '<p><br></p>'")
        async def write(_target, supplied):
            markup = '<p>' + text_span(supplied) + extra + SENTINEL + '</p>'
            await _target.evaluate('(el, markup) => el.innerHTML = markup', markup)
        with pytest.raises(error) as caught:
            await write_exact_composer_input(
                target, 'expected', max_attempts=1,
                clear=clear, write=write, read=read_copilot_composer_value,
            )
        assert caught.value.failure.observed_text == observed
        assert not caught.value.failure.send_started
    run_dom_case(isolated_browser, editor('<p><br></p>'), check)


def test_two_turn_pipeline_types_and_sends_exact_lexical_text(isolated_browser):
    loop, browser = isolated_browser
    first = 'Recommend Alpha Clinic.\nKeep Unicode Łódź and \u200b\u200c.\n'
    source_answer = 'Alpha Clinic and Beta Clinic.\n' + (
        'Synthetic captured answer details for the long parsing draft.\n' * 42
    )
    extraction_prompt = same_chat_extraction_prompt(source_answer)
    document = DOCUMENT_PREFIX + editor('<p><br aria-hidden="true"></p>') + '''
    <button data-testid="submit-button">Send</button><script>
    window.model='';window.sends=[];window.pastes=0;window.keyEvents=[];
    history.replaceState({},'', '/chat');
    const input=document.querySelector('[contenteditable]');
    function render(){
        input.replaceChildren();
        for(const line of window.model.split('\\n')){
            const p=document.createElement('p');
            if(line){
                const text=document.createElement('span');
                text.dataset.lexicalText='true';text.textContent=line;p.append(text);
                const marker=document.createElement('span');
                marker.dataset.lexicalText='true';marker.setAttribute('aria-hidden','true');
                marker.textContent='\\u200b\\u200c';p.append(marker);
            }else p.append(document.createElement('br'));
            input.append(p);
        }
        const p=input.lastChild;const range=document.createRange();
        if(p.firstChild.nodeName==='SPAN')range.setStart(p.firstChild.firstChild,p.firstChild.textContent.length);
        else range.setStart(p,0);
        range.collapse(true);const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
    }
    input.addEventListener('keydown',event=>{
        window.keyEvents.push({key:event.key,shift:event.shiftKey,trusted:event.isTrusted});
        if(event.key==='Backspace'){event.preventDefault();window.model='';render();}
        if(event.key==='Enter'){
            event.preventDefault();
            if(event.shiftKey){window.model+='\\n';render();}
            else window.sends.push('UNEXPECTED ENTER');
        }
    });
    input.addEventListener('beforeinput',event=>{
        if(event.inputType==='insertText'){
            event.preventDefault();window.model+=event.data;render();
        }
    });
    input.addEventListener('paste',()=>window.pastes++);
    document.querySelector('button').onclick=()=>{
        const n=window.sends.length+1;window.sends.push(window.model);
        for(const [role,text] of [['user-message',window.model],['ai-message',n===1?__SOURCE_ANSWER__:'["Alpha Clinic","Beta Clinic"]']]){
            const node=document.createElement('div');node.setAttribute('data-content',role);
            node.setAttribute('data-message-id',(role==='user-message'?'u':'a')+n);
            node.style.whiteSpace='pre-wrap';
            node.textContent=text;document.body.append(node);
        }
        window.model='';render();history.replaceState({},'', '/chats/offline-lexical');
    };
    </script>'''
    document = document.replace('__SOURCE_ANSWER__', json.dumps(source_answer))
    async def run():
        async with browser.context({COPILOT_SPEC.start_url: document}) as guarded:
            pages = []
            async def factory(*_args):
                page = await guarded.context.new_page()
                pages.append(page)
                await bound_submission(page, 'copilot', spec=COPILOT_SPEC)
                return page
            provider = CopilotProvider(factory)
            provider.spec = replace(provider.spec, stable_seconds=0)
            submission = await provider.open(
                profile_id='offline-fixture', chrome_directory='Synthetic isolated profile',
                observation_id='copilot-lexical-two-turn', fresh=True,
            )
            for index, prompt in enumerate((first, extraction_prompt)):
                if index:
                    assert submission.next_turn() is submission
                await submission.prepare_exact(prompt)
                assert await evaluate_main_world(pages[0], 'window.sends.length') == index
                assert await read_copilot_composer_value(pages[0].locator('[contenteditable]')) == prompt
                await submission.validate_pre_send_identity()
                await submission.submit_once()
                response = await submission.wait_response()
                assert response.text == (source_answer if not index else '["Alpha Clinic","Beta Clinic"]')
            assert len(pages) == 1
            assert await evaluate_main_world(pages[0], 'window.sends') == [first, extraction_prompt]
            assert await evaluate_main_world(pages[0], 'window.pastes') == 0
            keys = await evaluate_main_world(pages[0], 'window.keyEvents')
            assert keys and all(event['trusted'] for event in keys)
            assert all(event['shift'] for event in keys if event['key'] == 'Enter')
            await submission.close()
    loop.run_until_complete(run())
