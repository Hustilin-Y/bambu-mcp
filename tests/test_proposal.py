import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from bambu_mcp.config import Settings
from bambu_mcp.cost import full_cost
from bambu_mcp.manifest import parse_manifest
from bambu_mcp.messages import render_cost, render_proposal, render_sliced
from bambu_mcp.printer.state import Tray
from bambu_mcp.proposal import (
    NAMED_SETS,
    SlotChoice,
    job_hints,
    job_material,
    last_settings,
    pick_slot,
    predict_cost,
    resolve_settings,
    slot_number,
)
from bambu_mcp.store.models import Amortization, Job, Spool, Tariff
from slice_agent.runner import Overrides

FIXTURES = Path(__file__).parent / "fixtures" / "manifest"
TARIFF = Tariff(valid_from=date(2024, 6, 1), day_uah=4.32, night_uah=None)
NOW = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


def _raw(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.print.json").read_text(encoding="utf-8"))


def _job(name: str = "part", raw: dict | None = None, status: str = "new", job_id: int = 7,
         settings: dict | None = None) -> Job:
    raw = raw if raw is not None else _raw(name)
    return Job(
        id=job_id, name=raw["name"], kind=raw["kind"], manifest_json=json.dumps(raw),
        file_sha256=raw["file_sha256"], created_at=NOW, status=status,
        settings_json=json.dumps(settings) if settings is not None else None,
        result_json=None, sliced_file=None, error=None,
    )


def _spool(id=1, material="PETG", color="чёрный", remaining=310.0, price=550.0, hex="#000000") -> Spool:
    return Spool(
        id=id, brand="Elegoo", material=material, color_name=color, color_hex=hex,
        filament_preset=None, price_uah=price, net_g=1000.0, remaining_g=remaining, active=True,
    )


def _tray(ams, slot, tray_type, color="000000FF") -> Tray:
    return Tray(ams=ams, slot=slot, tray_type=tray_type, color_rgba=color, remain_pct=None)


def _unescape(text: str) -> str:
    return text.replace("\\~", "~")


# -- resolve_settings ---------------------------------------------------------------


EXPLICIT = {"wall_loops": 6}
LAST = {"wall_loops": 5, "sparse_infill_density": 40, "brim_type": "outer_only"}
HINTS = {"walls": 4, "infill_pct": 25, "infill_pattern": "gyroid", "brim": "auto", "supports": "tree"}


@pytest.mark.parametrize(
    ("explicit", "last", "hints", "named", "checks", "expected"),
    [
        # explicit > last > hints > named set
        (EXPLICIT, LAST, HINTS, "strong", {}, {
            "wall_loops": 6, "sparse_infill_density": 40, "brim_type": "outer_only",
            "sparse_infill_pattern": "gyroid", "enable_support": True, "support_type": "tree(auto)",
        }),
        ({}, LAST, HINTS, "strong", {}, {
            "wall_loops": 5, "sparse_infill_density": 40, "brim_type": "outer_only",
            "sparse_infill_pattern": "gyroid", "enable_support": True, "support_type": "tree(auto)",
        }),
        ({}, None, HINTS, "strong", {}, {
            "wall_loops": 4, "sparse_infill_density": 25, "brim_type": "auto_brim",
            "sparse_infill_pattern": "gyroid", "enable_support": True, "support_type": "tree(auto)",
        }),
        ({}, None, {}, "strong", {}, {
            "wall_loops": 4, "sparse_infill_density": 25, "enable_support": False,
        }),
        ({}, None, {}, "draft", {}, {
            "sparse_infill_density": 10, "layer_height": 0.28, "enable_support": False,
        }),
        ({}, None, {"walls": 3}, "draft", {}, {
            "wall_loops": 3, "sparse_infill_density": 10, "layer_height": 0.28, "enable_support": False,
        }),
        ({"curr_bed_type": "Cool Plate", "layer_height": 0.2}, {"enable_support": True}, {"supports": "none"},
         "standard", {}, {"curr_bed_type": "Cool Plate", "layer_height": 0.2, "enable_support": True}),
    ],
)
def test_precedence_explicit_over_last_over_hints(explicit, last, hints, named, checks, expected):
    got = resolve_settings(explicit, last, hints, named, checks=checks)
    assert got == expected
    # Every result goes to the slice-agent unchanged: it must pass its whitelist as is.
    assert Overrides.parse(got, "0.4").values == got


def test_named_sets_pass_the_whitelist():
    for name, values in NAMED_SETS.items():
        assert Overrides.parse(dict(values), "0.4").values == values, name


def test_hint_mapping_passes_the_whitelist():
    for supports in ("none", "auto", "tree", "normal"):
        for brim in ("none", "auto", "outer"):
            for suggested in (False, True):
                got = resolve_settings(
                    {}, None, {"supports": supports, "brim": brim}, "standard",
                    checks={"supports_suggested": suggested},
                )
                assert Overrides.parse(got, "0.4").values == got


@pytest.mark.parametrize(
    ("hints", "suggested", "expected"),
    [
        ({}, False, {"enable_support": False}),
        ({}, True, {"enable_support": True}),
        ({"supports": "auto"}, False, {"enable_support": False}),
        ({"supports": "auto"}, True, {"enable_support": True}),
        ({"supports": "none"}, True, {"enable_support": False}),
        ({"supports": "tree"}, False, {"enable_support": True, "support_type": "tree(auto)"}),
        ({"supports": "normal"}, False, {"enable_support": True, "support_type": "normal(auto)"}),
    ],
)
def test_supports_only_when_suggested(hints, suggested, expected):
    got = resolve_settings({}, None, hints, "standard", checks={"supports_suggested": suggested})
    assert got == expected


def test_supports_default_off_without_checks():
    assert resolve_settings({}, None, {}, "standard") == {"enable_support": False}


def test_brim_mapping():
    for hint, value in (("none", "no_brim"), ("auto", "auto_brim"), ("outer", "outer_only")):
        got = resolve_settings({}, None, {"brim": hint}, "standard")
        assert got["brim_type"] == value


def test_explicit_support_type_enables_supports():
    got = resolve_settings({"support_type": "normal(auto)"}, None, {"supports": "none"}, "standard")
    assert got["enable_support"] is True and got["support_type"] == "normal(auto)"


def test_explicit_invalid_raises():
    with pytest.raises(ValueError, match="wall_loops"):
        resolve_settings({"wall_loops": 11}, None, {}, "standard")
    with pytest.raises(ValueError, match="unknown override"):
        resolve_settings({"bed_temp": 60}, None, {}, "standard")
    with pytest.raises(ValueError, match="layer_height"):
        resolve_settings({"layer_height": 0.3}, None, {}, "standard")


def test_unknown_named_set_raises():
    with pytest.raises(ValueError, match="named set"):
        resolve_settings({}, None, {}, "turbo")


def test_last_job_invalid_entries_dropped():
    last = {"wall_loops": 99, "bed_temp": 60, "sparse_infill_density": 30}
    assert resolve_settings({}, last, {}, "standard") == {"sparse_infill_density": 30, "enable_support": False}


def test_material_hints_are_not_overrides():
    got = resolve_settings({}, None, {"material": "PETG", "material_reason": "heat"}, "standard")
    assert got == {"enable_support": False}


def test_last_settings_takes_newest_sliced_job_of_the_same_name():
    jobs = [  # newest first, as Store.jobs returns them
        _job(job_id=9, status="new", settings={"wall_loops": 9}),
        _job(job_id=8, status="slice_failed", settings={"wall_loops": 8}),
        _job("kit", job_id=7, status="sliced", settings={"wall_loops": 7}),
        _job(job_id=6, status="sliced", settings={"wall_loops": 6}),
        _job(job_id=5, status="sliced", settings={"wall_loops": 5}),
    ]
    assert last_settings(jobs, "lamp-arm") == {"wall_loops": 6}
    assert last_settings(jobs, "lamp-arm", exclude_id=6) == {"wall_loops": 5}
    assert last_settings(jobs, "other") is None


def test_last_settings_ignores_unreadable_settings():
    bad = replace(_job(job_id=3, status="sliced"), settings_json="[1, 2")
    assert last_settings([bad], "lamp-arm") is None


# -- material and kit hints ---------------------------------------------------------


def test_job_material_from_hint_with_reason():
    m = parse_manifest(_raw("part"))
    assert job_material(m) == ("PETG", "near a lamp")


def test_job_material_falls_back_to_profile():
    raw = _raw("part")
    raw["hints"] = {"walls": 4}
    assert job_material(parse_manifest(raw)) == ("PETG", None)
    raw["profile"]["material"] = "PLA"
    assert job_material(parse_manifest(raw)) == ("PLA", None)


def test_kit_hints_keep_only_what_all_plates_agree_on():
    raw = _raw("kit")
    assert job_hints(parse_manifest(raw)) == {}  # plate 1 says walls 3, plate 2 has a conflict
    raw["plates"][1]["hints"] = {"walls": 3, "brim": "auto"}
    assert job_hints(parse_manifest(raw)) == {"walls": 3}


def test_part_hints_are_the_manifest_hints():
    m = parse_manifest(_raw("part"))
    assert job_hints(m) == m.hints


# -- pick_slot ----------------------------------------------------------------------


def test_pick_slot_material_match_prefers_last_color():
    trays = (
        _tray(0, 0, "PLA", "FFFFFFFF"),
        _tray(0, 1, "PETG", "FFFFFFFF"),
        _tray(0, 2, "PETG", "000000FF"),
        _tray(0, 3, None, None),
    )
    spools = {(0, 1): _spool(1, color="белый", remaining=500, hex="#FFFFFF"), (0, 2): _spool(2, color="чёрный")}
    choice = pick_slot(trays, spools, "petg", "чёрный")
    assert (choice.ams, choice.slot, choice.spool_id, choice.warning) == (0, 2, 2, None)
    assert (choice.tray_type, choice.color_name, choice.remaining_g) == ("PETG", "чёрный", 310.0)
    # The colour also matches by hex, from the tray or from the spool.
    assert pick_slot(trays, spools, "PETG", "#000000").slot == 2
    assert pick_slot(trays, {}, "PETG", "000000").slot == 2
    # Without a colour preference: the first matching slot.
    first = pick_slot(trays, spools, "PETG", None)
    assert (first.ams, first.slot, first.spool_id) == (0, 1, 1)
    # A colour nobody has: still a material match, no warning.
    other = pick_slot(trays, spools, "PETG", "красный")
    assert (other.slot, other.warning) == (1, None)


def test_pick_slot_unknown_spool_has_no_remaining():
    choice = pick_slot((_tray(1, 0, "PLA"),), {}, "PLA", None)
    assert (choice.ams, choice.slot, choice.spool_id, choice.remaining_g) == (1, 0, None, None)


def test_pick_slot_mismatch_warns():
    trays = (_tray(0, 0, None, None), _tray(0, 1, "PETG"), _tray(0, 2, "PETG"))
    choice = pick_slot(trays, {(0, 1): _spool()}, "PLA", None)
    assert (choice.ams, choice.slot, choice.spool_id) == (0, 1, 1)
    assert choice.warning is not None
    assert "PLA" in choice.warning and "PETG" in choice.warning
    assert "слот 2" in choice.warning
    assert "—" not in choice.warning and ";" not in choice.warning


def test_pick_slot_nothing_loaded():
    choice = pick_slot((_tray(0, 0, None, None),), {}, "PLA", None)
    assert (choice.ams, choice.slot, choice.spool_id) == (None, None, None)
    assert "PLA" in choice.warning


def test_pick_slot_escapes_printer_and_manifest_text():
    choice = pick_slot((_tray(0, 0, "PLA_CF"),), {}, "PETG*", None)
    assert "PLA\\_CF" in choice.warning and "PETG\\*" in choice.warning


# -- texts --------------------------------------------------------------------------

SAMPLE = "🖨 lamp-arm, пластина 1: слот 3 PETG чёрный (~310 г), 4 стенки, 25% gyroid, без поддержек, Textured PEI. ~1 ч 40 мин, 14 г"
SETTINGS = {"wall_loops": 4, "sparse_infill_density": 25, "sparse_infill_pattern": "gyroid", "enable_support": False}
CHOICE = SlotChoice(0, 2, 1, None, tray_type="PETG", color_name="чёрный", remaining_g=310.0)


def _cost(grams, minutes, kwh, spool=True, amort=None):
    return full_cost(grams, _spool() if spool else None, minutes, kwh, TARIFF, amort, currency="UAH")


def test_render_sliced_ru_format():
    cost = _cost(14.0, 100.2, 0.39)
    plates = [{"index": 1, "minutes": 100.2, "grams": {"PETG": 14.04}}]
    text = render_sliced(
        _job("part"), plates, [cost], settings=SETTINGS, slot_choice=CHOICE, plate_type="Textured PEI Plate"
    )
    lines = _unescape(text).split("\n")
    assert lines[0] == SAMPLE
    assert lines[1:] == [
        "🧵 пластик 7.70 грн (14 г × 550 грн/кг)",
        "⚡ свет 1.68 грн (0.39 кВт·ч × 4.32)",
        "🛠 амортизация: не задана",
        "💰 итого 9.38 грн",
    ]
    # Tildes are escaped: the add-in reads the text as Markdown (a ~ pair is strikethrough).
    assert text.split("\n")[0] == SAMPLE.replace("~", "\\~")
    assert "—" not in text and ";" not in text


def test_render_sliced_unknown_filament_price():
    cost = _cost(14.0, 40, 0.39, spool=False)
    plates = [{"index": 1, "minutes": 40, "grams": {"PETG": 14.0}}]
    choice = SlotChoice(0, 0, None, None, tray_type="PETG")
    text = _unescape(render_sliced(_job(), plates, [cost], settings=SETTINGS, slot_choice=choice,
                                   plate_type="Cool Plate"))
    assert "слот 1 PETG (~? г)" in text
    assert "Cool Plate." not in text and ", Cool. ~40 мин, 14 г\n" in text
    assert "🧵 пластик: цена неизвестна" in text and "💰 итого 1.68 грн" in text


def test_render_sliced_kit_lists_plates_and_total():
    raw = _raw("kit")
    amort = Amortization(date(2024, 6, 1), 30000.0, 5000.0, 2.0, "UAH")
    costs = [_cost(3.0, 20, 0.1, amort=amort), _cost(18.4, 90, 0.3, amort=amort)]
    plates = [
        {"index": 1, "minutes": 20, "grams": {"PLA": 3.0}},
        {"index": 2, "minutes": 90, "grams": {"PLA": 18.4}},
    ]
    choice = SlotChoice(0, 0, 1, None, tray_type="PLA", color_name="белый", remaining_g=800.0)
    text = _unescape(render_sliced(_job("kit", raw), plates, costs, settings={"enable_support": True},
                                   slot_choice=choice, plate_type="Textured PEI Plate"))
    lines = text.split("\n")
    assert lines[0] == "🖨 desk-kit, 2 пластины: слот 1 PLA белый (~800 г), с поддержками, Textured PEI."
    assert lines[1] == f"Пластина 1: ~20 мин, 3 г, ~{costs[0].total.amount:.2f} грн"
    assert lines[2] == f"Пластина 2: ~1 ч 30 мин, 18 г, ~{costs[1].total.amount:.2f} грн"
    assert lines[3] == "Всего: ~1 ч 50 мин, 21 г"
    assert lines[4:] == [
        "🧵 пластик 11.77 грн (21 г × 550 грн/кг)",
        "⚡ свет 1.73 грн (0.40 кВт·ч × 4.32)",
        "🛠 амортизация 14.67 грн (110 мин × 8.00 грн/ч)",
        "💰 итого 28.16 грн",
    ]


def test_render_sliced_shows_a_currency_refusal():
    eur_tariff = replace(TARIFF, currency="EUR")
    off = full_cost(14.0, _spool(), 40, 0.39, eur_tariff, None, currency="EUR")
    text = render_sliced(_job(), [{"index": 1, "minutes": 40, "grams": {"PETG": 14.0}}], [off],
                         settings=SETTINGS, slot_choice=CHOICE, plate_type="Cool Plate", symbol="€")
    assert text.split("\n")[1] == render_cost(off, "€")[0]
    assert text.split("\n")[1].startswith("Стоимость не посчитана: катушка 1 в UAH")


def test_render_proposal_shows_the_cost_lines():
    cost = _cost(14.0, None, None)
    text = _unescape(render_proposal(_job("part"), SETTINGS, CHOICE, "Textured PEI Plate", None, cost))
    lines = text.split("\n")
    assert lines[0].endswith("Textured PEI. ~14 г")
    assert lines[1:4] == ["🧵 пластик 7.70 грн (14 г × 550 грн/кг)", "⏱ время неизвестно до нарезки", "💰 итого 7.70 грн"]


def test_render_proposal_part():
    choice = SlotChoice(0, 2, 1, None, tray_type="PETG", color_name="чёрный", remaining_g=310.0)
    text = _unescape(render_proposal(_job("part"), SETTINGS, choice, "Textured PEI Plate", None))
    lines = text.split("\n")
    assert lines[0] == (
        "🖨 lamp-arm, пластина 1: слот 3 PETG чёрный (~310 г), 4 стенки, 25% gyroid, без поддержек, "
        "Textured PEI. ~14 г"
    )
    assert "Почему PETG: near a lamp" in text
    assert "—" not in text and ";" not in text


def test_render_proposal_settings_words():
    settings = {
        "wall_loops": 1, "sparse_infill_density": 15, "enable_support": True, "support_type": "tree(auto)",
        "brim_type": "outer_only", "layer_height": 0.28, "wall_generator": "classic",
        "curr_bed_type": "Cool Plate",
    }
    text = _unescape(render_proposal(_job(), settings, CHOICE, "Textured PEI Plate", {1: 14.2}))
    assert "1 стенка, 15% заполнение, поддержки tree, кайма снаружи, слой 0.28 мм, стенки classic, Cool." in text
    text = _unescape(render_proposal(_job(), {"wall_loops": 5}, CHOICE, "Textured PEI Plate", {1: 14.2}))
    assert "5 стенок, " in text


def test_render_proposal_shows_slot_warning_and_low_spool():
    choice = SlotChoice(0, 1, 1, "⚠️ warning text", tray_type="PETG", color_name="чёрный", remaining_g=10.0)
    text = _unescape(render_proposal(_job(), SETTINGS, choice, "Textured PEI Plate", {1: 14.2}))
    assert "⚠️ warning text" in text
    assert "⚠️ На катушке ~10 г, а пластине 1 нужно ~14 г" in text


def test_render_proposal_no_slot():
    choice = SlotChoice(None, None, None, "⚠️ none loaded")
    text = _unescape(render_proposal(_job(), SETTINGS, choice, "Textured PEI Plate", None))
    assert "пластина 1: слот не выбран, 4 стенки" in text
    assert "⚠️ none loaded" in text


def test_render_proposal_kit_lists_conflicts_and_plate_hints():
    raw = _raw("kit")
    raw["plates"][1]["hint_conflicts"] = ["walls", "infill_pct"]
    choice = SlotChoice(0, 0, 1, None, tray_type="PLA", color_name="белый", remaining_g=20.0)
    settings = {"enable_support": True}
    text = _unescape(render_proposal(_job("kit", raw), settings, choice, "Textured PEI Plate", None))
    lines = text.split("\n")
    assert lines[0] == "🖨 desk-kit, 2 пластины: слот 1 PLA белый (~20 г), с поддержками, Textured PEI."
    assert "Пластина 1: ~3 г" in lines
    assert "Пластина 2: ~18 г" in lines
    assert "Пластина 1: в CAD 3 стенки, в настройках по профилю" in text
    assert "Пластина 2: у деталей разные подсказки (стенки, заполнение)" in text
    # each plate fits the spool, the kit as a whole does not
    assert "пластине" not in text
    assert "⚠️ На катушке ~20 г, а набору нужно ~21 г" in text


def test_render_proposal_escapes_names_and_reason():
    raw = _raw("part")
    raw["name"] = "arm_v2"
    raw["hints"]["material_reason"] = "*hot* [lamp]"
    text = render_proposal(_job("part", raw), SETTINGS, CHOICE, "Textured PEI Plate", None)
    assert text.startswith("🖨 arm\\_v2, ")
    assert "\\*hot\\* \\[lamp\\]" in text


# -- cost ---------------------------------------------------------------------------


def test_predict_cost_uses_spool_price_tariff_and_amortization():
    amort = Amortization(date(2024, 6, 1), 30000.0, 5000.0, 2.0, "UAH")
    cost = predict_cost(14.0, 120, _spool(price=550.0), "PETG", TARIFF, amort, Settings(), NOW, currency="UAH")
    assert round(cost.plastic.amount, 2) == 7.70
    assert round(cost.kwh, 3) == 0.45  # 120 min at the PLA fallback 200 W + 0.05 heat-up
    assert round(cost.power.amount, 3) == round(0.45 * 4.32, 3)
    assert cost.amort.amount == pytest.approx(16.0)


def test_predict_cost_without_minutes_has_no_power():
    cost = predict_cost(14.0, None, None, "PETG", TARIFF, None, Settings(), NOW, currency="UAH")
    assert cost.plastic is None and cost.power is None and cost.total.amount == 0


# -- fix round 1 --------------------------------------------------------------------


def _kit_render(raw: dict, settings: dict) -> str:
    choice = SlotChoice(0, 0, 1, None, tray_type="PLA", color_name="белый", remaining_g=800.0)
    return _unescape(render_proposal(_job("kit", raw), settings, choice, "Textured PEI Plate", None))


def test_kit_note_names_a_plate_supports_wish_the_kit_drops():
    raw = _raw("kit")
    raw["checks"]["supports_suggested"] = False
    for plate in raw["plates"]:
        plate["checks"]["supports_suggested"] = False
    raw["plates"][0]["hints"] = {"walls": 3, "supports": "tree"}
    m = parse_manifest(raw)
    settings = resolve_settings({}, None, job_hints(m), "standard", checks=m.checks)
    assert settings == {"enable_support": False}
    text = _kit_render(raw, settings)
    assert "Пластина 1: в CAD поддержки tree, в настройках без поддержек" in text
    assert "Пластина 2: в CAD" not in text  # no supports hint, its checks agree with the kit


def test_kit_note_names_a_plate_that_gets_supports_against_its_none():
    raw = _raw("kit")
    raw["plates"][0]["hints"] = {"walls": 3, "supports": "none"}  # plate 2's checks suggest supports
    m = parse_manifest(raw)
    settings = resolve_settings({}, None, job_hints(m), "standard", checks=m.checks)
    assert settings["enable_support"] is True
    text = _kit_render(raw, settings)
    assert "Пластина 1: в CAD без поддержек, в настройках с поддержками" in text


def test_kit_note_names_a_plate_whose_checks_want_supports_the_kit_turns_off():
    raw = _raw("kit")
    text = _kit_render(raw, {"enable_support": False})
    assert "Пластина 2: в CAD с поддержками, в настройках без поддержек" in text
    assert "Пластина 1: в CAD без" not in text


def test_kit_note_names_a_different_support_type():
    raw = _raw("kit")
    raw["plates"][1]["hints"] = {"supports": "normal"}
    text = _kit_render(raw, {"enable_support": True, "support_type": "tree(auto)"})
    assert "Пластина 2: в CAD поддержки normal, в настройках поддержки tree" in text


def test_kit_root_hint_does_not_override_a_plate_own_hint():
    raw = _raw("kit")
    raw["hints"] = {"walls": 4, "brim": "auto"}
    raw["plates"][0]["hints"] = {"walls": 2}  # its own walls, no brim
    del raw["plates"][1]["hints"]  # inherits the root hints
    m = parse_manifest(raw)
    assert job_hints(m) == {"brim": "auto"}
    settings = resolve_settings({}, None, job_hints(m), "standard", checks=m.checks)
    text = _kit_render(raw, settings)
    assert "Пластина 1: в CAD 2 стенки, в настройках по профилю" in text
    assert "Пластина 2: в CAD 4 стенки, в настройках по профилю" in text


def test_pick_slot_skips_non_string_tray_types():
    trays = (_tray(0, 0, 123), _tray(0, 1, "PLA"))
    choice = pick_slot(trays, {}, "PLA", None)
    assert (choice.slot, choice.warning) == (1, None)
    nothing = pick_slot((_tray(0, 0, 5),), {}, "PLA", None)
    assert nothing.slot is None


def test_named_set_value_refused_for_nozzle_names_set_and_key():
    with pytest.raises(ValueError, match=r"named set 'draft'.*layer_height"):
        resolve_settings({}, None, {}, "draft", nozzle="0.6")
    # A set without a nozzle-specific value works on any nozzle.
    assert resolve_settings({}, None, {}, "strong", nozzle="0.6") == {
        "wall_loops": 4, "sparse_infill_density": 25, "enable_support": False,
    }


def test_render_sliced_shows_the_slot_warning():
    warning = "⚠️ Нужен PETG, а в AMS его нет: выбран слот 1 PLA. Заправь PETG или подтверди печать из PLA"
    choice = SlotChoice(0, 0, 1, warning, tray_type="PLA", color_name="белый", remaining_g=800.0)
    cost = _cost(14.0, 100.2, 0.39)
    part = render_sliced(_job("part"), [{"index": 1, "minutes": 100.2, "grams": {"PLA": 14.0}}], [cost],
                         settings=SETTINGS, slot_choice=choice, plate_type="Textured PEI Plate")
    assert part.split("\n")[-1] == warning and part.count("\n") == 5
    kit = render_sliced(_job("kit", _raw("kit")),
                        [{"index": 1, "minutes": 20, "grams": {"PLA": 3.0}},
                         {"index": 2, "minutes": 90, "grams": {"PLA": 18.4}}],
                        [cost, cost], settings={}, slot_choice=choice, plate_type="Textured PEI Plate")
    assert kit.split("\n")[-1] == warning and kit.split("\n")[-2].startswith("💰 итого ")


# -- owner's slot choice (Task 12) ---------------------------------------------------


def _preset_spool(id, material, color, **changes) -> Spool:
    return replace(_spool(id, material=material, color=color), filament_preset=f"Generic {material}", **changes)


def test_slot_number_round_trip_over_16_slots():
    from bambu_mcp.proposal import slot_position

    assert [slot_number(*slot_position(n)) for n in range(1, 17)] == list(range(1, 17))
    assert [slot_position(n) for n in (1, 2, 4, 5, 16)] == [(0, 0), (0, 1), (0, 3), (1, 0), (3, 3)]
    for bad in (0, 17, -1, True):
        with pytest.raises(ValueError, match="1..16"):
            slot_position(bad)


def test_usable_slots_need_a_loaded_tray_and_an_active_spool_with_a_preset():
    from bambu_mcp.proposal import usable_slots

    trays = (
        _tray(0, 0, "PLA"), _tray(0, 1, "PETG"), _tray(0, 2, "PETG"), _tray(0, 3, None, None),
        _tray(1, 0, "PLA"), _tray(1, 1, "PLA"),
    )
    spools = {
        (0, 0): _preset_spool(1, "PLA", "белый"),
        (0, 1): _preset_spool(2, "PETG", "синий"),
        (0, 2): _spool(3),  # no preset
        (0, 3): _preset_spool(4, "PLA", "серый"),  # tray empty
        (1, 0): _preset_spool(5, "PLA", "красный", active=False),
    }
    usable = usable_slots(trays, spools)
    assert [(u.number, u.label) for u in usable] == [(1, "1 PLA белый"), (2, "2 PETG синий")]
    assert usable[1].spool.id == 2 and (usable[1].ams, usable[1].slot) == (0, 1)


def test_pick_slot_forced_slot_with_the_matching_material():
    trays = (_tray(0, 0, "PETG"), _tray(0, 1, "PETG"))
    spools = {(0, 0): _preset_spool(1, "PETG", "чёрный"), (0, 1): _preset_spool(2, "PETG", "синий", remaining_g=5.0)}
    choice = pick_slot(trays, spools, "PETG", "чёрный", forced=(0, 1))
    assert (choice.ams, choice.slot, choice.spool_id, choice.warning) == (0, 1, 2, None)
    assert (choice.tray_type, choice.color_name, choice.remaining_g) == ("PETG", "синий", 5.0)
    # Without a forced slot the material match is unchanged.
    assert pick_slot(trays, spools, "PETG", "чёрный").slot == 0


def test_pick_slot_forced_slot_keeps_the_material_mismatch_warning():
    trays = (_tray(0, 0, "PLA"), _tray(0, 1, "PETG"))
    spools = {(0, 0): _preset_spool(1, "PLA", "белый"), (0, 1): _preset_spool(2, "PETG", "синий", remaining_g=10.0)}
    choice = pick_slot(trays, spools, "PLA", None, forced=(0, 1))
    assert (choice.ams, choice.slot, choice.spool_id) == (0, 1, 2)
    assert choice.warning == "⚠️ Нужен PLA, а выбран слот 2 PETG: печать будет из PETG"
    assert "—" not in choice.warning and ";" not in choice.warning
    # The remainder warning comes from the forced slot's spool.
    text = render_proposal(_job(), SETTINGS, choice, "Textured PEI Plate", None)
    assert choice.warning in text and "На катушке" in text


def test_pick_slot_forced_slot_must_be_usable():
    from bambu_mcp.proposal import SlotUnusable

    trays = (_tray(0, 0, "PLA"), _tray(0, 1, "PETG"), _tray(0, 2, "PETG"), _tray(0, 3, None, None))
    spools = {(0, 1): _preset_spool(2, "PETG", "синий"), (0, 2): _spool(3)}
    for forced, number in (((0, 0), 1), ((0, 2), 3), ((0, 3), 4), ((1, 0), 5)):
        with pytest.raises(SlotUnusable) as info:
            pick_slot(trays, spools, "PLA", None, forced=forced)
        assert info.value.number == number
        assert str(info.value) == (
            f"slot {number} is empty or has no active spool with a filament preset, usable: 2 PETG синий"
        )
    with pytest.raises(SlotUnusable, match="usable: none"):
        pick_slot(trays, {}, "PLA", None, forced=(0, 1))


def test_slot_buttons_mark_the_chosen_slot():
    from bambu_mcp.messages import slot_buttons
    from bambu_mcp.proposal import UsableSlot

    usable = [
        UsableSlot(n, *divmod(n - 1, 4), _preset_spool(n, "PETG", "синий" if n != 3 else "x" * 80))
        for n in range(1, 18)
    ]
    rows = slot_buttons(7, usable, chosen=2)
    assert [len(r) for r in rows] == [4, 4, 4, 4]  # at most 16, four per row
    flat = [b for row in rows for b in row]
    assert flat[0].id == "job:7:slot:1" and flat[0].text == "1️⃣ PETG синий"
    assert flat[1].text == "✅ 2️⃣ PETG синий"
    assert len(flat[2].text) == 64 and flat[2].text.startswith("3️⃣ PETG x")
    assert flat[-1].id == "job:7:slot:16"
    assert slot_buttons(7, [], chosen=None) == []


def test_precedence_with_goal_and_variant():
    # explicit > variant > goal base point > last job > hints > named set (global constraints).
    keys = ("wall_loops", "sparse_infill_density", "sparse_infill_pattern", "layer_height", "brim_type", "seam_position")
    named = "strong"  # wall_loops 4, sparse_infill_density 25
    hints = {"walls": 1, "infill_pct": 1, "infill_pattern": "grid", "brim": "outer"}
    last = {"wall_loops": 2, "sparse_infill_density": 2, "sparse_infill_pattern": "honeycomb", "layer_height": 0.12}
    base = {"wall_loops": 3, "sparse_infill_density": 3, "layer_height": 0.24}
    variant = {"wall_loops": 5, "sparse_infill_density": 5}
    explicit = {"wall_loops": 6}
    out = resolve_settings(explicit, last, hints, named, goal_base=base, variant=variant, nozzle="0.4")
    assert {k: out.get(k) for k in keys} == {
        "wall_loops": 6,  # explicit
        "sparse_infill_density": 5,  # variant
        "sparse_infill_pattern": "honeycomb",  # last job: the base point does not set it
        "layer_height": 0.24,  # goal base point
        "brim_type": "outer_only",  # hints
        "seam_position": None,  # nobody sets it: left to the process preset
    }
    # Without the new layers nothing changes.
    assert resolve_settings(explicit, last, hints, named, nozzle="0.4") == resolve_settings(
        explicit, last, hints, named, goal_base=None, variant=None, nozzle="0.4"
    )
    # A base point or variant entry the nozzle cannot take is dropped, like a stale last job's.
    out = resolve_settings({}, None, {}, "standard", goal_base={"layer_height": 0.24}, variant={"wall_loops": 99},
                           nozzle="0.6")
    assert "layer_height" not in out and "wall_loops" not in out
    Overrides.parse(out, "0.6")
