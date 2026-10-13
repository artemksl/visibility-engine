"""Check local installation only; never open Chrome or send provider requests."""
from importlib import import_module, metadata
from inspect import signature
from pathlib import Path
import os


def main():
    root = Path(__file__).resolve().parents[1]
    os.environ["VISIBILITY_ENGINE_ROOT"] = str(root)
    expected = {"visibility-runner": "0.2.80", "workspace-automation-core": "0.1.3", "workspace-platform-runtime": "0.1.0"}
    for distribution, version in expected.items():
        actual = metadata.version(distribution)
        if actual != version:
            raise SystemExit(f"{distribution}: expected {version}, installed {actual}")
        print(f"{distribution} {actual}")
    from visibility_runner import Runner
    from automation_core.browser.async_actions import AsyncActionExecutor
    from automation_core.contracts import SensitiveValue
    import_module("visibility_panel.app")
    from visibility_panel.paths import repo_root
    from visibility_panel.web.jobs import REPO_ROOT
    assert repo_root() == root and REPO_ROOT == root, "Panel must use this checkout's configuration and worker"
    for name in ("file_locks", "storage", "security", "processes", "paths", "credentials", "native_errors"):
        import_module("platform_runtime." + name)
    assert "replace_checked_sends" in signature(Runner.refetch).parameters
    assert AsyncActionExecutor and SensitiveValue
    for relative in ("scripts/run_visibility_measurement.py", "config/launch_logged_in.sh", "config/launch_logged_in.py", "config/profile_anchor.html"):
        assert (root / relative).is_file(), f"Required checkout file missing: {relative}"
    print("Panel imports and guarded recovery API available. No browser actions performed.")
    for name in ("profiles.json", "settings.json"):
        if not (root / "config" / name).is_file():
            print(f"Before live measurements, create config/{name}; see INSTALL.md.")


if __name__ == "__main__":
    main()

