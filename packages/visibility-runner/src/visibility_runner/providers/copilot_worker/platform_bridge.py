"""Native Windows publication/termination for the owned Node worker.

The Python owner freezes this script and interpreter paths in its child-only
environment. It is an OS boundary, with no provider decisions or credentials.
"""
from __future__ import annotations

from pathlib import Path
import sys

from platform_runtime.processes import terminate_process_tree
from platform_runtime.storage import publish_complete


def main(arguments: list[str]) -> None:
    if len(arguments) == 3 and arguments[0] == "publish":
        temporary, target = map(Path, arguments[1:])
        if temporary.resolve().parent != target.resolve().parent:
            raise ValueError("publication must stay on the same volume and directory")
        publish_complete(temporary, target, durable=True)
    elif len(arguments) == 2 and arguments[0] == "terminate":
        pid = int(arguments[1])
        if pid <= 0:
            raise ValueError("invalid owned worker pid")
        terminate_process_tree(pid, force=True)
    else:
        raise ValueError("unsupported native operation")


if __name__ == "__main__":
    main(sys.argv[1:])
