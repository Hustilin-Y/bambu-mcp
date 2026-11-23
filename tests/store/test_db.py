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
    assert s._conn().execute("PRAGMA user_version").fetchone()[0] == 1
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
