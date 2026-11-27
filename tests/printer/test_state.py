import json
from pathlib import Path

import pytest

from bambu_mcp.printer.state import PrinterState, apply, identity

FIXTURES = Path(__file__).parent.parent / "fixtures" / "p2s"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_full_report_idle():
    report = load("pushall_idle")
    state = apply(PrinterState.empty(), report)
    assert state.gcode_state == report["print"]["gcode_state"].upper()
    assert state.nozzle_diameter == 0.4
    assert len(state.trays) == 4
    assert [t.tray_type for t in state.trays] == ["PETG", "PETG", "PETG", None]
    assert all(t.remain_pct is None for t in state.trays)
    assert state.trays[1].color_rgba == "057748FF"
    assert (state.trays[0].ams, state.trays[3].slot) == (0, 3)


def test_idle_state_value():
    state = apply(PrinterState.empty(), {"print": {"gcode_state": "idle"}})
    assert state.gcode_state == "IDLE"
    assert state.raw_unknown_state is None


@pytest.mark.synthetic
def test_partial_report_keeps_other_fields():
    base = apply(PrinterState.empty(), load("pushall_idle"))
    state = apply(base, {"print": {"mc_percent": 5}})
    assert state.percent == 5
    assert state.trays == base.trays
    assert state.nozzle_diameter == base.nozzle_diameter
    assert state.gcode_state == base.gcode_state
    assert base.percent == 100  # input state is not mutated


@pytest.mark.synthetic
def test_nozzle_from_device_info_or_legacy_field():
    empty = PrinterState.empty()
    dev = {"print": {"device": {"nozzle": {"info": [{"id": 0, "diameter": 0.6}]}}}}
    assert apply(empty, dev).nozzle_diameter == 0.6
    assert apply(empty, {"print": {"nozzle_diameter": "0.4"}}).nozzle_diameter == 0.4
    both = {"print": {"nozzle_diameter": "0.4", **dev["print"]}}
    assert apply(empty, both).nozzle_diameter == 0.6


@pytest.mark.synthetic
def test_unknown_gcode_state():
    state = apply(PrinterState.empty(), {"print": {"gcode_state": "WEIRD"}})
    assert state.gcode_state == "UNKNOWN"
    assert state.raw_unknown_state == "WEIRD"


def test_identity_from_running_fixture():
    report = load("running_layer1")
    state = apply(PrinterState.empty(), report)
    name, task_id, start = identity(state)
    assert name == report["print"]["subtask_name"]
    assert task_id == report["print"]["task_id"]
    assert start == report["print"].get("gcode_start_time")
    assert (state.layer, state.total_layers) == (1, 100)
    assert (state.percent, state.remaining_min) == (24, 21)


def test_non_dict_or_info_messages_ignored():
    base = apply(PrinterState.empty(), load("running_mid"))
    assert apply(base, load("version")) == base
    assert apply(base, load("gcode_line")) == base
    assert apply(base, ["not", "a", "dict"]) == base  # type: ignore[arg-type]
    assert apply(base, {"print": "nope"}) == base


@pytest.mark.synthetic
def test_hms_and_error_parsed():
    report = {"print": {"hms": [{"attr": 5, "code": 7}, {"bad": 1}], "print_error": 3}}
    state = apply(PrinterState.empty(), report)
    assert [(h.attr, h.code) for h in state.hms] == [(5, 7)]
    assert state.print_error == 3


@pytest.mark.synthetic
def test_rfid_remain_percent():
    tray = {"id": "2", "tray_type": "PLA", "tray_color": "FF0000FF", "remain": 80}
    report = {"print": {"ams": {"ams": [{"id": "0", "tray": [tray]}]}}}
    (parsed,) = apply(PrinterState.empty(), report).trays
    assert (parsed.ams, parsed.slot, parsed.remain_pct) == (0, 2, 80)
