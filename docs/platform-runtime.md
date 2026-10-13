# Windows and macOS platform boundary

The panel, scheduler, provider adapters, action executor, response correlation,
send admission and recovery remain one shared implementation. Future engine
changes therefore apply to both platforms. Do not add a Windows engine fork.

`packages/platform-runtime` supplies OS mechanisms through small modules:

| Module | macOS | Windows |
| --- | --- | --- |
| `paths` | Existing Chrome/cache paths | Chrome App Paths and standard installation/data directories |
| `file_locks` | Native `flock` on existing lock-file inodes | Native shared/exclusive `LockFileEx`; contention refuses work |
| `storage` | File/full sync, atomic publication and directory sync | Flushed files and same-volume write-through rename; no-overwrite publication |
| `security` | Owner UID, modes and no-follow open | Owner SID, private ACLs and reparse-point refusal |
| `processes` | Existing boot session and process-group operations | OS boot identity, checked process identity, child cleanup and Job Objects |
| `credentials` | Explicit environment/Keychain lookup | Explicit environment/generic Credential Manager lookup |
| `desktop` | Existing AXBridge factories | UI Automation observations and native pointer driver supplied to those same factories |

The Windows desktop reader translates UI Automation roles/states into the
existing AXBridge contract. Challenge recognition, exact-tab/foreground guards,
one guarded click, deadlines, cancellation and organic mouse motion remain
shared. Normal browser input still uses the imported asynchronous executor.
Unsupported native controls remain a correlated hold for manual attention.

The panel's Windows launcher is `config/launch_logged_in.py`; macOS delegates to
the existing shell launcher. Both use the same selected manifest, clean-start
preparation, lifecycle lock, profile anchors and startup barriers. Windows asks
Chrome to open minimized without activation; Chrome's actual focus behavior
needs verification on Windows. Authorized Start/Refetch still restart Chrome;
Resume and finalization do not. Startup mechanics do not grant run permission.

The optional Node SDK worker contains a thin file-publication bridge to the
installed Python platform layer. It receives the exact frozen interpreter and
helper path; it does not implement a second recovery or send-fence algorithm.

Lock callers explicitly acquire once, retain their lease and unlock before
closing. Shared-to-exclusive transitions may release and reacquire, as native
advisory conversion does; failed promotion restores the original reader before
returning a refusal. Windows lock bookkeeping is never proof of acquisition:
even a reused descriptor/native handle must obtain a real OS lock. Application
lease nesting is handled by the existing shared lifecycle object.

Install with `setup.cmd` / `run.cmd` on Windows or `./setup.sh` / `./run.sh` on
macOS. Both invoke shared Python bootstrap files and install the local packages
normally. Rerun setup after source changes. Desktop verification is optional:
add `--desktop-verification`. Provider accounts and saved profiles are local
configuration, not portable browser-session exports.

`run.py` freezes the checkout root before starting the installed panel. Its
configuration, worker scripts and run directories stay outside site-packages.
The optional SDK dependencies are installed beside the actual installed worker;
rerun setup with `--github-copilot` after updating that package. Serialized files
use explicit UTF-8 on both platforms, including paths with accented characters.

## Verification status and remaining Windows acceptance

macOS regression checks, real independent-process file contention, temporary
storage and installed-package checks pass. Strict Win32/UI Automation doubles
cover native signatures, ownership refusal, write-through publication, process
cleanup and shared verification/motion wiring. They do not prove native Windows
behavior. No live provider run was performed for this portability change.

The final installed-package selection passed 219 checks. The public setup command
passed with tests, desktop verification and the optional SDK enabled. An isolated
HTTP check served the installed panel and assets using temporary state. A macOS
and Windows CI matrix runs the same offline selection. The
[first public CI run](https://github.com/artemksl/visibility-engine/actions/runs/38462367145)
passed on macOS. Windows installation passed, but its offline selection reported
8 failures, 210 passes and 1 skip: the private-resource checks rejected newly
created files or directories because their owner SID differed from the current
user SID. This Windows ownership issue needs repair and native verification;
installation alone is not Windows qualification. Desktop-binding tests require
the optional desktop extra.

Before claiming Windows live parity, on an actual Windows machine:

1. Run setup, `pip check`, `tools/check_install.py` and the offline suites listed
   in INSTALL. Ensure independent-process contention and private ACL checks run
   against the native backend.
2. Use dedicated synthetic profiles and an explicit selection to verify startup,
   exact profile anchors, two concurrent tabs and Stop/worker cleanup. Confirm
   unrelated processes and profile selections are never substituted.
3. Verify minimized startup, ordinary browser input and native verification
   separately, including focus return, display scaling and an unsupported
   challenge retaining its hold. Real provider input requires owner authorization.
4. Confirm the same two-turn result, uncertainty retention and bounded recovery
   behavior using authorized measurements. Retain the evidence; do not treat
   installation success as provider availability or live qualification.

Windows native offline acceptance is blocked by the ownership failures above.
Live Chrome and desktop qualification remain pending.
