"""The router module runs inside multiclaude, as multiclaude.addons.bambu. Here it is the
top-level package ``bambu``: its own imports are relative, and multiclaude is faked per test."""

import sys
from pathlib import Path

ROUTER = Path(__file__).resolve().parents[2] / "multiclaude" / "router"
if str(ROUTER) not in sys.path:
    sys.path.insert(0, str(ROUTER))
