import asyncio
import copy
import hashlib
import io
import json
import logging
import os
import threading
import zipfile
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from bambu_mcp import jobs as jobs_module
from bambu_mcp import mcp_server
from bambu_mcp.config import Secrets, Settings
from bambu_mcp.messages import render_proposal, render_sliced
from bambu_mcp.names import slug
from bambu_mcp.notify import Button, LogNotifier, Press, SendError, ServiceNotifyClient
from bambu_mcp.printer.ftps import UploadError
from bambu_mcp.printer.link import LinkStatus, PrinterNotConnected, tray_setting
from bambu_mcp.printer.state import PrinterState, Tray
from bambu_mcp.printgate import gate_hash
from bambu_mcp.proposal import SlotChoice, SlotUnusable, estimate_cost
from bambu_mcp.service import (
    AUTH_FAILED_TEXT,
    CHANGE_TEXT,
    TARIFF_META_KEY,
    ControlRefused,
    JobBusy,
    PrepareResult,
    PrintRefused,
    Service,
    build_notifier,
    notify_dedupe,
)
from bambu_mcp.slicer import FetchError, SliceOutcome, WakeError
from bambu_mcp.store.db import Store

FIXTURES = Path(__file__).parent / "fixtures" / "p2s"
T0 = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
SECRETS = Secrets(access_code="12345678", serial="SERIALX", host="192.0.2.1")
JPEG = b"\xff\xd8fake-jpeg"


def report(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


class FakeLink:
    def __init__(self, on_report, on_status) -> None:
        self.on_report = on_report
        self.on_status = on_status
        self.started = False
        self.stopped = False
        self.trays: list[dict] = []
        self.full_reports = 0

    def start(self) -> None:
        self.started = True

    def set_tray(self, **fields) -> dict:
        self.trays.append(fields)
        return tray_setting(**fields)

    def request_full_report(self) -> None:
        self.full_reports += 1

    def stop(self) -> None:
        self.stopped = True


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[tuple[str, str | None]] = []
        self.edit_buttons: list[list[list[Button]] | None] = []
        self.edit_ok = True
        self.press_queue: asyncio.Queue[Press] = asyncio.Queue()
        self.fail = False
        self.attempts = 0

    async def send(self, text, buttons=None, files=None, dedupe=None, expires_in=None, silent=False):
        self.attempts += 1
        if self.fail:
            raise SendError(503, "telegram_unreachable")
        self.sent.append(
            {"text": text, "buttons": buttons or [], "files": files or [], "dedupe": dedupe, "expires_in": expires_in}
        )
        return f"m{len(self.sent)}"

    async def edit(self, message_id, text=None, buttons=None):
        self.edits.append((message_id, text))
        self.edit_buttons.append(buttons)
        return self.edit_ok

    async def presses(self):
        while True:
            yield await self.press_queue.get()

    def button_ids(self, index: int) -> list[str]:
        return [b.id for row in self.sent[index]["buttons"] for b in row]


async def until(predicate, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


class Harness:
    def __init__(self, store: Store, clock: Clock, notifier: RecordingNotifier, tmp: Path) -> None:
        self.store, self.clock, self.notifier = store, clock, notifier
        self.links: list[FakeLink] = []
        self.settings = Settings(data_dir=tmp)
        self.service: Service | None = None
        self.task: asyncio.Task | None = None
        self.sleep = asyncio.sleep
        self.slicer = None
        self.waker = None
        self.link_class = FakeLink
        self.service_kwargs: dict = {}

    def _factory(self, on_report, on_status) -> FakeLink:
        link = self.link_class(on_report, on_status)
        self.links.append(link)
        return link

    async def start(self) -> Service:
        async def camera() -> bytes | None:
            return JPEG

        self.service = Service(
            SECRETS,
            self.settings,
            self.store,
            self.notifier,
            self._factory,
            camera,
            clock=self.clock,
            sleep=self.sleep,
            slicer=self.slicer,
            waker=self.waker,
            **self.service_kwargs,
        )
        count = len(self.links)
        self.task = asyncio.create_task(self.service.run())
        await until(lambda: len(self.links) > count and self.links[-1].started)
        return self.service

    async def feed(self, *names: str, minutes: float = 1, deliver: bool = True) -> None:
        for name in names:
            await self.feed_report(report(name), minutes=minutes, deliver=deliver)

    async def feed_report(self, payload: dict, minutes: float = 1, deliver: bool = True) -> None:
        self.clock.advance(minutes=minutes)
        # The real link calls back from paho's network thread.
        await asyncio.to_thread(self.links[-1].on_report, payload)
        await self.service.settle()
        if deliver:
            await until(lambda: self.store.outbox_count() == 0)

    async def stop(self) -> None:
        await self.service.stop()
        await self.task


@pytest.fixture
async def harness(tmp_path):
    store = Store(tmp_path / "bambu.db")
    store.migrate()
    clock = Clock(T0)
    # Tariff reminder already sent today, so it stays out of these tests.
    store.set_meta(TARIFF_META_KEY, T0.date().isoformat())
    h = Harness(store, clock, RecordingNotifier(), tmp_path)
    yield h
    if h.task is not None and not h.task.done():
        await h.stop()
    store.close()


def texts(notifier: RecordingNotifier) -> list[str]:
    return [m["text"] for m in notifier.sent]


async def drain(service: Service) -> None:
    """Send every outbox message that is due now."""
    while await service.send_next() == 0:
        pass


async def test_lifecycle_notifies_once_each(harness):
    spool = harness.store.add_spool("Elegoo", "PETG", "Black", "#000000", None, 600.0, 1000.0)
    await harness.start()
    await harness.feed("pushall_idle", "version", "running_layer0", "gcode_line")
    await harness.feed("running_layer1", "running_layer2", "running_layer2", minutes=10)
    await harness.feed("running_mid", minutes=60)
    await harness.feed("finish", "finish", minutes=59)

    sent = harness.notifier.sent
    assert len(sent) == 5, texts(harness.notifier)
    started, first_layer, finished, fit, which_spool = sent

    assert started["text"].startswith("▶️ Печать началась\ncube, ")
    assert started["files"] == []
    assert first_layer["text"].startswith("🧱 Первый слой готов")
    assert first_layer["files"] == [("snapshot.jpg", JPEG)]
    assert finished["text"].startswith("✅ Печать завершена")
    assert "Длительность: 2 ч 30 мин" in finished["text"]  # layer0 -> finish: 1+30+60+59
    assert "Электричество" in finished["text"]
    assert finished["files"] == [("snapshot.jpg", JPEG)]
    for message in (started, first_layer, finished):
        assert message["dedupe"] and message["dedupe"].startswith("cube|1795385329|")

    row = harness.store.print_by_identity(("cube", "1795385329", None))
    assert row.source == "external"
    assert row.outcome == "finished"
    assert row.actual_min == pytest.approx(150)
    assert row.kwh == pytest.approx(150 / 60 * 0.2 + 0.05)
    assert row.cost_power_uah == pytest.approx(row.kwh * 4.32)
    assert row.started_at.tzinfo is not None and row.ended_at.tzinfo is not None

    assert harness.notifier.button_ids(3) == [
        f"fit:{row.id}:ok",
        f"fit:{row.id}:tight",
        f"fit:{row.id}:loose",
        f"fit:{row.id}:failed",
    ]
    assert fit["dedupe"] and which_spool["dedupe"] and fit["dedupe"] != which_spool["dedupe"]
    assert harness.notifier.button_ids(4) == [f"spool:{row.id}:none", f"spool:{row.id}:{spool}"]
    assert which_spool["buttons"][0][0].text == "🚫 Не списывать"
    assert harness.store.spools()[0].remaining_g == 1000.0  # nothing deducted in phase 1


async def test_restart_mid_print(harness):
    spool = harness.store.add_spool("Elegoo", "PETG", "Black", "#000000", None, 600.0, 1000.0)
    harness.store.map_slot(0, 2, spool)  # running reports have tray_now 2: AMS 0, slot 2
    first = await harness.start()
    await harness.feed("running_layer0", "running_layer1", "running_layer2")
    await harness.stop()
    assert harness.links[0].stopped
    assert [m["text"].splitlines()[0] for m in harness.notifier.sent] == [
        "▶️ Печать началась",
        "🧱 Первый слой готов",
    ]

    second = await harness.start()
    assert second is not first
    await harness.feed("running_layer2", "finish")

    heads = [m["text"].splitlines()[0] for m in harness.notifier.sent]
    assert heads.count("▶️ Печать началась") == 1
    assert heads.count("🧱 Первый слой готов") == 1
    assert heads[2] == "✅ Печать завершена"
    # The slot's spool is known: only the fit question, no spool question.
    assert len(harness.notifier.sent) == 4
    assert harness.notifier.button_ids(3)[0].startswith("fit:")

    rows = harness.store.history(None, 10)
    assert len(rows) == 1
    assert rows[0].outcome == "finished" and rows[0].ended_at is not None
    assert rows[0].spool_id == spool
    assert rows[0].actual_min == pytest.approx(4)


async def test_auth_failed_message_once(harness):
    service = await harness.start()
    link = harness.links[-1]
    await asyncio.to_thread(link.on_status, LinkStatus.CONNECTING)
    await asyncio.to_thread(link.on_status, LinkStatus.AUTH_FAILED)
    await asyncio.to_thread(link.on_status, LinkStatus.AUTH_FAILED)
    await until(lambda: service.link_status is LinkStatus.AUTH_FAILED)
    await service.settle()
    await until(lambda: harness.store.outbox_count() == 0)
    await asyncio.sleep(0.05)
    assert texts(harness.notifier) == [AUTH_FAILED_TEXT]
    assert harness.notifier.sent[0]["dedupe"]
    assert AUTH_FAILED_TEXT == "Принтер отклонил код доступа: проверь, не сменился ли он"


async def test_fit_press_updates_journal_and_edits_message(harness):
    spool = harness.store.add_spool("Elegoo", "PETG", "Black", "#000000", None, 600.0, 1000.0)
    await harness.start()
    await harness.feed("running_layer0", "running_layer2", "finish")
    row = harness.store.print_by_identity(("cube", "1795385329", None))
    fit_mid = f"m{len(harness.notifier.sent) - 1}"
    spool_mid = f"m{len(harness.notifier.sent)}"
    queue = harness.notifier.press_queue

    await queue.put(Press(1, fit_mid, f"fit:{row.id}:tight", T0))
    await until(lambda: len(harness.notifier.edits) == 1)
    assert harness.store.print_by_identity(row.identity).fit == "tight"
    assert harness.notifier.edits[0] == (fit_mid, "Посадка: 🔒 Туго")

    await queue.put(Press(2, spool_mid, f"spool:{row.id}:{spool}", T0))
    await until(lambda: len(harness.notifier.edits) == 2)
    assert harness.store.print_by_identity(row.identity).spool_id == spool
    assert harness.notifier.edits[1] == (spool_mid, "Катушка: Elegoo PETG Black")

    # Malformed, unknown or foreign presses change nothing and edit nothing.
    for seq, button in enumerate(
        ("fit:x:ok", f"fit:{row.id}:great", "fit:999:ok", f"spool:{row.id}:999", "other:1:2"), 3
    ):
        await queue.put(Press(seq, "m1", button, T0))
    await queue.put(Press(9, spool_mid, f"spool:{row.id}:none", T0))
    await until(lambda: len(harness.notifier.edits) == 3)
    assert harness.notifier.edits[2] == (spool_mid, "Катушка: не списывать")
    final = harness.store.print_by_identity(row.identity)
    assert final.fit == "tight" and final.spool_id is None  # "не списывать" clears the spool


async def test_tariff_reminder_quarterly(tmp_path):
    store = Store(tmp_path / "bambu.db")
    store.migrate()
    clock = Clock(T0)
    notifier = RecordingNotifier()

    async def camera():
        return None

    service = Service(
        SECRETS, Settings(data_dir=tmp_path), store, notifier, FakeLink, camera, clock=clock
    )
    expected = "Проверь тариф на электроэнергию: сейчас 4.32 грн/кВт·ч с 01.06.2024"

    assert await service.check_tariff() is True
    await drain(service)
    assert texts(notifier) == [expected]
    assert notifier.sent[0]["dedupe"] == f"tariff-reminder|{T0.date().isoformat()}"
    assert await service.check_tariff() is False  # same day
    clock.advance(days=90)
    assert await service.check_tariff() is False
    clock.advance(days=1)
    assert await service.check_tariff() is True
    await drain(service)
    assert texts(notifier) == [expected, expected]
    assert store.get_meta(TARIFF_META_KEY) == (T0 + timedelta(days=91)).date().isoformat()

    # Current values: a newer two-zone tariff shows up in the next reminder.
    store.add_tariff(date(2026, 12, 1), 4.8, 2.4)
    clock.advance(days=91)
    assert await service.check_tariff() is True
    await drain(service)
    assert texts(notifier)[-1] == (
        "Проверь тариф на электроэнергию: сейчас день 4.80 / ночь 2.40 грн/кВт·ч с 01.12.2026"
    )
    store.close()


async def test_country_ua_keeps_reminder_other_disables(tmp_path):
    store = Store(tmp_path / "bambu.db")
    store.migrate()
    notifier = RecordingNotifier()

    async def camera():
        return None

    service = Service(
        SECRETS, Settings(data_dir=tmp_path), store, notifier, FakeLink, camera, clock=Clock(T0)
    )
    assert store.country() == "UA"
    store.set_country("PL")
    assert await service.check_tariff() is False
    assert store.get_meta(TARIFF_META_KEY) is None
    store.set_country("UA")
    assert await service.check_tariff() is True
    store.close()


async def test_tariff_reminder_is_queued_and_retried_by_the_outbox(tmp_path):
    store = Store(tmp_path / "bambu.db")
    store.migrate()
    notifier = RecordingNotifier()
    notifier.fail = True
    clock = Clock(T0)

    async def camera():
        return None

    service = Service(
        SECRETS, Settings(data_dir=tmp_path), store, notifier, FakeLink, camera, clock=clock
    )
    assert await service.check_tariff() is True
    assert store.get_meta(TARIFF_META_KEY) == T0.date().isoformat()
    assert await service.send_next() == 30
    assert store.outbox_count() == 1
    notifier.fail = False
    clock.advance(seconds=30)
    await drain(service)
    assert len(notifier.sent) == 1 and store.outbox_count() == 0
    store.close()


async def test_undelivered_event_logs_warning_without_text(harness, caplog):
    harness.notifier.fail = True
    await harness.start()
    with caplog.at_level(logging.WARNING, logger="bambu_mcp.service"):
        await harness.feed("running_layer0", deliver=False)
        await until(lambda: harness.notifier.attempts >= 1)
        await asyncio.sleep(0.01)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("started" in w and "not delivered" in w for w in warnings)
    assert harness.store.outbox_count() == 1  # kept for the next attempt
    assert not any("cube" in w for w in warnings)


async def test_missing_hms_file_warns_once(tmp_path, caplog):
    store = Store(tmp_path / "bambu.db")
    store.migrate()

    async def camera():
        return None

    with caplog.at_level(logging.WARNING, logger="bambu_mcp.service"):
        Service(SECRETS, Settings(data_dir=tmp_path), store, LogNotifier(), FakeLink, camera)
    assert sum("HMS" in r.getMessage() for r in caplog.records) == 1

    caplog.clear()
    (tmp_path / "hms_en_22E.json").write_text('{"hms": {}, "errors": {}}', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="bambu_mcp.service"):
        Service(SECRETS, Settings(data_dir=tmp_path), store, LogNotifier(), FakeLink, camera)
    assert not caplog.records
    store.close()


TRAY_FIELDS = dict(
    ams=0, slot=2, tray_info_idx="GFA00", tray_type="PLA", color_rgba="FF8800FF",
    nozzle_temp_min=190, nozzle_temp_max=230,
)


async def test_set_tray_goes_through_the_running_link_only(harness):
    store = harness.store

    async def camera():
        return None

    idle = Service(SECRETS, harness.settings, store, LogNotifier(), FakeLink, camera)
    with pytest.raises(PrinterNotConnected):
        idle.set_tray(**TRAY_FIELDS)
    with pytest.raises(PrinterNotConnected):
        idle.request_full_report()

    service = await harness.start()
    assert service.set_tray(**TRAY_FIELDS) == tray_setting(**TRAY_FIELDS)
    service.request_full_report()
    link = harness.links[-1]
    assert link.trays == [TRAY_FIELDS] and link.full_reports == 1
    await harness.stop()
    with pytest.raises(PrinterNotConnected):
        service.set_tray(**TRAY_FIELDS)
    assert link.trays == [TRAY_FIELDS]


async def _status(harness, status: LinkStatus) -> None:
    await asyncio.to_thread(harness.links[-1].on_status, status)
    await harness.service.settle()


async def test_load_spool_waits_for_a_fresh_report_after_a_reconnect(harness):
    """A drop makes the state UNKNOWN; after CONNECTED, load_spool waits for the next report."""
    store = harness.store
    spool_id = store.add_spool(
        "Bambu", "PLA", "Orange", "#FF8800", None, 600, 1000,
        tray_info_idx="GFA00", nozzle_temp_min=190, nozzle_temp_max=230,
    )

    async def camera():
        return None

    service = await harness.start()
    mcp = mcp_server.build_mcp(service, store, harness.settings, camera)
    link = harness.links[-1]

    async def load() -> str | None:
        """None when sent, else the refusal text."""
        try:
            result = await mcp.call_tool("load_spool", {"spool_id": spool_id, "ams": 0, "slot": 1})
        except ToolError as exc:  # MCPServer.call_tool raises the tool's refusal
            return str(exc)
        assert not result.is_error
        return None

    assert not service.state_fresh
    await _status(harness, LinkStatus.CONNECTED)
    assert "first report" in await load()  # connected, no report yet
    await harness.feed("pushall_idle")  # FINISH, one AMS unit
    assert service.state.gcode_state == "FINISH" and service.state_fresh
    assert await load() is None
    assert len(link.trays) == 1 and link.full_reports == 1

    await _status(harness, LinkStatus.DISCONNECTED)
    assert service.state.gcode_state == "UNKNOWN" and not service.state_fresh
    assert service.state.trays  # only gcode_state is forgotten
    await _status(harness, LinkStatus.CONNECTING)
    await _status(harness, LinkStatus.CONNECTED)
    refused = await load()
    assert "first report" in refused and "nothing was sent" in refused
    await harness.feed_report({"info": {"command": "get_version", "sequence_id": "0"}})
    assert "first report" in await load()  # not a state report
    assert len(link.trays) == 1

    await harness.feed("pushall_idle")
    assert service.state_fresh and service.state.gcode_state == "FINISH"
    assert await load() is None
    assert len(link.trays) == 2


async def test_link_drop_forgets_sdcard(harness):
    """Pre-flight trusts sdcard only from a report since the last CONNECTED."""
    service = await harness.start()
    await _status(harness, LinkStatus.CONNECTED)
    await harness.feed_report({"print": {"gcode_state": "IDLE", "sdcard": True, "task_id": "42"}})
    assert service.state.sdcard is True and service.state.task_id == "42"
    for status in (LinkStatus.CONNECTING, LinkStatus.DISCONNECTED, LinkStatus.AUTH_FAILED):
        await _status(harness, LinkStatus.CONNECTED)
        await harness.feed_report({"print": {"gcode_state": "IDLE", "sdcard": True, "task_id": "42"}})
        await _status(harness, status)
        assert service.state.sdcard is None, status
        assert service.state.task_id == "42"  # kept: see the next test
    await _status(harness, LinkStatus.CONNECTED)
    assert service.state.sdcard is None  # CONNECTED alone says nothing about the stick
    await harness.feed_report({"print": {"gcode_state": "IDLE", "sdcard": False}})
    assert service.state.sdcard is False


async def test_reconnect_report_without_task_id_does_not_announce_a_start(harness):
    """The job identity survives a drop: a first report without task_id is not a new job."""
    await harness.start()
    await _status(harness, LinkStatus.CONNECTED)
    await harness.feed("running_mid")
    await until(lambda: harness.store.outbox_count() == 0)
    sent = len(harness.notifier.sent)
    await _status(harness, LinkStatus.DISCONNECTED)
    await _status(harness, LinkStatus.CONNECTED)
    await harness.feed_report({"print": {"gcode_state": "RUNNING", "mc_percent": 51}})
    await until(lambda: harness.store.outbox_count() == 0)
    assert len(harness.notifier.sent) == sent


async def test_disconnect_does_not_announce_events(harness):
    """UNKNOWN on a drop is not a state change for the event detector (phase 1 logic)."""
    service = await harness.start()
    await _status(harness, LinkStatus.CONNECTED)
    await harness.feed("running_mid")
    sent = len(harness.notifier.sent)
    await _status(harness, LinkStatus.DISCONNECTED)
    await _status(harness, LinkStatus.CONNECTED)
    await harness.feed("running_mid")
    await until(lambda: harness.store.outbox_count() == 0)
    assert len(harness.notifier.sent) == sent
    assert service.state.gcode_state == "RUNNING"


def test_notify_dedupe_fits_the_api_limit():
    assert notify_dedupe("cube|1|2|started") == "cube|1|2|started"
    long_key = "x" * 200 + "|started"
    short = notify_dedupe(long_key)
    assert len(short) <= 128
    assert short == notify_dedupe(long_key)
    assert short != notify_dedupe("y" * 200 + "|started")


async def test_build_notifier():
    with_url = Settings(notify_url="http://127.0.0.1:8790")
    tokened = Secrets(access_code="a", serial="s", host="h", notify_token="tok")
    assert isinstance(build_notifier(SECRETS, with_url), LogNotifier)
    assert isinstance(build_notifier(tokened, Settings()), LogNotifier)
    client = build_notifier(tokened, with_url)
    assert isinstance(client, ServiceNotifyClient)
    await client.aclose()


# -- outbox ----------------------------------------------------------------------


class Camera:
    def __init__(self, image: bytes | None = JPEG, error: Exception | None = None) -> None:
        self.calls = 0
        self.image, self.error = image, error

    async def __call__(self) -> bytes | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.image


def bare_service(tmp_path, notifier, clock, camera=None, settings=None) -> tuple[Service, Store]:
    store = Store(tmp_path / "bambu.db")
    store.migrate()
    service = Service(
        SECRETS,
        settings or Settings(data_dir=tmp_path),
        store,
        notifier,
        FakeLink,
        camera or Camera(),
        clock=clock,
    )
    return service, store


async def test_outbox_backoff_is_capped_and_gives_up_after_a_day(tmp_path, caplog):
    notifier = RecordingNotifier()
    notifier.fail = True
    clock = Clock(T0)
    service, store = bare_service(tmp_path, notifier, clock)
    store.outbox_add("finished", "secret job text", "k1", now=clock().timestamp())
    store.outbox_add("fit", "next", "k2", now=clock().timestamp())

    delays = []
    with caplog.at_level(logging.WARNING, logger="bambu_mcp.service"):
        for _ in range(9):
            delay = await service.send_next()
            delays.append(delay)
            assert await service.send_next() == delay  # not due yet: no new attempt
            clock.advance(seconds=delay)
        assert delays == [30, 60, 120, 240, 480, 960, 1800, 1800, 1800]
        assert notifier.attempts == 9

        clock.now = T0 + timedelta(hours=24, seconds=1)
        assert await service.send_next() == 0  # gives up on k1, k2 is next
    head = store.outbox_head()
    assert head.dedupe == "k2"
    assert any("given up" in r.getMessage() and "finished" in r.getMessage() for r in caplog.records)
    assert "secret job text" not in caplog.text
    store.close()


async def test_outbox_429_waits_at_least_retry_after(tmp_path):
    clock = Clock(T0)

    class RateLimited(RecordingNotifier):
        async def send(self, *args, **kwargs):
            self.attempts += 1
            raise SendError(429, "rate_limited", retry_after=90)

    service, store = bare_service(tmp_path, RateLimited(), clock)
    store.outbox_add("started", "t", "k", now=clock().timestamp())
    assert await service.send_next() == 90
    store.close()


async def test_outbox_drops_non_retryable_and_continues_in_order(tmp_path, caplog):
    clock = Clock(T0)

    class Refusing(RecordingNotifier):
        async def send(self, text, buttons=None, files=None, dedupe=None, **kwargs):
            if dedupe == "bad":
                raise SendError(400, "bad_request", detail="text")
            return await super().send(text, buttons, files, dedupe)

    notifier = Refusing()
    service, store = bare_service(tmp_path, notifier, clock)
    for key in ("bad", "good1", "good2"):
        store.outbox_add("started", key, key, now=clock().timestamp())
    with caplog.at_level(logging.WARNING, logger="bambu_mcp.service"):
        await drain(service)
    assert [m["dedupe"] for m in notifier.sent] == ["good1", "good2"]
    assert store.outbox_count() == 0
    assert any("400" in r.getMessage() and "dropped" in r.getMessage() for r in caplog.records)
    store.close()


async def test_outbox_buttons_and_snapshot_taken_once_at_first_attempt(tmp_path):
    clock = Clock(T0)
    notifier = RecordingNotifier()
    notifier.fail = True
    camera = Camera()
    service, store = bare_service(tmp_path, notifier, clock, camera=camera)
    store.outbox_add(
        "finished",
        "t",
        "k",
        snapshot=True,
        now=clock().timestamp(),
        buttons=[[{"id": "fit:1:ok", "text": "OK"}]],
    )
    assert camera.calls == 0  # not at enqueue
    await service.send_next()
    clock.advance(seconds=30)
    notifier.fail = False
    await service.send_next()
    assert camera.calls == 1
    assert notifier.sent[0]["files"] == [("snapshot.jpg", JPEG)]
    assert notifier.button_ids(0) == ["fit:1:ok"]
    store.close()


async def test_outbox_failed_snapshot_sends_without_picture(tmp_path):
    notifier = RecordingNotifier()
    service, store = bare_service(
        tmp_path, notifier, Clock(T0), camera=Camera(error=RuntimeError("camera down"))
    )
    store.outbox_add("first_layer_done", "t", "k", snapshot=True, now=T0.timestamp())
    await drain(service)
    assert notifier.sent[0]["files"] == []
    store.close()


async def test_log_notifier_drains_the_outbox(tmp_path):
    service, store = bare_service(tmp_path, LogNotifier(), Clock(T0))
    store.outbox_add("started", "t", "k", now=T0.timestamp())
    await drain(service)
    assert store.outbox_count() == 0
    store.close()


class BlockingNotifier(RecordingNotifier):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def send(self, *args, **kwargs):
        await self.release.wait()
        return await super().send(*args, **kwargs)


async def test_reports_are_processed_while_delivery_is_stuck_and_survive_restart(tmp_path):
    store = Store(tmp_path / "bambu.db")
    store.migrate()
    store.set_meta(TARIFF_META_KEY, T0.date().isoformat())
    clock = Clock(T0)
    stuck = BlockingNotifier()
    h = Harness(store, clock, stuck, tmp_path)
    await h.start()
    await h.feed("running_layer0", "running_layer2", "finish", deliver=False)
    # The journal kept up although nothing was delivered.
    row = store.print_by_identity(("cube", "1795385329", None))
    assert row.outcome == "finished"
    assert store.outbox_count() == 4  # no spools, so no spool question
    await h.stop()  # a restart in the middle of delivery
    assert stuck.sent == []

    h.notifier = RecordingNotifier()
    await h.start()
    await until(lambda: store.outbox_count() == 0)
    heads = [m["text"].splitlines()[0] for m in h.notifier.sent]
    assert heads[:3] == ["▶️ Печать началась", "🧱 Первый слой готов", "✅ Печать завершена"]
    assert len(h.notifier.sent) == 4
    assert all(m["dedupe"] for m in h.notifier.sent)
    await h.stop()
    store.close()


# -- journal failures --------------------------------------------------------------


async def test_journal_failure_does_not_block_notifications(harness, monkeypatch, caplog):
    from bambu_mcp import journal as journal_module

    def broken(self, event, slot=None, first_seen=None):
        raise RuntimeError("journal broken")

    monkeypatch.setattr(journal_module.Journal, "on_event", broken)
    await harness.start()
    with caplog.at_level(logging.ERROR, logger="bambu_mcp.service"):
        await harness.feed("running_layer2")  # started + first layer in one report
    heads = [m["text"].splitlines()[0] for m in harness.notifier.sent]
    assert heads == ["▶️ Печать началась", "🧱 Первый слой готов"]
    assert "journal" in caplog.text


async def test_cost_failure_still_sends_finished_and_questions(tmp_path):
    store = Store(tmp_path / "bambu.db")
    store.migrate()
    store.set_meta(TARIFF_META_KEY, T0.date().isoformat())
    h = Harness(store, Clock(T0), RecordingNotifier(), tmp_path)
    h.settings = Settings(data_dir=tmp_path, power_w={"ABS": 250.0})  # no PLA, no PETG (tray)
    await h.start()
    await h.feed("running_layer0", "running_layer2", "finish")
    heads = [m["text"].splitlines()[0] for m in h.notifier.sent]
    assert "✅ Печать завершена" in heads
    finished = next(m for m in h.notifier.sent if m["text"].startswith("✅"))
    assert "Длительность" in finished["text"] and "Электричество" not in finished["text"]
    assert any(m["text"].startswith("Как села деталь") for m in h.notifier.sent)
    await h.stop()
    store.close()


# -- prints that end unseen ----------------------------------------------------------


IDLE_NO_JOB = {
    "print": {"command": "push_status", "gcode_state": "IDLE", "subtask_name": "", "task_id": "0"}
}


async def _start_print_then_restart(harness) -> None:
    await harness.start()
    await harness.feed("running_layer0", "running_layer2")
    await harness.stop()


async def test_finish_while_service_was_down_is_reported_once(harness):
    await _start_print_then_restart(harness)
    await harness.start()
    await harness.feed("finish", minutes=60)
    await harness.feed("finish")
    finished = [m for m in harness.notifier.sent if m["text"].startswith("✅ Печать завершена")]
    assert len(finished) == 1
    assert "Точное время окончания неизвестно" in finished[0]["text"]
    assert finished[0]["dedupe"] == notify_dedupe("cube|1795385329||finished")
    row = harness.store.print_by_identity(("cube", "1795385329", None))
    assert row.outcome == "finished" and row.ended_at is not None and row.actual_min is None
    assert harness.store.open_prints() == []


async def test_power_loss_while_service_was_down_closes_the_row(harness):
    await _start_print_then_restart(harness)
    await harness.start()
    await harness.feed_report(IDLE_NO_JOB)
    await harness.feed_report(IDLE_NO_JOB)
    lost = [m for m in harness.notifier.sent if "пока сервис не видел" in m["text"]]
    assert len(lost) == 1 and "cube" in lost[0]["text"]
    row = harness.store.print_by_identity(("cube", "1795385329", None))
    assert row.outcome == "failed"


async def test_new_job_after_downtime_closes_the_old_row(harness):
    await _start_print_then_restart(harness)
    other = report("running_layer2")
    other["print"].update(subtask_name="bracket", task_id="999")
    await harness.start()
    await harness.feed_report(other)
    heads = [m["text"].splitlines()[0] for m in harness.notifier.sent]
    assert heads[-3:] == [
        "❌ Печать прервана, пока сервис не видел",
        "▶️ Печать началась",
        "🧱 Первый слой готов",
    ]
    assert harness.store.print_by_identity(("cube", "1795385329", None)).outcome == "failed"
    assert [r.identity[0] for r in harness.store.open_prints()] == ["bracket"]


async def test_print_of_a_sliced_job_books_its_spool_without_a_question(harness):
    store = harness.store
    spool = store.add_spool("Elegoo", "PETG", "Black", "#000000", None, 600.0, 1000.0)
    job_id = store.add_job("Lamp Arm", "part", "{}", "a" * 64, T0)
    plates = [{"index": 1, "minutes": 150.0, "grams": {"PETG": 12.5}}]
    result = {"plates": plates, "slot": {"spool_id": spool}}
    store.update_job(job_id, status="sliced", result_json=json.dumps(result))
    name = f"lamp-arm-{job_id}-p1"

    def renamed(fixture: str) -> dict:
        payload = report(fixture)
        payload["print"]["subtask_name"] = name
        return payload

    await harness.start()
    await harness.feed_report(renamed("running_layer0"))
    await harness.feed_report(renamed("running_layer2"))
    await harness.stop()
    await harness.start()  # a restart mid-print sees the start again
    await harness.feed_report(renamed("running_layer2"))
    await harness.feed_report(renamed("finish"), minutes=60)

    heads = [m["text"].splitlines()[0] for m in harness.notifier.sent]
    assert heads.count("▶️ Печать началась") == 1
    assert heads[-2] == "✅ Печать завершена"
    assert harness.notifier.button_ids(len(heads) - 1)[0].startswith("fit:")  # no spool question
    (row,) = store.history(None, 10)
    assert (row.source, row.job_id, row.plate, row.spool_id) == ("job", job_id, 1, spool)
    assert store.spools()[0].remaining_g == pytest.approx(987.5)


async def test_running_to_idle_is_reported_as_failed(harness):
    await harness.start()
    await harness.feed("running_layer0", "running_layer2")
    await harness.feed_report(IDLE_NO_JOB)
    failed = [m for m in harness.notifier.sent if m["text"].startswith("❌ Печать прервана ошибкой")]
    assert len(failed) == 1 and "cube" in failed[0]["text"]
    row = harness.store.print_by_identity(("cube", "1795385329", None))
    assert row.outcome == "failed" and row.actual_min is not None
    # A failed print has no part to fit: no fit question (the spool question may still come).
    assert not any(b.startswith("fit:") for i in range(len(harness.notifier.sent)) for b in harness.notifier.button_ids(i))


async def test_normal_finish_then_restart_sends_nothing_more(harness):
    await harness.start()
    await harness.feed("running_layer0", "running_layer2", "finish")
    count = len(harness.notifier.sent)
    await harness.stop()
    await harness.start()
    await harness.feed("finish", "finish")
    assert len(harness.notifier.sent) == count


# -- presses, time zone, escaping ----------------------------------------------------


async def test_press_consumer_is_restarted_after_a_crash(harness, caplog):
    class FlakyPresses(RecordingNotifier):
        def __init__(self) -> None:
            super().__init__()
            self.runs = 0

        async def presses(self):
            self.runs += 1
            if self.runs == 1:
                raise TypeError("'NoneType' object is not iterable")
            async for press in super().presses():
                yield press

    harness.notifier = FlakyPresses()

    async def no_wait(_s: float) -> None:
        await asyncio.sleep(0)

    harness.sleep = no_wait
    with caplog.at_level(logging.ERROR, logger="bambu_mcp.service"):
        await harness.start()
        await harness.feed("running_layer0", "running_layer2", "finish")
        row = harness.store.print_by_identity(("cube", "1795385329", None))
        await harness.notifier.press_queue.put(Press(1, "m4", f"fit:{row.id}:ok", T0))
        await until(lambda: harness.notifier.edits)
    assert harness.notifier.runs >= 2
    assert "press" in caplog.text


async def test_times_follow_the_configured_timezone(harness):
    harness.settings = Settings(data_dir=harness.settings.data_dir, timezone="UTC")
    await harness.start()
    await harness.feed("running_layer0")
    assert harness.notifier.sent[0]["text"].splitlines()[1].endswith(", 10:01")


async def test_job_name_is_escaped_in_questions(harness):
    await harness.start()
    for name in ("running_layer0", "running_layer2", "finish"):
        payload = report(name)
        payload["print"]["subtask_name"] = "cube_v2"
        await harness.feed_report(payload)
    fit = next(m for m in harness.notifier.sent if m["text"].startswith("Как села"))
    assert fit["text"] == r"Как села деталь «cube\_v2»?"


# -- jobs: proposal, slicing, presses ----------------------------------------------------

MANIFESTS = Path(__file__).parent / "fixtures" / "manifest"
THREE_MF = b"PK\x03\x04fake-3mf-body" + bytes(range(256))
PNG = b"\x89PNG\r\n\x1a\nplate-1"
PRESET = "Generic PETG @BBL P2S"
TRAYS = (
    Tray(0, 0, "PETG", "FFFFFFFF", None),
    Tray(0, 1, "PETG", "057748FF", None),
    Tray(0, 2, "PETG", "161616FF", None),
    Tray(0, 3, None, None, None),
)
PLATES = ({"index": 1, "minutes": 100.0, "grams": {"PETG": 14.0}},)
OVERRIDES = {"wall_loops": 4, "sparse_infill_density": 25, "sparse_infill_pattern": "gyroid", "enable_support": False}


def ok(file: str = "lamp-arm-1-p1.gcode.3mf", plates=PLATES) -> SliceOutcome:
    return SliceOutcome("ok", file=file, plates=plates, slicer_version="02.08.02.61")


class FakeSlicer:
    def __init__(self, *outcomes: SliceOutcome, png: bytes | None = PNG, gate: asyncio.Event | None = None):
        self.outcomes = list(outcomes) or [ok()]
        self.png, self.gate = png, gate
        self.requests: list = []
        self.files: list[bytes] = []
        self.png_requests: list[tuple[str, int]] = []

    async def health(self) -> bool:
        return True

    async def slice(self, job, file_bytes):
        self.requests.append(job)
        self.files.append(file_bytes)
        if self.gate is not None:
            await self.gate.wait()
        return self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]

    async def fetch_png(self, name, plate):
        self.png_requests.append((name, plate))
        return self.png


class FakeWaker:
    def __init__(self, result: bool | WakeError = True) -> None:
        self.result = result
        self.budgets: list[float] = []

    async def wake_and_wait(self, budget_s: float = 120) -> bool:
        self.budgets.append(budget_s)
        if isinstance(self.result, WakeError):
            raise self.result
        return self.result


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def manifest_for(body: bytes = THREE_MF, kind: str = "part", **changes) -> bytes:
    data = json.loads((MANIFESTS / f"{kind}.print.json").read_text(encoding="utf-8"))
    data["file_sha256"] = hashlib.sha256(body).hexdigest()
    data.update(changes)
    return json.dumps(data).encode("utf-8")


def add_job(store: Store, settings: Settings, **changes):
    return jobs_module.create_job_from_bytes(store, settings, manifest_for(**changes), THREE_MF)


def add_spool(store: Store, *, material="petg", preset: str | None = PRESET, slot=(0, 0)) -> int:
    spool = store.add_spool("Elegoo", material, "белый", "#FFFFFF", preset, 550.0, 1000.0)
    store.map_slot(*slot, spool)
    return spool


def job_settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, power_w={"PLA": 200.0, "PETG": 230.0})


def job_service(tmp_path, slicer=None, waker=None, *, configured=True, sleep=None):
    store = Store(tmp_path / "bambu.db")
    store.migrate()
    notifier = RecordingNotifier()
    service = Service(
        SECRETS,
        job_settings(tmp_path),
        store,
        notifier,
        FakeLink,
        Camera(),
        clock=Clock(T0),
        sleep=sleep or Sleeps(),
        slicer=(slicer or FakeSlicer()) if configured else None,
        waker=(waker or FakeWaker()) if configured else None,
    )
    service._state = replace(PrinterState.empty(), trays=TRAYS)
    return service, store, notifier


async def test_propose_sends_the_summary_with_slice_and_change_buttons(tmp_path):
    service, store, notifier = job_service(tmp_path)
    spool = add_spool(store)
    job = add_job(store, service._settings)
    await service.propose(job.id)
    await drain(service)
    choice = SlotChoice(0, 0, spool, None, tray_type="PETG", color_name="белый", remaining_g=1000.0)
    assert notifier.sent[0]["text"] == render_proposal(job, OVERRIDES, choice, "Textured PEI Plate", None)
    assert notifier.button_ids(0) == [f"job:{job.id}:slice", f"job:{job.id}:change", f"job:{job.id}:slot:1"]
    # Only slot 1 has a spool with a preset: slots 2 and 3 are loaded but not mapped.
    assert notifier.sent[0]["buttons"][1] == [Button(f"job:{job.id}:slot:1", "✓ 1 petg белый")]
    assert store.job(job.id).status == "new"
    store.close()


async def test_propose_warns_when_the_spool_has_no_filament_preset(tmp_path):
    service, store, notifier = job_service(tmp_path)
    add_spool(store, preset=None)
    job = add_job(store, service._settings)
    await service.propose(job.id)
    await drain(service)
    assert "пресет" in notifier.sent[0]["text"].splitlines()[-1]
    store.close()


async def test_slice_job_happy_path_stores_result_and_enqueues_summary(tmp_path):
    slicer, waker = FakeSlicer(), FakeWaker()
    service, store, notifier = job_service(tmp_path, slicer, waker)
    spool_id = add_spool(store)
    job = add_job(store, service._settings)

    done = await service.slice_job(job.id)

    assert waker.budgets == [120]
    req = slicer.requests[0]
    assert (req.job_id, req.name, req.kit, req.plates) == (job.id, "lamp-arm", False, (1,))
    assert (req.nozzle, req.process_set, req.filament, req.plate_type) == (
        "0.4", "standard", PRESET, "Textured PEI Plate"
    )
    assert dict(req.overrides) == OVERRIDES
    assert slicer.files == [THREE_MF]
    assert slicer.png_requests == [("lamp-arm-1-p1.gcode.3mf", 1)]

    assert done.status == "sliced" and done.error is None
    assert done.sliced_file == "lamp-arm-1-p1.gcode.3mf"
    assert json.loads(done.settings_json) == OVERRIDES
    result = json.loads(done.result_json)
    spool = next(s for s in store.spools() if s.id == spool_id)
    # Canonical material: the spool says "petg", the power table has "PETG".
    cost = estimate_cost(100.0, 14.0, spool, "PETG", store.tariff_on(T0.date()), service._settings, T0)
    assert result["plates"] == list(PLATES)
    assert result["costs"] == [{
        "index": 1, "filament_uah": cost.filament_uah, "power_uah": cost.power_uah,
        "kwh": cost.kwh, "total_uah": cost.total_uah,
    }]
    assert result["slot"]["spool_id"] == spool_id and (result["slot"]["ams"], result["slot"]["slot"]) == (0, 0)
    assert result["slot"]["color"] == "белый"
    assert (result["plate_type"], result["nozzle"], result["process_set"], result["filament"]) == (
        "Textured PEI Plate", "0.4", "standard", PRESET
    )
    assert result["material"] == "PETG" and result["slicer_version"] == "02.08.02.61"

    await drain(service)
    choice = SlotChoice(0, 0, spool_id, None, tray_type="PETG", color_name="белый", remaining_g=1000.0)
    expected = render_sliced(
        done, list(PLATES), [cost], settings=OVERRIDES, slot_choice=choice, plate_type="Textured PEI Plate"
    )
    assert notifier.sent[0]["text"] == expected
    assert notifier.sent[0]["files"] == [("plate-1.png", PNG)]
    assert store.outbox_count() == 0
    assert not any((tmp_path / "outbox-files").glob("*"))  # the picture goes once it is sent
    store.close()


async def test_slice_job_records_the_sliced_file_sha256(tmp_path):
    """The agent's sha256 goes into result_json: pre-flight compares the fetched file with it."""
    sha = "ab" * 32
    service, store, _ = job_service(tmp_path, FakeSlicer(replace(ok(), sha256=sha)))
    add_spool(store)
    job = await service.slice_job(add_job(store, service._settings).id)
    assert json.loads(job.result_json)["file_sha256"] == sha
    store.close()


async def test_slice_job_from_an_older_agent_records_no_sha256(tmp_path):
    service, store, _ = job_service(tmp_path, FakeSlicer(ok()))
    add_spool(store)
    job = await service.slice_job(add_job(store, service._settings).id)
    assert json.loads(job.result_json)["file_sha256"] is None  # the print asks for a re-slice
    store.close()


async def test_sliced_summary_picture_survives_a_failed_attempt(tmp_path):
    service, store, notifier = job_service(tmp_path)
    add_spool(store)
    job = add_job(store, service._settings)
    await service.slice_job(job.id)
    notifier.fail = True
    await service.send_next()
    service._clock.advance(seconds=60)
    notifier.fail = False
    await drain(service)
    assert notifier.sent[0]["files"] == [("plate-1.png", PNG)]
    store.close()


async def test_missing_png_sends_the_text_alone(tmp_path):
    service, store, notifier = job_service(tmp_path, FakeSlicer(png=None))
    add_spool(store)
    job = add_job(store, service._settings)
    await service.slice_job(job.id)
    await drain(service)
    assert notifier.sent[0]["files"] == []
    assert notifier.sent[0]["text"].startswith("lamp-arm, пластина 1:")
    store.close()


async def test_kit_sends_every_plate(tmp_path):
    raw = json.loads((MANIFESTS / "kit.print.json").read_text(encoding="utf-8"))
    indexes = tuple(p["index"] for p in raw["plates"])
    plates = tuple({"index": i, "minutes": 30.0 * i, "grams": {"PETG": 2.5 * i}} for i in indexes)
    slicer = FakeSlicer(ok(f"{raw['name']}-1-kit.gcode.3mf", plates))
    service, store, _ = job_service(tmp_path, slicer)
    add_spool(store)
    job = add_job(store, service._settings, kind="kit")
    done = await service.slice_job(job.id)
    req = slicer.requests[0]
    assert req.kit is True and req.plates == indexes
    assert done.status == "sliced"
    assert [c["index"] for c in json.loads(done.result_json)["costs"]] == list(indexes)
    store.close()


async def test_name_sent_to_the_agent_is_the_slug(tmp_path):
    slicer = FakeSlicer(ok("lamp-arm-v2-1-p1.gcode.3mf"))
    service, store, _ = job_service(tmp_path, slicer)
    add_spool(store)
    job = add_job(store, service._settings, name="Lamp Arm v2")
    await service.slice_job(job.id)
    assert slicer.requests[0].name == "lamp-arm-v2"
    store.close()


async def test_pc_not_woken_marks_failed_and_tells_owner(tmp_path):
    slicer = FakeSlicer()
    service, store, notifier = job_service(tmp_path, slicer, FakeWaker(False))
    add_spool(store)
    job = add_job(store, service._settings)
    done = await service.slice_job(job.id)
    assert done.status == "slice_failed"
    assert done.error == "the PC did not wake within 2 minutes"
    assert slicer.requests == []
    await drain(service)
    assert len(notifier.sent) == 1
    assert "ПК не проснулся" in notifier.sent[0]["text"] and "lamp-arm" in notifier.sent[0]["text"]
    store.close()


async def test_slice_agent_busy_retries_once_after_30s(tmp_path):
    sleeps = Sleeps()
    slicer = FakeSlicer(SliceOutcome("busy"), ok())
    service, store, _ = job_service(tmp_path, slicer, sleep=sleeps)
    add_spool(store)
    job = add_job(store, service._settings)
    done = await service.slice_job(job.id)
    assert sleeps.calls == [30.0]
    assert len(slicer.requests) == 2
    assert done.status == "sliced"
    store.close()


async def test_slice_agent_busy_twice_fails(tmp_path):
    sleeps = Sleeps()
    slicer = FakeSlicer(SliceOutcome("busy"))
    service, store, notifier = job_service(tmp_path, slicer, sleep=sleeps)
    add_spool(store)
    job = add_job(store, service._settings)
    done = await service.slice_job(job.id)
    assert sleeps.calls == [30.0] and len(slicer.requests) == 2
    assert done.status == "slice_failed" and done.error == "slice-agent is busy with another slice"
    await drain(service)
    assert "занят" in notifier.sent[0]["text"]
    store.close()


async def test_slice_failure_detail_in_message(tmp_path):
    detail = "plate 2 is not in the sliced file (bad_plate)"
    slicer = FakeSlicer(SliceOutcome("failed", detail=detail))
    service, store, notifier = job_service(tmp_path, slicer)
    add_spool(store)
    job = add_job(store, service._settings)
    done = await service.slice_job(job.id)
    assert done.status == "slice_failed" and done.error == detail
    assert done.sliced_file is None and done.result_json is None
    await drain(service)
    text = notifier.sent[0]["text"]
    assert text.startswith("❌ Нарезка «lamp-arm» не удалась")
    assert r"plate 2 is not in the sliced file (bad\_plate)" in text
    store.close()


async def test_unreachable_slice_agent_fails_without_retry(tmp_path):
    slicer = FakeSlicer(SliceOutcome("unreachable", detail="ConnectError"))
    sleeps = Sleeps()
    service, store, notifier = job_service(tmp_path, slicer, sleep=sleeps)
    add_spool(store)
    job = add_job(store, service._settings)
    done = await service.slice_job(job.id)
    assert done.status == "slice_failed" and len(slicer.requests) == 1 and sleeps.calls == []
    await drain(service)
    assert "не ответил" in notifier.sent[0]["text"]
    store.close()


async def test_damaged_stored_file_is_never_sliced(tmp_path):
    slicer, waker = FakeSlicer(), FakeWaker()
    service, store, notifier = job_service(tmp_path, slicer, waker)
    add_spool(store)
    job = add_job(store, service._settings)
    stored = jobs_module.job_dir(service._settings, job.id) / "lamp-arm.3mf"
    stored.write_bytes(THREE_MF[:-1])
    done = await service.slice_job(job.id)
    assert done.status == "slice_failed" and done.error == "stored file is damaged, upload again"
    assert waker.budgets == [] and slicer.requests == []
    await drain(service)
    assert "загрузи" in notifier.sent[0]["text"]
    store.close()


async def test_no_filament_preset_fails_before_waking(tmp_path):
    waker = FakeWaker()
    service, store, notifier = job_service(tmp_path, waker=waker)
    spool = add_spool(store, preset=None)
    job = add_job(store, service._settings)
    done = await service.slice_job(job.id)
    assert done.status == "slice_failed"
    assert done.error == f"spool {spool} in slot 1 has no filament preset"
    assert waker.budgets == []
    await drain(service)
    text = notifier.sent[0]["text"]
    assert text.endswith(f"у катушки {spool} (Elegoo petg белый) в слоте 1 не указан пресет филамента")
    store.close()


async def test_no_spool_mapped_to_the_slot_names_the_slot(tmp_path):
    service, store, notifier = job_service(tmp_path)
    job = add_job(store, service._settings)  # no spool mapped: slot 1 (first PETG tray) is chosen
    await service.propose(job.id)
    done = await service.slice_job(job.id)
    assert done.error == "slot 1 has no spool mapped, so there is no filament preset"
    await drain(service)
    assert notifier.sent[0]["text"].splitlines()[-1] == (
        "⚠️ Слот 1 не привязан к катушке, нет пресета филамента: нарезка не начнётся"
    )
    assert notifier.sent[1]["text"].endswith("слот 1 не привязан к катушке, нет пресета филамента")
    store.close()


async def test_slicer_not_configured_fails(tmp_path):
    service, store, _ = job_service(tmp_path, configured=False)
    add_spool(store)
    job = add_job(store, service._settings)
    done = await service.slice_job(job.id)
    assert done.status == "slice_failed" and done.error == "slice-agent is not configured"
    store.close()


async def test_slice_job_refusals_leave_the_job_alone(tmp_path):
    service, store, _ = job_service(tmp_path)
    add_spool(store)
    job = add_job(store, service._settings)
    with pytest.raises(KeyError):
        await service.slice_job(999)
    with pytest.raises(ValueError, match="wall_loops"):
        await service.slice_job(job.id, {"wall_loops": 99})
    assert store.job(job.id).status == "new"
    store.update_job(job.id, status="slicing")
    with pytest.raises(JobBusy):
        await service.slice_job(job.id)
    assert store.outbox_count() == 0
    store.close()


async def test_explicit_settings_win_and_are_stored(tmp_path):
    slicer = FakeSlicer()
    service, store, _ = job_service(tmp_path, slicer)
    add_spool(store)
    job = add_job(store, service._settings)
    done = await service.slice_job(job.id, {"wall_loops": 6, "curr_bed_type": "Cool Plate"})
    req = slicer.requests[0]
    assert req.overrides["wall_loops"] == 6 and req.plate_type == "Cool Plate"
    assert json.loads(done.settings_json)["wall_loops"] == 6
    assert json.loads(done.result_json)["plate_type"] == "Cool Plate"
    store.close()


async def test_default_plate_from_the_store(tmp_path):
    slicer = FakeSlicer()
    service, store, _ = job_service(tmp_path, slicer)
    add_spool(store)
    store.set_default_plate(store.add_plate("smooth", "Smooth PEI Plate"))
    job = add_job(store, service._settings)
    await service.slice_job(job.id)
    assert slicer.requests[0].plate_type == "Smooth PEI Plate"
    store.close()


async def test_slice_press_starts_job_once(harness):
    gate = asyncio.Event()
    harness.slicer, harness.waker = FakeSlicer(gate=gate), FakeWaker()
    harness.settings = job_settings(harness.settings.data_dir)
    service = await harness.start()
    service._state = replace(PrinterState.empty(), trays=TRAYS)
    add_spool(harness.store)
    job = add_job(harness.store, harness.settings)
    queue = harness.notifier.press_queue

    await queue.put(Press(1, "m7", f"job:{job.id}:slice", T0))
    await until(lambda: len(harness.notifier.edits) == 1 and harness.slicer.requests)
    assert harness.notifier.edits[0] == ("m7", "✂️ Режу «lamp-arm», пришлю результат")
    assert harness.store.job(job.id).status == "slicing"

    await queue.put(Press(2, "m7", f"job:{job.id}:slice", T0))
    await until(lambda: len(harness.notifier.edits) == 2)
    assert harness.notifier.edits[1] == ("m7", "Уже режу «lamp-arm»")
    assert len(harness.slicer.requests) == 1

    gate.set()
    await until(lambda: harness.store.job(job.id).status == "sliced")
    await until(lambda: harness.store.outbox_count() == 0)
    assert harness.notifier.sent[-1]["text"].startswith("lamp-arm, пластина 1:")
    assert len(harness.slicer.requests) == 1


async def test_change_press_edits_the_message(harness):
    await harness.start()
    job = add_job(harness.store, harness.settings)
    queue = harness.notifier.press_queue
    for seq, button in enumerate(("job:999:change", "job:x:slice", f"job:{job.id}:print", "job:999:slice"), 1):
        await queue.put(Press(seq, "m1", button, T0))
    await queue.put(Press(9, "m3", f"job:{job.id}:change", T0))
    await until(lambda: harness.notifier.edits)
    assert harness.notifier.edits == [("m3", CHANGE_TEXT)]
    assert CHANGE_TEXT == "Скажи ассистенту, что поменять: слот, стенки, заполнение, поддержки, пластину"
    assert harness.store.job(job.id).status == "new"


async def test_stuck_slicing_job_failed_at_startup(harness):
    job = add_job(harness.store, harness.settings)
    other = add_job(harness.store, harness.settings)
    harness.store.update_job(job.id, status="slicing")
    await harness.start()
    await until(lambda: harness.store.outbox_count() == 0 and harness.notifier.sent)
    stuck = harness.store.job(job.id)
    assert stuck.status == "slice_failed" and stuck.error == "interrupted by a service restart"
    assert harness.store.job(other.id).status == "new"
    assert len(harness.notifier.sent) == 1
    assert "прервано перезапуском" in harness.notifier.sent[0]["text"]


async def test_stop_while_slicing_marks_the_job_failed(harness):
    harness.slicer, harness.waker = FakeSlicer(gate=asyncio.Event()), FakeWaker()
    service = await harness.start()
    service._state = replace(PrinterState.empty(), trays=TRAYS)
    add_spool(harness.store)
    job = add_job(harness.store, harness.settings)
    await harness.notifier.press_queue.put(Press(1, "m7", f"job:{job.id}:slice", T0))
    await until(lambda: harness.slicer.requests)
    await harness.stop()
    stopped = harness.store.job(job.id)
    assert stopped.status == "slice_failed" and stopped.error == "interrupted by a service stop"


async def test_build_slicer(tmp_path, caplog):
    from bambu_mcp.service import build_slicer
    from bambu_mcp.slicer import GatewayWaker, SliceClient

    async with httpx.AsyncClient() as http:
        with caplog.at_level(logging.WARNING, logger="bambu_mcp.service"):
            assert build_slicer(SECRETS, Settings(data_dir=tmp_path), http) == (None, None)
        full = Secrets(**{**SECRETS.__dict__, "gateway_key": "gw-key", "slice_agent_token": "agent-token"})
        settings = Settings(
            data_dir=tmp_path, gateway_url="http://192.0.2.11:8780", slice_agent_url="http://192.0.2.20:11436"
        )
        client, waker = build_slicer(full, settings, http)
        assert isinstance(client, SliceClient) and isinstance(waker, GatewayWaker)
    assert "jobs cannot be sliced" in caplog.text
    assert "agent-token" not in caplog.text



async def test_named_set_reaches_the_agent_as_standard_plus_overrides(tmp_path):
    slicer = FakeSlicer()
    service, store, _ = job_service(tmp_path, slicer)
    add_spool(store)
    job = add_job(store, service._settings, hints={})
    done = await service.slice_job(job.id, named_set="strong")
    req = slicer.requests[0]
    assert req.process_set == "standard"
    assert req.overrides["wall_loops"] == 4 and req.overrides["sparse_infill_density"] == 25
    result = json.loads(done.result_json)
    assert result["process_set"] == "standard" and result["named_set"] == "strong"
    store.close()


@pytest.mark.parametrize(
    ("error", "text"),
    [
        (WakeError("refused", "the gateway refused the wake request (HTTP 401)"),
         "шлюз отклонил запрос пробуждения ПК"),
        (WakeError("unreachable", "the gateway did not answer the wake request (ConnectError)"),
         "шлюз не ответил на запрос пробуждения ПК"),
        (WakeError("not_configured", "the PC is down and no gateway is configured to wake it"),
         "ПК выключен, а шлюз для пробуждения не настроен"),
    ],
)
async def test_gateway_failure_has_its_own_message(tmp_path, error, text):
    slicer = FakeSlicer()
    service, store, notifier = job_service(tmp_path, slicer, FakeWaker(error))
    add_spool(store)
    job = add_job(store, service._settings)
    done = await service.slice_job(job.id)
    assert done.status == "slice_failed" and done.error == str(error)
    assert slicer.requests == []
    await drain(service)
    assert notifier.sent[0]["text"].endswith(text)
    assert "2 минуты" not in notifier.sent[0]["text"]
    store.close()


async def test_orphan_attachment_folders_removed_at_startup(harness):
    root = harness.settings.data_dir / "outbox-files"
    orphan = root / ("0" * 64)
    orphan.mkdir(parents=True)
    (orphan / "plate-1.png").write_bytes(PNG)
    stray_file = root / "stray.txt"
    stray_file.write_text("x")
    harness.notifier.fail = True  # the queued summary stays in the outbox
    harness.store.outbox_add("job_sliced", "summary", "job|1|sliced|abc", now=T0.timestamp())
    service = await harness.start()
    kept = service._attachments_dir("job|1|sliced|abc")
    assert not orphan.exists() and not stray_file.exists()
    kept.mkdir(parents=True)
    (kept / "plate-1.png").write_bytes(PNG)
    service._sweep_attachments()
    assert kept.is_dir()


async def test_press_with_an_unexpected_error_answers_the_owner(harness, caplog, monkeypatch):
    service = await harness.start()
    job = add_job(harness.store, harness.settings)

    def broken(*args, **kwargs):
        raise RuntimeError("corrupt row")

    monkeypatch.setattr(service, "_plan", broken)
    with caplog.at_level(logging.ERROR, logger="bambu_mcp.service"):
        await harness.notifier.press_queue.put(Press(1, "m5", f"job:{job.id}:slice", T0))
        await until(lambda: harness.notifier.edits)
    assert harness.notifier.edits == [("m5", "Не получилось начать нарезку «lamp-arm»: внутренняя ошибка сервиса")]
    assert "corrupt row" in caplog.text
    assert harness.store.job(job.id).status == "new"


def long_kit(plates: int) -> dict:
    raw = json.loads((MANIFESTS / "kit.print.json").read_text(encoding="utf-8"))
    first = raw["plates"][0]
    raw["plates"] = [{**first, "index": i} for i in range(1, plates + 1)]
    return raw


async def test_long_summary_sends_the_picture_then_the_text(tmp_path):
    raw = long_kit(30)
    plates = tuple({"index": i, "minutes": 61.0, "grams": {"PETG": 3.0}} for i in range(1, 31))
    slicer = FakeSlicer(ok("desk-kit-1-kit.gcode.3mf", plates))
    service, store, notifier = job_service(tmp_path, slicer)
    add_spool(store)
    job = add_job(store, service._settings, kind="kit", plates=raw["plates"])
    done = await service.slice_job(job.id)
    assert done.status == "sliced"
    await drain(service)
    assert len(notifier.sent) == 2
    picture, summary = notifier.sent
    assert picture["files"] == [("plate-1.png", PNG)]
    assert len(picture["text"]) <= 1024 and picture["text"] == "desk-kit: пластина 1"
    assert summary["files"] == [] and len(summary["text"]) > 1024
    assert summary["text"].startswith("desk-kit, 30 пластин:")
    assert not any((tmp_path / "outbox-files").glob("*"))
    store.close()


async def test_short_summary_is_the_picture_caption(tmp_path):
    service, store, notifier = job_service(tmp_path)
    add_spool(store)
    job = add_job(store, service._settings)
    await service.slice_job(job.id)
    await drain(service)
    assert len(notifier.sent) == 1 and notifier.sent[0]["files"] == [("plate-1.png", PNG)]
    store.close()


# -- the owner's slot choice (Task 12) --------------------------------------------------

BLUE_PRESET = "Generic PETG HF @BBL P2S"


def add_blue_spool(store: Store) -> int:
    """A second PETG spool with its own preset in slot 2 (ams 0, slot 1)."""
    spool = store.add_spool("Elegoo", "PETG", "синий", "#1E4FC8", BLUE_PRESET, 600.0, 1000.0)
    store.map_slot(0, 1, spool)
    return spool


def flat_buttons(rows) -> list[Button]:
    return [b for row in rows or [] for b in row]


async def test_propose_with_a_slot_stores_it_and_marks_its_button(tmp_path):
    service, store, notifier = job_service(tmp_path)
    add_spool(store)
    add_blue_spool(store)
    job = add_job(store, service._settings)
    text = await service.propose(job.id, slot=2)
    assert store.job(job.id).slot == 2
    assert "слот 2 PETG синий" in text
    await drain(service)
    assert notifier.sent[0]["text"] == text
    assert [b.text for b in flat_buttons(notifier.sent[0]["buttons"][1:])] == ["1 petg белый", "✓ 2 PETG синий"]
    # A later proposal without a slot keeps the stored one.
    assert "слот 2 PETG синий" in await service.propose(job.id)
    store.close()


async def test_propose_with_a_slot_is_refused_while_slicing(tmp_path):
    service, store, notifier = job_service(tmp_path)
    add_spool(store)
    add_blue_spool(store)
    job = add_job(store, service._settings)
    store.update_job(job.id, status="slicing")
    with pytest.raises(JobBusy):
        await service.propose(job.id, slot=2)
    assert store.job(job.id).slot is None
    assert notifier.sent == []
    store.close()


async def test_slice_job_with_a_slot_stores_it_and_uses_its_preset(tmp_path):
    slicer, waker = FakeSlicer(), FakeWaker()
    service, store, _ = job_service(tmp_path, slicer, waker)
    add_spool(store)
    blue = add_blue_spool(store)
    job = add_job(store, service._settings)
    done = await service.slice_job(job.id, slot=2)
    assert done.slot == 2 and done.status == "sliced"
    assert slicer.requests[0].filament == BLUE_PRESET
    slot = json.loads(done.result_json)["slot"]
    assert (slot["ams"], slot["slot"], slot["spool_id"], slot["warning"]) == (0, 1, blue, None)
    store.close()


async def test_unusable_slot_refused_before_anything_changes(tmp_path):
    waker = FakeWaker()
    service, store, _ = job_service(tmp_path, waker=waker)
    add_spool(store)
    job = add_job(store, service._settings)
    for number in (2, 4):  # 2: loaded, no spool mapped; 4: empty tray
        with pytest.raises(SlotUnusable, match=f"slot {number} .*usable: 1 petg белый"):
            await service.slice_job(job.id, slot=number)
        with pytest.raises(SlotUnusable):
            await service.propose(job.id, slot=number)
    after = store.job(job.id)
    assert (after.status, after.slot) == ("new", None)
    assert waker.budgets == [] and store.outbox_count() == 0
    store.close()


async def slot_harness(harness, **slicer_kwargs):
    harness.slicer, harness.waker = FakeSlicer(**slicer_kwargs), FakeWaker()
    harness.settings = job_settings(harness.settings.data_dir)
    service = await harness.start()
    service._state = replace(PrinterState.empty(), trays=TRAYS)
    add_spool(harness.store)
    blue = add_blue_spool(harness.store)
    job = add_job(harness.store, harness.settings)
    return service, job, blue


async def test_slot_press_edits_the_proposal_and_a_later_slice_press_uses_it(harness):
    service, job, blue = await slot_harness(harness)
    queue = harness.notifier.press_queue

    await queue.put(Press(1, "m4", f"job:{job.id}:slot:2", T0))
    await until(lambda: harness.notifier.edits)
    message_id, text = harness.notifier.edits[0]
    assert message_id == "m4" and "слот 2 PETG синий" in text
    rows = harness.notifier.edit_buttons[0]
    assert [b.id for b in rows[0]] == [f"job:{job.id}:slice", f"job:{job.id}:change"]
    assert [(b.id, b.text) for b in flat_buttons(rows[1:])] == [
        (f"job:{job.id}:slot:1", "1 petg белый"),
        (f"job:{job.id}:slot:2", "✓ 2 PETG синий"),
    ]
    assert harness.store.job(job.id).slot == 2
    assert harness.store.outbox_count() == 0  # edited in place, nothing new sent

    await queue.put(Press(2, "m4", f"job:{job.id}:slice", T0))
    await until(lambda: harness.store.job(job.id).status == "sliced")
    assert harness.slicer.requests[0].filament == BLUE_PRESET
    assert json.loads(harness.store.job(job.id).result_json)["slot"]["spool_id"] == blue


async def test_slot_press_sends_a_new_proposal_when_the_edit_fails(harness):
    _service, job, _blue = await slot_harness(harness)
    harness.notifier.edit_ok = False
    await harness.notifier.press_queue.put(Press(1, "m4", f"job:{job.id}:slot:2", T0))
    await until(lambda: harness.notifier.sent)
    sent = harness.notifier.sent[0]
    assert sent["text"] == harness.notifier.edits[0][1]
    assert "✓ 2 PETG синий" in [b.text for b in flat_buttons(sent["buttons"])]
    assert harness.store.job(job.id).slot == 2


async def test_unusable_slot_press_is_answered_and_changes_nothing(harness):
    _service, job, _blue = await slot_harness(harness)
    queue = harness.notifier.press_queue
    # Malformed slot buttons are ignored like any unknown button.
    for seq, button in enumerate((f"job:{job.id}:slot:x", f"job:{job.id}:slot:17", f"job:{job.id}:slot:"), 1):
        await queue.put(Press(seq, "m4", button, T0))
    await queue.put(Press(9, "m4", f"job:{job.id}:slot:4", T0))  # empty tray
    await until(lambda: harness.notifier.sent)
    assert texts(harness.notifier) == ["Слот 4 пустой или без катушки с пресетом, выбери другой"]
    assert harness.notifier.edits == []
    assert harness.store.job(job.id).slot is None


async def test_slot_press_while_slicing_is_ignored_like_the_slice_button(harness):
    _service, job, _blue = await slot_harness(harness)
    harness.store.update_job(job.id, status="slicing")
    await harness.notifier.press_queue.put(Press(1, "m4", f"job:{job.id}:slot:2", T0))
    await until(lambda: harness.notifier.edits)
    assert harness.notifier.edits == [("m4", "Уже режу «lamp-arm»")]
    assert harness.store.job(job.id).slot is None
    assert harness.store.outbox_count() == 0


# -- phase 3: prepare, confirm and start a print -------------------------------------------

MD5_1 = "DD49CB18BFA49DB2DCDC78948AEFBA81"  # Bambu Studio writes it in upper case
MD5_2 = "0123456789ABCDEF0123456789ABCDEF"
CA_NAME = "ca.pem"
GO_TEXT = "Стол чистый, печатать"
UPLOADING = "⏳ Загружаю на принтер…"


def _slice_info(plates) -> str:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>"]
    for idx, secs, filaments in plates:
        parts += ["  <plate>", f'    <metadata key="index" value="{idx}"/>', f'    <metadata key="prediction" value="{secs}"/>']
        for fid, kind, grams in filaments:
            parts.append(f'    <filament id="{fid}" type="{kind}" color="#FFFFFF" used_m="1.7" used_g="{grams}"/>')
        parts.append("  </plate>")
    parts.append("</config>")
    return "\n".join(parts)


PART_PLATES = ((1, 6000, (("1", "PETG", "14.0"),)),)
KIT_PLATES = ((1, 6000, (("1", "PETG", "14.0"),)), (2, 1200, (("1", "PETG", "5.0"),)))


def sliced_bytes(plates=PART_PLATES) -> bytes:
    """A sliced file shaped like slice-agent output (as in tests/test_printgate.py)."""
    settings = {"printable_area": ["0x0", "256x0", "256x256", "0x256"], "printer_model": "Bambu Lab P2S",
                "nozzle_diameter": ["0.4"]}
    md5 = {1: MD5_1, 2: MD5_2}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/slice_info.config", _slice_info(plates))
        z.writestr("Metadata/project_settings.config", json.dumps(settings))
        for idx, _, _ in plates:
            z.writestr(f"Metadata/plate_{idx}.gcode", f"; plate {idx}\nG1 X0\n")
            z.writestr(f"Metadata/plate_{idx}.gcode.md5", md5[idx] + "\n")
    return buf.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PrintSlicer(FakeSlicer):
    """Serves one sliced file. Never a network call."""

    def __init__(self, data: bytes) -> None:
        super().__init__()
        self.data = data
        self.fetched: list[tuple[str, Path]] = []
        self.error: Exception | None = None

    async def fetch_file(self, name, dest, max_bytes=50 * 2**20):
        self.fetched.append((name, dest))
        if self.error is not None:
            raise self.error
        dest.write_bytes(self.data)
        return sha(self.data)


class PrintLink(FakeLink):
    """A printer that answers a pushall with ``printer`` and a project_file with ``answer``."""

    def __init__(self, on_report, on_status) -> None:
        super().__init__(on_report, on_status)
        self.printer: dict | None = None
        self.projects: list[dict] = []
        self.answer: str | None = "RUNNING"
        self.refuse = False
        self.after_send: Exception | None = None  # raised once the project_file is out
        self.events: list[str] = []

    def request_full_report(self) -> None:
        super().request_full_report()
        if self.printer is not None:
            self.on_report(copy.deepcopy(self.printer))

    def pause(self) -> None:
        if self.refuse:
            raise PrinterNotConnected("printer link is not connected, nothing was sent")
        self.events.append("pause")

    def stop_print(self) -> None:
        if self.refuse:
            raise PrinterNotConnected("printer link is not connected, nothing was sent")
        self.events.append("stop")
        if self.after_send is not None:
            raise self.after_send

    def start_project(self, **fields) -> None:
        if self.refuse:
            raise PrinterNotConnected("printer link is not connected, nothing was sent")
        self.events.append("project_file")
        self.projects.append(fields)
        if self.answer is not None:
            self.on_report({"print": {
                "gcode_state": self.answer, "task_id": fields["task_id"], "subtask_name": fields["subtask_name"],
                "mc_percent": 0, "layer_num": 0, "total_layer_num": 50,
            }})
        if self.after_send is not None:
            raise self.after_send


class FakeUploader:
    """``ftps.upload`` stand-in, called in a worker thread."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[dict] = []
        self.error: Exception | None = None
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def __call__(self, host, serial, access_code, ca_file, local, remote_name) -> None:
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(5)
        self.events.append("upload")
        self.calls.append({
            "host": host, "serial": serial, "access_code": access_code, "ca_file": ca_file,
            "remote": remote_name, "sha": sha(Path(local).read_bytes()),
        })
        if self.error is not None:
            raise self.error


def idle_printer() -> dict:
    payload = report("pushall_idle")  # one AMS unit: slot 1 white PETG
    payload["print"].update(gcode_state="IDLE", sdcard=True, subtask_name="", task_id="0")
    return payload


def sliced_job(store: Store, settings: Settings, data: bytes, spool: int, *, kit: bool = False, **result):
    job = add_job(store, settings, kind="kit") if kit else add_job(store, settings)
    name = f"{slug(job.name)}-{job.id}-{'kit' if kit else 'p1'}.gcode.3mf"
    plates = [{"index": 1, "minutes": 100.0, "grams": {"PETG": 14.0}}]
    if kit:
        plates.append({"index": 2, "minutes": 20.0, "grams": {"PETG": 5.0}})
    body = {
        "plates": plates,
        "slot": {"ams": 0, "slot": 0, "spool_id": spool, "tray_type": "PETG", "color": "белый", "warning": None},
        "material": "PETG", "plate_type": "Textured PEI Plate", "nozzle": "0.4", "file_sha256": sha(data),
        **result,
    }
    store.update_job(job.id, status="sliced", sliced_file=name, result_json=json.dumps(body, ensure_ascii=False))
    return store.job(job.id)


async def connect_printer(harness, printer: dict) -> PrintLink:
    link = harness.links[-1]
    link.printer = printer
    link.events = harness.events
    await _status(harness, LinkStatus.CONNECTED)
    await harness.feed_report(printer)
    return link


async def print_harness(harness, *, kit: bool = False, watch_s: float = 2.0):
    data = sliced_bytes(KIT_PLATES if kit else PART_PLATES)
    tmp = harness.settings.data_dir
    harness.events = []
    harness.slicer, harness.waker = PrintSlicer(data), FakeWaker()
    harness.uploader = FakeUploader(harness.events)
    harness.link_class = PrintLink
    harness.settings = Settings(data_dir=tmp, ca_file=tmp / CA_NAME, power_w={"PLA": 200.0, "PETG": 230.0})
    harness.service_kwargs = {"uploader": harness.uploader, "watch_s": watch_s, "report_wait_s": 1.0}
    service = await harness.start()
    printer = idle_printer()
    await connect_printer(harness, printer)
    spool = add_spool(harness.store)
    job = sliced_job(harness.store, harness.settings, data, spool, kit=kit)
    harness.data, harness.spool = data, spool
    return service, job, printer


def confirmations(store: Store) -> int:
    return store._conn().execute("SELECT COUNT(*) FROM confirmations").fetchone()[0]


async def press(harness, button: str, message_id: str = "m1") -> None:
    harness.seq = getattr(harness, "seq", 0) + 1
    await harness.notifier.press_queue.put(Press(harness.seq, message_id, button, T0))


async def pressed_to_the_end(harness, cid: str) -> str:
    await until(lambda: harness.store.confirmation(cid).outcome is not None, timeout=5)
    await until(lambda: harness.store.outbox_count() == 0)
    return harness.store.confirmation(cid).outcome


def plain(rows) -> list[list[tuple[str, str]]]:
    return [[(b.id, b.text) for b in row] for row in rows]


async def test_prepare_sends_confirmation_and_starts_nothing(harness):
    service, job, _printer = await print_harness(harness)
    link = harness.links[-1]
    reports = link.full_reports

    result = await service.prepare_print(job.id)

    assert isinstance(result, PrepareResult)
    cid = result.confirm_id
    assert len(cid) == 32 and int(cid, 16) >= 0
    assert result.warnings == []
    await until(lambda: harness.store.outbox_count() == 0)
    (message,) = harness.notifier.sent
    assert message["text"] == result.text
    assert "lamp-arm" in result.text and "Textured PEI Plate" in result.text and "слот 1" in result.text
    assert "—" not in result.text and ";" not in result.text
    assert plain(message["buttons"]) == [[(f"start:{cid}:go", GO_TEXT), (f"start:{cid}:cancel", "Отмена")]]
    conf = harness.store.confirmation(cid)
    assert (conf.kind, conf.state, conf.job_id, conf.plate, conf.outcome) == ("start", "pending", job.id, 1, None)
    assert conf.expires_at - conf.created_at == 600
    assert conf.created_at == harness.clock().timestamp()
    assert conf.gate_hash == gate_hash(sha(harness.data), 1, 0, 0, SECRETS.serial, "0.4", "Textured PEI Plate")
    cache = harness.settings.data_dir / "print-cache"
    assert harness.slicer.fetched == [(job.sliced_file, cache / job.sliced_file)]
    assert (cache / job.sliced_file).read_bytes() == harness.data
    assert harness.waker.budgets == [120]
    assert link.full_reports == reports + 1  # the checks ran on a fresh report
    assert link.projects == [] and harness.uploader.calls == []


async def test_prepare_refused_lists_reasons_and_sends_nothing(harness):
    service, job, printer = await print_harness(harness)
    printer["print"].update(gcode_state="RUNNING", sdcard=False)  # the next pushall says so
    with pytest.raises(PrintRefused) as info:
        await service.prepare_print(job.id)
    lines = info.value.lines
    assert len(lines) == 2 and "занят" in lines[0] and "флешки" in lines[1]
    assert "занят" in str(info.value)  # Claude sees the same reasons
    await until(lambda: harness.store.outbox_count() == 0)  # the phase 1 "started" news only
    assert confirmations(harness.store) == 0
    assert [b for m in harness.notifier.sent for row in m["buttons"] for b in row] == []
    assert not any("Печатать" in m["text"] for m in harness.notifier.sent)
    assert harness.links[-1].projects == [] and harness.uploader.calls == []


@pytest.mark.parametrize("case", ["not_sliced", "tls_pin", "no_ca_file", "kit_plate_missing", "part_plate_2"])
async def test_prepare_refuses_before_waking_the_pc(harness, case):
    service, job, _printer = await print_harness(harness, kit=case == "kit_plate_missing")
    plate = 1
    if case == "not_sliced":
        harness.store.update_job(job.id, status="slice_failed")
    elif case == "tls_pin":
        service._settings = replace(service._settings, tls_mode="pin", cert_sha256="ab" * 32)
    elif case == "no_ca_file":
        service._settings = replace(service._settings, ca_file=None)
    else:
        plate = 3 if case == "kit_plate_missing" else 2
    with pytest.raises((PrintRefused, ValueError)) as info:
        await service.prepare_print(job.id, plate)
    if case in ("tls_pin", "no_ca_file"):
        assert isinstance(info.value, PrintRefused)
        assert "tls_mode" in str(info.value) and "ca_file" in str(info.value)  # English, for Claude
        assert "TLS" in info.value.lines[0]  # Russian, for Telegram
    if case == "not_sliced":
        assert isinstance(info.value, PrintRefused) and "не нарезано" in info.value.lines[0]
    assert harness.waker.budgets == [] and harness.slicer.fetched == []
    assert confirmations(harness.store) == 0 and harness.store.outbox_count() == 0


async def test_press_starts_once_then_started_message(harness):
    service, job, _printer = await print_harness(harness)
    link = harness.links[-1]
    cid = (await service.prepare_print(job.id)).confirm_id
    await until(lambda: harness.store.outbox_count() == 0)

    await press(harness, f"start:{cid}:go")
    assert await pressed_to_the_end(harness, cid) == "started"

    assert harness.notifier.edits[0] == ("m1", UPLOADING)
    assert harness.store.confirmation(cid).state == "used"
    assert harness.events == ["upload", "project_file"]
    (upload,) = harness.uploader.calls
    assert upload == {
        "host": SECRETS.host, "serial": SECRETS.serial, "access_code": SECRETS.access_code,
        "ca_file": harness.settings.data_dir / CA_NAME, "remote": job.sliced_file, "sha": sha(harness.data),
    }
    (project,) = link.projects
    task_id = project["task_id"]
    assert task_id.isascii() and task_id.isdigit() and task_id != "0"
    assert project == {
        "file": job.sliced_file, "plate": 1, "md5": MD5_1.lower(),
        "subtask_name": job.sliced_file.removesuffix(".gcode.3mf"), "task_id": task_id, "ams": 0, "slot": 0,
    }
    started = [m for m in harness.notifier.sent if "Печать началась" in m["text"]]
    assert len(started) == 1
    assert plain(started[0]["buttons"]) == [[(f"stop:ask:{task_id}", "Остановить…")]]


async def test_double_press_and_restart_replay_start_once(harness):
    service, job, printer = await print_harness(harness)
    cid = (await service.prepare_print(job.id)).confirm_id
    harness.uploader.gate = threading.Event()

    await press(harness, f"start:{cid}:go")
    await until(lambda: harness.uploader.entered.is_set())
    await press(harness, f"start:{cid}:go")  # while the first one uploads
    await until(lambda: len(harness.notifier.edits) == 2)
    assert harness.notifier.edits[1] == ("m1", "⏳ Уже запускаю печать")
    harness.uploader.gate.set()
    assert await pressed_to_the_end(harness, cid) == "started"

    await press(harness, f"start:{cid}:go")  # after it started
    await until(lambda: len(harness.notifier.edits) == 3)
    assert "уже нажата" in harness.notifier.edits[2][1]

    (project,) = harness.links[-1].projects
    await harness.stop()
    await harness.start()
    printer["print"].update(gcode_state="RUNNING", task_id=project["task_id"], subtask_name=project["subtask_name"])
    await connect_printer(harness, printer)
    await press(harness, f"start:{cid}:go")  # the add-in replays the press after the restart
    await until(lambda: len(harness.notifier.edits) == 4)
    assert "уже нажата" in harness.notifier.edits[3][1]
    assert sum(len(link.projects) for link in harness.links) == 1
    assert len(harness.uploader.calls) == 1
    assert harness.store.confirmation(cid).outcome == "started"


@pytest.mark.parametrize("change", ["printer_busy", "plate_type", "cached_file"])
async def test_press_after_state_changed_refuses(harness, change):
    service, job, printer = await print_harness(harness)
    cid = (await service.prepare_print(job.id)).confirm_id
    await until(lambda: harness.store.outbox_count() == 0)
    if change == "printer_busy":  # the owner started another print from Bambu Studio
        printer["print"].update(gcode_state="RUNNING", subtask_name="other", task_id="555")
        await harness.feed_report(printer)
        expected = "занят"
    elif change == "plate_type":  # sliced again for another plate
        result = json.loads(job.result_json)
        result["plate_type"] = "Smooth PEI Plate"
        harness.store.update_job(job.id, result_json=json.dumps(result))
        expected = "изменилось"
    else:
        (harness.settings.data_dir / "print-cache" / job.sliced_file).write_bytes(b"something else")
        expected = "изменился"
    sent = len(harness.notifier.sent)

    await press(harness, f"start:{cid}:go")
    outcome = await pressed_to_the_end(harness, cid)

    assert outcome.startswith("refused:")
    assert harness.store.confirmation(cid).state == "used"
    assert harness.uploader.calls == [] and harness.links[-1].projects == []
    (refusal,) = harness.notifier.sent[sent:]
    assert "не запущена" in refusal["text"] and expected in refusal["text"]
    assert "—" not in refusal["text"] and ";" not in refusal["text"]
    await press(harness, f"start:{cid}:go")  # the confirmation stays used
    await until(lambda: len(harness.notifier.edits) == 2)
    assert "уже нажата" in harness.notifier.edits[1][1]
    assert harness.uploader.calls == [] and harness.links[-1].projects == []


async def test_press_after_expiry_refuses(harness):
    service, job, _printer = await print_harness(harness)
    cid = (await service.prepare_print(job.id)).confirm_id
    await until(lambda: harness.store.outbox_count() == 0)
    harness.clock.advance(minutes=10)  # valid for 10 minutes, so this moment is too late
    await press(harness, f"start:{cid}:go")
    await until(lambda: harness.notifier.edits)
    assert "истекло" in harness.notifier.edits[0][1]
    assert harness.store.confirmation(cid).state == "expired"
    assert harness.uploader.calls == [] and harness.links[-1].projects == []


async def test_unconfirmed_tells_owner_and_never_resends(harness):
    service, job, printer = await print_harness(harness, watch_s=0.3)
    link = harness.links[-1]
    link.answer = None  # the printer stays silent about our task
    cid = (await service.prepare_print(job.id)).confirm_id
    await press(harness, f"start:{cid}:go")
    assert await pressed_to_the_end(harness, cid) == "unconfirmed"

    (project,) = link.projects
    unconfirmed = harness.notifier.sent[-1]["text"]
    assert "не подтвердил" in unconfirmed and "IDLE" in unconfirmed and "повторно" in unconfirmed
    assert "—" not in unconfirmed and ";" not in unconfirmed

    # A later report of our task is still a print of the job.
    printer["print"].update(gcode_state="RUNNING", task_id=project["task_id"], subtask_name=project["subtask_name"])
    await harness.feed_report(printer)
    (row,) = harness.store.history(None, 10)
    assert (row.source, row.job_id) == ("job", job.id)
    await press(harness, f"start:{cid}:go")
    await until(lambda: len(harness.notifier.edits) == 2)
    assert len(link.projects) == 1 and len(harness.uploader.calls) == 1
    assert harness.store.confirmation(cid).outcome == "unconfirmed"


async def test_press_answers_when_the_upload_or_the_send_fails(harness):
    service, job, _printer = await print_harness(harness)
    link = harness.links[-1]
    harness.uploader.error = UploadError(
        "printer cannot store the file (553): no USB storage in the printer?",
        "принтер не может сохранить файл, вставлена ли флешка?",
    )
    cid = (await service.prepare_print(job.id)).confirm_id
    await press(harness, f"start:{cid}:go")
    assert await pressed_to_the_end(harness, cid) == "refused:upload"
    assert link.projects == []
    refusal = harness.notifier.sent[-1]["text"]
    assert "не загрузился" in refusal and "флешка" in refusal and "USB storage" not in refusal

    harness.uploader.error = None
    link.refuse = True
    cid = (await service.prepare_print(job.id)).confirm_id
    await press(harness, f"start:{cid}:go")
    assert await pressed_to_the_end(harness, cid) == "refused:not_connected"
    assert link.projects == []
    assert "не на связи" in harness.notifier.sent[-1]["text"]


async def test_cancel(harness):
    service, job, _printer = await print_harness(harness)
    cid = (await service.prepare_print(job.id)).confirm_id
    await press(harness, f"start:{cid}:cancel")
    await until(lambda: harness.notifier.edits)
    assert harness.notifier.edits == [("m1", "Печать отменена")]
    conf = harness.store.confirmation(cid)
    assert (conf.state, conf.outcome) == ("cancelled", "cancelled")
    await press(harness, f"start:{cid}:go")
    await until(lambda: len(harness.notifier.edits) == 2)
    assert "отменена" in harness.notifier.edits[1][1]
    assert harness.uploader.calls == [] and harness.links[-1].projects == []


async def test_malformed_start_buttons_are_ignored(harness):
    service, job, _printer = await print_harness(harness)
    cid = (await service.prepare_print(job.id)).confirm_id
    for button in (f"start:{cid}:resume", f"start:{cid.upper()}:go", f"start:{cid}", "start::go", f"start:{cid}:go:1"):
        await press(harness, button)
    await press(harness, f"job:{job.id}:change", "m9")  # handled after the ones above
    await until(lambda: harness.notifier.edits)
    assert harness.notifier.edits == [("m9", CHANGE_TEXT)]
    assert harness.store.confirmation(cid).state == "pending"


async def test_part_print_button_prepares_plate_one(harness):
    _service, job, _printer = await print_harness(harness)
    await press(harness, f"job:{job.id}:print", "m5")
    await until(lambda: any(b.id.startswith("start:") for m in harness.notifier.sent for row in m["buttons"] for b in row))
    (message,) = harness.notifier.sent
    cid = message["buttons"][0][0].id.split(":")[1]
    assert harness.store.confirmation(cid).plate == 1
    assert harness.notifier.edits == []  # the sliced summary stays as it was


async def test_kit_print_button_asks_for_plate(harness):
    _service, job, _printer = await print_harness(harness, kit=True)
    await press(harness, f"job:{job.id}:print", "m5")
    await until(lambda: harness.notifier.sent)
    (question,) = harness.notifier.sent
    assert "desk-kit" in question["text"]
    assert [b.id for row in question["buttons"] for b in row] == [f"job:{job.id}:print:1", f"job:{job.id}:print:2"]
    assert harness.slicer.fetched == []  # nothing prepared before the owner picks a plate

    await press(harness, f"job:{job.id}:print:2", "m6")
    await until(lambda: len(harness.notifier.sent) == 2)
    confirmation = harness.notifier.sent[1]
    cid = confirmation["buttons"][0][0].id.split(":")[1]
    assert harness.store.confirmation(cid).plate == 2
    assert "пластина 2" in confirmation["text"]
    assert harness.notifier.edits[0][0] == "m6"

    await press(harness, f"start:{cid}:go")
    assert await pressed_to_the_end(harness, cid) == "started"
    (project,) = harness.links[-1].projects
    assert (project["plate"], project["md5"]) == (2, MD5_2.lower())


@pytest.mark.parametrize(
    ("result", "text"),
    [(False, "не проснулся"), (WakeError("unreachable", "the gateway did not answer"), "шлюз не ответил")],
)
async def test_pc_asleep_wake_fails_no_confirmation(harness, result, text):
    service, job, _printer = await print_harness(harness)
    harness.waker.result = result
    with pytest.raises(PrintRefused) as info:
        await service.prepare_print(job.id)
    assert text in info.value.lines[0]
    assert harness.slicer.fetched == []
    assert confirmations(harness.store) == 0 and harness.store.outbox_count() == 0

    await press(harness, f"job:{job.id}:print", "m5")  # the same from the button
    await until(lambda: harness.notifier.sent)
    (message,) = harness.notifier.sent
    assert "не подготовлена" in message["text"] and text in message["text"] and message["buttons"] == []
    assert confirmations(harness.store) == 0


async def test_started_print_matched_by_journal(harness):
    service, job, _printer = await print_harness(harness)
    cid = (await service.prepare_print(job.id)).confirm_id
    await press(harness, f"start:{cid}:go")
    assert await pressed_to_the_end(harness, cid) == "started"
    (row,) = harness.store.history(None, 10)
    assert (row.source, row.job_id, row.plate, row.spool_id) == ("job", job.id, 1, harness.spool)
    assert row.grams == pytest.approx(14.0)
    assert harness.store.spools()[0].remaining_g == pytest.approx(986.0)


async def test_print_cache_keeps_the_last_three_files(harness):
    service, job, _printer = await print_harness(harness)
    cache = harness.settings.data_dir / "print-cache"
    cache.mkdir()
    for age, name in enumerate(("a-1-p1.gcode.3mf", "b-2-p1.gcode.3mf", "c-3-p1.gcode.3mf", "d-4-p1.gcode.3mf")):
        (cache / name).write_bytes(b"old")
        stamp = T0.timestamp() - 1000 * (age + 1)
        os.utime(cache / name, (stamp, stamp))
    (cache / ".a-1-p1.gcode.3mf.0011223344556677.part").write_bytes(b"partial")
    await service.prepare_print(job.id)
    assert sorted(p.name for p in cache.iterdir()) == sorted(
        [job.sliced_file, "a-1-p1.gcode.3mf", "b-2-p1.gcode.3mf", ".a-1-p1.gcode.3mf.0011223344556677.part"]
    )


async def test_sliced_summary_has_a_print_button(tmp_path):
    service, store, notifier = job_service(tmp_path)
    add_spool(store)
    job = add_job(store, service._settings)
    await service.slice_job(job.id)
    await drain(service)
    assert plain(notifier.sent[0]["buttons"]) == [[(f"job:{job.id}:print", "Печатать…")]]
    store.close()


async def test_long_summary_puts_the_print_button_on_the_text(tmp_path):
    raw = long_kit(30)
    plates = tuple({"index": i, "minutes": 61.0, "grams": {"PETG": 3.0}} for i in range(1, 31))
    service, store, notifier = job_service(tmp_path, FakeSlicer(ok("desk-kit-1-kit.gcode.3mf", plates)))
    add_spool(store)
    job = add_job(store, service._settings, kind="kit", plates=raw["plates"])
    await service.slice_job(job.id)
    await drain(service)
    picture, summary = notifier.sent
    assert picture["buttons"] == []
    assert plain(summary["buttons"]) == [[(f"job:{job.id}:print", "Печатать…")]]
    store.close()


# -- pause and the confirmed stop (Task 6) -----------------------------------------------

STOP_GO_TEXT = "Да, остановить"


async def running(harness, printer: dict, *, task_id: str = "777", name: str = "other-print", state: str = "RUNNING"):
    printer["print"].update(gcode_state=state, task_id=task_id, subtask_name=name)
    await harness.feed_report(printer)


async def test_pause_only_while_running(harness):
    service, _job, printer = await print_harness(harness)
    link = harness.links[-1]
    with pytest.raises(ControlRefused) as info:
        await service.pause_print()  # IDLE
    assert "IDLE" in str(info.value) and "nothing was sent" in str(info.value)
    await running(harness, printer, state="PAUSE")
    with pytest.raises(ControlRefused):
        await service.pause_print()
    assert "pause" not in harness.events

    await running(harness, printer)
    result = await service.pause_print()
    assert result == {"task_id": "777", "job_name": "other-print"}
    assert harness.events.count("pause") == 1

    link.refuse = True  # the link dropped between the check and the send
    with pytest.raises(ControlRefused) as info:
        await service.pause_print()
    assert "not connected" in str(info.value)
    await _status(harness, LinkStatus.DISCONNECTED)
    with pytest.raises(ControlRefused):
        await service.pause_print()
    assert harness.events.count("pause") == 1
    assert "resume" not in harness.events and "stop" not in harness.events


async def test_request_stop_sends_button_stops_nothing(harness):
    service, _job, printer = await print_harness(harness)
    with pytest.raises(ControlRefused) as info:
        await service.request_stop()  # nothing is printing
    assert "IDLE" in str(info.value)
    assert confirmations(harness.store) == 0
    await running(harness, printer)
    sent = len(harness.notifier.sent)

    result = await service.request_stop()

    cid = result.confirm_id
    assert len(cid) == 32
    await until(lambda: harness.store.outbox_count() == 0)
    (message,) = harness.notifier.sent[sent:]
    assert message["text"] == result.text
    assert "Остановить печать «other-print»?" in result.text
    assert "—" not in result.text and ";" not in result.text
    assert plain(message["buttons"]) == [[(f"stop:{cid}:go", STOP_GO_TEXT), (f"stop:{cid}:cancel", "Отмена")]]
    assert cid not in message["dedupe"]
    conf = harness.store.confirmation(cid)
    assert (conf.kind, conf.state, conf.outcome) == ("stop", "pending", None)
    assert conf.expires_at - conf.created_at == 600
    assert "stop" not in harness.events and "pause" not in harness.events


async def test_stop_ask_button_sends_the_confirmation(harness):
    _service, _job, printer = await print_harness(harness)
    await running(harness, printer, task_id="888")
    sent = len(harness.notifier.sent)
    await press(harness, "stop:ask:777", "m3")  # an old print's button: nothing to stop
    await until(lambda: len(harness.notifier.sent) == sent + 1)
    assert "уже не идёт" in harness.notifier.sent[-1]["text"]
    assert confirmations(harness.store) == 0

    await press(harness, "stop:ask:888", "m4")
    await until(lambda: len(harness.notifier.sent) == sent + 2)
    message = harness.notifier.sent[-1]
    cid = message["buttons"][0][0].id.split(":")[1]
    assert plain(message["buttons"]) == [[(f"stop:{cid}:go", STOP_GO_TEXT), (f"stop:{cid}:cancel", "Отмена")]]
    assert harness.store.confirmation(cid).kind == "stop"
    assert harness.notifier.edits == []  # the start message keeps its text
    assert "stop" not in harness.events


async def test_stop_press_sends_stop_once(harness):
    service, _job, printer = await print_harness(harness)
    await running(harness, printer)
    cid = (await service.request_stop()).confirm_id

    await press(harness, f"stop:{cid}:go", "m7")
    await until(lambda: harness.notifier.edits)
    assert harness.notifier.edits[0] == ("m7", "⏹ Команда остановки отправлена")
    await press(harness, f"stop:{cid}:go", "m7")
    await until(lambda: len(harness.notifier.edits) == 2)
    assert "уже нажата" in harness.notifier.edits[1][1]
    assert harness.events.count("stop") == 1
    conf = harness.store.confirmation(cid)
    assert (conf.state, conf.outcome) == ("used", "sent")

    await harness.stop()  # the add-in replays the press after a restart
    await harness.start()
    await connect_printer(harness, printer)
    await press(harness, f"stop:{cid}:go", "m7")
    await until(lambda: len(harness.notifier.edits) == 3)
    assert "уже нажата" in harness.notifier.edits[2][1]
    assert harness.events.count("stop") == 1


async def test_stop_send_that_raises_is_never_repeated(harness):
    service, _job, printer = await print_harness(harness)
    await running(harness, printer)
    cid = (await service.request_stop()).confirm_id
    harness.links[-1].after_send = RuntimeError("paho broke after the publish")
    await press(harness, f"stop:{cid}:go", "m7")
    await until(lambda: harness.notifier.edits)
    assert "Не знаю" in harness.notifier.edits[0][1]
    assert harness.store.confirmation(cid).outcome == "unconfirmed"
    await press(harness, f"stop:{cid}:go", "m7")
    await until(lambda: len(harness.notifier.edits) == 2)
    assert harness.events.count("stop") == 1


@pytest.mark.parametrize("change", ["finished", "other_print", "disconnected", "expired"])
async def test_stop_press_after_print_changed_refuses(harness, change):
    service, _job, printer = await print_harness(harness)
    await running(harness, printer)
    cid = (await service.request_stop()).confirm_id
    if change == "finished":
        await running(harness, printer, state="FINISH")
        expected = "уже не идёт"
    elif change == "other_print":
        await running(harness, printer, task_id="999", name="next-print")
        expected = "другая"
    elif change == "disconnected":
        await _status(harness, LinkStatus.DISCONNECTED)
        expected = "не на связи"
    else:
        harness.clock.advance(minutes=10)
        expected = "истекло"

    await press(harness, f"stop:{cid}:go", "m7")
    await until(lambda: harness.notifier.edits)
    answer = harness.notifier.edits[0][1]
    assert expected in answer
    assert "—" not in answer and ";" not in answer
    assert "stop" not in harness.events
    assert harness.store.confirmation(cid).state in ("used", "expired")
    await press(harness, f"stop:{cid}:go", "m7")  # spent, whatever happens next
    await until(lambda: len(harness.notifier.edits) == 2)
    assert "stop" not in harness.events


async def test_stop_cancel_and_malformed_stop_buttons(harness):
    service, _job, printer = await print_harness(harness)
    await running(harness, printer)
    cid = (await service.request_stop()).confirm_id
    await until(lambda: harness.store.outbox_count() == 0)
    sent = len(harness.notifier.sent)
    for button in (f"stop:{cid}:resume", f"stop:{cid.upper()}:go", f"stop:{cid}", "stop:ask:", "stop:ask:7x7",
                   f"stop:{cid}:go:1", "stop:ask:777:go", "stop:ask:" + "1" * 20):
        await press(harness, button)
    await press(harness, f"stop:{cid}:cancel", "m8")
    await until(lambda: harness.notifier.edits)
    assert harness.notifier.edits == [("m8", "Остановка отменена")]
    assert len(harness.notifier.sent) == sent
    conf = harness.store.confirmation(cid)
    assert (conf.state, conf.outcome) == ("cancelled", "cancelled")
    await press(harness, f"stop:{cid}:go", "m8")
    await until(lambda: len(harness.notifier.edits) == 2)
    assert "отменена" in harness.notifier.edits[1][1]
    assert "stop" not in harness.events


async def test_start_and_stop_confirmations_do_not_cross(harness):
    service, job, printer = await print_harness(harness)
    start_cid = (await service.prepare_print(job.id)).confirm_id
    await running(harness, printer)
    stop_cid = (await service.request_stop()).confirm_id
    await press(harness, f"stop:{start_cid}:go", "m1")
    await press(harness, f"start:{stop_cid}:go", "m2")
    await press(harness, f"job:{job.id}:change", "m9")  # handled after the ones above
    await until(lambda: harness.notifier.edits)
    assert harness.notifier.edits == [("m9", CHANGE_TEXT)]
    assert harness.store.confirmation(start_cid).state == "pending"
    assert harness.store.confirmation(stop_cid).state == "pending"
    assert harness.events == []


# -- Task X1: confirmation buttons expire in the add-in ---------------------------------------

START_EXPIRED = "⌛ Подтверждение истекло, подготовь печать заново"
STOP_EXPIRED = "⌛ Подтверждение истекло, остановка не отправлена. Запроси её заново, если нужно"


async def _confirm(harness, service, job, printer, kind: str) -> str:
    """Ask for a start or stop confirmation (a stop needs a running print), return its id."""
    if kind == "start":
        return (await service.prepare_print(job.id)).confirm_id
    return (await service.request_stop()).confirm_id


@pytest.mark.parametrize("kind", ["start", "stop"])
async def test_confirmation_sent_with_expires_in(harness, kind):
    service, job, printer = await print_harness(harness)
    if kind == "stop":
        await running(harness, printer)
    sent = len(harness.notifier.sent)
    attempts = harness.notifier.attempts
    harness.notifier.fail = True  # the first attempt fails, the retry goes out 3 minutes later
    cid = await _confirm(harness, service, job, printer, kind)
    await until(lambda: harness.notifier.attempts > attempts)
    assert harness.store.outbox_head().expires_at == harness.store.confirmation(cid).expires_at
    harness.clock.advance(minutes=3)
    harness.notifier.fail = False
    await drain(service)
    (message,) = harness.notifier.sent[sent:]
    assert message["expires_in"] == 420
    assert [b.id for row in message["buttons"] for b in row] == [f"{kind}:{cid}:go", f"{kind}:{cid}:cancel"]


async def test_other_messages_have_no_expires_in(harness):
    _service, _job, printer = await print_harness(harness)
    await running(harness, printer)
    assert harness.notifier.sent
    assert all(m["expires_in"] is None for m in harness.notifier.sent)


@pytest.mark.parametrize("kind", ["start", "stop"])
async def test_late_outbox_send_drops_buttons_and_says_expired(harness, kind):
    service, job, printer = await print_harness(harness)
    if kind == "stop":
        await running(harness, printer)
    sent = len(harness.notifier.sent)
    attempts = harness.notifier.attempts
    harness.notifier.fail = True
    cid = await _confirm(harness, service, job, printer, kind)
    await until(lambda: harness.notifier.attempts > attempts)
    text = harness.store.outbox_head().text
    harness.clock.advance(seconds=596)  # 4 s left, under the 5 s minimum
    harness.notifier.fail = False
    await drain(service)
    (message,) = harness.notifier.sent[sent:]
    expired = START_EXPIRED if kind == "start" else STOP_EXPIRED
    assert message["text"] == f"{text}\n\n{expired}"
    assert message["buttons"] == [] and message["expires_in"] is None
    assert harness.store.confirmation(cid).state == "pending"  # nothing else is marked
    assert harness.store.outbox_count() == 0


async def test_outbox_send_with_five_seconds_left_keeps_the_buttons(harness):
    service, job, _printer = await print_harness(harness)
    sent = len(harness.notifier.sent)
    attempts = harness.notifier.attempts
    harness.notifier.fail = True
    await service.prepare_print(job.id)
    await until(lambda: harness.notifier.attempts > attempts)
    harness.clock.advance(seconds=595.5)  # 4.5 s left: ceil gives 5
    harness.notifier.fail = False
    await drain(service)
    (message,) = harness.notifier.sent[sent:]
    assert message["expires_in"] == 5 and message["buttons"]


@pytest.mark.parametrize("kind", ["start", "stop"])
async def test_expired_press_edit_failure_sends_new_message(harness, kind, caplog):
    service, job, printer = await print_harness(harness)
    if kind == "stop":
        await running(harness, printer)
    cid = await _confirm(harness, service, job, printer, kind)
    await until(lambda: harness.store.outbox_count() == 0)
    sent = len(harness.notifier.sent)
    harness.clock.advance(minutes=10)
    harness.notifier.edit_ok = False
    with caplog.at_level(logging.INFO, logger="bambu_mcp"):
        await press(harness, f"{kind}:{cid}:go", "m7")
        await until(lambda: len(harness.notifier.sent) > sent)
    expired = START_EXPIRED if kind == "start" else STOP_EXPIRED
    assert harness.notifier.edits == [("m7", expired)]
    (message,) = harness.notifier.sent[sent:]
    assert message["text"] == expired and message["buttons"] == []
    assert harness.store.confirmation(cid).state == "expired"
    assert cid not in caplog.text and "m7" not in caplog.text
    assert "spent" in caplog.text or "new message" in caplog.text
    assert harness.uploader.calls == [] and "stop" not in harness.events


async def test_spent_press_edit_that_raises_sends_new_message(harness):
    service, job, _printer = await print_harness(harness)
    cid = (await service.prepare_print(job.id)).confirm_id
    await until(lambda: harness.store.outbox_count() == 0)
    sent = len(harness.notifier.sent)
    harness.clock.advance(minutes=10)

    async def broken(message_id, text=None, buttons=None):
        raise RuntimeError("add-in client bug")

    harness.notifier.edit = broken
    await press(harness, f"start:{cid}:go", "m7")
    await until(lambda: len(harness.notifier.sent) > sent)
    assert harness.notifier.sent[-1]["text"] == START_EXPIRED


# -- Task 5 review fixes -------------------------------------------------------------------


async def test_failure_after_the_send_is_unconfirmed_never_not_started(harness):
    service, job, _printer = await print_harness(harness, watch_s=0.3)
    link = harness.links[-1]
    link.answer = None
    link.after_send = RuntimeError("paho broke after the publish")
    cid = (await service.prepare_print(job.id)).confirm_id
    await press(harness, f"start:{cid}:go")
    assert await pressed_to_the_end(harness, cid) == "unconfirmed"
    assert len(link.projects) == 1
    text = harness.notifier.sent[-1]["text"]
    assert "не подтвердил" in text and "не запущена" not in text


async def test_store_failure_in_the_watch_is_unconfirmed(harness, monkeypatch):
    service, job, _printer = await print_harness(harness)
    cid = (await service.prepare_print(job.id)).confirm_id
    real = service._store.set_confirmation_outcome

    def flaky(confirm_id, outcome):
        if outcome == "started":
            raise RuntimeError("disk full")
        real(confirm_id, outcome)

    monkeypatch.setattr(service._store, "set_confirmation_outcome", flaky)
    await press(harness, f"start:{cid}:go")
    assert await pressed_to_the_end(harness, cid) == "unconfirmed"
    assert len(harness.links[-1].projects) == 1
    assert not any("не запущена" in m["text"] for m in harness.notifier.sent)


async def test_upload_deadline_refuses_and_sends_nothing(harness):
    service, job, _printer = await print_harness(harness)
    service._upload_timeout_s = 0.2
    harness.uploader.gate = threading.Event()  # a transfer that trickles on
    cid = (await service.prepare_print(job.id)).confirm_id
    try:
        await press(harness, f"start:{cid}:go")
        assert await pressed_to_the_end(harness, cid) == "refused:upload_timeout"
    finally:
        harness.uploader.gate.set()
    assert harness.links[-1].projects == []
    assert "не уложилась в 5 минут" in harness.notifier.sent[-1]["text"]
    assert harness.store.confirmation(cid).state == "used"
    await press(harness, f"start:{cid}:go")
    await until(lambda: len(harness.notifier.edits) == 2)
    assert harness.links[-1].projects == []


async def test_prepare_press_refusals_are_russian(harness):
    _service, job, _printer = await print_harness(harness, kit=True)
    await press(harness, f"job:{job.id}:print:3", "m5")  # the kit has plates 1 and 2
    await until(lambda: harness.notifier.sent)
    text = harness.notifier.sent[-1]["text"]
    assert "не подготовлена" in text and "пластины 3" in text
    assert "has no plate" not in text and "job" not in text


async def test_press_refuses_a_retired_spool(harness):
    service, job, _printer = await print_harness(harness)
    cid = (await service.prepare_print(job.id)).confirm_id
    harness.store.update_spool(harness.spool, active=False)
    await press(harness, f"start:{cid}:go")
    assert await pressed_to_the_end(harness, cid) == "refused:preflight"
    assert "списана" in harness.notifier.sent[-1]["text"]
    assert harness.uploader.calls == [] and harness.links[-1].projects == []


async def test_press_refuses_a_multi_filament_plate(harness):
    service, job, _printer = await print_harness(harness)
    data = sliced_bytes(((1, 6000, (("1", "PETG", "10.0"), ("2", "PETG", "4.0"))),))
    harness.store.update_job(job.id, result_json=job.result_json.replace(sha(harness.data), sha(data)))
    cache = harness.settings.data_dir / "print-cache"
    cache.mkdir()
    (cache / job.sliced_file).write_bytes(data)
    target = service._print_target(harness.store.job(job.id), 1)
    confirmation = harness.store.add_confirmation(  # as a prepare of an older build would have stored it
        None, "start", job_id=job.id, plate=1, gate_hash=service._gate(target, sha(data)),
        payload_json=json.dumps({"file": job.sliced_file, "plate": 1}), now=harness.clock().timestamp(),
    )
    await press(harness, f"start:{confirmation.id}:go")
    assert await pressed_to_the_end(harness, confirmation.id) == "refused:preflight"
    assert "несколько филаментов" in harness.notifier.sent[-1]["text"]
    assert harness.uploader.calls == [] and harness.links[-1].projects == []


async def test_start_lock_covers_another_confirmation(harness):
    service, job, _printer = await print_harness(harness)
    first = (await service.prepare_print(job.id)).confirm_id
    second = (await service.prepare_print(job.id)).confirm_id
    await until(lambda: harness.store.outbox_count() == 0)
    harness.uploader.gate = threading.Event()
    await press(harness, f"start:{first}:go", "m1")
    await until(lambda: harness.uploader.entered.is_set())
    await press(harness, f"start:{second}:go", "m2")
    await until(lambda: len(harness.notifier.edits) == 2)
    assert harness.notifier.edits[1] == ("m2", "⏳ Уже запускаю печать")
    assert harness.store.confirmation(second).state == "pending"
    harness.uploader.gate.set()
    assert await pressed_to_the_end(harness, first) == "started"
    assert len(harness.links[-1].projects) == 1


async def test_confirmation_id_stays_out_of_logs_and_dedupe(harness, caplog):
    caplog.set_level(logging.DEBUG)
    service, job, _printer = await print_harness(harness)
    harness.uploader.error = UploadError("printer cannot store the file (553)")
    cid = (await service.prepare_print(job.id)).confirm_id
    await press(harness, f"start:{cid}:go")
    assert await pressed_to_the_end(harness, cid) == "refused:upload"
    assert all(cid not in (m["dedupe"] or "") for m in harness.notifier.sent)
    assert cid not in caplog.text
    with pytest.raises(KeyError) as info:
        harness.store.set_confirmation_outcome("ab" * 16, "started")
    assert "ab" * 16 not in str(info.value)


# -- final review fixes ----------------------------------------------------------------------


@pytest.mark.parametrize("entry", ["request_stop", "button"])
async def test_stop_during_prepare(harness, entry):
    """Heating and levelling: the owner may notice a wrong plate there, so the stop works."""
    service, _job, printer = await print_harness(harness)
    await running(harness, printer, task_id="777", state="PREPARE")
    if entry == "request_stop":
        cid = (await service.request_stop()).confirm_id
    else:
        sent = len(harness.notifier.sent)
        await press(harness, "stop:ask:777", "m3")
        await until(lambda: len(harness.notifier.sent) == sent + 1)
        cid = harness.notifier.sent[-1]["buttons"][0][0].id.split(":")[1]
    assert harness.store.confirmation(cid).kind == "stop"
    assert "stop" not in harness.events

    await press(harness, f"stop:{cid}:go", "m7")
    await until(lambda: harness.notifier.edits)
    assert harness.notifier.edits[-1] == ("m7", "⏹ Команда остановки отправлена")
    await press(harness, f"stop:{cid}:go", "m7")
    await until(lambda: len(harness.notifier.edits) == 2)
    assert harness.events.count("stop") == 1


async def test_start_interrupted_by_a_restart_is_unconfirmed(harness):
    service, job, _printer = await print_harness(harness)
    cid = (await service.prepare_print(job.id)).confirm_id
    waiting = (await service.prepare_print(job.id)).confirm_id
    await until(lambda: harness.store.outbox_count() == 0)
    # The press claimed it, then the service stopped before the start was decided.
    assert harness.store.claim_confirmation(cid, harness.clock().timestamp()) is not None
    await harness.stop()
    sent = len(harness.notifier.sent)

    await harness.start()
    await until(lambda: harness.store.confirmation(cid).outcome is not None)
    await until(lambda: harness.store.outbox_count() == 0)

    assert harness.store.confirmation(cid).outcome == "unconfirmed"
    (message,) = harness.notifier.sent[sent:]
    text = message["text"]
    assert "перезапуск" in text and "Не знаю" in text and "экран" in text and "lamp-arm" in text
    assert "—" not in text and ";" not in text
    assert cid not in message["dedupe"]
    conf = harness.store.confirmation(waiting)
    assert (conf.state, conf.outcome) == ("pending", None)
    assert sum(len(link.projects) for link in harness.links) == 0 and harness.uploader.calls == []

    await harness.stop()  # told once
    await harness.start()
    await harness.service.settle()
    await until(lambda: harness.store.outbox_count() == 0)
    assert len(harness.notifier.sent) == sent + 1


async def test_fetch_refusal_is_russian_for_the_owner_english_for_claude(harness):
    service, job, _printer = await print_harness(harness)
    harness.slicer.error = FetchError(
        "slice-agent answered HTTP 404 for the sliced file", "ПК не отдал нарезанный файл (HTTP 404)"
    )
    with pytest.raises(PrintRefused) as info:
        await service.prepare_print(job.id)
    assert "HTTP 404" in str(info.value) and "slice-agent" in str(info.value)
    (line,) = info.value.lines
    assert "ПК не отдал" in line and "slice-agent" not in line
