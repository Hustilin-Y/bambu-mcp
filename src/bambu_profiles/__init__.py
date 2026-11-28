"""Printer model profiles: everything model-specific, one TOML file per model.

``load_profile(name)`` reads ``<name>.toml`` from this package. The service and the
slice-agent pick the same name (``printer`` in their config, default ``p2s``).
A profile lists the fields it has not verified on a real printer in ``unverified``.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from functools import cache
from importlib.resources import files
from typing import Any, Literal

CameraKind = Literal["rtsps_322", "jpeg_6000"]
StorageKind = Literal["usb", "sd"]
CAMERAS: tuple[str, ...] = ("rtsps_322", "jpeg_6000")
STORAGES: tuple[str, ...] = ("usb", "sd")
PRINT_FLAG_KEYS = frozenset(
    {
        "timelapse",
        "bed_type",
        "bed_leveling",
        "bed_levelling",
        "auto_bed_leveling",
        "flow_cali",
        "vibration_cali",
        "layer_inspect",
    }
)


def _names() -> tuple[str, ...]:
    return tuple(
        sorted(p.name.removesuffix(".toml") for p in files(__name__).iterdir() if p.name.endswith(".toml"))
    )


PROFILE_NAMES: tuple[str, ...] = _names()


@dataclass(frozen=True)
class PrinterProfile:
    name: str
    model_name: str
    bed_mm: tuple[int, int, int]
    hms_prefix: str
    camera: CameraKind
    storage: StorageKind
    ams_units: int
    ams_slots: int
    external_spool: bool
    machine_presets: dict[str, str]
    process_sets: dict[str, str]
    filaments: tuple[str, ...]
    plate_types: tuple[str, ...]
    layer_heights: dict[str, tuple[float, ...]]
    nozzle_temp_range: tuple[int, int]
    power_w: dict[str, float]
    heatup_kwh: float
    print_flags: dict[str, Any]
    idle_states: frozenset[str]
    stoppable_states: frozenset[str]
    unverified: frozenset[str]

    @property
    def has_ams(self) -> bool:
        return self.ams_units > 0

    @property
    def max_slot_number(self) -> int:
        """The highest AMS slot number (1-based); 0 without an AMS."""
        return self.ams_units * self.ams_slots

    @property
    def bed_area(self) -> str:
        return f"{self.bed_mm[0]}x{self.bed_mm[1]}"

    @property
    def bed_height(self) -> str:
        return str(self.bed_mm[2])

    @property
    def hms_file(self) -> str | None:
        """The HMS table name scripts/extract_hms.py writes, None when the prefix is unknown."""
        return f"hms_en_{self.hms_prefix}.json" if self.hms_prefix else None


_FIELDS = tuple(f.name for f in fields(PrinterProfile))


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _need(cond: bool, key: str, what: str) -> None:
    if not cond:
        raise ValueError(f"profile key {key!r} must be {what}")


def _str_list(raw: dict, key: str) -> tuple[str, ...]:
    v = raw[key]
    _need(isinstance(v, list) and all(isinstance(x, str) and x for x in v), key, "a list of strings")
    return tuple(v)


def _str_map(raw: dict, key: str) -> dict[str, str]:
    v = raw[key]
    _need(
        isinstance(v, dict) and bool(v) and all(isinstance(x, str) and x for x in v.values()),
        key,
        "a non-empty table of strings",
    )
    return dict(v)


def _parse(text: str, name: str) -> PrinterProfile:
    """A profile from its TOML text. ``ValueError`` on any missing, unknown or ill-typed key."""
    raw = tomllib.loads(text)
    expected = set(_FIELDS) - {"name"}
    unknown = set(raw) - expected
    if unknown:
        raise ValueError(f"profile {name}: unknown keys {', '.join(sorted(unknown))}")
    missing = expected - set(raw)
    if missing:
        raise ValueError(f"profile {name}: missing keys {', '.join(sorted(missing))}")

    _need(isinstance(raw["model_name"], str) and bool(raw["model_name"]), "model_name", "a string")
    bed = raw["bed_mm"]
    _need(isinstance(bed, list) and len(bed) == 3 and all(_is_int(x) and x > 0 for x in bed), "bed_mm", "3 positive integers")
    _need(isinstance(raw["hms_prefix"], str), "hms_prefix", "a string")
    _need(raw["camera"] in CAMERAS, "camera", f"one of {', '.join(CAMERAS)}")
    _need(raw["storage"] in STORAGES, "storage", f"one of {', '.join(STORAGES)}")
    for key in ("ams_units", "ams_slots"):
        _need(_is_int(raw[key]) and 0 <= raw[key] <= 16, key, "an integer 0..16")
    _need((raw["ams_units"] == 0) == (raw["ams_slots"] == 0), "ams_slots", "0 exactly when ams_units is 0")
    _need(isinstance(raw["external_spool"], bool), "external_spool", "a boolean")
    temps = raw["nozzle_temp_range"]
    _need(
        isinstance(temps, list) and len(temps) == 2 and all(_is_int(t) for t in temps) and temps[0] < temps[1],
        "nozzle_temp_range",
        "[low, high] integers",
    )
    _need(_is_num(raw["heatup_kwh"]) and raw["heatup_kwh"] >= 0, "heatup_kwh", "a non-negative number")

    layers = raw["layer_heights"]
    _need(
        isinstance(layers, dict)
        and bool(layers)
        and all(isinstance(v, list) and v and all(_is_num(x) and x > 0 for x in v) for v in layers.values()),
        "layer_heights",
        "a table of nozzle -> list of heights",
    )
    power = raw["power_w"]
    _need(isinstance(power, dict) and all(_is_num(v) and v > 0 for v in power.values()), "power_w", "a table of watts")
    _need("PLA" in power, "power_w", "a table with a PLA entry (the fallback for other materials)")
    flags = raw["print_flags"]
    _need(isinstance(flags, dict) and set(flags) == PRINT_FLAG_KEYS, "print_flags", f"exactly {', '.join(sorted(PRINT_FLAG_KEYS))}")
    unverified = _str_list(raw, "unverified")
    bad = set(unverified) - set(_FIELDS)
    _need(not bad, "unverified", f"field names, not {', '.join(sorted(bad))}")

    return PrinterProfile(
        name=name,
        model_name=raw["model_name"],
        bed_mm=(bed[0], bed[1], bed[2]),
        hms_prefix=raw["hms_prefix"],
        camera=raw["camera"],
        storage=raw["storage"],
        ams_units=raw["ams_units"],
        ams_slots=raw["ams_slots"],
        external_spool=raw["external_spool"],
        machine_presets=_str_map(raw, "machine_presets"),
        process_sets=_str_map(raw, "process_sets"),
        filaments=_str_list(raw, "filaments"),
        plate_types=_str_list(raw, "plate_types"),
        layer_heights={k: tuple(float(x) for x in v) for k, v in layers.items()},
        nozzle_temp_range=(temps[0], temps[1]),
        power_w={k: float(v) for k, v in power.items()},
        heatup_kwh=float(raw["heatup_kwh"]),
        print_flags=dict(flags),
        idle_states=frozenset(_str_list(raw, "idle_states")),
        stoppable_states=frozenset(_str_list(raw, "stoppable_states")),
        unverified=frozenset(unverified),
    )


@cache
def load_profile(name: str = "p2s") -> PrinterProfile:
    """The named profile. ``ValueError`` naming the known profiles for an unknown name."""
    if name not in PROFILE_NAMES:
        raise ValueError(f"unknown printer {name!r}, known: {', '.join(PROFILE_NAMES)}")
    return _parse(files(__name__).joinpath(f"{name}.toml").read_text(encoding="utf-8"), name)
