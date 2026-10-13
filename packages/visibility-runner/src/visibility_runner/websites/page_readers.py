"""Read website controls, transcripts and answer content without browser input."""
from __future__ import annotations

import asyncio
import hashlib
import sys
import time
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlsplit

from ..browser.operations import _await_call, _BrowserOperationDeadline
from ..errors import BrowserInteractionError, HumanVerificationRequiredError, ProviderAccessBlockedError
from .contracts import PageProviderSpec, VisibleAnswerSnapshot, _ResponseScope
from .conversation_binding import is_temporary_message_id


async def _message_items(page: object, selectors: Iterable[str], *, collapse_nested=False, turn_owner_selector="", _deadline=None) -> tuple[object, ...]:
    """Collapse aliases and optional assistant fragments within one user turn."""

    values = tuple(selector for selector in selectors if selector)
    if not values:
        return ()
    locator = page.locator(", ".join(values))
    evaluate = getattr(locator, "evaluate_all", None)
    if (collapse_nested or turn_owner_selector) and callable(evaluate):
        # Enumerate and collapse aliases in one DOM snapshot. Messages can
        # arrive between browser calls, so an earlier count cannot bound it.
        indices = await _await_call(evaluate, """(nodes, options) => {
            const indices = nodes.map((_, index) => index).filter(index => {
                if (!options.collapseNested) return true;
                const ancestors = nodes.filter((other, otherIndex) => otherIndex !== index && other.contains(nodes[index]));
                return !ancestors.length || ancestors.some(other => options.selectors.some(selector =>
                    other.matches(selector) && nodes[index].matches(selector)));
            });
            if (!options.turnOwnerSelector) return indices;
            const owners = new Map();
            const latest = new Map();
            for (const index of indices) {
                const node = nodes[index];
                const owner = node.closest(options.turnOwnerSelector);
                owners.set(index, owner);
                if (owner) {
                    const rect = node.getBoundingClientRect();
                    const visibility = getComputedStyle(node).visibility;
                    if (rect.width > 0 && rect.height > 0 && visibility !== 'hidden' && visibility !== 'collapse') {
                        latest.set(owner, index);
                    }
                }
            }
            return indices.filter(index => !owners.get(index) || latest.get(owners.get(index)) === index);
        }""", {"selectors": list(values), "collapseNested": collapse_nested,
                   "turnOwnerSelector": turn_owner_selector},
            _deadline=_deadline)
        if (not isinstance(indices, list)
                or any(type(index) is not int or index < 0 for index in indices)
                or any(left >= right for left, right in zip(indices, indices[1:]))):
            raise BrowserInteractionError("provider_message_structure_uninspectable")
    else:
        count = int(await _await_call(locator.count, _deadline=_deadline) or 0)
        indices = list(range(count))
    return tuple(locator.nth(index) for index in indices)


async def _message_id(item: object, spec: PageProviderSpec, *, _deadline=None) -> tuple[Optional[str], bool]:
    """Keep legacy identities and optionally read a semantic owning wrapper."""

    attribute = getattr(item, "get_attribute", None)
    if attribute is None:
        return None, False
    for key in ("data-message-id", "data-response-id", "data-query-id", "id"):
        value = await _await_call(attribute, key, _deadline=_deadline)
        if isinstance(value, str) and value:
            return (None if is_temporary_message_id(value, spec.temporary_message_id_prefixes) else value), False
    owner_attribute = spec.message_id_ancestor_attribute
    if not owner_attribute:
        return None, False
    owners = item.locator(f"xpath=ancestor-or-self::*[@{owner_attribute}][1]")
    if not await _await_call(owners.count, _deadline=_deadline):
        return None, False
    value = await _await_call(owners.nth(0).get_attribute, owner_attribute, _deadline=_deadline)
    identities = list(dict.fromkeys(str(value or "").split()))
    if len(identities) != 1:
        return None, len(identities) > 1
    identity = identities[0]
    return (None if is_temporary_message_id(identity, spec.temporary_message_id_prefixes) else identity), False


async def response_turn_is_generating(page: object, spec: PageProviderSpec, *, _scope=None, _deadline=None) -> bool:
    """Read the latest response's explicit turn state when the site exposes it."""
    state_attribute = spec.response_turn_state_attribute
    if not state_attribute:
        return False
    latest = _scope.latest_response if _scope is not None else None
    if _scope is None:
        messages = await _message_items(page, spec.response_node_selectors or spec.answer_selectors,
            collapse_nested=spec.collapse_nested_message_aliases,
            turn_owner_selector=spec.current_turn_owner_selector, _deadline=_deadline)
        latest = messages[-1] if messages else None
    if latest is None:
        return False
    locate = getattr(latest, "locator", None)
    if not callable(locate):
        return False
    owners = locate(f"xpath=ancestor-or-self::*[@{state_attribute}][1]")
    if not await _await_call(owners.count, _deadline=_deadline):
        return False
    state = await _await_call(owners.nth(0).get_attribute, state_attribute, _deadline=_deadline)
    return state != "complete"


@dataclass(frozen=True)
class _ConversationSnapshot:
    history: dict[str, object]
    prompt_text: str
    answer: VisibleAnswerSnapshot
    response_scope: _ResponseScope


async def _read_conversation(page: object, spec: PageProviderSpec, *, _deadline=None) -> _ConversationSnapshot:
    """Read the visible transcript and latest answer together, once per probe."""
    result: dict[str, object] = {"history_complete": True, "branch_id": None}
    response_scope = _ResponseScope()
    latest_text = {"user": "", "assistant": ""}
    for role, selectors in (("user", spec.user_message_selectors), ("assistant", spec.response_node_selectors or spec.answer_selectors)):
        messages = await _message_items(page, selectors,
            collapse_nested=spec.collapse_nested_message_aliases,
            turn_owner_selector=spec.current_turn_owner_selector if role == "assistant" else "",
            _deadline=_deadline)
        digests, identities = [], []
        for item in messages:
            if not await _await_call(item.is_visible, _deadline=_deadline):
                continue
            if role == "assistant":
                response_scope.latest_response = item
            else:
                response_scope.latest_user = item
            if role == "assistant" and spec.response_content_selectors:
                text = await _response_content_text(item, spec.response_content_selectors,
                    decoration_selectors=spec.response_content_decoration_selectors,
                    _deadline=_deadline)
            elif role == "user":
                text = await _read_user_message_text(item, spec, _deadline=_deadline)
            else:
                text = await _await_call(item.inner_text, _deadline=_deadline)
                text = await _without_rendered_answer_labels(item, text,
                    spec.rendered_answer_label_selectors, _deadline=_deadline)
            latest_text[role] = text
            digests.append(_text_sha256(text))
            identifier, ambiguous = await _message_id(item, spec, _deadline=_deadline)
            if ambiguous:
                result["history_ambiguous"] = True
            attribute = getattr(item, "get_attribute", None)
            if attribute is not None:
                branch = await _await_call(attribute, "data-branch-id", _deadline=_deadline)
                if isinstance(branch, str) and branch:
                    # The latest exposed branch identity binds the checkpoint.
                    result["branch_id"] = branch
            identities.append(identifier)
        result[role + "_sha256"] = digests
        result[role + "_ids"] = identities
    result["generating"] = (
        bool(spec.stop_selectors) and await any_visible(page, spec.stop_selectors, _deadline=_deadline)
    ) or await response_turn_is_generating(page, spec, _scope=response_scope, _deadline=_deadline)
    return _ConversationSnapshot(result, latest_text["user"],
        VisibleAnswerSnapshot(len(result["assistant_sha256"]), latest_text["assistant"]), response_scope)


async def _conversation_history(page: object, spec: PageProviderSpec, *, _deadline=None) -> dict[str, object]:
    """Expose only content digests and identities for durable checkpoints."""
    return (await _read_conversation(page, spec, _deadline=_deadline)).history


async def _read_user_message_text(item: object, spec: PageProviderSpec, *, _deadline=None) -> str:
    if spec.platform == "gemini":
        evaluate = getattr(item, "evaluate", None)
        if callable(evaluate):
            # Gemini's user-query includes a truncated accessibility preview
            # and controls. Its full prompt is rendered as one paragraph per
            # original line; wrapper innerText adds paragraph spacing instead.
            prompt = await _await_call(evaluate, r"""node => {
                const bodies = node.querySelectorAll('.query-text');
                if (bodies.length !== 1) return null;
                const lines = bodies[0].querySelectorAll(':scope > p.query-text-line');
                if (!lines.length) return null;
                return Array.from(lines, line => {
                    // A sole BR is the provider's empty-line placeholder;
                    // BRs inside actual text still preserve their line breaks.
                    const children = Array.from(line.childNodes).filter(child =>
                        child.nodeType !== Node.COMMENT_NODE &&
                        !(child.nodeType === Node.TEXT_NODE && !child.textContent.trim()));
                    if (children.length === 1 && children[0].nodeName === 'BR') return '';
                    return line.innerText;
                }).join('\n');
            }""", _deadline=_deadline)
            if isinstance(prompt, str):
                return prompt
    if spec.platform == "copilot":
        evaluate = getattr(item, "evaluate", None)
        if callable(evaluate):
            # Copilot preserves literal newlines and also inserts BR elements.
            # Read literal characters only from this known plain-text root;
            # rich/hidden descendants continue through the rendered reader.
            literal = await _await_call(evaluate, r"""node => {
                if (!node.matches('[data-testid="chatOutput"]') ||
                    !Array.from(node.childNodes).every(child => child.nodeType === Node.TEXT_NODE ||
                        (child.nodeType === Node.ELEMENT_NODE && child.tagName === 'BR' &&
                            child.previousSibling?.textContent.endsWith('\n') &&
                            child.nextSibling?.textContent.startsWith('\n')))) return null;
                return node.textContent;
            }""", _deadline=_deadline)
            if isinstance(literal, str):
                return literal.strip()
    text = await _await_call(item.inner_text, _deadline=_deadline)
    text = await _without_rendered_answer_labels(
        item, text, spec.rendered_user_label_selectors, _deadline=_deadline,
    )
    return text.strip() if spec.platform == "copilot" else text


def _remove_leading_dom_label(text: str, label_text: str) -> str:
    """Remove one label proven to be a website-owned descendant node."""

    label = label_text.strip()
    if not label:
        return text
    leading_width = len(text) - len(text.lstrip(" \t\r\n"))
    candidate = text[leading_width:]
    if not candidate.startswith(label):
        return text
    remainder = candidate[len(label) :]
    if remainder and not remainder[0].isspace():
        return text
    return remainder.lstrip(" \t\r\n")


async def _without_rendered_answer_labels(
    item: object,
    text: str,
    selectors: Iterable[str],
    *,
    _deadline: Optional[_BrowserOperationDeadline] = None,
) -> str:
    """Exclude only text owned by explicitly selected website-label nodes."""

    value = text
    nested_locator = getattr(item, "locator", None)
    if nested_locator is None:
        return value
    for selector in selectors:
        labels = nested_locator(selector)
        count = int(
            await _await_call(
                getattr(labels, "count"),
                _deadline=_deadline,
            )
            or 0
        )
        for index in range(count):
            label = getattr(labels, "nth")(index)
            text_content = getattr(label, "text_content", None)
            inner_text = getattr(label, "inner_text", None)
            read = text_content or inner_text
            if read is None:
                continue
            label_text = await _await_call(
                read,
                _deadline=_deadline,
            )
            if isinstance(label_text, str):
                value = _remove_leading_dom_label(value, label_text)
    return value


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _matching_marker(
    text: str,
    markers: Iterable[str],
) -> Optional[str]:
    folded = text.casefold()
    return next(
        (
            marker
            for marker in markers
            if marker and marker.casefold() in folded
        ),
        None,
    )


def _is_authentication_url(url: object, prefixes: Iterable[str]) -> bool:
    """Match an exact origin and path boundary, never a query substring."""

    if not isinstance(url, str):
        return False
    try:
        observed = urlsplit(url)
        for prefix in prefixes:
            expected = urlsplit(prefix)
            if (
                observed.scheme != expected.scheme
                or observed.netloc.casefold() != expected.netloc.casefold()
            ):
                continue
            boundary = expected.path.rstrip("/")
            if not boundary or observed.path == boundary or observed.path.startswith(
                boundary + "/"
            ):
                return True
    except ValueError:
        return False
    return False


def _access_block_error(platform: str, marker: str) -> ProviderAccessBlockedError:
    error_type = (
        HumanVerificationRequiredError
        if marker in {"human_verification_required", "verify you are human"}
        else ProviderAccessBlockedError
    )
    return error_type(f"{platform} displayed an authentication or access wall")


async def _visible_access_block_marker(
    page: object,
    spec: PageProviderSpec,
    *,
    _deadline: Optional[_BrowserOperationDeadline] = None,
) -> Optional[str]:
    """Require a website access surface, not words in a user's or AI's text.

    Scoped live DOM reads prove liveness. Hidden widgets and ordinary
    links or prose cannot block a valid answer. Website-owned sign-in controls
    do prove a guest session, which is outside authenticated measurement.
    Unknown no-response states retain the existing ambiguous-send protection.
    """

    if spec.platform == "copilot" and getattr(page, "_visibility_runner_blocking_dialog", None) is not None:
        return "browser_dialog"
    if _is_authentication_url(
        getattr(page, "url", ""),
        spec.authentication_url_prefixes,
    ):
        return "authentication_redirect"
    if await visible_items(
        page,
        spec.human_verification_selectors,
        _deadline=_deadline,
    ):
        return "human_verification_required"
    if spec.human_verification_text_markers:
        # Match the complete rendered label, not a substring in page prose.
        # Reading inner_text also excludes hidden descendants of a heading.
        labels = await visible_text_values(
            page, spec.human_verification_text_selectors, _deadline=_deadline,
        )
        for label in labels:
            normalized = " ".join(label.split()).casefold()
            if normalized.endswith((".", "!", ":")):
                normalized = normalized[:-1].rstrip()
            if normalized in spec.human_verification_text_markers:
                return "human_verification_required"
    if await visible_items(
        page,
        spec.unauthenticated_control_selectors,
        _deadline=_deadline,
    ):
        return "authentication_required"
    if await visible_items(
        page,
        spec.access_wall_selectors,
        _deadline=_deadline,
    ):
        return "authentication_required"
    if await visible_items(page, spec.cookie_notice_selectors, _deadline=_deadline):
        return "cookie_notice"
    for blocker in await visible_items(page, spec.other_blocker_selectors, _deadline=_deadline):
        if spec.platform == "copilot":
            from .copilot_access import tooltip_only_dialog
            if await tooltip_only_dialog(blocker, deadline=_deadline):
                continue
        return "unknown_dialog"
    if spec.platform == "copilot" and spec.other_blocker_selectors:
        from .copilot_access import obstructed_composer
        if await obstructed_composer(page, spec, deadline=_deadline):
            return "unknown_overlay"
    return None


async def visible_items(
    page: object,
    selectors: Iterable[str],
    *,
    _deadline: Optional[_BrowserOperationDeadline] = None,
) -> tuple[object, ...]:
    """Return one DOM-order union of unique visible website-owned nodes."""

    return await _visible_items_from_locator_owner(
        page,
        selectors,
        _deadline=_deadline,
    )


async def _visible_items_from_locator_owner(
    owner: object,
    selectors: Iterable[str],
    *,
    _deadline: Optional[_BrowserOperationDeadline] = None,
) -> tuple[object, ...]:
    """Resolve a visible DOM-order selector union beneath one locator owner."""

    values = tuple(str(selector) for selector in selectors if selector)
    if not values:
        return ()
    locate = getattr(owner, "locator", None)
    if not callable(locate):
        return ()
    locator = locate(", ".join(values))
    count = int(
        await _await_call(
            getattr(locator, "count"),
            _deadline=_deadline,
        )
        or 0
    )
    visible: list[object] = []
    for index in range(count):
        item = getattr(locator, "nth")(index)
        is_visible = getattr(item, "is_visible", None)
        if is_visible is not None and not bool(
            await _await_call(
                is_visible,
                _deadline=_deadline,
            )
        ):
            continue
        visible.append(item)
    return tuple(visible)


async def visible_items_within_latest_response(
    page: object,
    response_selectors: Iterable[str],
    relative_selectors: Iterable[str],
    *,
    _scope: Optional[_ResponseScope] = None,
    _deadline: Optional[_BrowserOperationDeadline] = None,
) -> tuple[object, ...]:
    """Return visible matches beneath only the newest visible response node.

    Error cards and retry controls from an older assistant turn must never
    authorize a retry for the newest user message.  Resolve the top-level
    response union first, choose its last visible node, and only then evaluate
    website-specific relative selectors inside that node.  Missing nested
    locator support fails closed with no matches.
    """

    relative = tuple(
        str(selector) for selector in relative_selectors if selector
    )
    if not relative:
        return ()
    if _scope is None:
        responses = []
        for item in await _message_items(page, response_selectors, _deadline=_deadline):
            if await _await_call(item.is_visible, _deadline=_deadline):
                responses.append(item)
        latest = responses[-1] if responses else None
    else:
        latest = _scope.latest_response
    if latest is None:
        return ()
    return await _visible_items_from_locator_owner(
        latest,
        relative,
        _deadline=_deadline,
    )


async def current_turn_notices(
    page: object,
    spec: PageProviderSpec,
    expected_prompt: str,
    *,
    _scope: Optional[_ResponseScope] = None,
    _deadline: Optional[_BrowserOperationDeadline] = None,
) -> tuple[str, ...]:
    """Read website notices owned by the newest exact submitted user turn.

    A pending turn can have alerts or status without an assistant content
    node. Keep this owner separate from the response used for answer capture;
    neither previous-turn notices nor quoted notices inside messages qualify.
    """
    owner_selector = spec.current_turn_notice_owner_selector or spec.current_turn_owner_selector
    if not owner_selector or not expected_prompt:
        return ()
    latest = _scope.latest_user if _scope is not None else None
    if latest is None:
        for item in await _message_items(
            page, spec.user_message_selectors,
            collapse_nested=spec.collapse_nested_message_aliases,
            _deadline=_deadline,
        ):
            if await _await_call(item.is_visible, _deadline=_deadline):
                latest = item
        if _scope is not None:
            _scope.latest_user = latest
    if latest is None or not await _await_call(latest.is_visible, _deadline=_deadline):
        return ()
    text = await _read_user_message_text(latest, spec, _deadline=_deadline)
    if text != expected_prompt:
        return ()
    locate = getattr(latest, "locator", None)
    if not callable(locate):
        return ()
    owners = locate("xpath=ancestor-or-self::*")
    evaluate = getattr(owners, "evaluate_all", None)
    if not callable(evaluate):
        return ()
    index = await _await_call(
        evaluate,
        """(nodes, selector) => {
            for (let index = nodes.length - 1; index >= 0; index--) {
                if (nodes[index].matches(selector)) return index;
            }
            return -1;
        }""",
        owner_selector,
        _deadline=_deadline,
    )
    if type(index) is not int or index < 0:
        return ()
    owner = owners.nth(index)
    message_selectors = tuple(spec.user_message_selectors) + tuple(
        spec.response_content_selectors or spec.response_node_selectors or spec.answer_selectors
    )
    values = []
    for notice in await _visible_items_from_locator_owner(
        owner, spec.current_turn_notice_selectors, _deadline=_deadline,
    ):
        in_message = await _await_call(
            notice.evaluate,
            "(node, selectors) => selectors.some(selector => node.closest(selector))",
            list(message_selectors),
            _deadline=_deadline,
        )
        if not in_message:
            value = await _await_call(notice.inner_text, _deadline=_deadline)
            if isinstance(value, str) and value:
                values.append(value)
    return tuple(values)


async def visible_text_values(
    page: object,
    selectors: Iterable[str],
    *,
    _deadline: Optional[_BrowserOperationDeadline] = None,
) -> tuple[str, ...]:
    values: list[str] = []
    for item in await visible_items(
        page,
        selectors,
        _deadline=_deadline,
    ):
        inner_text = getattr(item, "inner_text", None)
        if inner_text is None:
            continue
        value = await _await_call(
            inner_text,
            _deadline=_deadline,
        )
        if isinstance(value, str):
            values.append(value)
    return tuple(values)


async def visible_text_values_within_latest_response(
    page: object,
    response_selectors: Iterable[str],
    relative_selectors: Iterable[str],
    *,
    _scope: Optional[_ResponseScope] = None,
    _deadline: Optional[_BrowserOperationDeadline] = None,
) -> tuple[str, ...]:
    """Read visible text beneath only the newest visible response node."""

    values: list[str] = []
    for item in await visible_items_within_latest_response(
        page,
        response_selectors,
        relative_selectors,
        _scope=_scope,
        _deadline=_deadline,
    ):
        inner_text = getattr(item, "inner_text", None)
        if inner_text is None:
            continue
        value = await _await_call(
            inner_text,
            _deadline=_deadline,
        )
        if isinstance(value, str):
            values.append(value)
    return tuple(values)


async def first_visible(
    page: object,
    selectors: Iterable[str],
    *,
    timeout_ms: int,
    _deadline: Optional[_BrowserOperationDeadline] = None,
) -> object:
    local_deadline = time.monotonic() + max(
        0.0,
        timeout_ms / 1000,
    )
    selectors = tuple(selectors)
    while time.monotonic() <= local_deadline:
        if _deadline is not None and _deadline.expired():
            _deadline.raise_expired()
        for selector in selectors:
            locator = getattr(page, "locator")(selector)
            count = await _await_call(
                getattr(locator, "count"),
                _deadline=_deadline,
            )
            for index in range(int(count or 0)):
                item = getattr(locator, "nth")(index)
                visible = getattr(item, "is_visible", None)
                if visible is None or bool(
                    await _await_call(
                        visible,
                        _deadline=_deadline,
                    )
                ):
                    return item
        remaining = max(0.0, local_deadline - time.monotonic())
        if _deadline is not None:
            remaining = min(
                remaining,
                max(0.0, _deadline.remaining()),
            )
        await asyncio.sleep(min(0.05, remaining))
    if _deadline is not None and _deadline.expired():
        _deadline.raise_expired()
    raise TimeoutError(f"no visible element matched: {', '.join(selectors)}")


async def visible_answer_snapshot(
    page: object,
    selectors: Iterable[str],
    *,
    label_selectors: Iterable[str] = (),
    content_selectors: Iterable[str] = (),
    decoration_selectors: Iterable[str] = (),
    collapse_nested: bool = False,
    turn_owner_selector: str = "",
    _scope: Optional[_ResponseScope] = None,
    _deadline: Optional[_BrowserOperationDeadline] = None,
) -> VisibleAnswerSnapshot:
    """Count response wrappers, then read content only from the newest one."""

    if _scope is not None:
        _scope.latest_response = None
    selector_values = tuple(str(selector) for selector in selectors if selector)
    if not selector_values:
        return VisibleAnswerSnapshot(count=0, text="")
    messages = await _message_items(page, selector_values, collapse_nested=collapse_nested,
        turn_owner_selector=turn_owner_selector, _deadline=_deadline)
    visible_count = 0
    latest_item = None
    for item in messages:
        visible = getattr(item, "is_visible", None)
        if visible is not None and not bool(
            await _await_call(
                visible,
                _deadline=_deadline,
            )
        ):
            continue
        visible_count += 1
        latest_item = item
    if latest_item is None:
        return VisibleAnswerSnapshot(count=0, text="")
    if _scope is not None:
        _scope.latest_response = latest_item

    content_values = tuple(str(value) for value in content_selectors if value)
    if content_values:
        latest_text = await _response_content_text(
            latest_item,
            content_values,
            decoration_selectors=decoration_selectors,
            _deadline=_deadline,
        )
    else:
        latest_text = ""
        inner_text = getattr(latest_item, "inner_text", None)
        if inner_text is not None:
            observed = await _await_call(inner_text, _deadline=_deadline)
            if isinstance(observed, str):
                latest_text = await _without_rendered_answer_labels(
                    latest_item,
                    observed,
                    label_selectors,
                    _deadline=_deadline,
                )
    # An empty or not-yet-present content root still belongs to this newest
    # wrapper. Never substitute an older response or the wrapper's UI text.
    return VisibleAnswerSnapshot(count=visible_count, text=latest_text)


async def _response_content_text(
    response: object,
    selectors: tuple[str, ...],
    *,
    decoration_selectors: Iterable[str] = (),
    _deadline: Optional[_BrowserOperationDeadline] = None,
) -> str:
    """Read one structurally unambiguous website-owned content root.

    Nested selector aliases are one root; disjoint roots cannot safely be
    combined or reduced to a partial answer. Content inside the chosen root is
    returned unchanged, including any model-authored label-like text. A known
    website decoration is removed only when it owns the first rendered text.
    """

    nested_locator = getattr(response, "locator", None)
    if nested_locator is None:
        raise BrowserInteractionError(
            "provider_response_content_uninspectable"
        )
    candidates = nested_locator(", ".join(selectors))
    total = int(
        await _await_call(getattr(candidates, "count"), _deadline=_deadline)
        or 0
    )
    visible_indices: list[int] = []
    for index in range(total):
        candidate = getattr(candidates, "nth")(index)
        visible = getattr(candidate, "is_visible", None)
        if visible is None:
            raise BrowserInteractionError(
                "provider_response_content_uninspectable"
            )
        if bool(await _await_call(visible, _deadline=_deadline)):
            visible_indices.append(index)
    if not visible_indices:
        return ""
    if total > 1:
        evaluate_all = getattr(candidates, "evaluate_all", None)
        if evaluate_all is None:
            raise BrowserInteractionError(
                "provider_response_content_uninspectable"
            )
        roots = await _await_call(
            evaluate_all,
            """(nodes, {candidateCount, visibleIndices}) => {
              if (nodes.length !== candidateCount) return null;
              return nodes.map((_node, index) => index).filter(index =>
                visibleIndices.some(visible => index === visible ||
                  nodes[index].contains(nodes[visible])) &&
                !nodes.some((other, otherIndex) => otherIndex !== index &&
                  other.contains(nodes[index])));
            }""",
            {"candidateCount": total, "visibleIndices": visible_indices},
            _deadline=_deadline,
        )
        if (
            not isinstance(roots, list)
            or len(roots) != 1
            or type(roots[0]) is not int
            or not 0 <= roots[0] < total
        ):
            raise BrowserInteractionError("provider_response_content_ambiguous")
        selected_index = roots[0]
        # A box-less outer alias can still contain a visible child and real
        # assistant text outside it. Never demote that child to a partial root.
        if selected_index not in visible_indices:
            # Gemini can reveal a child while its full response container is
            # still animating from zero height. This poll has no complete
            # readable answer yet; keep waiting for the same newest wrapper.
            return ""
    else:
        selected_index = visible_indices[0]
    selected = getattr(candidates, "nth")(selected_index)
    inner_text = getattr(selected, "inner_text", None)
    if inner_text is None:
        raise BrowserInteractionError("provider_response_content_uninspectable")
    decorations = tuple(value for value in decoration_selectors if value)
    evaluate = getattr(selected, "evaluate", None)
    if decorations and callable(evaluate):
        observed = await _await_call(evaluate, """(root, selectors) => {
            const text = root.innerText;
            const decorations = [...root.querySelectorAll(selectors.join(', '))];
            if (!decorations.length) return {text, label: ''};
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
            while (walker.nextNode()) {
                const node = walker.currentNode;
                if (!node.textContent.trim()) continue;
                const parent = node.parentElement;
                if (!parent || parent.closest('script, style')) continue;
                const visibility = getComputedStyle(parent).visibility;
                if (visibility === 'hidden' || visibility === 'collapse') continue;
                const range = document.createRange();
                range.selectNodeContents(node);
                if (![...range.getClientRects()].some(rect => rect.width > 0 && rect.height > 0)) continue;
                const decoration = decorations.find(element => element.contains(node));
                return {text, label: decoration ? decoration.innerText : ''};
            }
            return {text, label: ''};
        }""", list(decorations), _deadline=_deadline)
        if (not isinstance(observed, dict) or not isinstance(observed.get("text"), str)
                or not isinstance(observed.get("label"), str)):
            raise BrowserInteractionError("provider_response_content_uninspectable")
        text = _remove_leading_dom_label(observed["text"], observed["label"])
    else:
        text = await _await_call(inner_text, _deadline=_deadline)
    if not isinstance(text, str):
        raise BrowserInteractionError("provider_response_content_uninspectable")
    return text


async def last_visible_text(
    page: object,
    selectors: Iterable[str],
    *,
    label_selectors: Iterable[str] = (),
    content_selectors: Iterable[str] = (),
    decoration_selectors: Iterable[str] = (),
    collapse_nested: bool = False,
    _deadline: Optional[_BrowserOperationDeadline] = None,
) -> str:
    return (
        await visible_answer_snapshot(
            page,
            selectors,
            label_selectors=label_selectors,
            content_selectors=content_selectors,
            decoration_selectors=decoration_selectors,
            collapse_nested=collapse_nested,
            _deadline=_deadline,
        )
    ).text


async def any_visible(
    page: object,
    selectors: Iterable[str],
    *,
    _deadline: Optional[_BrowserOperationDeadline] = None,
) -> bool:
    if _deadline is not None:
        return bool(
            await visible_items(
                page,
                selectors,
                _deadline=_deadline,
            )
        )
    try:
        await first_visible(page, selectors, timeout_ms=1)
        return True
    except TimeoutError:
        return False


def _is_native_playwright_timeout(error: BaseException) -> bool:
    """Identify a browser-operation timeout, not a similarly named error."""

    # The browser runtime lazily loads the public API. Reuse its concrete
    # class without introducing a browser import into the website contract.
    for module_name in (
        "playwright.async_api", "playwright.sync_api",
        "patchright.async_api", "patchright.sync_api",
    ):
        timeout_type = getattr(sys.modules.get(module_name), "TimeoutError", None)
        if (
            isinstance(timeout_type, type)
            and issubclass(timeout_type, BaseException)
            and isinstance(error, timeout_type)
        ):
            return True
    return False


