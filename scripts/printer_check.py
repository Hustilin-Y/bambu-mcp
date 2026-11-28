"""Read-only printer check: `uv run python scripts/printer_check.py --help`.

The same as the `bambu-printer-check` command; see bambu_mcp/printer_check.py.
"""

import sys

from bambu_mcp.printer_check import main

if __name__ == "__main__":
    sys.exit(main())
