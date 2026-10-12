#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo "Run ./setup.sh first." >&2
  exit 1
fi
ulimit -n 8192 2>/dev/null || true
exec "$ROOT/.venv/bin/python" -m visibility_panel.app "$@"

