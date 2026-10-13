"""ChatGPT browser measurement driver."""

from typing import Callable, Optional

from .chat_actions import PageProvider
from .contracts import PageFactory, PageProviderSpec


_MESSAGE_REGIONS = (
    '[data-message-author-role]',
    '[data-user-message-bubble]',
    '[data-markdown-text-style="assistant-message"]',
    '[data-chatgpt-search-unit-key$=":assistant"]',
)
_OUTSIDE_CONVERSATION = ':not(' + ', '.join(
    selector for region in _MESSAGE_REGIONS for selector in (region, region + ' *')
) + ')'

_ASSISTANT_MESSAGES = (
    '[data-message-author-role="assistant"]',
    'main [data-markdown-text-style="assistant-message"]',
    'main [data-conversation-role="assistant"] + *',
)


CHATGPT_SPEC = PageProviderSpec(
    platform="chatgpt",
    start_url="https://chatgpt.com/",
    composer_selectors=(
        '[data-testid="prompt-textarea"]',
        'div[contenteditable="true"]',
        # Do not add a bare textarea fallback. ChatGPT briefly renders one
        # during hydration and then hides it, invalidating a prepared locator.
    ),
    send_selectors=(
        'button[data-testid="send-button"]',
        'button[aria-label="Send prompt"]',
        'button[aria-label="Send message"]',
    ),
    answer_selectors=_ASSISTANT_MESSAGES,
    # Content markers exclude the website's role heading. The reader collapses
    # nested aliases so a hybrid layout still contributes one message.
    response_node_selectors=_ASSISTANT_MESSAGES,
    collapse_nested_message_aliases=True,
    user_message_selectors=(
        '[data-message-author-role="user"]',
        'main [data-user-message-bubble]',
    ),
    rendered_answer_label_selectors=('[data-conversation-role="assistant"]',),
    response_turn_state_attribute="data-talvt-turn-state",
    current_turn_owner_selector="[data-turn-key]",
    message_id_ancestor_attribute="data-chatgpt-search-message-ids",
    stop_selectors=(
        'button[data-testid="stop-button"]',
        'button[aria-label*="Stop"]',
    ),
    blocked_markers=(
        "log in",
        "sign up",
        "verify you are human",
        "unusual activity",
    ),
    access_wall_selectors=tuple(selector + _OUTSIDE_CONVERSATION for selector in (
        '[role="dialog"][aria-modal="true"]:has(button:text-is("Log in"))',
        '[role="dialog"][aria-modal="true"]:has(button:text-is("Sign up"))',
        '[role="dialog"][aria-modal="true"]:has(a[href^="/auth/login"])',
        'form[action^="/auth/login"]',
        'form[action^="https://auth.openai.com/"]',
        'form[action^="https://auth0.openai.com/"]',
    )),
    human_verification_selectors=tuple(selector + _OUTSIDE_CONVERSATION for selector in (
        'form#challenge-form',
        'form#captcha-form',
        'iframe[src^="https://challenges.cloudflare.com/"]',
        'iframe[src^="https://www.google.com/recaptcha/api2/bframe"]',
        'iframe[src^="https://newassets.hcaptcha.com/captcha/"][title*="challenge"]',
    )),
    authentication_url_prefixes=(
        "https://auth.openai.com/",
        "https://auth0.openai.com/",
        "https://chatgpt.com/auth/",
    ),
    unauthenticated_control_selectors=tuple(selector + _OUTSIDE_CONVERSATION for selector in (
        'button[data-testid="login-button"]',
        'button[data-testid="signup-button"]',
    )),
    terminal_error_within_response_selectors=(
        '[role="alert"]',
        '.text-token-text-error',
        '[class*="text-token-text-error"]',
    ),
    terminal_error_markers=(
        "something went wrong",
        "there was an error generating a response",
        "error generating a response",
        "try again",
    ),
    # Every selector is scoped beneath an assistant turn so a toolbar action
    # for an older answer cannot authorize native regeneration.
    native_retry_within_response_selectors=(
        '[role="alert"] button:has-text("Retry")',
        '.text-token-text-error button:has-text("Retry")',
        '[class*="text-token-text-error"] button:has-text("Try again")',
    ),
    conversation_url_prefixes=(
        "https://chatgpt.com/c/",
    ),
    # While ChatGPT writes an answer, the assistant message carries
    # data-message-id="request-placeholder-request-WEB:<uuid>-1"; the final id
    # replaces it when the answer is complete. Read as unknown until then.
    temporary_message_id_prefixes=(
        "request-placeholder-",
    ),
)


class ChatGPTProvider(PageProvider):
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
    ):
        super().__init__(
            page_factory,
            CHATGPT_SPEC,
            composer_attempts=composer_attempts,
            response_timeout_seconds=response_timeout_seconds,
            extraction_timeout_seconds=extraction_timeout_seconds,
            interaction_min_delay_seconds=interaction_min_delay_seconds,
            interaction_max_delay_seconds=interaction_max_delay_seconds,
            cancelled=cancelled,
        )


__all__ = ["CHATGPT_SPEC", "ChatGPTProvider"]
