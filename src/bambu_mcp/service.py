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
import math
import os
import re
import secrets as _secrets
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
from bambu_mcp.jobs import JobBusy, SliceStart
from bambu_mcp.journal import NO_SPOOL, TERMINAL_KINDS, Journal, Slot
from bambu_mcp.logthrottle import Throttle
from bambu_mcp.manifest import Manifest, ManifestError, parse_manifest, verify_file
from bambu_mcp.messages import (
    fit_question,
    job_name,
    md_literal,
    plate_buttons,
    plate_question,
    post_print_buttons,
    print_buttons,
    render_event,
    render_print_refused,
    render_print_started,
    render_proposal,
    render_sliced,
    render_start_confirmation,
    render_start_unconfirmed,
    slot_buttons,
    spool_buttons,
    spool_question,
    start_buttons,
    stop_ask_buttons,
)
from bambu_mcp.names import slug
from bambu_mcp.notify import Button, LogNotifier, Notifier, Press, SendError, ServiceNotifyClient
from bambu_mcp.printer import ftps
from bambu_mcp.printer.ftps import UploadError
from bambu_mcp.printer.hms import HmsTexts
from bambu_mcp.printer.link import GCODE_3MF_SUFFIX, LinkStatus, PrinterLink, PrinterNotConnected
from bambu_mcp.printer.state import PrinterState, apply
from bambu_mcp.printgate import PrintFileError, PrintFileInfo, gate_hash, preflight, print_file_info
from bambu_mcp.proposal import (
    SlotChoice,
    SlotUnusable,
    UsableSlot,
    estimate_cost,
    job_hints,
    job_material,
    last_settings,
    pick_slot,
    resolve_settings,
    slot_number,
    slot_position,
    usable_slots,
)
from bambu_mcp.redact import install_redaction
from bambu_mcp.slicer import (
    WAKE_BUDGET_S,
    FetchError,
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
# tray_now: 0..15 = the owner's slot number - 1, 254 = external spool, 255 = nothing loaded.
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
CHANGE_TEXT = "Скажи ассистенту, что поменять: слот, стенки, заполнение, поддержки, пластину"
SLOT_UNUSABLE_TEXT = "Слот {n} пустой или без катушки с пресетом, выбери другой"
SLICED_KIND = "job_sliced"
SLICED_TEXT_KIND = "job_sliced_text"  # the summary sent after its picture when too long for a caption
CAPTION_MAX = 1024  # Telegram photo caption limit, in UTF-16 code units
_WAKE_TEXTS = {
    "refused": "шлюз отклонил запрос пробуждения ПК",
    "unreachable": "шлюз не ответил на запрос пробуждения ПК",
    "not_configured": "ПК выключен, а шлюз для пробуждения не настроен",
}
ATTACHMENTS_DIR = "outbox-files"  # data_dir/<this>/<sha256 of the dedupe key>/<file>
# Printing (phase 3).
PRINT_CACHE_DIR = "print-cache"  # data_dir/<this>/<sliced file>, fetched from the PC
PRINT_CACHE_KEEP = 3  # sliced files kept, newest first
START_WATCH_S = 30.0  # after project_file: our task_id must show up PREPARE or RUNNING
REPORT_WAIT_S = 5.0  # a fresh pushall before the checks
START_REPORTED = frozenset({"PREPARE", "RUNNING"})
_CONFIRM_ID_RE = re.compile(r"[0-9a-f]{32}", re.ASCII)
_HASH_CHUNK = 1 << 16
UPLOADING_TEXT = "⏳ Загружаю на принтер…"
STARTING_TEXT = "⏳ Уже запускаю печать"
CANCELLED_TEXT = "Печать отменена"
_SPENT_TEXTS = {
    "used": "Эта кнопка уже нажата, второй раз печать не запускается",
    "expired": "⌛ Подтверждение истекло, подготовь печать заново",
    "cancelled": "Печать отменена, подготовь её заново, если нужно",
}
NOT_SLICED_TEXT = "Задание «{name}» не нарезано, сначала нарежь его"
JOB_CHANGED_TEXT = "Задание изменилось после подтверждения, подготовь печать заново"
FILE_GONE_TEXT = "Нарезанного файла уже нет на сервере, подготовь печать заново"
FILE_CHANGED_TEXT = "Нарезанный файл изменился после подтверждения, подготовь печать заново"
NOT_CONNECTED_TEXT = "Принтер не на связи, команда не отправлена"
INTERNAL_TEXT = "внутренняя ошибка сервиса"
TLS_TEXT = (
    "Загрузка на принтер идёт по TLS с проверкой сертификата: в настройках нужны "
    + md_literal('tls_mode = "ca"')
    + " и "
    + md_literal("ca_file")
)
TLS_ERROR = 'printing needs tls_mode = "ca" and ca_file in the config: the FTPS upload checks the printer certificate'

Camera = Callable[[], Awaitable[bytes | None]]
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]
# ftps.upload(host, serial, access_code, ca_file, local, remote_name): blocking, run in a thread.
Uploader = Callable[[str, str, str, Path, Path, str], None]


class Link(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def request_full_report(self) -> None: ...

    def set_tray(
        self,
        ams: int,
        slot: int,
        tray_info_idx: str,
        tray_type: str,
        color_rgba: str,
        nozzle_temp_min: int,
        nozzle_temp_max: int,
    ) -> dict: ...

    def start_project(
        self, *, file: str, plate: int, md5: str, subtask_name: str, task_id: str, ams: int, slot: int
    ) -> None: ...


class Slicer(Protocol):
    """``slicer.SliceClient``."""

    async def health(self) -> bool: ...

    async def slice(self, job: SliceRequest, file_bytes: bytes) -> SliceOutcome: ...

    async def fetch_png(self, name: str, plate: int) -> bytes | None: ...

    async def fetch_file(self, name: str, dest: Path) -> str: ...


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
    usable: tuple[UsableSlot, ...] = ()  # slots the owner may choose instead


class PrintRefused(Exception):
    """A print was not prepared. ``lines``: Russian, Markdown-escaped, one per reason.

    ``str()`` is for Claude: the English ``detail`` when there is one, else the lines.
    """

    def __init__(self, lines: list[str], detail: str | None = None) -> None:
        self.lines = list(lines)
        super().__init__(detail or "print refused: " + " / ".join(self.lines))


@dataclass(frozen=True)
class PrepareResult:
    """A start confirmation sent to Telegram: only the owner's press on it starts the print."""

    confirm_id: str
    text: str
    warnings: list[str]


@dataclass(frozen=True)
class _PrintTarget:
    """What a start prints, read from the job's slice result and the current slot mapping.

    Built the same way at prepare and at the press, so the gate hash compares like with like.
    """

    job: Job
    plate: int
    file: str  # the sliced file name: on the PC, in the cache, on the USB stick
    expected_sha256: str | None  # recorded at slicing
    choice: SlotChoice
    spool: Spool | None  # mapped to the slot now
    nozzle: str  # "0.4"
    plate_type: str


class _StartRefused(Exception):
    """The press does not start the print. ``reason`` goes into the confirmation's outcome."""

    def __init__(self, reason: str, lines: list[str]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.lines = lines


@dataclass
class _StartWatch:
    """The report the press waits for: our ``task_id``, PREPARE or RUNNING, after the send."""

    task_id: str
    event: asyncio.Event
    announced: bool = False  # the STARTED event message (with the stop button) was queued


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
    return slot_position(tray + 1)


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
        uploader: Uploader = ftps.upload,
        watch_s: float = START_WATCH_S,
        report_wait_s: float = REPORT_WAIT_S,
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
        self._link: Link | None = None  # while run() is running
        self._state_fresh = False  # a report with gcode_state arrived since the last CONNECTED
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
        # Printing (phase 3).
        self._uploader = uploader
        self._watch_s = watch_s
        self._report_wait_s = report_wait_s
        self._start_lock = asyncio.Lock()  # one start at a time
        self._press_tasks: set[asyncio.Task] = set()  # starts and prepares run off the press loop
        self._preparing: set[int] = set()  # job ids with a prepare from a button in flight
        self._report_waiters: set[asyncio.Event] = set()
        self._watch: _StartWatch | None = None
        self._own_tasks: set[str] = set()  # task ids this process sent: their start gets the stop button
        self._last_task_id = 0

    # -- public ----------------------------------------------------------------

    @property
    def state(self) -> PrinterState:
        return self._state

    @property
    def link_status(self) -> LinkStatus:
        return self._link_status

    @property
    def state_fresh(self) -> bool:
        """True once a report with ``gcode_state`` arrived since the link last (re)connected."""
        return self._state_fresh and self._link_status is LinkStatus.CONNECTED

    def _running_link(self) -> Link:
        if self._link is None:
            raise PrinterNotConnected("printer link is not running, nothing was sent")
        return self._link

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
    ) -> dict:
        """The one printer write (``load_spool``): see ``PrinterLink.set_tray``. Returns the payload."""
        return self._running_link().set_tray(
            ams=ams,
            slot=slot,
            tray_info_idx=tray_info_idx,
            tray_type=tray_type,
            color_rgba=color_rgba,
            nozzle_temp_min=nozzle_temp_min,
            nozzle_temp_max=nozzle_temp_max,
        )

    def request_full_report(self) -> None:
        self._running_link().request_full_report()

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
        self._link = link
        try:
            await self._stop_requested.wait()
        finally:
            self._link = None
            await asyncio.to_thread(link.stop)  # joins paho's network thread
            tasks += self._slice_tasks  # a cancelled slice marks its job failed
            tasks += self._press_tasks
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
        self._state_fresh = False  # CONNECTED included: wait for the printer's next report
        if status is not LinkStatus.CONNECTED:
            # The printer may change unseen while the link is down. The event detector
            # compares an UNKNOWN state with the last known one, so this announces nothing.
            # Pre-flight trusts the USB stick only from a report since the next CONNECTED, so
            # it is forgotten too. The task id stays: the detector compares job identities,
            # and a first report without it would announce the running job again.
            forget = {"gcode_state": "UNKNOWN", "raw_unknown_state": None, "sdcard": None}
            if any(getattr(self._state, key) != value for key, value in forget.items()):
                self._state = replace(self._state, **forget)
        if status is LinkStatus.AUTH_FAILED and not self._auth_alerted:
            self._auth_alerted = True
            at = self._clock().astimezone(UTC)
            self._enqueue("auth", AUTH_FAILED_TEXT, f"auth-failed|{at:%Y%m%dT%H%M%S}")

    async def _handle_press(self, press: Press) -> None:
        try:
            if press.button_id.startswith("start:"):
                await self._on_start_press(press)
                return
            if _print_press(press.button_id) is not None:
                await self._on_print_press(press)
                return
            if _slot_press(press.button_id) is not None:
                await self._on_slot_press(press)
                return
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
        body = report.get("print")
        state_report = isinstance(body, dict) and "gcode_state" in body
        if state_report:
            self._state_fresh = True
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
        # After the events: a STARTED message for our task is queued before the press goes on.
        if state_report:
            for waiter in self._report_waiters:
                waiter.set()
        watch = self._watch
        if watch is not None and new.task_id == watch.task_id and new.gcode_state in START_REPORTED:
            watch.event.set()

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
        buttons = None
        task_id = event.state.task_id
        if event.kind is EventKind.STARTED and task_id and task_id in self._own_tasks:
            buttons = stop_ask_buttons(task_id)  # a print started from our button
        self._enqueue(
            str(event.kind), text, event.dedupe_key, buttons=buttons, snapshot=event.kind in SNAPSHOT_KINDS
        )
        if buttons is not None and self._watch is not None and self._watch.task_id == task_id:
            self._watch.announced = True
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

    async def propose(self, job_id: int, slot: int | None = None) -> str:
        """Queue the job's proposal with the slice, change and slot buttons. Returns its text.

        ``slot`` (1..16) is the owner's AMS slot: stored with the job, it replaces the one
        stored before. Raises ``KeyError`` for an unknown job, ``SlotUnusable`` for a slot
        that cannot be printed from, ``ValueError`` when the settings do not resolve.
        """
        job = self._job(job_id)
        if slot is not None and job.status == "slicing":
            raise JobBusy(job.id)  # the slot of a running slice never changes under it
        plan = self._plan(job, None, DEFAULT_NAMED_SET, slot)
        text, buttons = _proposal(job, plan)
        with self._store.transaction():
            if slot is not None:
                self._store.update_job(job.id, slot=slot)
            self._enqueue("job_proposal", text, _job_key(job.id, "proposal"), buttons=buttons)
        return text

    async def slice_job(
        self,
        job_id: int,
        explicit: Mapping | None = None,
        *,
        named_set: str = DEFAULT_NAMED_SET,
        slot: int | None = None,
    ) -> Job:
        """Wake the PC, slice the job, store the result and queue the summary (or the failure).

        ``explicit`` is what the owner asked for (ruling R6). ``slot`` is the owner's AMS
        slot, stored with the job (None: the stored one, else the material match). Raises
        ``KeyError`` (unknown job), ``ValueError`` (settings outside the whitelist, or
        ``SlotUnusable``) or ``JobBusy`` before anything changes. Every later failure ends
        the job ``slice_failed`` and tells the owner.
        """
        job, plan = self._claim(job_id, explicit, named_set, slot)
        await self._slice_claimed(job, plan)
        return self._job(job.id)

    def start_slice(
        self,
        job_id: int,
        explicit: Mapping | None = None,
        *,
        named_set: str = DEFAULT_NAMED_SET,
        slot: int | None = None,
    ) -> SliceStart:
        """``slice_job`` in the background: the job is claimed before this returns.

        The slot warning is returned at once, so a caller that skipped the proposal sees it.
        """
        job, plan = self._claim(job_id, explicit, named_set, slot)
        task = asyncio.create_task(self._slice_claimed(job, plan), name=f"bambu-slice-{job.id}")
        self._slice_tasks.add(task)
        task.add_done_callback(self._slice_tasks.discard)
        return SliceStart(job, plan.choice.warning)

    def _job(self, job_id: int) -> Job:
        job = self._store.job(job_id)
        if job is None:
            raise KeyError(f"no job {job_id}")
        return job

    def _claim(
        self, job_id: int, explicit: Mapping | None, named_set: str, slot: int | None = None
    ) -> tuple[Job, SlicePlan]:
        job = self._job(job_id)
        plan = self._plan(job, explicit, named_set, slot)
        chosen = {"slot": slot} if slot is not None else {}
        claimed = self._store.transition_job(
            job.id,
            CLAIMABLE,
            status="slicing",
            settings_json=json.dumps(plan.settings),
            result_json=None,
            sliced_file=None,
            error=None,
            **chosen,
        )
        if not claimed:
            raise JobBusy(f"job {job.id} is being sliced already")
        return job, plan

    def _plan(
        self, job: Job, explicit: Mapping | None, named_set: str, slot: int | None = None
    ) -> SlicePlan:
        """``slot`` (1..16) wins over the job's stored slot, which wins over the material match."""
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
        number = slot if slot is not None else job.slot
        forced = slot_position(number) if number is not None else None
        choice = pick_slot(trays, spools, wanted, _last_color(history, job.name), forced=forced)
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
            usable=tuple(usable_slots(trays, spools)),
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
                "warning": choice.warning,
            },
            "material": plan.material,
            "plate_type": plan.plate_type,
            "nozzle": plan.nozzle,
            "process_set": PROCESS_SET,
            "named_set": plan.named_set,
            "filament": plan.filament,
            "slicer_version": outcome.slicer_version,
            "file_sha256": outcome.sha256,  # None from an older agent: the print asks for a re-slice
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
                    self._enqueue(SLICED_TEXT_KIND, text, key, buttons=print_buttons(job.id))
                else:
                    self._enqueue(SLICED_KIND, text, key, buttons=print_buttons(job.id))
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

    async def _on_slot_press(self, press: Press) -> None:
        """``job:<id>:slot:<n>``: store the slot and edit the proposal in place with it chosen.

        A slot that cannot be printed from is answered with a new message and nothing
        changes. While the job is slicing the press is answered like the slice button.
        """
        parsed = _slot_press(press.button_id)
        assert parsed is not None
        job_id, number = parsed
        job = self._store.job(job_id)
        if job is None:
            log.warning("slot press for unknown job %d ignored", job_id)
            return
        name = job_name(job.name)
        if job.status == "slicing":
            await self._notifier.edit(press.message_id, f"Уже режу «{name}»")
            return
        try:
            plan = self._plan(job, None, DEFAULT_NAMED_SET, number)
        except SlotUnusable:
            self._enqueue("job_slot", SLOT_UNUSABLE_TEXT.format(n=number), _job_key(job.id, "slot"))
            return
        except ValueError as exc:
            log.warning("job %d: slot press refused: %s", job.id, exc)
            text = f"Не получилось выбрать слот для «{name}»: {md_literal(str(exc))}"
            self._enqueue("job_slot", text, _job_key(job.id, "slot"))
            return
        # A slice claimed in the meantime keeps its own plan: the slot is not changed under it.
        if not self._store.transition_job(job.id, CLAIMABLE, slot=number):
            await self._notifier.edit(press.message_id, f"Уже режу «{name}»")
            return
        text, buttons = _proposal(self._job(job.id), plan)
        if not await self._notifier.edit(press.message_id, text, buttons):
            log.warning("job %d: editing the proposal failed, sending it again", job.id)
            self._enqueue("job_proposal", text, _job_key(job.id, "proposal"), buttons=buttons)

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

    # -- printing: prepare, confirm, start (phase 3) ------------------------------

    async def prepare_print(self, job_id: int, plate: int = 1) -> PrepareResult:
        """Check a sliced job against the printer and send the start confirmation to Telegram.

        Wakes the PC, fetches the sliced file into the print cache, runs the pre-flight on a
        fresh report, then stores a single-use confirmation bound to the gate hash and queues
        the message with the start and cancel buttons. Starts nothing: only the owner's press
        on that message does. Raises ``KeyError`` (unknown job), ``ValueError`` (no such
        plate) or ``PrintRefused`` (anything that blocks), and then nothing is stored or sent.
        """
        target = self._print_target(self._job(job_id), plate)
        problem = self._ftps_problem()
        if problem is not None:
            raise problem
        slicer = self._slicer
        if slicer is None:
            raise PrintRefused(
                [f"слайсер не настроен, нужны {md_literal('slice_agent_url')} и {md_literal('SLICE_AGENT_TOKEN')}"],
                "slice-agent is not configured, the sliced file cannot be fetched",
            )
        await self._wake_pc(slicer)
        cache = Path(self._settings.data_dir) / PRINT_CACHE_DIR
        path = cache / target.file
        try:
            cache.mkdir(parents=True, exist_ok=True)
            file_sha = await slicer.fetch_file(target.file, path)
        except FetchError as exc:
            raise PrintRefused([f"Не удалось забрать нарезанный файл с ПК: {md_literal(str(exc))}"], str(exc)) from None
        except OSError as exc:
            log.warning("print cache not writable: %s", type(exc).__name__)
            raise PrintRefused(["Не удалось сохранить нарезанный файл на сервере"], "the print cache is not writable") from None
        self._prune_print_cache(path)
        info = await self._file_info(path, plate)
        await self._refresh_state()
        # The awaits above took time: the job and its slot mapping are read again.
        target = self._print_target(self._job(job_id), plate)
        check = preflight(
            self._state,
            target.job,
            plate,
            target.choice,
            target.spool,
            info,
            target.expected_sha256,
            file_sha,
            target.nozzle,
            fresh=self.state_fresh,
        )
        if check.blocking:
            raise PrintRefused(check.blocking)
        text = render_start_confirmation(
            target.job.name,
            plate=plate if target.job.kind == "kit" else None,
            slot_choice=self._label_choice(target),
            minutes=info.minutes,
            grams=info.grams,
            plate_type=target.plate_type,
            warnings=check.warnings,
        )
        with self._store.transaction():
            confirmation = self._store.add_confirmation(
                None,
                "start",
                job_id=target.job.id,
                plate=plate,
                gate_hash=self._gate(target, file_sha),
                payload_json=json.dumps({"file": target.file, "plate": plate}),
                now=self._clock().timestamp(),
            )
            self._enqueue(
                "print_confirm", text, f"start|{confirmation.id}", buttons=start_buttons(confirmation.id)
            )
        log.info("job %d plate %d: start confirmation sent", target.job.id, plate)
        return PrepareResult(confirmation.id, text, list(check.warnings))

    def _print_target(self, job: Job, plate: int) -> _PrintTarget:
        if job.status != "sliced" or not job.sliced_file:
            raise PrintRefused(
                [NOT_SLICED_TEXT.format(name=job_name(job.name))], f"job {job.id} is not sliced (status {job.status})"
            )
        try:
            result = json.loads(job.result_json or "{}")
        except (ValueError, RecursionError):
            result = {}
        if not isinstance(result, dict):
            result = {}
        raw_plates = result.get("plates")
        plates = sorted(
            p["index"] for p in (raw_plates if isinstance(raw_plates, list) else [])
            if isinstance(p, dict) and type(p.get("index")) is int
        )
        if type(plate) is not int or plate not in plates or (job.kind != "kit" and plate != 1):
            raise ValueError(f"job {job.id} has no plate {plate!r}, its plates are {plates}")
        slot = result.get("slot") if isinstance(result.get("slot"), dict) else {}
        ams, number = slot.get("ams"), slot.get("slot")
        if type(ams) is not int or type(number) is not int:
            ams = number = None
        spool = self._store.slot_spool(ams, number) if ams is not None and number is not None else None
        choice = SlotChoice(
            ams,
            number,
            spool.id if spool else None,
            None,
            color_name=spool.color_name if spool else None,
            remaining_g=spool.remaining_g if spool else None,
        )
        expected = result.get("file_sha256")
        return _PrintTarget(
            job=job,
            plate=plate,
            file=job.sliced_file,
            expected_sha256=expected if isinstance(expected, str) and expected else None,
            choice=choice,
            spool=spool,
            nozzle=_nozzle_text(result.get("nozzle")),
            plate_type=str(result.get("plate_type") or ""),
        )

    def _label_choice(self, target: _PrintTarget) -> SlotChoice:
        """The slot as the confirmation names it: number, the tray's material, the spool's colour."""
        choice = target.choice
        tray = next((t for t in self._state.trays if (t.ams, t.slot) == (choice.ams, choice.slot)), None)
        return replace(choice, tray_type=tray.tray_type if tray else None)

    def _gate(self, target: _PrintTarget, file_sha256: str) -> str:
        """The gate hash, from the same normalized sources at prepare and at the press."""
        assert target.choice.ams is not None and target.choice.slot is not None  # pre-flight passed
        return gate_hash(
            file_sha256=file_sha256,
            plate=int(target.plate),
            ams=int(target.choice.ams),
            slot=int(target.choice.slot),
            serial=str(self._secrets.serial),
            nozzle=target.nozzle,
            plate_type=target.plate_type,
        )

    def _ftps_problem(self) -> PrintRefused | None:
        """``printer.ftps.upload`` checks the printer by its CA: other TLS modes cannot print."""
        if self._settings.tls_mode != "ca" or self._settings.ca_file is None:
            return PrintRefused([TLS_TEXT], TLS_ERROR)
        return None

    async def _wake_pc(self, slicer: Slicer) -> None:
        try:
            awake = await (self._waker.wake_and_wait(WAKE_BUDGET_S) if self._waker else slicer.health())
        except WakeError as exc:
            raise PrintRefused([_WAKE_TEXTS.get(exc.kind, "ПК не удалось разбудить")], str(exc)) from None
        if not awake:
            raise PrintRefused(
                ["ПК не проснулся за 2 минуты, нарезанный файл не забран"], "the PC did not wake within 2 minutes"
            )

    async def _file_info(self, path: Path, plate: int) -> PrintFileInfo:
        try:
            return await asyncio.to_thread(print_file_info, path, plate)
        except PrintFileError as exc:
            raise PrintRefused(
                [f"Нарезанный файл не годится для печати ({md_literal(str(exc))}), нарежь заново"], str(exc)
            ) from None

    def _prune_print_cache(self, keep: Path) -> None:
        """Keep ``keep`` and the newest others, ``PRINT_CACHE_KEEP`` sliced files in all.

        Only ``*.gcode.3mf`` count: a fetch's ``.part`` and a press's upload copy start with a dot.
        """
        try:
            files = [
                p
                for p in keep.parent.iterdir()
                if p.name.endswith(GCODE_3MF_SUFFIX) and not p.name.startswith(".") and p != keep and p.is_file()
            ]
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            log.exception("listing the print cache failed")
            return
        for old in files[PRINT_CACHE_KEEP - 1 :]:
            log.info("removing %s from the print cache", old.name)
            with contextlib.suppress(OSError):
                old.unlink()

    async def _refresh_state(self) -> None:
        """Ask the printer for a full report and wait for it (up to ``report_wait_s``)."""
        link = self._link
        if link is None or self._link_status is not LinkStatus.CONNECTED:
            return
        waiter = asyncio.Event()
        self._report_waiters.add(waiter)
        try:
            try:
                link.request_full_report()
            except Exception:
                log.exception("asking the printer for a full report failed")
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(waiter.wait(), self._report_wait_s)
        finally:
            self._report_waiters.discard(waiter)

    async def _on_print_press(self, press: Press) -> None:
        """``job:<id>:print`` prepares plate 1 of a part, or asks a kit's plate first.

        ``job:<id>:print:<n>`` prepares plate ``n``. The sliced summary is never edited: the
        confirmation, or the reason there is none, comes as a new message.
        """
        parsed = _print_press(press.button_id)
        assert parsed is not None
        job_id, plate = parsed
        job = self._store.job(job_id)
        if job is None:
            log.warning("print press for unknown job %d ignored", job_id)
            return
        if plate is None and job.kind == "kit" and job.status == "sliced":
            try:
                plates = sorted(
                    p["index"] for p in json.loads(job.result_json or "{}").get("plates", [])
                    if isinstance(p, dict) and type(p.get("index")) is int
                )
            except (ValueError, RecursionError, AttributeError, TypeError):
                plates = []
            if plates:
                self._enqueue(
                    "print_plate", plate_question(job.name), _job_key(job.id, "print-plate"),
                    buttons=plate_buttons(job.id, plates),
                )
                return
        if job.id in self._preparing:
            log.info("job %d: a print is being prepared already, press ignored", job.id)
            return
        if plate is not None:
            await self._notifier.edit(press.message_id, f"Готовлю печать «{job_name(job.name)}», пластина {plate}…")
        self._preparing.add(job.id)
        try:
            task = asyncio.create_task(self._prepare_pressed(job, plate or 1), name=f"bambu-prepare-{job.id}")
        except BaseException:
            self._preparing.discard(job.id)
            raise
        self._press_tasks.add(task)
        task.add_done_callback(self._press_tasks.discard)

    async def _prepare_pressed(self, job: Job, plate: int) -> None:
        try:
            await self.prepare_print(job.id, plate)
        except PrintRefused as exc:
            log.info("job %d plate %d: print not prepared: %s", job.id, plate, exc)
            lines = exc.lines
        except (KeyError, ValueError) as exc:
            log.warning("job %d plate %d: print press refused: %s", job.id, plate, exc)
            lines = [md_literal(str(exc))]
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("job %d: preparing the print failed", job.id)
            lines = [INTERNAL_TEXT]
        else:
            return
        finally:
            self._preparing.discard(job.id)
        self._enqueue(
            "print_refused", render_print_refused(job.name, lines, started=False), _job_key(job.id, "print-refused")
        )

    async def _on_start_press(self, press: Press) -> None:
        """``start:<id>:go`` starts the print at most once, ``start:<id>:cancel`` cancels it."""
        parsed = _start_press(press.button_id)
        if parsed is None:
            log.warning("malformed start button ignored")
            return
        confirm_id, action = parsed
        confirmation = self._store.confirmation(confirm_id)
        if confirmation is None or confirmation.kind != "start":
            log.warning("start press for an unknown confirmation ignored")
            return
        if action == "cancel":
            if self._store.cancel_confirmation(confirm_id):
                self._store.set_confirmation_outcome(confirm_id, "cancelled")
                await self._notifier.edit(press.message_id, CANCELLED_TEXT)
            else:
                await self._answer_spent(press, confirm_id)
            return
        if self._start_lock.locked():
            await self._notifier.edit(press.message_id, STARTING_TEXT)
            return
        await self._start_lock.acquire()  # free, so it never waits: no other press gets in
        try:
            task = asyncio.create_task(self._start_pressed(press, confirm_id), name="bambu-start")
        except BaseException:
            self._start_lock.release()
            raise
        self._press_tasks.add(task)
        task.add_done_callback(self._press_tasks.discard)

    async def _answer_spent(self, press: Press, confirm_id: str) -> None:
        confirmation = self._store.confirmation(confirm_id)
        text = _SPENT_TEXTS.get(confirmation.state) if confirmation is not None else None
        if text is not None:
            await self._notifier.edit(press.message_id, text)

    async def _start_pressed(self, press: Press, confirm_id: str) -> None:
        """Holds the start lock (taken by the caller) until the start is decided."""
        try:
            confirmation = self._store.claim_confirmation(confirm_id, self._clock().timestamp())
            if confirmation is None:
                await self._answer_spent(press, confirm_id)
                return
            job = self._store.job(confirmation.job_id) if confirmation.job_id is not None else None
            name = job.name if job is not None else None
            try:
                outcome = await self._start(press, confirmation, job)
            except _StartRefused as exc:
                log.warning("print start refused: %s", exc.reason)
                self._finish_start(confirm_id, f"refused:{exc.reason}", name, exc.lines)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("print start failed")
                self._finish_start(confirm_id, "refused:internal", name, [INTERNAL_TEXT])
            else:
                log.info("print start %s", outcome)
        finally:
            self._start_lock.release()

    def _finish_start(self, confirm_id: str, outcome: str, name: str | None, lines: list[str]) -> None:
        """Record a refused start and tell the owner why. The confirmation stays used."""
        try:
            with self._store.transaction():
                self._store.set_confirmation_outcome(confirm_id, outcome)
                text = render_print_refused(name, lines, started=True)
                self._enqueue("print_refused", text, f"start|{confirm_id}|refused")
        except Exception:
            log.exception("recording a refused print start failed")

    async def _start(self, press: Press, confirmation: Any, job: Job | None) -> str:
        """The claimed press: check again, upload, send ``project_file`` once, watch. Its outcome."""
        try:
            await self._notifier.edit(press.message_id, UPLOADING_TEXT)
        except Exception:
            log.exception("answering the start press failed, starting anyway")
        if job is None:
            raise _StartRefused("job_changed", [JOB_CHANGED_TEXT])
        try:
            target = self._print_target(job, confirmation.plate)
        except (PrintRefused, ValueError):
            raise _StartRefused("job_changed", [JOB_CHANGED_TEXT]) from None
        try:
            payload = json.loads(confirmation.payload_json)
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or payload.get("file") != target.file:
            raise _StartRefused("job_changed", [JOB_CHANGED_TEXT])
        problem = self._ftps_problem()
        if problem is not None:
            raise _StartRefused("config", problem.lines)
        await self._refresh_state()

        cache = Path(self._settings.data_dir) / PRINT_CACHE_DIR
        pinned = await asyncio.to_thread(_pin_file, cache / target.file)
        if pinned is None:
            raise _StartRefused("file_missing", [FILE_GONE_TEXT])
        try:
            return await self._start_pinned(confirmation, target, pinned)
        finally:
            with contextlib.suppress(OSError):
                pinned.unlink()

    async def _start_pinned(self, confirmation: Any, target: _PrintTarget, pinned: Path) -> str:
        """``pinned``: this press's own link to the cached file, so a later fetch cannot swap it."""
        file_sha = await asyncio.to_thread(_sha256_file, pinned)
        if file_sha != target.expected_sha256:
            raise _StartRefused("file_changed", [FILE_CHANGED_TEXT])
        try:
            info = await self._file_info(pinned, target.plate)
        except PrintRefused as exc:
            raise _StartRefused("file", exc.lines) from None
        self._preflight_or_refuse(target, info, file_sha)
        if self._gate(target, file_sha) != confirmation.gate_hash:
            raise _StartRefused("gate", [JOB_CHANGED_TEXT])

        secrets, settings = self._secrets, self._settings
        assert settings.ca_file is not None  # _ftps_problem
        try:
            await asyncio.to_thread(
                self._uploader, secrets.host, secrets.serial, secrets.access_code, settings.ca_file, pinned, target.file
            )
        except (UploadError, ValueError) as exc:
            raise _StartRefused("upload", [f"Файл не загрузился на принтер: {md_literal(str(exc))}"]) from None
        # The upload took a while: the printer is checked once more before the one send.
        self._preflight_or_refuse(target, info, file_sha)
        link = self._link
        if link is None:
            raise _StartRefused("not_connected", [NOT_CONNECTED_TEXT])
        assert target.choice.ams is not None and target.choice.slot is not None  # pre-flight passed
        task_id = self._new_task_id()
        watch = _StartWatch(task_id, asyncio.Event())
        self._watch = watch
        self._own_tasks.add(task_id)
        try:
            # The one send. Never repeated: the confirmation is used, whatever follows.
            link.start_project(
                file=target.file,
                plate=target.plate,
                md5=info.md5.lower(),
                subtask_name=target.file.removesuffix(GCODE_3MF_SUFFIX),
                task_id=task_id,
                ams=target.choice.ams,
                slot=target.choice.slot,
            )
        except PrinterNotConnected:
            self._watch = None
            raise _StartRefused("not_connected", [NOT_CONNECTED_TEXT]) from None
        except ValueError as exc:
            self._watch = None
            log.error("project_file refused by the link: %s", exc)
            raise _StartRefused("payload", [INTERNAL_TEXT]) from None
        return await self._watch_start(confirmation.id, target, watch)

    def _preflight_or_refuse(self, target: _PrintTarget, info: PrintFileInfo, file_sha: str) -> None:
        check = preflight(
            self._state,
            target.job,
            target.plate,
            target.choice,
            target.spool,
            info,
            target.expected_sha256,
            file_sha,
            target.nozzle,
            fresh=self.state_fresh,
        )
        if check.blocking:
            raise _StartRefused("preflight", check.blocking)

    def _new_task_id(self) -> str:
        """A fresh numeric task id: epoch seconds as in phase 0, never one seen or sent before."""
        candidate = max(int(self._clock().timestamp()), self._last_task_id + 1)
        if self._state.task_id == str(candidate):
            candidate += 1
        self._last_task_id = candidate
        return str(candidate)

    async def _watch_start(self, confirm_id: str, target: _PrintTarget, watch: _StartWatch) -> str:
        try:
            await asyncio.wait_for(watch.event.wait(), self._watch_s)
            started = True
        except TimeoutError:
            started = False
        finally:
            if self._watch is watch:
                self._watch = None
        plate = target.plate if target.job.kind == "kit" else None
        with self._store.transaction():
            if started:
                self._store.set_confirmation_outcome(confirm_id, "started")
                if not watch.announced:  # the STARTED event normally carries the stop button
                    self._enqueue(
                        "print_started",
                        render_print_started(target.job.name, plate),
                        f"start|{confirm_id}|started",
                        buttons=stop_ask_buttons(watch.task_id),
                    )
                return "started"
            self._store.set_confirmation_outcome(confirm_id, "unconfirmed")
            state = self._state
            self._enqueue(
                "print_unconfirmed",
                render_start_unconfirmed(target.job.name, self._watch_s, state.gcode_state, state.subtask_name),
                f"start|{confirm_id}|unconfirmed",
            )
            return "unconfirmed"


def _nozzle_text(value: object) -> str:
    """A nozzle diameter as the manifest and the gate spell it: "0.4"."""
    try:
        number = float(str(value))
    except ValueError:
        return str(value or "")
    return f"{number:g}" if math.isfinite(number) else str(value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _pin_file(path: Path) -> Path | None:
    """A private hard link to ``path`` (a streamed copy where links fail), None when it is gone.

    The press hashes, reads and uploads this one: a prepare fetching the same name meanwhile
    replaces the cache entry, never the file the press checked.
    """
    pinned = path.with_name(f".{path.name}.{_secrets.token_hex(8)}.upload")
    try:
        os.link(path, pinned)
    except FileNotFoundError:
        return None
    except OSError:
        try:
            shutil.copyfile(path, pinned)
        except FileNotFoundError:
            with contextlib.suppress(OSError):
                pinned.unlink()
            return None
    return pinned


def _start_press(button_id: str) -> tuple[str, str] | None:
    """(confirmation id, ``go`` or ``cancel``) of a ``start:<id>:<action>`` button, else None."""
    parts = button_id.split(":")
    if len(parts) != 3 or parts[0] != "start" or parts[2] not in ("go", "cancel"):
        return None
    if not _CONFIRM_ID_RE.fullmatch(parts[1]):
        return None
    return parts[1], parts[2]


def _print_press(button_id: str) -> tuple[int, int | None] | None:
    """(job id, plate or None) of ``job:<id>:print`` and ``job:<id>:print:<n>``, else None."""
    parts = button_id.split(":")
    if len(parts) not in (3, 4) or parts[0] != "job" or parts[2] != "print":
        return None
    numbers = [parts[1], *parts[3:]]
    if not all(p.isascii() and p.isdigit() and len(p) <= 9 for p in numbers):
        return None
    plate = int(parts[3]) if len(parts) == 4 else None
    if plate is not None and not 1 <= plate <= 50:
        return None
    return int(parts[1]), plate


def _slot_press(button_id: str) -> tuple[int, int] | None:
    """(job id, slot number) of a ``job:<id>:slot:<n>`` button, None for any other button."""
    parts = button_id.split(":")
    if len(parts) != 4 or parts[0] != "job" or parts[2] != "slot":
        return None
    if not all(p.isascii() and p.isdigit() for p in (parts[1], parts[3])):
        return None
    job_id, number = int(parts[1]), int(parts[3])
    try:
        slot_position(number)
    except ValueError:
        return None
    return job_id, number


def _proposal(job: Job, plan: SlicePlan) -> tuple[str, list[list[Button]]]:
    """The proposal's text and buttons: slice and change, then the slot rows."""
    text = render_proposal(job, plan.settings, plan.choice, plan.plate_type, None)
    warning = _preset_warning(plan)
    if warning:
        text = f"{text}\n{warning}"
    choice = plan.choice
    chosen = None
    if choice.ams is not None and choice.slot is not None:
        chosen = slot_number(choice.ams, choice.slot)
    buttons = [
        [
            Button(f"job:{job.id}:slice", SLICE_BUTTON_TEXT),
            Button(f"job:{job.id}:change", CHANGE_BUTTON_TEXT),
        ],
        *slot_buttons(job.id, plan.usable, chosen),
    ]
    return text, buttons


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
