# Visibility engine

A local testing panel and browser measurement engine in one checkout. Submit a
list of prompts to ChatGPT, Gemini or browser Copilot, collect each answer and its
ordered clinic-name extraction in the same conversation, and inspect saved chat
results. GitHub Copilot with a Gemini parser is an optional provider.

The panel retains Preview, Start, Stop, run history and recovery controls.
Resume/Refetch performs up to five automatic recovery rounds, stops early when
all results are filled, and leaves remaining failures for manual handling.
Completed results remain saved. An uncertain send is checked for a late answer
and is never automatically resent.

## Install and open

Use Python 3.12 or later. Browser startup and desktop verification currently
support macOS with Google Chrome. The panel and offline tests also run on Linux;
Windows is not supported by the current filesystem locks.

```bash
./setup.sh --with-tests
./run.sh
```

Open <http://127.0.0.1:8766/visibility-testing>. To choose another port, use
`./run.sh --port 8767`. Opening the panel and using Preview do not send prompts.
Configure your own signed-in Chrome profiles before starting measurements.

Follow [INSTALL.md](INSTALL.md) for configuration, optional providers, verification
and troubleshooting. An installing agent should read [AGENTS.md](AGENTS.md) first.

## Contents

| Folder | Responsibility |
| --- | --- |
| `panel/` | Testing interface, local job control and guarded worker integration |
| `packages/visibility-runner/` | Runner, scheduler, ownership, website adapters, evidence and recovery |
| `packages/automation-core/` | Shared asynchronous action executor and organic pointer movement |
| `packages/axbridge/` | Optional macOS accessibility bridge for Copilot verification |
| `config/` | Browser launcher and fictional configuration examples |
| `scripts/` | Worker launched by the panel |

This is a fresh source export. It contains no saved runs, personal profile
bindings, browser sessions, credentials or original repository history. Runtime
data and local configuration are ignored by Git. Public availability does not
grant an open-source license; no license has been selected for this code.

