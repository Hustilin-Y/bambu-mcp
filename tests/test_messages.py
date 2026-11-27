from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from bambu_mcp.config import Settings
from bambu_mcp.cost import full_cost
from bambu_mcp.events import Event, EventKind
from bambu_mcp.messages import (
    _setting_parts,
    post_print_buttons,
    render_cost,
    render_event,
    spool_buttons,
    tariff_reminder,
)
from bambu_mcp.printer.hms import HmsTexts
from bambu_mcp.printer.state import Hms, PrinterState
from bambu_mcp.store.models import Amortization, Spool, Tariff

AT = datetime(2026, 11, 22, 12, 30, tzinfo=UTC)  # 14:30 Kyiv
TARIFF = Tariff(valid_from=date(2024, 6, 1), day_uah=4.32, night_uah=None)


def state(**kw) -> PrinterState:
    return replace(PrinterState.empty(), subtask_name="Benchy", gcode_state="FINISH", **kw)


def event(kind, st=None, hms=None) -> Event:
    return Event(kind, ("Benchy", "1", "t"), AT, st or state(), hms)


def test_render_finished_ru():
    cost = full_cost(8, SPOOL, 52, 0.4444, TARIFF, AMORT, currency="UAH")
    text = render_event(event(EventKind.FINISHED), HmsTexts(), cost, symbol="грн")
    assert "✅ Печать завершена" in text
    assert "Benchy" in text
    assert "14:30" in text
    assert text.split("\n")[-4:] == SAMPLE_LINES


def test_render_finished_without_cost():
    text = render_event(event(EventKind.FINISHED), HmsTexts(), None)
    assert "✅ Печать завершена" in text
    assert "грн" not in text and "итого" not in text


def test_render_finished_without_filament_cost():
    cost = full_cost(8, None, 52, 0.4444, TARIFF, None, currency="UAH")
    text = render_event(event(EventKind.FINISHED), HmsTexts(), cost, symbol="грн")
    assert "свет 1.92 грн (0.44 кВт·ч × 4.32)" in text
    assert "пластик: цена неизвестна" in text


# -- cost lines (evaluation Task 2) ------------------------------------------------

SPOOL = Spool(
    id=3, brand="Bambu", material="PLA", color_name="Black", color_hex="#000000", filament_preset=None,
    price_uah=700.0, net_g=1000.0, remaining_g=1000.0, active=True,
)
AMORT = Amortization(date(2024, 6, 1), 30000.0, 5000.0, 2.0, "UAH")
TWO_ZONE = Tariff(valid_from=date(2024, 6, 1), day_uah=4.32, night_uah=2.16)
SAMPLE_LINES = [
    "пластик 5.60 грн (8 г × 700 грн/кг)",
    "свет 1.92 грн (0.44 кВт·ч × 4.32)",
    "амортизация 6.93 грн (52 мин × 8.00 грн/ч)",
    "итого 14.45 грн",
]


def test_render_cost_exact_lines():
    cost = full_cost(8, SPOOL, 52, 0.4444, TARIFF, AMORT, currency="UAH")
    assert render_cost(cost, "грн") == SAMPLE_LINES


def test_render_cost_without_amortization_excludes_it():
    cost = full_cost(8, SPOOL, 52, 0.4444, TARIFF, None, currency="UAH")
    assert render_cost(cost, "грн") == [*SAMPLE_LINES[:2], "амортизация: не задана", "итого 7.52 грн"]


def test_render_cost_before_slicing_has_no_time():
    cost = full_cost(8, SPOOL, None, None, TARIFF, AMORT, currency="UAH")
    assert render_cost(cost, "грн") == [SAMPLE_LINES[0], "время неизвестно до нарезки", "итого 5.60 грн"]


def test_render_cost_two_zone_says_if_started_now():
    start = datetime(2026, 6, 1, 19, 0, tzinfo=UTC)  # 22:00 Kyiv, the night starts at 23:00
    cost = full_cost(8, SPOOL, 120, 0.45, TWO_ZONE, None, currency="UAH", start=start, settings=Settings())
    power = render_cost(cost, "грн", start_now=True)[1]
    assert power.startswith(f"свет {cost.power.amount:.2f} грн (0.45 кВт·ч × ")
    assert power.endswith(" в среднем, если начать сейчас)")
    assert "если начать сейчас" not in render_cost(cost, "грн")[1]
    single = full_cost(8, SPOOL, 52, 0.4444, TARIFF, None, currency="UAH")
    assert "если начать сейчас" not in render_cost(single, "грн", start_now=True)[1]


def test_render_cost_escapes_the_symbol():
    cost = full_cost(8, SPOOL, 52, 0.4444, TARIFF, AMORT, currency="UAH")
    lines = render_cost(cost, "<$*>")
    assert lines[-1] == "итого 14.45 &lt;\\$\\*&gt;"
    assert all("<" not in line and "*>" not in line for line in lines)


def test_render_cost_refused_names_the_input_in_russian():
    eur = Tariff(valid_from=date(2024, 6, 1), day_uah=0.25, night_uah=None, currency="EUR")
    cases = {
        "катушка 3 в UAH": full_cost(8, SPOOL, 52, 0.44, eur, None, currency="EUR"),
        "тариф с 01.06.2024 в UAH": full_cost(8, None, 52, 0.44, TARIFF, None, currency="EUR"),
        "амортизация с 01.06.2024 в UAH": full_cost(8, None, 52, 0.44, eur, AMORT, currency="EUR"),
    }
    for words, cost in cases.items():
        (line,) = render_cost(cost, "€")
        assert line.startswith("Стоимость не посчитана: ") and words in line and "EUR" in line
        assert "—" not in line and ";" not in line


def test_tariff_reminder_uses_the_symbol():
    assert tariff_reminder(TARIFF, "€") == "Проверь тариф на электроэнергию: сейчас 4.32 €/кВт·ч с 01.06.2024"
    assert tariff_reminder(TWO_ZONE, "грн") == (
        "Проверь тариф на электроэнергию: сейчас день 4.32 / ночь 2.16 грн/кВт·ч с 01.06.2024"
    )


def test_settings_summary_shows_the_new_whitelist_keys():
    parts = _setting_parts({
        "wall_generator": "classic", "top_shell_layers": 6, "bottom_shell_layers": 5,
        "outer_wall_speed": 60, "seam_position": "back", "ironing_type": "top",
    })
    assert parts == ["стенки classic", "верх/низ 6/5", "внешняя стенка 60 мм/с", "шов сзади", "разглаживание"]
    assert _setting_parts({"top_shell_layers": 7}) == ["верх 7 слоёв"]
    assert _setting_parts({"bottom_shell_layers": 3, "ironing_type": "no ironing"}) == [
        "низ 3 слоя", "без разглаживания"
    ]


def test_no_hardcoded_grn_left():
    """The currency symbol comes from the store: only its stored default spells it."""
    src = Path(__file__).resolve().parents[1] / "src"
    allowed = {src / "bambu_mcp" / "store" / "db.py"}
    found = [
        f"{path.relative_to(src)}:{n}"
        for path in sorted(src.rglob("*.py"))
        if path not in allowed
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "грн" in line
    ]
    assert found == []


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
