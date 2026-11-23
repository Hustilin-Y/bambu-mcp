from dataclasses import replace
from datetime import UTC, date, datetime

from bambu_mcp.cost import CostLine
from bambu_mcp.events import Event, EventKind
from bambu_mcp.messages import post_print_buttons, render_event, spool_buttons
from bambu_mcp.printer.hms import HmsTexts
from bambu_mcp.printer.state import Hms, PrinterState
from bambu_mcp.store.models import Spool, Tariff

AT = datetime(2026, 11, 22, 12, 30, tzinfo=UTC)  # 14:30 Kyiv
TARIFF = Tariff(valid_from=date(2024, 6, 1), day_uah=4.32, night_uah=None)


def state(**kw) -> PrinterState:
    return replace(PrinterState.empty(), subtask_name="Benchy", gcode_state="FINISH", **kw)


def event(kind, st=None, hms=None) -> Event:
    return Event(kind, ("Benchy", "1", "t"), AT, st or state(), hms)


def test_render_finished_ru():
    cost = CostLine(filament_uah=8.52, power_uah=1.94, kwh=0.449, tariff=TARIFF)
    text = render_event(event(EventKind.FINISHED), HmsTexts(), cost)
    assert "✅ Печать завершена" in text
    assert "Benchy" in text
    assert "14:30" in text
    assert "Пластик 8.52 грн, электричество 1.94 грн, всего 10.46 грн" in text
    assert "тариф 4.32 грн/кВт·ч с 01.06.2024" in text


def test_render_finished_without_cost():
    text = render_event(event(EventKind.FINISHED), HmsTexts(), None)
    assert "✅ Печать завершена" in text
    assert "грн" not in text


def test_render_finished_without_filament_cost():
    cost = CostLine(filament_uah=None, power_uah=1.94, kwh=0.449, tariff=TARIFF)
    text = render_event(event(EventKind.FINISHED), HmsTexts(), cost)
    assert "Электричество 1.94 грн" in text
    assert "Пластик" not in text


def test_render_hms_unknown_text():
    h = Hms(attr=0x0C000300, code=0x00010001)
    text = render_event(event(EventKind.HMS_ERROR, hms=h), HmsTexts(), None)
    assert "0C00_0300_0001_0001" in text


def test_render_hms_known_text():
    h = Hms(attr=0x0C000300, code=0x00030001)
    texts = HmsTexts(hms={"0C00_0300_0003_0001": "Nozzle clogged"})
    text = render_event(event(EventKind.HMS_WARNING, hms=h), texts, None)
    assert "Nozzle clogged" in text and "0C00_0300_0003_0001" in text


def test_render_other_kinds_have_title():
    for kind in EventKind:
        if kind in (EventKind.HMS_ERROR, EventKind.HMS_WARNING):
            continue
        assert "Benchy" in render_event(event(kind), HmsTexts(), None)


def test_post_print_button_ids():
    rows = post_print_buttons(12)
    ids = [b.id for row in rows for b in row]
    assert ids == ["fit:12:ok", "fit:12:tight", "fit:12:loose", "fit:12:failed"]
    assert all(b.text for row in rows for b in row)


def _spool(i: int) -> Spool:
    return Spool(i, "Brand", "PLA", "Red", "#ff0000", None, 600.0, 1000.0, 800.0, True)


def test_spool_buttons_ids():
    rows = spool_buttons(12, [_spool(3), _spool(4), _spool(5)])
    ids = [b.id for row in rows for b in row]
    assert ids == ["spool:12:3", "spool:12:4", "spool:12:5", "spool:12:none"]
    assert all(len(row) <= 8 for row in rows)
    assert all(len(b.text) <= 64 for row in rows for b in row)


def test_render_duration_hours_padded():
    for kind in (EventKind.FINISHED, EventKind.FAILED, EventKind.CANCELLED):
        text = render_event(event(kind), HmsTexts(), None, duration_min=65)
        assert "Длительность: 1 ч 05 мин" in text


def test_render_duration_under_hour():
    text = render_event(event(EventKind.FINISHED), HmsTexts(), None, duration_min=28)
    assert "Длительность: 28 мин" in text


def test_render_duration_full_hours_and_omitted():
    assert "Длительность: 2 ч 00 мин" in render_event(
        event(EventKind.FINISHED), HmsTexts(), None, duration_min=120
    )
    assert "Длительность" not in render_event(event(EventKind.FINISHED), HmsTexts(), None)
    assert "Длительность" not in render_event(
        event(EventKind.STARTED), HmsTexts(), None, duration_min=65
    )
