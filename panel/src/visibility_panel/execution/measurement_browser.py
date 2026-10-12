"""Prepare externally owned Chrome before a new Start or explicit Refetch.

The launcher only opens local profile anchors. Visibility-runner still owns
every measurement tab, provider interaction and send. A shared lifecycle lease
protects those tabs until the worker has finished using the browser.
"""

from contextlib import ExitStack, contextmanager, nullcontext
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from visibility_panel.browser.browser_lifecycle import (
    LOCK_HELD_ENV, LOCK_PATH_ENV, shared_browser_lifecycle,
)
from visibility_panel.integrity import canonical_sha256
from visibility_panel.execution.worker_registry import publish_json, read_record
from visibility_panel.execution.measurement_inputs import FrozenExecutionRequest


class BrowserStartupCancelled(RuntimeError):
    """Stop was requested before measurement could begin."""


# Trial setting, off by default. Measured on run 20260918T191851Z-02734817:
# page openings timed out almost only in the first two minutes after the
# restart. This wait lets that be tested without changing the engine or a
# run's frozen execution policy. Read from the web server's environment.
POST_RESTART_WAIT_ENV = "CITEUP_POST_RESTART_WAIT_SECONDS"
POST_RESTART_WAIT_MAX_SECONDS = 300.0


def _post_restart_wait_seconds() -> float:
    raw = os.environ.get(POST_RESTART_WAIT_ENV, "").strip()
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{POST_RESTART_WAIT_ENV} must be a number of seconds") from None
    if not math.isfinite(value) or not 0 <= value <= POST_RESTART_WAIT_MAX_SECONDS:
        raise ValueError(f"{POST_RESTART_WAIT_ENV} must be between 0 and {POST_RESTART_WAIT_MAX_SECONDS:.0f}")
    return value


def _wait_after_restart(seconds: float, *, check_cancelled=lambda: None) -> None:
    if seconds <= 0:
        return
    print(f"[browser] Waiting {seconds:.0f} s after the Chrome restart before measurement starts", flush=True)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        check_cancelled()
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    check_cancelled()


def _wait_for_profiles(endpoint: str, directories: list[str], *, timeout: float = 40,
                       check_cancelled=lambda: None) -> None:
    deadline = time.monotonic() + timeout
    while True:
        check_cancelled()
        try:
            with urlopen(endpoint.rstrip("/") + "/json/list", timeout=1) as response:
                pages = json.load(response)
            if not isinstance(pages, list) or any(not isinstance(page, dict) for page in pages):
                raise ValueError("Chrome tab inventory is incomplete")
            anchors = []
            page_count = 0
            for page in pages:
                if not isinstance(page, dict) or page.get("type") != "page":
                    continue
                page_count += 1
                url = urlsplit(str(page.get("url") or ""))
                for component in (url.fragment, url.query):
                    anchors.extend(parse_qs(component).get("citeup-profile", []))
            observed, selected = set(anchors), set(directories)
            if len(anchors) == len(directories) == page_count and observed == selected:
                return
        except (OSError, ValueError, TypeError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("Chrome's selected profile anchors are unavailable; no measurement was started")
        time.sleep(0.25)


def _selected_profile_rows(runtime: dict) -> list[dict]:
    profiles = runtime["profiles"]
    rows = json.loads(Path(profiles["manifest_path"]).read_text(encoding="utf-8"))
    if canonical_sha256(rows) != profiles["manifest_sha256"]:
        raise ValueError("Chrome startup profile manifest integrity mismatch")
    selected = profiles["selected_profile_ids"]
    if not rows or [row["id"] for row in rows] != selected:
        raise ValueError("Chrome startup requires the exact explicit profile selection")
    return rows


def _restart_chrome(runtime: dict, *, root: Path, lease, prepare_restart=None, check_cancelled=lambda: None) -> None:
    rows = _selected_profile_rows(runtime)
    # Validate the trial setting before Chrome is closed.
    post_restart_wait = _post_restart_wait_seconds()
    directories = [row["chrome_directory"] for row in rows]
    endpoint = runtime["browser"]["cdp_url"]
    launcher = root / "config" / "launch_logged_in.sh"
    environment = dict(os.environ)
    environment.update({LOCK_HELD_ENV: "1", LOCK_PATH_ENV: str(lease.path), "PYTHON": sys.executable})
    # Pass a verified private copy: the launcher must not use a broader catalog
    # or reread a manifest that changed between validation and process startup.
    with tempfile.TemporaryDirectory(prefix="visibility-chrome-start-") as directory:
        manifest = Path(directory) / "profiles.json"
        manifest.write_text(json.dumps(rows), encoding="utf-8")
        check_cancelled()
        barrier = prepare_restart() if prepare_restart is not None else None
        with barrier if barrier is not None else nullcontext():
            check_cancelled()
            print("[browser] Closing Chrome and reopening selected profiles for measurement", flush=True)
            command = ["/bin/bash", str(launcher), "--profiles-path", str(manifest), "--clean-start"]
            if runtime["browser"].get("user_data_dir"):
                command.extend(["--user-data-dir", str(runtime["browser"]["user_data_dir"])])
            command.extend(["--cdp-url", endpoint, "--force-close"])
            subprocess.run(command, cwd=root, env=environment, check=True, timeout=90)
            check_cancelled()
            _wait_for_profiles(endpoint, directories, check_cancelled=check_cancelled)
            _wait_after_restart(post_restart_wait, check_cancelled=check_cancelled)
    print("[browser] Chrome automation and selected profiles are ready", flush=True)


@contextmanager
def measurement_browser_session(runtime: dict, request: dict, *, root: Path, prepare_restart=None, check_cancelled=lambda: None):
    """Restart on Start/Refetch; keep subsequent measurement in the background."""
    if request["mode"] == "finalize":
        yield
        return
    endpoint = runtime.get("browser", {}).get("cdp_url", "http://127.0.0.1:9222")
    background_only = runtime.get("browser", {}).get("background_only", True)
    if type(background_only) is not bool:
        raise ValueError("browser background_only must be a boolean")
    restart = request["mode"] in {"run", "refetch"}
    # Never wait indefinitely before the engine has created cancellable state.
    with shared_browser_lifecycle(environment={"CDP_URL": endpoint}, blocking=False) as lease:
        check_cancelled()
        if restart:
            with lease.exclusive(blocking=False):
                _restart_chrome(runtime, root=root, lease=lease, prepare_restart=prepare_restart,
                                check_cancelled=check_cancelled)
        check_cancelled()
        yield


class MeasurementBrowserCoordinator:
    """Keep restart preparation before admission and Chrome after source checks.

    Engine maintenance leases and the exclusive host lease span preparation and
    source admission. The deferred startup releases both before provider work;
    the shared host lease remains held through host result publication.
    """

    def __init__(self, component, runtime: dict, request: dict, *, root: Path,
                 execution_dir: Path, execution: FrozenExecutionRequest | None = None,
                 check_cancelled=lambda: None,
                 startup_session=measurement_browser_session):
        self.component, self.runtime, self.request = component, runtime, request
        self.execution = execution or FrozenExecutionRequest.from_record(request)
        self.root, self.execution_dir = root, execution_dir
        self.check_cancelled, self.startup_session = check_cancelled, startup_session
        self.recovery_request = self.execution.recovery
        self.startup_pending = False
        self._started = False
        self._preparation = ExitStack()
        self._outcome = None
        self._replay = False

    def _check_recovery_binding(self, effective):
        original = self.execution.recovery
        if effective is not None and (not isinstance(effective, dict) or original is None
            or any(effective.get(key) != original.get(key) for key in
                   ("run_id", "request_id", "scope_digest", "manifest_digest", "schema_version"))):
            raise ValueError("browser preparation recovery identity changed")

    @contextmanager
    def prepare(self):
        if self.execution.mode == "finalize":
            yield self
            return
        endpoint = self.runtime.get("browser", {}).get("cdp_url", "http://127.0.0.1:9222")
        with shared_browser_lifecycle(environment={"CDP_URL": endpoint}, blocking=False) as lease:
            with self._preparation:
                self.check_cancelled()
                if self.execution.mode in {"run", "refetch"}:
                    self.startup_pending = True
                    saved = read_record(self.execution_dir / "browser-preparation.json", optional=True)
                    if saved is not None:
                        if (saved.get("schema_version") != "visibility-browser-preparation-v1"
                            or saved.get("execution_id") != self.execution.execution_id
                            or saved.get("request_sha256") != self.execution.request_sha256
                            or saved.get("original_recovery_request") != self.execution.recovery):
                            raise ValueError("browser preparation execution binding changed")
                        self.recovery_request = saved.get("effective_recovery_request")
                        self._outcome = saved["outcome"]
                        self._check_recovery_binding(self.recovery_request)
                        if self._outcome.get("recovery_request", self.execution.recovery) != self.recovery_request:
                            raise ValueError("browser preparation recovery receipt changed")
                        # A journal changed by admission must reach the engine's
                        # duplicate/stale-request decision without another restart.
                        self._replay = self.execution.mode == "run" or self.recovery_request != (
                            self.component.plan_refetch(self.execution.run_id,
                                request_id=self.execution.recovery["request_id"]))
                    if not self._replay:
                        self._preparation.enter_context(lease.exclusive(blocking=False))
                        outcome = self._preparation.enter_context(self.component.browser_restart(
                            request_id=self.execution.execution_id, recovery_request=self.recovery_request))
                        effective = outcome.get("recovery_request", self.recovery_request)
                        self._check_recovery_binding(effective)
                        if saved is not None and effective != self.recovery_request:
                            raise ValueError("browser preparation recovery revision changed")
                        self.recovery_request, self._outcome = effective, outcome
                        if saved is None:
                            publish_json(self.execution_dir / "browser-preparation.json", {
                                "schema_version": "visibility-browser-preparation-v1",
                                "execution_id": self.execution.execution_id,
                                "request_sha256": self.execution.request_sha256,
                                "original_recovery_request": self.execution.recovery,
                                "effective_recovery_request": effective, "outcome": outcome,
                            })
                    self.startup_pending = False
                yield self

    @contextmanager
    def start(self):
        """Start once after admission; later recovery rounds reuse Chrome."""
        if self._replay:
            raise ValueError("browser preparation replay is not a new execution")
        self.check_cancelled()
        if self._started:
            yield
            return
        self.startup_pending = True
        if self._outcome is not None:
            publish_json(self.execution_dir / "browser-restart.json", self._outcome)
        with self.startup_session(self.runtime, self.request, root=self.root,
                prepare_restart=nullcontext, check_cancelled=self.check_cancelled):
            # The startup context has finished its nested exclusive section.
            # Release maintenance and downgrade before engine tab reservations.
            self._preparation.close()
            self.startup_pending = False
            self._started = True
            yield
