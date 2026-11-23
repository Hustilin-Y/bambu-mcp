"""bambu-service: one asyncio process wiring the printer link to notifications and the journal.

MQTT link (paho thread) -> PrinterState -> events -> notifier + journal + camera,
plus the post-print questions, the button presses and the quarterly tariff reminder.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import logging
import os
import signal
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from bambu_mcp import camera as camera_module
from bambu_mcp.config import Secrets, Settings
from bambu_mcp.cost import tariff_check_due
from bambu_mcp.events import Event, EventDetector, EventKind
from bambu_mcp.journal import NO_SPOOL, TERMINAL_KINDS, Journal, Slot
from bambu_mcp.messages import post_print_buttons, render_event, spool_buttons
from bambu_mcp.notify import Button, LogNotifier, Notifier, ServiceNotifyClient
from bambu_mcp.printer.hms import HmsTexts
from bambu_mcp.printer.link import LinkStatus, PrinterLink
from bambu_mcp.printer.state import PrinterState, apply
from bambu_mcp.redact import install_redaction
from bambu_mcp.store.db import Store
from bambu_mcp.store.models import Tariff

log = logging.getLogger(__name__)

DEFAULT_ENV = "/srv/bambu/.env"
DB_NAME = "bambu.db"
HMS_FILE = "hms_en_22E.json"  # scripts/extract_hms.py output for the P2S, kept in data_dir
SNAPSHOT_NAME = "snapshot.jpg"
SNAPSHOT_KINDS = frozenset({EventKind.FIRST_LAYER_DONE, EventKind.FINISHED, EventKind.FAILED})
ACTIVE_STATES = frozenset({"PREPARE", "RUNNING", "PAUSE"})
AUTH_FAILED_TEXT = "Принтер отклонил код доступа: проверь, не сменился ли он"
TARIFF_META_KEY = "tariff_reminded_on"
TARIFF_CHECK_INTERVAL_S = 24 * 3600
DEDUPE_MAX = 128  # service-notify limit for a dedupe key
MAX_BUTTONS = 24  # service-notify limit per message
MAX_SPOOL_BUTTONS = MAX_BUTTONS - 1  # plus "не списывать"
NO_SPOOL_TEXT = "🚫 Не списывать"
# tray_now: 0..15 = AMS unit * 4 + slot, 254 = external spool, 255 = nothing loaded.
_SLOTS_PER_AMS = 4
_MAX_AMS_TRAY = 15

Camera = Callable[[], Awaitable[bytes | None]]
Clock = Callable[[], datetime]


class Link(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


LinkFactory = Callable[[Callable[[dict], None], Callable[[LinkStatus], None]], Link]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def notify_dedupe(key: str) -> str:
    """The key itself, or its SHA-256 when it exceeds the add-in's 128-character limit."""
    if len(key) <= DEDUPE_MAX:
        return key
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def tariff_text(tariff: Tariff) -> str:
    if tariff.night_uah is None:
        rate = f"{tariff.day_uah:.2f} грн/кВт·ч"
    else:
        rate = f"день {tariff.day_uah:.2f} / ночь {tariff.night_uah:.2f} грн/кВт·ч"
    return f"Проверь тариф на электроэнергию: сейчас {rate} с {tariff.valid_from:%d.%m.%Y}"


def _tray_slot(report: dict) -> Slot | None:
    """AMS slot the printer currently feeds from, if the report says so."""
    p = report.get("print")
    ams = p.get("ams") if isinstance(p, dict) else None
    raw = ams.get("tray_now") if isinstance(ams, dict) else None
    try:
        tray = int(raw)
    except (TypeError, ValueError):
        return None
    if not 0 <= tray <= _MAX_AMS_TRAY:
        return None
    return divmod(tray, _SLOTS_PER_AMS)


class Service:
    def __init__(
        self,
        secrets: Secrets,
        settings: Settings,
        store: Store,
        notifier: Notifier,
        link_factory: LinkFactory,
        camera: Camera,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._secrets = secrets
        self._settings = settings
        self._store = store
        self._notifier = notifier
        self._link_factory = link_factory
        self._camera = camera
        self._clock = clock
        self._tz = ZoneInfo(settings.timezone)
        self._journal = Journal(store, settings)
        self._detector = EventDetector(store)
        hms_path = Path(settings.data_dir) / HMS_FILE
        if not hms_path.is_file():
            log.warning("HMS text table %s not found, HMS messages show codes only", hms_path)
        self._hms_texts = HmsTexts.load(hms_path)

        self._state = PrinterState.empty()
        self._link_status = LinkStatus.DISCONNECTED
        self._slot: Slot | None = None  # AMS slot of the current or last print
        self._auth_alerted = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._inbox: asyncio.Queue[tuple[str, Any]] | None = None
        self._stop_requested: asyncio.Event | None = None

    # -- public ----------------------------------------------------------------

    @property
    def state(self) -> PrinterState:
        return self._state

    @property
    def link_status(self) -> LinkStatus:
        return self._link_status

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._inbox = asyncio.Queue()
        self._stop_requested = asyncio.Event()
        link = self._link_factory(self._on_report_threadsafe, self._on_status_threadsafe)
        tasks = [
            asyncio.create_task(self._consume_inbox(), name="bambu-inbox"),
            asyncio.create_task(self._consume_presses(), name="bambu-presses"),
            asyncio.create_task(self._tariff_loop(), name="bambu-tariff"),
        ]
        link.start()
        try:
            await self._stop_requested.wait()
        finally:
            await asyncio.to_thread(link.stop)  # joins paho's network thread
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._loop = None

    async def stop(self) -> None:
        if self._stop_requested is not None:
            self._stop_requested.set()

    async def settle(self) -> None:
        """Wait until every report and status handed in so far has been handled."""
        if self._inbox is None:
            return
        await asyncio.sleep(0)
        await self._inbox.join()

    async def check_tariff(self) -> bool:
        """Send the quarterly tariff reminder when due. Returns True when it was sent."""
        today = self._clock().astimezone(self._tz).date()
        raw = self._store.get_meta(TARIFF_META_KEY)
        last = date.fromisoformat(raw) if raw else None
        if not tariff_check_due(last, today):
            return False
        text = tariff_text(self._store.tariff_on(today))
        message_id = await self._notifier.send(text, dedupe=f"tariff-reminder|{today.isoformat()}")
        if message_id is None and not isinstance(self._notifier, LogNotifier):
            log.warning("tariff reminder not delivered, retrying tomorrow")
            return False
        self._store.set_meta(TARIFF_META_KEY, today.isoformat())
        return True

    # -- paho thread -> loop ---------------------------------------------------

    def _post(self, kind: str, value: Any) -> None:
        loop, inbox = self._loop, self._inbox
        if loop is None or inbox is None:
            return
        with contextlib.suppress(RuntimeError):  # loop already closed at shutdown
            loop.call_soon_threadsafe(inbox.put_nowait, (kind, value))

    def _on_report_threadsafe(self, report: dict) -> None:
        self._post("report", report)

    def _on_status_threadsafe(self, status: LinkStatus) -> None:
        self._post("status", status)

    # -- loop tasks ------------------------------------------------------------

    async def _consume_inbox(self) -> None:
        assert self._inbox is not None
        while True:
            kind, value = await self._inbox.get()
            try:
                if kind == "report":
                    await self._handle_report(value)
                else:
                    await self._handle_status(value)
            except Exception:
                log.exception("handling a printer %s failed", kind)
            finally:
                self._inbox.task_done()

    async def _consume_presses(self) -> None:
        async for press in self._notifier.presses():
            try:
                text = self._journal.on_press(press)
                if text is not None:
                    await self._notifier.edit(press.message_id, text)
            except Exception:
                log.exception("handling a button press failed")

    async def _tariff_loop(self) -> None:
        while True:
            try:
                await self.check_tariff()
            except Exception:
                log.exception("tariff reminder check failed")
            await asyncio.sleep(TARIFF_CHECK_INTERVAL_S)

    # -- handlers --------------------------------------------------------------

    async def _handle_status(self, status: LinkStatus) -> None:
        self._link_status = status
        if status is LinkStatus.AUTH_FAILED and not self._auth_alerted:
            self._auth_alerted = True
            await self._notifier.send(AUTH_FAILED_TEXT)

    async def _handle_report(self, report: dict) -> None:
        prev = self._state
        new = apply(prev, report)
        if new is prev:  # gcode_line, version and other non-state messages
            return
        events = self._detector.feed(prev, new, self._clock())
        self._state = new
        if any(e.kind is EventKind.STARTED for e in events):
            self._slot = None
        if new.gcode_state in ACTIVE_STATES:
            slot = _tray_slot(report)
            if slot is not None:
                self._slot = slot
        for event in events:
            await self._deliver(event)

    async def _deliver(self, event: Event) -> None:
        print_id = self._journal.on_event(event, slot=self._slot)
        cost = duration = None
        if event.kind in TERMINAL_KINDS and print_id is not None:
            row = self._store.print_by_identity(event.identity)
            if row is not None and row.actual_min is not None:
                duration = round(row.actual_min)
                cost = self._journal.cost_line(row)

        files = None
        if event.kind in SNAPSHOT_KINDS:
            image = await self._snapshot()
            files = [(SNAPSHOT_NAME, image)] if image else None
        text = render_event(event, self._hms_texts, cost, duration)
        message_id = await self._notifier.send(
            text, files=files, dedupe=notify_dedupe(event.dedupe_key)
        )
        # The detector marked the event seen before delivery: a failed send loses it.
        if message_id is None and not isinstance(self._notifier, LogNotifier):
            log.warning("notification for a %s event not delivered", event.kind)

        if event.kind in TERMINAL_KINDS and print_id is not None:
            await self._ask_post_print(event, print_id)

    async def _snapshot(self) -> bytes | None:
        try:
            return await self._camera()
        except Exception:
            log.exception("camera snapshot failed")
            return None

    async def _ask_post_print(self, event: Event, print_id: int) -> None:
        name = event.state.subtask_name or "без названия"
        await self._notifier.send(
            f"Как села деталь «{name}»?",
            buttons=post_print_buttons(print_id),
            dedupe=notify_dedupe(f"{event.dedupe_key}|fit"),
        )
        row = self._store.print_by_identity(event.identity)
        spools = self._store.spools()
        if row is None or row.spool_id is not None or not spools:
            return
        rows = spool_buttons(print_id, spools[:MAX_SPOOL_BUTTONS])
        # Replace the generic last row: phase 1 deducts nothing unless the owner says so.
        rows = [[Button(f"spool:{print_id}:{NO_SPOOL}", NO_SPOOL_TEXT)], *rows[:-1]]
        await self._notifier.send(
            f"С какой катушки печаталась «{name}»? Граммы спишутся, только когда ты их укажешь.",
            buttons=rows,
            dedupe=notify_dedupe(f"{event.dedupe_key}|spool"),
        )


# -- entry point ---------------------------------------------------------------


def build_notifier(secrets: Secrets, settings: Settings) -> Notifier:
    if settings.notify_url and secrets.notify_token:
        return ServiceNotifyClient(settings.notify_url, secrets.notify_token)
    log.warning("notify_url or NOTIFY_TOKEN not set, notifications go to the log only")
    return LogNotifier()


def extra_coroutines(
    service: Service, secrets: Secrets, settings: Settings, store: Store
) -> list[Coroutine[Any, Any, None]]:
    """Coroutines that share the service's event loop.

    Task 12: the MCP server is added here (for example ``[mcp_server.serve(...)]``).
    """
    return []


async def _amain(secrets: Secrets, settings: Settings) -> None:
    store = Store(Path(settings.data_dir) / DB_NAME)
    store.migrate()
    notifier = build_notifier(secrets, settings)
    service = Service(
        secrets,
        settings,
        store,
        notifier,
        lambda on_report, on_status: PrinterLink(secrets, settings, on_report, on_status),
        functools.partial(camera_module.snapshot, secrets, settings),
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, AttributeError, ValueError):
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(service.stop()))
    tasks = [asyncio.ensure_future(c) for c in extra_coroutines(service, secrets, settings, store)]
    try:
        await service.run()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if isinstance(notifier, ServiceNotifyClient):
            await notifier.aclose()
        store.close()


def main() -> None:
    env_path = Path(os.environ.get("BAMBU_ENV", DEFAULT_ENV))
    secrets = Secrets.load(env_path)
    install_redaction(secrets)  # before anything logs
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config_path = Path(os.environ.get("BAMBU_CONFIG", env_path.parent / "config.toml"))
    settings = Settings.load(config_path)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_amain(secrets, settings))
