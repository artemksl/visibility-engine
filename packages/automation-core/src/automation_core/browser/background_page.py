"""Create one exact Chromium tab without selecting it or focusing its window.

The caller owns admission, history and the original deadline. A lost creation
response is never retried or replaced with foreground tab creation.
"""
from typing import TYPE_CHECKING
from uuid import uuid4

from automation_core.storage.models import OperationBudget, NeedsAttention, check_budget, timeout_milliseconds

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page


def open_background_page(context: 'BrowserContext', *, budget: OperationBudget | None) -> 'Page':
    check_budget(budget)
    anchor = next((page for page in context.pages if not page.is_closed()), None)
    if anchor is None:
        raise NeedsAttention('Background tab creation requires an existing page in the exact context.')
    control = context.new_cdp_session(anchor)
    candidate_control = None
    try:
        check_budget(budget)
        source = control.send('Target.getTargetInfo').get('targetInfo', {})
        check_budget(budget)
        if source.get('type') != 'page' or not source.get('targetId'):
            raise NeedsAttention('Background tab context could not be established.')
        marker = 'about:blank#automation-background-' + uuid4().hex
        params = dict(url=marker, background=True, newWindow=False)
        context_id = source.get('browserContextId')
        if context_id is not None:
            if type(context_id) is not str or not context_id:
                raise NeedsAttention('Background tab context could not be established.')
            params['browserContextId'] = context_id
        # The nonce selects the event; the returned native target ID below is
        # the ownership proof. A similar URL or an unrelated popup never is.
        with context.expect_page(predicate=lambda page: page.url == marker,
                timeout=timeout_milliseconds(budget, 10000)) as created:
            response = control.send('Target.createTarget', params)
        page = created.value
        check_budget(budget)
        target_id = response.get('targetId')
        if type(target_id) is not str or not target_id or page.context is not context or page.is_closed():
            raise NeedsAttention('The created background tab could not be identified.')
        candidate_control = context.new_cdp_session(page)
        check_budget(budget)
        actual = candidate_control.send('Target.getTargetInfo').get('targetInfo', {})
        check_budget(budget)
        if (actual.get('targetId') != target_id or actual.get('type') != 'page'
                or actual.get('browserContextId') != context_id or page.url != marker):
            raise NeedsAttention('The created background tab did not match its native identity.')
        return page
    finally:
        # Detach only our protocol observers. Keep any possibly created page:
        # the caller's pending intent owns recovery of an uncertain creation.
        for observer in (candidate_control, control):
            if observer is not None:
                try:
                    observer.detach()
                except Exception:
                    pass
