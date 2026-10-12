"""Ordinary browser interactions on the attached session's owning thread.

The executor reports API entry and return, never business success. Deadlines are
absolute values from the injected monotonic clock. No browser is launched here.
"""

from dataclasses import dataclass, replace
from contextlib import contextmanager
import asyncio
import math
import random
import re
import threading
from urllib.parse import urlsplit
from uuid import uuid4

from automation_core.contracts import (
    ActionRequest, DispatchReceipt, DialogReceipt, PageBinding, PreparedDialogPolicy,
    SensitiveValue, TargetRef, ViewRef,
)
from automation_core.browser.browser_geometry import GEOMETRY_STATE, POINTER_GEOMETRY, SCROLL_LAYOUT, SCROLL_BY, WHEEL_SCOPE
from automation_core.browser.movement import Geometry, Stamp, Vec, rectangle
from automation_core.browser.pointer import CommandTrace, MovementTrace, PointerCheck, PointerController, PointerError
from automation_core.browser.scrolling import ScrollController, ScrollIntent, ScrollLayout
from automation_core.browser.blocker_bindings import BlockerBinding, BlockerBindingError, BlockerInputAuthority, _identifier


class InteractionError(RuntimeError):
    """A fixed, sanitized technical reason; never a browser exception message."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class InputBehavior:
    """Shared input pacing; optional work consumes only existing spare time."""

    enabled: bool = True
    maximum_extra_s: float = 1.2
    scroll_probability: float = .15

    def __post_init__(self):
        if (type(self.enabled) is not bool
                or not math.isfinite(self.maximum_extra_s) or self.maximum_extra_s < 0
                or not math.isfinite(self.scroll_probability)
                or not 0 <= self.scroll_probability <= 1):
            raise ValueError("Invalid input behavior.")


class _InputBehavior:
    """One action's soft cutoff, using the driver's existing pointer ledger.

    Bounding boxes guide motion only. The ordinary retained target, final purpose
    check and native locator still decide where activation is allowed.
    """

    def __init__(self, driver, policy, rng, binding, view, deadline, reserve, state):
        self.driver, self.policy, self.rng = driver, policy, rng
        self.binding, self.view, self.deadline, self.state = binding, view, deadline, state
        self.until = min(driver.clock.monotonic() + policy.maximum_extra_s, deadline - reserve)
        self.last_point = None
        self.viewport = None

    def guard(self):
        self.driver._guard(self.deadline)
        self.driver._check_input_binding(self.view, self.binding)
        if self.state["reason"] is not None:
            raise InteractionError(self.state["reason"])

    def room(self, seconds=0.):
        self.guard()
        return self.driver.clock.monotonic() + seconds < self.until

    def pause(self, seconds):
        remaining = seconds
        while remaining > 0 and self.room():
            interval = min(.02, remaining, self.until - self.driver.clock.monotonic())
            if interval <= 0:
                return
            self.view.page.wait_for_timeout(1000 * interval)
            remaining -= interval  # Also bounded with a manually advanced test clock.

    def command(self, kind, callback, point=None):
        self.guard()
        now = self.driver.clock.monotonic()
        trace = self.state["movement"] or MovementTrace()
        command = CommandTrace(kind, now, requested=point)
        self.state["movement"] = replace(trace, commands=(*trace.commands, command))
        # Ambient movement belongs to its trace. The business-action receipt
        # enters only when the requested native operation begins.
        callback()
        returned = self.driver.clock.monotonic()
        self.state["movement"] = replace(trace,
            commands=(*trace.commands, replace(command, returned_at=returned)))
        return now, returned

    def bounds(self, locator):
        if not self.room(.04):
            return None
        # A small read, with no DOM inventory or repeated geometry certification.
        try:
            box = locator.bounding_box(timeout=max(1, int(1000 * min(.15,
                self.until - self.driver.clock.monotonic()))))
            viewport = self.view.page.viewport_size
            if viewport is None:
                with self.driver._observation_deadline(self.view.page, self.until):
                    viewport = self.view.page.evaluate("() => ({width: innerWidth, height: innerHeight})")
        except Exception:
            self.guard()
            return None  # Optional geometry must not reject an otherwise usable control.
        self.guard()
        if not box or not viewport or box["width"] <= 0 or box["height"] <= 0:
            return None
        return box, (viewport["width"], viewport["height"])

    def can_scroll(self):
        if not self.room(.04):
            return False
        try:
            # A wheel over a focused number/select can edit it instead of
            # scrolling. Skip that optional gesture; inspect no other controls.
            with self.driver._observation_deadline(self.view.page, self.until):
                allowed = self.view.page.evaluate("""() => !document.activeElement?.closest(
                    'input,textarea,select,[contenteditable]:not([contenteditable="false"]),'
                    + '[role="spinbutton"],[role="slider"],[role="listbox"],[role="combobox"]')""")
        except Exception:
            self.guard()
            return False
        return allowed is True

    def prepare(self, locator):
        geometry = self.bounds(locator)
        if geometry is None or not self.room(.08):
            return
        box, viewport = geometry
        self.viewport = viewport
        width, height = viewport
        endpoint = Vec(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        if not (0 <= endpoint.x < width and 0 <= endpoint.y < height):
            # Native locator actionability owns required scrolling into view.
            return
        # Admission already checked the local capability. Optional motion uses
        # the technical guard; run the source verifier once at actual activation.
        # Some verifiers make a one-send claim that movement must not consume.
        self.guard()
        ledger = self.driver.pointer.ledger
        stamp = Stamp(self.view.ref.view_id, self.view.document, self.view.layout)
        start = ledger.state.position
        if (ledger.binding != self.binding or ledger.viewport != viewport
                or ledger.state.stamp is None
                or (ledger.state.stamp.view, ledger.state.stamp.document) != (stamp.view, stamp.document)):
            start = None

        def move(point):
            ledger.invalidate("move_in_flight")
            begin, end = self.command("move", lambda: self.driver.pointer_move(
                self.binding, point.x, point.y), point)
            ledger.commanded(point, stamp, self.binding, begin, end, viewport)
            self.last_point = point

        if start is None:
            # Initialize an unknown cursor explicitly; never invent a start location.
            start = Vec(width / 2, height / 2)
            if not self.room(.04):
                return
            move(start)
        bend = self.rng.uniform(-35., 35.)
        steps = self.rng.randint(5, 9)
        for index in range(1, steps + 1):
            if not self.room(.02):
                break
            t = index / steps
            eased = t * t * (3 - 2 * t)
            point = Vec(max(1., min(width - 1., start.x + (endpoint.x - start.x) * eased)),
                        max(1., min(height - 1., start.y + (endpoint.y - start.y) * eased
                                    + bend * math.sin(math.pi * t))))
            move(point)
            self.pause(self.rng.uniform(.012, .025))
        if (self.room(.16) and self.rng.random() < self.policy.scroll_probability
                and self.can_scroll() and self.room(.1)):
            # One short wheel gesture. No random controls, history navigation or
            # click pairs are hidden inside the business action's receipt.
            delta = self.rng.choice((-1, 1)) * self.rng.randint(35, 90)
            self.command("wheel", lambda: self.view.page.mouse.wheel(0, delta))
            self.pause(.08)
            # Scrolling can move the target. Aim at its fresh box once, then let
            # the final native target check/actionability handle any later change.
            geometry = self.bounds(locator)
            if geometry is not None and self.room(.02):
                box, _ = geometry
                point = Vec(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                if 0 <= point.x < width and 0 <= point.y < height:
                    move(point)
        self.pause(self.rng.uniform(.03, .08))

    def after_locator(self):
        # Locator actionability may have moved the cursor. One explicit move to
        # the last waypoint re-establishes continuity for the next action, only
        # on this same document and while optional time remains.
        if (self.last_point is None or self.view.page.is_closed()
                or self.driver._input_document != (self.view.ref.view_id, self.view.document)
                or _origin(self.view.page.url) != _origin(self.binding.origin)
                or not self.room(.02)):
            return
        point = self.last_point
        begin, end = self.command("move", lambda: self.driver.pointer_move(
            self.binding, point.x, point.y), point)
        self.driver.pointer.ledger.commanded(point,
            Stamp(self.view.ref.view_id, self.view.document, self.view.layout),
            self.binding, begin, end, self.viewport)

    def type(self, locator, value):
        # A single admitted fill with the exact node and original hard deadline.
        # Each character is sent once. Cancellation/partial failure is left to
        # the existing uncertain-effect recovery; there is no fill fallback.
        for key in ("ControlOrMeta+A", "Backspace"):
            self.guard()
            locator.press(key, timeout=self.driver._timeout_ms(self.deadline))
        for character in value:
            self.guard()
            locator.type(character, delay=0, timeout=self.driver._timeout_ms(self.deadline))
            self.pause(self.rng.uniform(.035, .075))


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    port = parts.port
    if port is None:
        port = {"http": 80, "https": 443}.get(parts.scheme)
    return parts.scheme, parts.hostname or "", port


@dataclass
class _View:
    page: object
    ref: ViewRef
    owned: bool
    document: int = 0
    layout: int = 0
    observed_counter: int | None = None
    pointer_signature: tuple | None = None


@dataclass(frozen=True)
class _ObservationBudget:
    driver: object
    until: float

    def checkpoint(self):
        return self.until - self.driver._guard(self.until, completion=True)

    def timeout_ms(self, cap_ms=None):
        remaining = self.checkpoint()
        if cap_ms is not None:
            if type(cap_ms) not in (int, float) or not math.isfinite(cap_ms) or cap_ms <= 0:
                raise InteractionError("invalid_deadline")
            remaining = min(remaining, cap_ms / 1000)
        value = math.floor(remaining * 1000)
        if value < 1:
            raise InteractionError("insufficient_call_budget")
        return min(value, 30_000)

    def timeout_s(self, cap_s=None):
        remaining = self.checkpoint()
        if cap_s is not None:
            if type(cap_s) not in (int, float) or not math.isfinite(cap_s) or cap_s <= 0:
                raise InteractionError("invalid_deadline")
            remaining = min(remaining, cap_s)
        if remaining < .001:
            raise InteractionError("insufficient_call_budget")
        return remaining


class _RetainedInputTarget:
    """Exact native node with deadline-checked read-only handle evaluation."""

    def __init__(self, driver, target, handle, deadline):
        self.driver, self.target, self.handle, self.deadline = driver, target, handle, deadline

    def evaluate(self, expression, arg=None, *, timeout=None):
        # ElementHandle.evaluate has no timeout option. Cancel only the
        # read's protocol wait at its original deadline; retain the exact node.
        self.driver._guard(self.deadline, completion=True)
        with self.driver._observation_deadline(self.handle, self.deadline):
            return self.handle.evaluate(expression, arg)

    def bounding_box(self, *, timeout=None):
        # Unlike Locator.bounding_box, ElementHandle.bounding_box has no timeout
        # parameter. Bound its read without replacing the retained target.
        until = self.deadline
        if timeout is not None:
            until = min(until, self.driver.clock.monotonic() + timeout / 1000)
        with self.driver._observation_deadline(self.handle, until):
            return self.handle.bounding_box()

    def __getattr__(self, name):
        return getattr(self.handle, name)


class BrowserDriver:
    """The only workflow adapter touching Playwright; it owns no session locks.

    Read-only methods admit completion observation after cancellation, but still
    require a live lease and positive deadline. Their returned evidence is not
    discarded merely because an uninterruptible read finished late.
    """

    def __init__(self, session, clock, cancellation, ownership):
        self.session = session
        self.clock = clock
        self.cancellation = cancellation
        self.ownership = ownership
        self.generation = uuid4().hex
        self._thread_id = threading.get_ident()
        self._views: dict[str, _View] = {}
        self._input_active = False
        self._input_behavior = None
        self._native_input_guard = None
        self._physical_input_guard = None
        self._source_verification_deadline = None
        self._input_targets = {}
        self._quarantined = False
        self.pointer = PointerController(self)
        self._input_document: tuple[str, int] | None = None
        self._input_created_view: ViewRef | None = None
        self._blocker_bindings = {}
        self._blocker_input = None
        self._blocker_dispatch_claims = set()
        self._navigation_scope = None
        self._navigation_failures = {}

    def assert_owner(self) -> None:
        if threading.get_ident() != self._thread_id:
            raise InteractionError("wrong_thread")
        try:
            self.ownership.assert_owned()
        except Exception:
            raise InteractionError("ownership_lost") from None

    def _guard(self, deadline: float, *, completion: bool = False) -> float:
        self.assert_owner()
        if isinstance(deadline, bool) or not isinstance(deadline, (float, int)):
            raise InteractionError("invalid_deadline")
        try:
            finite = math.isfinite(deadline)
        except OverflowError:
            finite = False
        if not finite:
            raise InteractionError("invalid_deadline")
        if not completion and self.cancellation.is_set():
            raise InteractionError("cancelled")
        if not completion and self._physical_input_guard is not None:
            try:
                self._physical_input_guard()
            except Exception:
                self.cancellation.set()
                raise InteractionError("input_protection_lost") from None
        now = self.clock.monotonic()
        if now >= deadline:
            raise InteractionError("deadline_exhausted")
        return now

    def _timeout_ms(self, deadline: float, *, completion: bool = False) -> int:
        now = self._guard(deadline, completion=completion)
        value = math.floor(1000 * (deadline - now))
        if value < 1:
            raise InteractionError("insufficient_call_budget")
        # A generous run allowance must not become an hours-long native wait.
        # Keep the original deadline authoritative when it is closer.
        return min(value, 30_000)

    def bind_page(self, page, *, owned: bool = False) -> ViewRef:
        """Register an already supplied page; never promote a borrowed page."""
        self.assert_owner()
        if type(owned) is not bool:
            raise InteractionError("invalid_view")
        for view in self._views.values():
            if view.page is page:
                return view.ref
        if page.context is not self.session.context or page.is_closed():
            raise InteractionError("invalid_view")
        ref = ViewRef(uuid4().hex, self.generation)
        self._views[ref.view_id] = _View(page, ref, owned)
        if callable(getattr(page, "on", None)):
            def navigated(frame):
                if frame is page.main_frame:
                    view = self._views[ref.view_id]
                    view.document += 1
                    view.layout += 1
                    view.observed_counter = None
                    view.pointer_signature = None
                    self.pointer.invalidate("document_changed")
            page.on("framenavigated", navigated)
        return ref



    def discover_views(self, *, deadline: float) -> tuple[ViewRef, ...]:
        """Freshly enumerate session tabs; newly discovered tabs are borrowed.

        Discovery provides candidates, never readiness or identity evidence. A
        page created by an API that raised without returning its page is also
        borrowed: a context-page delta cannot establish task ownership.
        """
        self._guard(deadline, completion=True)
        try:
            pages = tuple(self.session.context.pages)
            refs = []
            for page in pages:
                self._guard(deadline, completion=True)
                if not page.is_closed():
                    refs.append(self.bind_page(page))
            return tuple(refs)
        except InteractionError:
            raise
        except Exception:
            raise InteractionError("observation_failed") from None

    def view_owned(self, ref: ViewRef, *, deadline: float) -> bool:
        self._guard(deadline, completion=True)
        return self._view(ref).owned

    def document_generation(self, ref: ViewRef, *, deadline: float) -> int:
        self._guard(deadline, completion=True)
        return self._view(ref).document

    def navigation_failure_for(self, receipt, *, operation_id, attempt_id,
                               view_ref, destination_id):
        """Return only this driver's settled exact-attempt failure proof.

        A copied receipt, historical document or matching error string cannot
        reconstruct a proof. This owner-thread lookup sends no browser command.
        """
        self.assert_owner()
        saved = self._navigation_failures.get(id(receipt))
        if saved is None or saved.receipt is not receipt or self._quarantined or self._input_active:
            return None
        if (saved.operation_id, saved.attempt_id, saved.view_ref, saved.destination_id) != (
                operation_id, attempt_id, view_ref, destination_id):
            return None
        try:
            if self._view(view_ref).document != saved.document_generation:
                return None
        except InteractionError:
            return None
        return saved

    def _scoped_navigation(self, request, view, timeout, scope):
        """Observe exact failed network entry inside the existing goto call.

        These callbacks are passive. They never publish common assessments,
        inspect a native dialog, retry navigation or use browser error wording.
        """
        page = view.page
        initial_request = None
        listening, in_call, terminal_error = False, False, False

        def requested(native_request):
            nonlocal initial_request
            try:
                self.assert_owner()
                if (in_call and initial_request is None and self._navigation_scope is scope
                        and self._input_active and native_request.is_navigation_request()
                        and native_request.frame is page.main_frame
                        and native_request.url == request.url and native_request.redirected_from is None):
                    initial_request = native_request
            except Exception:
                # Inability to establish exact event identity denies evidence.
                scope["event_invalid"] = True

        def failed(native_request):
            try:
                self.assert_owner()
                if (in_call and initial_request is native_request and self._navigation_scope is scope
                        and self._input_active and native_request.is_navigation_request()
                        and native_request.frame is page.main_frame and native_request.url == request.url):
                    scope["failed"] = True
            except Exception:
                scope["event_invalid"] = True

        def navigated(frame):
            nonlocal terminal_error
            try:
                self.assert_owner()
                if (self._navigation_scope is scope and scope["failed"]
                        and frame is page.main_frame and frame.url == "chrome-error://chromewebdata/"):
                    terminal_error = True
            except Exception:
                scope["event_invalid"] = True

        installed = []
        try:
            for event, listener in (("request", requested), ("requestfailed", failed), ("framenavigated", navigated)):
                page.on(event, listener)
                installed.append((event, listener))
        except Exception:
            # Optional provenance must not prevent the original mechanical call.
            # No browser input has entered here, so this is one ordinary goto,
            # with no failure evidence from unsupported or partial event ports.
            scope["event_invalid"] = True
            for event, listener in installed:
                try:
                    page.remove_listener(event, listener)
                except Exception:
                    pass
            page.goto(request.url, wait_until="domcontentloaded", timeout=timeout)
            return
        try:
            listening = True
            in_call = True
            page.goto(request.url, wait_until="domcontentloaded", timeout=timeout)
        finally:
            in_call = False
            if scope["failed"] and not scope["event_invalid"] and not self._quarantined:
                # Chromium can queue its terminal error document after goto
                # rejects. Settle that same started navigation inside the
                # original cutoff before assigning the proof's document.
                # This waits for one exact event; it is no poll or retry loop.
                try:
                    if not terminal_error:
                        page.wait_for_event("framenavigated", predicate=lambda frame:
                            frame is page.main_frame and frame.url == "chrome-error://chromewebdata/",
                            timeout=self._timeout_ms(scope["deadline"], completion=True))
                except Exception:
                    # Positive failure remains historical, but uncertain terminal
                    # document continuity cannot be promoted to current proof.
                    scope["event_invalid"] = True
            scope["returned_at"] = self.clock.monotonic()
            scope["settled"] = listening
            for event, listener in (("request", requested), ("requestfailed", failed), ("framenavigated", navigated)):
                try:
                    page.remove_listener(event, listener)
                except Exception:
                    scope["event_invalid"] = True

    def _view(self, ref: ViewRef) -> _View:
        self.assert_owner()
        browser = getattr(self.session, '_browser', None)
        if browser is not None and not browser.is_connected():
            raise InteractionError('connection_lost')
        if (not isinstance(ref, ViewRef) or type(ref.view_id) is not str
                or type(ref.generation) is not str or ref.generation != self.generation):
            raise InteractionError("stale_view")
        view = self._views.get(ref.view_id)
        if view is None or view.ref != ref or view.page.is_closed():
            raise InteractionError("stale_view")
        if view.page.context is not self.session.context:
            raise InteractionError("invalid_view")
        return view

    def _locator(self, target: TargetRef, deadline: float, *, unique=False):
        self._guard(deadline, completion=True)
        if not isinstance(target, TargetRef) or not isinstance(target.selector, str) or not target.selector:
            raise InteractionError("invalid_target")
        locator = self._view(target.view).page.locator(target.selector)
        if unique:
            self._guard(deadline, completion=True)
            if locator.count() != 1:
                raise InteractionError("target_not_unique")
        return locator

    def _check_input_binding(self, view: _View, binding: PageBinding | BlockerBinding | None = None) -> None:
        """Recheck the admitted document after browser I/O, including late reads."""
        self._view(view.ref)
        if self._input_document is not None and self._input_document != (view.ref.view_id, view.document):
            self.pointer.invalidate("binding_changed")
            raise InteractionError("binding_changed")
        if binding is not None and _origin(view.page.url) != _origin(binding.origin):
            self.pointer.invalidate("binding_origin_changed")
            raise InteractionError("binding_origin_changed")

    def _retain_input_target(self, target, deadline):
        if target in self._input_targets:
            return self._input_targets[target]
        locator = self._locator(target, deadline, unique=True)
        handle = locator.element_handle(timeout=self._timeout_ms(deadline))
        if handle is None:
            raise InteractionError("binding_changed")
        retained = _RetainedInputTarget(self, target, handle, deadline)
        self._input_targets[target] = retained
        self._validate_input_targets(deadline)
        return retained

    def _validate_input_targets(self, deadline):
        for target, retained in self._input_targets.items():
            self._guard(deadline)
            view = self._view(target.view)
            self._check_input_binding(view)
            if retained.evaluate("""(element, selector) => element.isConnected
                    && document.querySelectorAll(selector).length === 1
                    && document.querySelector(selector) === element""", target.selector) is not True:
                raise InteractionError("binding_changed")
            self._guard(deadline)
            self._check_input_binding(view)

    def _input_locator(self, target, deadline, *, unique=True):
        if self._native_input_guard is not None:
            if self._blocker_input is not None and target == self._blocker_input[0].target:
                handle = self._blocker_bindings[self._blocker_input[0].retention_id][2]
                return _RetainedInputTarget(self, target, handle, deadline)
            retained = self._input_targets.get(target)
            if retained is None:
                raise InteractionError("binding_changed")
            return retained
        return self._locator(target, deadline, unique=unique)

    def _before_native_input(self, binding, deadline, state):
        if self._source_verification_deadline is not None:
            raise InteractionError("input_busy")
        if self._native_input_guard is not None:
            self._native_input_guard(binding, deadline, state)

    def _native_method_allowed(self):
        self.assert_owner()
        if self._source_verification_deadline is not None:
            raise InteractionError("input_busy")

    def retain_blocker_binding(self, root: TargetRef, target: TargetRef, *, origin,
                               rule_id, rule_version, observation_id, choice_id, deadline,
                               root_handle=None, control_handle=None):
        """Retain exact main-document nodes observed by a read-only recognizer.

        Handles provided by the scan are checked against fresh selectors, so an
        identical replacement cannot silently become the observed root/control.
        The driver owns retained handles until explicit release or session end.
        """
        self._guard(deadline, completion=True)
        if self._input_active or self._quarantined:
            raise InteractionError("input_busy" if self._input_active else "input_quarantined")
        if (type(root) is not TargetRef or type(target) is not TargetRef or root.view != target.view
                or type(origin) is not str or _origin(origin)[0] not in {"http", "https"}
                or not _origin(origin)[1] or not _identifier(rule_id) or not _identifier(choice_id)
                or type(rule_version) is not int or not 0 < rule_version <= 2**63 - 1
                or type(observation_id) is not str or not 1 <= len(observation_id) <= 128):
            raise InteractionError("invalid_blocker_binding")
        view = self._view(root.view)
        document = view.document
        if _origin(view.page.url) != _origin(origin):
            raise InteractionError("binding_origin_changed")
        acquired = []
        binding = None
        try:
            if root_handle is None:
                root_handle = self._locator(root, deadline, unique=True).element_handle(
                    timeout=self._timeout_ms(deadline, completion=True))
                acquired.append(root_handle)
            if control_handle is None:
                control_handle = self._locator(target, deadline, unique=True).element_handle(
                    timeout=self._timeout_ms(deadline, completion=True))
                acquired.append(control_handle)
            binding = BlockerBinding(view.ref, origin, document, root, target, rule_id,
                rule_version, observation_id, choice_id, uuid4().hex)
            self._blocker_bindings[binding.retention_id] = (binding, root_handle, control_handle)
            self.validate_blocker_binding(binding, deadline=deadline)
            return binding
        except InteractionError:
            if binding is not None:
                self._blocker_bindings.pop(binding.retention_id, None)
            for handle in acquired:
                if handle is not None:
                    try:
                        handle.dispose()
                    except Exception:
                        pass
            raise
        except Exception:
            if binding is not None:
                self._blocker_bindings.pop(binding.retention_id, None)
            for handle in acquired:
                if handle is not None:
                    try:
                        handle.dispose()
                    except Exception:
                        pass
            raise InteractionError("blocker_observation_failed") from None

    def validate_blocker_binding(self, binding, *, deadline, input_guard=False):
        self._guard(deadline, completion=not input_guard)
        if type(binding) is not BlockerBinding:
            raise InteractionError("invalid_blocker_binding")
        retained = self._blocker_bindings.get(binding.retention_id)
        if retained is None or retained[0] is not binding:
            raise InteractionError("stale_blocker_binding")
        view = self._view(binding.view)
        if view.document != binding.document_generation:
            raise InteractionError("binding_changed")
        self._check_input_binding(view, binding)
        if input_guard:
            if self._quarantined:
                raise InteractionError("input_quarantined")
            if self._blocker_input is None or self._blocker_input[0] is not binding:
                raise InteractionError("blocker_authority_required")
            self._blocker_input[1].require(self, binding)
        root, control = retained[1:]
        root_now = control_now = None
        try:
            if root.owner_frame() is not view.page.main_frame or control.owner_frame() is not view.page.main_frame:
                raise InteractionError("cross_frame_blocker")
            # Locator handles are temporary readback objects, never new targets.
            try:
                root_now = self._locator(binding.root, deadline, unique=True).element_handle(
                    timeout=self._timeout_ms(deadline, completion=not input_guard))
                control_now = self._locator(binding.target, deadline, unique=True).element_handle(
                    timeout=self._timeout_ms(deadline, completion=not input_guard))
                result = root.evaluate("""(root, spec) => {
                    if (root.ownerDocument !== document || spec.control.ownerDocument !== document)
                        return 'cross_frame_blocker';
                    if (!root.isConnected || root !== spec.rootNow) return 'root_changed';
                    if (!spec.control.isConnected || spec.control !== spec.controlNow
                            || !root.contains(spec.control)) return 'blocker_control_changed';
                    const visible = e => { const s = getComputedStyle(e), r = e.getBoundingClientRect();
                        return s.display !== 'none' && !['hidden','collapse'].includes(s.visibility)
                            && r.width > 0 && r.height > 0; };
                    if (!visible(root) || !visible(spec.control) || spec.control.matches(':disabled')
                            || spec.control.closest('[inert], [aria-disabled="true"], [hidden]'))
                        return 'blocker_control_inaccessible';
                    return 'same';
                }""", {"control": control, "rootNow": root_now, "controlNow": control_now})
            finally:
                for handle in (root_now, control_now):
                    if handle is not None:
                        handle.dispose()
            self._check_input_binding(self._view(binding.view), binding)
            if self._view(binding.view).document != binding.document_generation:
                raise InteractionError("binding_changed")
            if result != "same":
                raise InteractionError(result if result in {"cross_frame_blocker", "root_changed",
                    "blocker_control_changed", "blocker_control_inaccessible"} else "blocker_observation_failed")
            self._guard(deadline, completion=not input_guard)
            return True
        except (InteractionError, BlockerBindingError):
            raise
        except Exception:
            raise InteractionError("blocker_observation_failed") from None

    def release_blocker_binding(self, binding):
        self.assert_owner()
        retained = self._blocker_bindings.get(getattr(binding, "retention_id", None))
        if retained is None or retained[0] is not binding:
            return
        self._blocker_bindings.pop(binding.retention_id)
        for handle in retained[1:]:
            # A scan can retain several configured choices under the same
            # observed root handle. Release only its last retained reference.
            if any(handle is other for entry in self._blocker_bindings.values() for other in entry[1:]):
                continue
            try:
                handle.dispose()
            except Exception:
                pass

    def _dispatch(self, request: ActionRequest, view: _View | None, locator,
                  value: str | None, timeout: int) -> ViewRef | None:
        """One already-admitted input call; the executor marks entry first."""
        self._native_method_allowed()
        kind = request.kind
        if kind == "click":
            if self._blocker_input is not None:
                # The exact handle prevents Playwright's locator retry from
                # activating a replacement node during actionability waiting.
                locator = self._blocker_bindings[self._blocker_input[0].retention_id][2]
            locator.click(force=False, timeout=timeout)
        elif kind == "hover":
            locator.hover(force=False, timeout=timeout)
        elif kind == "fill":
            if self._input_behavior is not None:
                self._input_behavior.type(locator, value)
            else:
                locator.fill(value, timeout=timeout)
        elif kind == "selectOption":
            locator.select_option(value=request.option_value, timeout=timeout)
        elif kind == "setChecked":
            locator.set_checked(request.checked, force=False, timeout=timeout)
        elif kind == "setSlider":
            # The component supplies its observed track, avoiding guesses about
            # a custom slider's parent structure. All motion stays in this input.
            track = self._input_locator(request.slider_track, locator.deadline, unique=True)
            thumb_box, track_box = locator.bounding_box(timeout=timeout), track.bounding_box(timeout=timeout)
            if not thumb_box or not track_box or track_box['width'] <= 0:
                raise InteractionError('unsupported_control')
            x = thumb_box['x'] + thumb_box['width'] / 2
            y = thumb_box['y'] + thumb_box['height'] / 2
            end = track_box['x'] + track_box['width'] * request.slider_fraction
            mouse = view.page.mouse
            mouse.move(x, y)
            mouse.down()
            try:
                for index in range(1, 13):
                    self._guard(locator.deadline)
                    self._check_input_binding(view)
                    mouse.move(x + (end - x) * index / 12, y)
            finally:
                mouse.up()
        elif kind == "uploadImage":
            asset = request.asset
            locator.set_input_files({"name": "image.png" if asset.media_type == "image/png" else "image.jpg",
                                     "mimeType": asset.media_type, "buffer": asset.data}, timeout=timeout)
        elif kind == "navigate":
            scope = self._navigation_scope
            if scope is not None and scope["request"] is request and scope["view"] is view:
                self._scoped_navigation(request, view, timeout, scope)
            else:
                view.page.goto(request.url, wait_until="domcontentloaded", timeout=timeout)
        elif kind == "openTab":
            # Keep exact returned-page evidence even if a surrounding adapter
            # raises after this call. Never infer ownership from page discovery.
            from automation_core.browser.background_page import open_background_page
            from automation_core.storage.models import BudgetWindow
            budget = BudgetWindow(self.clock.monotonic() + timeout / 1000,
                monotonic=self.clock.monotonic)
            self._input_created_view = self.bind_page(
                open_background_page(self.session.context, budget=budget), owned=True)
            return self._input_created_view
        elif kind == "activateTab":
            # Selecting the executor's exact view needs no desktop activation.
            # Playwright routes input to that Page even while it is backgrounded.
            # Keep this saved step/receipt without stealing the user's focus.
            return None
        elif kind in {"closeOwnedTab", "closeTab"}:
            view.page.close(run_before_unload=request.run_before_unload)
        return None

    @contextmanager
    def dialog_scope(self, binding, prepared_policy, receipt_builder: dict,
                     deadline: float):
        """Resolve native modals within the admitted, owner-thread interaction.

        Listeners are context-scoped so unexpected dialogs from another tab cannot
        silently authorize an effect. Matching additionally requires the exact page.
        """
        self.assert_owner()
        state = receipt_builder
        expected_page = None
        if prepared_policy is not None:
            if type(prepared_policy) is not PreparedDialogPolicy:
                raise InteractionError("invalid_dialog_policy")
            from automation_core.storage.persistence import validate
            try:
                validate(prepared_policy.policy)
            except Exception:
                raise InteractionError("invalid_dialog_policy") from None
            ref = binding.view if isinstance(binding, (PageBinding, BlockerBinding)) else binding
            if prepared_policy.view != ref:
                raise InteractionError("dialog_view_mismatch")
            expected_page = self._view(ref).page
            policy = prepared_policy.policy
            if not callable(prepared_policy.matches_message) or not callable(prepared_policy.require_acceptance):
                raise InteractionError("invalid_dialog_policy")
            if ((policy.prompt_ref is None) != (prepared_policy.prompt_value is None)
                    or (prepared_policy.prompt_value is not None
                        and type(prepared_policy.prompt_value) is not SensitiveValue)):
                raise InteractionError("invalid_dialog_policy")
        resolved_expected = 0

        def latch(reason):
            if state["reason"] is None or reason in {"ownership_lost", "wrong_thread"}:
                state["reason"] = reason

        def on_dialog(dialog):
            nonlocal resolved_expected
            state["dialog_count"] += 1
            kind, expected, response = "unknown", False, "unresolved"
            entered, returned, reason = None, None, None
            try:
                self.assert_owner()
                observed_type = dialog.type
                if observed_type in {"alert", "confirm", "prompt", "beforeunload"}:
                    kind = observed_type
                if prepared_policy is not None:
                    policy = prepared_policy.policy
                    if dialog.page is expected_page and kind == policy.dialog_type:
                        try:
                            matched = prepared_policy.matches_message(dialog.message)
                            if type(matched) is not bool:
                                raise ValueError()
                            expected = matched
                        except Exception:
                            reason = "invalid_dialog_policy"
                    if state["dialog_count"] > policy.max_count:
                        reason = "dialog_limit"
                if not expected and reason is None:
                    reason = "unexpected_dialog"
                if reason is None and state["reason"] is not None:
                    reason = "dialog_sequence_stopped"
                response = "dismiss"
                if expected and reason is None:
                    response = prepared_policy.policy.response
                    if response == "accept":
                        try:
                            self._guard(deadline)
                            prepared_policy.require_acceptance()
                            self._guard(deadline)
                        except Exception as error:
                            raw = getattr(error, "reason", None)
                            raw = getattr(raw, "value", raw)
                            if type(raw) is not str:
                                raw = None
                            raw = {"completion_only": "deadline_exhausted",
                                   "wrong_owner_thread": "wrong_thread",
                                   "wall_clock_discontinuity": "clock_untrusted",
                                   "clock_reversed": "clock_untrusted"}.get(raw, raw)
                            reason = raw if raw in {
                                "cancelled", "deadline_exhausted", "input_expired", "input_unavailable",
                                "clock_untrusted", "storage_unavailable", "ownership_lost", "wrong_thread",
                            } else "dialog_guard_denied"
                            response = "dismiss"
                if reason in {"ownership_lost", "wrong_thread"}:
                    raise InteractionError(reason)
                # A deadline can prohibit acceptance, but never grants foreign input.
                self.assert_owner()
                entered = self.clock.monotonic()
                if state["status"] == "not_attempted":
                    state.update(status="attempted", entered_at=entered)
                if response == "accept":
                    prompt = prepared_policy.prompt_value
                    if prompt is None:
                        dialog.accept()
                    else:
                        dialog.accept(prompt_text=prompt.reveal())
                else:
                    dialog.dismiss()
                returned = self.clock.monotonic()
                if expected:
                    resolved_expected += 1
                if returned >= deadline and reason is None:
                    # Entry was admitted. A late API return is dispatch evidence,
                    # distinct from a guard that denied acceptance at the cutoff.
                    latch("deadline_exhausted")
                elif self.cancellation.is_set() and reason is None:
                    reason = "cancelled"
            except InteractionError as error:
                reason = error.reason
                self._quarantined = True
            except Exception:
                reason = "dialog_unsettled"
                self._quarantined = True
            finally:
                if reason is not None:
                    latch(reason)
                if len(state["dialogs"]) < 64:
                    state["dialogs"] += (DialogReceipt(kind, expected, response, entered, returned, reason),)

        context = self.session.context
        try:
            context.on("dialog", on_dialog)
        except Exception:
            self._quarantined = True
            raise InteractionError("dialog_unsettled") from None
        try:
            yield
        finally:
            try:
                if (prepared_policy is not None and prepared_policy.policy.expect_dialog
                        and resolved_expected == 0 and not self._quarantined):
                    while resolved_expected == 0:
                        try:
                            now = self._guard(deadline)
                            if expected_page.is_closed():
                                latch("expected_dialog_missing")
                                break
                            expected_page.wait_for_timeout(min(5., 1000 * (deadline - now)))
                        except InteractionError as error:
                            latch("expected_dialog_missing" if error.reason == "deadline_exhausted" else error.reason)
                            break
                        except Exception:
                            # A successful beforeunload response can close the page
                            # that is pumping this wait; that is settled evidence.
                            if (resolved_expected and prepared_policy.policy.dialog_type == "beforeunload"
                                    and expected_page.is_closed()):
                                break
                            latch("dialog_unsettled")
                            self._quarantined = True
                            break
            finally:
                # Playwright listener removal is local event-emitter housekeeping.
                # It sends no browser command and never resolves a foreign modal.
                try:
                    context.remove_listener("dialog", on_dialog)
                except Exception:
                    latch("dialog_unsettled")
                    self._quarantined = True

    def _release_left_button(self, view: _View | None, state: dict) -> None:
        """One best-effort release; no deadline/cancellation can roll it back."""
        self.assert_owner()
        if view is None or view.page.is_closed():
            raise InteractionError("cleanup_unavailable")
        self._check_input_binding(self._view(view.ref))
        state["cleanup_attempted"] = True
        view.page.mouse.up(button="left")
        state["cleanup_returned"] = True

    def invalidate_pointer(self) -> None:
        """Call on manual intervention or another controller's input handoff."""
        self.assert_owner()
        self.pointer.invalidate("external_input")

    def _geometry_key(self, view: _View) -> str:
        return "__automation_geometry_" + self.generation + view.ref.view_id

    def _observe_layout_counter(self, view: _View, counter: int) -> None:
        if type(counter) is not int or counter < 0:
            raise InteractionError("geometry_observation_failed")
        if view.observed_counter is not None and counter != view.observed_counter:
            view.layout += 1
        view.observed_counter = counter

    def pointer_guard(self, binding: PageBinding, deadline: float) -> None:
        self._pointer_guard(binding, deadline, read_blocker=True)

    def pointer_postproof_guard(self, binding: PageBinding, deadline: float) -> None:
        """Local state only; the final source proof must be the last browser read."""
        self._pointer_guard(binding, deadline, read_blocker=False)

    def _pointer_guard(self, binding, deadline, *, read_blocker):
        self._guard(deadline)
        if self._quarantined:
            raise InteractionError("input_quarantined")
        view = self._view(binding.view)
        self._check_input_binding(view, binding)
        if type(binding) is BlockerBinding:
            if read_blocker:
                self.validate_blocker_binding(binding, deadline=deadline, input_guard=True)
            else:
                self._validate_blocker_input_local(binding, deadline)
        pointer = self.pointer.ledger.state
        if (pointer.stamp is not None and pointer.stamp.view == binding.view.view_id
                and pointer.stamp.document != view.document):
            self.pointer.invalidate("binding_changed")
            raise InteractionError("binding_changed")

    def _validate_blocker_input_local(self, binding, deadline):
        """Recheck admitted retention/authority without reading browser nodes."""
        self._guard(deadline)
        retained = self._blocker_bindings.get(binding.retention_id)
        if retained is None or retained[0] is not binding:
            raise InteractionError("stale_blocker_binding")
        view = self._view(binding.view)
        if view.document != binding.document_generation:
            raise InteractionError("binding_changed")
        self._check_input_binding(view, binding)
        if self._quarantined:
            raise InteractionError("input_quarantined")
        if self._blocker_input is None or self._blocker_input[0] is not binding:
            raise InteractionError("blocker_authority_required")
        self._blocker_input[1].require(self, binding)

    def pointer_stamp(self, binding: PageBinding) -> Stamp:
        self.assert_owner()
        view = self._view(binding.view)
        counter = view.page.evaluate(GEOMETRY_STATE, self._geometry_key(view))
        self._observe_layout_counter(view, counter)
        return Stamp(view.ref.view_id, view.document, view.layout)

    def _pointer_geometry(self, target, binding, deadline, endpoint=None) -> Geometry:
        self.pointer_guard(binding, deadline)
        if target.view != binding.view:
            raise InteractionError("target_binding_mismatch")
        view = self._view(binding.view)
        self.pointer_stamp(binding)
        locator = self._input_locator(target, deadline, unique=True)
        timeout = self._timeout_ms(deadline)
        data = locator.evaluate(POINTER_GEOMETRY, {
            "key": self._geometry_key(view),
            "endpoint": None if endpoint is None else [endpoint.x, endpoint.y],
        }, timeout=timeout)
        self.pointer_guard(binding, deadline)
        if (type(data) is not dict or data.get("kind") not in {"certified", "unsupported", "blocked"}
                or type(data.get("node")) is not int
                or not all(type(data.get(key)) in {int, float} and math.isfinite(data[key])
                           and 0 < data[key] <= 1e6 for key in ("width", "height"))
                or type(data.get("rect")) is not list or len(data["rect"]) != 4
                or type(data.get("origin")) is not list or len(data["origin"]) != 2
                or not all(type(v) in {int, float} and math.isfinite(v) and abs(v) <= 1e6
                           for v in (*data["rect"], *data["origin"]))):
            raise InteractionError("geometry_observation_failed")
        self._observe_layout_counter(view, data["layout"])
        signature = (target.selector, data["node"], data["kind"], data["width"],
                     data["height"], tuple(data["rect"]), tuple(data["origin"]))
        if view.pointer_signature is not None and signature != view.pointer_signature:
            view.layout += 1
        view.pointer_signature = signature
        stamp = Stamp(view.ref.view_id, view.document, view.layout)
        certified = data["kind"] == "certified"
        return Geometry(data["width"], data["height"],
            (rectangle(*data["rect"]),) if certified else (), stamp,
            self.clock.monotonic(), certified_usable=certified,
            locator_origin=Vec(*data["origin"]) if certified else None)

    def prepare_pointer(self, target, binding, deadline, state) -> Geometry:
        """Read-only preparation; unsupported/offscreen geometry never invents a cell.

        Normal locator fallback may do its own actionability scrolling. Custom
        movement requires a fully visible rectangle; explicit scroll requests can
        establish that state before a later admitted movement.
        """
        return self._pointer_geometry(target, binding, deadline)

    def revalidate_pointer(self, target, binding, endpoint, previous_stamp, deadline) -> PointerCheck:
        geometry = self._pointer_geometry(target, binding, deadline, endpoint)
        if (geometry.stamp.view, geometry.stamp.document) != (previous_stamp.view, previous_stamp.document):
            return PointerCheck("changed", geometry)
        if endpoint is not None and not geometry.certified_usable:
            return PointerCheck("blocked", geometry)
        return PointerCheck("same" if geometry.stamp == previous_stamp else "changed", geometry)

    def certify_pointer_continuity(self, ledger, geometry, binding) -> bool:
        self.assert_owner()
        pointer = ledger.state
        return (pointer.position is not None and pointer.provenance in {"commanded", "observed"}
            and pointer.exclusive_owner and pointer.stamp is not None
            and ledger.binding == binding and ledger.viewport == (geometry.width, geometry.height)
            and (pointer.stamp.view, pointer.stamp.document) == (geometry.stamp.view, geometry.stamp.document)
            and not self._quarantined)

    def pointer_move(self, binding, x, y) -> None:
        self._native_method_allowed()
        # PointerController has just run pointer_guard before marking API entry.
        # No observation I/O belongs between that marker and effect-bearing input.
        self._view(binding.view).page.mouse.move(x, y, steps=1)

    def pointer_locator(self, binding, target, kind, position, button, delay_ms, timeout_ms) -> None:
        self._native_method_allowed()
        if type(binding) is BlockerBinding:
            if kind != "click" or target != binding.target:
                raise InteractionError("target_binding_mismatch")
            locator = self._blocker_bindings[binding.retention_id][2]
        else:
            if self._native_input_guard is not None:
                locator = self._input_locator(target, self._input_targets[target].deadline)
            else:
                locator = self._view(binding.view).page.locator(target.selector)
        kwargs = {"force": False, "timeout": timeout_ms}
        if position is not None:
            kwargs["position"] = {"x": position.x, "y": position.y}
        if kind == "click":
            locator.click(button=button, delay=delay_ms, **kwargs)
        else:
            locator.hover(**kwargs)

    def pointer_down(self, binding, button) -> None:
        self._native_method_allowed()
        if type(binding) is BlockerBinding:
            raise InteractionError("unsupported_blocker_input")
        self._view(binding.view).page.mouse.down(button=button)

    def pointer_up(self, binding, button, *, cleanup=False) -> None:
        self._native_method_allowed()
        self._view(binding.view).page.mouse.up(button=button)

    def pointer_wait(self, binding, seconds, deadline) -> None:
        self.pointer_guard(binding, deadline)
        self._view(binding.view).page.wait_for_timeout(1000 * min(.005, seconds))

    def pointer_cleanup_allowed(self, binding) -> bool:
        try:
            self.assert_owner()
            view = self._view(binding.view)
            self._check_input_binding(view, binding)
            return True
        except InteractionError:
            return False

    def quarantine_pointer(self) -> None:
        self.pointer.invalidate("input_quarantined")
        self._quarantined = True

    def _scroll_scope(self, scope, binding, deadline, *, completion=False):
        self._guard(deadline, completion=completion)
        view = self._view(binding.view)
        self._check_input_binding(view, binding)
        if scope is not None and scope.view != binding.view:
            raise InteractionError("target_binding_mismatch")
        locator = self._input_locator(scope or TargetRef(binding.view, "html"), deadline, unique=True)
        # Resolving uniqueness pumps browser events and can replace this document.
        self._check_input_binding(view, binding)
        return locator

    def scroll_layout(self, target, scope, binding, deadline) -> ScrollLayout:
        scope_locator = self._scroll_scope(scope, binding, deadline, completion=True)
        if target is not None and target.view != binding.view:
            raise InteractionError("target_binding_mismatch")
        locator = self._input_locator(target, deadline, unique=True) if target is not None else scope_locator
        self._check_input_binding(self._view(binding.view), binding)
        data = locator.evaluate(SCROLL_LAYOUT, {
            "scope": scope.selector if scope is not None else None,
            "has_target": target is not None,
        }, timeout=self._timeout_ms(deadline, completion=True))
        # A read can pump navigation. Preserve late observations, but never
        # relabel old-document geometry as evidence about its replacement.
        self._check_input_binding(self._view(binding.view), binding)
        if type(data) is not dict:
            raise InteractionError("unsupported_scroll_geometry")
        data["target_rect"] = tuple(data["target_rect"]) if data["target_rect"] is not None else None
        return ScrollLayout(**data, observed_at=self.clock.monotonic())

    def _enter_scroll(self, state) -> None:
        """Record technical input entry only after read-only preparation passes."""
        if state["reason"] is not None:
            raise InteractionError(state["reason"])
        if state["entered_at"] is None:
            state["entered_at"] = self.clock.monotonic()
        state.update(status="attempted", phase="scroll")

    def scroll_into_view(self, target, deadline, state) -> None:
        locator = self._input_locator(target, deadline, unique=True)
        view = self._view(target.view)
        self._check_input_binding(view)
        self._before_native_input(None, deadline, state)
        timeout = self._timeout_ms(deadline)
        self._enter_scroll(state)
        locator.scroll_into_view_if_needed(timeout=timeout)

    def scroll_by(self, scope, binding, dx, dy, deadline, state) -> None:
        locator = self._scroll_scope(scope, binding, deadline)
        self._before_native_input(binding, deadline, state)
        timeout = self._timeout_ms(deadline)
        self._enter_scroll(state)
        locator.evaluate(SCROLL_BY, {"dx": dx, "dy": dy, "document": scope is None}, timeout=timeout)

    def prepare_scroll_wheel(self, scope, binding, dx, dy, deadline) -> None:
        locator = self._scroll_scope(scope, binding, deadline)
        self.pointer_guard(binding, deadline)
        stamp = self.pointer_stamp(binding)
        ledger = self.pointer.ledger
        pointer = ledger.state
        if (pointer.position is None or pointer.provenance != "commanded"
                or not pointer.exclusive_owner or ledger.binding != binding
                or pointer.stamp is None
                or (pointer.stamp.view, pointer.stamp.document) != (stamp.view, stamp.document)):
            raise InteractionError("wheel_pointer_unknown")
        # A fresh hit test certifies current routing, including layout-only changes.
        routed = locator.evaluate(WHEEL_SCOPE, {"x": pointer.position.x, "y": pointer.position.y,
            "dx": dx, "dy": dy, "viewport": ledger.viewport,
            "document": scope is None}, timeout=self._timeout_ms(deadline))
        self.pointer_guard(binding, deadline)
        if routed is not True:
            raise InteractionError("wheel_scope_uncertified")

    def scroll_wheel(self, scope, binding, dx, dy, deadline, state) -> None:
        self._guard(deadline)
        view = self._view(binding.view)
        self._check_input_binding(view, binding)
        if self._native_input_guard is not None:
            # Routing is preparation. No browser read may follow final proof.
            self.prepare_scroll_wheel(scope, binding, dx, dy, deadline)
            self._guard(deadline)
        self._before_native_input(binding, deadline, state)
        self._enter_scroll(state)
        view.page.mouse.wheel(dx, dy)

    def url(self, view: ViewRef, *, deadline: float) -> str:
        self._guard(deadline, completion=True)
        try:
            return self._view(view).page.url
        except InteractionError:
            raise
        except Exception:
            raise InteractionError("observation_failed") from None

    def count(self, target: TargetRef, *, deadline: float) -> int:
        try:
            locator = self._locator(target, deadline)
            self._guard(deadline, completion=True)
            return locator.count()
        except InteractionError:
            raise
        except Exception:
            raise InteractionError("observation_failed") from None

    def visible(self, target: TargetRef, *, deadline: float) -> bool:
        try:
            locator = self._locator(target, deadline, unique=True)
            self._guard(deadline, completion=True)
            return locator.is_visible()
        except InteractionError:
            raise
        except Exception:
            raise InteractionError("observation_failed") from None

    def attribute(self, target: TargetRef, name: str, *, deadline: float) -> str | None:
        try:
            locator = self._locator(target, deadline, unique=True)
            timeout = self._timeout_ms(deadline, completion=True)
            return locator.get_attribute(name, timeout=timeout)
        except InteractionError:
            raise
        except Exception:
            raise InteractionError("observation_failed") from None

    def text(self, target: TargetRef, *, deadline: float) -> str:
        try:
            locator = self._locator(target, deadline, unique=True)
            timeout = self._timeout_ms(deadline, completion=True)
            return locator.inner_text(timeout=timeout)
        except InteractionError:
            raise
        except Exception:
            raise InteractionError("observation_failed") from None

    def observe_page(self, view_or_binding, observer, *, deadline: float):
        """Guarded read-only adapter port for existing component observers.

        The callback is trusted component code, subject to the same read-only
        contract as PageComponent.observe. It receives the raw page only inside
        this owning-thread port, with a borrowed finite budget. No input ownership
        or workflow admission is granted by the callback.
        """
        self._guard(deadline, completion=True)
        if self._input_active and (self._source_verification_deadline is None
                or deadline > self._source_verification_deadline):
            raise InteractionError("input_busy")
        if not callable(observer):
            raise InteractionError("invalid_observation")
        binding = view_or_binding if type(view_or_binding) is PageBinding else None
        ref = binding.view if binding is not None else view_or_binding
        view = self._view(ref)
        self.last_observed_page = view.page
        document = view.document
        observed_origin = _origin(view.page.url)
        if binding is not None and _origin(view.page.url) != _origin(binding.origin):
            raise InteractionError("binding_origin_changed")
        try:
            with self._observation_deadline(view.page, deadline):
                result = observer(view.page, _ObservationBudget(self, deadline))
            current = self._view(ref)
            if current.document != document:
                raise InteractionError("binding_changed")
            if _origin(current.page.url) != observed_origin:
                raise InteractionError("binding_origin_changed")
            if binding is not None and _origin(current.page.url) != _origin(binding.origin):
                raise InteractionError("binding_origin_changed")
            return result
        except InteractionError:
            raise
        except Exception:
            browser = getattr(self.session, '_browser', None)
            if browser is not None and not browser.is_connected():
                raise InteractionError('connection_lost') from None
            if view.page.is_closed():
                raise InteractionError('stale_view') from None
            raise InteractionError("observation_failed") from None

    @contextmanager
    def _observation_deadline(self, native, deadline):
        """Bound read-only Playwright calls that provide no timeout argument.

        The owning sync API continues pumping its own loop. Expiry cancels the
        read wait, never closes a profile, retries input or renews an action.
        Synthetic ports without a Playwright connection retain their own bounds.
        """
        self._guard(deadline, completion=True)
        connection = getattr(getattr(native, "_impl_obj", None), "_connection", None)
        if connection is None:
            yield
            return
        original = connection.wrap_api_call

        async def limited(callback, is_internal=False, title=None):
            remaining = deadline - self.clock.monotonic()
            if remaining <= 0:
                raise InteractionError("deadline_exhausted")
            try:
                return await asyncio.wait_for(original(callback, is_internal=is_internal, title=title), remaining)
            except asyncio.TimeoutError:
                raise InteractionError("deadline_exhausted") from None

        connection.wrap_api_call = limited
        try:
            yield
        finally:
            connection.wrap_api_call = original

    def inspect_component(self, root: TargetRef, selectors: dict[str, str],
                          attributes: tuple[str, ...], *, deadline: float) -> dict:
        """Read marker attributes and rendered control state in one DOM task.

        This fixed script performs no input and reads no form values, storage,
        application variables or network state. The component supplies selectors
        and its declared non-secret marker attributes.
        """
        if (type(selectors) is not dict or len(selectors) > 64
                or any(type(key) is not str or not key or len(key) > 256
                       or type(value) is not str or not value or len(value) > 4096
                       for key, value in selectors.items())
                or type(attributes) is not tuple or len(attributes) > 64
                or any(type(name) is not str or not name or len(name) > 128
                       or name in {"value", "password"} for name in attributes)
                or len(set(attributes)) != len(attributes)):
            raise InteractionError("invalid_observation")
        expression = """(root, spec) => {
            const visible = element => {
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.visibility !== 'collapse'
                    && rect.width > 0 && rect.height > 0;
            };
            const attributes = Object.fromEntries(spec.attributes.map(name => [name, root.getAttribute(name)]));
            const controls = Object.fromEntries(Object.entries(spec.selectors).map(([name, selector]) => {
                const elements = root.ownerDocument.querySelectorAll(selector);
                return [name, {count: elements.length, visible: elements.length === 1 && visible(elements[0])}];
            }));
            return {url: root.ownerDocument.location.href, attributes, controls,
                    root: {count: 1, visible: visible(root)}};
        }"""
        try:
            locator = self._locator(root, deadline, unique=True)
            timeout = self._timeout_ms(deadline, completion=True)
            result = locator.evaluate(expression, {"selectors": selectors, "attributes": list(attributes)},
                                      timeout=timeout)
            if (type(result) is not dict or set(result) != {"url", "attributes", "controls", "root"}
                    or type(result["url"]) is not str or type(result["attributes"]) is not dict
                    or set(result["attributes"]) != set(attributes)
                    or any(value is not None and type(value) is not str for value in result["attributes"].values())
                    or type(result["controls"]) is not dict or set(result["controls"]) != set(selectors)):
                raise InteractionError("observation_failed")
            for item in (result["root"], *result["controls"].values()):
                if (type(item) is not dict or set(item) != {"count", "visible"}
                        or type(item["count"]) is not int or item["count"] < 0
                        or type(item["visible"]) is not bool):
                    raise InteractionError("observation_failed")
            return result
        except InteractionError:
            raise
        except Exception:
            raise InteractionError("observation_failed") from None

    def input_equals(self, target: TargetRef, expected: SensitiveValue, *, deadline: float) -> bool:
        """Privately compare native or contenteditable text; expose an equality bit."""
        if type(expected) is not SensitiveValue:
            raise InteractionError("private_value_required")
        try:
            view = self._view(target.view)
            document = view.document
            locator = self._locator(target, deadline, unique=True)
            timeout = self._timeout_ms(deadline, completion=True)
            try:
                actual = locator.input_value(timeout=timeout)
            except Exception:
                # Playwright's input_value intentionally rejects contenteditable.
                # The fallback is another admitted read, never a DOM mutation.
                timeout = self._timeout_ms(deadline, completion=True)
                actual = locator.evaluate("""element => element.isContentEditable
                    ? element.innerText : null""", None, timeout=timeout)
            if self._view(target.view).document != document:
                raise InteractionError("binding_changed")
            return type(actual) is str and actual == expected.reveal()
        except InteractionError:
            raise
        except Exception:
            raise InteractionError("observation_failed") from None

    def input_present(self, target: TargetRef, *, deadline: float,
                      allow_contenteditable: bool = False) -> bool:
        """Observe presence privately; contenteditable requires explicit support.

        Existing controlled authentication only declares native password fields.
        Its unsupported-control protection remains unchanged by the new text port.
        """
        if type(allow_contenteditable) is not bool:
            raise InteractionError("invalid_observation")
        try:
            view = self._view(target.view)
            document = view.document
            locator = self._locator(target, deadline, unique=True)
            timeout = self._timeout_ms(deadline, completion=True)
            expression = """element => {
                if (element.tagName === 'INPUT' || element.tagName === 'TEXTAREA') return element.value !== '';
                return null;
            }"""
            if allow_contenteditable:
                expression = expression.replace("return null;", "if (element.isContentEditable) return element.innerText !== ''; return null;")
            present = locator.evaluate(expression, None, timeout=timeout)
            if self._view(target.view).document != document:
                raise InteractionError("binding_changed")
            if type(present) is not bool:
                raise InteractionError("observation_failed")
            return present
        except InteractionError:
            raise
        except Exception:
            raise InteractionError("observation_failed") from None

    def private_text(self, target: TargetRef, *, deadline: float,
                     max_characters: int = 16384) -> SensitiveValue:
        """Read rendered text privately, with current-view/document checks.

        The runtime value must be cleared by its consumer. No plain text is added
        to receipts or observation records; callers cannot read storage or JS
        application state through this port.
        """
        if type(max_characters) is not int or not 1 <= max_characters <= 1_048_576:
            raise InteractionError("invalid_observation")
        try:
            view = self._view(target.view)
            document = view.document
            locator = self._locator(target, deadline, unique=True)
            timeout = self._timeout_ms(deadline, completion=True)
            value = locator.evaluate("""(element, limit) => {
                const value = element.tagName === 'INPUT' || element.tagName === 'TEXTAREA'
                    ? element.value : element.innerText;
                return typeof value === 'string' && value.length <= limit ? value : null;
            }""", max_characters, timeout=timeout)
            if self._view(target.view).document != document:
                raise InteractionError("binding_changed")
            if type(value) is not str or len(value) > max_characters:
                raise InteractionError("observation_failed")
            return SensitiveValue(value)
        except InteractionError:
            raise
        except Exception:
            raise InteractionError("observation_failed") from None

    def selected_option_equals(self, target: TargetRef, expected: str, *, deadline: float) -> bool:
        if type(expected) is not str or not expected or len(expected) > 256:
            raise InteractionError("invalid_observation")
        try:
            document = self._view(target.view).document
            locator = self._locator(target, deadline, unique=True)
            result = locator.evaluate("""(element, expected) => element.tagName === 'SELECT'
                ? element.value === expected : null""", expected,
                timeout=self._timeout_ms(deadline, completion=True))
            if self._view(target.view).document != document:
                raise InteractionError("binding_changed")
            if type(result) is not bool:
                raise InteractionError("observation_failed")
            return result
        except InteractionError:
            raise
        except Exception:
            raise InteractionError("observation_failed") from None

    def checked_equals(self, target: TargetRef, expected: bool, *, deadline: float) -> bool:
        if type(expected) is not bool:
            raise InteractionError("invalid_observation")
        try:
            document = self._view(target.view).document
            locator = self._locator(target, deadline, unique=True)
            result = locator.evaluate("""(element, expected) => element.tagName === 'INPUT'
                && ['checkbox', 'radio'].includes(element.type) ? element.checked === expected : null""",
                expected, timeout=self._timeout_ms(deadline, completion=True))
            if self._view(target.view).document != document:
                raise InteractionError("binding_changed")
            if type(result) is not bool:
                raise InteractionError("observation_failed")
            return result
        except InteractionError:
            raise
        except Exception:
            raise InteractionError("observation_failed") from None

    def uploaded_image_equals(self, target: TargetRef, asset, *, deadline: float) -> bool:
        """Separate private readback of selected file bytes; no publication claim."""
        from hashlib import sha256
        from automation_core.assets import ResolvedPrivateAsset
        if type(asset) is not ResolvedPrivateAsset:
            raise InteractionError("private_asset_required")
        try:
            view = self._view(target.view)
            document = view.document
            locator = self._locator(target, deadline, unique=True)
            result = locator.evaluate("""async (element, expected) => {
                if (element.tagName !== 'INPUT' || element.type !== 'file') return null;
                if (element.files.length !== 1) return false;
                const file = element.files[0];
                if (file.type !== expected.type || file.size !== expected.size) return false;
                const hash = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
                const actual = Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('');
                return actual === expected.digest;
            }""", {"type": asset.media_type, "size": len(asset.data), "digest": sha256(asset.data).hexdigest()},
                timeout=self._timeout_ms(deadline, completion=True))
            if self._view(target.view).document != document:
                raise InteractionError("binding_changed")
            if type(result) is not bool:
                raise InteractionError("observation_failed")
            return result
        except InteractionError:
            raise
        except Exception:
            raise InteractionError("observation_failed") from None

    def pump_wait(self, seconds: float, deadline: float, *, completion: bool = False) -> None:
        """Wait in owner-thread Playwright slices, never blocking it in sleep()."""
        if isinstance(seconds, bool) or not isinstance(seconds, (float, int)) or not math.isfinite(seconds) or seconds < 0:
            raise InteractionError("invalid_wait")
        until = self._guard(deadline, completion=completion) + seconds
        while True:
            now = self._guard(deadline, completion=completion)
            if now >= until:
                return
            live = next((v for v in self._views.values() if not v.page.is_closed()), None)
            if live is None:
                raise InteractionError("no_live_view")
            try:
                live.page.wait_for_timeout(1000 * min(0.005, until - now, deadline - now))
            except Exception:
                raise InteractionError("wait_failed") from None


class ActionExecutor:
    """Sequential input, desired-state form operations and one private image."""

    def __init__(self, driver: BrowserDriver, default_policy=None, *, dispatch_policy=None, rng=None):
        self.behavior = InputBehavior() if default_policy is None else default_policy
        if type(self.behavior) is not InputBehavior:
            raise ValueError("Expected InputBehavior.")
        self.rng = random.Random() if rng is None else rng
        self.driver = driver
        self.dispatch_policy = dispatch_policy
        self.pointer = driver.pointer
        self.scroll = ScrollController(driver)

    def execute_scoped(self, request, binding, deadline, *, operation_id, attempt_id,
                       destination_id, dispatch_assessment=None, dispatch_context=None,
                       required_action_reserve=.5):
        """The ordinary executor with exact navigation-attempt provenance.

        It performs one existing input. Settled API failure is descriptive
        evidence and never proves the original effect did not happen.
        """
        driver = self.driver
        if type(request) is not ActionRequest or request.kind != "navigate":
            return self.execute(request, binding, deadline, dispatch_assessment=dispatch_assessment,
                                dispatch_context=dispatch_context, required_action_reserve=required_action_reserve)
        valid = lambda value: type(value) is str and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value, re.ASCII) is not None
        try:
            driver.assert_owner()
            if (not all(valid(value) for value in (operation_id, attempt_id, destination_id))
                    or driver._navigation_scope is not None):
                raise InteractionError("invalid_execution_scope")
            ref = binding.view if type(binding) is PageBinding else binding
            view = driver._view(ref)
            if type(request.url) is not str or not request.url:
                return self.execute(request, binding, deadline, dispatch_assessment=dispatch_assessment,
                                    dispatch_context=dispatch_context, required_action_reserve=required_action_reserve)
            parts = urlsplit(request.url)
            if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username is not None or parts.password is not None:
                return self.execute(request, binding, deadline, dispatch_assessment=dispatch_assessment,
                                    dispatch_context=dispatch_context, required_action_reserve=required_action_reserve)
            if (not all(callable(getattr(view.page, name, None))
                        for name in ("on", "remove_listener", "wait_for_event"))
                    or getattr(view.page, "main_frame", None) is None):
                return self.execute(request, binding, deadline, dispatch_assessment=dispatch_assessment,
                                    dispatch_context=dispatch_context, required_action_reserve=required_action_reserve)
        except InteractionError as error:
            return DispatchReceipt("not_attempted", reason=error.reason)
        except Exception:
            return DispatchReceipt("not_attempted", reason="invalid_execution_scope")
        scope = dict(request=request, view=view, deadline=deadline, failed=False, event_invalid=False,
                     settled=False, returned_at=None)
        driver._navigation_scope = scope
        try:
            receipt = self.execute(request, binding, deadline, dispatch_assessment=dispatch_assessment,
                                   dispatch_context=dispatch_context, required_action_reserve=required_action_reserve)
        finally:
            driver._navigation_scope = None
        if (scope["failed"] and scope["settled"] and not scope["event_invalid"]
                and receipt.entered_at is not None and receipt.reason in {None, "browser_call_failed"}
                and not driver._quarantined and not driver._input_active
                and not any(dialog.response == "unresolved" or dialog.entered_at is not None
                            and dialog.returned_at is None for dialog in receipt.dialogs)):
            try:
                current = driver._view(ref)
                settled = replace(receipt, status="returned", reason="network_failed",
                                  returned_at=scope["returned_at"])
                from automation_core.workflow.interruptions import NavigationFailureProof
                proof = NavigationFailureProof(ref, current.document, operation_id,
                                               attempt_id, destination_id, settled, "network_failed")
                driver._navigation_failures[id(settled)] = proof
                while len(driver._navigation_failures) > 32:
                    driver._navigation_failures.pop(next(iter(driver._navigation_failures)))
                return settled
            except (InteractionError, ValueError):
                pass
        return receipt

    def execute(self, request: ActionRequest, binding: PageBinding | BlockerBinding | ViewRef | None,
                deadline: float, *, blocker_authority=None, dispatch_assessment=None,
                dispatch_context=None, native_witness=None, required_action_reserve=.5) -> DispatchReceipt:
        driver = self.driver
        if type(request) is ActionRequest and request.kind == "closeTab":
            from automation_core.workflow.dispatch_policy import DispatchAssessment, DispatchPurpose
            if (self.dispatch_policy is None or type(dispatch_assessment) is not DispatchAssessment
                    or dispatch_assessment.purpose is not DispatchPurpose.PROFILE_TAB_CLEANUP):
                return DispatchReceipt("not_attempted", reason="dispatch_policy_unknown_effect")
        if native_witness is not None:
            # Only the fixed passive consent producer may observe these local
            # markers. It grants no authority and cannot replace ordinary gates.
            try:
                if type(native_witness) not in _NATIVE_WITNESS_TYPES or dispatch_context is None:
                    raise InteractionError("invalid_request")
                native_witness._require(driver, request, binding, dispatch_context, deadline)
            except Exception:
                return DispatchReceipt("not_attempted", reason="invalid_request")
        if self.dispatch_policy is not None:
            reason = self.dispatch_policy.rejection(request, binding, deadline,
                assessment=dispatch_assessment, context=dispatch_context)
            if reason is not None:
                return DispatchReceipt("not_attempted", reason=reason)
        state = dict(status="not_attempted", reason=None, entered_at=None,
                     returned_at=None, phase="preparation", created_view=None,
                     activation_entered=False, activation_returned=False,
                     cleanup_attempted=False, cleanup_returned=False, cleanup_reason=None,
                     dialogs=(), dialog_count=0, view_closed=None, movement=None, scroll=())
        acquired = False
        possible_button = False
        fault_readiness = None
        view = None
        locator = None
        try:
            driver._guard(deadline)
            if (type(required_action_reserve) not in (float, int)
                    or not math.isfinite(required_action_reserve) or required_action_reserve < 0):
                raise InteractionError("invalid_deadline")
            if driver._input_active:
                raise InteractionError("input_busy")
            if driver._quarantined:
                raise InteractionError("input_quarantined")
            driver._input_active = True
            driver._input_created_view = None
            acquired = True
            if self.dispatch_policy is not None:
                def native_guard(_binding, until, current_state):
                    if current_state["reason"] is not None:
                        raise InteractionError(current_state["reason"])
                    def local():
                        driver._guard(until)
                        if view is not None:
                            driver._check_input_binding(view, binding if isinstance(binding,
                                (PageBinding, BlockerBinding)) else None)
                        if type(binding) is BlockerBinding:
                            driver._validate_blocker_input_local(binding, until)
                    local()
                    if type(binding) is BlockerBinding:
                        driver.validate_blocker_binding(binding, deadline=until, input_guard=True)
                    driver._validate_input_targets(until)
                    retained_targets = tuple(driver._input_targets.items())
                    blocker_retention = (driver._blocker_bindings.get(binding.retention_id)
                        if type(binding) is BlockerBinding else None)
                    driver._source_verification_deadline = until
                    try:
                        reason = self.dispatch_policy.native_rejection(request, binding, until,
                            assessment=dispatch_assessment, context=dispatch_context)
                    finally:
                        driver._source_verification_deadline = None
                    # Only local state may follow final source I/O. Reading
                    # identity/geometry again would reopen a pre-entry proof gap.
                    local()
                    if (len(driver._input_targets) != len(retained_targets)
                            or any(driver._input_targets.get(target) is not retained
                                   for target, retained in retained_targets)
                            or type(binding) is BlockerBinding and
                               driver._blocker_bindings.get(binding.retention_id) is not blocker_retention):
                        raise InteractionError("binding_changed")
                    if reason is not None:
                        raise InteractionError(reason)
                    reason = self.dispatch_policy.native_rejection(request, binding, until,
                        assessment=dispatch_assessment, context=dispatch_context, verify=False)
                    if reason is not None:
                        raise InteractionError(reason)
                    driver._guard(until)
                driver._native_input_guard = native_guard
            if not isinstance(request, ActionRequest) or type(request.kind) is not str or request.kind not in {
                "click", "move", "hover", "scroll", "fill", "selectOption", "setChecked", "uploadImage", "setSlider",
                "navigate", "openTab", "activateTab", "closeOwnedTab", "closeTab",
            }:
                raise InteractionError("unsupported_request")
            kind = request.kind
            if type(binding) is BlockerBinding:
                if type(blocker_authority) is not BlockerInputAuthority:
                    raise InteractionError("blocker_authority_required")
                if (kind != "click" or (request.movement is not None
                        and (request.movement.mode != "locator" or request.movement.button != "left"))):
                    raise InteractionError("unsupported_blocker_input")
                blocker_authority.claim(driver, binding)
                if blocker_authority.operation_id in driver._blocker_dispatch_claims:
                    raise InteractionError("blocker_authority_invalid")
                # Minting another capability for the same current ticket cannot
                # create a second dispatch. Durable recovery still owns all
                # admissions; this local latch is never a substitute for it.
                driver._blocker_dispatch_claims.add(blocker_authority.operation_id)
                driver._blocker_input = (binding, blocker_authority, deadline)
                driver.validate_blocker_binding(binding, deadline=deadline, input_guard=True)
            elif blocker_authority is not None:
                raise InteractionError("invalid_blocker_binding")
            if ((request.movement is not None and kind not in {"click", "move", "hover"})
                    or (request.scroll is not None and kind != "scroll")
                    or (kind == "move" and request.movement is None)):
                raise InteractionError("invalid_request")
            if type(request.run_before_unload) is not bool or (request.run_before_unload and kind != "closeOwnedTab"):
                raise InteractionError("invalid_request")
            if ((request.option_value is not None and kind != "selectOption")
                    or (request.checked is not None and kind != "setChecked")
                    or (request.asset is not None and kind != "uploadImage")):
                raise InteractionError("invalid_request")
            if kind == 'setSlider':
                if (type(request.slider_fraction) is not float or not 0 <= request.slider_fraction <= 1
                        or type(request.slider_track) is not TargetRef
                        or request.target is None or request.slider_track.view != request.target.view):
                    raise InteractionError('invalid_request')
            elif request.slider_fraction is not None or request.slider_track is not None:
                raise InteractionError('invalid_request')
            if kind == "openTab":
                if binding is not None or request.target is not None or request.value is not None or request.url is not None:
                    raise InteractionError("invalid_request")
            else:
                ref = binding.view if isinstance(binding, (PageBinding, BlockerBinding)) else binding
                view = driver._view(ref)
                driver._input_document = (view.ref.view_id, view.document)
            if kind in {"click", "move", "hover", "scroll", "fill", "selectOption", "setChecked", "uploadImage", "setSlider"}:
                if not isinstance(binding, (PageBinding, BlockerBinding)):
                    raise InteractionError("binding_required")
                if ((request.target is None and kind != "scroll")
                        or (request.target is not None and
                            (not isinstance(request.target, TargetRef) or request.target.view != binding.view))):
                    raise InteractionError("target_binding_mismatch")
                if type(binding) is BlockerBinding and request.target != binding.target:
                    raise InteractionError("target_binding_mismatch")
                driver._guard(deadline)
                driver._check_input_binding(view, binding)
                if request.url is not None or (kind != "fill" and request.value is not None):
                    raise InteractionError("invalid_request")
                if kind == "fill" and not isinstance(request.value, SensitiveValue):
                    raise InteractionError("private_value_required")
                if request.target is not None:
                    locator = (driver._retain_input_target(request.target, deadline)
                               if (self.dispatch_policy is not None or kind == "setSlider"
                                   or kind == "fill" and self.behavior.enabled)
                               and type(binding) is not BlockerBinding
                               else driver._locator(request.target, deadline, unique=True))
                if kind == "scroll" and self.dispatch_policy is not None:
                    scope = request.scroll.scope if type(request.scroll) is ScrollIntent else None
                    driver._retain_input_target(scope or TargetRef(binding.view, "html"), deadline)
                if kind == "selectOption":
                    if (type(request.option_value) is not str or not request.option_value
                            or len(request.option_value) > 256
                            or any(ord(character) < 32 or ord(character) == 127 for character in request.option_value)):
                        raise InteractionError("invalid_request")
                elif kind == "setChecked" and type(request.checked) is not bool:
                    raise InteractionError("invalid_request")
                elif kind == 'setSlider':
                    if locator.get_attribute('role') != 'slider':
                        raise InteractionError('unsupported_control')
                    driver._retain_input_target(request.slider_track, deadline)
                elif kind == "uploadImage":
                    from automation_core.assets import ResolvedPrivateAsset, PrivateAssetError
                    if type(request.asset) is not ResolvedPrivateAsset:
                        raise InteractionError("private_asset_required")
                    try:
                        request.asset.validate()
                    except PrivateAssetError:
                        raise InteractionError("private_asset_invalid") from None
                if kind in {"selectOption", "setChecked", "uploadImage"}:
                    expected_control = {"selectOption": "select", "setChecked": "check", "uploadImage": "file"}[kind]
                    control = locator.evaluate("""element => {
                        if (element.tagName === 'SELECT' && !element.multiple) return 'select';
                        if (element.tagName === 'INPUT' && ['checkbox', 'radio'].includes(element.type)) return 'check';
                        // uploadImage always supplies exactly one validated asset.
                        // A picker accepting several files does not change that.
                        if (element.tagName === 'INPUT' && element.type === 'file') return 'file';
                        return null;
                    }""", None, timeout=driver._timeout_ms(deadline, completion=True))
                    if control != expected_control:
                        raise InteractionError("unsupported_control")
            elif kind != "openTab":
                if request.target is not None or request.value is not None:
                    raise InteractionError("invalid_request")
                if kind == "navigate":
                    if not isinstance(request.url, str) or not request.url:
                        raise InteractionError("destination_required")
                elif request.url is not None:
                    raise InteractionError("invalid_request")
                if kind == "closeOwnedTab" and not view.owned:
                    raise InteractionError("view_not_owned")

            from automation_core.diagnostics.live_faults import before_action
            before_action(driver, request, binding, dispatch_context, deadline)
            if view is not None:
                driver._check_input_binding(view,
                    binding if isinstance(binding, (PageBinding, BlockerBinding)) else None)
            value = request.value.reveal() if kind == "fill" else None
            with driver.dialog_scope(binding, request.dialog, state, deadline):
                if self.dispatch_policy is not None and locator is not None and type(binding) is not BlockerBinding:
                    # Resolve actionability through read-only waits on the same
                    # node before the final purpose proof, under the old cutoff.
                    states = {"click": ("visible", "stable", "enabled"),
                              "hover": ("visible", "stable"), "fill": ("visible", "editable"),
                              "setChecked": ("visible", "stable", "enabled"),
                              "setSlider": ("visible", "stable", "enabled"),
                              "selectOption": ("enabled",), "scroll": ("visible", "stable")}
                    for readiness in states.get(kind, ()):
                        fault_readiness = readiness
                        locator.wait_for_element_state(readiness, timeout=driver._timeout_ms(deadline))
                        fault_readiness = None
                if request.movement is not None:
                    self.pointer.execute(request, binding, deadline, state)
                elif kind == "scroll":
                    self.scroll.execute(request, binding, deadline, state)
                else:
                    behavior = None
                    if (self.behavior.enabled and type(binding) is PageBinding
                            and kind in {"click", "hover", "fill", "setChecked"}):
                        behavior = _InputBehavior(driver, self.behavior, self.rng,
                            binding, view, deadline, required_action_reserve, state)
                        driver._input_behavior = behavior
                        behavior.prepare(locator)
                    # Guard after listener registration; entry precedes the API.
                    if state["reason"] is not None:
                        raise InteractionError(state["reason"])
                    if view is not None:
                        driver._check_input_binding(view, binding if isinstance(binding, (PageBinding, BlockerBinding)) else None)
                    if type(binding) is BlockerBinding:
                        driver.validate_blocker_binding(binding, deadline=deadline, input_guard=True)
                    driver._before_native_input(binding, deadline, state)
                    timeout = driver._timeout_ms(deadline)
                    entered_at = driver.clock.monotonic()
                    if native_witness is not None:
                        native_witness._native_started(entered_at)
                    state.update(status="attempted", entered_at=state["entered_at"] or entered_at, phase=kind)
                    # Typing keeps the commanded cursor. Locator clicks may move
                    # it again during actionability; never keep a guessed point.
                    if kind != "fill" or behavior is None:
                        self.pointer.invalidate("opaque_locator_or_lifecycle_input")
                    if kind == "click":
                        state["activation_entered"] = True
                        possible_button = True
                    state["created_view"] = driver._dispatch(request, view, locator, value, timeout)
                    if kind == "click":
                        possible_button = False
                        state["activation_returned"] = True
                    state.update(status="returned", returned_at=driver.clock.monotonic())
                    if native_witness is not None:
                        native_witness._native_returned(state["returned_at"])
                    if behavior is not None and kind != "fill":
                        behavior.after_locator()
                        state.update(status="returned", phase=kind, returned_at=driver.clock.monotonic())
            if kind in {"closeOwnedTab", "closeTab"}:
                driver.assert_owner()
                state["view_closed"] = view.page.is_closed()
            # A completed call remains returned even if it arrived late/cancelled.
            if state["reason"] == "expected_dialog_missing":
                state["status"] = "attempted"
            elif state["reason"] is not None:
                pass
            elif driver.clock.monotonic() >= deadline:
                state["reason"] = "deadline_exhausted"
            elif driver.cancellation.is_set():
                state["reason"] = "cancelled"
        except (InteractionError, PointerError, BlockerBindingError) as error:
            from automation_core.diagnostics.live_faults import observe_failure
            observe_failure(driver, request, dispatch_context, error, readiness=fault_readiness)
            state["reason"] = state["reason"] or error.reason
        except Exception as error:
            from automation_core.diagnostics.live_faults import observe_failure
            observe_failure(driver, request, dispatch_context, error, readiness=fault_readiness)
            # A detached retained node during readiness is a stale target, not
            # an adapter bug. No native activation has begun at this boundary.
            reason = ("binding_changed" if fault_readiness is not None
                and not state["activation_entered"] and "not attached" in str(error)
                else "browser_call_failed")
            state["reason"] = state["reason"] or reason
        finally:
            if possible_button:
                # This bounded release is not a retry or proof of rollback.
                try:
                    driver._release_left_button(view, state)
                except Exception:
                    state["cleanup_reason"] = "cleanup_uncertain"
                    driver._quarantined = True
            if acquired:
                if state["created_view"] is None:
                    state["created_view"] = driver._input_created_view
                driver._input_created_view = None
                driver._input_document = None
                driver._blocker_input = None
                driver._native_input_guard = None
                driver._source_verification_deadline = None
                for retained in driver._input_targets.values():
                    try:
                        retained.handle.dispose()
                    except Exception:
                        pass
                driver._input_targets.clear()
                driver._input_active = False
                driver._input_behavior = None
            if self.dispatch_policy is not None:
                self.dispatch_policy.release(dispatch_assessment)
        receipt = DispatchReceipt(**state)
        from automation_core.diagnostics.live_faults import after_action
        after_action(driver, request, dispatch_context, receipt, deadline)
        return receipt


_NATIVE_WITNESS_TYPES = set()


def register_native_witness(witness_type):
    """Register a source-defined passive witness; all native gates still run."""
    _NATIVE_WITNESS_TYPES.add(witness_type)
