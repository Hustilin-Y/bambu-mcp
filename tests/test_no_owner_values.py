"""The public tree carries example values only: no owner addresses, user names or paths.

docs/ (dated specs and reports) and spike/ (phase 0 throwaways) are history and stay as
written. LICENSE names the copyright holder, and repo links name the GitHub account.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = re.compile(r"192\.168\.|shiba-srv|\bShiba\b|Hustilin", re.IGNORECASE)
SKIP_PREFIXES = ("docs/", "spike/", ".superpowers/")
SKIP_FILES = {"LICENSE", "tests/test_no_owner_values.py"}


def owner_value_hits(root: Path = ROOT, extra: tuple[str, ...] = ()) -> list[str]:
    """``path:line: text`` for every tracked (or ``extra``) file line with an owner value."""
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    hits = []
    for name in sorted({*listed, *extra}):
        if name.startswith(SKIP_PREFIXES) or name in SKIP_FILES:
            continue
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if PATTERNS.search(line) and "github.com/" not in line:
                hits.append(f"{name}:{n}: {line.strip()[:120]}")
    return hits


def test_tree_has_no_owner_values():
    assert owner_value_hits() == []


def test_default_listen_is_loopback():
    from bambu_mcp.config import Settings

    assert Settings().listen == [("127.0.0.1", 8765)]
