#!/usr/bin/env bash
#
# launch_logged_in.sh — open the configured logged-in browser profiles (one window each, in
# order) with remote debugging, for the "logged_in" regime.
#
# Reads the profile list from profiles.json. Canonical rows use a stable CiteUp ``id``, a
# human-facing ``display_name``, and Chrome's immutable ``chrome_directory`` (for example
# "Profile 5" or "Default"). Legacy ``profile``/``label`` rows remain readable. Browser launch and
# the compatible ``citeup-profile`` anchor marker deliberately continue to use the raw Chrome
# directory. Rows are opened in file order and only those identities should be active.
#
# Chrome allows one process per user-data-dir, so this quits any running Chrome and reopens just
# these profiles WITH debugging (session restore preserves your logins).

set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"     # config/
ROOT="$(cd "$DIR/.." && pwd)"                            # repo root

[ -f "$DIR/.env" ] && set -a && . "$DIR/.env" >/dev/null 2>&1 && set +a || true

# A workflow can pin its own profile set (e.g. just "Default") via LOGGEDIN_PROFILES_PATH.
CONFIG="${LOGGEDIN_PROFILES_PATH:-$DIR/profiles.json}"
CDP_ENDPOINT="http://127.0.0.1:9222"
FORCE_CLOSE="no"
CLEAN_START="no"
EXPLICIT_USER_DATA_DIR=""
EXPLICIT_PROFILES="no"
EXPLICIT_CDP="no"

# Frozen workflow arguments deliberately win over values loaded from .env.
# Parse function-local arguments so the lifecycle wrapper can forward the
# original arguments intact when it reinvokes this script under its lock.
parse_launcher_arguments() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --clean-start)
        CLEAN_START="yes"; shift ;;
      --user-data-dir)
        [ "$#" -ge 2 ] && [ -n "$2" ] && [[ "$2" != --* ]] || {
          echo "✗ --user-data-dir needs a saved Chrome directory." >&2; return 1;
        }
        EXPLICIT_USER_DATA_DIR="$2"; shift 2 ;;
      --profiles-path)
        [ "$#" -ge 2 ] && [ -n "$2" ] && [[ "$2" != --* ]] || {
          echo "✗ --profiles-path requires an explicit non-empty path." >&2; return 1;
        }
        CONFIG="$2"; EXPLICIT_PROFILES="yes"; shift 2 ;;
      --cdp-url)
        [ "$#" -ge 2 ] && [ -n "$2" ] && [[ "$2" != --* ]] || {
          echo "✗ --cdp-url requires an explicit URL." >&2; return 1;
        }
        CDP_ENDPOINT="$2"; EXPLICIT_CDP="yes"; shift 2 ;;
      --force-close)
        FORCE_CLOSE="yes"; shift ;;
      *) echo "✗ Unknown launcher argument: $1" >&2; return 1 ;;
    esac
  done
}
parse_launcher_arguments "$@" || exit 1
if { [ "$FORCE_CLOSE" = "yes" ] || [ "$CLEAN_START" = "yes" ]; } && { [ "$EXPLICIT_PROFILES" != "yes" ] || [ "$EXPLICIT_CDP" != "yes" ]; }; then
  echo "✗ --force-close requires explicit --profiles-path and --cdp-url arguments." >&2; exit 1
fi

CHROME="${BROWSER_CMD:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
USER_DATA_DIR="${EXPLICIT_USER_DATA_DIR:-${CHROME_USER_DATA_DIR:-$HOME/Library/Application Support/Google/Chrome}}"
# The anchor only identifies the real Chrome profile over CDP. Keeping it
# local avoids losing that identity when a provider has a transient network
# error during Chrome startup. Workers navigate their own profile-owned popup
# to ChatGPT, Gemini, Google, or Copilot after matching the anchor.
PROFILE_ANCHOR_URL="${PROFILE_ANCHOR_URL:-$DIR/profile_anchor.html}"
PY="${PYTHON:-python3}"

DEBUG_ADDRESS=$("$PY" - "$CDP_ENDPOINT" <<'PY'
import sys
from urllib.parse import urlsplit

try:
    value = urlsplit(sys.argv[1])
    if (value.scheme != "http" or value.hostname not in {"127.0.0.1", "localhost"}
            or value.port is None or not 1 <= value.port <= 65535
            or value.username is not None or value.password is not None
            or value.path not in {"", "/"} or value.query or value.fragment):
        raise ValueError
except ValueError:
    print("✗ --cdp-url must be an HTTP loopback URL with an explicit port.", file=sys.stderr)
    raise SystemExit(1)
print(f"127.0.0.1 {value.port}")
PY
) || exit 1
read -r DEBUG_HOST DEBUG_PORT <<< "$DEBUG_ADDRESS"

if [ ! -f "$CONFIG" ]; then
  echo "✗ $CONFIG not found. Create it: [{\"id\":\"account_name\",\"display_name\":\"loggedin(account.name)\",\"chrome_directory\":\"Profile 5\",\"project_url\":\"…\"}, …]" >&2; exit 1
fi
if [ ! -x "$CHROME" ]; then
  echo "✗ Chrome not found at: $CHROME" >&2; exit 1
fi

# Stable ids, friendly display names and raw Chrome directory names, in file order.
# Bash 3.2 has no mapfile, so retain the read loop.
PROFILE_IDS=()
DISPLAY_NAMES=()
CHROME_DIRECTORIES=()
while IFS=$'\t' read -r profile_id chrome_directory display_name; do
  if [ -n "$chrome_directory" ]; then
    PROFILE_IDS+=("${profile_id:-$chrome_directory}")
    CHROME_DIRECTORIES+=("$chrome_directory")
    DISPLAY_NAMES+=("${display_name:-$chrome_directory}")
  fi
done < <("$PY" - "$CONFIG" <<'PY'
import json
import re
import sys

def fallback_id(display_name, chrome_directory):
    value = str(display_name or "").strip()
    wrapped = re.fullmatch(r"loggedin\((.+)\)", value, flags=re.IGNORECASE)
    if wrapped:
        value = wrapped.group(1)
    elif re.fullmatch(r"loggedin\d+", value, flags=re.IGNORECASE):
        value = re.sub(r"(?i)^loggedin", "loggedin_", value)
    value = value or str(chrome_directory or "").strip()
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_") or "chrome_profile"

def clean(value):
    return str(value or "").replace("\t", " ").replace("\n", " ").strip()

with open(sys.argv[1], encoding="utf-8") as handle:
    rows = json.load(handle)
for row in rows:
    if not isinstance(row, dict):
        continue
    chrome_directory = clean(row.get("chrome_directory") or row.get("profile"))
    if not chrome_directory:
        continue
    display_name = clean(
        row.get("display_name") or row.get("label") or row.get("name")
    ) or chrome_directory
    profile_id = clean(row.get("id")) or fallback_id(display_name, chrome_directory)
    print("\t".join((profile_id, chrome_directory, display_name)))
PY
)
if [ "${#CHROME_DIRECTORIES[@]}" -eq 0 ]; then
  echo "✗ No profiles in $CONFIG." >&2; exit 1
fi

# Fail before quitting Chrome. A typo in a raw directory name would otherwise make Chrome create a
# fresh empty profile, which looks like an unexpected sign-out on every platform.
for ((idx = 0; idx < ${#CHROME_DIRECTORIES[@]}; idx++)); do
  chrome_directory="${CHROME_DIRECTORIES[$idx]}"
  if [ ! -d "$USER_DATA_DIR/$chrome_directory" ]; then
    echo "✗ Chrome profile directory does not exist: $USER_DATA_DIR/$chrome_directory (${PROFILE_IDS[$idx]}: ${DISPLAY_NAMES[$idx]})" >&2
    exit 1
  fi
  for ((previous = 0; previous < idx; previous++)); do
    if [ "${CHROME_DIRECTORIES[$previous]}" = "$chrome_directory" ]; then
      echo "✗ Duplicate Chrome profile directory in $CONFIG: $chrome_directory" >&2
      exit 1
    fi
    if [ "${PROFILE_IDS[$previous]}" = "${PROFILE_IDS[$idx]}" ]; then
      echo "✗ Duplicate logged-in profile id in $CONFIG: ${PROFILE_IDS[$idx]}" >&2
      exit 1
    fi
  done
done
echo "Opening ${#CHROME_DIRECTORIES[@]} logged-in identities: ${PROFILE_IDS[*]}"

CLEAN_START_HELPER="$ROOT/panel/src/visibility_panel/execution/chrome_clean_start.py"
if [ "$CLEAN_START" = "yes" ]; then
  "$PY" "$CLEAN_START_HELPER" validate "$USER_DATA_DIR" "${CHROME_DIRECTORIES[@]}" || exit 1
fi

# Direct/manual launcher calls must obey the same lifecycle lease as Python
# preflight. check_chrome.py sets the held marker while its exclusive lock is
# live; otherwise this wrapper retains exclusive ownership for this script's
# entire destructive quit/reopen sequence.
LIFECYCLE_GUARD="$ROOT/panel/src/visibility_panel/browser/browser_lifecycle.py"
if [ "${CITEUP_BROWSER_LIFECYCLE_LOCK_HELD:-0}" != "1" ] && [ -f "$LIFECYCLE_GUARD" ]; then
  exec env \
    CHROME_USER_DATA_DIR="$USER_DATA_DIR" \
    CDP_URL="$CDP_ENDPOINT" \
    "$PY" "$LIFECYCLE_GUARD" --run-exclusive "$0" "$@"
fi

if pgrep -x "Google Chrome" >/dev/null 2>&1; then
  echo "Quitting running Chrome..."
  if [ "$FORCE_CLOSE" = "yes" ]; then
    osascript -e 'with timeout of 10 seconds' -e 'quit app "Google Chrome"' -e 'end timeout' >/dev/null 2>&1 || true
  else
    osascript -e 'quit app "Google Chrome"' >/dev/null 2>&1 || true
  fi
  for _ in $(seq 1 20); do pgrep -x "Google Chrome" >/dev/null 2>&1 || break; sleep 0.5; done
  if [ "$FORCE_CLOSE" = "yes" ] && pgrep -x "Google Chrome" >/dev/null 2>&1; then
    echo "Chrome did not quit; terminating the remaining Google Chrome process..."
    pkill -TERM -x "Google Chrome" >/dev/null 2>&1 || true
    for _ in $(seq 1 10); do pgrep -x "Google Chrome" >/dev/null 2>&1 || break; sleep 0.5; done
  fi
  if [ "$FORCE_CLOSE" = "yes" ] && pgrep -x "Google Chrome" >/dev/null 2>&1; then
    echo "Chrome is still running; force-closing the remaining Google Chrome process..."
    pkill -KILL -x "Google Chrome" >/dev/null 2>&1 || true
    for _ in $(seq 1 10); do pgrep -x "Google Chrome" >/dev/null 2>&1 || break; sleep 0.5; done
  fi
  if pgrep -x "Google Chrome" >/dev/null 2>&1; then
    echo "✗ Chrome is still running; quit it manually and re-run." >&2; exit 1
  fi
fi
rm -f "$USER_DATA_DIR/SingletonLock" "$USER_DATA_DIR/SingletonCookie" "$USER_DATA_DIR/SingletonSocket" 2>/dev/null || true

if [ "$CLEAN_START" = "yes" ]; then
  # Re-read after shutdown: Chrome may have written its last preferences on exit.
  # Preserve cookies, account storage, history, extensions and session files.
  "$PY" "$CLEAN_START_HELPER" apply "$USER_DATA_DIR" "${CHROME_DIRECTORIES[@]}" || exit 1
fi

# Chrome remembers every profile that was active when it last quit and may restore all of them
# before processing --profile-directory. Limit that automatic restore to the first requested
# profile; the loop below opens the remaining requested profiles explicitly. Without this, a
# "logged in only" launch can silently bring back windows from the rotating profile pool.
LOCAL_STATE="$USER_DATA_DIR/Local State"
if [ -f "$LOCAL_STATE" ]; then
  "$PY" - "$LOCAL_STATE" "${CHROME_DIRECTORIES[0]}" <<'PY'
import json
import os
import sys

path, first_chrome_directory = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    state = json.load(handle)
profile = state.setdefault("profile", {})
profile["last_active_profiles"] = [first_chrome_directory]
profile["last_used"] = first_chrome_directory
temporary = f"{path}.logged-in-launch.tmp"
with open(temporary, "w", encoding="utf-8") as handle:
    json.dump(state, handle, separators=(",", ":"))
os.replace(temporary, path)
PY
fi

# Keep the tabs running at FULL SPEED even when the window is hidden / minimized / occluded, so
# the run can sit in the background out of your way without Chrome throttling its background tabs.
BG_FLAGS=(--disable-background-timer-throttling --disable-backgrounding-occluded-windows
          --disable-renderer-backgrounding --disable-features=CalculateNativeWinOcclusion)

# The Copilot verification reader (AX Bridge, used by visibility-runner 0.2.26+)
# finds the "Verify you are human" checkbox through the macOS accessibility API.
# Chrome shows web page content to that API only while its accessibility mode is
# on, and the mode is off by default (see chrome://accessibility). Without this
# flag the reader finds no AXWebArea and the verification stops.
# "complete" is the mode AX Bridge's own demo launcher uses (axbridge/demo.py).
AX_FLAGS=(--force-renderer-accessibility=complete)

# LaunchServices keeps normal macOS startup hidden without activating Chrome.
# A new launch delivers each profile's arguments to Chrome's singleton process;
# an ordinary open of an already-running app would ignore those arguments.
CHROME_LAUNCH=(nohup "$CHROME")
if [[ "$CHROME" == *.app/Contents/MacOS/* ]] && [ "$(uname -s)" = "Darwin" ]; then
  CHROME_LAUNCH=(open -g -j -n -a "${CHROME%/Contents/MacOS/*}" --args)
fi

profile_anchor_url() {  # $1=raw Chrome directory (legacy-compatible marker value)
  "$PY" - "$PROFILE_ANCHOR_URL" "$1" <<'PY'
import sys
from pathlib import Path
from urllib.parse import quote

base, chrome_directory = sys.argv[1:]
if "://" not in base and not base.startswith(("about:", "data:")):
    base = Path(base).resolve().as_uri()
base = base.split("#", 1)[0]
print(f"{base}#citeup-profile={quote(chrome_directory, safe='')}")
PY
}

open_profile() {  # $1=raw Chrome directory  $2=with_debug(yes/no)
  local chrome_directory="$1"
  local anchor_url
  anchor_url="$(profile_anchor_url "$chrome_directory")"
  if [ "$2" = "yes" ]; then
    "${CHROME_LAUNCH[@]}" --remote-debugging-address="$DEBUG_HOST" --remote-debugging-port="$DEBUG_PORT" \
      --user-data-dir="$USER_DATA_DIR" --profile-directory="$chrome_directory" \
      --no-first-run --no-default-browser-check --disable-popup-blocking "${BG_FLAGS[@]}" "${AX_FLAGS[@]}" \
      --new-window "$anchor_url" >/dev/null 2>&1 &
  else
    "${CHROME_LAUNCH[@]}" --user-data-dir="$USER_DATA_DIR" --profile-directory="$chrome_directory" \
      --no-first-run --no-default-browser-check --disable-popup-blocking "${BG_FLAGS[@]}" "${AX_FLAGS[@]}" \
      --new-window "$anchor_url" >/dev/null 2>&1 &
  fi
  disown 2>/dev/null || true
}

# First profile owns the debug port; wait for it; then open the rest as added windows.
open_profile "${CHROME_DIRECTORIES[0]}" yes
ready="no"
for _ in $(seq 1 30); do
  curl --connect-timeout 1 --max-time 2 -fsS -o /dev/null "http://${DEBUG_HOST}:${DEBUG_PORT}/json/version" && { ready="yes"; break; }
  sleep 0.5
done
[ "$ready" = "yes" ] || { echo "✗ Timed out waiting for the debug port." >&2; exit 1; }
# open the remaining profiles (none when there is only one — C-style loop avoids `seq 1 0` + set -u).
for ((idx = 1; idx < ${#CHROME_DIRECTORIES[@]}; idx++)); do
  open_profile "${CHROME_DIRECTORIES[$idx]}" no
  sleep 0.6
done
echo "✓ ${#CHROME_DIRECTORIES[@]} logged-in identity window(s) ready: ${PROFILE_IDS[*]}. Debug: http://${DEBUG_HOST}:${DEBUG_PORT}"
