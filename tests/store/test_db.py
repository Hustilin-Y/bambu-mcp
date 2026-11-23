import sqlite3
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from bambu_mcp.events import SeenStore
from bambu_mcp.store.db import Store


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "bambu.db")
    s.migrate()
    yield s
    s.close()


def _spool(store: Store, net_g: float = 1000, price: float = 700) -> int:
    return store.add_spool("Bambu", "PLA", "Black", "#000000", "Bambu PLA Basic", price, net_g)


def test_migrate_is_idempotent_and_seeds_tariff(tmp_path: Path):
    s = Store(tmp_path / "a.db")
    s.migrate()
    s.migrate()
    t = s.tariff_on(date(2026, 11, 22))
    assert t.day_uah == 4.32
    assert t.night_uah is None
    assert t.valid_from == date(2024, 6, 1)
    assert s._conn().execute("PRAGMA user_version").fetchone()[0] == 4
    assert s._conn().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    s.close()


def test_tariff_history(store: Store):
    store.add_tariff(date(2027, 1, 1), 4.80, None)
    assert store.tariff_on(date(2026, 12, 31)).day_uah == 4.32
    assert store.tariff_on(date(2027, 1, 1)).day_uah == 4.80
    with pytest.raises(LookupError):
        store.tariff_on(date(2024, 5, 31))


def test_spool_remaining(store: Store):
    sid = _spool(store)
    store.adjust_spool(sid, 14.2)
    assert store.spools()[0].remaining_g == pytest.approx(985.8)
    store.set_spool_weight(sid, 900)
    spool = store.spools()[0]
    assert spool.remaining_g == 900
    assert spool.price_per_kg_uah == pytest.approx(700)
    store.adjust_spool(sid, 5000)
    assert store.spools()[0].remaining_g == 0


def test_update_and_inactive_spools(store: Store):
    sid = _spool(store)
    store.update_spool(sid, price_uah=650.5, filament_preset=None, active=False)
    assert store.spools() == []
    (spool,) = store.spools(active_only=False)
    assert spool.price_uah == 650.5
    assert spool.filament_preset is None
    assert spool.active is False
    with pytest.raises(ValueError):
        store.update_spool(sid, bogus=1)


def test_slot_mapping_replaces(store: Store):
    a, b = _spool(store), _spool(store)
    assert store.slot_spool(0, 1) is None
    store.map_slot(0, 1, a)
    assert store.slot_spool(0, 1).id == a
    store.map_slot(0, 1, b)
    assert store.slot_spool(0, 1).id == b
    assert store.slot_spool(0, 0) is None
    store.map_slot(0, 1, None)
    assert store.slot_spool(0, 1) is None


def test_plates_default(store: Store):
    a = store.add_plate("Textured PEI", "textured_pei")
    b = store.add_plate("Cool", "cool_plate")
    store.set_default_plate(b)
    store.set_default_plate(b)
    assert [(p.id, p.is_default) for p in store.plates()] == [(a, False), (b, True)]
    with pytest.raises(KeyError):
        store.set_default_plate(99)


def test_print_open_close_by_identity(store: Store):
    t0 = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 11, 22, 11, 30, tzinfo=timezone.utc)
    sid = _spool(store)
    pid = store.open_print(("benchy.3mf", "123", None), t0, "job")
    row = store.print_by_identity(("benchy.3mf", "123", ""))
    assert row is not None and row.id == pid
    assert row.identity == ("benchy.3mf", "123", "")
    assert row.outcome is None and row.ended_at is None and row.started_at == t0
    store.close_print(pid, "finished", t1, 90.0)
    store.set_print_fields(
        pid, spool_id=sid, grams=14.2, kwh=0.35, cost_filament_uah=9.94, cost_power_uah=1.512, fit="ok"
    )
    row = store.print_by_identity(("benchy.3mf", "123", ""))
    assert (row.outcome, row.ended_at, row.actual_min) == ("finished", t1, 90.0)
    assert (row.spool_id, row.grams, row.kwh, row.fit) == (sid, 14.2, 0.35, "ok")
    assert row.cost_power_uah == 1.512
    assert store.print_by_identity(("other", "", "")) is None
    with pytest.raises(ValueError):
        store.set_print_fields(pid, outcome="x")


def test_history_filter_and_order(store: Store):
    for i, name in enumerate(["Benchy.3mf", "cube.3mf", "benchy_v2.3mf"]):
        store.open_print((name, str(i), ""), datetime(2026, 11, 20 + i), "external")
    assert [r.identity[0] for r in store.history(None, 10)] == ["benchy_v2.3mf", "cube.3mf", "Benchy.3mf"]
    assert [r.identity[0] for r in store.history("benchy", 10)] == ["benchy_v2.3mf", "Benchy.3mf"]
    assert store.history("100%", 10) == []
    assert len(store.history(None, 1)) == 1


def test_meta(store: Store):
    assert store.get_meta("tariff_reminded_on") is None
    store.set_meta("tariff_reminded_on", "2026-11-01")
    store.set_meta("tariff_reminded_on", "2026-11-22")
    assert store.get_meta("tariff_reminded_on") == "2026-11-22"


def test_seen_survives_reopen(tmp_path: Path):
    path = tmp_path / "seen.db"
    first = Store(path)
    first.migrate()
    assert not first.seen_contains("k")
    first.seen_add("k")
    first.seen_add("k")
    first.close()
    second = Store(path)
    second.migrate()
    assert second.seen_contains("k")
    second.close()


def test_seen_pruned_after_30_days(tmp_path: Path):
    s = Store(tmp_path / "seen.db")
    s.migrate()
    s.seen_add("fresh")
    s.seen_add("stale")
    with s._conn() as conn:
        conn.execute("UPDATE seen SET seen_at = ? WHERE key = 'stale'", (time.time() - 31 * 86400,))
    s.migrate()
    assert s.seen_contains("fresh") and not s.seen_contains("stale")
    s.close()


def test_store_satisfies_seen_store_protocol(store: Store):
    seen: SeenStore = store
    seen.seen_add("x")
    assert seen.seen_contains("x")


def test_usable_from_other_thread(store: Store):
    errors: list[BaseException] = []

    def work() -> None:
        try:
            store.seen_add("from-thread")
            store.add_tariff(date(2027, 1, 1), 5.0, 2.5)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=work)
    t.start()
    t.join()
    assert not errors
    assert store.seen_contains("from-thread")
    assert store.tariff_on(date(2027, 2, 1)).night_uah == 2.5
    assert len({id(c) for c in store._conns}) == 2


def test_schema_tables_exist(tmp_path: Path):
    path = tmp_path / "x.db"
    s = Store(path)
    s.migrate()
    s.close()
    con = sqlite3.connect(path)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert {"seen", "meta", "spools", "slots", "plates", "tariffs", "prints"} <= tables


def _closed_print(store: Store) -> int:
    pid = store.open_print(("part", "1", "s"), datetime(2026, 11, 22, 8, tzinfo=timezone.utc), "external")
    store.close_print(pid, "finished", datetime(2026, 11, 22, 9, tzinfo=timezone.utc), 60)
    return pid


def _remaining(store: Store) -> dict[int, float]:
    return {s.id: s.remaining_g for s in store.spools(active_only=False)}


def test_migrate_v1_database_adds_deduction_columns(tmp_path: Path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    # schema.sql is the frozen version-1 schema, later versions are ALTER steps.
    v1 = (Path(__file__).parents[2] / "src/bambu_mcp/store/schema.sql").read_text(encoding="utf-8")
    conn.executescript(v1 + "PRAGMA user_version = 1;")
    conn.execute(
        "INSERT INTO prints (name, source, started_at) VALUES ('old', 'external', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    s = Store(path)
    s.migrate()
    (row,) = s.history(None, 10)
    assert row.identity[0] == "old"
    assert (row.deducted_spool_id, row.deducted_g) == (None, None)
    assert s._conn().execute("PRAGMA user_version").fetchone()[0] == 4
    s.close()


def test_rebook_moves_the_recorded_deduction(store: Store):
    a, b = _spool(store), _spool(store)
    pid = _closed_print(store)
    store.rebook_print_grams(pid, a, 14, 9.8)
    row = store.print_by_id(pid)
    assert (row.spool_id, row.grams, row.deducted_spool_id, row.deducted_g) == (a, 14, a, 14)
    assert row.cost_filament_uah == 9.8
    assert _remaining(store) == {a: 986, b: 1000}

    # Someone changes the spool without moving grams (old behaviour): the refund
    # still goes to the spool that was actually debited.
    store.set_print_fields(pid, spool_id=b)
    store.rebook_print_grams(pid, b, 20, None)
    assert _remaining(store) == {a: 1000, b: 980}

    store.rebook_print_grams(pid, None, 20, None)  # "do not deduct"
    assert _remaining(store) == {a: 1000, b: 1000}
    row = store.print_by_id(pid)
    assert (row.spool_id, row.grams, row.deducted_spool_id, row.deducted_g) == (None, 20, None, None)


def test_rebook_records_floored_deduction(store: Store):
    a = _spool(store)
    store.set_spool_weight(a, 5)
    pid = _closed_print(store)
    store.rebook_print_grams(pid, a, 14, None)
    assert store.print_by_id(pid).deducted_g == 5
    assert _remaining(store) == {a: 0}
    store.rebook_print_grams(pid, a, 3, None)  # correction refunds only the 5 g removed
    assert _remaining(store) == {a: 2}


def test_rebook_unknown_ids(store: Store):
    a = _spool(store)
    with pytest.raises(KeyError):
        store.rebook_print_grams(999, a, 1, None)
    pid = _closed_print(store)
    with pytest.raises(KeyError):
        store.rebook_print_grams(pid, 999, 1, None)
    assert _remaining(store) == {a: 1000}


def test_migrate_v2_database_adds_outbox(tmp_path: Path):
    s = Store(tmp_path / "v2.db")
    conn = s._conn()
    v1 = (Path(__file__).parents[2] / "src/bambu_mcp/store/schema.sql").read_text(encoding="utf-8")
    conn.executescript(
        v1
        + "ALTER TABLE prints ADD COLUMN deducted_spool_id INTEGER REFERENCES spools(id);\n"
        + "ALTER TABLE prints ADD COLUMN deducted_g REAL;\nPRAGMA user_version = 2;"
    )
    s.migrate()
    assert s._conn().execute("PRAGMA user_version").fetchone()[0] == 4
    assert s.outbox_head() is None
    s.close()


def test_outbox_is_fifo_and_keeps_fields(store: Store):
    buttons = [[{"id": "fit:1:ok", "text": "OK"}]]
    assert store.outbox_add("finished", "done", "k1", snapshot=True, now=100.0)
    assert store.outbox_add("fit", "how?", "k2", buttons=buttons, now=101.0)
    head = store.outbox_head()
    assert (head.kind, head.text, head.dedupe, head.snapshot, head.buttons) == (
        "finished", "done", "k1", True, None
    )
    assert (head.created_at, head.attempts, head.next_attempt_at) == (100.0, 0, 100.0)
    store.outbox_defer(head.id, attempts=1, next_attempt_at=130.0)
    deferred = store.outbox_head()
    assert deferred.id == head.id and deferred.attempts == 1 and deferred.next_attempt_at == 130.0
    store.outbox_delete(head.id)
    second = store.outbox_head()
    assert second.dedupe == "k2" and second.buttons == buttons and second.snapshot is False
    assert store.outbox_count() == 1


def test_outbox_ignores_a_pending_duplicate_key(store: Store):
    assert store.outbox_add("started", "a", "same", now=1.0)
    assert not store.outbox_add("started", "b", "same", now=2.0)
    assert store.outbox_count() == 1


def test_transaction_commits_seen_and_outbox_together(store: Store):
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.seen_add("evt")
            store.outbox_add("started", "a", "evt", now=1.0)
            raise RuntimeError("crash before commit")
    assert not store.seen_contains("evt") and store.outbox_count() == 0

    with store.transaction():
        store.seen_add("evt")
        store.outbox_add("started", "a", "evt", now=1.0)
    assert store.seen_contains("evt") and store.outbox_count() == 1


def test_nested_transaction_rolls_back_only_the_inner_part(store: Store):
    with store.transaction():
        store.seen_add("outer")
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.seen_add("inner")
                raise RuntimeError("inner fails")
        store.outbox_add("x", "t", "k", now=1.0)
    assert store.seen_contains("outer") and not store.seen_contains("inner")
    assert store.outbox_count() == 1


def test_transaction_is_invisible_to_other_threads_until_commit(store: Store):
    seen_from_thread = []
    with store.transaction():
        store.seen_add("pending")
        t = threading.Thread(target=lambda: seen_from_thread.append(store.seen_contains("pending")))
        t.start()
        t.join()
    assert seen_from_thread == [False]
    assert store.seen_contains("pending")


def test_open_prints_and_close_without_minutes(store: Store):
    a = store.open_print(("a", "1", ""), datetime(2026, 1, 1, tzinfo=timezone.utc), "external")
    b = store.open_print(("b", "2", ""), datetime(2026, 1, 2, tzinfo=timezone.utc), "external")
    store.close_print(a, "failed", datetime(2026, 1, 3, tzinfo=timezone.utc), None)
    assert [r.id for r in store.open_prints()] == [b]
    assert store.print_by_id(a).actual_min is None


def test_migrate_v3_to_v4(tmp_path: Path):
    path = tmp_path / "v3.db"
    conn = sqlite3.connect(path)
    v1 = (Path(__file__).parents[2] / "src/bambu_mcp/store/schema.sql").read_text(encoding="utf-8")
    conn.executescript(
        v1
        + "ALTER TABLE prints ADD COLUMN deducted_spool_id INTEGER REFERENCES spools(id);\n"
        + "ALTER TABLE prints ADD COLUMN deducted_g REAL;\n"
        + "CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,"
        " text TEXT NOT NULL, buttons TEXT, snapshot INTEGER NOT NULL DEFAULT 0,"
        " dedupe TEXT NOT NULL UNIQUE, created_at REAL NOT NULL,"
        " attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL);\n"
        + "PRAGMA user_version = 3;"
    )
    conn.execute(
        "INSERT INTO prints (name, source, started_at) VALUES ('kept', 'external', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO outbox (kind, text, dedupe, created_at, next_attempt_at)"
        " VALUES ('finished', 'done', 'k1', 1.0, 1.0)"
    )
    conn.commit()
    conn.close()

    s = Store(path)
    s.migrate()
    assert s._conn().execute("PRAGMA user_version").fetchone()[0] == 4
    (row,) = s.history(None, 10)
    assert row.identity[0] == "kept"
    assert s.outbox_head().dedupe == "k1"
    assert s.jobs(10) == []
    job_id = s.add_job("lamp-arm", "part", "{}", "a" * 64, datetime(2026, 11, 23, tzinfo=timezone.utc))
    job = s.job(job_id)
    assert (job.name, job.kind, job.status, job.created_at.year) == ("lamp-arm", "part", "new", 2026)
    with pytest.raises(sqlite3.IntegrityError):
        s._conn().execute("UPDATE jobs SET status = 'bogus' WHERE id = ?", (job_id,))
    s.close()
