import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from bambu_mcp.config import Settings
from bambu_mcp.events import Event, EventKind
from bambu_mcp.journal import Journal, rebook_grams
from bambu_mcp.printer.state import PrinterState
from bambu_mcp.store.db import Store

T0 = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
IDENTITY = ("part", "42", "")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "bambu.db")
    s.migrate()
    yield s
    s.close()


def ev(kind: EventKind, at: datetime, **kw) -> Event:
    st = replace(PrinterState.empty(), subtask_name="part", task_id="42", gcode_state="FINISH")
    return Event(kind, IDENTITY, at, st, **kw)


def test_close_deducts_grams_set_earlier_without_spool(store):
    spool = store.add_spool("Elegoo", "PLA", "Black", "#000000", None, 600.0, 1000.0)
    store.map_slot(0, 1, spool)
    journal = Journal(store, Settings())
    print_id = journal.on_event(ev(EventKind.STARTED, T0))
    rebook_grams(store, print_id, None, 30.0)  # grams via MCP before the spool was known

    journal.on_event(ev(EventKind.FINISHED, T0 + timedelta(hours=1)), slot=(0, 1))

    row = store.print_by_id(print_id)
    assert row.spool_id == spool
    assert row.deducted_spool_id == spool and row.deducted_g == pytest.approx(30.0)
    assert store.spools()[0].remaining_g == pytest.approx(970.0)
    assert row.cost_filament_uah == pytest.approx(30 * 0.6)


def test_close_without_open_row_opens_one_at_first_seen(store, caplog):
    journal = Journal(store, Settings())
    first_seen = T0 - timedelta(minutes=90)
    with caplog.at_level(logging.WARNING):
        print_id = journal.on_event(ev(EventKind.FINISHED, T0), first_seen=first_seen)
    assert print_id is not None
    row = store.print_by_id(print_id)
    assert row.started_at == first_seen
    assert row.outcome == "finished"
    assert row.actual_min == pytest.approx(90)


def test_cost_failure_still_closes_the_row(store, caplog):
    journal = Journal(store, Settings(power_w={"PETG": 150.0}))  # no PLA entry
    print_id = journal.on_event(ev(EventKind.STARTED, T0))
    with caplog.at_level(logging.ERROR):
        assert journal.on_event(ev(EventKind.FINISHED, T0 + timedelta(minutes=30))) == print_id
    row = store.print_by_id(print_id)
    assert row.ended_at is not None and row.actual_min == pytest.approx(30)
    assert row.kwh is None and row.cost_power_uah is None
    assert "cost" in caplog.text


def test_end_unknown_closes_without_duration_or_cost(store):
    journal = Journal(store, Settings())
    print_id = journal.on_event(ev(EventKind.STARTED, T0))
    journal.on_event(ev(EventKind.FINISHED, T0 + timedelta(days=2), end_unknown=True))
    row = store.print_by_id(print_id)
    assert row.outcome == "finished" and row.ended_at is not None
    assert row.actual_min is None and row.kwh is None and row.cost_power_uah is None
