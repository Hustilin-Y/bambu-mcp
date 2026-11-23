import asyncio
import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from bambu_mcp.config import Secrets, Settings
from bambu_mcp.notify import LogNotifier, Press, SendError, ServiceNotifyClient
from bambu_mcp.printer.link import LinkStatus
from bambu_mcp.service import (
    AUTH_FAILED_TEXT,
    TARIFF_META_KEY,
    Service,
    build_notifier,
    notify_dedupe,
)
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

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[tuple[str, str | None]] = []
        self.press_queue: asyncio.Queue[Press] = asyncio.Queue()
        self.fail = False
        self.attempts = 0

    async def send(self, text, buttons=None, files=None, dedupe=None, expires_in=None, silent=False):
        self.attempts += 1
        if self.fail:
            raise SendError(503, "telegram_unreachable")
        self.sent.append(
            {"text": text, "buttons": buttons or [], "files": files or [], "dedupe": dedupe}
        )
        return f"m{len(self.sent)}"

    async def edit(self, message_id, text=None, buttons=None):
        self.edits.append((message_id, text))

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

    def _factory(self, on_report, on_status) -> FakeLink:
        link = FakeLink(on_report, on_status)
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


async def test_running_to_idle_is_reported_as_failed(harness):
    await harness.start()
    await harness.feed("running_layer0", "running_layer2")
    await harness.feed_report(IDLE_NO_JOB)
    failed = [m for m in harness.notifier.sent if m["text"].startswith("❌ Печать прервана ошибкой")]
    assert len(failed) == 1 and "cube" in failed[0]["text"]
    row = harness.store.print_by_identity(("cube", "1795385329", None))
    assert row.outcome == "failed" and row.actual_min is not None


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
