"""Immutable printer state built from Bambu MQTT report messages."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

KNOWN_STATES = frozenset(
    {"FAILED", "FINISH", "IDLE", "INIT", "OFFLINE", "PAUSE", "PREPARE", "RUNNING", "SLICING"}
)


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
            remain = _int(raw.get("remain"))
            trays.append(
                Tray(
                    ams=ams_id,
                    slot=slot,
                    tray_type=raw.get("tray_type") or None,
                    color_rgba=raw.get("tray_color") or None,
                    remain_pct=remain if remain is not None and remain >= 0 else None,
                )
            )
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


def apply(state: PrinterState, report: dict) -> PrinterState:
    """Merge a report into the state. Fields absent from the report are kept."""
    if not isinstance(report, dict):
        return state
    p = report.get("print")
    if not isinstance(p, dict) or p.get("command") == "gcode_line":
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
    if "print_error" in p:
        changes["print_error"] = _int(p["print_error"]) or 0
    hms = _parse_hms(p.get("hms"))
    if hms is not None:
        changes["hms"] = hms
    nozzle = _parse_nozzle(p)
    if nozzle is not None:
        changes["nozzle_diameter"] = nozzle
    trays = _parse_trays(p.get("ams"))
    if trays is not None:
        changes["trays"] = trays
    return replace(state, **changes) if changes else state
