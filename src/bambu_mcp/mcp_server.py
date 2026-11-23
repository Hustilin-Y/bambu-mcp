"""MCP server (Streamable HTTP) for the owner's clients, in the service's event loop.

Every request needs ``Authorization: Bearer <token>`` matching one of the
``CLIENT_TOKEN_<NAME>`` values. The middleware rejects anything else with 401
before the MCP app sees it and records the client name (never the token) for
logs. No tool accepts or returns the access code, serial, host or any token.

``POST /jobs`` (same middleware) takes a CAD Workshop manifest and its 3MF as
multipart parts ``manifest`` and ``file`` and creates a job; see ``bambu_mcp.jobs``.

All tools are ``async`` on purpose: the SDK runs sync tools in worker threads,
and the store keeps one SQLite connection per thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hmac
import logging
import socket
from collections.abc import Awaitable, Callable, Iterator
from datetime import date
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
from bambu_mcp.config import Secrets, Settings
from bambu_mcp.jobs import (
    MAX_FILE_BYTES,
    JobError,
    JobTooLarge,
    JobUpload,
    create_job_from_bytes,
    incoming_dir,
)
from bambu_mcp.journal import FIT_VALUES, rebook_grams
from bambu_mcp.manifest import MAX_MANIFEST_BYTES
from bambu_mcp.printer.hms import hms_code, severity
from bambu_mcp.printer.link import LinkStatus
from bambu_mcp.printer.state import PrinterState
from bambu_mcp.store.db import Store
from bambu_mcp.store.models import Plate, PrintRow, Spool, Tariff

log = logging.getLogger(__name__)

MCP_PATH = "/mcp"
JOBS_PATH = "/jobs"
_BODY_SLACK = 64 * 1024  # multipart boundaries and part headers
SERVER_NAME = "bambu-mcp"
GRACEFUL_SHUTDOWN_S = 5
HISTORY_MAX = 200

READ = ToolAnnotations(read_only_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False)

Camera = Callable[[], Awaitable[bytes | None]]


class StatusSource(Protocol):
    """What the MCP server needs from ``Service``."""

    @property
    def state(self) -> PrinterState: ...

    @property
    def link_status(self) -> LinkStatus: ...


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
    }


def _plate(p: Plate) -> dict[str, Any]:
    return {"id": p.id, "name": p.name, "bed_type": p.bed_type, "is_default": p.is_default}


def _tariff(t: Tariff) -> dict[str, Any]:
    return {"valid_from": t.valid_from.isoformat(), "day_uah": t.day_uah, "night_uah": t.night_uah}


def _print(row: PrintRow, tz: ZoneInfo) -> dict[str, Any]:
    costs = [c for c in (row.cost_filament_uah, row.cost_power_uah) if c is not None]
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
        "cost_total_uah": _money(sum(costs)) if costs else None,
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


def _spool_by_id(store: Store, spool_id: int) -> Spool:
    spool = next((s for s in store.spools(active_only=False) if s.id == spool_id), None)
    if spool is None:
        raise ToolError(f"no spool {spool_id}")
    return spool


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
    mcp = MCPServer(SERVER_NAME, instructions="Bambu Lab P2S monitor: status, camera, journal, inventory.")

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
        """Print journal, newest first; `part` matches a substring of the job name. Costs in UAH."""
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
    ) -> dict[str, Any]:
        """Add a full spool (remaining = net_g). Price in UAH for the whole spool."""
        if net_g <= 0 or price_uah < 0:
            raise ToolError("net_g must be positive and price_uah non-negative")
        spool_id = store.add_spool(
            brand, material, color_name, color_hex, filament_preset, price_uah, net_g
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
    ) -> dict[str, Any]:
        """Change spool fields; omitted fields stay as they are. active=false retires a spool."""
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
            }.items()
            if v is not None
        }
        if net_g is not None and net_g <= 0:
            raise ToolError("net_g must be positive")
        _spool_by_id(store, spool_id)
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
        """Electricity tariff (UAH/kWh) for prints from `valid_from` on; night_uah = two-zone."""
        if day_uah <= 0 or (night_uah is not None and night_uah <= 0):
            raise ToolError("tariff rates must be positive")
        store.add_tariff(valid_from, day_uah, night_uah)
        return _tariff(store.tariff_on(valid_from))

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
