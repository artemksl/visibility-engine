# Package source provenance

This directory contains source-installable workspace packages rather than opaque
release wheels. The package bodies preserve the releases used by the testing
panel at extraction:

| Directory | Distribution | Version | Source preserved |
| --- | --- | --- | --- |
| `visibility-runner` | `visibility-runner` | 0.2.55 | Engine Python modules, prompt/schema resources and optional Node worker |
| `automation-core` | `workspace-automation-core` | 0.1.2 | All 39 core Python modules, including the verified organic movement executor |
| `axbridge` | `axbridge` | 0.2.3 | All 15 Python modules from the installed optional macOS verification release |

The runner source has since received the Gemini and Copilot repairs and is
currently pinned to 0.2.80. The original wheel identities in
`SOURCE_MANIFEST.json` remain the extraction record; subsequent source repairs
and focused synthetic regressions are maintained in this checkout.

The 0.2.69 Copilot repair waits for the source conversation route before response
acknowledgement under the existing deadline, preserves typed refusals before
native input, and waits for active profile blockers before work/send admission.
It retains the 0.1.2 action core and Gemini's provider contracts.

The 0.2.70 update preserves the original owned submission when provider startup
fails, allowing the existing private HTML sampler to capture it without a send
fence. Supported sign-in control refusals are re-probed only before native input;
account, history and episode-deadline checks still govern any subsequent work.

The 0.2.71 sign-in update keeps waiting through supported authentication
transitions after a proven pre-input refusal, under the original episode deadline
and exact-tab checks. Its existing action latch prevents repeating that control.

The 0.2.72 update waits for Copilot's canonical route before measuring answer
stability. Copilot user-message reads trim rendered boundary whitespace while
preserving internal text, assistant text and native send hashes. A modal wrapper
is informational only when its complete rendered content belongs to one tooltip;
the existing composer obstruction and security checks remain active. The shared
action core remains 0.1.2. These source repairs do not claim live qualification.

The 0.2.73 update reads Copilot's current live quota notices outside individual
message turns without changing transcript grouping. Its reset parser accepts
explicit valid “check back” clocks without inventing a timezone or date. Native
verification reserves a bounded 500 ms for fresh browser and AX guards and
records typed refusal timing in the existing event path. It retains exact
foreground proof, one guarded click and the original episode deadline. Existing
private failure evidence now includes bounded browser-dialog metadata without
acting on that dialog or exposing its message through public issue summaries.
The action core and AXBridge releases remain unchanged; live qualification is
still pending.

The 0.2.74 update gives newly built Copilot parsing turns a concise instruction
and JSON-quoted exact source text. ChatGPT and Gemini retain their existing
prompt bytes. An admitted historical prompt is reproduced from its frozen hash;
the builder does not rewrite its fence or turn source. The original 0.2.73
attempt filled 10 of 15 observations and recorded 14 provider parsing refusals.
One Refetch under 0.2.74 completed the five remaining fresh pairs, bringing the
first batch to 15 of 15. Those five new parsing turns succeeded with the plain
quoted prompt; the provider's internal refusal cause remains unknown. This does
not establish three successful batches.

The 0.2.75 update reproduces the admitted Copilot parsing prompt from its exact
frozen hash even when original-page recovery does not supply a provider argument.
It checks supported prompt candidates without changing admitted bytes or send
fences. Focused synthetic recovery covers this compatibility edge without sending
another prompt. Shared core and AXBridge versions remain unchanged.

The 0.2.76 update gives Copilot user-message reads one shared literal-text rule
for history, current-prompt quota correlation and reconciliation. On the observed
plain `chatOutput` root, text nodes are authoritative only when every descendant
is text or a BR bracketed by literal newlines. Other structures keep the existing
rendered-text reader. It preserves internal characters without fabricating BR
line breaks, normalizing whitespace or changing native/frozen send hashes.
The final installed host validation wheel for this source revision has SHA256
`c254d17dc2329247e4564624931d44a5c029b32b3b7a25b9c46c6af6e459d12e`.
This bundle remains source-installable and does not include that wheel.

Core and AXBridge source bytes were checked against their installed release
files. The current development checkout of the shared core contains additional
unreleased changes and was deliberately not used. The unrelated `chatgpt`
demonstration application from the original core distribution is excluded.
Package metadata is adapted for source installation within this repository.
`SOURCE_MANIFEST.json` records the original wheel identities and each copied
source file's SHA256 without local machine paths.

The included focused tests use synthetic data and fake providers. They cover
planning, scheduler interleavings, bounded recovery, uncertain-send retention,
usage-limit parsing and native input settlement. Their behavior assertions are
preserved; only source import setup is made portable. No captures, historical
runs, Chrome profiles, credentials or browser session state are included.

## External dependencies

The 0.2.80 portability update introduces the separate
`workspace-platform-runtime` 0.1.0 package. It owns OS paths, locks, durable
publication, process containment, credential-store access and native desktop
transports. The core is now 0.1.3 and AXBridge 0.2.4: core storage delegates OS
operations, while AXBridge's motion and verification algorithms remain shared.
Its macOS dependencies are platform-marked; Windows uses pywin32 and optional
pywinauto. Original source hashes remain the extraction record, not a claim
that the current source is byte-identical. Native Windows live qualification
has not been performed on this macOS development machine.

External libraries remain dependencies installed by their own package managers;
this repository does not copy their installed code or browser binaries.
Installed upstream metadata and the worker lockfile identify Playwright as
Apache-2.0, psutil as BSD-3-Clause, Pillow as MIT-CMU, and the GitHub Copilot SDK
as MIT. The optional Node worker pins `@github/copilot-sdk` 1.0.13 and requires
Node 22.12 or newer. Its lockfile contains dependency versions and upstream
integrity hashes. The optional desktop package pins atomacos 3.3.0 and PyAutoGUI
0.9.41; installation retains their upstream package metadata and notices.

The original workspace packages did not contain a declared license or LICENSE
file. No third-party license has been invented for these local packages. Public
repository visibility by itself does not grant a license beyond applicable
hosting terms. See the repository's root documentation for any license chosen
by its owner.
