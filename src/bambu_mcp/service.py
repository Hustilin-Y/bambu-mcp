"""bambu-service: one asyncio process wiring the printer link to notifications and the journal.

MQTT link (paho thread) -> PrinterState -> events -> journal + outbox (one SQLite transaction),
then a sender task delivers the outbox in order (with camera snapshots), retrying through
outages and restarts. Plus the post-print questions, the button presses and the quarterly
tariff reminder. Phase 2: print jobs are proposed and sliced on the owner's PC (wake it,
slice, send the summary with the plate picture).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import os
import shutil
import signal
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from bambu_mcp import camera as camera_module
from bambu_mcp import jobs as jobs_module
from bambu_mcp import mcp_server
from bambu_mcp.config import Secrets, Settings
from bambu_mcp.cost import tariff_check_due
from bambu_mcp.events import CANCEL_ERROR, Event, EventDetector, EventKind, event_identity
from bambu_mcp.jobs import JobBusy
from bambu_mcp.journal import NO_SPOOL, TERMINAL_KINDS, Journal, Slot
from bambu_mcp.logthrottle import Throttle
from bambu_mcp.manifest import Manifest, ManifestError, parse_manifest, verify_file
from bambu_mcp.messages import (
    fit_question,
    job_name,
    md_literal,
    post_print_buttons,
    render_event,
    render_proposal,
    render_sliced,
    spool_buttons,
    spool_question,
)
from bambu_mcp.names import slug
from bambu_mcp.notify import Button, LogNotifier, Notifier, Press, SendError, ServiceNotifyClient
from bambu_mcp.printer.hms import HmsTexts
from bambu_mcp.printer.link import LinkStatus, PrinterLink
from bambu_mcp.printer.state import PrinterState, apply
from bambu_mcp.proposal import (
    SlotChoice,
    estimate_cost,
    job_hints,
    job_material,
    last_settings,
    pick_slot,
    resolve_settings,
    slot_number,
)
from bambu_mcp.redact import install_redaction
from bambu_mcp.slicer import (
    WAKE_BUDGET_S,
    GatewayWaker,
    SliceClient,
    SliceOutcome,
    SliceRequest,
    WakeError,
)
from bambu_mcp.store.db import Store
from bambu_mcp.store.models import Job, OutboxRow, Spool, Tariff
from slice_agent.runner import PLATE_TYPES

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
# Jobs (phase 2).
DEFAULT_NAMED_SET = "standard"
# The agent always slices on its standard process set: a named set travels as overrides.
PROCESS_SET = "standard"
DEFAULT_PLATE_TYPE = "Textured PEI Plate"
JOB_HISTORY = 200  # newest jobs searched for the part's last settings and colour
BUSY_RETRY_S = 30.0
CLAIMABLE = ("new", "sliced", "slice_failed")
SLICE_BUTTON_TEXT = "✂️ Нарезать"
CHANGE_BUTTON_TEXT = "⚙️ Изменить"
CHANGE_TEXT = "Скажи ассистенту, что поменять: стенки, заполнение, поддержки, пластину"
SLICED_KIND = "job_sliced"
SLICED_TEXT_KIND = "job_sliced_text"  # the summary sent after its picture when too long for a caption
CAPTION_MAX = 1024  # Telegram photo caption limit, in UTF-16 code units
_WAKE_TEXTS = {
    "refused": "шлюз отклонил запрос пробуждения ПК",
    "unreachable": "шлюз не ответил на запрос пробуждения ПК",
    "not_configured": "ПК выключен, а шлюз для пробуждения не настроен",
}
ATTACHMENTS_DIR = "outbox-files"  # data_dir/<this>/<sha256 of the dedupe key>/<file>

Camera = Callable[[], Awaitable[bytes | None]]
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


class Link(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


class Slicer(Protocol):
    """``slicer.SliceClient``."""

    async def health(self) -> bool: ...

    async def slice(self, job: SliceRequest, file_bytes: bytes) -> SliceOutcome: ...

    async def fetch_png(self, name: str, plate: int) -> bytes | None: ...


class Waker(Protocol):
    """``slicer.GatewayWaker``."""

    async def wake_and_wait(self, budget_s: float = WAKE_BUDGET_S) -> bool: ...


class _SliceFailed(Exception):
    """``error`` (English) goes into the job row, ``text`` (Russian, Markdown-safe) to the owner."""

    def __init__(self, error: str, text: str) -> None:
        super().__init__(error)
        self.error = error
        self.text = text


@dataclass(frozen=True)
class SlicePlan:
    """Everything a slice of one job is made with, resolved from the job and the printer state."""

    manifest: Manifest
    settings: dict[str, object]
    choice: SlotChoice
    spool: Spool | None
    material: str  # canonical: as in settings.power_w when listed there
    color: str | None  # the slot's colour, preferred for the part next time
    plate_type: str
    nozzle: str
    named_set: str  # its values are in ``settings``, the agent always gets PROCESS_SET
    filament: str | None  # the spool's Bambu Studio filament preset


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
        slicer: Slicer | None = None,
        waker: Waker | None = None,
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
        self._slicer = slicer
        self._waker = waker
        self._slice_tasks: set[asyncio.Task] = set()

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
        try:
            self._fail_interrupted_jobs()
        except Exception:
            log.exception("checking for interrupted slices failed")
        try:
            self._sweep_attachments()
        except Exception:
            log.exception("removing unused message attachments failed")
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
            tasks += self._slice_tasks  # a cancelled slice marks its job failed
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
        if row.kind == SLICED_KIND:
            files = [*(files or []), *self._attachments(row.dedupe)] or None
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
        if row.kind == SLICED_KIND:
            shutil.rmtree(self._attachments_dir(row.dedupe), ignore_errors=True)

    def _attachments_dir(self, dedupe: str) -> Path:
        name = hashlib.sha256(dedupe.encode("utf-8")).hexdigest()
        return Path(self._settings.data_dir) / ATTACHMENTS_DIR / name

    def _sweep_attachments(self) -> None:
        """Remove attachment folders no outbox message refers to (a crash before the commit)."""
        root = Path(self._settings.data_dir) / ATTACHMENTS_DIR
        if not root.is_dir():
            return
        used = {self._attachments_dir(d).name for d in self._store.outbox_dedupes()}
        for entry in root.iterdir():
            if entry.name in used and entry.is_dir() and not entry.is_symlink():
                continue
            log.info("removing an unused message attachment %s", entry.name)
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    entry.unlink()

    def _attachments(self, dedupe: str) -> list[tuple[str, bytes]]:
        """Files stored for an outbox message (a plate picture), kept until it is dropped."""
        folder = self._attachments_dir(dedupe)
        try:
            if not folder.is_dir():
                return []
            return [(p.name, p.read_bytes()) for p in sorted(folder.iterdir()) if p.is_file()]
        except OSError:
            log.exception("reading a message attachment failed, sending without it")
            return []

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
            if press.button_id.startswith("job:"):
                text = self._on_job_press(press)
            else:
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

    # -- jobs: proposal and slicing ----------------------------------------------

    async def propose(self, job_id: int) -> str:
        """Queue the job's proposal with the slice and change buttons. Returns its text.

        Raises ``KeyError`` for an unknown job, ``ValueError`` when the settings do not resolve.
        """
        job = self._job(job_id)
        plan = self._plan(job, None, DEFAULT_NAMED_SET)
        text = render_proposal(job, plan.settings, plan.choice, plan.plate_type, None)
        warning = _preset_warning(plan)
        if warning:
            text = f"{text}\n{warning}"
        buttons = [[
            Button(f"job:{job.id}:slice", SLICE_BUTTON_TEXT),
            Button(f"job:{job.id}:change", CHANGE_BUTTON_TEXT),
        ]]
        self._enqueue("job_proposal", text, _job_key(job.id, "proposal"), buttons=buttons)
        return text

    async def slice_job(
        self, job_id: int, explicit: Mapping | None = None, *, named_set: str = DEFAULT_NAMED_SET
    ) -> Job:
        """Wake the PC, slice the job, store the result and queue the summary (or the failure).

        ``explicit`` is what the owner asked for (ruling R6). Raises ``KeyError`` (unknown
        job), ``ValueError`` (settings outside the whitelist) or ``JobBusy`` before anything
        changes. Every later failure ends the job ``slice_failed`` and tells the owner.
        """
        job, plan = self._claim(job_id, explicit, named_set)
        await self._slice_claimed(job, plan)
        return self._job(job.id)

    def start_slice(
        self, job_id: int, explicit: Mapping | None = None, *, named_set: str = DEFAULT_NAMED_SET
    ) -> Job:
        """``slice_job`` in the background: the job is claimed before this returns."""
        job, plan = self._claim(job_id, explicit, named_set)
        task = asyncio.create_task(self._slice_claimed(job, plan), name=f"bambu-slice-{job.id}")
        self._slice_tasks.add(task)
        task.add_done_callback(self._slice_tasks.discard)
        return job

    def _job(self, job_id: int) -> Job:
        job = self._store.job(job_id)
        if job is None:
            raise KeyError(f"no job {job_id}")
        return job

    def _claim(self, job_id: int, explicit: Mapping | None, named_set: str) -> tuple[Job, SlicePlan]:
        job = self._job(job_id)
        plan = self._plan(job, explicit, named_set)
        claimed = self._store.transition_job(
            job.id,
            CLAIMABLE,
            status="slicing",
            settings_json=json.dumps(plan.settings),
            result_json=None,
            sliced_file=None,
            error=None,
        )
        if not claimed:
            raise JobBusy(f"job {job.id} is being sliced already")
        return job, plan

    def _plan(self, job: Job, explicit: Mapping | None, named_set: str) -> SlicePlan:
        manifest = parse_manifest(json.loads(job.manifest_json))  # checked at intake
        nozzle = f"{manifest.nozzle_mm:g}"
        history = self._store.jobs(JOB_HISTORY)
        settings = resolve_settings(
            explicit or {},
            last_settings(history, job.name),
            job_hints(manifest),
            named_set,
            checks=manifest.checks,
            nozzle=nozzle,
        )
        wanted, _reason = job_material(manifest)
        trays = self._state.trays
        spools: dict[tuple[int, int], Spool] = {}
        for tray in trays:
            spool = self._store.slot_spool(tray.ams, tray.slot)
            if spool is not None:
                spools[(tray.ams, tray.slot)] = spool
        choice = pick_slot(trays, spools, wanted, _last_color(history, job.name))
        where = (choice.ams, choice.slot)
        spool = spools.get(where) if choice.ams is not None else None
        tray = next((t for t in trays if (t.ams, t.slot) == where), None)
        color = choice.color_name or ((tray.color_rgba or "")[:6] if tray else "") or None
        return SlicePlan(
            manifest=manifest,
            settings=settings,
            choice=choice,
            spool=spool,
            material=self._canonical(spool.material if spool else (choice.tray_type or wanted)),
            color=color,
            plate_type=str(settings.get("curr_bed_type") or self._default_plate_type()),
            nozzle=nozzle,
            named_set=named_set,
            filament=(spool.filament_preset or None) if spool else None,
        )

    def _canonical(self, material: str) -> str:
        """The material as the power table spells it (``print_kwh`` is case-sensitive)."""
        for key in self._settings.power_w:
            if key.casefold() == material.casefold():
                return key
        return material

    def _default_plate_type(self) -> str:
        for plate in self._store.plates():
            if plate.is_default:
                if plate.bed_type in PLATE_TYPES:
                    return plate.bed_type
                log.warning("default plate %d has an unknown bed type, using %s", plate.id, DEFAULT_PLATE_TYPE)
        return DEFAULT_PLATE_TYPE

    async def _slice_claimed(self, job: Job, plan: SlicePlan) -> None:
        try:
            await self._slice(job, plan)
        except _SliceFailed as exc:
            log.warning("job %d not sliced: %s", job.id, exc.error)
            self._fail_job(job, exc.error, exc.text)
        except asyncio.CancelledError:
            self._fail_job(job, "interrupted by a service stop", "прервано остановкой сервиса")
            raise
        except Exception:
            log.exception("slicing job %d failed", job.id)
            self._fail_job(job, "internal error in the service", "внутренняя ошибка сервиса")

    async def _slice(self, job: Job, plan: SlicePlan) -> None:
        slicer = self._slicer
        if slicer is None:
            raise _SliceFailed(
                "slice-agent is not configured",
                f"слайсер не настроен, нужны {md_literal('slice_agent_url')} и {md_literal('SLICE_AGENT_TOKEN')}",
            )
        data = await asyncio.to_thread(self._stored_file, job)
        problem = _preset_problem(plan)
        if problem is not None:
            raise _SliceFailed(*problem)
        try:
            awake = await (self._waker.wake_and_wait(WAKE_BUDGET_S) if self._waker else slicer.health())
        except WakeError as exc:
            raise _SliceFailed(str(exc), _WAKE_TEXTS.get(exc.kind, "ПК не удалось разбудить")) from None
        if not awake:
            raise _SliceFailed(
                "the PC did not wake within 2 minutes", "ПК не проснулся за 2 минуты, нарезка не запускалась"
            )
        request = SliceRequest(
            job_id=job.id,
            name=slug(job.name),
            kit=plan.manifest.kind == "kit",
            plates=tuple(p.index for p in plan.manifest.plates) if plan.manifest.kind == "kit" else (1,),
            nozzle=plan.nozzle,
            process_set=PROCESS_SET,
            filament=plan.filament,
            plate_type=plan.plate_type,
            overrides=plan.settings,
        )
        log.info("job %d: slicing as %s", job.id, request.out_file)
        outcome = await slicer.slice(request, data)
        if outcome.status == "busy":
            log.info("job %d: slice-agent busy, one retry in %.0f s", job.id, BUSY_RETRY_S)
            await self._sleep(BUSY_RETRY_S)
            outcome = await slicer.slice(request, data)
        if outcome.status == "busy":
            raise _SliceFailed(
                "slice-agent is busy with another slice", "слайсер на ПК занят другой нарезкой, попробуй позже"
            )
        if outcome.status == "unreachable":
            raise _SliceFailed(f"slice-agent did not answer ({outcome.detail})", "слайсер на ПК не ответил")
        if outcome.status != "ok" or not outcome.file:
            detail = outcome.detail or "slice failed"
            raise _SliceFailed(detail, md_literal(detail))
        await self._finish(job, plan, outcome)

    def _stored_file(self, job: Job) -> bytes:
        """The job's 3MF, checked again: the intake does not fsync before the rename."""
        try:
            manifest = jobs_module.load_job_manifest(self._settings, job)
            data = (jobs_module.job_dir(self._settings, job.id) / manifest.file).read_bytes()
            if manifest.file_sha256 != job.file_sha256:
                raise ManifestError("the stored manifest changed")
            verify_file(manifest, data)
        except (OSError, ManifestError):
            log.warning("job %d: stored file does not match its manifest", job.id)
            raise _SliceFailed(
                "stored file is damaged, upload again", "файл задания повреждён, загрузи его заново"
            ) from None
        return data

    async def _finish(self, job: Job, plan: SlicePlan, outcome: SliceOutcome) -> None:
        assert self._slicer is not None and outcome.file
        now = self._clock()
        tariff = self._store.tariff_on(now.astimezone(self._tz).date())
        plates = [dict(p) for p in outcome.plates]
        costs = [
            estimate_cost(
                p["minutes"], sum(p["grams"].values()), plan.spool, plan.material, tariff, self._settings, now
            )
            for p in plates
        ]
        first = min(p["index"] for p in plates)
        try:
            png = await self._slicer.fetch_png(outcome.file, first)
        except Exception:
            log.exception("job %d: plate picture failed, sending the text alone", job.id)
            png = None
        text = render_sliced(
            job, plates, costs, settings=plan.settings, slot_choice=plan.choice, plate_type=plan.plate_type
        )
        choice = plan.choice
        result = {
            "plates": plates,
            "costs": [
                {
                    "index": p["index"],
                    "filament_uah": c.filament_uah,
                    "power_uah": c.power_uah,
                    "kwh": c.kwh,
                    "total_uah": c.total_uah,
                }
                for p, c in zip(plates, costs, strict=True)
            ],
            "slot": {
                "ams": choice.ams,
                "slot": choice.slot,
                "spool_id": choice.spool_id,
                "tray_type": choice.tray_type,
                "color": plan.color,
            },
            "material": plan.material,
            "plate_type": plan.plate_type,
            "nozzle": plan.nozzle,
            "process_set": PROCESS_SET,
            "named_set": plan.named_set,
            "filament": plan.filament,
            "slicer_version": outcome.slicer_version,
            "sliced_at": now.astimezone(UTC).isoformat(),
        }
        key = _job_key(job.id, "sliced")
        # A photo caption holds 1024 characters: a longer summary follows its picture.
        split = png is not None and _caption_len(text) > CAPTION_MAX
        picture_key = _job_key(job.id, "picture") if split else key
        folder = self._attachments_dir(notify_dedupe(picture_key)) if png else None
        try:
            if folder is not None and png is not None:
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"plate-{first}.png").write_bytes(png)
            with self._store.transaction():
                done = self._store.transition_job(
                    job.id,
                    ("slicing",),
                    status="sliced",
                    settings_json=json.dumps(plan.settings),
                    result_json=json.dumps(result, ensure_ascii=False),
                    sliced_file=outcome.file,
                    error=None,
                )
                if not done:
                    raise RuntimeError(f"job {job.id} left the slicing status")
                if split:
                    caption = f"{job_name(job.name)}: пластина {first}"
                    self._enqueue(SLICED_KIND, caption, picture_key)
                    self._enqueue(SLICED_TEXT_KIND, text, key)
                else:
                    self._enqueue(SLICED_KIND, text, key)
        except BaseException:
            if folder is not None:
                shutil.rmtree(folder, ignore_errors=True)
            raise
        log.info("job %d sliced: %s", job.id, outcome.file)

    def _fail_job(self, job: Job, error: str, text: str) -> None:
        """``slicing`` -> ``slice_failed`` with ``error``, and tell the owner ``text``."""
        try:
            with self._store.transaction():
                if self._store.transition_job(job.id, ("slicing",), status="slice_failed", error=error):
                    self._enqueue(
                        "job_failed",
                        f"❌ Нарезка «{job_name(job.name)}» не удалась: {text}",
                        _job_key(job.id, "failed"),
                    )
        except Exception:
            log.exception("marking job %d failed did not work", job.id)

    def _fail_interrupted_jobs(self) -> None:
        """Jobs left ``slicing`` by a stop that could not mark them: no slice runs for them now."""
        for job in self._store.jobs_with_status("slicing"):
            self._fail_job(job, "interrupted by a service restart", "прервано перезапуском сервиса")

    def _on_job_press(self, press: Press) -> str | None:
        """``job:<id>:slice`` starts the slice once, ``job:<id>:change`` asks for the changes."""
        _kind, _sep, rest = press.button_id.partition(":")
        raw_id, sep, action = rest.partition(":")
        if not sep or action not in ("slice", "change"):
            return None
        try:
            job_id = int(raw_id)
        except ValueError:
            return None
        job = self._store.job(job_id)
        if job is None:
            log.warning("job press for unknown job %d ignored", job_id)
            return None
        if action == "change":
            return CHANGE_TEXT
        name = job_name(job.name)
        if job.status == "slicing":
            return f"Уже режу «{name}»"
        try:
            self.start_slice(job.id)
        except JobBusy:
            return f"Уже режу «{name}»"
        except ValueError as exc:
            log.warning("job %d: slice press refused: %s", job.id, exc)
            return f"Не получилось начать нарезку «{name}»: {md_literal(str(exc))}"
        except Exception:
            log.exception("job %d: slice press failed", job.id)
            return f"Не получилось начать нарезку «{name}»: внутренняя ошибка сервиса"
        return f"✂️ Режу «{name}», пришлю результат"


def _job_key(job_id: int, what: str) -> str:
    """A fresh dedupe key: every proposal, summary and failure is its own message."""
    return f"job|{job_id}|{what}|{uuid.uuid4().hex}"


def _last_color(history: list[Job], name: str) -> str | None:
    """The slot colour of the newest sliced job of the part (``history`` newest first)."""
    for job in history:
        if job.name != name or job.status != "sliced" or not job.result_json:
            continue
        try:
            color = json.loads(job.result_json).get("slot", {}).get("color")
        except (ValueError, RecursionError, AttributeError):
            continue
        if isinstance(color, str) and color:
            return color
    return None


def _preset_problem(plan: SlicePlan) -> tuple[str, str] | None:
    """(English error, Russian reason) when there is no filament preset to slice with."""
    if plan.filament is not None:
        return None
    choice = plan.choice
    if choice.ams is None or choice.slot is None:
        return "no loaded slot with a filament preset", "нет заправленного слота с пресетом филамента"
    n = slot_number(choice.ams, choice.slot)
    spool = plan.spool
    if spool is None:
        return (
            f"slot {n} has no spool mapped, so there is no filament preset",
            f"слот {n} не привязан к катушке, нет пресета филамента",
        )
    label = md_literal(f"{spool.brand} {spool.material} {spool.color_name}")
    return (
        f"spool {spool.id} in slot {n} has no filament preset",
        f"у катушки {spool.id} ({label}) в слоте {n} не указан пресет филамента",
    )


def _preset_warning(plan: SlicePlan) -> str | None:
    """The proposal's line for a slice that would stop before it starts."""
    problem = _preset_problem(plan)
    if problem is None:
        return None
    reason = problem[1]
    return f"⚠️ {reason[0].upper()}{reason[1:]}: нарезка не начнётся"


def _caption_len(text: str) -> int:
    """Length as Telegram counts it (UTF-16 code units), before Markdown escapes are removed."""
    return len(text.encode("utf-16-le")) // 2


# -- entry point ---------------------------------------------------------------


def build_notifier(secrets: Secrets, settings: Settings) -> Notifier:
    if settings.notify_url and secrets.notify_token:
        return ServiceNotifyClient(settings.notify_url, secrets.notify_token)
    log.warning("notify_url or NOTIFY_TOKEN not set, notifications go to the log only")
    return LogNotifier()


def build_slicer(
    secrets: Secrets, settings: Settings, http: httpx.AsyncClient
) -> tuple[SliceClient | None, GatewayWaker | None]:
    """slice-agent client and the PC waker. Without a gateway the PC must be awake already."""
    if not settings.slice_agent_url or not secrets.slice_agent_token:
        log.warning("slice_agent_url or SLICE_AGENT_TOKEN not set, jobs cannot be sliced")
        return None, None
    client = SliceClient(settings.slice_agent_url, secrets.slice_agent_token, http)
    if not settings.gateway_url or not secrets.gateway_key:
        log.warning("gateway_url or GATEWAY_KEY not set, the PC is not woken for slicing")
    return client, GatewayWaker(settings.gateway_url, secrets.gateway_key, http, ready=client.health)


def extra_coroutines(
    service: Service, secrets: Secrets, settings: Settings, store: Store
) -> list[Coroutine[Any, Any, None]]:
    """Coroutines that share the service's event loop: the MCP server."""
    return [mcp_server.serve(service, store, secrets, settings)]


async def _amain(secrets: Secrets, settings: Settings) -> None:
    store = Store(Path(settings.data_dir) / DB_NAME)
    store.migrate()
    notifier = build_notifier(secrets, settings)
    http = httpx.AsyncClient(trust_env=False)  # LAN only: no proxies from the environment
    slicer, waker = build_slicer(secrets, settings, http)
    service = Service(
        secrets,
        settings,
        store,
        notifier,
        lambda on_report, on_status: PrinterLink(secrets, settings, on_report, on_status),
        functools.partial(camera_module.snapshot, secrets, settings),
        slicer=slicer,
        waker=waker,
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
        await http.aclose()
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
