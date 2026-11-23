"""slice-agent entry point. Task 1 provides only the `flatten` subcommand."""

from __future__ import annotations

import sys

from slice_agent.config import load_config
from slice_agent.profiles import flatten_all


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["flatten"]:
        print("usage: slice-agent flatten", file=sys.stderr)
        return 2
    cfg = load_config()
    for path in flatten_all(cfg):
        print(f"wrote {path}")
    print("assertions OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
