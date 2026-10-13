# Copilot and Gemini refurbishment guide

Use this guide to improve the browser Copilot and Gemini engines without accumulating fixes for individual symptoms. Establish the cause, repair the underlying rule at its existing owner, and verify the complete affected flow. The procedure incorporates the ChatGPT startup, transcript, input and editor failures investigated in October 2026.

Read [AGENTS.md](../AGENTS.md) first. Carry forward applicable user authorization and frozen run settings. Writing or following this guide does not itself launch or authorize measurements.

## Preserve the execution model

Each ordinary browser observation uses one fresh conversation: measurement answer, then ordered-name extraction from that captured answer. Accept only a closed JSON array whose names match the source exactly, preserving order and duplicates. Keep historical admitted prompt bytes and send records intact.

Browser Microsoft `copilot` and `github_copilot` are separate transports. The latter uses the GitHub SDK for its source answer and a fresh Gemini parser conversation; parser recovery preserves the accepted SDK source.

Keep one owner for each responsibility:

| Responsibility | Existing owner |
| --- | --- |
| Frozen requests, workers, progress and automatic recovery rounds | Panel |
| Run ownership, scheduling, send admission, evidence and recovery eligibility | Visibility runner |
| Website selectors, logical turns, answer content, notices and editor interpretation | Website adapters and page readers |
| Retained elements, native input, cancellation, deadlines and unsettled-call protection | Shared action executor |

A returned browser call is separate from a correlated website answer. An uncertain send remains fenced. Multiple owned tabs/workers remain supported; simplify duplicated state without merging independent resource lifetimes.

## Follow one repair procedure

### 1. Locate the failure and the running code

Record the provider, observation, turn, original error and last proven stage: browser attached, draft prepared, send admitted, native Send entered/returned, answer correlated, or extraction accepted. Use durable records alongside panel status.

Identify the interpreter and imported source used by the actual worker. In the bundled checkout, this inspection uses its dedicated environment:

```sh
.venv/bin/python -c 'import importlib.metadata as m; import visibility_runner, automation_core; print(m.version("visibility-runner"), visibility_runner.__file__); print(m.version("workspace-automation-core"), automation_core.__file__)'
```

Compare these paths with the worker configuration. A repaired source checkout does not establish that an already running worker imported the repair.

### 2. Classify the evidence before editing

| Classification | Response |
| --- | --- |
| Lifecycle or integration defect | Trace the state transition or ignored component contract. |
| Incorrect DOM/editor assumption | Reproduce the observed structure and rendering conditions. |
| Provider or model outcome | Preserve the outcome; use existing bounded recovery or attention handling. |
| Insufficient evidence | Improve the relevant diagnostic before changing behavior. |

Failed fetches, quota notices and invalid model extraction do not by themselves establish an application defect. For limits, distinguish text, tools and unknown scope. Parse only explicit valid reset clocks/day hints; derive an absolute reset or countdown only when date and timezone are established.

Capture the original owned page before cleanup through the existing HTML sampler. Preserve the relevant editor, transcript, current-turn notice, controls and CSS. A reopened page establishes its current state, not what happened at the original failure. Keep original captures private and immutable; use sanitized fixtures for distributable tests.

### 3. Reproduce the cause offline

Use an isolated page with provider traffic denied and no saved profiles. Include CSS when layout, visibility or editor readback depends on it. Remove executable/authenticated material without changing the structure being investigated.

Demonstrate the existing code's wrong result and the expected result. An import failure, missing fixture or timeout is not proof of the proposed cause. When a second fix is needed for the same symptom family, revisit the representation and missing evidence before adding another exception.

### 4. Repair the rule at its owner

Write the rule in one sentence before coding. Examples: several rendered fragments may belong to one logical turn; decorative textless graphics do not change a draft; recovery admission validates the preview, and deferred startup validates that admission's receipt.

Keep site-specific evidence in the provider spec. Put general transcript/content interpretation in the existing page reader. Put general input mechanics in the shared core. Keep send authority and recovery decisions in the runner. Use the existing asynchronous executor for Fill, typing, Send and supported native Retry.

Choose selectors from observed role/content/turn relationships and scoped controls. A page reader still needs website evidence; it cannot infer message meaning merely by simplifying the page. Avoid speculative fallback selectors, whole-body phrase searches and Retry controls belonging to older turns.

Remove a repeated check only after identifying its invariant and execution time. Preparation and final dispatch checks can protect different facts; an awaited verifier can allow the DOM to change. Consolidate identical checks and state ownership without weakening the final input boundary. Keep one coherent editor traversal rather than accumulating provider-class exceptions.

Do not conceal an unexplained failure with longer deadlines, extra retries, account rotation, higher concurrency or relaxed draft/source matching. Do not add the core's workflow/journal/recovery framework alongside the runner's existing machinery. Organic movement remains a bounded action feature, not proof of message delivery.

### 5. Verify the affected flow

First verify the reproduced failure and its closest counterexample. Then exercise the affected sequence with synthetic pages and records:

`prepare → admit Send → correlated answer → source-bound extraction → validate/save → read-only reopen`

For lifecycle changes, include `Refetch admission → deferred startup → restart`; the original ChatGPT failure was missed when these stages were tested separately.

Choose checks by the changed boundary:

| Boundary | Essential cases |
| --- | --- |
| Transcript/content | Logical turns and content roots; hidden aliases; delayed rendering; changed prior identity; newest empty answer cannot fall back to an older answer. |
| Input | Actual Fill/typing mode; exact multiline/long draft; changed retained element/document; cancellation or unsettled native input prevents another action. |
| Completion/recovery | Post-send correlation; streaming/idle state; partial results remain incomplete; uncertain sends are never automatically repeated. |
| Blockers | Real website notice versus quoted answer text; current-turn scope; retained target and original deadline survive continuation. |
| Scheduling | Two owned tabs can progress independently; occupied slots exclude peers; cancellation and peer failures settle cleanly. |

Use representative cases for shared logic. Avoid helper-call counts, duplicated status matrices and provider multiplication when selectors are stubbed. Broaden verification only when the change crosses another boundary.

The bundled baseline is available through [INSTALL.md](../INSTALL.md). It is not a full provider DOM suite. In the full development checkout, useful search targets include `test_shared_actions_dom.py`, `test_visibility_runner_response_content_dom.py`, `test_copilot_lexical_input.py`, `test_copilot_blockers_dom.py` and `test_source_admission_browser_startup.py`. Use its guarded offline runner and test documentation. In a checkout without those fixtures, add a focused isolated regression; never substitute a live provider page for a test fixture.

### 6. Validate the installed repair and close the loop

Confirm the worker's imported artifact before interpreting another run. Restart the relevant process when needed; do not replace code during an active run. Perform live validation through the panel only within applicable user authorization, using the explicitly selected profiles and frozen concurrency/deadlines.

Assess durable accepted answer/extraction pairs, send outcomes, retries and remaining issues. Report eventual completion separately from clean first-attempt execution. Offline results do not establish current provider availability, automatic sign-in success or desktop focus behavior. Any requested consecutive-run target must be satisfied by the actual qualifying runs.

Finish each repair with a short record: symptom, confirmed cause, changed owner/rule, failing-before/passing-after evidence, installed artifact and remaining limitation. Update the provider facts below when their contracts change. Preserve provider failures as provider failures. If another defect appears, return to diagnosis.

## Inspect each provider on its own terms

These are implementation facts inspected on 2026-10-13, not a guarantee of current live markup. Confirm them against new captures before changing selectors or input mode.

| Concern | Gemini | Browser Copilot |
| --- | --- | --- |
| Editor | Quill/rich-textarea; native Fill is the current default. | Zero-delay native typing in bounded chunks and dedicated Lexical readback. |
| Transcript | Count response wrappers, read their content roots, and remove only website-owned accessibility labels and leading code toolbar text. Label selectors tolerate changed HTML tags; preserve identical phrases in model text. A temporarily boxless outer content root remains pending. | Read observed user/assistant content roots and message identity. Avoid broad wrappers combining source cards, toolbars and multiple turns. |
| Completion | Correlated post-send content, nonempty answer, idle generation state and stable text under the existing deadline. | Same shared completion contract; inspect actual turn evidence instead of borrowing ChatGPT attributes. |
| Retry | Error-scoped native Retry selectors exist; retain exact failed-turn eligibility. | The current spec has no native Retry selectors. New support requires captured controls and proven eligibility. |
| Access | Detect authentication/verification; dismiss only the captured optional cookie/location notices through guarded input. Location permission is never granted. Copilot's automatic handling policy does not apply. | Preserve the existing retained blocker service and request-bound permissions. |

Gemini's immersive map view can leave the editor boxless after its Close action.
When that exact view blocks the second turn, close it through the shared executor,
then restore the already bound canonical conversation and prove its existing
history before preparing extraction. Use the original preparation deadline.
Exact CSS text selectors must match nested button labels as well as direct text;
test the captured button structure rather than a simplified flat button.
Keep the original failure evidence and live qualification records private.
The installing agent must obtain any needed records from the owner.

Copilot normal chat and supported saved-account sign-in stay in background Playwright. Exact-account sign-in requires the current request receipt and proof of the returned account. Require empty history for a fresh unsent chat, or the original canonical conversation and corresponding history for existing work. Refill only an empty/exact unsent draft.

Copilot requests zero typing delay so the shared executor uses native keyboard
chunks of at most 128 characters, with separate Shift+Enter operations for
newlines. Each native operation still runs its guards, followed by exact Lexical
readback. Organic pointer movement retains the shared executor's existing policy.

Copilot can finish displaying its source answer before its fresh `/chat` route
becomes a durable conversation route, and that transition can rebuild the rendered
answer. Wait for the route before reading answer stability, under the same response
deadline, then prove the corresponding history before acknowledgement. Parsing must inherit the
already bound conversation; it must not establish a new identity after the
source acknowledgement.

Copilot's rendered user-message container can add whitespace at its boundaries.
Trim only those boundaries when reading user-message text for correlation and
history; preserve every internal character, assistant text, native draft and
frozen send hash. Verify this with live `innerText` as well as a saved HTML page:
the live site's styles can change the rendered text and are absent from an
isolated replay.

For the observed plain Copilot `chatOutput` root, preserve literal text characters
when its only children are text nodes or BR nodes bracketed by literal newlines.
The inserted BR duplicates existing line breaks; it must not add another newline.
Use the same reader for history, current-prompt quota notices and reconciliation.
Unknown roots, rich descendants, hidden descendants or BR-only line breaks keep
the rendered reader; do not infer exact sent text from those shapes or normalize
internal whitespace to make a prompt match. Native and frozen send hashes stay
unchanged.


Copilot may wrap an informational tooltip in a modal dialog. Ignore that wrapper
only when one rendered tooltip contains all rendered text and controls, and keep
the independent composer obstruction check. Evaluate actual computed visibility:
a security dialog can remain in the markup while hidden. Visible security,
sign-in and unfamiliar modal content still block work.

For newly built Copilot parsing turns, use a concise names-only instruction
with the exact source as one JSON-quoted string. Keep exact spelling, order and
repeated occurrences in the contract and retain the same source validation.
ChatGPT and Gemini keep their existing builders. Recovery must reproduce an
admitted prompt from its frozen hash, including historical prompts; it must not
silently substitute the new wording into an old send fence.

Bound-hash recovery should recognize all supported admitted prompt candidates
even when the recovery caller omits the provider; provider selection governs only
the creation of new prompts. In the first live batch, five fresh parsing turns
using the plain quoted prompt succeeded after earlier provider refusals. That
supports the prompt repair, but the provider's internal refusal cause is unknown.


Copilot can render a live quota notice outside the assistant message. Bind it
to the exact current prompt and its conversation owner, excluding quoted notices
inside transcript messages; keep notice scope separate from message grouping.
Accept a reset clock only when its format is valid. A display such as “check back
at 1:00 AM” establishes neither an absolute date nor a timezone.

A typed ownership or provider-access refusal before native composer input must
remain that refusal. The runner forwards only the pinned executor's direct
typed cause when its receipt proves `not_attempted`, `preparation`, and no possible
dispatch. Native failures and refusals after partial input retain the sanitized
executor error and receipt. Never retry a refused guard as editor corruption or
use that repair to admit another send.

`automatic_hold_v1` permits only supported verification actions through the FIFO desktop queue/shared process lock, with exact owned-tab/foreground checks and one guarded checkbox attempt per continuous challenge. Preserve the original episode deadline and the user's subsequent app switch. Known optional cookie notices use the existing Reject-and-prove-clearance route.

Native verification guards perform fresh CDP, DOM and AX reads. Their bounded
callback budget must accommodate those reads while remaining within the original
episode deadline; Copilot reserves 500 ms and retains the sampled settle delay.
Keep the exact owned-tab and OS foreground proofs immediately before the press.
Record a typed refusal reason and elapsed/budget milliseconds in the existing
event so a callback timeout does not masquerade as demonstrated focus loss. Do
not retry the click or replace fresh proof with cached identity. Browser JavaScript
dialogs are absent from captured HTML; preserve their bounded message privately in
the existing failure sampler without accepting or dismissing an unfamiliar dialog.

Unknown dialogs, missing accounts, password/MFA/consent and failed/expired clearance retain their exact target and reason through the existing correlated controls. Do not create a second blocker/retry state machine. Start/Refetch retains its authorized restart lifecycle; Resume/finalization does not acquire restart or automatic-input authority. Automatic recovery remains bounded at five rounds and never turns uncertain sends into resend permission.

Gemini reader/input changes may also affect the `github_copilot` parser. Check that lane when shared parser behavior changes, preserving its SDK source and parser-only recovery.

Gemini completion now uses a final corresponding conversation snapshot after
lightweight stability polling. Read prompt paragraph lines from the actual query
body; accessibility summaries and wrapper controls are not prompt content.
Keep restart context rebinding scoped to the current execution owner and verified
browser generation across all its recovery scopes. Preserve the original
qualification records locally, outside the public source export.

## Start with these files

Paths below are relative to the bundled repository. In split development checkouts, resolve the same owners from their own instructions and imported module paths.

| Work | Entry points |
| --- | --- |
| Provider evidence | [gemini.py](../packages/visibility-runner/src/visibility_runner/websites/gemini.py), [copilot.py](../packages/visibility-runner/src/visibility_runner/websites/copilot.py), [page_readers.py](../packages/visibility-runner/src/visibility_runner/websites/page_readers.py) |
| Conversation and editor | [chat_actions.py](../packages/visibility-runner/src/visibility_runner/websites/chat_actions.py), [input.py](../packages/visibility-runner/src/visibility_runner/browser/input.py) |
| Shared input | [shared_actions.py](../packages/visibility-runner/src/visibility_runner/browser/shared_actions.py), [async_actions.py](../packages/automation-core/src/automation_core/browser/async_actions.py) |
| Copilot blockers | [copilot_blockers.py](../packages/visibility-runner/src/visibility_runner/browser/copilot_blockers.py), [copilot_verification.py](../packages/visibility-runner/src/visibility_runner/browser/copilot_verification.py), [copilot_access.py](../packages/visibility-runner/src/visibility_runner/websites/copilot_access.py) |

Read large modules by symbol or line range. Prefer a small correction in the existing owner over another coordinator, adapter layer or parallel mutable state store.
