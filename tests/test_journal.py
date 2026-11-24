import json
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


# -- matching prints to sliced jobs (phase 2) ----------------------------------


def sliced_job(
    store,
    name="Lamp Arm",
    kind="part",
    plates=((1, {"Bambu PLA Basic": 12.5}),),
    spool_id=None,
    status="sliced",
) -> int:
    job_id = store.add_job(name, kind, "{}", "a" * 64, T0)
    result = {
        "plates": [{"index": i, "minutes": 30.0, "grams": g} for i, g in plates],
        "slot": {"ams": 0, "slot": 0, "spool_id": spool_id, "tray_type": "PLA", "color": "white"},
    }
    store.update_job(job_id, status=status, result_json=json.dumps(result))
    return job_id


KIT_PLATES = ((1, {"PLA": 10.0}), (2, {"PLA": 4.0, "PLA2": 1.5}))
LATER = T0 + timedelta(hours=1)


def job_ev(
    kind: EventKind,
    name: str,
    at: datetime = T0,
    plate: int | None = 1,
    percent: int | None = None,
    **kw,
) -> Event:
    st = replace(
        PrinterState.empty(),
        subtask_name=name,
        task_id="77",
        gcode_state="RUNNING",
        plate=plate,
        percent=percent,
    )
    return Event(kind, (name, "77", ""), at, st, **kw)


def add_spool(store) -> int:
    return store.add_spool("Elegoo", "PLA", "White", "#FFFFFF", None, 600.0, 1000.0)


def remaining(store, spool_id: int) -> float:
    return next(s.remaining_g for s in store.spools(active_only=False) if s.id == spool_id)


@pytest.mark.parametrize("suffix", ["", ".gcode.3mf", ".3mf", ".GCODE.3MF"])
def test_match_by_subtask_name(store, suffix):
    spool = add_spool(store)
    job_id = sliced_job(store, spool_id=spool)
    journal = Journal(store, Settings())

    print_id = journal.on_event(job_ev(EventKind.STARTED, f"lamp-arm-{job_id}-p1{suffix}", plate=3))

    row = store.print_by_id(print_id)
    assert (row.source, row.job_id, row.plate) == ("job", job_id, 1)  # a part is plate 1
    assert row.spool_id == spool and row.grams == pytest.approx(12.5)
    assert row.deducted_spool_id == spool and row.deducted_g == pytest.approx(12.5)
    assert row.cost_filament_uah == pytest.approx(12.5 * 0.6)
    assert remaining(store, spool) == pytest.approx(987.5)

    # The close keeps the job's spool even when the feeding slot maps to another one.
    other = add_spool(store)
    store.map_slot(0, 1, other)
    finished = job_ev(EventKind.FINISHED, f"lamp-arm-{job_id}-p1{suffix}", LATER)
    journal.on_event(finished, slot=(0, 1))
    row = store.print_by_id(print_id)
    assert row.spool_id == spool and row.outcome == "finished"
    assert remaining(store, spool) == pytest.approx(987.5)
    assert remaining(store, other) == pytest.approx(1000.0)


def test_kit_plate_comes_from_the_report(store):
    spool = add_spool(store)
    job_id = sliced_job(store, name="Desk Kit", kind="kit", plates=KIT_PLATES, spool_id=spool)
    journal = Journal(store, Settings())

    print_id = journal.on_event(job_ev(EventKind.STARTED, f"desk-kit-{job_id}-kit", plate=2))

    row = store.print_by_id(print_id)
    assert (row.job_id, row.plate, row.spool_id) == (job_id, 2, spool)
    assert row.grams == pytest.approx(5.5)
    assert remaining(store, spool) == pytest.approx(994.5)


def test_kit_without_plate_in_the_report_uses_plate_1_grams(store):
    """Ruling R2: the job matches, the plate stays unknown, plate 1's grams are booked."""
    spool = add_spool(store)
    job_id = sliced_job(store, name="Desk Kit", kind="kit", plates=KIT_PLATES, spool_id=spool)
    started = job_ev(EventKind.STARTED, f"desk-kit-{job_id}-kit", plate=None)
    print_id = Journal(store, Settings()).on_event(started)
    row = store.print_by_id(print_id)
    assert (row.job_id, row.plate, row.grams) == (job_id, None, pytest.approx(10.0))


def test_kit_plate_outside_the_job_is_not_matched(store):
    spool = add_spool(store)
    job_id = sliced_job(store, name="Desk Kit", kind="kit", plates=KIT_PLATES, spool_id=spool)
    started = job_ev(EventKind.STARTED, f"desk-kit-{job_id}-kit", plate=3)
    print_id = Journal(store, Settings()).on_event(started)
    row = store.print_by_id(print_id)
    assert (row.source, row.job_id, row.spool_id, row.grams) == ("external", None, None, None)
    assert remaining(store, spool) == pytest.approx(1000.0)


@pytest.mark.parametrize(
    "name",
    [
        "lamp-arm",  # the owner renamed the file in Studio
        "lamp-arm-v2",
        "lamp-arm-{id}-p1-copy",
        "lamp-arm-{id}-p2",  # a part is only ever p1
        "lamp-arm-{id}-kit",  # the job is a part
        "other-part-{id}-p1",  # the slug is not the job's
        "Lamp-Arm-{id}-p1",
        "lamp-arm-0{id}-p1",
        "lamp-arm-{id}-p1.stl",
        "-{id}-p1",
    ],
)
def test_renamed_file_falls_back_to_question(store, name):
    spool = add_spool(store)
    job_id = sliced_job(store, spool_id=spool)
    journal = Journal(store, Settings())
    subtask = name.format(id=job_id)

    print_id = journal.on_event(job_ev(EventKind.STARTED, subtask))

    row = store.print_by_id(print_id)
    assert (row.source, row.job_id, row.plate) == ("external", None, None)
    assert (row.spool_id, row.grams) == (None, None)
    assert remaining(store, spool) == pytest.approx(1000.0)
    # Phase 1 at the close: the slot's spool, else the question (spool_id stays None).
    journal.on_event(job_ev(EventKind.FINISHED, subtask, LATER))
    assert store.print_by_id(print_id).spool_id is None


@pytest.mark.parametrize("status", ["new", "slicing", "slice_failed"])
def test_job_not_sliced_is_not_matched(store, status):
    spool = add_spool(store)
    job_id = sliced_job(store, spool_id=spool, status=status)
    print_id = Journal(store, Settings()).on_event(job_ev(EventKind.STARTED, f"lamp-arm-{job_id}-p1"))
    row = store.print_by_id(print_id)
    assert (row.job_id, row.spool_id) == (None, None)
    assert remaining(store, spool) == pytest.approx(1000.0)


@pytest.mark.parametrize("job_id", ["999", "9" * 18, "9" * 40])
def test_unknown_job_id_ignored(store, job_id):
    spool = add_spool(store)
    sliced_job(store, spool_id=spool)
    print_id = Journal(store, Settings()).on_event(job_ev(EventKind.STARTED, f"lamp-arm-{job_id}-p1"))
    row = store.print_by_id(print_id)
    assert (row.source, row.job_id, row.spool_id) == ("external", None, None)
    assert remaining(store, spool) == pytest.approx(1000.0)


@pytest.mark.parametrize(
    "result",
    [
        None,
        "not json",
        "[]",
        json.dumps({"plates": [{"index": 2, "grams": {"PLA": 1.0}}], "slot": {"spool_id": None}}),
        json.dumps({"plates": [{"index": 1, "grams": {"PLA": "x"}}], "slot": {"spool_id": None}}),
        json.dumps({"plates": [{"index": 1, "grams": {"PLA": float("nan")}}]}),
        json.dumps({"plates": [{"index": True, "grams": {"PLA": 1.0}}]}),
        json.dumps({"plates": "nope"}),
    ],
)
def test_result_without_the_plate_keeps_phase_1(store, result):
    job_id = sliced_job(store)
    store.update_job(job_id, result_json=result)
    print_id = Journal(store, Settings()).on_event(job_ev(EventKind.STARTED, f"lamp-arm-{job_id}-p1"))
    row = store.print_by_id(print_id)
    assert (row.source, row.job_id, row.grams) == ("external", None, None)


def test_resliced_job_uses_its_current_result(store):
    spool = add_spool(store)
    job_id = sliced_job(store, spool_id=spool)
    sliced_job_result = json.loads(store.job(job_id).result_json)
    sliced_job_result["plates"][0]["grams"] = {"PLA": 20.0}
    store.update_job(job_id, result_json=json.dumps(sliced_job_result))
    print_id = Journal(store, Settings()).on_event(job_ev(EventKind.STARTED, f"lamp-arm-{job_id}-p1"))
    assert store.print_by_id(print_id).grams == pytest.approx(20.0)


@pytest.mark.parametrize("spool_id", [None, 999])
def test_job_without_its_spool_records_grams_and_keeps_the_question(store, spool_id):
    job_id = sliced_job(store, spool_id=spool_id)
    journal = Journal(store, Settings())
    print_id = journal.on_event(job_ev(EventKind.STARTED, f"lamp-arm-{job_id}-p1"))
    row = store.print_by_id(print_id)
    assert (row.job_id, row.plate, row.spool_id, row.grams) == (job_id, 1, None, pytest.approx(12.5))
    assert row.deducted_g is None

    # Phase 1 at the close: the slot's spool takes the predicted grams.
    spool = add_spool(store)
    store.map_slot(0, 1, spool)
    journal.on_event(job_ev(EventKind.FINISHED, f"lamp-arm-{job_id}-p1", LATER), slot=(0, 1))
    assert store.print_by_id(print_id).spool_id == spool
    assert remaining(store, spool) == pytest.approx(987.5)


def test_match_deducts_once_across_restart(tmp_path):
    path = tmp_path / "bambu.db"
    first = Store(path)
    first.migrate()
    spool = add_spool(first)
    job_id = sliced_job(first, spool_id=spool)
    name = f"lamp-arm-{job_id}-p1"
    print_id = Journal(first, Settings()).on_event(job_ev(EventKind.STARTED, name))
    first.close()

    second = Store(path)  # the service restarts mid-print and sees the start again
    second.migrate()
    journal = Journal(second, Settings())
    assert journal.on_event(job_ev(EventKind.STARTED, name, T0 + timedelta(minutes=5))) == print_id
    journal.on_event(job_ev(EventKind.FINISHED, name, LATER))
    rows = second.history(None, 10)
    assert [r.id for r in rows] == [print_id]
    assert rows[0].job_id == job_id and rows[0].deducted_g == pytest.approx(12.5)
    assert remaining(second, spool) == pytest.approx(987.5)
    second.close()


def test_close_without_start_matches_the_job(store):
    spool = add_spool(store)
    job_id = sliced_job(store, spool_id=spool)
    print_id = Journal(store, Settings()).on_event(
        job_ev(EventKind.FINISHED, f"lamp-arm-{job_id}-p1", T0), first_seen=T0 - timedelta(minutes=30)
    )
    row = store.print_by_id(print_id)
    assert (row.job_id, row.spool_id, row.outcome) == (job_id, spool, "finished")
    assert remaining(store, spool) == pytest.approx(987.5)


def test_booking_failure_keeps_the_row_as_external(store, monkeypatch, caplog):
    spool = add_spool(store)
    job_id = sliced_job(store, spool_id=spool)

    def broken(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(store, "rebook_print_grams", broken)
    with caplog.at_level(logging.ERROR):
        print_id = Journal(store, Settings()).on_event(job_ev(EventKind.STARTED, f"lamp-arm-{job_id}-p1"))
    row = store.print_by_id(print_id)
    assert (row.source, row.job_id, row.plate, row.spool_id) == ("external", None, None, None)
    assert remaining(store, spool) == pytest.approx(1000.0)
    assert "sliced job" in caplog.text


@pytest.mark.parametrize("kind", [EventKind.FAILED, EventKind.CANCELLED])
@pytest.mark.parametrize(("percent", "grams"), [(40, 5.0), (33, 4.1), (0, 0.0), (100, 12.5)])
def test_unfinished_match_keeps_the_printed_share(tmp_path, kind, percent, grams):
    path = tmp_path / "bambu.db"
    store = Store(path)
    store.migrate()
    spool = add_spool(store)
    other = add_spool(store)
    store.map_slot(0, 1, other)
    job_id = sliced_job(store, spool_id=spool)
    name = f"lamp-arm-{job_id}-p1"
    journal = Journal(store, Settings())
    print_id = journal.on_event(job_ev(EventKind.STARTED, name))
    assert remaining(store, spool) == pytest.approx(987.5)

    journal.on_event(job_ev(kind, name, LATER, percent=percent), slot=(0, 1))

    row = store.print_by_id(print_id)
    assert (row.outcome, row.job_id, row.spool_id) == (str(kind), job_id, spool)  # no question
    assert row.grams == pytest.approx(grams)
    assert row.cost_filament_uah == pytest.approx(grams * 0.6)
    assert remaining(store, spool) == pytest.approx(1000.0 - grams)
    assert remaining(store, other) == pytest.approx(1000.0)
    store.close()

    again = Store(path)  # a restart replays the end: nothing moves twice
    again.migrate()
    Journal(again, Settings()).on_event(job_ev(kind, name, LATER, percent=percent), slot=(0, 1))
    assert again.print_by_id(print_id).grams == pytest.approx(grams)
    assert remaining(again, spool) == pytest.approx(1000.0 - grams)
    again.close()


@pytest.mark.parametrize("kind", [EventKind.FAILED, EventKind.CANCELLED])
@pytest.mark.parametrize("lost", [False, True])
def test_unfinished_match_without_percent_is_refunded_and_asked(tmp_path, kind, lost):
    path = tmp_path / "bambu.db"
    store = Store(path)
    store.migrate()
    spool = add_spool(store)
    other = add_spool(store)
    store.map_slot(0, 1, other)
    job_id = sliced_job(store, spool_id=spool)
    name = f"lamp-arm-{job_id}-p1"
    journal = Journal(store, Settings())
    print_id = journal.on_event(job_ev(EventKind.STARTED, name))

    end = job_ev(kind, name, LATER, percent=None, end_unknown=lost, lost=lost)
    journal.on_event(end, slot=None if lost else (0, 1))

    row = store.print_by_id(print_id)
    # spool_id None: the service asks the phase 1 spool question, even with the slot's spool known.
    assert (row.outcome, row.job_id, row.spool_id) == (str(kind), job_id, None)
    assert row.grams == pytest.approx(12.5) and row.deducted_g is None
    assert row.cost_filament_uah is None
    assert remaining(store, spool) == pytest.approx(1000.0)
    assert remaining(store, other) == pytest.approx(1000.0)
    store.close()

    again = Store(path)
    again.migrate()
    Journal(again, Settings()).on_event(end, slot=None if lost else (0, 1))
    assert again.print_by_id(print_id).spool_id is None
    assert remaining(again, spool) == pytest.approx(1000.0)
    assert remaining(again, other) == pytest.approx(1000.0)
    again.close()


def test_kit_plate_learnt_at_the_close_rebooks_its_grams(store):
    spool = add_spool(store)
    job_id = sliced_job(store, name="Desk Kit", kind="kit", plates=KIT_PLATES, spool_id=spool)
    name = f"desk-kit-{job_id}-kit"
    journal = Journal(store, Settings())
    print_id = journal.on_event(job_ev(EventKind.STARTED, name, plate=None))
    assert remaining(store, spool) == pytest.approx(990.0)  # R2: plate 1 grams meanwhile

    journal.on_event(job_ev(EventKind.FINISHED, name, LATER, plate=2))

    row = store.print_by_id(print_id)
    assert (row.plate, row.spool_id) == (2, spool)
    assert row.grams == pytest.approx(5.5)
    assert remaining(store, spool) == pytest.approx(994.5)


def test_kit_plate_learnt_at_a_failed_close_takes_its_share(store):
    spool = add_spool(store)
    job_id = sliced_job(store, name="Desk Kit", kind="kit", plates=KIT_PLATES, spool_id=spool)
    name = f"desk-kit-{job_id}-kit"
    journal = Journal(store, Settings())
    print_id = journal.on_event(job_ev(EventKind.STARTED, name, plate=None))
    journal.on_event(job_ev(EventKind.FAILED, name, LATER, plate=2, percent=50))
    row = store.print_by_id(print_id)
    assert (row.plate, row.grams) == (2, pytest.approx(2.8))  # 5.5 * 50 % = 2.75
    assert remaining(store, spool) == pytest.approx(997.2)


def test_kit_plate_at_the_close_outside_the_result_keeps_the_row(store):
    spool = add_spool(store)
    job_id = sliced_job(store, name="Desk Kit", kind="kit", plates=KIT_PLATES, spool_id=spool)
    name = f"desk-kit-{job_id}-kit"
    journal = Journal(store, Settings())
    print_id = journal.on_event(job_ev(EventKind.STARTED, name, plate=None))
    journal.on_event(job_ev(EventKind.FINISHED, name, LATER, plate=7))
    row = store.print_by_id(print_id)
    assert (row.plate, row.grams) == (None, pytest.approx(10.0))
    assert remaining(store, spool) == pytest.approx(990.0)


def test_inactive_spool_is_treated_as_gone(store):
    spool = add_spool(store)
    store.update_spool(spool, active=False)  # retired after the slice
    job_id = sliced_job(store, spool_id=spool)
    name = f"lamp-arm-{job_id}-p1"
    journal = Journal(store, Settings())
    print_id = journal.on_event(job_ev(EventKind.STARTED, name))

    row = store.print_by_id(print_id)
    assert (row.source, row.job_id, row.plate, row.spool_id) == ("job", job_id, 1, None)
    assert row.deducted_g is None
    assert remaining(store, spool) == pytest.approx(1000.0)

    journal.on_event(job_ev(EventKind.FINISHED, name, LATER))  # no slot: the question is asked
    assert store.print_by_id(print_id).spool_id is None
    assert remaining(store, spool) == pytest.approx(1000.0)


def test_end_unknown_closes_without_duration_or_cost(store):
    journal = Journal(store, Settings())
    print_id = journal.on_event(ev(EventKind.STARTED, T0))
    journal.on_event(ev(EventKind.FINISHED, T0 + timedelta(days=2), end_unknown=True))
    row = store.print_by_id(print_id)
    assert row.outcome == "finished" and row.ended_at is not None
    assert row.actual_min is None and row.kwh is None and row.cost_power_uah is None
