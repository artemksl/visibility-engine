"""Disk oracle that does not follow symlinks or mistake atime for a write."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import stat

def tree_snapshot(root):
    root = Path(root)
    result = {}
    def visit(path):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        relative = str(path.relative_to(root))
        mode = metadata.st_mode
        if stat.S_ISLNK(mode):
            result[relative] = ("symlink", os.readlink(path), metadata.st_mtime_ns)
        elif stat.S_ISREG(mode):
            result[relative] = ("file", hashlib.sha256(path.read_bytes()).hexdigest(),
                                metadata.st_size, metadata.st_mtime_ns)
        elif stat.S_ISDIR(mode):
            result[relative] = ("directory", metadata.st_mtime_ns)
            for child in sorted(path.iterdir()):
                visit(child)
        else:
            result[relative] = ("special", stat.S_IFMT(mode), metadata.st_mtime_ns)
    visit(root)
    return result
