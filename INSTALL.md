# Installation and agent handoff

## 1. Check the machine

Use Python 3.12 or later, Git and Google Chrome on macOS or Windows 10/11.
Both platforms install the same engine. OS mechanisms live in the separate
platform-runtime package. Native Windows acceptance remains pending; see
`docs/platform-runtime.md`. Linux supports offline checks and the panel; its
Chrome startup is outside this launcher implementation.

```bash
python3 --version
git --version
```

Clone this repository and keep it in one directory. No sibling private project
is needed. Use a separate checkout and environment from other clinic software.

```bash
git clone https://github.com/artemksl/visibility-engine.git
cd visibility-engine
```

## 2. Install the source packages

```bash
./setup.sh --with-tests
# To select a specific interpreter:
VISIBILITY_PYTHON=python3.12 ./setup.sh --with-tests
```

Windows Command Prompt or PowerShell:

```powershell
.\setup.cmd --with-tests
# Alternatively, use an explicitly selected Python:
py -3.12 setup.py --with-tests
```

The script creates `.venv/`, installs the platform layer, shared core, runner and panel from this
checkout, downloads their ordinary dependencies, and runs `pip check` and a local
import check. It does not start Chrome or submit measurements. Do not run it with
`sudo`. Internet access to your configured Python package index is required.
The packages are installed from local source. Rerun setup after source updates;
there is one engine implementation for both platforms.

For automatic browser Copilot verification, add the accessibility
bridge:

```bash
./setup.sh --desktop-verification
```

On Windows use `.\setup.cmd --desktop-verification`. Windows installs pywin32
and the optional UI Automation dependency; macOS retains atomacos and PyAutoGUI.

Grant Accessibility access to the intended terminal/Python application through
macOS System Settings if native verification requires it. Normal chat work uses
background Playwright input. Supported automatic verification may briefly focus
Chrome and use the physical mouse through the owned desktop queue.

For the optional GitHub Copilot source provider, install Node.js 22.12 or later
and npm, then:

```bash
./setup.sh --github-copilot
```

On Windows use `.\setup.cmd --github-copilot` with the standard Node installation.

This installs the pinned SDK worker dependencies from its lockfile. Configure an
authorized Copilot account and credential reference separately; do not put a
token in the repository. Ordinary ChatGPT, Gemini and browser Copilot do not
require Node.js.

## 3. Configure your own Chrome profiles

Create a dedicated Chrome user-data directory for automation. Open that directory
manually, create the desired profiles, and sign in to the intended providers.
Use `chrome://version` in each profile to find its exact Profile Path. The final
component, such as `Default` or `Profile 1`, is its `chrome_directory`.

Copy the examples to ignored local files:

```bash
cp config/profiles.example.json config/profiles.json
cp config/settings.example.json config/settings.json
```

In PowerShell use `Copy-Item` instead of `cp` if needed. Supply a Windows absolute
`user_data_dir`, such as `C:\\VisibilityChrome`; JSON backslashes must be escaped.
Chrome is discovered from its standard installation or the explicit local
`BROWSER_CMD` override. Profile bindings and selection work identically.

Replace the fictional rows with actual profile bindings. Each ID and Chrome
directory must be unique. IDs are local stable labels; display names may be
human-readable. `project_url` may be empty. No real account mapping is shipped.

In `settings.json`, provide a nonempty explicit `selected_profile_ids` list and
set `user_data_dir` to the absolute path of your automation Chrome directory.
Every selected ID must exist in `profiles.json`. The example's placeholder path
is not a usable directory. The default example selects seven profiles with two
owned tabs per platform per profile, a ten-second stagger, and the existing
240-second response deadlines plus 30-second grace. Counts are total observations
distributed across profiles; they are not multiplied by the number of profiles.

An authorized Start or Refetch closes Chrome and reopens the same selected
profiles under browser lifecycle guards. Save other Chrome work first. Refetch
restarts once for the whole automatic loop; Resume continues without restarting.

Optional provider-specific configuration is described in
`config/README.md`. Ask the owner to complete login or account setup when needed;
do not invent account emails or enter new passwords automatically.

## 4. Open and verify the panel

```bash
.venv/bin/python tools/check_install.py
./run.sh
```

On Windows:

```powershell
.\.venv\Scripts\python.exe tools\check_install.py
.\run.cmd
```

Open <http://127.0.0.1:8766/visibility-testing>. Use `./run.sh --port 8767` if
8766 is occupied. The server binds to loopback; keep it local. Stop it with Ctrl+C.

Choose a platform and prompt count or list. **Preview test** validates the
request without sending it. Only choose **Start test** after the owner authorizes
the actual measurements. The Profiles checkboxes start with the explicit pool
in `settings.json`; uncheck blocked accounts for a new test. Saved runs and their
Refetch requests retain their original selection. Review each chat's source answer, ordered extraction,
status and evidence. **Stop** remains effective between automatic recovery
rounds. After five incomplete rounds, resolve provider limits, sign-in problems
or other remaining failures manually before authorizing another recovery.

## 5. Run offline checks

```bash
.venv/bin/python -m pytest -q panel/tests
.venv/bin/python -m pytest -q packages/platform-runtime/tests packages/automation-core/tests
.venv/bin/python tools/check_public_export.py
```

On Windows substitute `.venv\Scripts\python.exe`. The platform tests include
real local process/file checks and synthetic native desktop controls; they do
not open Chrome or send provider prompts.

Focused runner tests are under `packages/visibility-runner/tests`; their package
notes list the portable offline selection. These checks must use temporary
synthetic evidence, never saved browser profiles or live model requests.

## Troubleshooting

- Missing Python or an older interpreter: install Python 3.12+ and set
  `VISIBILITY_PYTHON` to that executable when running setup.
- Import or dependency failures: rerun `./setup.sh` in this checkout, then
  `.venv/bin/python -m pip check`. Do not borrow another project's virtualenv.
- Missing local configuration: edit the two local JSON files before Preview.
  Placeholder profile mappings do not prove those accounts exist or are signed in.
- Chrome unavailable or profile mismatch: check the exact user-data directory,
  profile directories and local Chrome installation. Ownership failures block
  execution; do not remove guards or point at a different account as a fallback.
- Provider usage limit, sign-out or verification: follow the panel's reason.
  Installation success does not imply provider availability. Leave uncertain
  sends intact and let the owner address the provider issue.

Local state is written under ignored `data/` directories. Back up that state and
local configuration separately. Keep credentials, sessions, captured HTML and
saved measurements out of public commits.
