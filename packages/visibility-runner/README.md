# Visibility Runner 0.2.55

The measurement engine used by the Visibility Engine testing panel. Install the
combined repository using the instructions in [the root README](../../README.md).

The engine owns planning, profile and tab ownership, browser input, response
correlation, ordered-name extraction, validation, evidence and recovery. It uses
one fresh two-turn conversation per browser measurement. Shared browser input
comes from the sibling `automation-core` package.

The optional macOS `desktop` extra uses the sibling `axbridge` package. The
GitHub Copilot SDK worker is optional and requires Node 22.12+ and its packaged
`package-lock.json`; it is not required for ChatGPT or Gemini browser runs.

Only a succeeded result means every required operation completed. An uncertain
send is retained for reconciliation and is never automatically repeated.

Package source provenance is documented in [PROVENANCE.md](../PROVENANCE.md).
