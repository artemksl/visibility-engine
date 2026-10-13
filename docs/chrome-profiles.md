# Connect Chrome profiles to the engine

The engine uses saved Chrome profiles from one dedicated user-data folder.
Sign in to the provider websites in those profiles, then register their exact
folder names in `config/profiles.json` and select their IDs in
`config/settings.json`. The same configuration format works on macOS and Windows.
Complete [installation](../INSTALL.md) first. Windows validation limitations are
recorded in [the platform notes](platform-runtime.md).

## 1. Open the automation Chrome folder

If you already have a dedicated automation Chrome folder, use its existing path
in the commands below and keep its saved profiles. Otherwise these commands
create a separate folder on first launch.

macOS, in Terminal:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --user-data-dir="$HOME/VisibilityChrome" --no-first-run
```

Windows, in PowerShell:

```powershell
$chrome = @(
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
  "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $chrome) { throw "Chrome was not found. Supply its installed executable path." }
& $chrome "--user-data-dir=$env:LOCALAPPDATA\VisibilityChrome" --no-first-run
```

Keep using this same folder for setup and measurements. Since Chrome 136,
remote debugging of the normal default Chrome data folder is disabled; a
non-default `--user-data-dir` is required for this connection. See
[Chrome's remote debugging change](https://developer.chrome.com/blog/remote-debugging-port).
Existing profiles in your ordinary Chrome installation are not automatically
available in the new folder. Create the intended profiles here and sign in
manually; do not copy browser sessions between machines or into the repository.

## 2. Create profiles and sign in

Use Chrome's profile menu to create as many profiles as you intend to use.
One profile is sufficient; seven is only the configuration example's size.
In each profile, open the providers you will measure and sign in to the intended
accounts:

- [ChatGPT](https://chatgpt.com/)
- [Gemini](https://gemini.google.com/)
- [Browser Copilot](https://copilot.microsoft.com/)

Confirm the account in each website. Chrome's profile display name and Chrome
sync account do not prove which account a provider website is using. Login,
passwords, MFA and any account setup are the owner's manual setup steps.

In **each profile window**, open `chrome://version` and record **Profile Path**.
The path splits into two configuration values:

```text
<automation user-data folder>/Profile 1
                            └──────── chrome_directory: "Profile 1"
└─────────────────────────── user_data_dir: the parent folder
```

On Windows the separator will normally be `\`. `Default` is also a valid
profile folder name. Copy the exact final component, including spaces and case;
do not guess it from the profile's display name. All selected Profile Paths must
have the same parent. That parent contains `Local State` and the profile folders.

## 3. Register the exact profile bindings

For a new installation, run these commands from the repository folder. If local
configuration already exists, edit it instead of overwriting it.

macOS:

```bash
cp config/profiles.example.json config/profiles.json
cp config/settings.example.json config/settings.json
```

Windows PowerShell:

```powershell
Copy-Item config/profiles.example.json config/profiles.json
Copy-Item config/settings.example.json config/settings.json
```

Replace `config/profiles.json` with your actual bindings. For example, if the
two Profile Paths really end in `Default` and `Profile 1`:

```json
[
  {
    "id": "account_1",
    "display_name": "Account 1",
    "chrome_directory": "Default",
    "project_url": ""
  },
  {
    "id": "account_2",
    "display_name": "Account 2",
    "chrome_directory": "Profile 1",
    "project_url": ""
  }
]
```

`id` is your stable engine label: use lowercase letters, digits and single
underscores between words. `display_name` is a friendly catalog label; the
testing panel's profile checkboxes show the IDs.
`chrome_directory` is the exact folder name from Chrome, not a full path.
Both IDs and folder names must be unique. Leave `project_url` empty for ordinary
fresh-chat measurements. No provider password, token or session file is needed
in this configuration.

## 4. Set the root folder and explicit selection

Edit `config/settings.json`:

```json
{
  "selected_profile_ids": ["account_1", "account_2"],
  "tabs_per_provider": 2,
  "packet_start_stagger_seconds": 10,
  "cdp_url": "http://127.0.0.1:9222",
  "user_data_dir": "/absolute/path/to/VisibilityChrome"
}
```

Replace the placeholder with the **actual parent from Profile Path**. On macOS,
the first command's folder is `$HOME/VisibilityChrome`; put the expanded absolute
path in JSON, not the literal `$HOME`. On Windows it is
`$env:LOCALAPPDATA\VisibilityChrome`; put the expanded path in JSON, not the
PowerShell expression. Windows paths can use forward slashes, for example
`C:/Path/To/VisibilityChrome`, or escaped backslashes,
`C:\\Path\\To\\VisibilityChrome`.

Select only IDs registered in `profiles.json`. To use just the first profile,
set `selected_profile_ids` to `["account_1"]`. Empty selection and an automatic
all-profile fallback are disabled. `tabs_per_provider` controls the simultaneous
owned tabs per selected profile and provider; it does not create Chrome profiles.

Keep the CDP endpoint on loopback. The engine enables this Chrome connection
during an authorized startup; you do not need to open a debugging port manually.
The panel's port `8766` and Chrome's port `9222` serve different purposes.

## 5. Check the panel and start authorized work

Close the setup Chrome windows after signing in. Start the panel with
`./run.sh` on macOS or `.\run.cmd` on Windows, then open
<http://127.0.0.1:8766/visibility-testing>. Reload the panel after configuration
changes and check the **Profiles** selection before creating a new test.

**Preview test** checks the request and profile configuration without sending
prompts. It does not prove that Chrome can attach or that a provider is signed in.
Only use **Start test** after authorizing the real measurements. Start and
explicit Refetch close Chrome and reopen the selected saved profiles, so save
other Chrome work first. The engine identifies each opened profile through its
owned anchor tab and opens the measurement tabs itself. Keep its anchor tabs.

Saved runs retain their frozen profile selection. Changing local configuration
or unchecking a profile affects new tests; it does not replace an account in an
existing run. See [provider-specific configuration](../config/README.md) for
optional Copilot verification and saved-account handling.

## Connection problems

| Symptom | Check |
| --- | --- |
| Profile directory missing | `user_data_dir` is the parent and `chrome_directory` exactly matches the final component of Profile Path. |
| Wrong account opens | Recheck `chrome://version` in that profile and the account inside the provider website. Correct the binding for a new test. |
| Chrome does not attach | Use a dedicated non-default data folder, the correct local Chrome executable and an unused loopback CDP port. Follow the panel's startup reason. |
| Provider is signed out or limited | Open that exact automation profile manually and resolve its login or availability. Registering a profile does not log it in. |
| JSON path error on Windows | Use forward slashes or double each backslash; replace environment expressions with the expanded absolute path. |

Chrome in a custom installation can be configured using the local ignored
`config/.env` file. Set `BROWSER_CMD` to its executable path; on Windows this must
be an absolute path to `chrome.exe` without extra command-line flags. Actual
profile configuration is local and ignored by Git. Keep the Chrome user-data
folder outside this checkout and back it up separately.
