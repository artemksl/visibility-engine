#!/usr/bin/env python3
"""Run isolated engine verification tiers; no provider traffic or saved profiles."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from support.offline_guard import install_guard

def prohibit_live_effects():
    install_guard("fast")

EXPRESSIONS = {
    "fast": "not (local_browser or native_process or native_storage or package or model_extended or performance)",
    "native": "(native_process or native_storage) and not local_browser",
    "browser": "local_browser",
    "package": "package",
    "extended": "model_extended or performance",
}

COMPOSITE_TIERS = {
    "integration": ("native", "browser"),
    "release": ("fast", "native", "browser", "package"),
    "all": tuple(EXPRESSIONS),
}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, default=ROOT)
    parser.add_argument("--tier", choices=[*EXPRESSIONS, *COMPOSITE_TIERS], default="fast")
    parser.add_argument("--portable-identity", action="store_true",
                        help="Explicit synthetic identity for portable fixtures; never the native gate")
    parser.add_argument("--browser-driver", choices=("playwright", "patchright"), default="playwright")
    parser.add_argument("--browser-executable", type=Path,
                        help="Exact executable for isolated browser fixtures, with no system fallback")
    parser.add_argument("--expected-browser-version", help="Exact browser version required by the matrix")
    parser.add_argument("--browser-driver-version", help="Exact installed driver version required by the matrix")
    parser.add_argument("--browser-headed", action="store_true", help="Run isolated fixture browser headed")
    args, additional = parser.parse_known_args()
    browser_options = ["--browser-driver", args.browser_driver]
    if args.browser_driver == "patchright" or args.browser_executable or args.expected_browser_version or args.browser_driver_version:
        if not args.browser_executable or not args.expected_browser_version or not args.browser_driver_version:
            parser.error("the browser matrix requires --browser-driver-version, --browser-executable and --expected-browser-version")
        browser_options.extend(["--browser-executable", str(args.browser_executable),
                                "--expected-browser-version", args.expected_browser_version])
    if args.browser_headed:
        browser_options.append("--browser-headed")
    if args.browser_driver_version:
        browser_options.extend(["--browser-driver-version", args.browser_driver_version])
    engine = args.engine.resolve(strict=True)
    if not (engine / "src" / "visibility_runner").is_dir(): parser.error("engine source directory missing")
    if args.portable_identity and (args.tier == "native" or args.tier in COMPOSITE_TIERS):
        parser.error("native tier requires real process identity")
    if args.tier in COMPOSITE_TIERS:
        for tier in COMPOSITE_TIERS[args.tier]:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--engine", str(engine),
                                     "--tier", tier, *browser_options, *additional], check=False)
            if result.returncode: return result.returncode
        return 0
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.path.insert(0, str(engine / "src"))
    sys.path.insert(0, str(engine / "tests"))
    os.environ["PYTHONPATH"] = os.pathsep.join((str(engine / "src"), str(engine / "tests")))
    install_guard(args.tier)
    import pytest
    from visibility_runner.execution import run_ownership
    boot_unavailable = not run_ownership._boot_identity()
    if boot_unavailable and not args.portable_identity:
        parser.error("OS boot identity unavailable; use native permissions or explicitly --portable-identity for fast fixtures")
    class OfflineObservations:
        @pytest.fixture(autouse=True)
        def synthetic_identity(self, monkeypatch):
            if boot_unavailable and args.portable_identity:
                monkeypatch.setattr(run_ownership, "_boot_identity", lambda: "synthetic-test-boot")
    selected_paths = any(not value.startswith("-") and ("::" in value or value.endswith(".py") or value == "tests") for value in additional)
    targets = [] if selected_paths else [str(engine / "tests")]
    return pytest.main([*targets, "-q", "-p", "no:cacheprovider", "--tb=short", "--durations=10",
                        "-m", EXPRESSIONS[args.tier], *browser_options, *additional], plugins=[OfflineObservations()])

if __name__ == "__main__":
    raise SystemExit(main())
