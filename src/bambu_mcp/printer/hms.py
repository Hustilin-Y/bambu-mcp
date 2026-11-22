"""Decode HMS codes from printer reports into readable text."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from bambu_mcp.printer.state import Hms

Severity = Literal["fatal", "serious", "common", "info", "unknown"]

_SEVERITY: dict[int, Severity] = {1: "fatal", 2: "serious", 3: "common", 4: "info"}


def hms_code(h: Hms) -> str:
    """Format as AAAA_AAAA_CCCC_CCCC (attr high/low, code high/low), upper-case hex."""
    return f"{h.attr >> 16 & 0xFFFF:04X}_{h.attr & 0xFFFF:04X}_{h.code >> 16 & 0xFFFF:04X}_{h.code & 0xFFFF:04X}"


def severity(h: Hms) -> Severity:
    return _SEVERITY.get(h.code >> 16, "unknown")


@dataclass(frozen=True)
class HmsTexts:
    """Code to English text tables produced by scripts/extract_hms.py."""

    hms: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None) -> HmsTexts:
        if path is None or not path.is_file():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(hms=dict(data.get("hms", {})), errors=dict(data.get("errors", {})))

    def text(self, code: str) -> str | None:
        return self.hms.get(code) or None

    def error_text(self, code: int) -> str | None:
        """Text for a print_error value, keyed by its 8-digit upper-case hex form."""
        return self.errors.get(f"{code:08X}") or None


def describe(h: Hms, texts: HmsTexts) -> str:
    code = hms_code(h)
    return f"HMS_{code} ({severity(h)}): {texts.text(code) or 'no description'}"
