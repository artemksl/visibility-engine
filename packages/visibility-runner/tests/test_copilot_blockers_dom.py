"""Synthetic Copilot/login documents in isolated, network-denied Chrome."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from automation_core.browser.async_actions import AsyncActionError
from automation_core.contracts import DispatchReceipt

from support.dom import DOCUMENT_PREFIX, bound_submission as _bound_submission, isolated_browser, run_dom_case
from visibility_runner.browser.copilot_blockers import CopilotBlockerService
from visibility_runner.errors import ProviderAccessBlockedError
from visibility_runner.websites import chat_actions as chat
from visibility_runner.websites.copilot import COPILOT_SPEC, CopilotProvider

pytestmark = pytest.mark.local_browser
SPEC = replace(COPILOT_SPEC, composer_typing_min_delay_ms=0, composer_typing_max_delay_ms=0,
    response_timeout_seconds=4, stable_seconds=0)
ROOT = "https://copilot.com/"
CHAT = "https://copilot.com/chat"
AUTH = "https://login.live.com/oauth20_authorize.srf"
GOOGLE = "https://accounts.google.com/o/oauth2/auth"
STAY = "https://login.live.com/HandleGoogleResponse.srf"
CONVERSATION = "https://copilot.com/chat/conversation/synthetic-owned"
EMAIL = "expected@example.test"
PROMPT = "Synthetic recommendation request."
WELCOME = '<h1>Welcome to Copilot</h1><button onclick="location.href=\'' + AUTH + '\'">Sign in</button>'
ACCOUNT = '''<button aria-label="Fixture Person, Personal account"
    onclick="document.querySelector('#account').hidden=false">Account</button>
<section id="account" hidden><button aria-label="Sign out from expected@example.test">Sign out</button></section>
<script>document.addEventListener('keydown',e=>{if(e.key==='Escape')document.querySelector('#account').hidden=true;});</script>'''
CONTROLS = '''<textarea data-testid="composer-input"></textarea>
<button data-testid="submit-button" onclick="window.sends++">Send</button>
<script>window.sends=0;</script>'''


async def bound_submission(page, platform, **kwargs):
    return await _bound_submission(page, platform, profile_id='offline_fixture', **kwargs)


class Verification:
    def __init__(self, holds=None, on_clear=None):
        self.calls, self.holds, self.on_clear = 0, holds, on_clear
    async def __call__(self, submission, deadline):
        self.calls += 1
        if self.holds is not None:
            await self.holds.begin(submission, deadline)
        await submission.page.locator('h2:text-is("Verification required")').evaluate('e=>e.remove()')
        if self.on_clear is not None:
            await self.on_clear(submission.page)
    async def validated(self, *args):
        if self.holds is not None: await self.holds.validated(*args)
    def failed(self, *args):
        if self.holds is not None: self.holds.failed(*args)
    async def poll_commands(self, *args):
        if self.holds is not None: await self.holds.poll_commands(*args)


def service():
    return CopilotBlockerService(Verification(), accounts={"offline_fixture": EMAIL})


def held_service(tmp_path, submission, *, on_clear=None, admitted=False, effect_action="submission"):
    from visibility_runner.browser.profile_matching import BrowserProfile
    from visibility_runner.browser.profile_access import ProfileReservationManager
    from visibility_runner.browser.tabs import OwnedTab, OwnedTabRegistry, TabSessionIdentity
    from visibility_runner.browser.verification_holds import VerificationHoldStore, CopilotVerificationHolds
    profile = BrowserProfile(id=submission.profile_id, display_name='Fixture', chrome_directory=submission.chrome_directory)
    store = VerificationHoldStore(ProfileReservationManager(tmp_path/'leases', namespace='offline_fixture'),
        (profile,), run_id='synthetic', plan_sha256='ab'*32)
    registry = OwnedTabRegistry()
    registry.add(OwnedTab(submission.page, TabSessionIdentity(**submission.identity.to_mapping(), provider='copilot'), ROOT))
    attachment = SimpleNamespace(attached_health=SimpleNamespace(browser_session_id='fixture-generation'),
        session=SimpleNamespace(registry=registry), config=SimpleNamespace(browser=SimpleNamespace(background_only=True)))
    def events(kind, **kwargs):
        if admitted and kind == 'attempt_started':
            return [dict(payload=dict(pass_id='fixture-pass', attempt_id='fixture-attempt', operation_id='fixture-operation'))]
        return [dict(payload={'action': effect_action})] if admitted and kind == 'effect_admitted' else []
    recovery = SimpleNamespace(owner=SimpleNamespace(execution_id='fixture', assert_owned=lambda:None),
        record_resource=lambda *args, **kwargs:None, pass_id='fixture-pass', attempts={}, finished=set(), failed=set(),
        coordinator=SimpleNamespace(ledger=SimpleNamespace(select=events)))
    holds = CopilotVerificationHolds(store, attachment, recovery, None, cancelled=lambda:False)
    return CopilotBlockerService(Verification(holds, on_clear), holds=holds, accounts={submission.profile_id:EMAIL}), store


def documents(*, root=WELCOME, conversation=None, wrong_account=False, stuck=False):
    wrong = '<div><span>wrong@example.test</span><button onclick="location.href=\'https://wrong.invalid/\'">Other Person</button></div>'
    choose = '<h1>Choose an account</h1>' + wrong + '<div><span>' + EMAIL + '</span>'
    action = 'window.accountClicks=(window.accountClicks||0)+1;'
    if not stuck:
        action += "document.querySelector('h1').textContent='Sign in on Google';this.parentElement.hidden=true;document.querySelector('#next').hidden=false;"
    choose += '<button onclick="' + action + '">Fixture Person</button>'
    choose += '<button aria-haspopup="menu" aria-label="Open menu for Fixture Person">Menu</button></div>'
    choose += '<button id="next" hidden onclick="location.href=\'' + GOOGLE + '\'">Next</button>'
    if wrong_account:
        choose = '<h1>Choose an account</h1>' + wrong
    google = '<div data-identifier="' + EMAIL + '" role="button" onclick="location.href=\'' + STAY + '\'">Expected Google account</div>'
    stay = '<h1>Stay signed in?</h1><span>' + EMAIL + '</span><button onclick="location.href=\'' + CHAT + '\'">No</button><button onclick="location.href=\'https://yes.invalid/\'">Yes</button>'
    return {url: DOCUMENT_PREFIX + markup for url, markup in {
        ROOT: root, AUTH: choose, GOOGLE: google, STAY: stay,
        CHAT: ACCOUNT + CONTROLS,
        CONVERSATION: conversation or ACCOUNT + CONTROLS,
    }.items()}


@pytest.mark.parametrize("markup, marker", [
    ('<button>Sign in</button>', 'authentication_required'),
    ('<div role="dialog"><button>Sign in with Microsoft</button></div>', 'authentication_required'),
    ('<div role="dialog"><h2>Verification required</h2><button>Sign in</button></div>', 'human_verification_required'),
    ('<dialog open>Unfamiliar blocking window<button>Continue</button></dialog>', 'unknown_dialog'),
    ('<div style="position:fixed;inset:0;z-index:999;background:white">Opaque overlay</div>', 'unknown_overlay'),
    ('<div role="dialog" aria-label="Cookies"><p>We use optional cookies to improve your experience.</p><button>Reject</button></div>', 'cookie_notice'),
])
def test_recognition_classifies_visible_blockers(isolated_browser, markup, marker):
    async def check(page):
        assert await chat._visible_access_block_marker(page, SPEC) == marker
    run_dom_case(isolated_browser, markup + CONTROLS, check)


@pytest.mark.parametrize("markup", [
    '<article data-testid="markdown-reply"><div role="dialog"><button>Sign in</button></div></article>',
    '<div data-testid="chatOutput"><dialog open>Verification required</dialog></div>',
    '<div role="dialog" hidden><button>Sign in</button></div>',
    '<p>A guide about signing in, verification and popup dialogs.</p>',
])
def test_conversation_quotes_hidden_surfaces_and_prose_are_clear(isolated_browser, markup):
    async def check(page):
        assert await chat._visible_access_block_marker(page, SPEC) is None
    run_dom_case(isolated_browser, markup + CONTROLS, check)


@pytest.mark.parametrize('case, marker', [
    ('tooltip', None),
    ('extra_modal_text', 'unknown_dialog'),
    ('tooltip_overlay', 'unknown_overlay'),
    ('security_dialog', 'human_verification_required'),
])
def test_tooltip_modal_requires_all_content_and_unobstructed_composer(isolated_browser, case, marker):
    tooltip = '<div role="tooltip">Privacy information <button role="link">Learn more</button></div>'
    extra = '<p>Confirm this unfamiliar action</p>' if case == 'extra_modal_text' else ''
    style = ' style="position:fixed;inset:0;z-index:999;background:white"' if case == 'tooltip_overlay' else ''
    markup = '<div role="dialog" aria-modal="true" aria-hidden="true"' + style + '>' + tooltip + extra + '</div>'
    if case == 'security_dialog':
        markup += '<div role="dialog" aria-modal="true" aria-label="Security check required"><span>Verification required</span><div></div></div>'
    async def check(page):
        assert await chat._visible_access_block_marker(page, SPEC) == marker
    run_dom_case(isolated_browser, markup + CONTROLS, check)


def test_saved_account_flow_runs_before_fresh_chat_binding(isolated_browser):
    loop, browser = isolated_browser
    async def run():
        async with browser.context(documents()) as guarded:
            page = await guarded.context.new_page()
            await bound_submission(page, 'copilot', spec=SPEC)
            async def factory(*args): return page
            handler = service()
            provider = CopilotProvider(factory, human_verification_handler=handler)
            provider.spec = SPEC
            submission = await provider.open(profile_id='offline_fixture', chrome_directory='Synthetic isolated profile', observation_id='synthetic')
            await submission.prepare_exact(PROMPT)
            assert page.url == CHAT
            assert await page.locator('textarea').input_value() == PROMPT
            assert await page.evaluate('window.sends') == 0
            assert handler.verification.calls == 0
            assert guarded.fulfilled == [ROOT, AUTH, GOOGLE, STAY, CHAT]
    loop.run_until_complete(run())


@pytest.mark.parametrize('outcome', [
    'self_clear', 'late_empty_clear', 'late_sign_in_clear', 'still_blocked', 'entered', 'unknown',
])
def test_sign_in_preinput_refusal_waits_for_existing_episode_clearance(isolated_browser, tmp_path, outcome):
    loop, browser = isolated_browser
    async def run():
        transition_url = ROOT + '?es=UnauthClick'
        fixtures = documents(root=WELCOME+CONTROLS)
        fixtures[transition_url] = DOCUMENT_PREFIX + '<title>Microsoft Copilot</title>'
        async with browser.context(fixtures) as guarded:
            page = await guarded.context.new_page()
            await page.goto(ROOT)
            submission = await bound_submission(page, 'copilot', spec=SPEC)
            handler, store = held_service(tmp_path, submission)
            submission.human_verification_handler = handler
            audit, clicks, hydration = [], [], None
            handler.holds.recovery.record_resource = lambda *args, **kwargs: audit.append(
                kwargs['reference']['verification_hold'])
            original_click = submission._actions.click
            reason = 'browser_call_failed' if outcome == 'entered' else 'binding_changed'
            phase = 'dispatch' if outcome == 'entered' else 'preparation'
            receipt = DispatchReceipt('entered' if outcome == 'entered' else 'not_attempted',
                reason=reason, phase=phase, entered_at=1.0 if outcome == 'entered' else None)

            async def refused(target, until, verify):
                nonlocal hydration
                label = await target.locator.inner_text()
                clicks.append(label)
                if label == 'Sign in':
                    if outcome.startswith('late_'):
                        await page.goto(transition_url if outcome == 'late_empty_clear' else ROOT)
                        async def finish_hydration():
                            await asyncio.sleep(.3)
                            await page.goto(CHAT)
                        hydration = asyncio.create_task(finish_hydration())
                    elif outcome == 'unknown':
                        await page.evaluate('html=>document.body.innerHTML=html',
                            '<dialog open>Unknown blocking step<button>Continue</button></dialog>' + CONTROLS)
                    elif outcome != 'still_blocked':
                        await page.goto(CHAT)  # Synthetic saved-session hydration, no sign-in input.
                    raise AsyncActionError(reason, receipt)
                return await original_click(target, until, verify)

            submission._actions.click = refused
            deadline = chat._BrowserOperationDeadline.after(
                1 if outcome == 'still_blocked' else 4, timeout_message='fixture deadline')
            if outcome == 'self_clear' or outcome.startswith('late_'):
                await handler.resolve(submission, 'authentication_required', deadline)
                if hydration is not None:
                    await hydration
                assert store.rows() == ()
                row, = store.rows(retained_only=False)
                assert row['status'] == 'resumed' and row['verification_cleared'] is True
                assert clicks == ['Sign in', 'Account']  # The exact frozen account was verified.
            elif outcome == 'still_blocked':
                with pytest.raises((ProviderAccessBlockedError, TimeoutError)):
                    await handler.resolve(submission, 'authentication_required', deadline)
                row, = store.rows()
                assert row['status'] == 'expired_held' and row['retained'] is True
                assert row['reason'] == 'copilot_blocker_deadline_expired'
                assert clicks == ['Sign in']
            else:
                with pytest.raises(AsyncActionError) as caught:
                    await handler.resolve(submission, 'authentication_required', deadline)
                assert caught.value.receipt is receipt
                row, = store.rows()
                assert row['status'] == 'unsupported_held' and row['retained'] is True
                assert clicks == ['Sign in']
            diagnostic = 'copilot_sign_in_action_' + reason + '_' + phase
            assert any(event['reason'] == diagnostic for event in audit)
            if outcome in {'entered', 'unknown'}:
                assert row['reason'] == diagnostic
            assert row['deadline_monotonic'] == deadline.expires_at
            assert await page.evaluate('window.sends') == 0
            assert not submission._submitted and handler.verification.calls == 0
    loop.run_until_complete(run())


def test_sign_in_appearing_after_preparation_restores_prompt_before_one_send(isolated_browser):
    loop, browser = isolated_browser
    async def run():
        async with browser.context(documents(root=ACCOUNT+CONTROLS)) as guarded:
            page = await guarded.context.new_page()
            await page.goto(ROOT)
            submission = await bound_submission(page, 'copilot', spec=SPEC)
            submission._require_fresh_blank_start = True
            submission.human_verification_handler = service()
            await submission.prepare_exact(PROMPT)
            await page.evaluate('html=>document.body.innerHTML=html', WELCOME)
            await submission.validate_pre_send_identity()
            assert await page.locator('textarea').input_value() == PROMPT
            assert await page.evaluate('window.sends') == 0
            await submission.submit_once()
            assert await page.evaluate('window.sends') == 1
    loop.run_until_complete(run())


def test_sign_in_after_admitted_navigation_clears_hold_and_prepares_unsent_prompt(isolated_browser, tmp_path):
    loop, browser = isolated_browser
    async def run():
        async with browser.context(documents()) as guarded:
            page = await guarded.context.new_page()
            await page.goto(ROOT)
            submission = await bound_submission(page, 'copilot', spec=SPEC)
            submission._require_fresh_blank_start = True
            handler, store = held_service(tmp_path, submission, admitted=True, effect_action='navigate')
            submission.human_verification_handler = handler
            await submission.prepare_exact(PROMPT)
            row, = store.rows(retained_only=False)
            assert row['dispatch_state'] == 'not_admitted' and row['status'] == 'resumed'
            assert store.rows() == () and row['verification_cleared'] is True
            assert page.url == CHAT and await page.locator('textarea').input_value() == PROMPT
            assert await page.evaluate('window.sends') == 0 and not submission._submitted
            assert handler.verification.calls == 0
    loop.run_until_complete(run())


def test_sign_in_does_not_overwrite_an_operator_draft(isolated_browser):
    loop, browser = isolated_browser
    async def run():
        fixtures = documents(root=ACCOUNT+CONTROLS)
        fixtures[CHAT] += '<script>document.querySelector("textarea").value="Operator draft";</script>'
        async with browser.context(fixtures) as guarded:
            page = await guarded.context.new_page()
            await page.goto(ROOT)
            submission = await bound_submission(page, 'copilot', spec=SPEC)
            submission.human_verification_handler = service()
            await submission.prepare_exact(PROMPT)
            await page.evaluate('html=>document.body.innerHTML=html', WELCOME)
            with pytest.raises(ProviderAccessBlockedError, match='prepared_prompt_changed'):
                await submission.validate_pre_send_identity()
            assert await page.locator('textarea').input_value() == 'Operator draft'
            assert await page.evaluate('window.sends') == 0 and not submission._submitted
    loop.run_until_complete(run())


def test_sign_in_during_response_reopens_proven_conversation_without_resend(isolated_browser):
    loop, browser = isolated_browser
    history = f'<div data-testid="chatQuestion"><div data-testid="chatOutput">{PROMPT}</div></div><div data-testid="markdown-reply" data-message-id="synthetic-answer">Synthetic answer.</div>'
    async def run():
        async with browser.context(documents(root=ACCOUNT+CONTROLS, conversation=ACCOUNT+CONTROLS+history)) as guarded:
            page = await guarded.context.new_page()
            await page.goto(ROOT)
            submission = await bound_submission(page, 'copilot', spec=SPEC)
            submission.human_verification_handler = service()
            await submission.prepare_exact(PROMPT)
            await submission.submit_once()
            await page.goto(CONVERSATION)
            submission._correlation_checkpoint = lambda value: None
            await submission._checkpoint_conversation()
            assert submission._last_correlation_value is not None
            await page.goto(ROOT)
            await page.evaluate('html=>document.body.innerHTML=html', WELCOME)
            answer = await submission.wait_response()
            assert answer.text == 'Synthetic answer.'
            assert page.url == CONVERSATION
            assert submission._submitted and await page.evaluate('window.sends') == 0
            with pytest.raises(Exception, match='only once'):
                await submission.submit_once()
    loop.run_until_complete(run())


def test_verification_transitioning_to_sign_in_resumes_one_durable_episode(isolated_browser, tmp_path):
    loop, browser = isolated_browser
    async def run():
        async with browser.context(documents(root=ACCOUNT+CONTROLS)) as guarded:
            page = await guarded.context.new_page()
            await page.goto(ROOT)
            submission = await bound_submission(page, 'copilot', spec=SPEC)
            submission._require_fresh_blank_start = True
            async def sign_in(page):
                await page.evaluate('html=>document.body.innerHTML=html', WELCOME)
            handler, store = held_service(tmp_path, submission, on_clear=sign_in)
            submission.human_verification_handler = handler
            await submission.prepare_exact(PROMPT)
            await page.evaluate('''()=>{let h=document.createElement('h2');h.textContent='Verification required';document.body.append(h)}''')
            await submission.validate_pre_send_identity()
            assert len(store.rows(retained_only=False)) == 1
            row = store.rows(retained_only=False)[0]
            assert row['status'] == 'resumed' and not row['retained'] and row['blocker_kind'] == 'sign_in'
            assert handler.verification.calls == 1
            assert page.url == CHAT and await page.locator('textarea').input_value() == PROMPT
            assert await page.evaluate('window.sends') == 0 and not submission._submitted
    loop.run_until_complete(run())


def test_authentication_on_reopen_proves_history_without_authorizing_a_send(isolated_browser, tmp_path):
    loop, browser = isolated_browser
    history = f'<div data-testid="chatQuestion"><div data-testid="chatOutput">{PROMPT}</div></div><div data-testid="markdown-reply" data-message-id="fixture-answer">Synthetic answer.</div>'
    async def run():
        async with browser.context(documents(root=ACCOUNT+CONTROLS, conversation=ACCOUNT+CONTROLS+history)) as guarded:
            old = await guarded.context.new_page()
            await old.goto(ROOT)
            original = await bound_submission(old, 'copilot', spec=SPEC)
            await original.prepare_exact(PROMPT)
            await original.submit_once()
            await old.goto(CONVERSATION)
            original._correlation_checkpoint = lambda value:None
            await original._checkpoint_conversation()
            binding = original._last_correlation_value
            pages, stores = [], []
            async def factory(*args):
                page = await guarded.context.new_page()
                temporary = await bound_submission(page, 'copilot', spec=SPEC)
                handler, store = held_service(tmp_path, temporary, admitted=True)
                provider.human_verification_handler = handler
                pages.append(page); stores.append(store)
                # The first saved-conversation load is obstructed; later reads
                # must return to that exact conversation after account proof.
                script = '''document.addEventListener('DOMContentLoaded',()=>{
                    if(location.href.includes('/chat/conversation/') && !sessionStorage.getItem('fixture-login')) {
                        sessionStorage.setItem('fixture-login','1');document.body.innerHTML=FIXTURE_WELCOME;
                    }
                })'''
                await page.add_init_script(script.replace('FIXTURE_WELCOME', json.dumps(WELCOME)))
                return page
            provider = CopilotProvider(factory)
            provider.spec = SPEC
            reopened = await provider.reopen(binding, observation_id='synthetic-reopen')
            assert reopened.identity.conversation_reference == CONVERSATION
            assert not reopened._submitted and not reopened._prepared
            row = stores[0].rows(retained_only=False)[0]
            assert row['dispatch_state'] == 'admitted_or_unknown' and row['status'] == 'resumed'
            reopened.restore_recovery_attempt(prompt=PROMPT, response_baseline=binding['response_baseline'], binding=binding)
            assert reopened._submitted
            assert await pages[0].evaluate('window.sends') == 0
            with pytest.raises(chat.BrowserInteractionError):
                await reopened.submit_once()
    loop.run_until_complete(run())


def test_verification_uses_existing_handler_and_keeps_prepared_prompt(isolated_browser):
    async def check(page):
        submission = await bound_submission(page, 'copilot', spec=SPEC)
        handler = submission.human_verification_handler = service()
        await submission.prepare_exact(PROMPT)
        await page.evaluate('''()=>{const h=document.createElement('h2');h.textContent='Verification required';document.body.append(h)}''')
        await submission.validate_pre_send_identity()
        assert handler.verification.calls == 1
        assert await page.locator('textarea').input_value() == PROMPT
        assert await page.evaluate('window.sends') == 0
    run_dom_case(isolated_browser, CONTROLS, check)


def test_cookie_notice_is_rejected_without_sending_or_native_input(isolated_browser):
    notice = '<div role="dialog" aria-label="Cookies"><p>We use optional cookies to improve your experience.</p><button onclick="this.parentElement.remove()">Reject</button></div>'
    async def check(page):
        submission = await bound_submission(page, 'copilot', spec=SPEC)
        handler = submission.human_verification_handler = service()
        await submission.prepare_exact(PROMPT)
        assert await chat._visible_access_block_marker(page, SPEC) is None
        assert await page.evaluate('window.sends') == 0 and handler.verification.calls == 0
    run_dom_case(isolated_browser, notice + CONTROLS, check)


def test_sign_in_interrupting_typing_reprepares_under_original_budget(isolated_browser):
    loop, browser = isolated_browser
    async def run():
        async with browser.context(documents(root=ACCOUNT+CONTROLS)) as guarded:
            page = await guarded.context.new_page()
            await page.goto(ROOT)
            page.set_default_timeout(250)
            submission = await bound_submission(page, 'copilot', spec=SPEC)
            submission._require_fresh_blank_start = True
            submission.human_verification_handler = service()
            await page.evaluate('html=>document.querySelector("textarea").addEventListener("input",()=>{document.body.innerHTML=html},{once:true})', WELCOME)
            await submission.prepare_exact(PROMPT)
            assert await page.locator('textarea').input_value() == PROMPT
            assert await page.evaluate('window.sends') == 0
            assert submission._prepared and not submission._submitted
    loop.run_until_complete(run())


@pytest.mark.parametrize('wrong_account, stuck', [(True, False), (False, True)])
def test_wrong_account_and_stuck_step_expire_without_fallback_or_repeat(isolated_browser, wrong_account, stuck):
    loop, browser = isolated_browser
    async def run():
        async with browser.context(documents(wrong_account=wrong_account, stuck=stuck)) as guarded:
            page = await guarded.context.new_page()
            await page.goto(ROOT)
            submission = await bound_submission(page, 'copilot', spec=SPEC)
            handler = submission.human_verification_handler = service()
            with pytest.raises((ProviderAccessBlockedError, TimeoutError)):
                await handler.resolve(submission, 'authentication_required',
                    chat._BrowserOperationDeadline.after(1, timeout_message='fixture deadline'))
            assert guarded.fulfilled == [ROOT, AUTH]
            assert not submission._submitted and handler.verification.calls == 0
            if stuck:
                assert await page.evaluate('window.accountClicks') == 1
    loop.run_until_complete(run())


def test_native_browser_alert_is_recognized_and_never_accepted(isolated_browser):
    async def check(page):
        submission = await bound_submission(page, 'copilot', spec=SPEC)
        handler = submission.human_verification_handler = service()
        await handler.watch_page(page, chat._BrowserOperationDeadline.after(2, timeout_message='fixture'))
        task = asyncio.create_task(page.evaluate('()=>confirm("Synthetic consequential prompt")'))
        while getattr(page, '_visibility_runner_blocking_dialog', None) is None:
            await asyncio.sleep(.01)
        assert await chat._visible_access_block_marker(page, SPEC) == 'browser_dialog'
        await page._visibility_runner_blocking_dialog.dismiss()  # Fixture cleanup, not production handling.
        assert await task is False
        await asyncio.sleep(.05)
        assert getattr(page, '_visibility_runner_blocking_dialog', None) is None
        assert await chat._visible_access_block_marker(page, SPEC) is None
    run_dom_case(isolated_browser, CONTROLS, check)
