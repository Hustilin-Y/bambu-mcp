"""bambu-service: one asyncio process wiring the printer link to notifications and the journal.

MQTT link (paho thread) -> PrinterState -> events -> journal + outbox (one SQLite transaction),
then a sender task delivers the outbox in order (with camera snapshots), retrying through
outages and restarts. Plus the post-print questions, the button presses and the quarterly
tariff reminder.
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
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from bambu_mcp import camera as camera_module
from bambu_mcp import mcp_server
from bambu_mcp.config import Secrets, Settings
from bambu_mcp.cost import tariff_check_due
from bambu_mcp.events import CANCEL_ERROR, Event, EventDetector, EventKind, event_identity
from bambu_mcp.journal import NO_SPOOL, TERMINAL_KINDS, Journal, Slot
from bambu_mcp.logthrottle import Throttle
from bambu_mcp.messages import (
    fit_question,
    post_print_buttons,
    render_event,
    spool_buttons,
    spool_question,
)
from bambu_mcp.notify import Button, LogNotifier, Notifier, Press, SendError, ServiceNotifyClient
from bambu_mcp.printer.hms import HmsTexts
from bambu_mcp.printer.link import LinkStatus, PrinterLink
from bambu_mcp.printer.state import PrinterState, apply
from bambu_mcp.redact import install_redaction
from bambu_mcp.store.db import Store
from bambu_mcp.store.models import OutboxRow, Tariff

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
# Outbox retries: 30 s doubling to 30 min. The add-in keeps dedupe keys for 7 days, so a
# retry never shows twice; after a day the news is stale and the message is dropped.
OUTBOX_BACKOFF_START_S = 30.0
OUTBOX_BACKOFF_MAX_S = 30 * 60.0
OUTBOX_GIVE_UP_S = 24 * 3600.0
PRESS_RESTART_S = 5.0
# tray_now: 0..15 = AMS unit * 4 + slot, 254 = external spool, 255 = nothing loaded.
_SLOTS_PER_AMS = 4
_MAX_AMS_TRAY = 15

Camera = Callable[[], Awaitable[bytes | None]]
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


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


def _rows_json(rows: list[list[Button]] | None) -> list[list[dict[str, str]]] | None:
    return [[{"id": b.id, "text": b.text} for b in row] for row in rows] if rows else None


def _rows_buttons(rows: list[list[dict[str, str]]] | None) -> list[list[Button]] | None:
    return [[Button(b["id"], b["text"]) for b in row] for row in rows] if rows else None


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
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._secrets = secrets
        self._settings = settings
        self._store = store
        self._notifier = notifier
        self._link_factory = link_factory
        self._camera = camera
        self._clock = clock
        self._sleep = sleep
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
        self._job_seen: tuple[tuple, datetime] | None = None  # job identity, first seen at
        self._first_report = True  # the first state after start is checked against the journal
        self._auth_alerted = False
        self._head_image: tuple[int, bytes | None] | None = None  # outbox head's snapshot
        self._quiet = Throttle(log)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._inbox: asyncio.Queue[tuple[str, Any]] | None = None
        self._outbox_wake: asyncio.Event | None = None
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
        self._outbox_wake = asyncio.Event()
        self._outbox_wake.set()  # messages left from the last run go out first
        self._stop_requested = asyncio.Event()
        link = self._link_factory(self._on_report_threadsafe, self._on_status_threadsafe)
        tasks = [
            asyncio.create_task(self._consume_inbox(), name="bambu-inbox"),
            asyncio.create_task(self._send_outbox(), name="bambu-outbox"),
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
            self._outbox_wake = None

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
        """Queue the quarterly tariff reminder when due. Returns True when it was queued."""
        today = self._clock().astimezone(self._tz).date()
        raw = self._store.get_meta(TARIFF_META_KEY)
        last = date.fromisoformat(raw) if raw else None
        if not tariff_check_due(last, today):
            return False
        text = tariff_text(self._store.tariff_on(today))
        with self._store.transaction():
            self._enqueue("tariff", text, f"tariff-reminder|{today.isoformat()}")
            self._store.set_meta(TARIFF_META_KEY, today.isoformat())
        return True

    async def send_next(self) -> float | None:
        """Try the oldest outbox message once.

        Returns None when the outbox is empty, 0 when the head was sent or dropped (go on),
        or the seconds until the head is due again.
        """
        row = self._store.outbox_head()
        if row is None:
            self._head_image = None
            return None
        now = self._clock().timestamp()
        if row.next_attempt_at > now:
            return row.next_attempt_at - now
        files = None
        if row.snapshot:
            image = await self._snapshot_for(row.id)
            files = [(SNAPSHOT_NAME, image)] if image else None
        try:
            await self._notifier.send(
                row.text, buttons=_rows_buttons(row.buttons), files=files, dedupe=row.dedupe
            )
        except SendError as exc:
            return self._delivery_failed(row, exc)
        except Exception as exc:  # a bug in the client must not stop the outbox
            log.exception("sending a %s message failed", row.kind)
            return self._delivery_failed(row, SendError(None, type(exc).__name__))
        self._drop(row)
        self._quiet.reset("outbox")
        return 0

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
        """Reports and link statuses. Never awaits HTTP or the camera: the outbox does."""
        assert self._inbox is not None
        while True:
            kind, value = await self._inbox.get()
            try:
                if kind == "report":
                    self._handle_report(value)
                else:
                    self._handle_status(value)
            except Exception:
                log.exception("handling a printer %s failed", kind)
            finally:
                self._inbox.task_done()

    async def _send_outbox(self) -> None:
        wake = self._outbox_wake
        assert wake is not None
        while True:
            try:
                delay = await self.send_next()
            except Exception:
                log.exception("outbox delivery failed")
                delay = OUTBOX_BACKOFF_START_S
            if delay == 0:
                continue
            if delay is None:
                await wake.wait()
                wake.clear()
            else:
                await self._sleep(delay)

    async def _consume_presses(self) -> None:
        """Supervises the press long-poll: a crash is logged and the poll restarted."""
        while True:
            try:
                async for press in self._notifier.presses():
                    await self._handle_press(press)
            except Exception:
                log.exception("button press consumer failed, restarting in %.0f s", PRESS_RESTART_S)
            else:
                return  # a notifier without presses (log only)
            await self._sleep(PRESS_RESTART_S)

    async def _tariff_loop(self) -> None:
        while True:
            try:
                await self.check_tariff()
            except Exception:
                log.exception("tariff reminder check failed")
            await asyncio.sleep(TARIFF_CHECK_INTERVAL_S)

    # -- outbox ----------------------------------------------------------------

    def _enqueue(
        self,
        kind: str,
        text: str,
        dedupe: str,
        *,
        buttons: list[list[Button]] | None = None,
        snapshot: bool = False,
    ) -> None:
        self._store.outbox_add(
            kind,
            text,
            notify_dedupe(dedupe),
            buttons=_rows_json(buttons),
            snapshot=snapshot,
            now=self._clock().timestamp(),
        )
        if self._outbox_wake is not None:
            self._outbox_wake.set()

    async def _snapshot_for(self, row_id: int) -> bytes | None:
        """The head message's picture: taken at its first attempt, reused for retries."""
        if self._head_image is None or self._head_image[0] != row_id:
            self._head_image = (row_id, await self._snapshot())
        return self._head_image[1]

    async def _snapshot(self) -> bytes | None:
        try:
            return await self._camera()
        except Exception:
            log.exception("camera snapshot failed, sending without a picture")
            return None

    def _drop(self, row: OutboxRow) -> None:
        self._store.outbox_delete(row.id)
        self._head_image = None

    def _delivery_failed(self, row: OutboxRow, exc: SendError) -> float:
        now = self._clock().timestamp()
        if not exc.retryable:
            log.warning("%s message dropped: the add-in refused it (%s)", row.kind, exc)
            self._drop(row)
            return 0
        attempts = row.attempts + 1
        if now - row.created_at >= OUTBOX_GIVE_UP_S:
            log.warning(
                "%s message given up after %d attempts in %.0f h (%s)",
                row.kind,
                attempts,
                (now - row.created_at) / 3600,
                exc,
            )
            self._drop(row)
            return 0
        delay = min(OUTBOX_BACKOFF_START_S * 2 ** (attempts - 1), OUTBOX_BACKOFF_MAX_S)
        if exc.retry_after is not None:
            delay = max(delay, min(exc.retry_after, OUTBOX_BACKOFF_MAX_S))
        self._store.outbox_defer(row.id, attempts, now + delay)
        self._quiet.warning(
            "outbox",
            "%s message not delivered (%s), attempt %d, next in %.0f s",
            row.kind,
            exc,
            attempts,
            delay,
        )
        return delay

    # -- handlers --------------------------------------------------------------

    def _handle_status(self, status: LinkStatus) -> None:
        self._link_status = status
        if status is LinkStatus.AUTH_FAILED and not self._auth_alerted:
            self._auth_alerted = True
            at = self._clock().astimezone(UTC)
            self._enqueue("auth", AUTH_FAILED_TEXT, f"auth-failed|{at:%Y%m%dT%H%M%S}")

    async def _handle_press(self, press: Press) -> None:
        try:
            text = self._journal.on_press(press)
            if text is not None:
                await self._notifier.edit(press.message_id, text)
        except Exception:
            log.exception("handling a button press failed")

    def _handle_report(self, report: dict) -> None:
        prev = self._state
        new = apply(prev, report)
        if new is prev:  # gcode_line, version and other non-state messages
            return
        at = self._clock()
        self._state = new
        if new.gcode_state != "UNKNOWN":
            identity = event_identity(new)
            if self._job_seen is None or self._job_seen[0] != identity:
                self._job_seen = (identity, at)
        # Seen keys, journal rows and outbox messages of one report commit together.
        with self._store.transaction():
            events = self._detector.feed(prev, new, at)
            if any(e.kind is EventKind.STARTED for e in events):
                self._slot = None
            if new.gcode_state in ACTIVE_STATES:
                slot = _tray_slot(report)
                if slot is not None:
                    self._slot = slot
            ended = {e.identity for e in events if e.kind in TERMINAL_KINDS}
            for event in [*self._unseen_ends(new, at, ended), *events]:
                try:
                    with self._store.transaction():
                        self._process(event)
                except Exception:
                    log.exception("handling a %s event failed", event.kind)

    def _unseen_ends(self, new: PrinterState, at: datetime, ended: set[tuple]) -> list[Event]:
        """Close journal rows of prints whose end the service did not see.

        Checked on the first report after start and on every non-active report. The
        printer still naming the job in FINISH/FAILED gives the outcome; IDLE or another
        job means it was interrupted. The dedupe key is the normal event's, so a print
        whose end was already announced is closed without a second message.
        """
        state = new.gcode_state
        if state == "UNKNOWN":
            return []
        active = state in ACTIVE_STATES
        if active and not self._first_report:
            return []
        self._first_report = False
        current = event_identity(new)
        out: list[Event] = []
        for row in self._store.open_prints():
            identity = row.identity
            if identity in ended or (active and identity == current):
                continue
            if identity == current and state == "FINISH":
                event = Event(EventKind.FINISHED, identity, at, new, end_unknown=True)
            elif identity == current and state == "FAILED":
                kind = EventKind.CANCELLED if new.print_error == CANCEL_ERROR else EventKind.FAILED
                event = Event(kind, identity, at, new, end_unknown=True)
            else:
                name, task_id, start = (part or None for part in identity)
                job = replace(
                    PrinterState.empty(), subtask_name=name, task_id=task_id, gcode_start_time=start
                )
                event = Event(EventKind.FAILED, identity, at, job, end_unknown=True, lost=True)
            if self._store.seen_contains(event.dedupe_key):
                try:
                    self._journal.on_event(event)  # announced before, only the row was left open
                except Exception:
                    log.exception("closing an unseen %s print failed", event.kind)
                continue
            self._store.seen_add(event.dedupe_key)
            out.append(event)
        return out

    def _process(self, event: Event) -> None:
        """Journal the event and queue its messages. A journal failure costs only the numbers."""
        print_id = cost = duration = None
        try:
            with self._store.transaction():
                first_seen = None
                if self._job_seen is not None and self._job_seen[0] == event.identity:
                    first_seen = self._job_seen[1]
                slot = None if event.lost else self._slot
                print_id = self._journal.on_event(event, slot=slot, first_seen=first_seen)
                if event.kind in TERMINAL_KINDS and print_id is not None:
                    row = self._store.print_by_id(print_id)
                    if row is not None and row.actual_min is not None:
                        duration = round(row.actual_min)
                        cost = self._journal.cost_line(row)
        except Exception:
            log.exception("journal update for a %s event failed", event.kind)
            print_id = cost = duration = None

        text = render_event(event, self._hms_texts, cost, duration, tz=self._tz)
        self._enqueue(
            str(event.kind), text, event.dedupe_key, snapshot=event.kind in SNAPSHOT_KINDS
        )
        # No questions for a print interrupted unseen: there is no finished part to ask about.
        if event.kind in TERMINAL_KINDS and print_id is not None and not event.lost:
            self._ask_post_print(event, print_id)

    def _ask_post_print(self, event: Event, print_id: int) -> None:
        name = event.state.subtask_name
        self._enqueue(
            "fit",
            fit_question(name),
            f"{event.dedupe_key}|fit",
            buttons=post_print_buttons(print_id),
        )
        row = self._store.print_by_id(print_id)
        spools = self._store.spools()
        if row is None or row.spool_id is not None or not spools:
            return
        rows = spool_buttons(print_id, spools[:MAX_SPOOL_BUTTONS])
        # Replace the generic last row: phase 1 deducts nothing unless the owner says so.
        rows = [[Button(f"spool:{print_id}:{NO_SPOOL}", NO_SPOOL_TEXT)], *rows[:-1]]
        self._enqueue("spool", spool_question(name), f"{event.dedupe_key}|spool", buttons=rows)


# -- entry point ---------------------------------------------------------------


def build_notifier(secrets: Secrets, settings: Settings) -> Notifier:
    if settings.notify_url and secrets.notify_token:
        return ServiceNotifyClient(settings.notify_url, secrets.notify_token)
    log.warning("notify_url or NOTIFY_TOKEN not set, notifications go to the log only")
    return LogNotifier()


def extra_coroutines(
    service: Service, secrets: Secrets, settings: Settings, store: Store
) -> list[Coroutine[Any, Any, None]]:
    """Coroutines that share the service's event loop: the MCP server."""
    return [mcp_server.serve(service, store, secrets, settings)]


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
    # The SDK logs "Terminating session" at INFO for every stateless request.
    logging.getLogger("mcp.server.streamable_http").setLevel(logging.WARNING)
    config_path = Path(os.environ.get("BAMBU_CONFIG", env_path.parent / "config.toml"))
    settings = Settings.load(config_path)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_amain(secrets, settings))
