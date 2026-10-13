"""Lossless, fail-closed composer input preparation.

The helper in this module performs no send.  It clears a composer, writes the
entire supplied string, reads it back, and compares Python strings exactly.
There is no whitespace normalization, newline conversion, trimming, byte
limit, or truncation.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import inspect
import math
import random
import sys
from typing import Awaitable, Callable, Optional, Protocol

from ..errors import (
    ComposerInputMismatchError as _CoreComposerInputMismatchError,
    IdentityMismatchError,
    ProviderAccessBlockedError,
    ProviderError as _CoreProviderError,
)

class ComposerFailureKind(str, Enum):
    CLEAR_FAILED = "composer_clear_failed"
    WRITE_FAILED = "composer_write_failed"
    READBACK_UNAVAILABLE = "composer_readback_unavailable"
    READBACK_MISMATCH = "composer_readback_mismatch"
    SEND_ALREADY_STARTED = "composer_send_already_started"
    SEND_FENCE_UNAVAILABLE = "composer_send_fence_unavailable"


@dataclass(frozen=True)
class ComposerPreSendFailure:
    """Durable-safe typed evidence for a failure before any authorized send."""

    kind: ComposerFailureKind
    phase: str
    attempt: int
    max_attempts: int
    expected_text: str
    observed_text: Optional[str]
    expected_sha256: str
    observed_sha256: Optional[str]
    expected_character_count: int
    observed_character_count: Optional[int]
    expected_utf8_byte_count: int
    observed_utf8_byte_count: Optional[int]
    send_started: bool
    detail: str

    @property
    def failure_type(self) -> str:
        return self.kind.value

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["kind"] = self.kind.value
        value["failure_type"] = self.kind.value
        return value


@dataclass(frozen=True)
class ComposerInputReceipt:
    """Proof that the exact prompt occupied the composer before send."""

    text: str
    sha256: str
    character_count: int
    utf8_byte_count: int
    attempts: int


class ExactComposerInputFailure(_CoreProviderError):
    """Exact composer preparation failed before an authorized send."""

    def __init__(self, failure: ComposerPreSendFailure):
        self.failure = failure
        super().__init__(
            f"{failure.kind.value} on attempt {failure.attempt}/"
            f"{failure.max_attempts}: {failure.detail}"
        )


class ComposerReadbackUnavailable(ExactComposerInputFailure):
    """The composer exposed no exact-value readback seam."""


class ComposerReadbackMismatch(
    ExactComposerInputFailure,
    _CoreComposerInputMismatchError,
):
    """The composer did not retain the exact supplied string."""


class ComposerSendAlreadyStarted(ExactComposerInputFailure):
    """Preparation or retry was refused because a send attempt was active."""


class FailureRecorder(Protocol):
    def __call__(
        self,
        failure: ComposerPreSendFailure,
    ) -> object | Awaitable[object]:
        ...


async def _await_if_needed(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


async def _call(method: Callable[..., object], *args: object) -> object:
    return await _await_if_needed(method(*args))


async def _is_content_editable(composer: object) -> bool:
    evaluate = getattr(composer, "evaluate", None)
    if not callable(evaluate):
        return False
    try:
        value = await _call(
            evaluate,
            "el => Boolean(el && el.isContentEditable)",
        )
    except Exception:
        return False
    return value is True


async def _set_content_editable_value(
    composer: object,
    value: str,
    *,
    input_type: str,
) -> None:
    evaluate = getattr(composer, "evaluate", None)
    if not callable(evaluate):
        raise TypeError("contenteditable composer has no evaluate operation")
    await _call(
        evaluate,
        """(el, payload) => {
            el.focus();
            el.textContent = payload.value;
            el.dispatchEvent(new InputEvent('input', {
                bubbles: true,
                inputType: payload.inputType,
                data: payload.value
            }));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        {"value": value, "inputType": input_type},
    )


async def clear_composer(composer: object) -> None:
    """Clear a textarea/input/contenteditable without selecting a website."""

    if await _is_content_editable(composer):
        await _set_content_editable_value(
            composer,
            "",
            input_type="deleteContent",
        )
        return
    clear = getattr(composer, "clear", None)
    if callable(clear):
        await _call(clear)
        return
    fill = getattr(composer, "fill", None)
    if callable(fill):
        await _call(fill, "")
        return
    evaluate = getattr(composer, "evaluate", None)
    if callable(evaluate):
        await _call(
            evaluate,
            """el => {
                el.focus();
                if (typeof el.value === 'string') {
                    el.value = '';
                } else if (el.isContentEditable) {
                    el.textContent = '';
                } else {
                    throw new Error('composer has no exact writable value');
                }
                el.dispatchEvent(new InputEvent('input', {
                    bubbles: true,
                    inputType: 'deleteContent'
                }));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
        )
        return
    raise TypeError("composer exposes no exact clear operation")


async def write_composer_value(composer: object, text: str) -> None:
    """Write the complete string in one lossless operation."""

    if await _is_content_editable(composer):
        # Playwright fill() represents line breaks as block elements in rich
        # editors. Legacy textContent readback loses those separators.
        # A single text node retains
        # every character while still emitting the editor's input event.
        await _set_content_editable_value(
            composer,
            text,
            input_type="insertText",
        )
        return
    fill = getattr(composer, "fill", None)
    if callable(fill):
        await _call(fill, text)
        return
    evaluate = getattr(composer, "evaluate", None)
    if callable(evaluate):
        await _call(
            evaluate,
            """(el, value) => {
                el.focus();
                if (typeof el.value === 'string') {
                    el.value = value;
                } else if (el.isContentEditable) {
                    el.textContent = value;
                } else {
                    throw new Error('composer has no exact writable value');
                }
                el.dispatchEvent(new InputEvent('input', {
                    bubbles: true,
                    inputType: 'insertText',
                    data: value
                }));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            text,
        )
        return
    raise TypeError("composer exposes no lossless write operation")


async def clear_composer_with_keyboard(composer: object) -> None:
    """Clear through the target locator, including the editor's key handlers."""

    press = getattr(composer, "press", None)
    if not callable(press):
        raise TypeError("composer exposes no target-local keyboard operation")
    await _call(press, "Meta+A" if sys.platform == "darwin" else "Control+A")
    await _call(press, "Backspace")


async def type_composer_value(
    composer: object, text: str, *, min_delay_ms: float = 5.0,
    max_delay_ms: float = 15.0,
    before_character: Optional[Callable[[], object]] = None,
) -> None:
    """Type character by character; newline keys must never be bare Enter.

    These locator operations use the page's input channel, not the desktop's
    focused window or clipboard. Unicode characters unsupported by the US key
    layout are inserted individually by Playwright. Each gap between characters
    gets an independently sampled pause, including gaps around line breaks.
    """

    if (not math.isfinite(min_delay_ms) or not math.isfinite(max_delay_ms)
            or min_delay_ms < 0 or max_delay_ms < min_delay_ms):
        raise ValueError("typing delay bounds must be finite, nonnegative and ordered")
    # A raw carriage return is an Enter key to Playwright. Never let arbitrary
    # control characters become submission/navigation keys during preparation.
    if any(ord(char) < 32 and char not in "\n\t" for char in text):
        raise ValueError("sequential typing cannot preserve this control character")
    sequential = getattr(composer, "press_sequentially", None)
    press = getattr(composer, "press", None)
    if not callable(sequential) or not callable(press):
        raise TypeError("composer exposes no target-local sequential typing operation")
    for index, character in enumerate(text):
        if index:
            await asyncio.sleep(random.uniform(min_delay_ms, max_delay_ms) / 1000.0)
        if before_character is not None:
            await _await_if_needed(before_character())
        if character == "\n":
            await _await_if_needed(press("Shift+Enter", delay=0))
        else:
            await _await_if_needed(sequential(
                character, delay=0, timeout=30_000,
            ))


async def read_typed_composer_value(composer: object) -> Optional[str]:
    """Read native editor line breaks without normalizing whitespace."""

    if not await _is_content_editable(composer):
        return await read_composer_value(composer)
    evaluate = getattr(composer, "evaluate", None)
    if not callable(evaluate):
        return None
    value = await _call(evaluate, """el => {
        if (!el.isContentEditable) return null;
        // Native Chromium fill uses flat DIV lines, or P lines when the
        // editor already has a paragraph. An empty line is one BR placeholder.
        // Read these exact line shapes; innerText adds layout whitespace
        // while textContent drops the paragraph separators.
        const meaningfulChildren = node => Array.from(node.childNodes).filter(
            child => child.nodeType !== Node.COMMENT_NODE &&
                !(child.nodeType === Node.TEXT_NODE && child.textContent === '')
        );
        const inlineTags = new Set([
            'A', 'ABBR', 'B', 'BDI', 'BDO', 'CITE', 'CODE', 'DATA', 'DEL', 'DFN',
            'EM', 'I', 'INS', 'KBD', 'MARK', 'Q', 'S', 'SAMP', 'SMALL', 'SPAN',
            'STRONG', 'SUB', 'SUP', 'TIME', 'U', 'VAR', 'WBR'
        ]);
        const blockTags = new Set([
            'ADDRESS', 'ARTICLE', 'ASIDE', 'BLOCKQUOTE', 'CAPTION', 'COLGROUP',
            'DD', 'DETAILS', 'DIALOG', 'DIV', 'DL', 'DT', 'FIELDSET', 'FIGCAPTION',
            'FIGURE', 'FOOTER', 'FORM', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6',
            'HEADER', 'HR', 'LI', 'MAIN', 'NAV', 'OL', 'P', 'PRE', 'SECTION',
            'SUMMARY', 'TABLE', 'TBODY', 'TD', 'TFOOT', 'TH', 'THEAD', 'TR', 'UL'
        ]);
        const exactText = (node, inlineOnly = false) => {
            if (node.nodeType === Node.TEXT_NODE) return node.textContent;
            if (node.nodeName === 'BR') return '\\n';
            if (node.nodeType !== Node.ELEMENT_NODE) return null;
            if (node.localName === 'svg' && node.namespaceURI === 'http://www.w3.org/2000/svg' &&
                node.textContent === '') {
                // Empty graphics contribute no text regardless of their CSS.
                // Mixed HTML descendants can contain editor line boundaries.
                return Array.from(node.querySelectorAll('*')).every(
                    child => child.namespaceURI === node.namespaceURI
                ) ? '' : null;
            }
            const children = meaningfulChildren(node);
            let value = '';
            for (const child of children) {
                if (child.nodeType === Node.ELEMENT_NODE && child.nodeName !== 'BR') {
                    const display = getComputedStyle(child).display;
                    const semanticInline = inlineTags.has(child.nodeName) ||
                        (child.localName === 'svg' && child.textContent === '');
                    const inline = semanticInline ||
                        ['inline', 'inline-block', 'inline-flex', 'inline-grid', 'contents', 'none'].includes(display);
                    // Flex layout can blockify inline formatting. Paragraphs,
                    // lists and tables still have no proven nested line shape.
                    if (inlineOnly && blockTags.has(child.nodeName)) return null;
                    if (!inline && (inlineOnly || children.length !== 1)) return null;
                }
                const part = exactText(child, inlineOnly);
                if (part === null) return null;
                value += part;
            }
            return value;
        };
        if (el.nodeName === 'DIV') {
            const children = meaningfulChildren(el);
            const paragraphTag = children[0]?.nodeName;
            const paragraphs = ['DIV', 'P'].includes(paragraphTag) && children.every(
                child => child.nodeType === Node.ELEMENT_NODE && child.nodeName === paragraphTag
            );
            const textThenLines = children.length > 1 && children[0].nodeType === Node.TEXT_NODE &&
                children.slice(1).every(child => child.nodeType === Node.ELEMENT_NODE && child.nodeName === 'DIV');
            if (paragraphs || textThenLines) {
                const lines = [];
                for (const child of children) {
                    if (child.nodeType === Node.TEXT_NODE) {
                        lines.push(child.textContent);
                        continue;
                    }
                    const parts = meaningfulChildren(child);
                    if (parts.length === 1 && parts[0].nodeName === 'BR' && !parts[0].childNodes.length) {
                        lines.push('');
                    } else {
                        const text = exactText(child, true);
                        if (text === null || !parts.length) return null;
                        lines.push(text);
                    }
                }
                return lines.join('\\n');
            }
        }
        let value = exactText(el);
        if (value === null) return null;
        const selection = el.ownerDocument.getSelection();
        if (!selection || !selection.isCollapsed || !el.contains(selection.focusNode)) {
            return value;
        }
        // Chromium adds one empty-line caret placeholder after a trailing
        // Shift+Enter. Prove that exact structure; never trim supplied text.
        const suffix = el.ownerDocument.createRange();
        suffix.selectNodeContents(el);
        suffix.setStart(selection.focusNode, selection.focusOffset);
        const tail = suffix.cloneContents();
        const only = tail.childNodes.length === 1 ? tail.firstChild : null;
        const isPlaceholder = only && (
            (only.nodeType === Node.TEXT_NODE && only.textContent === '\\n') ||
            (only.nodeType === Node.ELEMENT_NODE && only.nodeName === 'BR')
        );
        if (isPlaceholder && value.endsWith('\\n\\n')) {
            const prefix = el.ownerDocument.createRange();
            prefix.selectNodeContents(el);
            prefix.setEnd(selection.focusNode, selection.focusOffset);
            const head = prefix.cloneContents();
            let last = head.lastChild;
            while (last && last.nodeType === Node.TEXT_NODE && last.textContent === '') {
                last = last.previousSibling;
            }
            if (last && (
                (last.nodeType === Node.TEXT_NODE && last.textContent.endsWith('\\n')) ||
                (last.nodeType === Node.ELEMENT_NODE && last.nodeName === 'BR')
            )) value = value.slice(0, -1);
        }
        return value;
    }""")
    return value if isinstance(value, str) else None


async def read_copilot_composer_value(composer: object) -> Optional[str]:
    """Read Copilot's observed Lexical paragraphs without editor markers.

    This is deliberately specific to the identified Microsoft editor. Its
    terminal hidden U+200B/U+200C spans are editor-owned markers, while
    paragraph boundaries represent typed line feeds. All other text remains
    exact, including user-supplied zero-width characters and hidden text. A
    recognized editor with an unfamiliar structure has no provable readback;
    older Copilot editors keep the existing generic typed-value reader.
    """

    evaluate = getattr(composer, "evaluate", None)
    if not callable(evaluate):
        return await read_typed_composer_value(composer)
    result = await _call(evaluate, r"""el => {
        const matches = el.nodeName === 'SPAN' &&
            el.id === 'm365-chat-editor-target-element' &&
            el.getAttribute('role') === 'textbox' &&
            el.getAttribute('aria-label') === 'Message Copilot' &&
            el.getAttribute('contenteditable') === 'true' &&
            el.getAttribute('data-lexical-editor') === 'true' &&
            el.isContentEditable;
        if (!matches) return {matches: false};
        const unknown = {matches: true, value: null};
        const paragraphs = Array.from(el.childNodes);
        if (!paragraphs.length) return unknown;
        const lines = [];
        for (const paragraph of paragraphs) {
            if (paragraph.nodeType !== Node.ELEMENT_NODE || paragraph.nodeName !== 'P') {
                return unknown;
            }
            const parts = Array.from(paragraph.childNodes);
            // Both the initial empty editor and a typed trailing newline use
            // one BR in an otherwise empty paragraph. Neither contributes text.
            if (parts.length === 1 && parts[0].nodeName === 'BR' && !parts[0].childNodes.length) {
                lines.push('');
                continue;
            }
            if (!parts.length) return unknown;
            let line = '';
            for (const [index, part] of parts.entries()) {
                if (part.nodeType !== Node.ELEMENT_NODE || part.nodeName !== 'SPAN' ||
                    part.getAttribute('data-lexical-text') !== 'true' ||
                    Array.from(part.childNodes).some(child => child.nodeType !== Node.TEXT_NODE)) {
                    return unknown;
                }
                const text = part.textContent;
                const terminalEditorMarker = index === parts.length - 1 &&
                    part.getAttribute('aria-hidden') === 'true' &&
                    text === '\u200b\u200c';
                if (!terminalEditorMarker) line += text;
            }
            lines.push(line);
        }
        return {matches: true, value: lines.join('\n')};
    }""")
    if isinstance(result, dict) and result.get("matches") is False:
        return await read_typed_composer_value(composer)
    if isinstance(result, dict) and result.get("matches") is True:
        value = result.get("value")
        return value if isinstance(value, str) else None
    return None


async def read_composer_value(composer: object) -> Optional[str]:
    """Read the exact composer value, or ``None`` when it is unavailable."""

    input_value = getattr(composer, "input_value", None)
    if callable(input_value):
        try:
            value = await _call(input_value)
        except Exception:
            pass
        else:
            if isinstance(value, str):
                return value

    evaluate = getattr(composer, "evaluate", None)
    if callable(evaluate):
        try:
            value = await _call(
                evaluate,
                """el => {
                    if (typeof el.value === 'string') return el.value;
                    if (el.isContentEditable) return el.textContent;
                    return null;
                }""",
            )
        except Exception:
            pass
        else:
            if isinstance(value, str):
                return value

    # These are compatibility fallbacks for small browser adapters.  They are
    # not normalized; any DOM transformation causes a safe mismatch.
    for method_name in ("text_content", "inner_text"):
        method = getattr(composer, method_name, None)
        if not callable(method):
            continue
        try:
            value = await _call(method)
        except Exception:
            continue
        if isinstance(value, str):
            return value
    return None


def _failure(
    *,
    kind: ComposerFailureKind,
    attempt: int,
    max_attempts: int,
    expected: str,
    observed: Optional[str],
    send_started: bool,
    detail: str,
) -> ComposerPreSendFailure:
    expected_bytes = expected.encode("utf-8")
    observed_bytes = observed.encode("utf-8") if observed is not None else None
    return ComposerPreSendFailure(
        kind=kind,
        phase="pre_send",
        attempt=attempt,
        max_attempts=max_attempts,
        expected_text=expected,
        observed_text=observed,
        expected_sha256=hashlib.sha256(expected_bytes).hexdigest(),
        observed_sha256=(
            hashlib.sha256(observed_bytes).hexdigest()
            if observed_bytes is not None
            else None
        ),
        expected_character_count=len(expected),
        observed_character_count=len(observed) if observed is not None else None,
        expected_utf8_byte_count=len(expected_bytes),
        observed_utf8_byte_count=(
            len(observed_bytes) if observed_bytes is not None else None
        ),
        send_started=send_started,
        detail=detail,
    )


async def _record(
    recorder: Optional[FailureRecorder],
    failure: ComposerPreSendFailure,
) -> None:
    if recorder is not None:
        await _await_if_needed(recorder(failure))


def _exception(failure: ComposerPreSendFailure) -> ExactComposerInputFailure:
    if failure.kind is ComposerFailureKind.READBACK_UNAVAILABLE:
        return ComposerReadbackUnavailable(failure)
    if failure.kind is ComposerFailureKind.READBACK_MISMATCH:
        return ComposerReadbackMismatch(failure)
    if failure.kind in {
        ComposerFailureKind.SEND_ALREADY_STARTED,
        ComposerFailureKind.SEND_FENCE_UNAVAILABLE,
    }:
        return ComposerSendAlreadyStarted(failure)
    return ExactComposerInputFailure(failure)


async def _send_has_started(
    callback: Callable[[], object],
    *,
    attempt: int,
    max_attempts: int,
    expected: str,
    recorder: Optional[FailureRecorder],
) -> bool:
    try:
        value = await _await_if_needed(callback())
    except Exception as exc:
        failure = _failure(
            kind=ComposerFailureKind.SEND_FENCE_UNAVAILABLE,
            attempt=attempt,
            max_attempts=max_attempts,
            expected=expected,
            observed=None,
            send_started=True,
            detail=f"send-started fence could not be read: {type(exc).__name__}: {exc}",
        )
        await _record(recorder, failure)
        raise _exception(failure) from exc
    return bool(value)


async def write_exact_composer_input(
    composer: object,
    prompt: str,
    max_attempts: int = 2,
    *,
    send_started: Optional[Callable[[], object]] = None,
    record_failure: Optional[FailureRecorder] = None,
    on_failure: Optional[FailureRecorder] = None,
    clear: Optional[Callable[[object], object]] = None,
    write: Optional[Callable[[object, str], object]] = None,
    read: Optional[Callable[[object], object]] = None,
    clear_before_write: bool = True,
) -> ComposerInputReceipt:
    """Prepare and prove an exact composer value before any send.

    Mismatches and unavailable readback may be retried only while the supplied
    send attempt says no send has started.  The function itself never invokes a
    submit control.  On final failure, one typed pre-send record is delivered
    to the recorder before the corresponding typed exception is raised.
    A replacing native Fill needs no preliminary keyboard clear; callers using
    that input contract set ``clear_before_write=False``. Appending typing keeps
    the default clear, including before each bounded refill attempt.
    """

    if not isinstance(prompt, str):
        raise TypeError("prompt must be text")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise TypeError("max_attempts must be an integer")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least one")
    if record_failure is not None and on_failure is not None:
        raise ValueError("provide only one pre-send failure recorder")
    recorder = record_failure or on_failure
    started_callback = send_started or (lambda: False)
    clear_operation = clear or clear_composer
    write_operation = write or write_composer_value
    read_operation = read or read_composer_value
    last_failure: Optional[ComposerPreSendFailure] = None

    for attempt in range(1, max_attempts + 1):
        if await _send_has_started(
            started_callback,
            attempt=attempt,
            max_attempts=max_attempts,
            expected=prompt,
            recorder=recorder,
        ):
            failure = _failure(
                kind=ComposerFailureKind.SEND_ALREADY_STARTED,
                attempt=attempt,
                max_attempts=max_attempts,
                expected=prompt,
                observed=None,
                send_started=True,
                detail="exact composer preparation/refill refused after send started",
            )
            await _record(recorder, failure)
            raise _exception(failure)

        try:
            if clear_before_write:
                await _await_if_needed(clear_operation(composer))
        except (IdentityMismatchError, ProviderAccessBlockedError):
            raise
        except Exception as exc:
            last_failure = _failure(
                kind=ComposerFailureKind.CLEAR_FAILED,
                attempt=attempt,
                max_attempts=max_attempts,
                expected=prompt,
                observed=None,
                send_started=False,
                detail=f"composer could not be cleared: {type(exc).__name__}: {exc}",
            )
        else:
            if await _send_has_started(
                started_callback,
                attempt=attempt,
                max_attempts=max_attempts,
                expected=prompt,
                recorder=recorder,
            ):
                failure = _failure(
                    kind=ComposerFailureKind.SEND_ALREADY_STARTED,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    expected=prompt,
                    observed=None,
                    send_started=True,
                    detail="composer write refused because send started before write",
                )
                await _record(recorder, failure)
                raise _exception(failure)
            try:
                await _await_if_needed(write_operation(composer, prompt))
            except (IdentityMismatchError, ProviderAccessBlockedError):
                raise
            except Exception as exc:
                last_failure = _failure(
                    kind=ComposerFailureKind.WRITE_FAILED,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    expected=prompt,
                    observed=None,
                    send_started=False,
                    detail=f"complete composer write failed: {type(exc).__name__}: {exc}",
                )
            else:
                if await _send_has_started(
                    started_callback,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    expected=prompt,
                    recorder=recorder,
                ):
                    failure = _failure(
                        kind=ComposerFailureKind.SEND_ALREADY_STARTED,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        expected=prompt,
                        observed=None,
                        send_started=True,
                        detail="composer readback refused because send already started",
                    )
                    await _record(recorder, failure)
                    raise _exception(failure)
                try:
                    observed_value = await _await_if_needed(
                        read_operation(composer)
                    )
                except (IdentityMismatchError, ProviderAccessBlockedError):
                    raise
                except Exception as exc:
                    observed = None
                    last_failure = _failure(
                        kind=ComposerFailureKind.READBACK_UNAVAILABLE,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        expected=prompt,
                        observed=None,
                        send_started=False,
                        detail=(
                            "exact composer readback failed: "
                            f"{type(exc).__name__}: {exc}"
                        ),
                    )
                else:
                    observed = (
                        observed_value if isinstance(observed_value, str) else None
                    )
                    if observed is None:
                        last_failure = _failure(
                            kind=ComposerFailureKind.READBACK_UNAVAILABLE,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            expected=prompt,
                            observed=None,
                            send_started=False,
                            detail="composer exposes no exact string readback",
                        )
                    elif observed == prompt:
                        encoded = prompt.encode("utf-8")
                        return ComposerInputReceipt(
                            text=prompt,
                            sha256=hashlib.sha256(encoded).hexdigest(),
                            character_count=len(prompt),
                            utf8_byte_count=len(encoded),
                            attempts=attempt,
                        )
                    else:
                        last_failure = _failure(
                            kind=ComposerFailureKind.READBACK_MISMATCH,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            expected=prompt,
                            observed=observed,
                            send_started=False,
                            detail=(
                                "composer readback differs from the supplied "
                                "string exactly"
                            ),
                        )

        assert last_failure is not None
        if attempt < max_attempts:
            # This send attempt check is the only authorization for a destructive
            # clear-and-refill retry.
            if await _send_has_started(
                started_callback,
                attempt=attempt,
                max_attempts=max_attempts,
                expected=prompt,
                recorder=recorder,
            ):
                failure = _failure(
                    kind=ComposerFailureKind.SEND_ALREADY_STARTED,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    expected=prompt,
                    observed=last_failure.observed_text,
                    send_started=True,
                    detail="composer retry refused after send started",
                )
                await _record(recorder, failure)
                raise _exception(failure)
            continue

    assert last_failure is not None
    await _record(recorder, last_failure)
    raise _exception(last_failure)


prepare_exact_composer_input = write_exact_composer_input
exact_composer_input = write_exact_composer_input


__all__ = [
    "ComposerFailureKind",
    "ComposerInputReceipt",
    "ComposerPreSendFailure",
    "ComposerReadbackMismatch",
    "ComposerReadbackUnavailable",
    "ComposerSendAlreadyStarted",
    "ExactComposerInputFailure",
    "clear_composer",
    "clear_composer_with_keyboard",
    "exact_composer_input",
    "prepare_exact_composer_input",
    "read_composer_value",
    "read_copilot_composer_value",
    "read_typed_composer_value",
    "type_composer_value",
    "write_composer_value",
    "write_exact_composer_input",
]
