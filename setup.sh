#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${VISIBILITY_PYTHON:-python3}" "$ROOT/setup.py" "$@"
