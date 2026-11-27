"""Reader for CAD Workshop print manifests (``out/<name>.print.json``, schema 1).

Strict on the fields the service needs, tolerant of extra fields.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

SCHEMA = 1
INFILL_PATTERNS = (
    "grid", "gyroid", "honeycomb", "cubic", "line",
    "rectilinear", "zig-zag", "adaptivecubic", "lightning",
)
CHECK_BOOLS = ("failed", "overhang_warn", "base_warn", "supports_suggested")
CHECK_INTS = ("islands", "bodies_above_bed")
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_PLATES = 50
MAX_OBJECTS = 200
MAX_NUMBER = 1e9
NAME_RE = re.compile(r"[A-Za-z0-9._ -]{1,100}")


class ManifestError(ValueError):
    """The manifest is unusable. The message is safe to show to the owner."""


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: object) -> bool:
    if not isinstance(v, int | float) or isinstance(v, bool):
        return False
    try:
        return math.isfinite(v) and abs(v) <= MAX_NUMBER
    except OverflowError:
        return False


HINT_VALIDATORS = {
    "walls": (lambda v: _is_int(v) and 1 <= v <= 10, "an integer 1..10"),
    "infill_pct": (lambda v: _is_int(v) and 0 <= v <= 100, "an integer 0..100"),
    "infill_pattern": (lambda v: v in INFILL_PATTERNS, "one of " + ", ".join(INFILL_PATTERNS)),
    "supports": (lambda v: v in ("none", "auto", "tree", "normal"), "one of none, auto, tree, normal"),
    "brim": (lambda v: v in ("none", "auto", "outer"), "one of none, auto, outer"),
    "material": (lambda v: isinstance(v, str) and bool(v.strip()), "a non-empty string"),
    "material_reason": (lambda v: isinstance(v, str) and len(v) <= 200, "a string of at most 200 characters"),
}


@dataclass(frozen=True)
class PlateInfo:
    index: int
    objects: tuple[dict, ...]
    checks: dict
    hints: dict
    estimate_g: float
    hint_conflicts: tuple[str, ...] = ()  # hint keys the kit plate's parts disagree on


@dataclass(frozen=True)
class Manifest:
    schema: int
    kind: Literal["part", "kit"]
    name: str
    file: str
    file_sha256: str
    print_ready: bool
    material: str
    nozzle_mm: float
    plates: tuple[PlateInfo, ...]
    checks: dict
    hints: dict
    estimate_g: float
    source: dict = field(default_factory=dict)


def _need(data: Mapping, key: str, where: str = "manifest") -> Any:
    if key not in data or data[key] is None:
        raise ManifestError(f"{where}: missing required field '{key}'")
    return data[key]


def _obj(value: object, what: str) -> Mapping:
    if not isinstance(value, dict):
        raise ManifestError(f"{what} must be an object")
    return value


def _hints(value: object, where: str) -> dict:
    hints = _obj(value, f"{where} hints")
    for key, v in hints.items():
        if key not in HINT_VALIDATORS:
            raise ManifestError(
                f"{where}: unknown hint '{key}' (allowed: {', '.join(HINT_VALIDATORS)})"
            )
        ok, allowed = HINT_VALIDATORS[key]
        if not ok(v):
            raise ManifestError(f"{where}: hint '{key}' must be {allowed}, got {v!r}")
    return dict(hints)


def _checks(value: object, where: str) -> dict:
    checks = _obj(value, f"{where} checks")
    for key in CHECK_BOOLS:
        if key in checks and not isinstance(checks[key], bool):
            raise ManifestError(f"{where}: checks.{key} must be true or false")
    for key in CHECK_INTS:
        if key in checks and (not _is_int(checks[key]) or checks[key] < 0):
            raise ManifestError(f"{where}: checks.{key} must be a non-negative integer")
    return dict(checks)


def _hint_conflicts(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(k, str) and k in HINT_VALIDATORS for k in value):
        raise ManifestError(
            f"{where}: hint_conflicts must be a list of hint names ({', '.join(HINT_VALIDATORS)})"
        )
    return tuple(dict.fromkeys(value))


def _grams(value: object, where: str) -> float:
    if not _is_num(value) or value < 0:  # type: ignore[operator]
        raise ManifestError(f"{where}: estimate grams must be a non-negative number")
    return float(value)  # type: ignore[arg-type]


def _estimate(data: Mapping, where: str) -> float | None:
    est = data.get("estimate")
    if isinstance(est, dict) and est.get("grams") is not None:
        return _grams(est["grams"], where)
    if data.get("estimate_g") is not None:
        return _grams(data["estimate_g"], where)
    return None


def _plate(
    raw: object, pos: int, top_checks: dict, top_hints: dict, top_grams: float | None, single: bool
) -> PlateInfo:
    where = f"plates[{pos}]"
    data = _obj(raw, where)
    index = _need(data, "index", where)
    if not _is_int(index) or index < 1:
        raise ManifestError(f"{where}: index must be a positive integer")
    objects = _need(data, "objects", where)
    if not isinstance(objects, list) or not objects or not all(isinstance(o, dict) for o in objects):
        raise ManifestError(f"{where}: objects must be a non-empty list of objects")
    if len(objects) > MAX_OBJECTS:
        raise ManifestError(f"{where}: too many objects (max {MAX_OBJECTS})")
    checks = _checks(data["checks"], where) if "checks" in data else top_checks
    hints = _hints(data["hints"], where) if "hints" in data else top_hints
    grams = _estimate(data, where)
    if grams is None:
        if not single or top_grams is None:
            raise ManifestError(f"{where}: missing estimate grams")
        grams = top_grams
    conflicts = _hint_conflicts(data["hint_conflicts"], where) if "hint_conflicts" in data else ()
    return PlateInfo(index, tuple(dict(o) for o in objects), checks, hints, grams, conflicts)


def _merge_checks(plates: list) -> dict:
    merged: dict = {k: False for k in CHECK_BOOLS} | {k: 0 for k in CHECK_INTS}
    for p in plates:
        c = p.get("checks") if isinstance(p, dict) else None
        if not isinstance(c, dict):
            continue
        for k in CHECK_BOOLS:
            merged[k] = merged[k] or c.get(k) is True
        for k in CHECK_INTS:
            merged[k] += c[k] if _is_int(c.get(k)) else 0
    return merged


def parse_manifest(data: object) -> Manifest:
    root = _obj(data, "manifest")
    schema = _need(root, "schema")
    if isinstance(schema, bool) or schema != SCHEMA:
        raise ManifestError(f"unsupported manifest schema {schema!r}, this service reads schema {SCHEMA}")
    kind = _need(root, "kind")
    if kind not in ("part", "kit"):
        raise ManifestError(f"manifest kind must be 'part' or 'kit', got {kind!r}")
    name = _need(root, "name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name.startswith(".") or not name.strip():
        raise ManifestError("manifest name must be 1..100 characters of A-Z a-z 0-9 . _ - space, not starting with a dot")
    file = _need(root, "file")
    if not isinstance(file, str) or not NAME_RE.fullmatch(file) or file.startswith("."):
        raise ManifestError("manifest file must be a bare file name (1..100 characters of A-Z a-z 0-9 . _ - space)")
    sha = _need(root, "file_sha256")
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha.lower()):
        raise ManifestError("manifest file_sha256 must be 64 hex characters")
    if _need(root, "print_ready") is not True:
        raise ManifestError(
            "manifest is not print_ready: rebuild the part with --print-ready (or make a kit) "
            "so the checked orientation is the printed one"
        )
    profile = _obj(_need(root, "profile"), "manifest profile")
    material = _need(profile, "material", "profile")
    if not isinstance(material, str) or not material.strip():
        raise ManifestError("profile.material must be a non-empty string")
    nozzle = _need(profile, "nozzle_mm", "profile")
    if not _is_num(nozzle) or nozzle <= 0:
        raise ManifestError("profile.nozzle_mm must be a positive number")

    raw_plates = _need(root, "plates")
    if not isinstance(raw_plates, list) or not raw_plates:
        raise ManifestError("manifest plates must be a non-empty list")
    if len(raw_plates) > MAX_PLATES:
        raise ManifestError(f"manifest has too many plates (max {MAX_PLATES})")
    if kind == "part" and len(raw_plates) != 1:
        raise ManifestError("a part manifest must have exactly one plate")

    if "checks" in root:
        checks = _checks(root["checks"], "manifest")
    elif kind == "part":
        raise ManifestError("manifest: missing required field 'checks'")
    else:
        checks = _merge_checks(raw_plates)
    hints = _hints(root.get("hints", {}), "manifest")
    grams = _estimate(root, "manifest")
    if grams is None and kind == "part":
        raise ManifestError("manifest: missing required field 'estimate'")
    plates = [_plate(p, i, checks, hints, grams, kind == "part") for i, p in enumerate(raw_plates)]
    if grams is None:
        grams = round(sum(p.estimate_g for p in plates), 3)
    indexes = [p.index for p in plates]
    if len(set(indexes)) != len(indexes):
        raise ManifestError("manifest plates have duplicate indexes")
    source = root.get("source", {})
    if not isinstance(source, dict):
        raise ManifestError("manifest source must be an object")

    return Manifest(
        schema=SCHEMA, kind=kind, name=name, file=file, file_sha256=sha.lower(),
        print_ready=True, material=material, nozzle_mm=float(nozzle),
        plates=tuple(plates), checks=checks, hints=hints, estimate_g=grams, source=dict(source),
    )


def load_manifest(path: Path) -> Manifest:
    p = Path(path)
    try:
        with p.open("rb") as fh:
            raw = fh.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ManifestError(f"manifest {p.name} is too large (max {MAX_MANIFEST_BYTES} bytes)")
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"cannot read manifest {p.name}: {type(exc).__name__}") from None
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise ManifestError(f"manifest {p.name} is not valid JSON") from None
    return parse_manifest(data)


def verify_file(manifest: Manifest, file_bytes: bytes) -> None:
    if hashlib.sha256(file_bytes).hexdigest() != manifest.file_sha256:
        raise ManifestError(
            f"{manifest.file} does not match the manifest sha256 (the file changed after the build): "
            "rebuild with --print-ready and upload again"
        )
