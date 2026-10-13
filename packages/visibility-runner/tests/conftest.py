"""Test imports and options for isolated browser fixtures."""
from __future__ import annotations
from pathlib import Path
import sys
import pytest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

def pytest_addoption(parser):
    group = parser.getgroup("isolated browser matrix")
    group.addoption("--browser-driver", choices=("playwright", "patchright"), default="playwright",
                    help="Driver for synthetic browser fixtures; one driver per test process")
    group.addoption("--browser-executable", help="Exact provisioned Chromium executable; disables system fallback")
    group.addoption("--expected-browser-version", help="Required exact browser.version for the experiment")
    group.addoption("--browser-driver-version", help="Required exact installed driver version for the experiment")
    group.addoption("--browser-headed", action="store_true", default=False,
                    help="Show the isolated fixture browser; no persistent user profile")


def pytest_configure(config):
    executable = config.getoption("--browser-executable")
    expected = config.getoption("--expected-browser-version")
    driver_version = config.getoption("--browser-driver-version")
    driver = config.getoption("--browser-driver")
    if driver == "patchright" or executable or expected or driver_version:
        if not executable or not expected or not driver_version:
            raise pytest.UsageError("the browser matrix requires --browser-driver-version, --browser-executable and --expected-browser-version")
