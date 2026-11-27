"""MCP server (Streamable HTTP) for the owner's clients, in the service's event loop.

Every request needs ``Authorization: Bearer <token>`` matching one of the
``CLIENT_TOKEN_<NAME>`` values. The middleware rejects anything else with 401
before the MCP app sees it and records the client name (never the token) for
logs. No tool accepts or returns the access code, serial, host or any token.

``POST /jobs`` (same middleware) takes a CAD Workshop manifest and its 3MF as
multipart parts ``manifest`` and ``file`` and creates a job; see ``bambu_mcp.jobs``.
The job tools propose and slice jobs through ``Service`` (the same code path as the
Telegram buttons) and return file names only, never server paths.

All tools are ``async`` on purpose: the SDK runs sync tools in worker threads,
and the store keeps one SQLite connection per thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hmac
import json
import logging
import math
import re
import socket
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import uvicorn
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from python_multipart.multipart import parse_options_header
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from bambu_mcp import camera as camera_module
from bambu_mcp.config import Secrets, Settings, country_timezone, with_country
from bambu_mcp.cost import tariff_reminder_enabled
from bambu_mcp.evaluation import job_goal, parse_goal
from bambu_mcp.jobs import (
    MAX_FILE_BYTES,
    EvaluationFailed,
    JobBusy,
    JobError,
    JobTooLarge,
    JobUpload,
    SliceStart,
    create_job_from_bytes,
    create_job_from_path,
    incoming_dir,
)
from bambu_mcp.journal import FIT_VALUES, rebook_grams
from bambu_mcp.messages import render_variants
from bambu_mcp.manifest import INFILL_PATTERNS, MAX_MANIFEST_BYTES, ManifestError, parse_manifest
from bambu_mcp.printer.hms import hms_code, severity
from bambu_mcp.printer.link import (
    AMS_SLOTS,
    AMS_UNITS,
    NOZZLE_TEMP_RANGE,
    TRAY_INFO_IDX_RE,
    TRAY_TYPES,
    LinkStatus,
    PrinterNotConnected,
)
from bambu_mcp.printer.state import PrinterState
from bambu_mcp.printgate import ControlRefused, PrintRefused
from bambu_mcp.proposal import MAX_SLOT_NUMBER, NAMED_SETS, slot_number
from bambu_mcp.store.db import Store
from bambu_mcp.store.models import Amortization, Job, Plate, PrintRow, Spool, Tariff
from slice_agent.runner import (
    BRIM_TYPES,
    DENSITY_RANGE,
    IRONING_TYPES,
    LAYER_HEIGHTS,
    OUTER_WALL_SPEED_RANGE,
    OVERRIDE_KEYS,
    PLATE_TYPES,
    SEAM_POSITIONS,
    SHELL_LAYERS_RANGE,
    SUPPORT_TYPES,
    WALL_GENERATORS,
    WALL_LOOPS_RANGE,
    Overrides,
)

log = logging.getLogger(__name__)

MCP_PATH = "/mcp"
JOBS_PATH = "/jobs"
_BODY_SLACK = 64 * 1024  # multipart boundaries and part headers
SERVER_NAME = "bambu-mcp"
GRACEFUL_SHUTDOWN_S = 5
HISTORY_MAX = 200
JOBS_MAX = 50
EVAL_WAIT_S = 90.0  # evaluate_job waits this long for the run, then answers that it goes on
TARIFF_MAX = 1000.0  # per kWh: a sanity cap, a finite huge rate must not overflow a cost
AMORT_RATE_MAX = 1e6  # per hour of printing, the same reason
SET_KEY = "set"  # slice_job settings: the named set to start from
PATH_INTAKE_OFF = "path intake is off: upload with `bambu-mcp upload`"
# gcode_state values (phase 1 names) in which the AMS may be reconfigured.
LOADABLE_STATES = ("IDLE", "FINISH", "FAILED")
_SPOOL_HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}", re.ASCII)  # a spool's color_hex, exactly
LOAD_SPOOL_NOTE = (
    "Sent once, not retried. Check the AMS slot on the printer screen; "
    "ams_status shows the printer's next report."
)

INSTRUCTIONS = (
    "Bambu Lab P2S: status, camera, journal, inventory, print jobs. A print starts or stops only "
    "on the owner's press in Telegram: prepare_print and request_stop send the confirmation."
)
PREPARE_NOTE = (
    "Nothing is printing yet. The owner must press the start button under the confirmation in "
    "Telegram (valid 10 minutes). No tool can start a print."
)
PAUSE_NOTE = (
    "Sent once, not retried. There is no resume tool: the owner resumes on the printer screen "
    "or in Bambu Studio. printer_status shows the printer's next report."
)
STOP_NOTE = (
    "Nothing is stopped yet. The owner must press the stop button under the confirmation in "
    "Telegram (valid 10 minutes)."
)

READ = ToolAnnotations(read_only_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)

Camera = Callable[[], Awaitable[bytes | None]]


class StatusSource(Protocol):
    """What the MCP server needs from ``Service``."""

    @property
    def state(self) -> PrinterState: ...

    @property
    def link_status(self) -> LinkStatus: ...

    @property
    def state_fresh(self) -> bool: ...

    async def propose(self, job_id: int, slot: int | None = None, goal: object = None) -> str: ...

    def start_evaluate(
        self, job_id: int, goal: object = None, explicit: Mapping | None = None
    ) -> asyncio.Task: ...

    async def choose_variant(self, job_id: int, n: int) -> str: ...

    async def reset_settings(self, job_id: int, keys: Sequence[str] | None = None) -> dict: ...

    def start_slice(
        self,
        job_id: int,
        explicit: Mapping | None = None,
        *,
        named_set: str = ...,
        slot: int | None = None,
        goal: object = None,
    ) -> SliceStart: ...

    def set_tray(
        self,
        *,
        ams: int,
        slot: int,
        tray_info_idx: str,
        tray_type: str,
        color_rgba: str,
        nozzle_temp_min: int,
        nozzle_temp_max: int,
    ) -> dict: ...

    def request_full_report(self) -> None: ...

    async def prepare_print(self, job_id: int, plate: int = 1) -> Any: ...

    async def pause_print(self) -> dict[str, str | None]: ...

    async def request_stop(self) -> Any: ...


# -- auth ------------------------------------------------------------------------


class BearerTokenMiddleware:
    """Pure ASGI: only requests carrying a known client token reach the app."""

    def __init__(self, app: ASGIApp, tokens: dict[str, str]) -> None:
        self._app = app
        self._tokens = [(name, value.encode("utf-8")) for name, value in tokens.items() if value]

    def _client(self, scope: Scope) -> str | None:
        header = b""
        for key, value in scope.get("headers", ()):
            if key == b"authorization":
                header = value
                break
        scheme, _, presented = header.partition(b" ")
        if scheme.lower() != b"bearer" or not presented:
            return None
        found = None
        for name, token in self._tokens:  # compare against all, no early exit
            if hmac.compare_digest(presented.strip(), token) and found is None:
                found = name
        return found

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)  # lifespan
            return
        client = self._client(scope)
        peer = scope.get("client") or ("?", 0)
        if client is None:
            log.warning("MCP request from %s rejected: missing or unknown token", peer[0])
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["mcp_client"] = client
        log.debug("MCP %s %s from client %s", scope.get("method", ""), scope.get("path", ""), client)
        await self._app(scope, receive, send)


# -- serialisation ---------------------------------------------------------------


def _money(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _spool(s: Spool) -> dict[str, Any]:
    return {
        "id": s.id,
        "brand": s.brand,
        "material": s.material,
        "color_name": s.color_name,
        "color_hex": s.color_hex,
        "filament_preset": s.filament_preset,
        "price_uah": _money(s.price_uah),
        "net_g": s.net_g,
        "remaining_g": round(s.remaining_g, 1),
        "price_per_kg_uah": _money(s.price_per_kg_uah),
        "active": s.active,
        "tray_info_idx": s.tray_info_idx,
        "nozzle_temp_min": s.nozzle_temp_min,
        "nozzle_temp_max": s.nozzle_temp_max,
        "currency": s.currency,
    }


def _plate(p: Plate) -> dict[str, Any]:
    return {"id": p.id, "name": p.name, "bed_type": p.bed_type, "is_default": p.is_default}


def _tariff(t: Tariff) -> dict[str, Any]:
    return {
        "valid_from": t.valid_from.isoformat(),
        "day_uah": t.day_uah,
        "night_uah": t.night_uah,
        "currency": t.currency,
    }


def _amortization(a: Amortization) -> dict[str, Any]:
    return {
        "valid_from": a.valid_from.isoformat(),
        "printer_price": a.printer_price,
        "life_hours": a.life_hours,
        "upkeep_per_hour": a.upkeep_per_hour,
        "rate_per_hour": _money(a.rate_per_hour),
        "currency": a.currency,
    }


_CURRENCY_RE = re.compile(r"[A-Z]{3}", re.ASCII)
_COUNTRY_RE = re.compile(r"[A-Z]{2}", re.ASCII)
_SYMBOL_MAX = 8


def _finite(*values: float | None) -> bool:
    """Every given number is finite (None is skipped). NaN and infinity are not."""
    return all(v is None or math.isfinite(v) for v in values)


def _print(row: PrintRow, tz: ZoneInfo) -> dict[str, Any]:
    costs = [c for c in (row.cost_filament_uah, row.cost_power_uah, row.cost_amort) if c is not None]
    return {
        "id": row.id,
        "name": row.identity[0],
        "source": row.source,
        "started_at": row.started_at.astimezone(tz).isoformat(),
        "ended_at": row.ended_at.astimezone(tz).isoformat() if row.ended_at else None,
        "outcome": row.outcome,
        "duration_min": None if row.actual_min is None else round(row.actual_min),
        "spool_id": row.spool_id,
        "grams": row.grams,
        "kwh": None if row.kwh is None else round(row.kwh, 3),
        "cost_filament_uah": _money(row.cost_filament_uah),
        "cost_power_uah": _money(row.cost_power_uah),
        "cost_amort": _money(row.cost_amort),
        "cost_total_uah": _money(sum(costs)) if costs else None,
        "currency": row.currency,
        "fit": row.fit,
    }


def _status(state: PrinterState, link: LinkStatus) -> dict[str, Any]:
    return {
        "link": link.value,
        "gcode_state": state.gcode_state,
        "raw_unknown_state": state.raw_unknown_state,
        "job_name": state.subtask_name,
        "percent": state.percent,
        "remaining_min": state.remaining_min,
        "layer": state.layer,
        "total_layers": state.total_layers,
        "print_error": state.print_error,
        "hms": [{"code": hms_code(h), "severity": severity(h)} for h in state.hms],
        "nozzle_diameter": state.nozzle_diameter,
    }


@contextlib.contextmanager
def _tool_errors() -> Iterator[None]:
    """Turn the store's lookup and validation errors into messages for the model."""
    try:
        yield
    except KeyError as exc:
        raise ToolError(str(exc.args[0]) if exc.args else "not found") from exc
    except (ValueError, LookupError) as exc:
        raise ToolError(str(exc)) from exc


# -- jobs ----------------------------------------------------------------------------

def _allowed_values(key: str, nozzle: str) -> str:
    """What ``Overrides.parse`` accepts for ``key`` (one of ``OVERRIDE_KEYS``), for error messages."""
    heights = ", ".join(f"{h:g}" for h in LAYER_HEIGHTS.get(nozzle, ()))
    return {
        "sparse_infill_density": "an integer {}..{} (percent)".format(*DENSITY_RANGE),
        "sparse_infill_pattern": ", ".join(INFILL_PATTERNS),
        "wall_loops": "an integer {}..{}".format(*WALL_LOOPS_RANGE),
        "wall_generator": ", ".join(WALL_GENERATORS),
        "enable_support": "true or false",
        "support_type": ", ".join(SUPPORT_TYPES),
        "brim_type": ", ".join(BRIM_TYPES),
        "curr_bed_type": ", ".join(PLATE_TYPES),
        "layer_height": heights or f"nothing for nozzle {nozzle}",
        "top_shell_layers": "an integer {}..{}".format(*SHELL_LAYERS_RANGE),
        "bottom_shell_layers": "an integer {}..{}".format(*SHELL_LAYERS_RANGE),
        "outer_wall_speed": "a number {}..{} (mm/s)".format(*OUTER_WALL_SPEED_RANGE),
        "seam_position": ", ".join(SEAM_POSITIONS),
        "ironing_type": ", ".join(IRONING_TYPES),
    }[key]


def explicit_settings(
    settings: Mapping[str, Any] | None, nozzle: str
) -> tuple[dict[str, object], str | None]:
    """(explicit overrides, named set) from a ``slice_job`` request (ruling R6).

    A named set (``{"set": "strong"}``) is expanded into explicit values and the other
    keys go on top, so both outrank the part's last settings and its hints. Raises
    ``ToolError`` naming the key and what it accepts.
    """
    raw = dict(settings or {})
    named = raw.pop(SET_KEY, None)
    if named is not None and (not isinstance(named, str) or named not in NAMED_SETS):
        raise ToolError(f"{SET_KEY}: unknown named set {named!r}, allowed: {', '.join(NAMED_SETS)}")
    explicit: dict[str, object] = {**(NAMED_SETS[named] if named else {}), **raw}
    for key, value in explicit.items():
        if key not in OVERRIDE_KEYS:
            raise ToolError(f"{key}: unknown setting, allowed: {', '.join((SET_KEY, *OVERRIDE_KEYS))}")
        try:
            Overrides.parse({key: value}, nozzle)
        except ValueError:
            origin = "" if key in raw else f" (from set {named!r})"
            raise ToolError(
                f"{key}: {value!r}{origin} is not allowed for nozzle {nozzle}, "
                f"allowed: {_allowed_values(key, nozzle)}"
            ) from None
    return explicit, named


def _check_goal(goal: str | list[str] | None) -> None:
    """``ToolError`` naming the allowed keys for a goal that is not one key or two different ones."""
    if goal is not None:
        try:
            parse_goal(goal)
        except ValueError as exc:
            raise ToolError(str(exc)) from None


def _job_nozzle(job: Job) -> str:
    try:
        return f"{parse_manifest(json.loads(job.manifest_json)).nozzle_mm:g}"
    except (ValueError, RecursionError, ManifestError):
        raise ToolError(f"job {job.id} has an unreadable manifest") from None


def _stored_nozzle(job: Job) -> str | None:
    """The manifest's nozzle for a status report: None rather than an error when unreadable."""
    try:
        return _job_nozzle(job)
    except ToolError:
        return None


def _loads(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _job_summary(job: Job, tz: ZoneInfo) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "name": job.name,
        "kind": job.kind,
        "status": job.status,
        "created_at": job.created_at.astimezone(tz).isoformat(),
        "sliced_file": job.sliced_file,
    }


def _spool_name(store: Store, spool_id: object) -> str | None:
    if not isinstance(spool_id, int):
        return None
    spool = next((s for s in store.spools(active_only=False) if s.id == spool_id), None)
    if spool is None:
        return None
    return " ".join(part for part in (spool.brand, spool.material, spool.color_name) if part)


def _job_slot(store: Store, result: dict) -> dict[str, Any] | None:
    slot = result.get("slot")
    if not isinstance(slot, dict) or not all(isinstance(slot.get(k), int) for k in ("ams", "slot")):
        return None
    return {
        "ams": slot["ams"],
        "slot": slot["slot"],
        "number": slot_number(slot["ams"], slot["slot"]),
        "material": result.get("material") or slot.get("tray_type"),
        "color": slot.get("color"),
        "spool": _spool_name(store, slot.get("spool_id")),
    }


def _job_plates(result: dict) -> list[dict[str, Any]]:
    costs = {c.get("index"): c for c in result.get("costs") or () if isinstance(c, dict)}
    plates = []
    for plate in result.get("plates") or ():
        if not isinstance(plate, dict):
            continue
        cost = costs.get(plate.get("index"), {})
        grams = plate.get("grams")
        plates.append(
            {
                "index": plate.get("index"),
                "minutes": plate.get("minutes"),
                "grams": round(sum(grams.values()), 2) if isinstance(grams, dict) else None,
                "cost_filament_uah": _money(cost.get("filament_uah")),
                "cost_power_uah": _money(cost.get("power_uah")),
                "cost_amort": _money(cost.get("amort")),
                "cost_total_uah": _money(cost.get("total_uah")),
                "currency": cost.get("currency"),
            }
        )
    return plates


def _job_status(store: Store, job: Job, tz: ZoneInfo) -> dict[str, Any]:
    """The job for a client: file and spool names only, never a server path."""
    result = _loads(job.result_json) or {}
    return {
        **_job_summary(job, tz),
        "settings": _loads(job.settings_json),
        "named_set": result.get("named_set"),
        "slot": _job_slot(store, result),
        "chosen_slot": job.slot,
        "goal": _job_goal(job),
        "explicit": _loads(job.explicit_json),
        "evaluation": _job_evaluation(store, job),
        "plate_type": result.get("plate_type"),
        "nozzle": result.get("nozzle") or _stored_nozzle(job),
        "plates": _job_plates(result),
        "warning": _slot_warning(result),
        "error": job.error,
    }


def _job_variants(job: Job | None) -> dict[str, Any]:
    """The job's last evaluation: the chosen variant and each variant's numbers, no files."""
    data = _loads(job.variants_json) if job is not None else None
    entries = data.get("variants") if data is not None else None
    if not isinstance(entries, list):
        return {"chosen": None, "variants": []}
    keys = ("n", "status", "settings", "minutes", "grams", "cost", "currency", "strength_ratio", "error")
    return {
        "chosen": data.get("chosen"),
        "variants": [{k: v.get(k) for k in keys} for v in entries if isinstance(v, dict)],
    }


def _job_evaluation(store: Store, job: Job) -> dict[str, Any] | None:
    """The job's evaluation: ``running``, ``done`` with the variants and the table's lines, or
    ``failed`` with the English ``error`` and ``failed_at`` (and an earlier run's table under
    ``previous``, which is not this run's result)."""
    data = _loads(job.variants_json)
    if data is None:
        return None
    state = data.get("state")
    if state == "running":
        return {"state": "running"}
    entries = data.get("variants")
    done = None
    if isinstance(entries, list) and all(isinstance(v, dict) and type(v.get("n")) is int for v in entries):
        code, symbol = store.currency()
        table = render_variants(
            entries, data.get("chosen"), symbol, code=code, strength_unknown=bool(data.get("strength_unknown"))
        )
        done = {**_job_variants(job), "table": table[1:]}
    if state == "failed":
        failed = {"state": "failed", "error": data.get("error"), "failed_at": data.get("failed_at")}
        return {**failed, "previous": done} if done is not None else failed
    return {"state": "done", **done} if done is not None else None


def _job_goal(job: Job) -> dict[str, Any] | None:
    """The job's goal: the owner's, the CAD agent's (with its reason) or the fallback."""
    try:
        primary, secondary, source, reason = job_goal(job, parse_manifest(json.loads(job.manifest_json)))
    except (ValueError, RecursionError, ManifestError):
        return None
    return {"primary": primary, "secondary": secondary, "source": source, "reason": reason}


def _slot_warning(result: Mapping) -> str | None:
    """The material mismatch the job was sliced with (``SlotChoice.warning``), if any."""
    slot = result.get("slot")
    warning = slot.get("warning") if isinstance(slot, dict) else None
    return warning if isinstance(warning, str) and warning else None


def _check_slot_number(slot: int | None) -> None:
    """The owner's slot number range; whether it can be printed from is the service's check."""
    if slot is not None and not 1 <= slot <= MAX_SLOT_NUMBER:
        raise ToolError(f"slot must be 1..{MAX_SLOT_NUMBER}, got {slot}")


def _spool_by_id(store: Store, spool_id: int) -> Spool:
    spool = next((s for s in store.spools(active_only=False) if s.id == spool_id), None)
    if spool is None:
        raise ToolError(f"no spool {spool_id}")
    return spool


# -- load_spool ----------------------------------------------------------------------


def _check_tray_metadata(
    tray_info_idx: str | None, nozzle_temp_min: int | None, nozzle_temp_max: int | None
) -> None:
    """The spool's load_spool fields as they will be stored (None = not set)."""
    if tray_info_idx is not None and not TRAY_INFO_IDX_RE.fullmatch(tray_info_idx):
        raise ToolError(
            "tray_info_idx must be the preset's filament_id: 3..8 upper-case letters or digits, e.g. GFA00"
        )
    low, high = NOZZLE_TEMP_RANGE
    for name, temp in (("nozzle_temp_min", nozzle_temp_min), ("nozzle_temp_max", nozzle_temp_max)):
        if temp is not None and not low <= temp <= high:
            raise ToolError(f"{name} must be {low}..{high} °C")
    if nozzle_temp_min is not None and nozzle_temp_max is not None and nozzle_temp_min > nozzle_temp_max:
        raise ToolError(f"nozzle_temp_min {nozzle_temp_min} is above nozzle_temp_max {nozzle_temp_max}")


def _check_color_hex(color_hex: str | None) -> None:
    if color_hex is not None and not _SPOOL_HEX_RE.fullmatch(color_hex):
        raise ToolError(f"color_hex must be #RRGGBB (6 hex digits), got {color_hex!r}")


def _tray_color(color_hex: str) -> str | None:
    """A spool's ``#RRGGBB`` as the AMS wants it: ``RRGGBBFF`` upper-case (always opaque)."""
    return f"{color_hex[1:].upper()}FF" if _SPOOL_HEX_RE.fullmatch(color_hex) else None


def _tray_fields(spool: Spool) -> dict[str, Any]:
    """``set_tray`` keyword arguments from a spool, or ``ToolError`` naming what is missing."""
    if not spool.active:
        raise ToolError(f"spool {spool.id} is inactive (retired): reactivate it with update_spool first")
    missing = [
        name
        for name, value in (
            ("material", spool.material),
            ("color_hex", spool.color_hex),
            ("tray_info_idx", spool.tray_info_idx),
            ("nozzle_temp_min", spool.nozzle_temp_min),
            ("nozzle_temp_max", spool.nozzle_temp_max),
        )
        if value is None or (isinstance(value, str) and not value.strip())
    ]
    if missing:
        raise ToolError(
            f"spool {spool.id} lacks {', '.join(missing)}: set them with update_spool "
            "(tray_info_idx and the nozzle range are printed by `slice-agent flatten`)"
        )
    tray_type = spool.material.strip().upper()
    if tray_type not in TRAY_TYPES:
        raise ToolError(
            f"spool {spool.id} material {spool.material!r} is not an AMS type: {', '.join(TRAY_TYPES)}"
        )
    color = _tray_color(spool.color_hex)
    if color is None:  # a row written before the spool tools checked colours
        raise ToolError(
            f"spool {spool.id} color_hex {spool.color_hex!r} is not #RRGGBB: fix it with update_spool"
        )
    return {
        "tray_info_idx": spool.tray_info_idx,
        "tray_type": tray_type,
        "color_rgba": color,
        "nozzle_temp_min": spool.nozzle_temp_min,
        "nozzle_temp_max": spool.nozzle_temp_max,
    }


def _check_printer_ready(service: StatusSource, ams: int, slot: int) -> None:
    if ams not in AMS_UNITS or slot not in AMS_SLOTS:
        raise ToolError("ams and slot must each be 0..3")
    link = service.link_status
    if link is not LinkStatus.CONNECTED:
        raise ToolError(f"the printer is not connected (link {link.value}), nothing was sent")
    if not service.state_fresh:
        raise ToolError(
            "waiting for the printer's first report since it connected, try again in a few seconds; "
            "nothing was sent"
        )
    state = service.state
    if state.gcode_state not in LOADABLE_STATES:
        raise ToolError(
            f"the printer is {state.gcode_state}: load_spool works only when it is "
            f"{', '.join(LOADABLE_STATES)}, nothing was sent"
        )
    units = sorted({t.ams for t in state.trays})
    if ams not in units:
        found = ", ".join(map(str, units)) or "none"
        raise ToolError(f"no AMS unit {ams} in the printer's last report (units: {found}), nothing was sent")


# -- app ---------------------------------------------------------------------------


def _allowed(listen: list[tuple[str, int]]) -> TransportSecuritySettings:
    hosts: list[str] = []
    for host, port in listen:
        names = [f"[{host}]" if ":" in host else host]
        if host in ("127.0.0.1", "::1"):
            names.append("localhost")
        hosts += [f"{name}:{port}" for name in names]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"http://{h}" for h in hosts],
    )


def build_mcp(
    service: StatusSource, store: Store, settings: Settings, camera: Camera
) -> MCPServer:
    tz = ZoneInfo(settings.timezone)
    mcp = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS)

    # -- read ------------------------------------------------------------------

    @mcp.tool(annotations=READ)
    async def printer_status() -> dict[str, Any]:
        """Current printer state as last reported, plus the MQTT link status."""
        return _status(service.state, service.link_status)

    @mcp.tool(annotations=READ)
    async def ams_status() -> dict[str, Any]:
        """AMS trays as last reported, each with the spool mapped to its slot (or null)."""
        trays = []
        for t in service.state.trays:
            spool = store.slot_spool(t.ams, t.slot)
            trays.append(
                {
                    "ams": t.ams,
                    "slot": t.slot,
                    "tray_type": t.tray_type,
                    "color_rgba": t.color_rgba,
                    "remain_pct": t.remain_pct,
                    "spool": _spool(spool) if spool else None,
                }
            )
        return {"trays": trays}

    @mcp.tool(annotations=READ)
    async def camera_snapshot() -> Image:
        """One JPEG frame from the printer camera."""
        try:
            data = await camera()
        except camera_module.CameraBusy as exc:
            raise ToolError("camera busy with another snapshot, try again in a few seconds") from exc
        if not data:
            raise ToolError("camera snapshot failed, see the service log")
        return Image(data=data, format="jpeg")

    @mcp.tool(annotations=READ)
    async def print_history(part: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Print journal, newest first; `part` matches a substring of the job name.

        Costs are in the row's `currency` (the `_uah` names predate currencies).
        """
        limit = max(1, min(limit, HISTORY_MAX))
        return {"prints": [_print(r, tz) for r in store.history(part, limit)]}

    @mcp.tool(annotations=READ)
    async def spools() -> dict[str, Any]:
        """All spools, active and inactive."""
        return {"spools": [_spool(s) for s in store.spools(active_only=False)]}

    @mcp.tool(annotations=READ)
    async def plates() -> dict[str, Any]:
        """Build plates; one may be the default."""
        return {"plates": [_plate(p) for p in store.plates()]}

    # -- write -----------------------------------------------------------------

    @mcp.tool(annotations=WRITE)
    async def add_spool(
        brand: str,
        material: str,
        color_name: str,
        color_hex: str,
        price_uah: float,
        net_g: float,
        filament_preset: str | None = None,
        tray_info_idx: str | None = None,
        nozzle_temp_min: int | None = None,
        nozzle_temp_max: int | None = None,
    ) -> dict[str, Any]:
        """Add a full spool (remaining = net_g). price_uah is the whole spool's price in the
        current currency (see set_currency), whatever the parameter's name says.

        tray_info_idx (the preset's filament_id) and nozzle_temp_min/max (°C) are what
        load_spool sends to the AMS; `slice-agent flatten` prints them per filament preset.
        """
        if net_g <= 0 or price_uah < 0:
            raise ToolError("net_g must be positive and price_uah non-negative")
        _check_color_hex(color_hex)
        _check_tray_metadata(tray_info_idx, nozzle_temp_min, nozzle_temp_max)
        spool_id = store.add_spool(
            brand,
            material,
            color_name,
            color_hex,
            filament_preset,
            price_uah,
            net_g,
            tray_info_idx=tray_info_idx,
            nozzle_temp_min=nozzle_temp_min,
            nozzle_temp_max=nozzle_temp_max,
        )
        return _spool(_spool_by_id(store, spool_id))

    @mcp.tool(annotations=WRITE)
    async def update_spool(
        spool_id: int,
        brand: str | None = None,
        material: str | None = None,
        color_name: str | None = None,
        color_hex: str | None = None,
        filament_preset: str | None = None,
        price_uah: float | None = None,
        net_g: float | None = None,
        active: bool | None = None,
        tray_info_idx: str | None = None,
        nozzle_temp_min: int | None = None,
        nozzle_temp_max: int | None = None,
    ) -> dict[str, Any]:
        """Change spool fields; omitted fields stay as they are. active=false retires a spool.

        A new price_uah is in the current currency (see set_currency), and so becomes the spool's.

        tray_info_idx and nozzle_temp_min/max: see add_spool (needed by load_spool).
        """
        fields = {
            k: v
            for k, v in {
                "brand": brand,
                "material": material,
                "color_name": color_name,
                "color_hex": color_hex,
                "filament_preset": filament_preset,
                "price_uah": price_uah,
                "net_g": net_g,
                "active": active,
                "tray_info_idx": tray_info_idx,
                "nozzle_temp_min": nozzle_temp_min,
                "nozzle_temp_max": nozzle_temp_max,
            }.items()
            if v is not None
        }
        if net_g is not None and net_g <= 0:
            raise ToolError("net_g must be positive")
        current = _spool_by_id(store, spool_id)
        _check_color_hex(color_hex)
        _check_tray_metadata(  # the values as they will be stored, the other bound included
            fields.get("tray_info_idx", current.tray_info_idx),
            fields.get("nozzle_temp_min", current.nozzle_temp_min),
            fields.get("nozzle_temp_max", current.nozzle_temp_max),
        )
        with _tool_errors():
            store.update_spool(spool_id, **fields)
        return _spool(_spool_by_id(store, spool_id))

    @mcp.tool(annotations=WRITE)
    async def set_spool_weight(spool_id: int, remaining_g: float) -> dict[str, Any]:
        """Set the remaining filament weight of a spool, in grams."""
        if remaining_g < 0:
            raise ToolError("remaining_g must not be negative")
        with _tool_errors():
            store.set_spool_weight(spool_id, remaining_g)
        return _spool(_spool_by_id(store, spool_id))

    @mcp.tool(annotations=WRITE)
    async def map_slot(ams: int, slot: int, spool_id: int | None) -> dict[str, Any]:
        """Record which spool sits in an AMS slot (null clears it). Sends nothing to the printer."""
        if ams < 0 or slot < 0:
            raise ToolError("ams and slot must not be negative")
        if spool_id is not None:
            _spool_by_id(store, spool_id)
        store.map_slot(ams, slot, spool_id)
        spool = store.slot_spool(ams, slot)
        return {"ams": ams, "slot": slot, "spool": _spool(spool) if spool else None}

    @mcp.tool(annotations=DESTRUCTIVE)
    async def load_spool(spool_id: int, ams: int, slot: int) -> dict[str, Any]:
        """Tell the printer which filament is in an AMS slot (material, colour, nozzle range)
        and map the spool to that slot. The only tool that writes to the printer.

        Needs an idle printer (IDLE, FINISH or FAILED), ams and slot 0..3 of a reported
        AMS unit, and a spool with material, color_hex, tray_info_idx and nozzle_temp_min/max.
        Sent once: check the printer screen afterwards.
        """
        spool = _spool_by_id(store, spool_id)
        fields = _tray_fields(spool)
        # Synchronous from here on: no await between the checks and the send.
        _check_printer_ready(service, ams, slot)
        try:
            sent = service.set_tray(ams=ams, slot=slot, **fields)["print"]
        except PrinterNotConnected as exc:
            raise ToolError(str(exc)) from None
        except ValueError as exc:
            raise ToolError(f"refused before sending: {exc}") from None
        store.map_slot(ams, slot, spool_id)
        try:
            service.request_full_report()
        except PrinterNotConnected:
            log.warning("load_spool sent, but the follow-up full report could not be requested")
        return {
            "sent": {k: v for k, v in sent.items() if k != "sequence_id"},
            "ams": ams,
            "slot": slot,
            "spool": _spool(_spool_by_id(store, spool_id)),
            "note": LOAD_SPOOL_NOTE,
        }

    @mcp.tool(annotations=WRITE)
    async def add_plate(name: str, bed_type: str) -> dict[str, Any]:
        """Add a build plate."""
        plate_id = store.add_plate(name, bed_type)
        return _plate(next(p for p in store.plates() if p.id == plate_id))

    @mcp.tool(annotations=WRITE)
    async def set_default_plate(plate_id: int) -> dict[str, Any]:
        """Make one plate the default."""
        with _tool_errors():
            store.set_default_plate(plate_id)
        return {"plates": [_plate(p) for p in store.plates()]}

    @mcp.tool(annotations=WRITE)
    async def set_tariff(
        valid_from: date, day_uah: float, night_uah: float | None = None
    ) -> dict[str, Any]:
        """Electricity tariff per kWh, in the current currency (see set_currency), for prints
        from `valid_from` on; night_uah = two-zone."""
        if not _finite(day_uah, night_uah):
            raise ToolError("tariff rates must be finite numbers")
        if day_uah <= 0 or (night_uah is not None and night_uah <= 0):
            raise ToolError("tariff rates must be positive")
        if day_uah > TARIFF_MAX or (night_uah is not None and night_uah > TARIFF_MAX):
            raise ToolError(f"tariff rates must be at most {TARIFF_MAX:g} per kWh")
        store.add_tariff(valid_from, day_uah, night_uah)
        return _tariff(store.tariff_on(valid_from))

    @mcp.tool(annotations=WRITE)
    async def set_amortization(
        valid_from: date, printer_price: float, life_hours: float, upkeep_per_hour: float = 0
    ) -> dict[str, Any]:
        """The printer's amortization for prints from `valid_from` on, in the current currency.

        Rate per hour of printing = printer_price / life_hours (expected service life in
        hours of printing) + upkeep_per_hour (nozzles, belts, plates). Same date: replaced.
        """
        if not _finite(printer_price, life_hours, upkeep_per_hour):
            raise ToolError("printer_price, life_hours and upkeep_per_hour must be finite numbers")
        if not (printer_price >= 0 and life_hours > 0 and upkeep_per_hour >= 0):
            raise ToolError(
                "printer_price and upkeep_per_hour must not be negative, life_hours must be positive"
            )
        rate = printer_price / life_hours + upkeep_per_hour
        if not _finite(rate):
            raise ToolError("the rate per hour (printer_price / life_hours + upkeep_per_hour) is not finite")
        if rate > AMORT_RATE_MAX:
            raise ToolError(f"the rate per hour must be at most {AMORT_RATE_MAX:.0f}, got {rate:g}")
        store.add_amortization(valid_from, printer_price, life_hours, upkeep_per_hour)
        entry = store.amortization_on(valid_from)
        if entry is None:  # cannot happen: just written
            raise ToolError("amortization was not stored")
        return _amortization(entry)

    @mcp.tool(annotations=WRITE)
    async def set_currency(code: str, symbol: str) -> dict[str, Any]:
        """The currency of new prices and rates: an ISO code (UAH, EUR) and the symbol shown
        in messages (the local word for the hryvnia, €). Nothing is converted: a cost whose inputs are in another currency is
        refused until they are entered again in this one."""
        code = code.strip().upper()
        if not _CURRENCY_RE.fullmatch(code):
            raise ToolError("code must be a 3-letter ISO currency code, like UAH or EUR")
        if not symbol or symbol != symbol.strip() or len(symbol) > _SYMBOL_MAX or not symbol.isprintable():
            raise ToolError(f"symbol must be 1-{_SYMBOL_MAX} printable characters, no outer spaces")
        store.set_currency(code, symbol)
        current, current_symbol = store.currency()
        return {"currency": current, "currency_symbol": current_symbol}

    @mcp.tool(annotations=WRITE)
    async def set_country(code: str) -> dict[str, Any]:
        """The owner's country, an ISO code (UA, PL). It sets the default timezone (unless
        config.toml names one) and whether the quarterly tariff reminder runs (UA only)."""
        code = code.strip().upper()
        if not _COUNTRY_RE.fullmatch(code):
            raise ToolError("code must be a 2-letter ISO country code, like UA or PL")
        store.set_country(code)
        timezone = with_country(settings, code).timezone
        result: dict[str, Any] = {
            "country": code,
            "timezone": timezone,
            "tariff_reminder": tariff_reminder_enabled(code),
        }
        if country_timezone(code) is None:
            result["note"] = f"no default timezone for {code}: set timezone in config.toml"
        elif timezone != settings.timezone:
            result["note"] = "the timezone applies after a service restart"
        return result

    @mcp.tool(annotations=WRITE)
    async def update_print(
        print_id: int,
        grams: float | None = None,
        spool_id: int | None = None,
        fit: str | None = None,
    ) -> dict[str, Any]:
        """Set grams, spool or fit (ok/tight/loose/failed); grams are deducted from the spool."""
        row = store.print_by_id(print_id)
        if row is None:
            raise ToolError(f"no print {print_id}")
        if fit is not None and fit not in FIT_VALUES:
            raise ToolError(f"fit must be one of {sorted(FIT_VALUES)}")
        if grams is not None and grams < 0:
            raise ToolError("grams must not be negative")
        new_spool_id = row.spool_id if spool_id is None else spool_id
        new_grams = row.grams if grams is None else grams
        spool = _spool_by_id(store, new_spool_id) if new_spool_id is not None else None

        if spool_id is not None or grams is not None:
            # One transaction: refund the recorded deduction, deduct anew, record it.
            with _tool_errors():
                rebook_grams(store, print_id, spool, new_grams)
        if fit is not None:
            store.set_print_fields(print_id, fit=fit)
        updated = store.print_by_id(print_id)
        assert updated is not None
        return _print(updated, tz)

    # -- jobs ------------------------------------------------------------------

    def job_or_error(job_id: int) -> Job:
        job = store.job(job_id)
        if job is None:
            raise ToolError(f"job {job_id} not found")
        return job

    @mcp.tool(annotations=WRITE)
    async def create_job(manifest_path: str) -> dict[str, Any]:
        """Create a print job from a CAD Workshop manifest, by its absolute path on the service machine.

        Works only when the service allows a jobs root; otherwise upload with `bambu-mcp upload`.
        """
        root = settings.jobs_root_allowed
        if root is None:
            raise ToolError(PATH_INTAKE_OFF)
        path = Path(manifest_path)
        if not path.is_absolute():
            raise ToolError("manifest_path must be an absolute path")
        # Synchronous on the event loop on purpose (no await, no thread): intakes never interleave.
        try:
            job = create_job_from_path(store, settings, path, root)
        except JobError as exc:
            raise ToolError(str(exc)) from None
        return {"job_id": job.id, "name": job.name, "kind": job.kind, "status": job.status}

    @mcp.tool(annotations=WRITE)
    async def propose_job(
        job_id: int, slot: int | None = None, goal: str | list[str] | None = None
    ) -> dict[str, Any]:
        """Send the job's proposal (settings, AMS slot, plate, goal) to Telegram with slice, change,
        slot and goal buttons.

        `slot` (1..16, AMS 1 is 1..4, AMS 2 is 5..8) is the owner's choice of AMS slot: pass it
        only when the owner names one. It is stored with the job and used by later slices.
        Without it the stored slot is used, else the slot whose material matches the part.
        `goal` is the owner's print goal, pass it only when the owner names one: balanced,
        speed, strength, quality, cheap, light, fit or watertight, or [primary, secondary].
        It is stored with the job and sets that goal's base settings. Without it the stored
        goal is used, else the CAD agent's, else balanced. Returns the same text. Nothing is
        sliced or printed.
        """
        job_or_error(job_id)
        _check_slot_number(slot)
        _check_goal(goal)
        try:
            text = await service.propose(job_id, slot, goal)
        except JobBusy:
            raise ToolError(f"job {job_id} is being sliced already") from None
        except KeyError:
            raise ToolError(f"job {job_id} not found") from None
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        return {"job_id": job_id, "text": text}

    @mcp.tool(annotations=WRITE)
    async def slice_job(
        job_id: int,
        settings: dict[str, Any] | None = None,
        slot: int | None = None,
        goal: str | list[str] | None = None,
    ) -> dict[str, Any]:
        """Start slicing a job on the owner's PC (woken if needed). Returns at once, never prints.

        `settings`: {"set": "standard" | "strong" | "draft"} and/or overrides: sparse_infill_density
        (0..100), sparse_infill_pattern, wall_loops (1..10), wall_generator, enable_support,
        support_type, brim_type, curr_bed_type, layer_height, top_shell_layers and
        bottom_shell_layers (0..20), outer_wall_speed (20..300 mm/s), seam_position, ironing_type.
        They win over the part's last settings and its hints, and are stored with the job:
        later slices and evaluate_job keep them fixed. `slot` (1..16) is the owner's
        choice of AMS slot: pass it only when the owner names one. It is stored with the job. Without it the job's stored slot
        is used, else the slot whose material matches the part. `goal` (as in propose_job, only
        when the owner names one) is stored with the job and its base settings apply under
        `settings`. Follow it with job_status; the
        summary also goes to Telegram. `warning` is the AMS slot warning (a material mismatch,
        Russian) or null: tell the owner.
        """
        job = job_or_error(job_id)
        explicit, named = explicit_settings(settings, _job_nozzle(job))
        _check_slot_number(slot)
        _check_goal(goal)
        options: dict[str, Any] = {"named_set": named} if named else {}
        if slot is not None:
            options["slot"] = slot
        if goal is not None:
            options["goal"] = goal
        try:
            # The Telegram button's path: claimed now, sliced in a background task, so a
            # cancelled tool call never interrupts the slice.
            started = service.start_slice(job_id, explicit, **options)
        except JobBusy:
            raise ToolError(f"job {job_id} is being sliced already") from None
        except KeyError:
            raise ToolError(f"job {job_id} not found") from None
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        return {"job_id": job_id, "status": "slicing", "warning": started.warning}

    @mcp.tool(annotations=WRITE)
    async def evaluate_job(
        job_id: int, goal: str | list[str] | None = None, settings: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Find the best settings for a part's goal: wake the PC and slice up to 6 variants, one
        after another (the balanced settings first, then the goal's candidates, its base point
        among them). Never prints.

        `goal` (as in propose_job, only when the owner names one) is stored with the job first.
        `settings` (as in slice_job, only the owner's words) are stored with the job and fixed
        in every variant. The best variant for the goal becomes the job's settings, and the
        proposal with the table ("<n> [✓] <settings> · <min> мин · <g> г · <cost> · прочность
        <x>×", strength against the first, balanced variant, "?" when the agent did not
        measure it) goes to Telegram with «Вариант N» buttons. Kits are refused.

        The run takes minutes. Within 90 s this returns `status` "done" with the same text,
        `chosen` (the ✓ variant) and `variants`. Otherwise `status` "evaluating": the run goes
        on, the table goes to Telegram, and job_status shows it under `evaluation` when done.
        A run that fails (or a restart cuts off) shows there as state "failed" with `error`:
        a table under `previous` is an earlier run's, not this one's. Follow with
        choose_variant or slice_job.
        """
        job = job_or_error(job_id)
        explicit, _named = explicit_settings(settings, _job_nozzle(job))  # a set is expanded into it
        _check_goal(goal)
        try:
            task = service.start_evaluate(job_id, goal, explicit or None)
        except JobBusy:
            raise ToolError(f"job {job_id} is being sliced or evaluated already") from None
        except KeyError:
            raise ToolError(f"job {job_id} not found") from None
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        # asyncio.wait never cancels the run, also when this call is cancelled.
        done, _pending = await asyncio.wait({task}, timeout=EVAL_WAIT_S)
        if not done:
            return {
                "job_id": job_id,
                "status": "evaluating",
                "note": "the run goes on: the table goes to Telegram, job_status shows it when done",
            }
        text, error = task.result()
        if error is not None:
            raise ToolError(str(EvaluationFailed(error)))
        return {"job_id": job_id, "status": "done", "text": text, **_job_variants(store.job(job_id))}

    @mcp.tool(annotations=WRITE)
    async def choose_variant(job_id: int, n: int) -> dict[str, Any]:
        """Choose variant `n` (1..6) of the job's last evaluate_job run as its settings, as the
        «Вариант N» button does. The owner's words (stored from slice_job or evaluate_job
        settings) still win over it. A following slice_job slices that variant again.
        Returns the proposal text, also sent to Telegram."""
        job_or_error(job_id)
        try:
            text = await service.choose_variant(job_id, n)
        except JobBusy:
            raise ToolError(f"job {job_id} is being sliced or evaluated already") from None
        except KeyError:
            raise ToolError(f"job {job_id} not found") from None
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        return {"job_id": job_id, "text": text}

    @mcp.tool(annotations=WRITE)
    async def reset_settings(job_id: int, keys: list[str] | None = None) -> dict[str, Any]:
        """Clear the owner's explicit settings of a job (those slice_job or evaluate_job stored),
        when the owner asks to drop them: all of them, or only `keys` (setting names, as in
        slice_job). Returns `explicit` (what is left) and `settings` (the job's settings now).
        Nothing is sliced. Refused while the job is being sliced or evaluated."""
        job_or_error(job_id)
        for key in keys or []:
            if key not in OVERRIDE_KEYS:
                raise ToolError(f"{key}: unknown setting, allowed: {', '.join(OVERRIDE_KEYS)}")
        try:
            settings = await service.reset_settings(job_id, keys)
        except JobBusy:
            raise ToolError(f"job {job_id} is being sliced or evaluated already") from None
        except KeyError:
            raise ToolError(f"job {job_id} not found") from None
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        job = store.job(job_id)
        return {"job_id": job_id, "explicit": _loads(job.explicit_json if job else None) or {}, "settings": settings}

    @mcp.tool(annotations=READ)
    async def job_status(job_id: int) -> dict[str, Any]:
        """A job: status, settings, the owner's explicit settings, AMS slot it was sliced with,
        the owner's chosen slot (1..16 or null: by material), goal, the evaluation (`running`,
        `done` with its variants and table lines, or `failed` with `error` and `failed_at`),
        plate type, nozzle, per-plate minutes, grams
        and cost (in its currency), the sliced file name, the slot warning (material mismatch)
        and the last error."""
        return _job_status(store, job_or_error(job_id), tz)

    @mcp.tool(annotations=READ)
    async def jobs(limit: int = 10) -> dict[str, Any]:
        """Print jobs, newest first (limit 1..50)."""
        limit = max(1, min(limit, JOBS_MAX))
        return {"jobs": [_job_summary(j, tz) for j in store.jobs(limit)]}

    # -- printing (phase 3) ------------------------------------------------------

    @mcp.tool(annotations=DESTRUCTIVE)
    async def prepare_print(job_id: int, plate: int = 1) -> dict[str, Any]:
        """Check a sliced job against the printer and send the print confirmation to Telegram.

        Wakes the PC, fetches the sliced file and runs the pre-flight (idle printer, USB stick,
        nozzle, slot material, spool remainder, file unchanged). `plate` is the kit's plate,
        always 1 for a part. Starts nothing: the print starts only when the owner presses the
        button under the confirmation in Telegram, within 10 minutes. Returns the confirmation
        text and its warnings (show them to the owner). Refusals list every blocking reason.
        """
        try:
            result = await service.prepare_print(job_id, plate)
        except PrintRefused as exc:
            raise ToolError(str(exc)) from None
        except KeyError:
            raise ToolError(f"job {job_id} not found") from None
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        return {
            "confirm_id": result.confirm_id,
            "text": result.text,
            "warnings": list(result.warnings),
            "note": PREPARE_NOTE,
        }

    @mcp.tool(annotations=DESTRUCTIVE)
    async def pause_print() -> dict[str, Any]:
        """Pause the current print. Works only while the printer is RUNNING, sent once.

        There is no resume tool: the owner resumes on the printer or in Bambu Studio.
        """
        try:
            paused = await service.pause_print()
        except ControlRefused as exc:
            raise ToolError(str(exc)) from None
        return {"sent": "pause", **paused, "note": PAUSE_NOTE}

    @mcp.tool(annotations=DESTRUCTIVE)
    async def request_stop() -> dict[str, Any]:
        """Ask the owner in Telegram whether to stop the current print (PREPARE, RUNNING or PAUSE).

        Stops nothing itself: the print stops only when the owner presses the stop button
        under that message, within 10 minutes. A stopped print cannot be resumed.
        """
        try:
            stop = await service.request_stop()
        except ControlRefused as exc:
            raise ToolError(str(exc)) from None
        return {"confirm_id": stop.confirm_id, "text": stop.text, "note": STOP_NOTE}

    return mcp


def build_app(
    service: StatusSource,
    store: Store,
    secrets: Secrets,
    settings: Settings,
    *,
    camera: Camera | None = None,
    max_job_file_bytes: int = MAX_FILE_BYTES,
) -> Starlette:
    """The MCP Streamable HTTP app at ``/mcp`` and ``POST /jobs``, behind the bearer-token middleware."""
    if camera is None:
        # Fail fast when another snapshot is running instead of queueing behind it.
        camera = functools.partial(camera_module.snapshot, secrets, settings, wait=False)
    mcp = build_mcp(service, store, settings, camera)
    app = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=_allowed(settings.listen),
    )
    app.router.routes.append(
        Route(JOBS_PATH, _jobs_endpoint(store, settings, max_job_file_bytes), methods=["POST"])
    )
    app.add_middleware(BearerTokenMiddleware, tokens=secrets.client_tokens)
    return app


# -- job upload ----------------------------------------------------------------------


def _jobs_endpoint(
    store: Store, settings: Settings, max_file: int
) -> Callable[[Request], Awaitable[Response]]:
    """``POST /jobs``: multipart ``manifest`` + ``file`` -> 201 ``{"job_id", ...}``.

    The body is streamed: the declared length and a running total are checked
    against the caps, and the 3MF goes to a temp file, never fully into memory
    before the caps hold.
    """
    limit = max_file + MAX_MANIFEST_BYTES + _BODY_SLACK

    def refuse(exc: JobError) -> JSONResponse:
        too_large = isinstance(exc, JobTooLarge)
        log.info("job upload refused: %s", exc)
        return JSONResponse(
            {"error": "too_large" if too_large else "bad_request", "detail": str(exc)},
            status_code=413 if too_large else 422,
        )

    async def endpoint(request: Request) -> Response:
        upload: JobUpload | None = None
        try:
            kind, options = parse_options_header(request.headers.get("content-type", ""))
            boundary = options.get(b"boundary")
            if kind != b"multipart/form-data" or not boundary:
                raise JobError("expected multipart/form-data")
            declared = request.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > limit:
                raise JobTooLarge("request is too large")
            upload = JobUpload(boundary, incoming_dir(settings), max_file, MAX_MANIFEST_BYTES)
            total = 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > limit:
                    raise JobTooLarge("request is too large")
                upload.write(chunk)
            manifest_bytes, path = upload.finish()
            job = create_job_from_bytes(store, settings, manifest_bytes, path.read_bytes())
        except JobError as exc:
            return refuse(exc)
        except Exception:
            log.exception("job upload failed unexpectedly")
            return JSONResponse({"error": "internal", "detail": "internal error"}, status_code=500)
        finally:
            if upload is not None:
                upload.discard()
        return JSONResponse(
            {"job_id": job.id, "name": job.name, "kind": job.kind, "status": job.status},
            status_code=201,
        )

    return endpoint


# -- serving -----------------------------------------------------------------------


class _Server(uvicorn.Server):
    """The service owns SIGTERM/SIGINT; uvicorn must not replace its handlers."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def _bind(listen: list[tuple[str, int]]) -> list[socket.socket]:
    sockets = []
    for host, port in listen:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            sockets.append(socket.create_server((host, port), family=family))
        except OSError as exc:
            log.error("MCP server cannot listen on %s:%d: %s", host, port, exc)
    return sockets


class _StartupFailed(RuntimeError):
    """uvicorn called sys.exit() (failed lifespan startup or bind)."""


async def _run(server: uvicorn.Server, sockets: list[socket.socket]) -> None:
    # SystemExit must not leave this task: asyncio re-raises it out of the event
    # loop, which would stop the whole service, not just the MCP server.
    try:
        await server.serve(sockets=sockets)
    except SystemExit as exc:
        raise _StartupFailed(f"uvicorn exited with status {exc.code}") from exc


async def serve(
    service: StatusSource, store: Store, secrets: Secrets, settings: Settings
) -> None:
    """Serve MCP on every ``settings.listen`` address until cancelled."""
    if not secrets.client_tokens:
        log.warning("no CLIENT_TOKEN_* configured, MCP server not started")
        return
    app = build_app(service, store, secrets, settings)
    sockets = _bind(settings.listen)
    if not sockets:
        log.error("MCP server has no address to listen on, not started")
        return
    config = uvicorn.Config(
        app,
        log_config=None,  # keep the service's logging (and its redaction)
        access_log=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
    )
    server = _Server(config)
    log.info(
        "MCP server listening on %s for clients %s",
        ", ".join(str(s.getsockname()[:2]) for s in sockets),
        ", ".join(sorted(secrets.client_tokens)),
    )
    task = asyncio.ensure_future(_run(server, sockets))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await task
        raise
    except _StartupFailed:
        log.error("MCP server failed to start, the service keeps running without it")
    except Exception:
        log.exception("MCP server crashed, the service keeps running without it")
        raise
    finally:
        for sock in sockets:
            sock.close()
