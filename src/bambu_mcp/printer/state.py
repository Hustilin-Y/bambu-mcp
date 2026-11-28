"""Immutable printer state built from Bambu MQTT report messages."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

KNOWN_STATES = frozenset(
    {"FAILED", "FINISH", "IDLE", "INIT", "OFFLINE", "PAUSE", "PREPARE", "RUNNING", "SLICING"}
)
# The order state names are listed in to people: the start states first, as before profiles.
STATE_ORDER = ("IDLE", "FINISH", "FAILED", "INIT", "OFFLINE", "PAUSE", "PREPARE", "RUNNING", "SLICING")


def ordered_states(states) -> list[str]:
    """``states`` in ``STATE_ORDER``, unknown names last and sorted."""
    return sorted(states, key=lambda s: (STATE_ORDER.index(s) if s in STATE_ORDER else len(STATE_ORDER), s))


_PLATE_KEYS = ("plate_idx", "plate_id", "gcode_file")
_IDENTITY_KEYS = ("subtask_name", "task_id", "gcode_start_time")
# The external spool holder (no AMS, or next to one): ams_id 255 and tray_id 254, as
# ha-bambulab and Bambu Studio name it. Older firmware reports it as ``vt_tray`` (one
# object), newer (P2S 01.03) as ``vir_slot`` (a list, one entry per extruder).
EXTERNAL_AMS = 255
EXTERNAL_TRAY = 254
_PLATE_GCODE = re.compile(r"plate_([1-9][0-9]{0,3})\.gcode", re.ASCII)


@dataclass(frozen=True)
class Tray:
    ams: int
    slot: int
    tray_type: str | None
    color_rgba: str | None
    remain_pct: int | None


@dataclass(frozen=True)
class Hms:
    attr: int
    code: int


@dataclass(frozen=True)
class PrinterState:
    gcode_state: str
    percent: int | None
    remaining_min: int | None
    layer: int | None
    total_layers: int | None
    subtask_name: str | None
    task_id: str | None
    gcode_start_time: str | None
    print_error: int
    hms: tuple[Hms, ...]
    nozzle_diameter: float | None
    trays: tuple[Tray, ...]
    raw_unknown_state: str | None
    plate: int | None = None  # plate of the print's file: plate_idx, plate_id or gcode_file
    sdcard: bool | None = None  # USB storage present; None until a report says, or not a boolean

    @classmethod
    def empty(cls) -> PrinterState:
        return cls("UNKNOWN", None, None, None, None, None, None, None, 0, (), None, (), None)


def identity(state: PrinterState) -> tuple[str | None, str | None, str | None]:
    return (state.subtask_name, state.task_id, state.gcode_start_time)


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


def _tray(ams: int, slot: int, raw: dict) -> Tray:
    remain = _int(raw.get("remain"))
    return Tray(
        ams=ams,
        slot=slot,
        tray_type=raw.get("tray_type") or None,
        color_rgba=raw.get("tray_color") or None,
        remain_pct=remain if remain is not None and remain >= 0 else None,
    )


def _parse_external(p: dict) -> Tray | None:
    """The external spool from ``vt_tray`` or a one-entry ``vir_slot``, None when neither is usable."""
    raw = p.get("vt_tray")
    if raw is None:
        slots = p.get("vir_slot")
        raw = slots[0] if isinstance(slots, list) and len(slots) == 1 else None
    if not isinstance(raw, dict):
        return None
    tray_type = raw.get("tray_type")
    if tray_type is not None and not isinstance(tray_type, str):
        return None
    color = raw.get("tray_color")
    if color is not None and not isinstance(color, str):
        return None
    return _tray(EXTERNAL_AMS, EXTERNAL_TRAY, raw)


def _parse_trays(ams_block: Any) -> tuple[Tray, ...] | None:
    units = ams_block.get("ams") if isinstance(ams_block, dict) else None
    if not isinstance(units, list):
        return None
    trays: list[Tray] = []
    for unit in units:
        if not isinstance(unit, dict):
            continue
        ams_id = _int(unit.get("id"))
        for raw in unit.get("tray") or []:
            slot = _int(raw.get("id")) if isinstance(raw, dict) else None
            if ams_id is None or slot is None:
                continue
            trays.append(_tray(ams_id, slot, raw))
    return tuple(trays)


def _parse_hms(raw: Any) -> tuple[Hms, ...] | None:
    if not isinstance(raw, list):
        return None
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        attr, code = _int(item.get("attr")), _int(item.get("code"))
        if attr is not None and code is not None:
            out.append(Hms(attr, code))
    return tuple(out)


def _parse_nozzle(p: dict) -> float | None:
    device = p.get("device")
    nozzle = device.get("nozzle") if isinstance(device, dict) else None
    info = nozzle.get("info") if isinstance(nozzle, dict) else None
    if isinstance(info, list):
        for entry in info:
            if isinstance(entry, dict) and _int(entry.get("id")) == 0:
                diameter = _float(entry.get("diameter"))
                if diameter is not None:
                    return diameter
    return _float(p.get("nozzle_diameter"))


def _parse_plate(p: dict) -> int | None:
    """``plate_idx``, else ``plate_id``, else the number in ``gcode_file`` (``plate_2.gcode``)."""
    for key in ("plate_idx", "plate_id"):
        plate = _int(p.get(key))
        if plate is not None and plate >= 1:
            return plate
    gcode = p.get("gcode_file")
    match = _PLATE_GCODE.search(gcode) if isinstance(gcode, str) else None
    return int(match.group(1)) if match else None


def apply(state: PrinterState, report: dict) -> PrinterState:
    """Merge a report into the state. Fields absent from the report are kept."""
    if not isinstance(report, dict):
        return state
    p = report.get("print")
    # Only status pushes describe the printer. A command's echo (project_file, gcode_line, ...)
    # repeats the request: project_file names the new job while the printer still reports the
    # last one's FINISH, which would hide the start (events._deferred_start).
    if not isinstance(p, dict) or p.get("command", "push_status") != "push_status":
        return state

    changes: dict[str, Any] = {}
    if "gcode_state" in p:
        raw = _str(p["gcode_state"])
        upper = (raw or "").upper()
        if upper in KNOWN_STATES:
            changes.update(gcode_state=upper, raw_unknown_state=None)
        else:
            changes.update(gcode_state="UNKNOWN", raw_unknown_state=raw)
    for key, field, conv in (
        ("mc_percent", "percent", _int),
        ("mc_remaining_time", "remaining_min", _int),
        ("layer_num", "layer", _int),
        ("total_layer_num", "total_layers", _int),
        ("subtask_name", "subtask_name", _str),
        ("task_id", "task_id", _str),
        ("gcode_start_time", "gcode_start_time", _str),
    ):
        if key in p:
            changes[field] = conv(p[key])
    if any(key in p for key in _PLATE_KEYS):
        changes["plate"] = _parse_plate(p)
    elif any(key in changes and changes[key] != getattr(state, key) for key in _IDENTITY_KEYS):
        changes["plate"] = None  # another job without its plate: never keep the last one's
    if "sdcard" in p:
        changes["sdcard"] = p["sdcard"] if isinstance(p["sdcard"], bool) else None
    if "print_error" in p:
        changes["print_error"] = _int(p["print_error"]) or 0
    hms = _parse_hms(p.get("hms"))
    if hms is not None:
        changes["hms"] = hms
    nozzle = _parse_nozzle(p)
    if nozzle is not None:
        changes["nozzle_diameter"] = nozzle
    ams_trays = _parse_trays(p.get("ams"))
    external = _parse_external(p)
    if ams_trays is not None or external is not None:
        # Each block replaces only its own trays: a report with one keeps the other's.
        keep_ams = tuple(t for t in state.trays if t.ams != EXTERNAL_AMS)
        keep_ext = tuple(t for t in state.trays if t.ams == EXTERNAL_AMS)
        changes["trays"] = (ams_trays if ams_trays is not None else keep_ams) + (
            (external,) if external is not None else keep_ext
        )
    return replace(state, **changes) if changes else state
