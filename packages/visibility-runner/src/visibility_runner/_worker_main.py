"""Command-line entry point for versioned visibility-runner worker requests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from .planning.hashing import json_value
from .worker_request import (
    WORKER_REQUEST_SCHEMA_VERSION,
    execute_worker_request,
    load_worker_request,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m visibility_runner._worker_main",
        description="Execute one explicitly serialized visibility-runner request.",
    )
    parser.add_argument(
        "--request",
        required=True,
        type=Path,
        help=(
            "Path to a "
            f"{WORKER_REQUEST_SCHEMA_VERSION} JSON file."
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        request = load_worker_request(arguments.request)
        result = execute_worker_request(request)
    except Exception as exc:
        json.dump(
            {
                "ok": False,
                "error_type": type(exc).__name__,
                "message": str(exc),
            },
            sys.stderr,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        sys.stderr.write("\n")
        return 1
    json.dump(
        {"ok": True, "result": json_value(result)},
        sys.stdout,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a module process
    raise SystemExit(main())
