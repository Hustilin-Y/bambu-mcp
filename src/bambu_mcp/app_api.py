"""The Mini App tab's API under ``/v1/``: one snapshot of everything the tab shows, the
capabilities screen, and the few actions an app token may take.

None of it can start, resume or stop a print: those stay presses on the service's own buttons
(design 2026-11-28, section 6.3). Nothing here carries the serial, the printer's address, the
access code, a token or a server path.
"""

from __future__ import annotations

import json
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from bambu_mcp.config import Settings
from bambu_mcp.messages import BUTTONS, control_buttons, start_buttons, stop_buttons
from bambu_mcp.printer.state import EXTERNAL_AMS, EXTERNAL_TRAY
from bambu_mcp.printgate import ControlRefused
from bambu_mcp.proposal import slot_number
from bambu_mcp.store.db import Store
from bambu_mcp.store.models import Confirmation, Job, PrintRow, Spool

SNAPSHOT_MAX = 48 * 1024
JOBS_SHOWN = 20
HISTORY_SHOWN = 12
HISTORY_CUT = 5
NOTES_SHOWN = 5


def build_snapshot(service: Any, store: Store, settings: Settings, now: float) -> dict[str, Any]:
    """Everything the tab draws, at most ``SNAPSHOT_MAX`` bytes as JSON (``truncated`` says so)."""
    from bambu_mcp.mcp_server import _status  # one status shape for MCP and the tab

    state = service.state
    code, symbol = store.currency()
    snapshot: dict[str, Any] = {
        "status": _status(state, service.link_status),
        "printer": _printer(settings),
        "slots": _slots(state, store, settings),
        "jobs": [_job(j) for j in store.jobs(JOBS_SHOWN)],
        "history": [_history(r) for r in store.history(None, HISTORY_SHOWN)],
        "pending": _pending(state, store, settings, now),
        "notes": list(getattr(service, "app_notes", ()))[-NOTES_SHOWN:],
        "currency": {"code": code, "symbol": symbol},
        "truncated": False,
    }
    return _cut(snapshot)


async def build_capabilities(mcp: Any, settings: Settings) -> dict[str, Any]:
    """What the system can do, read from the code: the registered MCP tools with their class,
    the Telegram buttons, the printer profile."""
    tools = []
    for tool in await mcp.list_tools():
        summary = (tool.description or "").strip().splitlines()
        tools.append({"name": tool.name, "summary": summary[0] if summary else "", "class": _tool_class(tool)})
    tools.sort(key=lambda t: (t["class"], t["name"]))
    return {"tools": tools, "buttons": [dict(b) for b in BUTTONS], "printer": _printer(settings)}


def _tool_class(tool: Any) -> str:
    hints = tool.annotations
    if hints is not None and hints.read_only_hint:
        return "read"
    if hints is not None and hints.destructive_hint:
        return "destructive"
    return "change"


def _printer(settings: Settings) -> dict[str, Any]:
    p = settings.profile
    return {
        "model": p.model_name,
        "profile": p.name,
        "bed_mm": list(p.bed_mm),
        "ams_units": p.ams_units,
        "ams_slots": p.ams_slots,
        "external_spool": p.external_spool,
        "camera": str(getattr(p.camera, "value", p.camera)),
        "storage": str(getattr(p.storage, "value", p.storage)),
        "stoppable_states": sorted(p.stoppable_states),
        "unverified": sorted(p.unverified),
    }


def _slots(state: Any, store: Store, settings: Settings) -> list[dict[str, Any]]:
    """The trays the printer reported, numbered as the owner counts them. Without an AMS, the
    external spool, reported or not."""
    profile = settings.profile
    slots = []
    for tray in state.trays:
        external = (tray.ams, tray.slot) == (EXTERNAL_AMS, EXTERNAL_TRAY)
        if not external and not profile.has_ams:
            continue
        slots.append(_slot("external" if external else slot_number(tray.ams, tray.slot), tray, store.slot_spool(tray.ams, tray.slot)))
    if not profile.has_ams and not slots:
        slots.append(_slot("external", None, store.slot_spool(EXTERNAL_AMS, EXTERNAL_TRAY)))
    return slots


def _slot(number: int | str, tray: Any, spool: Spool | None) -> dict[str, Any]:
    return {
        "slot": number,
        "tray_type": tray.tray_type if tray else None,
        "color_rgba": tray.color_rgba if tray else None,
        "remain_pct": tray.remain_pct if tray else None,
        "spool": None
        if spool is None
        else {
            "material": spool.material,
            "color_name": spool.color_name,
            "color_hex": spool.color_hex,
            "remaining_g": round(spool.remaining_g),
        },
    }


def _job(job: Job) -> dict[str, Any]:
    result = _loads(job.result_json)
    plates = []
    for plate in result.get("plates") or ():
        if isinstance(plate, dict):
            grams = plate.get("grams")
            plates.append(
                {
                    "index": plate.get("index"),
                    "minutes": plate.get("minutes"),
                    "grams": round(sum(grams.values()), 1) if isinstance(grams, dict) else None,
                }
            )
    costs = {c.get("index"): c.get("total_uah") for c in result.get("costs") or () if isinstance(c, dict)}
    for plate in plates:
        total = costs.get(plate["index"])
        plate["cost"] = round(total, 2) if isinstance(total, int | float) else None
    return {"id": job.id, "name": job.name, "kind": job.kind, "status": job.status, "plates": plates}


def _history(row: PrintRow) -> dict[str, Any]:
    costs = [c for c in (row.cost_filament_uah, row.cost_power_uah, row.cost_amort) if c is not None]
    return {
        "name": row.identity[0],
        "ended_at": row.ended_at.timestamp() if row.ended_at else None,
        "outcome": row.outcome,
        "grams": row.grams,
        "cost": round(sum(costs), 2) if costs else None,
        "currency": row.currency,
    }


def _pending(state: Any, store: Store, settings: Settings, now: float) -> list[dict[str, Any]]:
    """Messages with buttons the owner may press from the tab: start and stop confirmations
    that went out and are still valid, and the current print's control message."""
    pending = []
    for confirmation in store.pending_confirmations(now):
        sent = store.sent_message(f"confirm:{confirmation.id}")
        if sent is not None:
            pending.append(
                {
                    "message_id": sent.message_id,
                    "kind": confirmation.kind,
                    "text": sent.text,
                    "labels": _labels(confirmation),
                    "expires_at": confirmation.expires_at,
                }
            )
    task_id = state.task_id or ""
    if task_id and state.gcode_state in settings.profile.stoppable_states:
        sent = store.sent_message(f"control:{task_id}")
        if sent is not None:
            buttons = control_buttons(task_id, state.gcode_state == "PAUSE")
            pending.append(
                {
                    "message_id": sent.message_id,
                    "kind": "control",
                    "text": sent.text,
                    "labels": [b.text for row in buttons for b in row],
                    "expires_at": None,
                }
            )
    return pending


def _labels(confirmation: Confirmation) -> list[str]:
    rows = start_buttons(confirmation.id) if confirmation.kind == "start" else stop_buttons(confirmation.id)
    return [b.text for row in rows for b in row]


def _cut(snapshot: dict[str, Any]) -> dict[str, Any]:
    """History to its last few, then jobs from the oldest, until the snapshot fits."""
    def size() -> int:
        return len(json.dumps(snapshot, ensure_ascii=False).encode())

    if size() <= SNAPSHOT_MAX:
        return snapshot
    snapshot["truncated"] = True
    snapshot["history"] = snapshot["history"][:HISTORY_CUT]
    while size() > SNAPSHOT_MAX and snapshot["jobs"]:
        snapshot["jobs"].pop()
    while size() > SNAPSHOT_MAX and snapshot["history"]:
        snapshot["history"].pop()
    return snapshot


def _loads(text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(text) if text else None
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


# -- endpoints -------------------------------------------------------------------------


def app_routes(service: Any, store: Store, settings: Settings, mcp: Any) -> list[Route]:
    """The ``/v1/`` routes. The bearer middleware lets only app tokens reach them."""

    async def snapshot(_request: Request) -> Response:
        return JSONResponse(build_snapshot(service, store, settings, time.time()))

    async def capabilities(_request: Request) -> Response:
        return JSONResponse(await build_capabilities(mcp, settings))

    async def pause(_request: Request) -> Response:
        try:
            await service.pause_print()
        except ControlRefused as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse({"sent": "pause"})

    async def request_stop(_request: Request) -> Response:
        try:
            await service.request_stop()  # its confirmation id stays in the service
        except ControlRefused as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse({"asked": True}, status_code=202)

    async def prepare(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            body = None
        job_id = body.get("job_id") if isinstance(body, dict) else None
        plate = body.get("plate") if isinstance(body, dict) else None
        if not (_positive_int(job_id) and _positive_int(plate)):
            return JSONResponse({"error": "job_id and plate must be positive integers"}, status_code=422)
        if not service.start_prepare(job_id, plate):
            return JSONResponse({"error": "уже готовлю печать"}, status_code=409)
        return JSONResponse({"pending": True}, status_code=202)

    async def snapshot_to_telegram(_request: Request) -> Response:
        if not service.send_snapshot():
            return JSONResponse({"error": "снимок уже в очереди"}, status_code=409)
        return JSONResponse({"queued": True}, status_code=202)

    return [
        Route("/v1/snapshot", snapshot, methods=["GET"]),
        Route("/v1/capabilities", capabilities, methods=["GET"]),
        Route("/v1/pause", pause, methods=["POST"]),
        Route("/v1/request-stop", request_stop, methods=["POST"]),
        Route("/v1/prepare", prepare, methods=["POST"]),
        Route("/v1/snapshot-to-telegram", snapshot_to_telegram, methods=["POST"]),
    ]


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
