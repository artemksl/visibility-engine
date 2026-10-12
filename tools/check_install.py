"""Check local installation only; never open Chrome or send provider requests."""
from importlib import import_module, metadata
from inspect import signature
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    expected = {"visibility-runner": "0.2.55", "workspace-automation-core": "0.1.2"}
    for distribution, version in expected.items():
        actual = metadata.version(distribution)
        if actual != version:
            raise SystemExit(f"{distribution}: expected {version}, installed {actual}")
        print(f"{distribution} {actual}")
    from visibility_runner import Runner
    from automation_core.browser.async_actions import AsyncActionExecutor
    from automation_core.contracts import SensitiveValue
    import_module("visibility_panel.app")
    assert "replace_checked_sends" in signature(Runner.refetch).parameters
    assert AsyncActionExecutor and SensitiveValue
    for relative in ("scripts/run_visibility_measurement.py", "config/launch_logged_in.sh", "config/profile_anchor.html"):
        assert (root / relative).is_file(), f"Required checkout file missing: {relative}"
    print("Panel imports and guarded recovery API available. No browser actions performed.")
    for name in ("profiles.json", "settings.json"):
        if not (root / "config" / name).is_file():
            print(f"Before live measurements, create config/{name}; see INSTALL.md.")


if __name__ == "__main__":
    main()

