"""Review publishable files without printing any matched credential content."""
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
IGNORED = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", "build", "dist"}
PRIVATE_ROOTS = {"data", "logs", "tmp", "output", "audit-results"}
SECRET_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{32,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
)


def candidates():
    if (ROOT / ".git").is_dir():
        result = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
        if result:
            return [ROOT / name.decode() for name in result.split(b"\0") if name]
    return [path for path in ROOT.rglob("*") if path.is_file()
            and not IGNORED.intersection(path.relative_to(ROOT).parts)
            and path.relative_to(ROOT).parts[0] not in PRIVATE_ROOTS
            and not (path.parent == ROOT / "config" and path.suffix == ".json"
                     and not path.name.endswith(".example.json"))]


def main():
    failures = []
    files = candidates()
    for path in files:
        relative = path.relative_to(ROOT)
        if path.is_symlink() or IGNORED.intersection(relative.parts):
            failures.append((str(relative), "unexpected symlink or generated file"))
            continue
        if (relative.parts[0] in PRIVATE_ROOTS or path.name.startswith(".env")
            or path.suffix in {".key", ".pem", ".whl", ".pyc"}
            or (path.parent == ROOT / "config" and path.suffix == ".json"
                and not path.name.endswith(".example.json"))):
            failures.append((str(relative), "private or generated material"))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            failures.append((str(relative), "unexpected binary file"))
            continue
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            failures.append((str(relative), "possible credential material"))
        if re.search(r"/(?:Users|home)/[^\s/]+/", text):
            failures.append((str(relative), "machine-specific absolute path"))
    if failures:
        for name, reason in failures:
            print(f"{name}: {reason}")
        raise SystemExit(1)
    print(f"Checked {len(files)} publishable text files: no private runtime files, machine paths or credential markers.")


if __name__ == "__main__":
    main()
