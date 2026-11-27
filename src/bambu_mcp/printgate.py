"""The print gate: what a start confirmation is bound to, its ids, and the pre-flight checks."""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from bambu_mcp.manifest import ManifestError, parse_manifest
from bambu_mcp.messages import _HINT_WORDS, md_literal
from bambu_mcp.printer.hms import hms_code, severity
from bambu_mcp.printer.state import PrinterState
from bambu_mcp.proposal import SlotChoice, _color_match, job_material, slot_number
from bambu_mcp.store.models import CONFIRMATION_TTL_S, Job, Spool
from slice_agent.output import OutputError, read_output

__all__ = [
    "CONFIRMATION_TTL_S",
    "SPOOL_MARGIN",
    "ControlRefused",
    "Preflight",
    "PrintRefused",
    "PrintFileError",
    "PrintFileInfo",
    "gate_hash",
    "new_confirmation_id",
    "preflight",
    "print_file_info",
]

SPOOL_MARGIN = 1.15  # the spool must hold the plate's grams plus 15 %
START_STATES = frozenset({"IDLE", "FINISH", "FAILED"})
BLOCKING_HMS = {"fatal": "критическая", "serious": "серьёзная"}
NOZZLE_TOLERANCE_MM = 0.001
_MD5_RE = re.compile(r"[0-9A-Fa-f]{32}")
_MD5_MAX_BYTES = 256


class PrintRefused(Exception):
    """A print was not prepared. ``lines``: Russian, Markdown-escaped, one per reason.

    ``str()`` is for Claude: the English ``detail`` when there is one, else the lines.
    """

    def __init__(self, lines: list[str], detail: str | None = None) -> None:
        self.lines = list(lines)
        super().__init__(detail or "print refused: " + " / ".join(self.lines))


class ControlRefused(Exception):
    """``pause_print`` or ``request_stop`` sent nothing. The message is English, for Claude."""


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


# -- the print file ------------------------------------------------------------------


class PrintFileError(Exception):
    """The sliced file cannot be printed as is. English, no path: safe to show to Claude."""


@dataclass(frozen=True)
class PrintFileInfo:
    """One plate of a sliced file: what ``project_file`` sends and pre-flight checks."""

    md5: str  # Metadata/plate_<n>.gcode.md5, as Bambu Studio wrote it
    grams: float  # all filaments of the plate
    minutes: float
    filament_types: tuple[str, ...]  # distinct materials of the plate, in file order
    filament_count: int = 1  # filament ids the plate uses: project_file maps exactly one slot


def _plate_md5(path: Path, plate: int) -> str:
    name = f"Metadata/plate_{plate}.gcode.md5"
    try:
        with zipfile.ZipFile(path) as z:
            try:
                info = z.getinfo(name)
            except KeyError:
                raise PrintFileError(f"{name} missing") from None
            with z.open(info) as f:
                raw = f.read(_MD5_MAX_BYTES + 1)
    except (zipfile.BadZipFile, OSError, RuntimeError, EOFError) as e:
        raise PrintFileError(f"cannot read {name}: {type(e).__name__}") from None
    text = raw[:_MD5_MAX_BYTES].decode("ascii", errors="replace").strip()
    if len(raw) > _MD5_MAX_BYTES or not _MD5_RE.fullmatch(text):
        raise PrintFileError(f"{name} is not an md5")
    return text


def print_file_info(path: Path, plate: int) -> PrintFileInfo:
    """Read plate ``plate`` of the sliced file at ``path``. ``PrintFileError`` when unusable."""
    try:
        result = read_output(path, plate)
    except OutputError as e:
        # "cannot open" carries the OS error, and with it the path: never shown.
        detail = "cannot open it" if str(e).startswith("cannot open") else str(e)
        raise PrintFileError(f"sliced file: {detail}") from None
    found = next((p for p in result.plates if p.index == plate), None)
    if found is None:
        raise PrintFileError(f"sliced file has no plate {plate}")
    if plate in result.gcode_missing:
        raise PrintFileError(f"sliced file has no Metadata/plate_{plate}.gcode")
    types = tuple(dict.fromkeys(t for t in found.filament_types if t))
    return PrintFileInfo(
        md5=_plate_md5(path, plate),
        grams=sum(found.grams_by_filament.values()),
        minutes=found.minutes,
        filament_types=types,
        filament_count=len(found.grams_by_filament),
    )


# -- pre-flight ----------------------------------------------------------------------


@dataclass(frozen=True)
class Preflight:
    """Russian lines, Markdown-escaped, one per problem, in the Global Constraints' order.

    Any ``blocking`` line refuses the start. ``warnings`` are shown with the confirmation.
    """

    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _norm(text: str | None) -> str:
    return (text or "").strip().casefold()


def _result(job: Job) -> dict:
    try:
        data = json.loads(job.result_json or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _nozzle_line(state: PrinterState, nozzle: str) -> str | None:
    if state.nozzle_diameter is None:
        return "Нет данных о сопле принтера, подожди отчёт принтера"
    try:
        wanted = float(nozzle)
    except ValueError:
        wanted = math.nan
    if not abs(state.nozzle_diameter - wanted) <= NOZZLE_TOLERANCE_MM:  # NaN never matches
        return (
            f"Сопло на принтере {state.nozzle_diameter:g} мм, "
            f"а задание нарезано под {md_literal(nozzle)} мм"
        )
    return None


def _slot_lines(
    state: PrinterState, fresh: bool, slot_choice: SlotChoice, spool: Spool | None, info: PrintFileInfo
) -> list[str]:
    """Slot empty, unmapped, or holding another material than the sliced file's."""
    if slot_choice.ams is None or slot_choice.slot is None:
        return ["Слот AMS не выбран, выбери слот и нарежь заново"]
    number = slot_number(slot_choice.ams, slot_choice.slot)
    tray = next((t for t in state.trays if (t.ams, t.slot) == (slot_choice.ams, slot_choice.slot)), None)
    loaded = tray is not None and isinstance(tray.tray_type, str) and tray.tray_type.strip()
    if fresh and not loaded:
        return [f"Слот {number} пустой, заправь его"]
    if spool is None:
        return [f"К слоту {number} не привязана катушка, привяжи её"]
    if not spool.active:
        return [f"Катушка {spool.id} в слоте {number} списана, привяжи к слоту другую"]
    if info.filament_count > 1:
        n = info.filament_count
        return [f"Пластина нарезана под несколько филаментов ({n}), а печать идёт из одного слота"]
    if len(info.filament_types) != 1:
        listed = ", ".join(md_literal(t) for t in info.filament_types) or "не указан"
        return [f"Материал пластины в файле: {listed}, а печать идёт из одного слота"]
    if fresh and tray is not None and _norm(tray.tray_type) != _norm(info.filament_types[0]):
        return [
            f"В слоте {number} {md_literal(str(tray.tray_type))}, "
            f"а файл нарезан под {md_literal(info.filament_types[0])}"
        ]
    return []


def _file_line(expected_sha256: str | None, file_sha256: str | None) -> str | None:
    if not expected_sha256:
        return "Задание нарезано до проверки файлов, нарежь заново"
    if not file_sha256:
        return "Нарезанного файла нет, нарежь заново"
    if file_sha256 != expected_sha256:
        return "Нарезанный файл изменился после нарезки, нарежь заново"
    return None


def _warnings(
    state: PrinterState, fresh: bool, job: Job, plate: int, slot_choice: SlotChoice, spool: Spool | None
) -> list[str]:
    out = []
    tray = None
    if fresh and slot_choice.ams is not None:
        tray = next((t for t in state.trays if (t.ams, t.slot) == (slot_choice.ams, slot_choice.slot)), None)
    sliced_slot = _result(job).get("slot")
    color = sliced_slot.get("color") if isinstance(sliced_slot, dict) else None  # the colour at slicing
    if tray is not None and isinstance(color, str) and color and not _color_match(tray, spool, color):
        number = slot_number(tray.ams, tray.slot)
        out.append(f"Цвет в слоте {number} не тот, что при нарезке ({md_literal(color)})")
    try:
        manifest = parse_manifest(json.loads(job.manifest_json))
    except (ValueError, ManifestError):
        return out
    want, reason = job_material(manifest)
    have = tray.tray_type if tray is not None else None
    if isinstance(have, str) and have.strip() and _norm(have) != _norm(want):
        because = f" ({md_literal(reason)})" if reason else ""
        number = slot_number(tray.ams, tray.slot)
        out.append(f"Задание просит {md_literal(want)}{because}, а в слоте {number} {md_literal(have)}")
    for p in manifest.plates:
        if p.index == plate and p.hint_conflicts:
            words = ", ".join(_HINT_WORDS.get(k, md_literal(k)) for k in p.hint_conflicts)
            out.append(f"Пластина {plate}: у деталей разные подсказки ({words}), взяты общие настройки")
    return out


def preflight(
    state: PrinterState,
    job: Job,
    plate: int,
    slot_choice: SlotChoice,
    spool: Spool | None,
    info: PrintFileInfo,
    expected_sha256: str | None,
    file_sha256: str | None,
    nozzle: str,
    *,
    fresh: bool,
) -> Preflight:
    """The hard stops and warnings of a start, in the Global Constraints' order.

    ``fresh``: the link is CONNECTED and a report arrived since (``Service.state_fresh``).
    Without it, or with an UNKNOWN state, everything read from printer reports is stale:
    one line says so instead of one per stale field. ``expected_sha256`` is the sha256
    recorded at slicing (None for a job sliced before phase 3), ``file_sha256`` the one of
    the fetched file (None when it is missing).
    """
    fresh = fresh and state.gcode_state != "UNKNOWN"
    blocking: list[str] = []
    if not fresh:
        blocking.append("Принтер не на связи или ещё не прислал отчёт после подключения")
    else:
        if state.gcode_state not in START_STATES:
            blocking.append(f"Принтер занят: {md_literal(state.gcode_state)}, нужен IDLE, FINISH или FAILED")
        for h in state.hms:
            level = BLOCKING_HMS.get(severity(h))
            if level is not None:
                code = md_literal(f"HMS_{hms_code(h)}")
                blocking.append(f"Ошибка принтера {code} ({level}), сначала устрани её")
        if state.sdcard is None:
            blocking.append("Нет данных о флешке, подожди отчёт принтера")
        elif not state.sdcard:
            blocking.append("В принтере нет флешки, вставь её")
        nozzle_line = _nozzle_line(state, nozzle)
        if nozzle_line:
            blocking.append(nozzle_line)
    slot_lines = _slot_lines(state, fresh, slot_choice, spool, info)
    blocking += slot_lines
    if spool is not None and slot_choice.ams is not None:
        need = info.grams * SPOOL_MARGIN
        if spool.remaining_g < need:
            blocking.append(
                f"На катушке осталось {math.floor(spool.remaining_g)} г, а нужно {math.ceil(need)} г "
                f"(пластина {plate}: {info.grams:.0f} г и запас 15%)"
            )
    file_line = _file_line(expected_sha256, file_sha256)
    if file_line:
        blocking.append(file_line)
    return Preflight(blocking=blocking, warnings=_warnings(state, fresh, job, plate, slot_choice, spool))
