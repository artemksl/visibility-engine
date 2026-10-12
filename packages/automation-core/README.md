# Shared Automation Core 0.1.2

This source snapshot is the exact core release used by the Visibility Runner
0.2.55. Install it through [the root setup instructions](../../README.md).
The original distribution's unrelated `chatgpt` demonstration application is
excluded. All 39 `automation_core` modules are retained unchanged.

Visibility input uses `automation_core.browser.async_actions.AsyncActionExecutor`
with the shared organic movement planner. The runner supplies target meaning,
ownership, cancellation, deadline, send admission and response correlation.
The executor reports native API entry/return, which is not proof of a website
result. It never automatically repeats a native action.

`Pillow` is needed only for the optional image-asset helpers. Install the
`assets` extra if using `automation_core.assets`; the visibility browser path
does not use it. Failure captures and mutable state should always be supplied
explicit project-local private paths.

General action-executor fixes belong in this package. Website selectors and
visibility recovery policy belong in the runner. Source provenance is in
[PROVENANCE.md](../PROVENANCE.md).
