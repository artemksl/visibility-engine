# Instructions for an installing or maintaining agent

Read `INSTALL.md`, then inspect the target machine before changing configuration.
Install this checkout with `./setup.sh`; do not search for another private runner
wheel or depend on directories from the original developer's computer.

Use a dedicated virtual environment. Keep the checkout intact: editable packages,
the worker and the browser launcher resolve files relative to this repository.
The source versions are visibility-runner 0.2.55 and workspace-automation-core
0.1.2. The shared action core is the exact runtime release used by this runner.

Configuration examples are fictional. Ask the owner for the intended signed-in
Chrome profile bindings and an explicit selected-profile list. Never infer login
status from an example or silently use every configured profile. Do not store
passwords, session files or provider tokens in tracked files.

Installation, imports, localhost health checks, Preview and offline tests are
safe readiness checks. They do not prove a provider is available. Start and
Refetch perform real browser work and send prompts; run them only when the user
authorizes the measurements. Never send live prompts, click Retry, launch saved
profiles or call model APIs from automated tests.

Keep these boundaries when making changes:

- The panel owns request freezing, worker control and at most five automatic
  recovery rounds. The runner owns scheduling, browser tabs and measurement.
- Website adapters identify composers and responses. Input goes through the
  imported shared asynchronous action executor.
- A returned browser call does not prove a successful send. Preserve admission
  records, response correlation and the guard against overlapping unsettled
  native input.
- Automatic recovery uses `replace_checked_sends=False`. Never turn an uncertain
  send into automatic resend permission or alter a saved run's plan and budget.
- A fully successful runner result is the only full-completion signal. Keep
  two-turn measurement and exact ordered-name validation, including duplicates.
- Tests use synthetic evidence. Preserve historical runtime artifacts unchanged.

Make scoped, readable changes and run affected offline tests. Avoid copying the
unrelated clinic application into this package. Shared executor changes belong
in `packages/automation-core`; runner changes belong in
`packages/visibility-runner`. Do not bypass profile ownership or cancellation to
make a failing run appear successful.

