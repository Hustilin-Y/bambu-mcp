"""bambu-service: one asyncio process wiring the printer link to notifications and the journal.

MQTT link (paho thread) -> PrinterState -> events -> journal + outbox (one SQLite transaction),
then a sender task delivers the outbox in order (with camera snapshots), retrying through
outages and restarts. Plus the post-print questions, the button presses and the quarterly
tariff reminder. Phase 2: print jobs are proposed and sliced on the owner's PC (wake it,
slice, send the summary with the plate picture).
"""

from __future__ import annotations

import asyncio
import collections
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
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from bambu_mcp import camera as camera_module
from bambu_mcp import jobs as jobs_module
from bambu_mcp import mcp_server
from bambu_mcp.config import Secrets, Settings, with_country
from bambu_mcp.cost import (
    CostBreakdown,
    CurrencyMismatch,
    sum_costs,
    tariff_check_due,
    tariff_reminder_enabled,
)
from bambu_mcp.events import CANCEL_ERROR, Event, EventDetector, EventKind, event_identity
from bambu_mcp.evaluation import (
    DEFAULT_GOAL,
    GOALS,
    VariantMetrics,
    base_variant,
    candidates,
    first_metric_unknown,
    goal_base,
    goal_record,
    job_goal,
    parse_goal,
    score,
    strength_proxy,
)
from bambu_mcp.jobs import EvaluationFailed, JobBusy, SliceStart
from bambu_mcp.journal import NO_SPOOL, TERMINAL_KINDS, Journal, Slot
from bambu_mcp.logthrottle import Throttle
from bambu_mcp.manifest import GOAL_KEYS, Manifest, ManifestError, parse_manifest, verify_file
from bambu_mcp.messages import (
    EVALUATING_TEXT,
    MAX_BUTTONS,
    eval_buttons,
    fit_question,
    goal_buttons,
    job_name,
    md_literal,
    plate_buttons,
    plate_question,
    post_print_buttons,
    print_buttons,
    reset_buttons,
    render_event,
    render_print_refused,
    render_print_started,
    render_proposal,
    render_sliced,
    render_start_confirmation,
    render_start_unconfirmed,
    render_stop_confirmation,
    render_variants,
    goal_warning,
    slot_buttons,
    spool_buttons,
    spool_question,
    start_buttons,
    control_buttons,
    stop_ask_buttons,
    stop_buttons,
    tariff_reminder,
    variant_buttons,
)
from bambu_mcp.names import slug
from bambu_mcp.notify import Button, LogNotifier, Notifier, Press, SendError, ServiceNotifyClient, paragraphs
from bambu_mcp.printer import ftps
from bambu_mcp.printer.ftps import UploadError
from bambu_mcp.printer.hms import HmsTexts
from bambu_mcp.printer.link import (
    GCODE_3MF_SUFFIX,
    TASK_ID_RE,
    LinkStatus,
    PrinterLink,
    PrinterNotConnected,
)
from bambu_mcp.printer.state import EXTERNAL_AMS, EXTERNAL_TRAY, PrinterState, apply
from bambu_mcp.printgate import (
    ControlRefused,
    PrintFileError,
    PrintFileInfo,
    PrintRefused,
    gate_hash,
    preflight,
    print_file_info,
)
from bambu_mcp.proposal import (
    SlotChoice,
    SlotUnusable,
    UsableSlot,
    job_hints,
    job_material,
    last_settings,
    pick_slot,
    predict_cost,
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
from bambu_mcp.store.models import Job, OutboxRow, Spool

log = logging.getLogger(__name__)

DEFAULT_ENV = "/srv/bambu/.env"
DB_NAME = "bambu.db"
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
UPLOAD_TIMEOUT_S = 300.0  # the whole FTPS upload: ftps.upload has only per-socket timeouts
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
# A start confirmation an evaluation run made stale (ruling R5: the PC file is re-sliced).
REEVALUATED_OUTCOME = "reevaluated"
REEVALUATED_TEXT = "⌛ Настройки подбираются заново, подготовь печать после подбора"
EVAL_INTERRUPTED_ERROR = "evaluation interrupted by a service restart"
RESTORE_WAIT_S = 60.0  # at start-up, how long a cut-off run's proposal waits for the slots
EVAL_INTERRUPTED_TEXT = (
    "Подбор вариантов для «{name}» не закончен: подбор прерван перезапуском сервиса. "
    "Нажми «🔍 Подобрать» ещё раз"
)
JOB_CHANGED_TEXT = "Задание изменилось после подтверждения, подготовь печать заново"
FILE_GONE_TEXT = "Нарезанного файла уже нет на сервере, подготовь печать заново"
FILE_CHANGED_TEXT = "Нарезанный файл изменился после подтверждения, подготовь печать заново"
NOT_CONNECTED_TEXT = "Принтер не на связи, команда не отправлена"
INTERNAL_TEXT = "внутренняя ошибка сервиса"
INTERRUPTED_TEXT = (
    "⚠️ Запуск печати «{name}» прервался из-за перезапуска сервиса. Не знаю, начал ли принтер печатать. "
    "Посмотри на экран принтера. Команда повторно не отправляется."
)
UPLOAD_TIMEOUT_TEXT = "Файл не загрузился на принтер: загрузка на принтер не уложилась в 5 минут"
NO_JOB_TEXT = "Задание не найдено"
NO_PLATE_TEXT = "В задании нет пластины {plate}"
# The confirmed stop.
STOP_SENT_TEXT = "⏹ Команда остановки отправлена"
STOP_CANCELLED_TEXT = "Остановка отменена"
STOP_NOT_CONNECTED_TEXT = "Принтер не на связи, остановка не отправлена"
STOP_NOT_FRESH_TEXT = "Принтер ещё не прислал свежий отчёт после переподключения, остановка не отправлена"
STOP_OVER_TEXT = "Эта печать уже не идёт, останавливать нечего"
STOP_UNSURE_TEXT = "⚠️ Не знаю, дошла ли команда остановки. Посмотри на принтер, повторно она не отправляется"
APP_NOTES_KEPT = 5
SNAPSHOT_TEXT = "📷 Снимок"
CONTROL_NOT_RUNNING_TEXT = "Печать не идёт, ставить на паузу нечего"
CONTROL_NOT_PAUSED_TEXT = "Печать не на паузе, продолжать нечего"
CONTROL_OTHER_TEXT = "Сейчас идёт другая печать, а кнопка была для прежней. Ничего не отправлено"
CONTROL_RESUME_SENT_TEXT = "Команда продолжить уже отправлена, жду принтер"
STOP_OTHER_TEXT = "Сейчас идёт другая печать, а кнопка была для прежней. Остановка не отправлена"
_STOP_SPENT_TEXTS = {
    "used": "Эта кнопка уже нажата, остановка второй раз не отправляется",
    "expired": "⌛ Подтверждение истекло, остановка не отправлена. Запроси её заново, если нужно",
    "cancelled": STOP_CANCELLED_TEXT,
}
# A confirmation that would reach Telegram with less than this left goes out without buttons.
MIN_BUTTONS_LIFE_S = 5
_EXPIRED_ON_SEND = {"print_confirm": _SPENT_TEXTS["expired"], "stop_confirm": _STOP_SPENT_TEXTS["expired"]}
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

    def pause(self) -> None: ...

    def stop_print(self) -> None: ...


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
    # (primary, secondary, source, reason) from evaluation.job_goal: its base point is in ``settings``
    goal: tuple[str, str | None, str, str | None] = ("balanced", None, "fallback", None)


@dataclass(frozen=True)
class _EvalRun:
    """An evaluation run claimed on a job: what it slices and what a failure restores."""

    job: Job
    plan: SlicePlan  # the goal's plan: its slot, spool and goal hold for every variant
    prev_status: str  # the job's status before the run, given back at its end
    settings: list[dict[str, object]]  # the balanced baseline first (evaluation.candidates)
    before_text: str  # the proposal before the run: a failed baseline puts it back
    before_buttons: list[list[Button]]
    message_id: str | None  # the proposal message pressed, None from a tool call
    previous: str | None  # the job's variants_json before the run, given back if it fails
    explicit: dict[str, object]  # the owner's settings, the same in every variant
    base_index: int | None  # the goal's base point among ``settings``


@dataclass(frozen=True)
class PrepareResult:
    """A start confirmation sent to Telegram: only the owner's press on it starts the print."""

    confirm_id: str
    text: str
    warnings: list[str]


@dataclass(frozen=True)
class StopRequest:
    """A stop confirmation sent to Telegram: only the owner's press on it stops the print."""

    confirm_id: str
    text: str


@dataclass(frozen=True)
class _StopProblem:
    """Why a stop is not asked for or not sent: ``reason`` for the outcome, English and Russian."""

    reason: str
    error: str
    text: str


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


class _StartSent(Exception):
    """``project_file`` may have left: the outcome is ``unconfirmed``, never a refusal."""


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


def _tray_slot(report: dict) -> Slot | None:
    """AMS slot the printer currently feeds from, if the report says so."""
    p = report.get("print")
    ams = p.get("ams") if isinstance(p, dict) else None
    raw = ams.get("tray_now") if isinstance(ams, dict) else None
    try:
        tray = int(raw)
    except (TypeError, ValueError):
        return None
    if tray == EXTERNAL_TRAY:  # 255 means nothing is feeding
        return EXTERNAL_AMS, EXTERNAL_TRAY
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
        upload_timeout_s: float = UPLOAD_TIMEOUT_S,
        restore_wait_s: float = RESTORE_WAIT_S,
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
        # scripts/extract_hms.py output for the profile's device prefix, kept in data_dir.
        hms_name = settings.profile.hms_file
        hms_path = Path(settings.data_dir) / hms_name if hms_name else None
        if hms_path is None or not hms_path.is_file():
            log.warning("HMS text table %s not found, HMS messages show codes only", hms_path or "(no prefix)")
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
        self._evaluating: set[int] = set()  # job ids with an evaluation run in flight
        # Printing (phase 3).
        self._uploader = uploader
        self._watch_s = watch_s
        self._report_wait_s = report_wait_s
        self._upload_timeout_s = upload_timeout_s
        self._start_lock = asyncio.Lock()  # one start at a time
        self._press_tasks: set[asyncio.Task] = set()  # starts and prepares run off the press loop
        self._preparing: set[int] = set()  # job ids with a prepare from a button in flight
        self._report_waiters: set[asyncio.Event] = set()
        self._restore_wait_s = restore_wait_s
        self._watch: _StartWatch | None = None
        self.app_notes: collections.deque[dict[str, Any]] = collections.deque(maxlen=APP_NOTES_KEPT)
        self._preparing: asyncio.Task | None = None  # the Mini App's prepare, one at a time
        self._control: dict[str, bool] = {}  # task id -> paused variant last put on its control message
        self._resumed: set[str] = set()  # resume sent, the printer not yet out of PAUSE
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
            await self._recover_evaluations()
        except Exception:
            log.exception("checking for interrupted evaluations failed")
        try:
            self._fail_interrupted_jobs()
        except Exception:
            log.exception("checking for interrupted slices failed")
        try:
            self._sweep_attachments()
        except Exception:
            log.exception("removing unused message attachments failed")
        try:
            self._close_interrupted_starts()
        except Exception:
            log.exception("checking for interrupted print starts failed")
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
        """Queue the quarterly tariff reminder when due. Returns True when it was queued.

        Runs for the country UA only (`set_country`).
        """
        if not tariff_reminder_enabled(self._store.country()):
            return False
        today = self._clock().astimezone(self._tz).date()
        raw = self._store.get_meta(TARIFF_META_KEY)
        last = date.fromisoformat(raw) if raw else None
        if not tariff_check_due(last, today):
            return False
        text = tariff_reminder(self._store.tariff_on(today), self._store.currency()[1])
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
        text, buttons, expires_in = row.text, _rows_buttons(row.buttons), None
        if row.expires_at is not None:
            # The add-in takes the buttons off when the confirmation expires.
            expires_in = math.ceil(row.expires_at - now)
            if expires_in < MIN_BUTTONS_LIFE_S:
                expired = _EXPIRED_ON_SEND.get(row.kind, _SPENT_TEXTS["expired"])
                text, buttons, expires_in = f"{text}\n\n{expired}", None, None
        try:
            message_id = await self._notifier.send(
                text, buttons=buttons, files=files, dedupe=row.dedupe, expires_in=expires_in
            )
        except SendError as exc:
            return self._delivery_failed(row, exc)
        except Exception as exc:  # a bug in the client must not stop the outbox
            log.exception("sending a %s message failed", row.kind)
            return self._delivery_failed(row, SendError(None, type(exc).__name__))
        if row.ref is not None and message_id is not None:
            try:
                self._store.record_sent(row.ref, message_id, text, now)
            except Exception:  # the message went out: only the Mini App's view of it is lost
                log.exception("recording a sent %s message failed", row.kind)
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
        expires_at: float | None = None,
        ref: str | None = None,
    ) -> bool:
        """Queue a message. ``expires_at`` (epoch seconds): when its buttons stop working.

        ``ref`` names what the message is for, so its Telegram id can be found once sent.
        False when a message with the same dedupe key is already waiting.
        """
        queued = self._store.outbox_add(
            kind,
            text,
            notify_dedupe(dedupe),
            buttons=_rows_json(buttons),
            snapshot=snapshot,
            now=self._clock().timestamp(),
            expires_at=expires_at,
            ref=ref,
        )
        if self._outbox_wake is not None:
            self._outbox_wake.set()
        return queued

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

    def _close_interrupted_starts(self) -> None:
        """Start presses a restart cut off: claimed, never decided. Never sent again.

        The confirmation became ``unconfirmed`` and the owner is asked to look at the printer:
        the ``project_file`` may or may not have left before the restart.
        """
        for confirmation in self._store.undecided_confirmations("start"):
            job = self._store.job(confirmation.job_id) if confirmation.job_id is not None else None
            name = job_name(job.name if job is not None else None)
            with self._store.transaction():
                self._store.set_confirmation_outcome(confirmation.id, "unconfirmed")
                self._enqueue(
                    "print_interrupted",
                    INTERRUPTED_TEXT.format(name=name),
                    f"start|{_id_tag(confirmation.id)}|interrupted",
                )
            log.warning("a print start was interrupted by a restart, the owner is asked to check the printer")

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
            if press.button_id.startswith("stop:"):
                await self._on_stop_press(press)
                return
            if _control_press(press.button_id) is not None:
                await self._on_control_press(press)
                return
            if _print_press(press.button_id) is not None:
                await self._on_print_press(press)
                return
            if _slot_press(press.button_id) is not None:
                await self._on_slot_press(press)
                return
            if _goal_press(press.button_id) is not None:
                await self._on_goal_press(press)
                return
            if _eval_press(press.button_id) is not None:
                await self._on_eval_press(press)
                return
            if _variant_press(press.button_id) is not None:
                await self._on_variant_press(press)
                return
            if _reset_press(press.button_id) is not None:
                await self._on_reset_press(press)
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
        if state_report:
            self._sync_control(new)
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

        code, symbol = self._store.currency()
        text = render_event(event, self._hms_texts, cost, duration, tz=self._tz, symbol=symbol, code=code)
        buttons = ref = None
        task_id = event.state.task_id
        if event.kind is EventKind.STARTED and _control_task(task_id):
            buttons, ref = control_buttons(task_id, False), f"control:{task_id}"  # any print: pause and stop
            self._control[task_id] = False
        self._enqueue(
            str(event.kind), text, event.dedupe_key, buttons=buttons, snapshot=event.kind in SNAPSHOT_KINDS, ref=ref
        )
        if buttons is not None and self._watch is not None and self._watch.task_id == task_id:
            self._watch.announced = True
        # No questions for a print interrupted unseen: there is no finished part to ask about.
        if event.kind in TERMINAL_KINDS and print_id is not None and not event.lost:
            self._ask_post_print(event, print_id)

    def _ask_post_print(self, event: Event, print_id: int) -> None:
        name = event.state.subtask_name
        if event.kind is EventKind.FINISHED:  # a failed or stopped print has no part to fit
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

    async def propose(self, job_id: int, slot: int | None = None, goal: object = None) -> str:
        """Queue the job's proposal with the slice, change, slot and goal buttons. Returns its text.

        ``slot`` (1..16) is the owner's AMS slot: stored with the job, it replaces the one
        stored before. ``goal`` (a key or [primary, secondary]) is the owner's goal: stored
        with the job, it applies its base point and drops a chosen variant. Raises
        ``KeyError`` for an unknown job, ``SlotUnusable`` for a slot that cannot be printed
        from, ``ValueError`` for an unknown goal or settings that do not resolve.
        """
        owner_goal = parse_goal(goal) if goal is not None else None
        job = self._job(job_id)
        if (slot is not None or owner_goal is not None) and job.status == "slicing":
            raise JobBusy(job.id)  # the slot or goal of a running slice never changes under it
        plan = self._plan(job, None, DEFAULT_NAMED_SET, slot, owner_goal)
        chosen = _chosen_fields(slot, owner_goal)
        if chosen:
            job = replace(job, **chosen)
        text, buttons = self._proposal(job, plan)
        with self._store.transaction():
            if chosen:
                self._store.update_job(job.id, **chosen)
            self._enqueue("job_proposal", text, _job_key(job.id, "proposal"), buttons=buttons)
        return text

    async def slice_job(
        self,
        job_id: int,
        explicit: Mapping | None = None,
        *,
        named_set: str = DEFAULT_NAMED_SET,
        slot: int | None = None,
        goal: object = None,
    ) -> Job:
        """Wake the PC, slice the job, store the result and queue the summary (or the failure).

        ``explicit`` is what the owner asked for (ruling R6): stored with the job
        (``explicit_json``, merged over what was stored), so later slices and evaluations
        keep it. ``slot`` is the owner's AMS
        slot, stored with the job (None: the stored one, else the material match). ``goal``
        is the owner's goal, stored with the job: its base point is under ``explicit``. Raises
        ``KeyError`` (unknown job), ``ValueError`` (settings outside the whitelist, or
        ``SlotUnusable``) or ``JobBusy`` before anything changes. Every later failure ends
        the job ``slice_failed`` and tells the owner.
        """
        job, plan = self._claim(job_id, explicit, named_set, slot, goal)
        await self._slice_claimed(job, plan)
        return self._job(job.id)

    def start_slice(
        self,
        job_id: int,
        explicit: Mapping | None = None,
        *,
        named_set: str = DEFAULT_NAMED_SET,
        slot: int | None = None,
        goal: object = None,
    ) -> SliceStart:
        """``slice_job`` in the background: the job is claimed before this returns.

        The slot warning is returned at once, so a caller that skipped the proposal sees it.
        """
        job, plan = self._claim(job_id, explicit, named_set, slot, goal)
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
        self,
        job_id: int,
        explicit: Mapping | None,
        named_set: str,
        slot: int | None = None,
        goal: object = None,
    ) -> tuple[Job, SlicePlan]:
        owner_goal = parse_goal(goal) if goal is not None else None
        job = self._job(job_id)
        plan = self._plan(job, explicit, named_set, slot, owner_goal)
        chosen = _chosen_fields(slot, owner_goal)
        if explicit:
            # The owner's words stay their own layer (ruling R2): an evaluation keeps them fixed.
            chosen["explicit_json"] = json.dumps({**_explicit(job), **explicit})
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
        self,
        job: Job,
        explicit: Mapping | None,
        named_set: str,
        slot: int | None = None,
        goal: tuple[str, str | None] | None = None,
        *,
        use_variant: bool = True,
        base_goal: str | None = None,
    ) -> SlicePlan:
        """``slot`` (1..16) wins over the job's stored slot, which wins over the material match.

        ``goal`` (primary, secondary) is the owner's new goal: it replaces the job's and drops
        its chosen variant. Without it the job's goal (``job_goal``) and variant apply
        (``use_variant`` False: not the variant, an evaluation run's baseline). The last
        sliced job of the part (another job, never this one) ranks above the goal's base point when its primary goal was
        the same (ruling R4), below it otherwise. ``base_goal``: that goal's base point instead
        of the job goal's (an evaluation's balanced baseline), none for the fallback goal.
        ``explicit`` goes over the owner's settings stored with the job (``explicit_json``).
        """
        manifest = parse_manifest(json.loads(job.manifest_json))  # checked at intake
        nozzle = f"{manifest.nozzle_mm:g}"
        history = self._store.jobs(JOB_HISTORY)
        if goal is not None:
            goal_now: tuple[str, str | None, str, str | None] = (goal[0], goal[1], "owner", None)
            variant = None
        else:
            goal_now = job_goal(job, manifest)
            variant = _variant(job) if use_variant else None
        # The fallback goal applies no base point: the hints and the last job stay as before
        # goals existed (controller ruling). Only the owner's or the CAD agent's goal sets one.
        base_key = base_goal or goal_now[0]
        base = None
        if goal_now[2] != "fallback":
            base = goal_base(base_goal, None) if base_goal else goal_base(goal_now[0], goal_now[1])
        # The last job is another job of the part: the job's own earlier slice is never a layer
        # (a goal change must not slice the old settings again).
        last = last_settings(history, job.name, exclude_id=job.id)
        if base is not None and last is not None and _last_primary(history, job.name, job.id) == base_key:
            base = {**base, **last}  # R4: the same goal keeps what the last job (or its variant) chose
        settings = resolve_settings(
            {**_explicit(job), **(explicit or {})},
            last,
            job_hints(manifest),
            named_set,
            goal_base=base,
            variant=variant,
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
        forced = slot_position(number, self._settings.profile.max_slot_number) if number is not None else None
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
            goal=goal_now,
        )

    def _predict(
        self, grams: float | None, minutes: float | None, spool: Spool | None, material: str
    ) -> CostBreakdown | CurrencyMismatch:
        """A predicted cost, priced as if the print started now with today's tariff and amortization."""
        now = self._clock()
        day = now.astimezone(self._tz).date()
        return predict_cost(
            grams,
            minutes,
            spool,
            material,
            self._store.tariff_on(day),
            self._store.amortization_on(day),
            self._settings,
            now,
            currency=self._store.currency()[0],
        )

    def _proposal(self, job: Job, plan: SlicePlan) -> tuple[str, list[list[Button]]]:
        """The proposal's text and buttons: slice and change, then the slot rows.

        The cost: the chosen evaluation variant's sliced grams and minutes when it has them,
        else the manifest's grams at the slot spool's price and the minutes of the job's last
        slice when it has one (else no electricity or amortization yet).
        """
        sliced = _sliced_minutes(job)
        plates = plan.manifest.plates
        stale = False
        data = _variants(job)
        entry = _chosen_entry(data) if data is not None else None
        try:
            if entry is not None:
                stale = entry["settings"] != plan.settings
                cost = self._predict(entry["grams"], entry["minutes"], plan.spool, plan.material)
            elif sliced is not None and all(p.index in sliced for p in plates):
                stale = _loads_dict(job.settings_json) != plan.settings
                # Per plate, as the slice summary does: each plate heats up once.
                cost = sum_costs(
                    [self._predict(p.estimate_g, sliced[p.index], plan.spool, plan.material) for p in plates]
                )
            else:
                cost = self._predict(sum(p.estimate_g for p in plates), None, plan.spool, plan.material)
        except Exception:
            log.exception("job %d: the proposal's cost failed, sent without it", job.id)
            cost = None
        code, symbol = self._store.currency()
        return _proposal(job, plan, cost, symbol, code=code, stale=stale)

    def _print_material(self, target: _PrintTarget, info: PrintFileInfo) -> str:
        """The material whose power draw prices the plate: the slot spool's, else the file's."""
        if target.spool is not None:
            return self._canonical(target.spool.material)
        return self._canonical(info.filament_types[0] if info.filament_types else "PLA")

    def _canonical(self, material: str) -> str:
        """The material as the power table spells it (``print_kwh`` is case-sensitive)."""
        for key in self._settings.power_w:
            if key.casefold() == material.casefold():
                return key
        return material

    def _default_plate_type(self) -> str:
        for plate in self._store.plates():
            if plate.is_default:
                if plate.bed_type in self._settings.profile.plate_types:
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

    def _require_slicer(self) -> Slicer:
        if self._slicer is None:
            raise _SliceFailed(
                "slice-agent is not configured",
                f"слайсер не настроен, нужны {md_literal('slice_agent_url')} и {md_literal('SLICE_AGENT_TOKEN')}",
            )
        return self._slicer

    async def _slice(self, job: Job, plan: SlicePlan) -> None:
        slicer = self._require_slicer()
        data = await self._ready_to_slice(job, plan)
        outcome = await self._slice_once(job, plan, slicer, data)
        await self._finish(job, plan, outcome)

    async def _ready_to_slice(self, job: Job, plan: SlicePlan) -> bytes:
        """The job's checked 3MF, once the PC is awake. ``_SliceFailed`` otherwise."""
        slicer = self._require_slicer()
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
        return data

    async def _slice_once(self, job: Job, plan: SlicePlan, slicer: Slicer, data: bytes) -> SliceOutcome:
        """One ``/slice`` with ``plan.settings`` (one retry when the agent is busy): an ``ok``
        outcome with a file, else ``_SliceFailed``."""
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
        return outcome

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
        plates = [dict(p) for p in outcome.plates]
        costs = [self._predict(sum(p["grams"].values()), p["minutes"], plan.spool, plan.material) for p in plates]
        first = min(p["index"] for p in plates)
        try:
            png = await self._slicer.fetch_png(outcome.file, first)
        except Exception:
            log.exception("job %d: plate picture failed, sending the text alone", job.id)
            png = None
        text = render_sliced(
            job,
            plates,
            costs,
            settings=plan.settings,
            slot_choice=plan.choice,
            plate_type=plan.plate_type,
            symbol=self._store.currency()[1],
        )
        broken = goal_warning(plan.goal[0], plan.settings)
        if broken:
            text = f"{text}\n{broken}"
        choice = plan.choice
        result = {
            "plates": plates,
            "costs": [_cost_json(p["index"], c) for p, c in zip(plates, costs, strict=True)],
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
        # Counted as sent: the client turns each newline into a blank line.
        split = png is not None and _caption_len(paragraphs(text)) > CAPTION_MAX
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

    def _fail_job(self, job: Job, error: str, text: str, **fields: Any) -> None:
        """``slicing`` -> ``slice_failed`` with ``error`` (and ``fields``), and tell the owner ``text``."""
        try:
            with self._store.transaction():
                if self._store.transition_job(
                    job.id, ("slicing",), status="slice_failed", error=error, **fields
                ):
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
            fields: dict[str, Any] = {}
            marker = _eval_marker(job)
            if marker is not None:  # an evaluation _recover_evaluations could not give back
                previous = marker.get("previous") if isinstance(marker.get("previous"), str) else None
                fields["variants_json"] = _failed_record(previous, EVAL_INTERRUPTED_ERROR, self._clock())
            self._fail_job(job, "interrupted by a service restart", "прервано перезапуском сервиса", **fields)

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
            await self._notifier.edit(press.message_id, self._busy_text(job))
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
            await self._notifier.edit(press.message_id, self._busy_text(job))
            return
        text, buttons = self._proposal(self._job(job.id), plan)
        if not await self._notifier.edit(press.message_id, text, buttons):
            log.warning("job %d: editing the proposal failed, sending it again", job.id)
            self._enqueue("job_proposal", text, _job_key(job.id, "proposal"), buttons=buttons)

    async def _on_goal_press(self, press: Press) -> None:
        """``job:<id>:goal:<key>``: store the owner's goal and edit the proposal in place.

        The goal's base point replaces the settings shown, a chosen variant is dropped.
        Nothing is sliced and the PC is not woken. While the job is slicing the press is
        answered like the slice button and nothing changes.
        """
        parsed = _goal_press(press.button_id)
        assert parsed is not None
        job_id, key = parsed
        job = self._store.job(job_id)
        if job is None:
            log.warning("goal press for unknown job %d ignored", job_id)
            return
        name = job_name(job.name)
        if job.status == "slicing":
            await self._notifier.edit(press.message_id, self._busy_text(job))
            return
        try:
            plan = self._plan(job, None, DEFAULT_NAMED_SET, None, (key, None))
        except ValueError as exc:  # SlotUnusable too: the stored slot was emptied since
            log.warning("job %d: goal press refused: %s", job.id, exc)
            text = f"Не получилось выбрать цель для «{name}»: {md_literal(str(exc))}"
            self._enqueue("job_goal", text, _job_key(job.id, "goal"))
            return
        chosen = _chosen_fields(None, (key, None))
        # A slice claimed in the meantime keeps its own plan: the goal is not changed under it.
        if not self._store.transition_job(job.id, CLAIMABLE, **chosen):
            await self._notifier.edit(press.message_id, self._busy_text(job))
            return
        text, buttons = self._proposal(self._job(job.id), plan)
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
            return self._busy_text(job)
        try:
            self.start_slice(job.id)
        except JobBusy:
            return self._busy_text(job)
        except ValueError as exc:
            log.warning("job %d: slice press refused: %s", job.id, exc)
            return f"Не получилось начать нарезку «{name}»: {md_literal(str(exc))}"
        except Exception:
            log.exception("job %d: slice press failed", job.id)
            return f"Не получилось начать нарезку «{name}»: внутренняя ошибка сервиса"
        return f"✂️ Режу «{name}», пришлю результат"

    # -- jobs: the evaluation run («Подобрать») ------------------------------------

    def _busy_text(self, job: Job) -> str:
        """The answer to a press on a job that is being sliced or evaluated."""
        name = job_name(job.name)
        if job.id in self._evaluating or _eval_marker(job) is not None:
            return f"Уже подбираю варианты для «{name}»"
        return f"Уже режу «{name}»"

    async def evaluate(self, job_id: int, goal: object = None, explicit: Mapping | None = None) -> str:
        """``start_evaluate`` and wait for the run: returns the proposal text.

        Raises what ``start_evaluate`` raises, and ``EvaluationFailed`` when the baseline did
        not slice (the owner is told, nothing is stored). A cancelled caller never
        interrupts the run.
        """
        text, error = await asyncio.shield(self.start_evaluate(job_id, goal, explicit))
        if error is not None:
            raise EvaluationFailed(error)
        assert text is not None
        return text

    def start_evaluate(self, job_id: int, goal: object = None, explicit: Mapping | None = None) -> asyncio.Task:
        """Claim the job and run its evaluation in the background. The task gives
        (proposal text, None) or (None, the English reason of a failed run).

        ``goal`` is the owner's goal and ``explicit`` the owner's settings, both stored with
        the job first. Wakes the PC once and slices, one at a time: the balanced baseline
        (the job with the balanced base point and the explicit settings, the ratio limits
        and «прочность N×» compare with it), then the goal's grid over the goal's settings,
        its base point among them, at most 6 in all. The winner becomes the job's ``variant``
        layer. A ``sliced`` job loses its result (ruling R5: the PC file is overwritten) and
        its pending start confirmations expire. Raises ``KeyError`` (unknown job),
        ``ValueError`` (a kit, an unknown goal, settings that do not resolve) or ``JobBusy``
        (slicing or evaluating already) before anything changes.
        """
        run = self._claim_eval(job_id, goal, None, explicit)
        return self._spawn_eval(run)

    def _spawn_eval(self, run: _EvalRun) -> asyncio.Task:
        task = asyncio.create_task(self._run_eval(run), name=f"bambu-eval-{run.job.id}")
        self._slice_tasks.add(task)
        task.add_done_callback(self._slice_tasks.discard)
        return task

    def _claim_eval(
        self, job_id: int, goal: object, message_id: str | None, explicit: Mapping | None = None
    ) -> _EvalRun:
        owner_goal = parse_goal(goal) if goal is not None else None
        job = self._job(job_id)
        if job.kind != "part":
            raise ValueError(f"job {job.id} is a kit: the evaluation runs for parts only")
        if job.status not in CLAIMABLE:
            raise JobBusy(f"job {job.id} is being sliced or evaluated already")
        fixed = {**_explicit(job), **(explicit or {})}
        before_text, before_buttons = self._proposal(job, self._plan(job, None, DEFAULT_NAMED_SET))
        plan = self._plan(job, fixed, DEFAULT_NAMED_SET, None, owner_goal, use_variant=False)
        balanced = self._plan(
            job, fixed, DEFAULT_NAMED_SET, None, owner_goal, use_variant=False, base_goal=DEFAULT_GOAL
        )
        primary, nozzle = plan.goal[0], plan.nozzle
        settings = candidates(primary, fixed, plan.settings, plan.manifest.checks, nozzle, baseline=balanced.settings)
        try:
            target = base_variant(primary, fixed, plan.settings, nozzle)
        except ValueError:
            target = None
        base_index = settings.index(target) if target in settings else None
        prev_status = "new" if job.status == "sliced" else job.status
        marker = {
            "state": "running",
            "prev_status": prev_status,
            "message_id": message_id,
            "before_text": before_text,
            "before_buttons": _rows_json(before_buttons),
            # A new goal drops the old evaluation, as a goal press does.
            "previous": None if owner_goal is not None else job.variants_json,
        }
        fields: dict[str, Any] = {
            **_chosen_fields(None, owner_goal),
            "status": "slicing",  # the busy guard of slicing: presses and tools wait for the run
            "variants_json": json.dumps(marker, ensure_ascii=False),
            "error": None,
        }
        if explicit:
            fields["explicit_json"] = json.dumps(fixed)
        if job.status == "sliced":
            # Ruling R5: every variant is sliced to the job's one PC file name, so the old
            # result no longer matches the file on the PC.
            fields.update(result_json=None, sliced_file=None)
        with self._store.transaction():
            if not self._store.transition_job(job.id, (job.status,), **fields):
                raise JobBusy(f"job {job.id} is being sliced or evaluated already")
            expired = self._store.expire_pending_confirmations(job.id, "start", REEVALUATED_OUTCOME)
        if expired:
            log.info("job %d: %d pending start confirmations expired by the evaluation", job.id, expired)
        self._evaluating.add(job.id)
        return _EvalRun(
            job, plan, prev_status, settings, before_text, before_buttons, message_id,
            marker["previous"], fixed, base_index,
        )

    async def _run_eval(self, run: _EvalRun) -> tuple[str | None, str | None]:
        """(proposal text, None) or (None, the English reason). Never raises but on cancel.

        A cancel (the service stops) leaves the job ``slicing`` with its running marker: the
        next start gives it back (``_recover_evaluations``).
        """
        job = run.job
        try:
            try:
                entries = await self._slice_variants(run)
                data = self._eval_commit(run, entries)
            except _SliceFailed as exc:
                log.warning("job %d: evaluation failed: %s", job.id, exc.error)
                await self._eval_failed(run, exc.text, exc.error)
                return None, exc.error
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("evaluating job %d failed", job.id)
                await self._eval_failed(run, INTERNAL_TEXT, "internal error in the service")
                return None, "internal error in the service"
            # Stored: whatever happens to the message now, the run did not fail.
            return await self._eval_announce(run, data), None
        finally:
            self._evaluating.discard(job.id)

    async def _slice_variants(self, run: _EvalRun) -> list[dict[str, Any]]:
        """Slice the run's settings in order. A failed baseline raises ``_SliceFailed``."""
        job, plan = run.job, run.plan
        slicer = self._require_slicer()
        data = await self._ready_to_slice(job, plan)
        entries: list[dict[str, Any]] = []
        for n, settings in enumerate(run.settings, 1):
            variant_plan = replace(plan, settings=settings)
            try:
                outcome = await self._slice_once(job, variant_plan, slicer, data)
            except _SliceFailed as exc:
                if n == 1:
                    raise
                log.info("job %d: variant %d not sliced: %s", job.id, n, exc.error)
                entries.append({"n": n, "status": "failed", "settings": settings, "error": exc.error})
                continue
            entries.append(self._variant_entry(n, settings, plan, outcome))
        return entries

    def _variant_entry(self, n: int, settings: dict, plan: SlicePlan, outcome: SliceOutcome) -> dict[str, Any]:
        plates = [dict(p) for p in outcome.plates]
        costs = [self._predict(sum(p["grams"].values()), p["minutes"], plan.spool, plan.material) for p in plates]
        cost = sum_costs(costs)
        pattern = settings.get("sparse_infill_pattern")
        strength = strength_proxy(plates[0].get("features"), pattern if isinstance(pattern, str) else None)
        return {
            "n": n,
            "status": "ok",
            "settings": settings,
            "minutes": sum(p["minutes"] for p in plates),
            "grams": sum(sum(p["grams"].values()) for p in plates),
            "cost": cost.total.amount if isinstance(cost, CostBreakdown) else None,
            "currency": cost.currency if isinstance(cost, CostBreakdown) else cost.expected,
            "strength": strength,
        }

    def _eval_commit(self, run: _EvalRun, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """Score the run, store it with the winner as the job's variant, give the job back."""
        job, plan = run.job, run.plan
        primary = plan.goal[0]
        metrics = [
            VariantMetrics(
                settings=e["settings"],
                minutes=e.get("minutes"),
                grams=e.get("grams"),
                cost=e.get("cost"),
                strength=e.get("strength"),
                ok=e["status"] == "ok",
            )
            for e in entries
        ]
        ranking = score(metrics, (primary, plan.goal[1]), fixed=set(run.explicit), base_index=run.base_index)
        winner = entries[ranking[0] if ranking else 0]
        strength_unknown = (
            GOALS[primary].score[0] == "strength"
            and first_metric_unknown(metrics, primary)
            and bool(ranking)
            and ranking[0] == run.base_index
        )
        base_strength = entries[0].get("strength")
        for e in entries:
            s = e.get("strength")
            e["strength_ratio"] = s / base_strength if s is not None and base_strength else None
        data = {
            "state": "done",
            "goal": [primary, plan.goal[1]],
            "chosen": winner["n"],
            "strength_unknown": strength_unknown,
            "variants": entries,
        }
        done = self._store.transition_job(
            job.id,
            ("slicing",),
            status=run.prev_status,
            variant_json=json.dumps(winner["settings"]),
            variants_json=json.dumps(data, ensure_ascii=False),
        )
        if not done:
            raise RuntimeError(f"job {job.id} left the slicing status during its evaluation")
        log.info("job %d evaluated: %d variants, variant %d chosen", job.id, len(entries), winner["n"])
        return data

    async def _eval_announce(self, run: _EvalRun, data: dict[str, Any]) -> str:
        """The proposal with the table: the pressed message edited, else a new message. Never raises."""
        job = run.job
        buttons: list[list[Button]] | None = None
        try:
            fresh = self._job(job.id)
            text, buttons = self._proposal(fresh, self._plan(fresh, None, DEFAULT_NAMED_SET))
        except Exception:
            log.exception("job %d: the proposal after the evaluation failed, sending the table alone", job.id)
            code, symbol = self._store.currency()
            text = "\n".join([
                job_name(job.name),
                *render_variants(data["variants"], data["chosen"], symbol, code=code,
                                 strength_unknown=data["strength_unknown"]),
            ])
        edited = False
        if run.message_id is not None:
            try:
                edited = await self._notifier.edit(run.message_id, text, buttons)
            except Exception:
                log.exception("job %d: editing the proposal after the evaluation raised", job.id)
        if not edited:
            try:
                self._enqueue("job_proposal", text, _job_key(job.id, "proposal"), buttons=buttons)
            except Exception:
                log.exception("job %d: queueing the proposal after the evaluation failed", job.id)
        return text

    async def _eval_failed(self, run: _EvalRun, reason: str, error: str) -> None:
        """The job back to its status with the run recorded as ``failed`` (``error`` English, for
        job_status), the owner told why (``reason``) and the proposal edited back."""
        job = run.job
        try:
            with self._store.transaction():
                self._store.transition_job(
                    job.id,
                    ("slicing",),
                    status=run.prev_status,
                    variants_json=_failed_record(run.previous, error, self._clock()),
                    error=error,
                )
                self._enqueue(
                    "job_failed",
                    f"❌ Подбор вариантов для «{job_name(job.name)}» не удался: {reason}",
                    _job_key(job.id, "eval-failed"),
                )
        except Exception:
            log.exception("job %d: recording a failed evaluation did not work", job.id)
            return
        if run.message_id is not None:
            await self._restore_proposal(job.id, run.message_id, run.before_text, run.before_buttons)

    async def _restore_proposal(
        self, job_id: int, message_id: str | None, before_text: str, before_buttons: list[list[Button]] | None
    ) -> None:
        """Edit the pressed proposal back after a run, rendered from the job as it is now (its
        goal, no minutes of a slice ruling R5 cleared). ``before_text`` when that cannot be
        rendered or the slots are not known yet. A new message when the edit fails. Never raises.
        """
        try:
            text, buttons = before_text, before_buttons
            if self._state.trays:
                try:
                    fresh = self._job(job_id)
                    text, buttons = self._proposal(fresh, self._plan(fresh, None, DEFAULT_NAMED_SET))
                except Exception:
                    log.exception("job %d: rendering the proposal after a run failed, the old text goes back", job_id)
            edited = False
            if message_id is not None:
                try:
                    edited = await self._notifier.edit(message_id, text, buttons)
                except Exception:
                    log.exception("job %d: editing the proposal back raised", job_id)
            if not edited:
                self._enqueue("job_proposal", text, _job_key(job_id, "proposal"), buttons=buttons)
        except Exception:
            log.exception("job %d: restoring the proposal failed", job_id)

    async def _recover_evaluations(self) -> None:
        """Runs a stop or crash cut off: each job back to its status with the run recorded as
        ``failed``, and the owner told to press «Подобрать» again. Before ``_fail_interrupted_jobs``,
        which marks a job this could not give back. One job's failure never stops the others.
        The proposals are edited back once the printer reports its slots (``_restore_later``).
        """
        for job in self._store.jobs_with_status("slicing"):
            if _eval_marker(job) is None:
                continue
            try:
                self._recover_evaluation(job)
            except Exception:
                log.exception("job %d: giving back an interrupted evaluation failed", job.id)

    def _recover_evaluation(self, job: Job) -> None:
        marker = _eval_marker(job)
        assert marker is not None
        prev = marker.get("prev_status") if marker.get("prev_status") in CLAIMABLE else "new"
        previous = marker.get("previous") if isinstance(marker.get("previous"), str) else None
        with self._store.transaction():
            if not self._store.transition_job(
                job.id,
                ("slicing",),
                status=prev,
                variants_json=_failed_record(previous, EVAL_INTERRUPTED_ERROR, self._clock()),
                error=EVAL_INTERRUPTED_ERROR,
            ):
                return
            self._enqueue(
                "job_eval_interrupted",
                EVAL_INTERRUPTED_TEXT.format(name=job_name(job.name)),
                _job_key(job.id, "eval-interrupted"),
            )
        log.info("job %d: an evaluation cut off by a restart was given back", job.id)
        text = marker.get("before_text")
        if not isinstance(text, str) or not text:
            return
        try:
            buttons = _rows_buttons(marker.get("before_buttons"))
        except (KeyError, TypeError):
            buttons = None
        message_id = marker.get("message_id") if isinstance(marker.get("message_id"), str) else None
        task = asyncio.create_task(self._restore_later(job.id, message_id, text, buttons), name="bambu-eval-restore")
        self._press_tasks.add(task)
        task.add_done_callback(self._press_tasks.discard)

    async def _restore_later(
        self, job_id: int, message_id: str | None, before_text: str, before_buttons: list[list[Button]] | None
    ) -> None:
        """At start-up the slots are unknown until the printer reports: wait for that (at most
        ``restore_wait_s``), then restore the proposal."""
        if not self._state.trays:
            waiter = asyncio.Event()
            self._report_waiters.add(waiter)
            try:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(waiter.wait(), self._restore_wait_s)
            finally:
                self._report_waiters.discard(waiter)
        await self._restore_proposal(job_id, message_id, before_text, before_buttons)

    async def _on_eval_press(self, press: Press) -> None:
        """``job:<id>:eval``: run the evaluation in the background, the proposal says so meanwhile."""
        job_id = _eval_press(press.button_id)
        assert job_id is not None
        job = self._store.job(job_id)
        if job is None:
            log.warning("eval press for unknown job %d ignored", job_id)
            return
        if job.status == "slicing":
            await self._notifier.edit(press.message_id, self._busy_text(job))
            return
        try:
            run = self._claim_eval(job.id, None, press.message_id)
        except JobBusy:
            await self._notifier.edit(press.message_id, self._busy_text(self._job(job.id)))
            return
        except ValueError as exc:
            log.warning("job %d: eval press refused: %s", job.id, exc)
            text = f"Не получилось подобрать варианты для «{job_name(job.name)}»: {md_literal(str(exc))}"
            self._enqueue("job_eval", text, _job_key(job.id, "eval"))
            return
        await self._notifier.edit(press.message_id, f"{run.before_text}\n{EVALUATING_TEXT}", run.before_buttons)
        self._spawn_eval(run)

    # -- jobs: resetting the owner's explicit settings ---------------------------

    async def reset_settings(self, job_id: int, keys: Sequence[str] | None = None) -> dict[str, object]:
        """Clear the owner's explicit settings (``explicit_json``): all, or only ``keys``.

        Returns the job's settings resolved after the reset. Nothing is sliced and the PC is
        not woken. ``KeyError`` (unknown job), ``JobBusy`` (slicing or evaluating),
        ``ValueError`` when the settings then do not resolve (a stored slot was emptied).
        """
        job = self._job(job_id)
        if job.status not in CLAIMABLE:
            raise JobBusy(f"job {job.id} is being sliced or evaluated already")
        remaining = {} if keys is None else {k: v for k, v in _explicit(job).items() if k not in set(keys)}
        # A slice or a run claimed in the meantime keeps its own plan.
        if not self._store.transition_job(job.id, CLAIMABLE, explicit_json=json.dumps(remaining) if remaining else None):
            raise JobBusy(f"job {job.id} is being sliced or evaluated already")
        return self._plan(self._job(job.id), None, DEFAULT_NAMED_SET).settings

    async def _on_reset_press(self, press: Press) -> None:
        """``job:<id>:reset``: clear the explicit layer and edit the proposal in place. Never slices."""
        job_id = _reset_press(press.button_id)
        assert job_id is not None
        job = self._store.job(job_id)
        if job is None:
            log.warning("reset press for unknown job %d ignored", job_id)
            return
        if job.status == "slicing":
            await self._notifier.edit(press.message_id, self._busy_text(job))
            return
        if not self._store.transition_job(job.id, CLAIMABLE, explicit_json=None):
            await self._notifier.edit(press.message_id, self._busy_text(self._job(job.id)))
            return
        try:
            fresh = self._job(job.id)
            text, buttons = self._proposal(fresh, self._plan(fresh, None, DEFAULT_NAMED_SET))
        except ValueError as exc:  # SlotUnusable too: the stored slot was emptied since
            log.warning("job %d: the proposal after a reset failed: %s", job.id, exc)
            text = f"Мои настройки для «{job_name(job.name)}» сброшены, но предложение не собралось: {md_literal(str(exc))}"
            self._enqueue("job_reset", text, _job_key(job.id, "reset"))
            return
        if not await self._notifier.edit(press.message_id, text, buttons):
            log.warning("job %d: editing the proposal failed, sending it again", job.id)
            self._enqueue("job_proposal", text, _job_key(job.id, "proposal"), buttons=buttons)

    async def choose_variant(self, job_id: int, n: int) -> str:
        """Make variant ``n`` of the job's last evaluation its ``variant`` layer and queue the
        proposal. Returns its text. ``KeyError`` (unknown job), ``ValueError`` (no evaluation,
        no such variant or one that failed), ``JobBusy`` (slicing or evaluating)."""
        job = self._job(job_id)
        if job.status not in CLAIMABLE:
            raise JobBusy(f"job {job.id} is being sliced or evaluated already")
        text, buttons = self._choose(job, n)
        self._enqueue("job_proposal", text, _job_key(job.id, "proposal"), buttons=buttons)
        return text

    def _choose(self, job: Job, n: int) -> tuple[str, list[list[Button]]]:
        data = _variants(job)
        if data is None:
            raise ValueError(f"job {job.id} has no evaluation: run evaluate_job first")
        entries = data["variants"]
        if type(n) is not int or not 1 <= n <= len(entries):
            raise ValueError(f"variant {n} is out of range 1..{len(entries)}")
        entry = next(v for v in entries if v["n"] == n)
        if entry.get("status") != "ok" or not isinstance(entry.get("settings"), dict):
            raise ValueError(f"variant {n} failed to slice and cannot be chosen")
        data["chosen"] = n
        fields = {
            "variant_json": json.dumps(entry["settings"]),
            "variants_json": json.dumps(data, ensure_ascii=False),
        }
        # A slice or a run claimed in the meantime keeps its own plan.
        if not self._store.transition_job(job.id, CLAIMABLE, **fields):
            raise JobBusy(f"job {job.id} is being sliced or evaluated already")
        fresh = self._job(job.id)
        return self._proposal(fresh, self._plan(fresh, None, DEFAULT_NAMED_SET))

    async def _on_variant_press(self, press: Press) -> None:
        """``job:<id>:variant:<n>``: choose the variant and edit the proposal in place."""
        parsed = _variant_press(press.button_id)
        assert parsed is not None
        job_id, n = parsed
        job = self._store.job(job_id)
        if job is None:
            log.warning("variant press for unknown job %d ignored", job_id)
            return
        if job.status == "slicing":
            await self._notifier.edit(press.message_id, self._busy_text(job))
            return
        try:
            text, buttons = self._choose(job, n)
        except JobBusy:
            await self._notifier.edit(press.message_id, self._busy_text(self._job(job.id)))
            return
        except ValueError as exc:  # SlotUnusable too: the stored slot was emptied since
            log.warning("job %d: variant press refused: %s", job.id, exc)
            text = f"Не получилось выбрать вариант для «{job_name(job.name)}»: {md_literal(str(exc))}"
            self._enqueue("job_variant", text, _job_key(job.id, "variant"))
            return
        if not await self._notifier.edit(press.message_id, text, buttons):
            log.warning("job %d: editing the proposal failed, sending it again", job.id)
            self._enqueue("job_proposal", text, _job_key(job.id, "proposal"), buttons=buttons)

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
            raise PrintRefused([f"Не удалось забрать нарезанный файл с ПК: {exc.text}"], str(exc)) from None
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
            profile=self._settings.profile,
        )
        if check.blocking:
            raise PrintRefused(check.blocking)
        try:
            cost = self._predict(info.grams, info.minutes, target.spool, self._print_material(target, info))
        except Exception:
            log.exception("job %d: the confirmation's cost failed, sent without it", target.job.id)
            cost = None
        text = render_start_confirmation(
            target.job.name,
            plate=plate if target.job.kind == "kit" else None,
            slot_choice=self._label_choice(target),
            minutes=info.minutes,
            grams=info.grams,
            plate_type=target.plate_type,
            warnings=check.warnings,
            cost=cost,
            symbol=self._store.currency()[1],
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
                "print_confirm",
                text,
                f"start|{_id_tag(confirmation.id)}",
                buttons=start_buttons(confirmation.id),
                expires_at=confirmation.expires_at,
                ref=f"confirm:{confirmation.id}",
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
        except KeyError:
            log.warning("job %d plate %d: print press for a job that is gone", job.id, plate)
            lines = [NO_JOB_TEXT]
        except ValueError as exc:
            log.warning("job %d plate %d: print press refused: %s", job.id, plate, exc)
            lines = [NO_PLATE_TEXT.format(plate=plate)]
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

    async def _answer_spent(self, press: Press, confirm_id: str, texts: Mapping[str, str] = _SPENT_TEXTS) -> None:
        """Answer a press on a used, expired or cancelled confirmation, in place or as a new message."""
        confirmation = self._store.confirmation(confirm_id)
        text = texts.get(confirmation.state) if confirmation is not None else None
        if confirmation is not None and confirmation.outcome == REEVALUATED_OUTCOME:
            text = REEVALUATED_TEXT
        if text is None:
            return
        assert confirmation is not None
        try:
            edited = await self._notifier.edit(press.message_id, text)
        except Exception:
            log.exception("answering a spent %s press in place raised", confirmation.kind)
            edited = False
        if edited:
            return
        log.warning("answering a spent %s press in place failed, sending a new message", confirmation.kind)
        self._enqueue(
            f"{confirmation.kind}_spent", text, f"{confirmation.kind}|{_id_tag(confirm_id)}|spent|{press.seq}"
        )

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
            except _StartSent:
                try:
                    self._record_unconfirmed(confirm_id, name)
                except Exception:
                    log.exception("recording an unconfirmed print start failed")
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
                self._enqueue("print_refused", text, f"start|{_id_tag(confirm_id)}|refused")
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
        upload = asyncio.to_thread(
            self._uploader, secrets.host, secrets.serial, secrets.access_code, settings.ca_file, pinned, target.file
        )
        try:
            # A worker thread cannot be killed: past the deadline it is left to finish or fail
            # on its own, and nothing is sent to the printer for this press.
            await asyncio.wait_for(upload, self._upload_timeout_s)
        except TimeoutError:
            log.warning("the upload did not finish in %.0f s, the print is not started", self._upload_timeout_s)
            raise _StartRefused("upload_timeout", [UPLOAD_TIMEOUT_TEXT]) from None
        except UploadError as exc:
            log.warning("the upload failed: %s", exc)
            raise _StartRefused("upload", [f"Файл не загрузился на принтер: {exc.text}"]) from None
        except ValueError as exc:  # an unsafe remote name: the slice result is odd
            log.warning("the upload was refused: %s", exc)
            raise _StartRefused("upload", ["Файл не загрузился на принтер: недопустимое имя файла"]) from None
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
        except Exception:
            # Raised after the publish may have happened: watched like a clean send.
            log.exception("sending project_file raised, it may have reached the printer")
        try:
            return await self._watch_start(confirmation.id, target, watch)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("watching the print start failed")
            raise _StartSent() from None

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
            profile=self._settings.profile,
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
        if not started:
            self._record_unconfirmed(confirm_id, target.job.name)
            return "unconfirmed"
        plate = target.plate if target.job.kind == "kit" else None
        with self._store.transaction():
            self._store.set_confirmation_outcome(confirm_id, "started")
            if not watch.announced:  # the STARTED event normally carries the stop button
                self._enqueue(
                    "print_started",
                    render_print_started(target.job.name, plate),
                    f"start|{_id_tag(confirm_id)}|started",
                    buttons=control_buttons(watch.task_id, False),
                    ref=f"control:{watch.task_id}",
                )
                self._control[watch.task_id] = False
        return "started"

    def _record_unconfirmed(self, confirm_id: str, name: str | None) -> None:
        """The outcome ``unconfirmed`` and the owner's message, saying what the printer last sent."""
        state = self._state
        with self._store.transaction():
            self._store.set_confirmation_outcome(confirm_id, "unconfirmed")
            self._enqueue(
                "print_unconfirmed",
                render_start_unconfirmed(name, self._watch_s, state.gcode_state, state.subtask_name),
                f"start|{_id_tag(confirm_id)}|unconfirmed",
            )

    # -- printing: pause and the confirmed stop (phase 3) -----------------------------

    def start_prepare(self, job_id: int, plate: int) -> bool:
        """``prepare_print`` for the Mini App, in the background: False while one runs.

        Its refusal or failure becomes a Russian line in ``app_notes`` (the tab shows it), and the
        confirmation itself appears in the snapshot's pending list once it went out.
        """
        if self._preparing is not None and not self._preparing.done():
            return False
        self._preparing = asyncio.create_task(self._prepare_for_app(job_id, plate), name=f"bambu-app-prepare-{job_id}")
        self._press_tasks.add(self._preparing)
        self._preparing.add_done_callback(self._press_tasks.discard)
        return True

    async def _prepare_for_app(self, job_id: int, plate: int) -> None:
        try:
            await self.prepare_print(job_id, plate)
        except PrintRefused as exc:
            self._app_note("Печать не подготовлена: " + ". ".join(_plain(line).rstrip(".") for line in exc.lines))
        except KeyError:
            self._app_note(f"Печать не подготовлена: задание {job_id} не найдено")
        except ValueError as exc:
            self._app_note(f"Печать не подготовлена: {_plain(str(exc))}")
        except Exception:
            log.exception("job %d: preparing for the Mini App failed", job_id)
            self._app_note(f"Печать не подготовлена: {INTERNAL_TEXT}")

    def _app_note(self, text: str) -> None:
        self.app_notes.append({"at": self._clock().timestamp(), "text": text})

    def send_snapshot(self) -> bool:
        """One camera frame to the owner's Telegram, at most one queued a minute."""
        minute = int(self._clock().timestamp() // 60)
        return self._enqueue("snapshot", SNAPSHOT_TEXT, f"snap|{minute}", snapshot=True)

    async def pause_print(self) -> dict[str, str | None]:
        """Pause the running print, once. Only while ``RUNNING``. Resume is the owner's button only.

        Raises ``ControlRefused`` (English) when nothing was sent.
        """
        if not self.state_fresh:
            await self._refresh_state()
        # Synchronous from here on: no await between the checks and the send.
        link = self._control_link()
        state = self._state
        if state.gcode_state != "RUNNING":
            raise ControlRefused(
                f"the printer is {state.gcode_state}: pause works only while RUNNING, nothing was sent"
            )
        try:
            link.pause()
        except PrinterNotConnected:
            raise ControlRefused("the printer link is not connected, nothing was sent") from None
        log.info("pause sent")
        return {"task_id": state.task_id, "job_name": state.subtask_name}

    async def request_stop(self) -> StopRequest:
        """Send the stop confirmation for the current print to Telegram. Stops nothing itself.

        Only the owner's press on "Да, остановить" sends ``stop``. Raises ``ControlRefused``
        (English) when there is no print to stop, and then nothing is stored or sent.
        """
        if not self.state_fresh:
            await self._refresh_state()
        problem = self._stop_problem()
        if problem is not None:
            raise ControlRefused(problem.error)
        return self._ask_stop()

    def _control_link(self) -> Link:
        link = self._link
        if link is None or self._link_status is not LinkStatus.CONNECTED:
            raise ControlRefused(
                f"the printer is not connected (link {self._link_status.value}), nothing was sent"
            )
        if not self.state_fresh:
            raise ControlRefused(
                "waiting for the printer's first report since it connected, try again in a few seconds; "
                "nothing was sent"
            )
        return link

    def _stop_problem(self, task_id: str | None = None, name: str | None = None) -> _StopProblem | None:
        """Why the current print cannot be stopped now, None when it can.

        ``task_id`` and ``name``, when given, are the print the confirmation was asked for.
        """
        if self._link is None or self._link_status is not LinkStatus.CONNECTED:
            return _StopProblem(
                "not_connected",
                f"the printer is not connected (link {self._link_status.value}), nothing was sent",
                STOP_NOT_CONNECTED_TEXT,
            )
        if not self.state_fresh:
            return _StopProblem(
                "not_fresh",
                "waiting for the printer's first report since it connected, try again in a few seconds; "
                "nothing was sent",
                STOP_NOT_FRESH_TEXT,
            )
        state = self._state
        if state.gcode_state not in self._settings.profile.stoppable_states:  # PREPARE: heating, levelling
            return _StopProblem(
                "print_over", f"the printer is {state.gcode_state}: there is no print to stop", STOP_OVER_TEXT
            )
        if (task_id is not None and (state.task_id or "") != task_id) or (
            name is not None and (state.subtask_name or "") != name
        ):
            return _StopProblem("print_changed", "another print is running now", STOP_OTHER_TEXT)
        return None

    def _ask_stop(self) -> StopRequest:
        """Store a stop confirmation bound to the current print and queue its message."""
        state = self._state
        task_id, name = state.task_id or "", state.subtask_name or ""
        text = render_stop_confirmation(name)
        with self._store.transaction():
            confirmation = self._store.add_confirmation(
                None,
                "stop",
                job_id=None,
                plate=None,
                gate_hash=None,
                payload_json=json.dumps({"task_id": task_id, "subtask_name": name}),
                now=self._clock().timestamp(),
            )
            self._enqueue(
                "stop_confirm",
                text,
                f"stop|{_id_tag(confirmation.id)}",
                buttons=stop_buttons(confirmation.id),
                expires_at=confirmation.expires_at,
                ref=f"confirm:{confirmation.id}",
            )
        log.info("stop confirmation sent")
        return StopRequest(confirmation.id, text)

    async def _on_stop_press(self, press: Press) -> None:
        """``stop:ask:<task id>`` asks, ``stop:<id>:go`` sends ``stop`` at most once, ``:cancel`` cancels."""
        parsed = _stop_press(press.button_id)
        if parsed is None:
            log.warning("malformed stop button ignored")
            return
        key, action = parsed
        if action == "ask":
            await self._on_stop_ask(key)
            await self._put_control(key)
            return
        confirmation = self._store.confirmation(key)
        if confirmation is None or confirmation.kind != "stop":
            log.warning("stop press for an unknown confirmation ignored")
            return
        if action == "cancel":
            if self._store.cancel_confirmation(key):
                self._store.set_confirmation_outcome(key, "cancelled")
                await self._notifier.edit(press.message_id, STOP_CANCELLED_TEXT)
            else:
                await self._answer_spent(press, key, _STOP_SPENT_TEXTS)
            return
        # The claim comes first: a double press, a replay after a restart or a press on an
        # expired confirmation gets nothing past this line.
        claimed = self._store.claim_confirmation(key, self._clock().timestamp())
        if claimed is None:
            await self._answer_spent(press, key, _STOP_SPENT_TEXTS)
            return
        outcome, text = await self._stop_claimed(claimed)
        try:
            self._store.set_confirmation_outcome(key, outcome)
        except Exception:
            log.exception("recording a stop outcome failed")
        log.info("stop press: %s", outcome)
        await self._notifier.edit(press.message_id, text)

    async def _stop_claimed(self, confirmation: Any) -> tuple[str, str]:
        """(outcome, the owner's answer) of a claimed stop. Sends ``stop`` at most once."""
        try:
            payload = json.loads(confirmation.payload_json)
        except ValueError:
            payload = None
        task_id = payload.get("task_id") if isinstance(payload, dict) else None
        name = payload.get("subtask_name") if isinstance(payload, dict) else None
        if not isinstance(task_id, str) or not isinstance(name, str):
            return "refused:internal", f"Остановка не отправлена: {INTERNAL_TEXT}"
        if not self.state_fresh:
            await self._refresh_state()
        # Synchronous from here on: no await between the checks and the one send.
        problem = self._stop_problem(task_id, name)
        if problem is not None:
            return f"refused:{problem.reason}", problem.text
        link = self._link
        assert link is not None  # _stop_problem
        try:
            link.stop_print()
        except PrinterNotConnected:
            return "refused:not_connected", STOP_NOT_CONNECTED_TEXT
        except Exception:
            # Raised after the publish may have happened: never sent again, the owner looks.
            log.exception("sending stop raised, it may have reached the printer")
            return "unconfirmed", STOP_UNSURE_TEXT
        return "sent", STOP_SENT_TEXT

    async def _on_control_press(self, press: Press) -> None:
        """``pause:<task id>`` pauses, ``resume:<task id>`` resumes, each only for the current print
        and in its state. The message's buttons are put back after the press either way."""
        parsed = _control_press(press.button_id)
        assert parsed is not None  # _handle_press
        action, task_id = parsed
        if not self.state_fresh:
            await self._refresh_state()
        refusal, paused_after = self._control_refusal(action, task_id), None
        if refusal is None:
            link = self._link
            assert link is not None  # _control_refusal
            try:
                if action == "pause":
                    link.pause()
                    paused_after = True
                else:
                    link.resume()
                    self._resumed.add(task_id)
                    paused_after = False
                log.info("%s sent from its button", action)
            except PrinterNotConnected:
                refusal = STOP_NOT_CONNECTED_TEXT.replace("остановка", "команда")
        if refusal is not None:
            log.info("%s press refused", action)
            self._enqueue("control_refused", f"{'⏸' if action == 'pause' else '▶️'} {refusal}", f"control|{uuid.uuid4().hex}")
        await self._put_control(task_id, paused_after)

    def _control_refusal(self, action: str, task_id: str) -> str | None:
        """Why a pause or resume press can't be sent now, None when it can."""
        if self._link is None or self._link_status is not LinkStatus.CONNECTED:
            return STOP_NOT_CONNECTED_TEXT.replace("остановка", "команда")
        if not self.state_fresh:
            return STOP_NOT_FRESH_TEXT.replace("остановка", "команда")
        state = self._state
        if (state.task_id or "") != task_id:
            return CONTROL_OTHER_TEXT
        if action == "pause" and state.gcode_state != "RUNNING":
            return CONTROL_NOT_RUNNING_TEXT
        if action == "resume":
            if state.gcode_state != "PAUSE":
                return CONTROL_NOT_PAUSED_TEXT
            if task_id in self._resumed:
                return CONTROL_RESUME_SENT_TEXT
        return None

    async def _put_control(self, task_id: str, paused: bool | None = None) -> None:
        """Put a print's control message back as it was sent, with the buttons for ``paused``
        (the printer's state when None), or with none once that print is over."""
        sent = self._store.sent_message(f"control:{task_id}")
        if sent is None:
            return
        state = self._state
        live = (state.task_id or "") == task_id and state.gcode_state in self._settings.profile.stoppable_states
        if live:
            if paused is None:
                paused = state.gcode_state == "PAUSE"
            self._control[task_id] = paused
            buttons = control_buttons(task_id, paused)
        else:
            self._control.pop(task_id, None)
            self._resumed.discard(task_id)
            buttons = []
        await self._notifier.edit(sent.message_id, sent.text, buttons=buttons)

    def _sync_control(self, state: PrinterState) -> None:
        """After a state report: control messages whose buttons no longer fit the printer get
        them changed (a pause from the printer's screen shows Resume, an ended print none)."""
        if state.gcode_state != "PAUSE":
            self._resumed.discard(state.task_id or "")
        for task_id, paused in list(self._control.items()):
            live = (state.task_id or "") == task_id and state.gcode_state in self._settings.profile.stoppable_states
            if live and paused == (state.gcode_state == "PAUSE"):
                continue
            if live and task_id in self._resumed:
                continue  # resume sent, the buttons already say Pause
            if not live:
                self._control.pop(task_id, None)
            else:
                self._control[task_id] = state.gcode_state == "PAUSE"
            task = asyncio.create_task(self._put_control(task_id), name="bambu-control-buttons")
            self._press_tasks.add(task)
            task.add_done_callback(self._press_tasks.discard)

    async def _on_stop_ask(self, task_id: str) -> None:
        """The "Остановить…" button of a print this service started: a new stop confirmation."""
        if not self.state_fresh:
            await self._refresh_state()
        problem = self._stop_problem()
        if problem is None and (self._state.task_id or "") != task_id:
            problem = _StopProblem("print_over", "the print of this button is over", STOP_OVER_TEXT)
        if problem is not None:
            log.info("stop ask press refused: %s", problem.reason)
            self._enqueue("stop_refused", f"⏹ {problem.text}", f"stop-ask|{uuid.uuid4().hex}")
            return
        self._ask_stop()


def _id_tag(confirm_id: str) -> str:
    """A confirmation's stand-in for dedupe keys: the id itself never leaves the database."""
    return hashlib.sha256(confirm_id.encode("ascii")).hexdigest()[:16]


def _control_task(task_id: str | None) -> bool:
    """Whether a print's task id can carry control buttons: digits, and not "0", which the
    printer reports for prints without a task of their own and so tells no two prints apart."""
    return bool(task_id) and task_id != "0" and TASK_ID_RE.fullmatch(task_id) is not None


def _plain(text: str) -> str:
    """A Markdown-escaped line as plain text, for the Mini App."""
    return re.sub(r"\\(.)", r"\1", text)


_CONTROL_PRESS_RE = re.compile(r"(pause|resume):([0-9]{1,19})", re.ASCII)


def _control_press(button_id: str) -> tuple[str, str] | None:
    """(``pause`` or ``resume``, task id) of a control button, else None."""
    match = _CONTROL_PRESS_RE.fullmatch(button_id)
    return (match.group(1), match.group(2)) if match else None


def _stop_press(button_id: str) -> tuple[str, str] | None:
    """(task id, ``ask``) of ``stop:ask:<task id>``, (confirmation id, ``go`` or ``cancel``)
    of ``stop:<id>:<action>``, else None."""
    parts = button_id.split(":")
    if len(parts) != 3 or parts[0] != "stop":
        return None
    if parts[1] == "ask":
        return (parts[2], "ask") if TASK_ID_RE.fullmatch(parts[2]) else None
    if parts[2] not in ("go", "cancel") or not _CONFIRM_ID_RE.fullmatch(parts[1]):
        return None
    return parts[1], parts[2]


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


def _goal_press(button_id: str) -> tuple[int, str] | None:
    """(job id, goal key) of a ``job:<id>:goal:<key>`` button, None for any other button."""
    parts = button_id.split(":")
    if len(parts) != 4 or parts[0] != "job" or parts[2] != "goal":
        return None
    if not (parts[1].isascii() and parts[1].isdigit()) or parts[3] not in GOAL_KEYS:
        return None
    return int(parts[1]), parts[3]


def _eval_press(button_id: str) -> int | None:
    """The job id of a ``job:<id>:eval`` button, None for any other button."""
    parts = button_id.split(":")
    if len(parts) != 3 or parts[0] != "job" or parts[2] != "eval":
        return None
    if not (parts[1].isascii() and parts[1].isdigit() and len(parts[1]) <= 18):
        return None
    return int(parts[1])


def _reset_press(button_id: str) -> int | None:
    """The job id of a ``job:<id>:reset`` button, None for any other button."""
    parts = button_id.split(":")
    if len(parts) != 3 or parts[0] != "job" or parts[2] != "reset":
        return None
    if not (parts[1].isascii() and parts[1].isdigit() and len(parts[1]) <= 18):
        return None
    return int(parts[1])


def _variant_press(button_id: str) -> tuple[int, int] | None:
    """(job id, variant number) of a ``job:<id>:variant:<n>`` button, None for any other button."""
    parts = button_id.split(":")
    if len(parts) != 4 or parts[0] != "job" or parts[2] != "variant":
        return None
    if not (parts[1].isascii() and parts[1].isdigit() and len(parts[1]) <= 18):
        return None
    if not (parts[3].isascii() and parts[3].isdigit() and len(parts[3]) <= 2):
        return None
    return int(parts[1]), int(parts[3])


def _chosen_fields(slot: int | None, goal: tuple[str, str | None] | None) -> dict[str, Any]:
    """Job fields for the owner's slot and goal: a new goal drops the chosen variant and the
    evaluation (its variants were scored for the old goal)."""
    chosen: dict[str, Any] = {"slot": slot} if slot is not None else {}
    if goal is not None:
        chosen.update(goal_json=goal_record(*goal), variant_json=None, variants_json=None)
    return chosen


def _variants(job: Job) -> dict | None:
    """The job's last evaluation run (``jobs.variants_json``), None when it has none."""
    try:
        data = json.loads(job.variants_json or "null")
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("variants"), list):
        return None
    if not all(isinstance(v, dict) and type(v.get("n")) is int for v in data["variants"]):
        return None
    return data


def _loads_dict(text: str | None) -> dict | None:
    try:
        value = json.loads(text or "null")
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _explicit(job: Job) -> dict[str, object]:
    """The owner's explicit settings stored with the job (``explicit_json``), {} when none."""
    return _loads_dict(job.explicit_json) or {}


def _failed_record(previous: str | None, error: str, at: datetime) -> str:
    """``variants_json`` after a run that failed or was cut off: state ``failed`` with the
    English ``error`` and when. A previous run's variants stay (their buttons still work),
    but the state says they are not this run's result."""
    data = _loads_dict(previous) or {}
    if data.get("state") == "running":
        data = {}
    record = {k: v for k, v in data.items() if k not in ("state", "error", "failed_at")}
    record.update(state="failed", error=error, failed_at=at.astimezone(UTC).isoformat())
    return json.dumps(record, ensure_ascii=False)


def _chosen_entry(data: dict) -> dict | None:
    """The chosen variant of an evaluation when it sliced, with usable minutes and grams."""
    entry = next((v for v in data["variants"] if v.get("n") == data.get("chosen")), None)
    if entry is None or entry.get("status") != "ok" or not isinstance(entry.get("settings"), dict):
        return None
    for key in ("minutes", "grams"):
        value = entry.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            return None
    return entry


def _eval_marker(job: Job) -> dict | None:
    """The running marker an evaluation run keeps in ``variants_json``, None when none runs."""
    data = _loads_dict(job.variants_json)
    return data if data is not None and data.get("state") == "running" else None


def _last_primary(history: list[Job], name: str, exclude_id: int | None = None) -> str | None:
    """The primary goal of the job ``last_settings`` takes the settings from (``history`` newest
    first, ``exclude_id`` skipped as there)."""
    for job in history:
        if job.name != name or job.status != "sliced" or job.id == exclude_id or not job.settings_json:
            continue
        try:
            if not isinstance(json.loads(job.settings_json), dict):
                continue
            return job_goal(job, parse_manifest(json.loads(job.manifest_json)))[0]
        except (ValueError, RecursionError, ManifestError):
            return None
    return None


def _variant(job: Job) -> dict | None:
    """The job's chosen evaluation variant (a settings layer), None when it has none."""
    try:
        variant = json.loads(job.variant_json or "null")
    except (ValueError, RecursionError):
        return None
    return variant if isinstance(variant, dict) else None


def _proposal(
    job: Job,
    plan: SlicePlan,
    cost: CostBreakdown | CurrencyMismatch | None,
    symbol: str,
    *,
    code: str | None = None,
    stale: bool = False,
) -> tuple[str, list[list[Button]]]:
    """The proposal's text and buttons: slice and change, the slot rows, the goal rows and,
    for a part, «Подобрать», then the variant row after an evaluation. At most
    ``MAX_BUTTONS``: the slot rows give way, and the goal rows to the variant row.
    ``stale``: the cost's time is from a slice made with other settings."""
    explicit = _explicit(job)
    text = render_proposal(
        job, plan.settings, plan.choice, plan.plate_type, None, cost, symbol=symbol, goal=plan.goal,
        stale_minutes=stale, explicit=explicit,
    )
    for warning in (goal_warning(plan.goal[0], plan.settings), _preset_warning(plan)):
        if warning:
            text = f"{text}\n{warning}"
    choice = plan.choice
    chosen = None
    if choice.ams is not None and choice.slot is not None:
        chosen = slot_number(choice.ams, choice.slot)
    data = _variants(job)
    variants = variant_buttons(job.id, data["variants"], data.get("chosen")) if data is not None else []
    if data is not None:
        table = render_variants(
            data["variants"], data.get("chosen"), symbol, code=code, strength_unknown=bool(data.get("strength_unknown"))
        )
        text = "\n".join([text, *table])
    head = [Button(f"job:{job.id}:slice", SLICE_BUTTON_TEXT), Button(f"job:{job.id}:change", CHANGE_BUTTON_TEXT)]
    goals = goal_buttons(job.id, plan.goal[0])
    evaluate = eval_buttons(job.id) if plan.manifest.kind == "part" else []
    reset = reset_buttons(job.id) if explicit else []
    fixed = len(head) + sum(len(r) for r in (*evaluate, *variants, *reset))
    if variants and fixed + sum(len(r) for r in goals) + len(plan.usable) > MAX_BUTTONS:
        goals = []  # spec 5.8: after an evaluation the variant row replaces the goal rows
    elif reset and fixed + sum(len(r) for r in goals) + len(plan.usable) > MAX_BUTTONS:
        goals = _without_one_goal(goals, plan.goal[0])  # the reset button takes a goal's place
    room = MAX_BUTTONS - fixed - sum(len(r) for r in goals)
    rows = [head, *slot_buttons(job.id, plan.usable, chosen, room), *goals, *evaluate, *variants, *reset]
    return text, rows


def _without_one_goal(rows: list[list[Button]], current: str) -> list[list[Button]]:
    """The goal rows less their last button that is not the ``current`` goal."""
    flat = [b for row in rows for b in row]
    drop = next(i for i in range(len(flat) - 1, -1, -1) if not flat[i].id.endswith(f":goal:{current}"))
    flat = flat[:drop] + flat[drop + 1:]
    return [flat[i : i + 4] for i in range(0, len(flat), 4)]


def _sliced_minutes(job: Job) -> dict[int, float] | None:
    """The minutes per plate of the job's last slice, None when it has none."""
    try:
        result = json.loads(job.result_json or "null")
    except (ValueError, RecursionError):
        return None
    plates = result.get("plates") if isinstance(result, dict) else None
    if not isinstance(plates, list):
        return None
    minutes: dict[int, float] = {}
    for p in plates:
        m = p.get("minutes") if isinstance(p, dict) else None
        if type(p.get("index") if isinstance(p, dict) else None) is not int:
            return None
        if type(m) not in (int, float) or not math.isfinite(m) or m < 0:
            return None
        minutes[p["index"]] = float(m)
    return minutes or None


def _cost_json(index: int, cost: CostBreakdown | CurrencyMismatch) -> dict[str, Any]:
    """A plate's predicted cost in the job result (the ``_uah`` names predate currencies)."""
    if isinstance(cost, CurrencyMismatch):
        return {"index": index, "currency": cost.expected, "error": cost.reason}
    return {
        "index": index,
        "filament_uah": cost.plastic.amount if cost.plastic is not None else None,
        "power_uah": cost.power.amount if cost.power is not None else None,
        "amort": cost.amort.amount if cost.amort is not None else None,
        "kwh": cost.kwh,
        "total_uah": cost.total.amount,
        "currency": cost.currency,
    }


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
    settings = with_country(settings, store.country())  # its timezone, unless config.toml has one
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
