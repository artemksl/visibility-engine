"""Gemini browser measurement and fresh-extractor driver."""

from typing import Callable, Optional

from .chat_actions import PageProvider
from .contracts import PageFactory, PageProviderSpec


_OUTSIDE_CONVERSATION = (
    ':not(model-response, model-response *, user-query, user-query *, '
    '[data-test-id="model-response"], [data-test-id="model-response"] *, '
    '[data-test-id="user-query"], [data-test-id="user-query"] *)'
)


GEMINI_SPEC = PageProviderSpec(
    platform="gemini",
    start_url="https://gemini.google.com/app",
    composer_selectors=(
        'div[contenteditable="true"][role="textbox"][aria-label="Enter a prompt for Gemini"]',
        "rich-textarea .ql-editor[contenteditable='true']",
        "rich-textarea [role='textbox']",
    ),
    composer_obstruction_close_selectors=(
        'map-local-immersive-panel .maps-container[aria-hidden="false"] '
        '.action-bar gem-icon-button.close-button button[aria-label="Close"]',
    ),
    composer_obstruction_view_query_parameters=("canvas-id",),
    send_selectors=(
        "gem-icon-button.send-button button",
        "gem-icon-button.submit button",
        'button[aria-label="Send message"]',
        'button[aria-label="Enviar mensaje"]',
        'button[data-testid*="send"]',
        'button:has-text("Submit")',
    ),
    answer_selectors=(
        "model-response .markdown",
        "model-response message-content",
        "model-response structured-content-container.model-response-text",
        "model-response .response-container",
        "[data-test-id='model-response']",
    ),
    response_node_selectors=(
        "model-response",
        "[data-test-id='model-response']",
    ),
    # Count the complete response wrapper, but capture only its existing
    # content roots. Website accessibility headings are outside these roots;
    # label-like text inside model content must remain byte-preserving.
    response_content_selectors=(
        ".markdown",
        "message-content",
        "structured-content-container.model-response-text",
    ),
    # A rendered fenced block starts with the website's language/copy toolbar.
    # Its label is UI text; an identical word in model prose is still content.
    response_content_decoration_selectors=(
        "code-block .code-block-decoration",
    ),
    user_message_selectors=(
        "user-query",
        "[data-test-id='user-query']",
    ),
    # Gemini renders this accessibility heading inside every user-query.
    # Remove only that website-owned descendant so exact prompt comparison
    # remains byte-preserving even when the prompt itself starts "You said".
    rendered_user_label_selectors=(
        ".screen-reader-user-query-label",
    ),
    stop_selectors=(
        'button[aria-label="Stop response"]',
        'button:has-text("Stop response")',
    ),
    blocked_markers=(
        "sign in",
        "verify it",
        "unusual traffic",
    ),
    access_wall_selectors=tuple(selector + _OUTSIDE_CONVERSATION for selector in (
        '[role="dialog"][aria-modal="true"]:has(a[href^="https://accounts.google.com/ServiceLogin"])',
        '[role="dialog"][aria-modal="true"]:has(a[href^="https://accounts.google.com/v3/signin/"])',
        '[role="dialog"][aria-modal="true"]:has(button:text-is("Sign in"))',
        'form[action^="https://accounts.google.com/ServiceLogin"]',
        'form[action^="https://accounts.google.com/v3/signin/"]',
    )),
    human_verification_selectors=tuple(selector + _OUTSIDE_CONVERSATION for selector in (
        'form#captcha-form',
        'form[action*="/sorry/"]',
        'iframe[src^="https://www.google.com/recaptcha/api2/bframe"]',
    )),
    authentication_url_prefixes=(
        "https://accounts.google.com/",
    ),
    cookie_notice_selectors=(
        '[role="dialog"]:has(cookie-banner)' + _OUTSIDE_CONVERSATION,
    ),
    cookie_reject_selectors=(
        'cookie-banner button[data-test-id="reject-button"]:text-is("Reject all")' + _OUTSIDE_CONVERSATION,
    ),
    optional_notice_dismiss_selectors=(
        '[role="dialog"]:has(button:is(:text-is("Use precise location"), '
        ':has(:text-is("Use precise location")))) '
        'button:is(:text-is("Dismiss"), :has(:text-is("Dismiss")))'
        + _OUTSIDE_CONVERSATION,
    ),
    # Account-management links are not sign-in proof. Limit these selectors to
    # the website banner's login routes, excluding all conversation content.
    unauthenticated_control_selectors=tuple(selector + _OUTSIDE_CONVERSATION for selector in (
        'top-bar-actions .signed-out-buttons button:text-is("Sign in")',
        'a[aria-label="Sign in"][href^="https://accounts.google.com/ServiceLogin"]',
        'header a[href^="https://accounts.google.com/ServiceLogin"]',
        '[role="banner"] a[href^="https://accounts.google.com/ServiceLogin"]',
        'header a[href^="https://accounts.google.com/v3/signin/"]',
        '[role="banner"] a[href^="https://accounts.google.com/v3/signin/"]',
    )),
    terminal_error_within_response_selectors=(
        "[role='alert']",
        ".error-container",
        "[data-test-id*='error']",
    ),
    terminal_error_markers=(
        "something went wrong",
        "there was an error",
        "couldn't generate a response",
        "could not generate a response",
        "try again",
    ),
    native_retry_within_response_selectors=(
        "[role='alert'] button:has-text('Retry')",
        ".error-container button:has-text('Try again')",
        "[data-test-id*='error'] button:has-text('Retry')",
        "[role='alert'] button:has-text('Try again')",
    ),
    conversation_url_prefixes=(
        "https://gemini.google.com/app/",
    ),
    # Gemini renders this accessibility heading inside every model-response.
    # Select the website-owned DOM node so identical model text stays intact.
    rendered_answer_label_selectors=("h2.cdk-visually-hidden",),
)


class GeminiProvider(PageProvider):
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
            GEMINI_SPEC,
            composer_attempts=composer_attempts,
            response_timeout_seconds=response_timeout_seconds,
            extraction_timeout_seconds=extraction_timeout_seconds,
            interaction_min_delay_seconds=interaction_min_delay_seconds,
            interaction_max_delay_seconds=interaction_max_delay_seconds,
            cancelled=cancelled,
        )


__all__ = ["GEMINI_SPEC", "GeminiProvider"]
