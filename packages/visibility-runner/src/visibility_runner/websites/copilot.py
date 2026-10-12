"""Microsoft Copilot website driver using the shared two-turn browser transport.

Composer selectors and Lexical readback follow the Microsoft editor observed
on 2026-09-27. Message selectors follow the copilot.com conversation page read
on 2026-09-28 (host ``tmp/copilot-verification-inspection-20260928/
copilot-chat-dom-a79ffe56.json``): one ``[data-testid="chatQuestion"]`` per
user message with the text in ``[data-testid="chatOutput"]``, and one
``[data-testid="markdown-reply"]`` (carrying ``data-message-id``) per answer.
The older ``data-content`` / ``ai-message`` names of the previous site version
are kept for the offline synthetic cases; they match nothing on the live site.
Conversation addresses are ``https://copilot.com/chat/conversation/<id>``;
only registered conversation paths are accepted.
"""
from typing import Callable, Optional

from ..config import COPILOT_BROWSER_TURN_TIMEOUT_SECONDS
from .chat_actions import PageProvider
from .contracts import PageFactory, PageProviderSpec


# Use one content-bearing root per message. Do not fall back to broad response
# class names, which can combine multiple messages or unrelated source cards.
_ASSISTANT = (
    '[data-testid="markdown-reply"], [data-content="ai-message"], [data-testid="ai-message-body"]'
)
# Only the message content roots are excluded (answer text, user text): a
# challenge that Copilot renders elsewhere in the message list must still count.
_OUTSIDE_CONVERSATION = (
    ':not([data-testid="markdown-reply"], [data-testid="markdown-reply"] *, '
    '[data-testid="chatOutput"], [data-testid="chatOutput"] *, '
    '[data-content="ai-message"], [data-content="ai-message"] *, '
    '[data-testid="ai-message-body"], [data-testid="ai-message-body"] *, '
    '[data-testid="ai-message"], [data-testid="ai-message"] *, '
    '[data-content="user-message"], [data-content="user-message"] *, '
    '[data-testid="user-message"], [data-testid="user-message"] *)'
)
# Scope text-based detection to website headings or dialog labels. Never scan
# the whole body: prompts and answers can legitimately quote the same phrase.
_NON_LABEL_CONTENT = (
    ':is([contenteditable], [role="textbox"], textarea, input, '
    '[data-testid="composer-input"], a, [role="link"], button, [role="button"])'
)
_VERIFICATION_SURFACES = ':is(dialog, [role="dialog"], [role="alertdialog"], [role="alert"])'
_VERIFICATION_LABEL = r':text-matches("^\\s*verification\\s+required[.!:]?\\s*$", "i")'
_VERIFICATION_LABEL_SCOPE = (
    _OUTSIDE_CONVERSATION + f':not({_NON_LABEL_CONTENT}, {_NON_LABEL_CONTENT} *)'
)
# A heading/dialog must not inherit its purported label from a nested prompt,
# answer or interactive control. Independently labelled child headings still
# match even when the surrounding dialog contains other controls.
_VERIFICATION_TEXT_SCOPE = _VERIFICATION_LABEL_SCOPE + (
    f':not(:has({_NON_LABEL_CONTENT}, [data-testid="markdown-reply"], [data-testid="chatOutput"], '
    '[data-content="ai-message"], [data-testid="ai-message-body"], [data-testid="ai-message"], '
    '[data-content="user-message"], [data-testid="user-message"]))'
)

COPILOT_SPEC = PageProviderSpec(
    platform="copilot",
    start_url="https://copilot.com/",
    blank_start_url_aliases=("https://copilot.com/chat",),
    composer_selectors=(
        '#m365-chat-editor-target-element[contenteditable="true"][role="textbox"]',
        '[data-testid="composer-input"]',
        'textarea[placeholder*="Ask"]',
        'textarea[aria-label*="Ask"]',
        'textarea[aria-label*="Message"]',
        '[contenteditable="true"][role="textbox"]',
    ),
    send_selectors=(
        'button[data-testid="submit-button"]',
        'button[aria-label="Submit"]',
        'button[aria-label="Send message"]',
        'button[aria-label="Send"]',
        'button[title="Submit"]',
    ),
    answer_selectors=(
        '[data-testid="markdown-reply"]',
        '[data-content="ai-message"]',
        '[data-testid="ai-message-body"]',
    ),
    response_node_selectors=(
        '[data-testid="markdown-reply"]',
        '[data-content="ai-message"]',
        '[data-testid="ai-message-body"]:not([data-content="ai-message"], [data-content="ai-message"] *)',
    ),
    user_message_selectors=(
        '[data-testid="chatQuestion"] [data-testid="chatOutput"]',
        '[data-content="user-message"]',
        '[data-testid="user-message"]:not([data-content="user-message"], [data-content="user-message"] *)',
    ),
    stop_selectors=(
        'button[data-testid="stop-button"]',
        'button[aria-label="Stop response"]',
        'button[aria-label="Stop generating"]',
        'button[aria-label="Stop"]',
    ),
    blocked_markers=("sign in to copilot", "verify you are human", "unusual activity"),
    access_wall_selectors=tuple(selector + _OUTSIDE_CONVERSATION for selector in (
        '[data-testid="anonymous-block-page-title"]',
        '[role="dialog"][aria-modal="true"]:has(button:text-is("Sign in with Microsoft"))',
        '[role="dialog"][aria-modal="true"]:has(a[href^="https://login.live.com/"])',
        'form[action^="https://login.live.com/"]',
        'form[action^="https://login.microsoftonline.com/"]',
    )),
    human_verification_selectors=tuple(selector + _OUTSIDE_CONVERSATION for selector in (
        'form#challenge-form',
        'form#captcha-form',
        'iframe[src^="https://challenges.cloudflare.com/"]',
        'iframe[src^="https://www.google.com/recaptcha/api2/bframe"]',
        'iframe[src^="https://newassets.hcaptcha.com/captcha/"][title*="challenge"]',
    )) + (
        _VERIFICATION_SURFACES + '[aria-label="Verification required" i]' + _VERIFICATION_LABEL_SCOPE,
    ),
    human_verification_text_selectors=(
        ':is(h1, h2, h3, h4, h5, h6, [role="heading"])' + _VERIFICATION_TEXT_SCOPE,
        _VERIFICATION_SURFACES + _VERIFICATION_LABEL + _VERIFICATION_TEXT_SCOPE,
        _VERIFICATION_SURFACES + ' ' + _VERIFICATION_LABEL + _VERIFICATION_TEXT_SCOPE,
    ),
    human_verification_text_markers=("verification required",),
    authentication_url_prefixes=("https://login.live.com/", "https://login.microsoftonline.com/",
        "https://accounts.google.com/", "https://copilot.com/login"),
    unauthenticated_control_selectors=tuple(selector + _OUTSIDE_CONVERSATION for selector in (
        'button:text-is("Sign in")',
        'button:text-is("Sign in with Microsoft")',
        'a[href^="/login"]',
        'a[href^="https://copilot.com/login"]',
    )),
    cookie_notice_selectors=tuple(selector + _OUTSIDE_CONVERSATION for selector in (
        '#wcpConsentBannerCtrl:has(button:text-is("Reject"))',
        ':is(dialog, [role="dialog"], [aria-label*="cookie" i]):has(button:text-is("Reject"))'
        ':has(:text-matches("^We use optional cookies", "i"))',
    )),
    other_blocker_selectors=tuple(selector + _OUTSIDE_CONVERSATION
        + ':not(:has([data-testid="markdown-reply"], [data-testid="chatOutput"], '
        '[data-content="ai-message"], [data-content="user-message"], [data-testid="user-message"]))'
        for selector in ('dialog[open]', '[role="dialog"][aria-modal="true"]', '[role="alertdialog"]', '[aria-modal="true"]')),
    terminal_error_selectors=tuple(
        root + ' [role="alert"]' for root in _ASSISTANT.split(', ')
    ),
    terminal_error_within_response_selectors=('[role="alert"]',),
    terminal_error_markers=("something went wrong", "please try again", "unable to respond"),
    conversation_url_prefixes=(
        "https://copilot.com/chat/conversation/",
        "https://copilot.com/chats/",
        "https://copilot.microsoft.com/chats/",
    ),
    response_timeout_seconds=COPILOT_BROWSER_TURN_TIMEOUT_SECONDS,
    type_composer_input=True,
    copilot_lexical_readback=True,
    composer_typing_min_delay_ms=5.0,
    composer_typing_max_delay_ms=15.0,
)


class CopilotProvider(PageProvider):
    def __init__(
        self,
        page_factory: PageFactory,
        *,
        composer_attempts: int = 3,
        response_timeout_seconds: Optional[float] = None,
        extraction_timeout_seconds: Optional[float] = None,
        interaction_min_delay_seconds: float = 0.0,
        interaction_max_delay_seconds: float = 0.0,
        cancelled: Optional[Callable[[], bool]] = None,
        human_verification_handler: Optional[Callable] = None,
    ):
        super().__init__(
            page_factory,
            COPILOT_SPEC,
            composer_attempts=composer_attempts,
            response_timeout_seconds=response_timeout_seconds,
            extraction_timeout_seconds=(
                COPILOT_BROWSER_TURN_TIMEOUT_SECONDS
                if extraction_timeout_seconds is None else extraction_timeout_seconds
            ),
            interaction_min_delay_seconds=interaction_min_delay_seconds,
            interaction_max_delay_seconds=interaction_max_delay_seconds,
            cancelled=cancelled,
            human_verification_handler=human_verification_handler,
        )


__all__ = ["COPILOT_SPEC", "CopilotProvider"]
