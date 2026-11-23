"""File-name-safe names shared by the slice flow and print matching."""

from __future__ import annotations

import re

MAX_SLUG = 40
FALLBACK = "job"
_OTHER = re.compile(r"[^a-z0-9]+")
# Names Windows reserves for devices: the slice-agent refuses them as a part name.
_RESERVED = re.compile(r"con|prn|aux|nul|com[1-9]|lpt[1-9]")


def slug(name: str) -> str:
    """``name`` lowercased to ``[a-z0-9-]``: other runs become one ``-``, trimmed, at most 40.

    Never empty (``job``) and never a reserved Windows device name (``-job`` is appended).
    The slice-agent builds ``<slug>-<job id>-p1`` / ``-kit`` from it.
    """
    out = _OTHER.sub("-", name.lower()).strip("-")[:MAX_SLUG].strip("-")
    if not out:
        return FALLBACK
    if _RESERVED.fullmatch(out):
        return f"{out}-{FALLBACK}"
    return out
