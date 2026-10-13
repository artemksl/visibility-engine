"""Start the same localhost panel from the checkout's virtual environment."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def main(argv=None):
    root = Path(__file__).resolve().parent
    args = list(sys.argv[1:] if argv is None else argv)
    python = root / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.is_file():
        raise SystemExit("Run setup.cmd first." if sys.platform == "win32" else "Run ./setup.sh first.")
    if os.name == "posix":
        import resource
        _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(8192, hard) if hard >= 0 else 8192, hard))
        except (OSError, ValueError):
            pass  # Match the existing best-effort ulimit, never require it.
    os.environ["VISIBILITY_ENGINE_ROOT"] = str(root)
    os.execv(str(python), [str(python), "-m", "visibility_panel.app", *args])


if __name__ == "__main__":
    main()
