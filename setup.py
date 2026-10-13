"""Install the checkout on Windows or macOS; this is not a setuptools file."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent

def venv_python(root=ROOT, *, platform=None):
    platform = platform or sys.platform
    return root / ".venv" / ("Scripts/python.exe" if platform == "win32" else "bin/python")


def installation_arguments(root, *, tests=False, desktop=False):
    platform = str(root / "packages/platform-runtime") + ("[desktop]" if desktop else "")
    packages = [platform, str(root / "packages/automation-core"),
        str(root / "packages/visibility-runner"), str(root / "panel") + ("[test]" if tests else "")]
    if desktop:
        packages.append(str(root / "packages/axbridge"))
    return packages


def install_sdk(root):
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        raise SystemExit("Install Node.js 22.12 or later and npm before using --github-copilot.")
    subprocess.run([node, "-e", 'const [major,minor]=process.versions.node.split(".").map(Number);'
        'if(major<22||(major===22&&minor<12))throw Error("Node.js 22.12 or later is required")'], check=True)
    command = [npm]
    if sys.platform == "win32":
        # Execute npm's JavaScript with node, avoiding shell parsing of paths
        # containing spaces/metacharacters and Windows' .cmd dispatch rules.
        npm_cli = Path(npm).parent / "node_modules/npm/bin/npm-cli.js"
        if not npm_cli.is_file():
            raise SystemExit("Cannot locate npm-cli.js beside npm; use a standard Node.js Windows installation.")
        command = [node, str(npm_cli)]
    # Normal package installs run the worker from site-packages. Install beside
    # that exact worker, rather than a source copy the runtime will never use.
    installed = subprocess.run([str(venv_python(root)), "-c",
        "from pathlib import Path; from visibility_runner.providers import github_copilot; "
        "print(Path(github_copilot.__file__).parent / 'copilot_worker')"],
        check=True, capture_output=True, text=True)
    worker = Path(installed.stdout.strip())
    if not worker.is_dir() or not (worker / "package-lock.json").is_file():
        raise SystemExit("Installed Copilot worker or its pinned dependency lockfile is missing.")
    subprocess.run([*command, "ci", "--ignore-scripts", "--omit=dev", "--no-audit", "--no-fund", "--prefix",
        str(worker)], check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-tests", action="store_true")
    parser.add_argument("--desktop-verification", action="store_true")
    parser.add_argument("--github-copilot", action="store_true")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 12):
        parser.error("Python 3.12 or later is required.")
    if args.desktop_verification and sys.platform not in {"darwin", "win32"}:
        parser.error("Native desktop verification requires Windows or macOS.")
    python = venv_python()
    if not python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")], check=True)
    subprocess.run([str(python), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"], check=True)
    subprocess.run([str(python), "-m", "pip", "install", *installation_arguments(ROOT,
        tests=args.with_tests, desktop=args.desktop_verification)], check=True)
    subprocess.run([str(python), "-m", "pip", "check"], check=True)
    if args.github_copilot:
        install_sdk(ROOT)
    subprocess.run([str(python), str(ROOT / "tools/check_install.py")], check=True)
    launcher = "run.cmd" if sys.platform == "win32" else "./run.sh"
    print(f"Installed. Run {launcher}, then open http://127.0.0.1:8766/visibility-testing")


if __name__ == "__main__":
    main()
