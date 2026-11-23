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
    assert r"0C00\_0300\_0001\_0001" in text  # escaped for Markdown


def test_render_hms_known_text():
    h = Hms(attr=0x0C000300, code=0x00030001)
    texts = HmsTexts(hms={"0C00_0300_0003_0001": "Nozzle clogged"})
    text = render_event(event(EventKind.HMS_WARNING, hms=h), texts, None)
    assert "Nozzle clogged" in text and r"0C00\_0300\_0003\_0001" in text


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


def test_render_uses_given_timezone():
    from zoneinfo import ZoneInfo

    assert "14:30" in render_event(event(EventKind.STARTED), HmsTexts(), None)
    assert "12:30" in render_event(event(EventKind.STARTED), HmsTexts(), None, tz=ZoneInfo("UTC"))
    assert "21:30" in render_event(
        event(EventKind.STARTED), HmsTexts(), None, tz=ZoneInfo("Asia/Tokyo")
    )


def test_md_literal_escapes_markdown_and_html():
    from bambu_mcp.messages import md_literal

    assert md_literal("a_b*c`d[e]f~g|h=i$j\\k") == r"a\_b\*c\`d\[e\]f\~g\|h\=i\$j\\k"
    assert md_literal("<b>&") == "&lt;b&gt;&amp;"
    assert md_literal("plain кубик 1.0") == "plain кубик 1.0"


def test_job_name_and_hms_text_are_escaped():
    st = state()
    st = replace(st, subtask_name="my_part*v2")
    text = render_event(event(EventKind.STARTED, st), HmsTexts(), None)
    assert r"my\_part\*v2" in text and "my_part" not in text

    h = Hms(attr=0x0C000300, code=0x00030001)
    texts = HmsTexts(hms={"0C00_0300_0003_0001": "Check *nozzle* [A]"})
    text = render_event(event(EventKind.HMS_WARNING, st, hms=h), texts, None)
    assert r"Check \*nozzle\* \[A\]" in text
    assert r"0C00\_0300\_0003\_0001" in text


def test_questions_escape_the_name():
    from bambu_mcp.messages import fit_question, spool_question

    assert fit_question("a_b") == r"Как села деталь «a\_b»?"
    assert fit_question(None) == "Как села деталь «без названия»?"
    assert spool_question("a_b").startswith(r"С какой катушки печаталась «a\_b»?")


def test_render_end_unknown_and_lost():
    finished = Event(EventKind.FINISHED, ("Benchy", "1", "t"), AT, state(), end_unknown=True)
    text = render_event(finished, HmsTexts(), None, duration_min=30)
    assert text.startswith("✅ Печать завершена")
    assert "Точное время окончания неизвестно" in text
    assert "Длительность" not in text

    lost = Event(EventKind.FAILED, ("Benchy", "1", "t"), AT, state(), end_unknown=True, lost=True)
    text = render_event(lost, HmsTexts(), None)
    assert text.splitlines()[0] == "❌ Печать прервана, пока сервис не видел"
    assert "Benchy" in text
