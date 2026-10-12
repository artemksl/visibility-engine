# Package source provenance

This directory contains source-installable workspace packages rather than opaque
release wheels. The package bodies preserve the releases used by the testing
panel at extraction:

| Directory | Distribution | Version | Source preserved |
| --- | --- | --- | --- |
| `visibility-runner` | `visibility-runner` | 0.2.55 | Engine Python modules, prompt/schema resources and optional Node worker |
| `automation-core` | `workspace-automation-core` | 0.1.2 | All 39 core Python modules, including the verified organic movement executor |
| `axbridge` | `axbridge` | 0.2.3 | All 15 Python modules from the installed optional macOS verification release |

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
