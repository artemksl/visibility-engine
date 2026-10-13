# Visibility Runner 0.2.80

The measurement engine used by the Visibility Engine testing panel. Install the
combined repository using the instructions in [the root README](../../README.md).

The engine owns planning, profile and tab ownership, browser input, response
correlation, ordered-name extraction, validation, evidence and recovery. It uses
one fresh two-turn conversation per browser measurement. Shared browser input
comes from the sibling `automation-core` package.

The optional `desktop` extra uses the sibling `axbridge` package on macOS and
Windows, with native transports from `platform-runtime`. The
GitHub Copilot SDK worker is optional and requires Node 22.12+ and its packaged
`package-lock.json`; it is not required for ChatGPT or Gemini browser runs.

Only a succeeded result means every required operation completed. An uncertain
send is retained for reconciliation and is never automatically repeated.

Package source provenance is documented in [PROVENANCE.md](../PROVENANCE.md).

The Copilot repairs are synchronized with runner 0.2.76. From the repository
root, run their focused synthetic checks after installing with `--with-tests`:

```bash
.venv/bin/python -m pytest -q packages/visibility-runner/tests/test_shared_actions_guard_errors.py
.venv/bin/python -m pytest -q packages/visibility-runner/tests/test_visibility_runner_retry_response_waits.py -k 'provider_open_failure_captures_original_page_without_a_send_fence or failure_dom_is_private_correlated_and_never_resends'
.venv/bin/python -m pytest -q packages/visibility-runner/tests/test_copilot_verification_holds.py packages/visibility-runner/tests/test_copilot_blockers.py packages/visibility-runner/tests/test_refetch_item_refusal.py packages/visibility-runner/tests/test_visibility_runner_retry_access.py -k 'preparation_control or after_preparation or preparation_sign_in_control or failed_profile_blocker or active_peer_blocker or false_recovery_result or access_reason_retains'
```

With Chromium already provisioned, the isolated Lexical check covers native
typing, exact multiline readback and a long source-bound parsing draft:

```bash
.venv/bin/python -m pytest -q packages/visibility-runner/tests/test_copilot_lexical_input.py -k two_turn_pipeline_types_and_sends_exact_lexical_text
.venv/bin/python -m pytest -q packages/visibility-runner/tests/test_copilot_browser_dom.py -k 'completed_source_waits or rendered_user_boundary or fresh_chat_sends_two_exact_turns'
.venv/bin/python -m pytest -q packages/visibility-runner/tests/test_copilot_blockers_dom.py -k 'sign_in_preinput_refusal_waits_for_existing_episode_clearance or tooltip_modal'
```
