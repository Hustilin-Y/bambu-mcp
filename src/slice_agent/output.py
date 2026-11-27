"""Read and check a sliced .gcode.3mf (Metadata/slice_info.config, project_settings.config).

Feature volumes: on request, Metadata/plate_<n>.gcode is scanned for the slicer's role markers
(`;TYPE:<role>`, or Bambu Studio's `; FEATURE: <role>`) and the extrusion of each move, which gives the
extruded volume per role in mm³. The scan is bounded by MAX_GCODE_BYTES. A file over the cap, a missing
or unreadable member or no usable filament_diameter gives no features (None), never an error: they are
a metric for comparing variants, not a check on the file.
"""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

EXPECTED_MODEL = "Bambu Lab P2S"
EXPECTED_BED = (256, 256)
MAX_XML_BYTES = 1024 * 1024
MAX_MEMBER_BYTES = 50 * 1024 * 1024
MAX_PNG_BYTES = 10 * 1024 * 1024
SLICE_INFO = "Metadata/slice_info.config"
VERSION_KEY = "X-BBL-Client-Version"
PROJECT_SETTINGS = "Metadata/project_settings.config"
MAX_GCODE_BYTES = MAX_MEMBER_BYTES  # inflated bytes scanned per plate; over it the features are unknown
MAX_GCODE_LINE = 4096  # a longer line is skipped whole

# The keys of `features`, in mm³. Wall, surface and infill roles map one to one, the support roles to
# "support", everything else (bridges, gap fill, brim, prime tower, start gcode) to "other".
FEATURES = (
    "outer_wall",
    "inner_wall",
    "top_surface",
    "bottom_surface",
    "sparse_infill",
    "internal_solid_infill",
    "support",
    "other",
)
_ROLES = {
    b"outer wall": "outer_wall",
    b"overhang wall": "outer_wall",  # a perimeter over air: wall material all the same
    b"inner wall": "inner_wall",
    b"top surface": "top_surface",
    b"bottom surface": "bottom_surface",
    b"sparse infill": "sparse_infill",
    b"internal solid infill": "internal_solid_infill",
    b"support": "support",
    b"support interface": "support",
    b"support transition": "support",
}
_ROLE_RE = re.compile(rb"\s*(?:TYPE|FEATURE)\s*:\s*(.*?)\s*$")  # the comment after ";"
_MOVES = (b"G0", b"G1", b"G2", b"G3")


class OutputError(Exception):
    """The sliced file is missing, malformed or not for this printer."""


@dataclass(frozen=True)
class PlateResult:
    index: int
    minutes: float
    grams_by_filament: dict[str, float]
    png: bytes | None = None
    filament_types: tuple[str, ...] = ()  # the material of each filament on the plate, e.g. "PETG"
    features: dict[str, float] | None = None  # extruded mm³ per FEATURES key, None when not known
    layer_height: float | None = None  # mm, from project_settings


@dataclass(frozen=True)
class SliceResult:
    plates: list[PlateResult]
    printer_model: str
    nozzle: str
    bed_xy: tuple[int, int]
    gcode_missing: tuple[int, ...] = field(default_factory=tuple)
    slicer_version: str = ""


def _read(z: zipfile.ZipFile, name: str, limit: int = MAX_MEMBER_BYTES) -> bytes | None:
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > limit:
        raise OutputError(f"{name} too large ({info.file_size} bytes)")
    with z.open(info) as f:
        data = f.read(limit + 1)  # do not trust the declared size alone
    if len(data) > limit:
        raise OutputError(f"{name} too large")
    return data


def _bed(area: object) -> tuple[int, int]:
    try:
        if not isinstance(area, list) or not area:
            raise ValueError("not a list")
        pts = [tuple(float(c) for c in str(p).split("x")) for p in area]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        if not all(math.isfinite(v) for v in xs + ys):
            raise ValueError("not finite")
        return round(max(xs) - min(xs)), round(max(ys) - min(ys))
    except (TypeError, ValueError, IndexError, OverflowError):
        raise OutputError("project_settings.config: bad printable_area") from None


def _grams(value: object) -> float:
    grams = float(value)  # type: ignore[arg-type]
    if not math.isfinite(grams) or grams < 0:
        raise ValueError("bad used_g")
    return grams


def _version(root: ET.Element) -> str:
    for item in root.iter("header_item"):
        if item.get("key") == VERSION_KEY:
            return str(item.get("value", ""))[:64]
    return ""


def _plates(root: ET.Element) -> list[tuple[int, float, dict[str, float], tuple[str, ...]]]:
    out = []
    for el in root.iter("plate"):
        meta = {m.get("key"): m.get("value") for m in el.findall("metadata")}
        try:
            idx = int(meta["index"])
            seconds = float(meta["prediction"])
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError("bad prediction")
            filaments = el.findall("filament")
            grams = {f.get("id", str(i + 1)): _grams(f.get("used_g", 0)) for i, f in enumerate(filaments)}
            types = tuple(str(f.get("type", "")).strip()[:32] for f in filaments)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise OutputError(f"{SLICE_INFO}: plate without index or prediction, or bad used_g") from None
        out.append((idx, seconds / 60, grams, types))
    if not out:
        raise OutputError(f"{SLICE_INFO}: no plates")
    return out


def _positive(value: object, upper: float) -> float | None:
    """A finite number in (0, upper] from a setting (a string, a number or a one-item list), else None."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and 0 < number <= upper else None


def _number(text: bytes) -> float | None:
    try:
        number = float(text)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _extruded_lengths(f) -> dict[str, float] | None:
    """Filament length (mm) per role. Only positive extrusion on moves in X or Y counts: a retract,
    an unretract and the retracting wipe do not. M82/M83 switch absolute/relative E, G92 sets E."""
    lengths = dict.fromkeys(FEATURES, 0.0)
    role, relative, last_e, total, skipping = "other", False, 0.0, 0, False
    while True:
        line = f.readline(MAX_GCODE_LINE + 1)
        if not line:
            return lengths
        total += len(line)
        if total > MAX_GCODE_BYTES:
            return None  # a partial count would mislead the comparison
        complete = line.endswith(b"\n")
        if skipping or (len(line) > MAX_GCODE_LINE and not complete):
            skipping = not complete  # drop an over-long line through its end
            continue
        code, _, comment = line.partition(b";")
        words = code.split()
        if not words:
            match = _ROLE_RE.match(comment)
            if match:
                role = _ROLES.get(match.group(1).lower(), "other")
            continue
        cmd = words[0].upper()
        if cmd == b"M82":
            relative = False
        elif cmd == b"M83":
            relative = True
        elif cmd == b"G92":
            values = [_number(w[1:]) for w in words[1:] if w[:1] in (b"E", b"e")]
            if len(words) == 1:
                last_e = 0.0
            elif values and values[-1] is not None:
                last_e = values[-1]
        elif cmd in _MOVES:
            e, moves = None, False
            for word in words[1:]:
                axis = word[:1].upper()
                if axis == b"E":
                    e = _number(word[1:])
                elif axis in (b"X", b"Y"):
                    moves = True
            if e is None:
                continue
            delta = e if relative else e - last_e
            if not relative:
                last_e = e
            if moves and delta > 0:
                lengths[role] += delta


def _feature_volumes(z: zipfile.ZipFile, plate: int, diameter: float | None) -> dict[str, float] | None:
    if diameter is None:
        return None
    try:
        info = z.getinfo(f"Metadata/plate_{plate}.gcode")
    except KeyError:
        return None
    if info.file_size > MAX_GCODE_BYTES:
        return None
    try:
        with z.open(info) as f:
            lengths = _extruded_lengths(f)
    except (zipfile.BadZipFile, OSError, EOFError, RuntimeError, zlib.error):
        return None
    if lengths is None:
        return None
    area = math.pi * (diameter / 2) ** 2
    return {k: v * area for k, v in lengths.items()}


def read_output(path: Path, plate: int, feature_plates: Iterable[int] = ()) -> SliceResult:
    """Parse the sliced file. `plate` is the plate the caller will print (its gcode must exist).

    `feature_plates`: the plates whose gcode is scanned for feature volumes (none by default)."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise OutputError(f"cannot open sliced file: {e}") from None
    with z:
        info = _read(z, SLICE_INFO, MAX_XML_BYTES)
        if info is None:
            raise OutputError(f"{SLICE_INFO} missing")
        settings_raw = _read(z, PROJECT_SETTINGS, MAX_XML_BYTES)
        if settings_raw is None:
            raise OutputError(f"{PROJECT_SETTINGS} missing")
        try:
            settings = json.loads(settings_raw)
        except (ValueError, RecursionError):
            raise OutputError(f"{PROJECT_SETTINGS}: not valid JSON") from None
        if not isinstance(settings, dict):
            raise OutputError(f"{PROJECT_SETTINGS}: not a JSON object")
        try:
            root = ET.fromstring(info)
        except ET.ParseError as e:
            raise OutputError(f"{SLICE_INFO}: not valid XML ({e})") from None
        parsed = _plates(root)
        names = set(z.namelist())
        wanted = {i for i, *_ in parsed} | {plate}
        missing = tuple(sorted(i for i in wanted if f"Metadata/plate_{i}.gcode" not in names))
        diameter = _positive(settings.get("filament_diameter"), 5.0)
        layer_height = _positive(settings.get("layer_height"), 2.0)
        scan = set(feature_plates)
        plates = [
            PlateResult(
                i,
                mins,
                g,
                _read(z, f"Metadata/plate_{i}.png"),
                types,
                features=_feature_volumes(z, i, diameter) if i in scan else None,
                layer_height=layer_height,
            )
            for i, mins, g, types in parsed
        ]
    nozzles = settings.get("nozzle_diameter")
    if not isinstance(nozzles, list) or not nozzles:
        raise OutputError(f"{PROJECT_SETTINGS}: nozzle_diameter is not a list")
    return SliceResult(
        plates=plates,
        printer_model=str(settings.get("printer_model", "")),
        nozzle=str(nozzles[0]),
        bed_xy=_bed(settings.get("printable_area")),
        gcode_missing=missing,
        slicer_version=_version(root),
    )


def read_plate_png(path: Path, plate: int) -> bytes | None:
    """The plate preview PNG inside a sliced file, or None if the file has none for that plate."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise OutputError(f"cannot open sliced file: {e}") from None
    with z:
        try:
            return _read(z, f"Metadata/plate_{int(plate)}.png", MAX_PNG_BYTES)
        except (zipfile.BadZipFile, OSError, RuntimeError, EOFError) as e:
            raise OutputError(f"cannot read plate {plate} preview: {type(e).__name__}") from None


def check_output(result: SliceResult, nozzle: str) -> None:
    """Raise OutputError unless the file is a P2S, 256 x 256 bed, matching nozzle, with gcode."""
    if result.gcode_missing:
        names = ", ".join(f"Metadata/plate_{i}.gcode" for i in result.gcode_missing)
        raise OutputError(f"missing {names}")
    if result.printer_model != EXPECTED_MODEL:
        raise OutputError(f"printer_model {result.printer_model!r}, expected {EXPECTED_MODEL!r}")
    if result.bed_xy != EXPECTED_BED:
        raise OutputError(f"bed {result.bed_xy[0]}x{result.bed_xy[1]}, expected 256x256")
    if result.nozzle != nozzle:
        raise OutputError(f"nozzle {result.nozzle!r}, expected {nozzle!r}")
