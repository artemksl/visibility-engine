#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${VISIBILITY_PYTHON:-python3}"
WITH_TESTS=no
WITH_DESKTOP=no
WITH_SDK=no
for option in "$@"; do
  case "$option" in
    --with-tests) WITH_TESTS=yes ;;
    --desktop-verification) WITH_DESKTOP=yes ;;
    --github-copilot) WITH_SDK=yes ;;
    *) echo "Usage: ./setup.sh [--with-tests] [--desktop-verification] [--github-copilot]" >&2; exit 2 ;;
  esac
done

"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12 or later is required"; assert sys.platform != "win32", "Windows is not supported by the current filesystem locking code"'
if [ "$WITH_DESKTOP" = yes ] && [ "$(uname -s)" != Darwin ]; then
  echo "Desktop verification requires macOS. Omit --desktop-verification here." >&2
  exit 2
fi
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  "$PYTHON" -m venv "$ROOT/.venv"
fi
VENV_PY="$ROOT/.venv/bin/python"
"$VENV_PY" -m pip install --upgrade pip setuptools wheel
install_args=(-e "$ROOT/packages/automation-core" -e "$ROOT/packages/visibility-runner")
if [ "$WITH_TESTS" = yes ]; then
  install_args+=(-e "$ROOT/panel[test]")
else
  install_args+=(-e "$ROOT/panel")
fi
if [ "$WITH_DESKTOP" = yes ]; then
  install_args+=(-e "$ROOT/packages/axbridge")
fi
"$VENV_PY" -m pip install "${install_args[@]}"
"$VENV_PY" -m pip check
if [ "$WITH_SDK" = yes ]; then
  command -v node >/dev/null && command -v npm >/dev/null || {
    echo "Install Node.js 22.12 or later and npm before using --github-copilot." >&2; exit 2;
  }
  node -e 'const [major,minor]=process.versions.node.split(".").map(Number); if(major<22 || (major===22 && minor<12)) throw Error("Node.js 22.12 or later is required")'
  npm ci --ignore-scripts --omit=dev --no-audit --no-fund --prefix "$ROOT/packages/visibility-runner/src/visibility_runner/providers/copilot_worker"
fi
"$VENV_PY" "$ROOT/tools/check_install.py"
echo "Installed. Run ./run.sh, then open http://127.0.0.1:8766/visibility-testing"

