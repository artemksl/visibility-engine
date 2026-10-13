# Local browser configuration

Only fictional `.example.json` files are tracked. Copy and edit them as described
in `../INSTALL.md`; actual `.json` files and `.env` are ignored. Do not copy a
Chrome user-data directory, account session or token into this repository.

`profiles.json` contains exact Chrome directory bindings. `settings.json` chooses
a nonempty explicit subset by ID, the shared user-data directory, the loopback
CDP endpoint and tab concurrency. There is no empty-selection or all-profile
fallback. The examples show seven accounts; use the owner's intended bindings.
An ID is a stable local label, separate from Chrome's `Profile 1` directory name.

The included launcher supports macOS and Windows and resolves files relative to this folder.
A local ignored `.env` may override `BROWSER_CMD` when Chrome is installed at a
different path. The selected manifest, user-data directory and CDP URL are frozen
in each worker request; changing local settings does not rewrite saved jobs.

## Browser Copilot

Install `./setup.sh --desktop-verification` for automatic native verification.
Without it, resolve verification manually; do not remove an ownership check or
force a checkbox click from another app. Native actions remain tied to the exact
owned tab through the desktop queue and lock.

Optional supported saved-account sign-in uses this ignored local file:

```bash
cp config/copilot-sign-in.example.json config/copilot-sign-in.json
```

Replace the example with exact user-approved email identifiers for configured
profiles. These identifiers are not passwords. Missing bindings leave sign-in
for manual attention. Password entry, MFA and unknown consent dialogs remain
outside this mechanism.

## GitHub Copilot with Gemini parsing

This is a separate source provider, requiring Node.js 22.12+, an eligible Copilot
account, its explicit account identity and an explicitly named OS credential.
The engine verifies eligibility before model work. Run
`./setup.sh --github-copilot`, then copy and edit:

```bash
cp config/github-copilot.example.json config/github-copilot.json
```

Set both source and runtime account fields to the same intended account. Set
`credential_ref` to `keychain:<service>:<account>` for an existing authorized
Keychain entry on macOS, or `credential-manager:<target>` for an existing generic
Windows Credential Manager entry. The engine reads that exact target only.
The example is a locator, not a credential. Do not replace it
with a token or commit real credential material. Credential resolution happens
only within authorized execution; Preview never fetches it. The source answer
is retained while Gemini performs source-bound parsing and parser recovery.

