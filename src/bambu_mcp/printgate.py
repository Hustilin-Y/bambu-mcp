"""The print gate: what a start confirmation is bound to, and its ids."""

from __future__ import annotations

import hashlib
import json
import secrets

from bambu_mcp.store.models import CONFIRMATION_TTL_S

__all__ = ["CONFIRMATION_TTL_S", "gate_hash", "new_confirmation_id"]


def new_confirmation_id() -> str:
    """A random 128-bit id, as 32 hex characters."""
    return secrets.token_hex(16)


def gate_hash(
    file_sha256: str,
    plate: int,
    ams: int,
    slot: int,
    serial: str,
    nozzle: str,
    plate_type: str,
) -> str:
    """sha256 hex of a canonical JSON of everything a start confirmation is bound to.

    The press recomputes it from the current state: any difference means the owner
    confirmed something else, and the start is refused.
    """
    canonical = json.dumps(
        {
            "ams": ams,
            "file_sha256": file_sha256,
            "nozzle": nozzle,
            "plate": plate,
            "plate_type": plate_type,
            "serial": serial,
            "slot": slot,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
