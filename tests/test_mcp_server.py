import asyncio
import base64
import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from starlette.testclient import TestClient

from bambu_mcp.camera import CameraBusy
from bambu_mcp.config import Secrets, Settings
from bambu_mcp.events import Event, EventKind
from bambu_mcp.jobs import create_job_from_bytes
from bambu_mcp.journal import Journal
from bambu_mcp.mcp_server import build_app
from bambu_mcp.notify import LogNotifier, Press
from bambu_mcp.printer.link import LinkStatus, PrinterNotConnected, tray_setting
from bambu_mcp.printer.state import Hms, PrinterState, Tray
from bambu_mcp.service import ControlRefused, PrepareResult, PrintRefused, Service, StopRequest
from bambu_mcp.slicer import SliceClient, SliceOutcome
from bambu_mcp.store.db import Store

ACCESS_CODE = "87654321"
SERIAL = "01P00A000000001"
SECRETS = Secrets(
    access_code=ACCESS_CODE,
    serial=SERIAL,
    host="192.0.2.7",
    notify_token="notify-token-xyz",
    client_tokens={"pc": "pc-token-abc", "assistant": "assistant-token-def"},
)
BASE_URL = "http://127.0.0.1:8765"
KYIV = ZoneInfo("Europe/Kyiv")
JPEG = b"\xff\xd8fake-jpeg"

TOOL_NAMES = {
    "printer_status",
    "ams_status",
    "camera_snapshot",
    "print_history",
    "spools",
    "plates",
    "add_spool",
    "update_spool",
    "set_spool_weight",
    "map_slot",
    "add_plate",
    "set_default_plate",
    "set_tariff",
    "set_amortization",
    "set_currency",
    "set_country",
    "update_print",
    "create_job",
    "propose_job",
    "slice_job",
    "job_status",
    "jobs",
    "load_spool",
    "prepare_print",
    "pause_print",
    "request_stop",
}
DESTRUCTIVE_TOOLS = {"prepare_print", "pause_print", "request_stop", "load_spool"}
WRITE_TOOLS = TOOL_NAMES - DESTRUCTIVE_TOOLS - {
    "printer_status",
    "ams_status",
    "camera_snapshot",
    "print_history",
    "spools",
    "plates",
    "job_status",
    "jobs",
}


class FakeService:
    def __init__(self) -> None:
        self.state = replace(
            PrinterState.empty(),
            gcode_state="RUNNING",
            percent=42,
            remaining_min=17,
            layer=12,
            total_layers=80,
            subtask_name="bracket",
            task_id="123",
            hms=(Hms(0x03000100, 0x00010001),),
            trays=(Tray(0, 0, "PLA", "FF0000FF", 80), Tray(0, 1, "PETG", "00FF00FF", None)),
        )
        self.link_status = LinkStatus.CONNECTED
        self.trays_sent: list[dict] = []
        self.full_reports = 0
        self.set_tray_error: Exception | None = None
        self.state_fresh = True
        self.control_error: Exception | None = None
        self.calls: list[tuple] = []

    async def prepare_print(self, job_id: int, plate: int = 1) -> PrepareResult:
        self.calls.append(("prepare_print", job_id, plate))
        if self.control_error is not None:
            raise self.control_error
        return PrepareResult("a" * 32, "🖨 Печатать «bracket»?", ["Цвет слота другой"])

    async def pause_print(self) -> dict:
        self.calls.append(("pause_print",))
        if self.control_error is not None:
            raise self.control_error
        return {"task_id": "123", "job_name": "bracket"}

    async def request_stop(self) -> StopRequest:
        self.calls.append(("request_stop",))
        if self.control_error is not None:
            raise self.control_error
        return StopRequest("b" * 32, "⏹ Остановить печать «bracket»?")

    def set_tray(self, **fields) -> dict:
        if self.set_tray_error is not None:
            raise self.set_tray_error
        self.trays_sent.append(fields)
        return tray_setting(**fields)

    def request_full_report(self) -> None:
        self.full_reports += 1


class Harness:
    def __init__(self, tmp_path, camera_result: bytes | None = JPEG) -> None:
        self.settings = Settings(data_dir=tmp_path)
        self.store = Store(tmp_path / "bambu.db")
        self.store.migrate()
        self.service = FakeService()
        self.camera_calls = 0

        async def camera():
            self.camera_calls += 1
            return camera_result

        self.app = build_app(self.service, self.store, SECRETS, self.settings, camera=camera)
        self.client = TestClient(self.app, base_url=BASE_URL)
        self._id = 0

    def post(self, body: dict, token: str | None = "pc-token-abc", **headers):
        h = {"Accept": "application/json, text/event-stream", **headers}
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        return self.client.post("/mcp", json=body, headers=h)

    def rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            body["params"] = params
        r = self.post(body)
        assert r.status_code == 200, r.text
        payload = r.json()
        assert "error" not in payload, payload
        return payload["result"]

    def call_raw(self, tool: str, /, **arguments) -> dict:
        return self.rpc("tools/call", {"name": tool, "arguments": arguments})

    def call(self, tool: str, /, **arguments) -> dict:
        result = self.call_raw(tool, **arguments)
        assert not result.get("isError"), result
        return result["structuredContent"]


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    with harness.client:
        yield harness
    harness.store.close()


TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


# -- auth --------------------------------------------------------------------------


def test_rejects_missing_or_wrong_token(h, caplog):
    body = TOOLS_LIST
    caplog.set_level(logging.DEBUG)
    for token in (None, "wrong", "pc-token-ab", "pc-token-abcd", ""):
        r = h.post(body, token=token)
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"
    r = h.post(body, token=None, Authorization="Basic pc-token-abc")
    assert r.status_code == 401
    assert h.post(body, token="pc-token-abc").status_code == 200
    assert h.post(body, token="assistant-token-def").status_code == 200
    assert "client assistant" in caplog.text
    for secret in ("pc-token-abc", "assistant-token-def", "wrong"):
        assert secret not in caplog.text


def test_rejects_foreign_host_header(h):
    r = h.post(TOOLS_LIST, Host="evil.example:8765")
    assert r.status_code == 421


# -- tool list ---------------------------------------------------------------------


def test_lists_exact_tool_names(h):
    tools = h.rpc("tools/list")["tools"]
    assert {t["name"] for t in tools} == TOOL_NAMES
    for tool in tools:
        hints = tool["annotations"]
        if tool["name"] in DESTRUCTIVE_TOOLS:
            assert hints["readOnlyHint"] is False
            assert hints["destructiveHint"] is True
        elif tool["name"] in WRITE_TOOLS:
            assert hints["readOnlyHint"] is False
            assert hints["destructiveHint"] is False
        else:
            assert hints["readOnlyHint"] is True
    text = json.dumps(tools).lower()
    for word in ("access_code", "serial", "token", "host"):
        assert word not in text


def test_tool_list_has_no_start_print(h):
    tools = {t["name"]: t for t in h.rpc("tools/list")["tools"]}
    assert not {name for name in tools if "start" in name or "resume" in name}
    for name in DESTRUCTIVE_TOOLS:
        assert tools[name]["annotations"]["destructiveHint"] is True
    assert "Telegram" in tools["prepare_print"]["description"]
    assert "Telegram" in tools["request_stop"]["description"]
    assert "RUNNING" in tools["pause_print"]["description"]


# -- printing tools ------------------------------------------------------------------


def test_prepare_print_returns_the_confirmation_and_starts_nothing(h):
    result = h.call("prepare_print", job_id=5, plate=2)
    assert result["confirm_id"] == "a" * 32
    assert result["text"].startswith("🖨 Печатать")
    assert result["warnings"] == ["Цвет слота другой"]
    assert "Telegram" in result["note"] and "press" in result["note"]
    assert h.service.calls == [("prepare_print", 5, 2)]


def test_pause_and_request_stop_tools(h):
    paused = h.call("pause_print")
    assert paused["sent"] == "pause" and paused["task_id"] == "123"
    assert "resume" in paused["note"].lower()
    stop = h.call("request_stop")
    assert stop["confirm_id"] == "b" * 32 and "Остановить" in stop["text"]
    assert "nothing" in stop["note"].lower() and "Telegram" in stop["note"]
    assert h.service.calls == [("pause_print",), ("request_stop",)]


@pytest.mark.parametrize(
    ("tool", "error", "message"),
    [
        ("prepare_print", PrintRefused(["Принтер занят"], "the printer is busy"), "the printer is busy"),
        ("prepare_print", KeyError("job 9"), "job 9 not found"),
        ("prepare_print", ValueError("job 9 has no plate 3, its plates are [1]"), "no plate 3"),
        ("pause_print", ControlRefused("the printer is IDLE: pause works only while RUNNING"), "IDLE"),
        ("request_stop", ControlRefused("the printer is FINISH: there is no print to stop"), "no print to stop"),
    ],
)
def test_printing_tool_refusals_are_tool_errors(h, tool, error, message):
    h.service.control_error = error
    args = {"job_id": 9, "plate": 3} if tool == "prepare_print" else {}
    result = h.call_raw(tool, **args)
    assert result.get("isError") is True
    assert message in result["content"][0]["text"]


# -- read tools --------------------------------------------------------------------


def test_printer_status_has_no_secrets(h):
    result = h.call_raw("printer_status")
    status = result["structuredContent"]
    assert status["link"] == "connected"
    assert status["gcode_state"] == "RUNNING"
    assert status["percent"] == 42
    assert status["job_name"] == "bracket"
    assert [e["code"] for e in status["hms"]] == ["0300_0100_0001_0001"]
    raw = json.dumps(result)
    for secret in (ACCESS_CODE, SERIAL, SECRETS.host, "pc-token-abc", "notify-token-xyz"):
        assert secret not in raw


def test_ams_status_shows_mapped_spools(h):
    spool = h.call("add_spool", brand="Bambu", material="PLA", color_name="Red",
                   color_hex="#FF0000", price_uah=600, net_g=1000)
    mapped = h.call("map_slot", ams=0, slot=0, spool_id=spool["id"])
    assert mapped["spool"]["id"] == spool["id"]
    trays = h.call("ams_status")["trays"]
    assert [(t["ams"], t["slot"]) for t in trays] == [(0, 0), (0, 1)]
    assert trays[0]["spool"]["color_name"] == "Red"
    assert trays[0]["remain_pct"] == 80
    assert trays[1]["spool"] is None
    assert h.call("map_slot", ams=0, slot=0, spool_id=None)["spool"] is None


def test_camera_snapshot_returns_jpeg_image(h):
    result = h.call_raw("camera_snapshot")
    assert not result.get("isError")
    (content,) = result["content"]
    assert content["type"] == "image"
    assert content["mimeType"] == "image/jpeg"
    assert base64.b64decode(content["data"]) == JPEG


def test_camera_snapshot_failure_is_tool_error(tmp_path):
    harness = Harness(tmp_path, camera_result=None)
    with harness.client:
        result = harness.call_raw("camera_snapshot")
    harness.store.close()
    assert result["isError"] is True
    assert "camera snapshot failed" in result["content"][0]["text"]


# -- inventory ---------------------------------------------------------------------


def test_spool_inventory(h):
    spool = h.call("add_spool", brand="Elegoo", material="PETG", color_name="Black",
                   color_hex="#000000", price_uah=550.0, net_g=1000, filament_preset="Elegoo PETG")
    assert spool["remaining_g"] == 1000
    assert spool["price_per_kg_uah"] == 550.0
    updated = h.call("update_spool", spool_id=spool["id"], price_uah=500.0, color_name="Jet")
    assert (updated["price_uah"], updated["color_name"], updated["brand"]) == (500.0, "Jet", "Elegoo")
    assert h.call("set_spool_weight", spool_id=spool["id"], remaining_g=420)["remaining_g"] == 420
    h.call("update_spool", spool_id=spool["id"], active=False)
    (listed,) = h.call("spools")["spools"]
    assert listed["active"] is False

    missing = h.call_raw("set_spool_weight", spool_id=999, remaining_g=1)
    assert missing["isError"] is True and "no spool 999" in missing["content"][0]["text"]


def test_spool_tools_store_tray_metadata(h):
    spool = h.call("add_spool", brand="Bambu", material="PLA", color_name="Orange", color_hex="#FF8800",
                   price_uah=600, net_g=1000, tray_info_idx="GFA00", nozzle_temp_min=190, nozzle_temp_max=230)
    assert (spool["tray_info_idx"], spool["nozzle_temp_min"], spool["nozzle_temp_max"]) == ("GFA00", 190, 230)
    plain = h.call("add_spool", brand="Elegoo", material="PETG", color_name="Black", color_hex="#000000",
                   price_uah=550, net_g=1000)
    assert (plain["tray_info_idx"], plain["nozzle_temp_min"], plain["nozzle_temp_max"]) == (None, None, None)
    updated = h.call("update_spool", spool_id=plain["id"], tray_info_idx="GFG99", nozzle_temp_min=220,
                     nozzle_temp_max=260)
    assert (updated["tray_info_idx"], updated["nozzle_temp_min"], updated["nozzle_temp_max"]) == ("GFG99", 220, 260)
    assert h.call("update_spool", spool_id=plain["id"], nozzle_temp_max=250)["nozzle_temp_min"] == 220


@pytest.mark.parametrize(
    "fields",
    [
        {"tray_info_idx": "gfa00"},
        {"tray_info_idx": "GF"},
        {"tray_info_idx": "GFA00/../x"},
        {"nozzle_temp_min": 149},
        {"nozzle_temp_max": 321},
        {"nozzle_temp_min": 240, "nozzle_temp_max": 230},
        {"nozzle_temp_min": 270},  # above the stored max 260
    ],
)
def test_spool_tools_reject_bad_tray_metadata(h, fields):
    spool = h.call("add_spool", brand="Elegoo", material="PETG", color_name="Black", color_hex="#000000",
                   price_uah=550, net_g=1000, nozzle_temp_min=220, nozzle_temp_max=260)
    result = h.call_raw("update_spool", spool_id=spool["id"], **fields)
    assert result["isError"] is True
    (stored,) = h.call("spools")["spools"]
    assert (stored["tray_info_idx"], stored["nozzle_temp_min"], stored["nozzle_temp_max"]) == (None, 220, 260)
    added = h.call_raw("add_spool", brand="X", material="PLA", color_name="Y", color_hex="#FFFFFF",
                       price_uah=1, net_g=1000, **{"nozzle_temp_min": 200, "nozzle_temp_max": 220, **fields})
    assert added["isError"] is True
    assert len(h.call("spools")["spools"]) == 1


# -- load_spool ----------------------------------------------------------------------


def _loadable_spool(h, **changes) -> dict:
    fields = dict(brand="Bambu", material="pla", color_name="Orange", color_hex="#ff8800", price_uah=600,
                  net_g=1000, filament_preset="Bambu PLA Basic @BBL P2S", tray_info_idx="GFA00",
                  nozzle_temp_min=190, nozzle_temp_max=230)
    fields.update(changes)
    return h.call("add_spool", **{k: v for k, v in fields.items() if v is not None})


def _idle(h, gcode_state: str = "IDLE") -> None:
    h.service.state = replace(h.service.state, gcode_state=gcode_state)


def test_load_spool_maps_slot_and_sends_once(h):
    _idle(h)
    spool = _loadable_spool(h)
    result = h.call_raw("load_spool", spool_id=spool["id"], ams=0, slot=1)
    assert not result.get("isError"), result
    out = result["structuredContent"]
    assert h.service.trays_sent == [
        dict(ams=0, slot=1, tray_info_idx="GFA00", tray_type="PLA", color_rgba="FF8800FF",
             nozzle_temp_min=190, nozzle_temp_max=230)
    ]
    assert h.service.full_reports == 1
    assert out["sent"] == {
        "command": "ams_filament_setting",
        "ams_id": 0,
        "tray_id": 1,
        "tray_info_idx": "GFA00",
        "tray_type": "PLA",
        "tray_color": "FF8800FF",
        "nozzle_temp_min": 190,
        "nozzle_temp_max": 230,
    }
    assert out["spool"]["id"] == spool["id"]
    assert "printer screen" in out["note"]
    assert h.store.slot_spool(0, 1).id == spool["id"]
    trays = {(t["ams"], t["slot"]): t for t in h.call("ams_status")["trays"]}
    assert trays[(0, 1)]["spool"]["id"] == spool["id"]
    raw = json.dumps(result)
    for secret in (ACCESS_CODE, SERIAL, SECRETS.host, "pc-token-abc", "device/"):
        assert secret not in raw


@pytest.mark.parametrize("gcode_state", ["IDLE", "FINISH", "FAILED"])
def test_load_spool_allowed_when_the_printer_is_not_busy(h, gcode_state):
    _idle(h, gcode_state)
    spool = _loadable_spool(h, color_hex="#00FF00")
    h.call("load_spool", spool_id=spool["id"], ams=0, slot=0)
    assert h.service.trays_sent[0]["color_rgba"] == "00FF00FF"


@pytest.mark.parametrize(
    "gcode_state", ["RUNNING", "PAUSE", "PREPARE", "SLICING", "INIT", "OFFLINE", "UNKNOWN"]
)
def test_load_spool_refuses_while_printing_or_unknown(h, gcode_state):
    _idle(h, gcode_state)
    spool = _loadable_spool(h)
    result = h.call_raw("load_spool", spool_id=spool["id"], ams=0, slot=0)
    assert result["isError"] is True
    assert gcode_state in result["content"][0]["text"]
    assert h.service.trays_sent == [] and h.store.slot_spool(0, 0) is None


@pytest.mark.parametrize("status", [LinkStatus.DISCONNECTED, LinkStatus.CONNECTING, LinkStatus.AUTH_FAILED])
def test_load_spool_refuses_without_a_connected_printer(h, status):
    _idle(h)
    h.service.link_status = status
    spool = _loadable_spool(h)
    result = h.call_raw("load_spool", spool_id=spool["id"], ams=0, slot=0)
    assert result["isError"] is True
    assert "not connected" in result["content"][0]["text"]
    assert h.service.trays_sent == [] and h.store.slot_spool(0, 0) is None


def test_load_spool_refuses_when_the_link_drops_before_the_send(h):
    _idle(h)
    h.service.set_tray_error = PrinterNotConnected("printer link is not connected, nothing was sent")
    spool = _loadable_spool(h)
    result = h.call_raw("load_spool", spool_id=spool["id"], ams=0, slot=0)
    assert result["isError"] is True
    assert "not connected" in result["content"][0]["text"]
    assert h.store.slot_spool(0, 0) is None and h.service.full_reports == 0


@pytest.mark.parametrize(
    ("ams", "slot", "message"),
    [(-1, 0, "0..3"), (4, 0, "0..3"), (0, -1, "0..3"), (0, 4, "0..3"), (1, 0, "no AMS unit 1")],
)
def test_load_spool_refuses_bad_or_absent_slots(h, ams, slot, message):
    _idle(h)  # the fake state has one AMS unit, 0
    spool = _loadable_spool(h)
    result = h.call_raw("load_spool", spool_id=spool["id"], ams=ams, slot=slot)
    assert result["isError"] is True
    assert message in result["content"][0]["text"]
    assert h.service.trays_sent == []


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"tray_info_idx": None}, "tray_info_idx"),
        ({"nozzle_temp_min": None, "nozzle_temp_max": None}, "nozzle_temp_min, nozzle_temp_max"),
        ({"color_hex": ""}, "color_hex"),
        ({"material": ""}, "material"),
        ({"material": "Silk PLA+"}, "material"),
        ({"color_hex": "orange"}, "color_hex"),
        ({"color_hex": "FF8800"}, "color_hex"),
        ({"color_hex": "#FF880000"}, "color_hex"),
        ({"color_hex": "#F80"}, "color_hex"),
    ],
)
def test_load_spool_refuses_incomplete_spools(h, changes, message):
    """Rows written before the spool tools validated colours, straight into the store."""
    _idle(h)
    fields = dict(brand="Bambu", material="PLA", color_name="Orange", color_hex="#FF8800",
                  filament_preset=None, price_uah=600, net_g=1000)
    meta = dict(tray_info_idx="GFA00", nozzle_temp_min=190, nozzle_temp_max=230)
    for key, value in changes.items():
        (fields if key in fields else meta)[key] = value
    spool_id = h.store.add_spool(**fields, **meta)
    result = h.call_raw("load_spool", spool_id=spool_id, ams=0, slot=0)
    assert result["isError"] is True
    assert message in result["content"][0]["text"]
    assert h.service.trays_sent == []


def test_load_spool_refuses_until_a_fresh_report_since_connect(h):
    _idle(h)
    h.service.state_fresh = False
    spool = _loadable_spool(h)
    result = h.call_raw("load_spool", spool_id=spool["id"], ams=0, slot=0)
    assert result["isError"] is True
    assert "first report" in result["content"][0]["text"]
    assert h.service.trays_sent == [] and h.store.slot_spool(0, 0) is None


@pytest.mark.parametrize("color_hex", ["FF8800", "#FF880000", "#F80", "orange", "", "#GG8800", " #FF8800"])
def test_spool_tools_require_rrggbb_colours(h, color_hex):
    added = h.call_raw("add_spool", brand="X", material="PLA", color_name="Y", color_hex=color_hex,
                       price_uah=1, net_g=1000)
    assert added["isError"] is True and "#RRGGBB" in added["content"][0]["text"]
    spool = h.call("add_spool", brand="X", material="PLA", color_name="Y", color_hex="#ff8800",
                   price_uah=1, net_g=1000)
    updated = h.call_raw("update_spool", spool_id=spool["id"], color_hex=color_hex)
    assert updated["isError"] is True and "#RRGGBB" in updated["content"][0]["text"]
    assert [s["color_hex"] for s in h.call("spools")["spools"]] == ["#ff8800"]


def test_load_spool_refuses_missing_and_retired_spools(h):
    _idle(h)
    missing = h.call_raw("load_spool", spool_id=999, ams=0, slot=0)
    assert missing["isError"] is True and "no spool 999" in missing["content"][0]["text"]
    spool = _loadable_spool(h)
    h.call("update_spool", spool_id=spool["id"], active=False)
    retired = h.call_raw("load_spool", spool_id=spool["id"], ams=0, slot=0)
    assert retired["isError"] is True and "inactive" in retired["content"][0]["text"]
    assert h.service.trays_sent == []


def test_plates(h):
    a = h.call("add_plate", name="Textured PEI", bed_type="Textured PEI Plate")
    b = h.call("add_plate", name="Smooth", bed_type="Cool Plate")
    h.call("set_default_plate", plate_id=b["id"])
    plates = h.call("plates")["plates"]
    assert {p["id"]: p["is_default"] for p in plates} == {a["id"]: False, b["id"]: True}
    assert h.call_raw("set_default_plate", plate_id=999)["isError"] is True


# -- journal and cost ----------------------------------------------------------------


def _run_print(store: Store, settings: Settings, name: str, start: datetime, end: datetime) -> int:
    journal = Journal(store, settings)
    state = replace(PrinterState.empty(), subtask_name=name, task_id="1", gcode_start_time="s")
    identity = (name, "1", "s")
    journal.on_event(Event(EventKind.STARTED, identity, start, state))
    print_id = journal.on_event(Event(EventKind.FINISHED, identity, end, state))
    assert print_id is not None
    return print_id


def test_set_tariff_then_history_cost(h):
    tariff = h.call("set_tariff", valid_from="2027-01-01", day_uah=4.80)
    assert tariff == {"valid_from": "2027-01-01", "day_uah": 4.80, "night_uah": None, "currency": "UAH"}
    start = datetime(2027, 1, 2, 10, 0, tzinfo=KYIV)
    _run_print(h.store, h.settings, "bracket", start, start.replace(hour=11))

    (row,) = h.call("print_history", part="brack")["prints"]
    # 60 min at 200 W + 0.05 kWh heat-up = 0.25 kWh at 4.80
    assert row["kwh"] == 0.25
    assert row["cost_power_uah"] == 1.20
    assert row["cost_total_uah"] == 1.20
    assert row["outcome"] == "finished"
    assert row["duration_min"] == 60
    assert row["ended_at"] == "2027-01-02T11:00:00+02:00"
    assert h.call("print_history", part="nothing")["prints"] == []


def test_set_currency_then_new_spool_in_eur(h):
    old = h.call("add_spool", brand="A", material="PLA", color_name="White",
                 color_hex="#FFFFFF", price_uah=700, net_g=1000)
    assert old["currency"] == "UAH"
    assert h.call("set_currency", code="eur", symbol="€") == {"currency": "EUR", "currency_symbol": "€"}
    new = h.call("add_spool", brand="B", material="PLA", color_name="Grey",
                 color_hex="#888888", price_uah=25, net_g=1000)
    assert new["currency"] == "EUR"
    assert h.call("set_tariff", valid_from="2027-01-01", day_uah=0.25)["currency"] == "EUR"
    amort = h.call("set_amortization", valid_from="2027-01-01", printer_price=600, life_hours=3000)
    assert amort["currency"] == "EUR"
    by_id = {s["id"]: s["currency"] for s in h.call("spools")["spools"]}
    assert by_id == {old["id"]: "UAH", new["id"]: "EUR"}  # nothing is converted
    assert h.call("update_spool", spool_id=old["id"], price_uah=22)["currency"] == "EUR"


def test_set_currency_validates(h):
    for code, symbol in (("EURO", "€"), ("E1R", "€"), ("", "€"), ("EUR", ""), ("EUR", "x" * 9),
                         ("EUR", "€\n")):
        result = h.call_raw("set_currency", code=code, symbol=symbol)
        assert result["isError"], (code, symbol)
    assert h.store.currency() == ("UAH", "грн")


def test_set_amortization(h):
    amort = h.call("set_amortization", valid_from="2027-01-01", printer_price=30000,
                   life_hours=5000, upkeep_per_hour=2)
    assert amort == {
        "valid_from": "2027-01-01", "printer_price": 30000, "life_hours": 5000,
        "upkeep_per_hour": 2, "rate_per_hour": 8.0, "currency": "UAH",
    }
    for bad in ({"printer_price": -1}, {"life_hours": 0}, {"upkeep_per_hour": -0.5}):
        args = {"valid_from": "2027-02-01", "printer_price": 30000, "life_hours": 5000, **bad}
        assert h.call_raw("set_amortization", **args)["isError"], bad
    assert h.store.amortization_on(datetime(2027, 3, 1).date()).valid_from.isoformat() == "2027-01-01"


def test_set_country(h):
    assert h.call("set_country", code="pl") == {
        "country": "PL", "timezone": "Europe/Warsaw", "tariff_reminder": False,
        "note": "the timezone applies after a service restart",
    }
    assert h.store.country() == "PL"
    assert h.call("set_country", code="UA") == {
        "country": "UA", "timezone": "Europe/Kyiv", "tariff_reminder": True,
    }
    assert h.call_raw("set_country", code="UKR")["isError"]
    assert h.store.country() == "UA"


def test_print_history_shows_amortization_and_currency(h):
    start = datetime(2027, 1, 2, 10, 0, tzinfo=KYIV)
    print_id = _run_print(h.store, h.settings, "bracket", start, start.replace(hour=11))
    h.store.set_print_fields(print_id, cost_amort=8.0)
    (row,) = h.call("print_history")["prints"]
    assert (row["cost_amort"], row["currency"]) == (8.0, "UAH")
    assert row["cost_total_uah"] == round(row["cost_power_uah"] + 8.0, 2)


def test_update_print_deducts_and_moves_grams(h):
    start = datetime(2027, 1, 2, 10, 0, tzinfo=KYIV)
    print_id = _run_print(h.store, h.settings, "gear", start, start.replace(hour=11))
    a = h.call("add_spool", brand="A", material="PLA", color_name="White",
               color_hex="#FFFFFF", price_uah=600, net_g=1000)
    b = h.call("add_spool", brand="B", material="PLA", color_name="Grey",
               color_hex="#888888", price_uah=500, net_g=1000)

    row = h.call("update_print", print_id=print_id, grams=14.2, spool_id=a["id"], fit="tight")
    assert (row["grams"], row["spool_id"], row["fit"]) == (14.2, a["id"], "tight")
    assert row["cost_filament_uah"] == 8.52
    assert row["cost_total_uah"] == round(8.52 + row["cost_power_uah"], 2)
    remaining = {s["id"]: s["remaining_g"] for s in h.call("spools")["spools"]}
    assert remaining == {a["id"]: 985.8, b["id"]: 1000}

    h.call("update_print", print_id=print_id, grams=20)  # same spool, corrected amount
    h.call("update_print", print_id=print_id, spool_id=b["id"])  # wrong spool, move it
    remaining = {s["id"]: s["remaining_g"] for s in h.call("spools")["spools"]}
    assert remaining == {a["id"]: 1000, b["id"]: 980}
    row = h.call("print_history")["prints"][0]
    assert row["cost_filament_uah"] == 10.0
    assert row["fit"] == "tight"

    bad = h.call_raw("update_print", print_id=print_id, fit="perfect")
    assert bad["isError"] is True
    assert h.call_raw("update_print", print_id=999, fit="ok")["isError"] is True
    assert h.call_raw("update_print", print_id=print_id, spool_id=999)["isError"] is True


def _spools_ab(h):
    a = h.call("add_spool", brand="A", material="PLA", color_name="White",
               color_hex="#FFFFFF", price_uah=600, net_g=1000)
    b = h.call("add_spool", brand="B", material="PLA", color_name="Grey",
               color_hex="#888888", price_uah=500, net_g=1000)
    return a["id"], b["id"]


def _remaining(h) -> dict[int, float]:
    return {s["id"]: s["remaining_g"] for s in h.call("spools")["spools"]}


def _press(journal: Journal, print_id: int, value: str) -> str | None:
    return journal.on_press(Press(1, "m1", f"spool:{print_id}:{value}", datetime.now(KYIV)))


def test_telegram_spool_press_then_correction_refunds_debited_spool(h):
    start = datetime(2027, 1, 2, 10, 0, tzinfo=KYIV)
    print_id = _run_print(h.store, h.settings, "gear", start, start.replace(hour=11))
    a, b = _spools_ab(h)
    h.call("update_print", print_id=print_id, grams=14, spool_id=a)
    assert _remaining(h) == {a: 986, b: 1000}

    journal = Journal(h.store, h.settings)
    assert _press(journal, print_id, str(b)) == "Катушка: B PLA Grey"
    assert _remaining(h) == {a: 1000, b: 986}  # the press moved the deduction

    row = h.call("update_print", print_id=print_id, grams=20)
    assert _remaining(h) == {a: 1000, b: 980}
    assert (row["spool_id"], row["cost_filament_uah"]) == (b, 10.0)

    assert _press(journal, print_id, "none") == "Катушка: не списывать"
    assert _remaining(h) == {a: 1000, b: 1000}
    assert h.call("print_history")["prints"][0]["cost_filament_uah"] is None


def test_correction_after_floored_deduction_refunds_only_removed_grams(h):
    start = datetime(2027, 1, 2, 10, 0, tzinfo=KYIV)
    print_id = _run_print(h.store, h.settings, "gear", start, start.replace(hour=11))
    a, _ = _spools_ab(h)
    h.call("set_spool_weight", spool_id=a, remaining_g=5)
    h.call("update_print", print_id=print_id, grams=14, spool_id=a)
    assert _remaining(h)[a] == 0
    h.call("update_print", print_id=print_id, grams=3)
    assert _remaining(h)[a] == 2


async def test_camera_busy_is_fast_tool_error(tmp_path):
    harness = Harness(tmp_path)

    async def busy():
        raise CameraBusy

    harness.app = build_app(harness.service, harness.store, SECRETS, harness.settings, camera=busy)
    harness.client = TestClient(harness.app, base_url=BASE_URL)
    with harness.client:
        result = harness.call_raw("camera_snapshot")
    harness.store.close()
    assert result["isError"] is True
    assert "camera busy" in result["content"][0]["text"]


# -- serving -----------------------------------------------------------------------


async def test_serve_without_client_tokens_does_not_listen(tmp_path, monkeypatch, caplog):
    from bambu_mcp import mcp_server

    def no_bind(listen):
        raise AssertionError("must not bind without client tokens")

    monkeypatch.setattr(mcp_server, "_bind", no_bind)
    store = Store(tmp_path / "bambu.db")
    no_tokens = replace(SECRETS, client_tokens={})
    await mcp_server.serve(FakeService(), store, no_tokens, Settings(data_dir=tmp_path))
    store.close()
    assert "MCP server not started" in caplog.text


def test_service_runs_mcp_server_in_its_loop(tmp_path):
    from bambu_mcp import service as service_module

    store = Store(tmp_path / "bambu.db")
    (coro,) = service_module.extra_coroutines(FakeService(), SECRETS, Settings(), store)
    assert coro.cr_code is service_module.mcp_server.serve.__code__
    coro.close()
    store.close()


class _FakeSocket:
    closed = False

    def getsockname(self):
        return ("127.0.0.1", 8765)

    def close(self):
        self.closed = True


async def test_serve_logs_uvicorn_crash(tmp_path, monkeypatch, caplog):
    from bambu_mcp import mcp_server

    sock = _FakeSocket()
    monkeypatch.setattr(mcp_server, "_bind", lambda listen: [sock])

    async def crash(self, sockets=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(mcp_server._Server, "serve", crash)
    store = Store(tmp_path / "bambu.db")
    with pytest.raises(RuntimeError):
        await mcp_server.serve(FakeService(), store, SECRETS, Settings(data_dir=tmp_path))
    store.close()
    assert "MCP server crashed" in caplog.text
    assert sock.closed


async def test_serve_startup_exit_does_not_stop_the_loop(tmp_path, monkeypatch, caplog):
    import asyncio
    import sys

    from bambu_mcp import mcp_server

    sock = _FakeSocket()
    monkeypatch.setattr(mcp_server, "_bind", lambda listen: [sock])

    async def exit_on_startup(self, sockets=None):
        await asyncio.sleep(0)
        sys.exit(3)  # what uvicorn does when the lifespan or a bind fails

    monkeypatch.setattr(mcp_server._Server, "serve", exit_on_startup)
    ticks = 0

    async def other_task():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    other = asyncio.ensure_future(other_task())
    store = Store(tmp_path / "bambu.db")
    await mcp_server.serve(FakeService(), store, SECRETS, Settings(data_dir=tmp_path))
    before = ticks
    await asyncio.sleep(0.05)
    assert ticks > before and not other.done()  # the loop and its other tasks survive
    other.cancel()
    store.close()
    assert "MCP server failed to start" in caplog.text
    assert sock.closed


# -- jobs --------------------------------------------------------------------------

MANIFESTS = Path(__file__).parent / "fixtures" / "manifest"
THREE_MF = b"PK\x03\x04fake-3mf-body" + bytes(range(256))
PRESET = "Generic PETG @BBL P2S"
JOB_TRAYS = (Tray(0, 0, "PETG", "FFFFFFFF", None), Tray(0, 1, "PLA", "161616FF", None))
SLICED_PLATES = ({"index": 1, "minutes": 100.0, "grams": {"PETG": 14.0}},)


def manifest_bytes(**changes) -> bytes:
    data = json.loads((MANIFESTS / "part.print.json").read_text(encoding="utf-8"))
    data["file_sha256"] = hashlib.sha256(THREE_MF).hexdigest()
    data.update(changes)
    return json.dumps(data).encode("utf-8")


class GatedSlicer:
    """slice-agent fake; while ``hold`` (thread-safe) is set the slice keeps running."""

    def __init__(self) -> None:
        self.hold = threading.Event()
        self.requests: list = []

    async def health(self) -> bool:
        return True

    async def slice(self, job, file_bytes):
        self.requests.append(job)
        while self.hold.is_set():
            await asyncio.sleep(0.01)
        return SliceOutcome("ok", file=job.out_file, plates=SLICED_PLATES, slicer_version="02.08.02.61")

    async def fetch_png(self, name, plate):
        return None


class CountingWaker:
    def __init__(self) -> None:
        self.calls = 0

    async def wake_and_wait(self, budget_s: float = 120) -> bool:
        self.calls += 1
        return True


class JobHarness(Harness):
    """The MCP app on a real ``Service`` (fake slicer and waker, no printer link)."""

    def __init__(self, tmp_path, *, jobs_root: Path | None = None) -> None:
        self.settings = Settings(
            data_dir=tmp_path, power_w={"PLA": 200.0, "PETG": 230.0}, jobs_root_allowed=jobs_root
        )
        self.store = Store(tmp_path / "bambu.db")
        self.store.migrate()
        self.slicer, self.waker = GatedSlicer(), CountingWaker()

        async def camera():
            return None

        self.service = Service(
            SECRETS,
            self.settings,
            self.store,
            LogNotifier(),
            lambda on_report, on_status: None,
            camera,
            slicer=self.slicer,
            waker=self.waker,
        )
        self.service._state = replace(PrinterState.empty(), trays=JOB_TRAYS)
        self.spool_id = self.store.add_spool("Elegoo", "PETG", "white", "#FFFFFF", PRESET, 550.0, 1000.0)
        self.store.map_slot(0, 0, self.spool_id)
        self.app = build_app(self.service, self.store, SECRETS, self.settings, camera=camera)
        self.client = TestClient(self.app, base_url=BASE_URL)
        self._id = 0

    def add_job(self, **changes) -> int:
        return create_job_from_bytes(self.store, self.settings, manifest_bytes(**changes), THREE_MF).id

    def wait_status(self, job_id: int, status: str) -> None:
        deadline = time.monotonic() + 5
        while self.store.job(job_id).status != status:
            assert time.monotonic() < deadline, self.store.job(job_id)
            time.sleep(0.01)

    def error(self, tool: str, /, **arguments) -> str:
        result = self.call_raw(tool, **arguments)
        assert result.get("isError") is True, result
        return result["content"][0]["text"].removeprefix(f"Error executing tool {tool}: ")


@pytest.fixture
def jh(tmp_path):
    harness = JobHarness(tmp_path)
    with harness.client:
        yield harness
    harness.store.close()


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


def test_create_job_disabled_without_root(jh):
    text = jh.error("create_job", manifest_path=str(jh.settings.data_dir / "x.print.json"))
    assert "path intake is off" in text and "bambu-mcp upload" in text
    assert jh.store.jobs(10) == []


def test_create_job_from_a_path_inside_the_root(tmp_path):
    root = tmp_path / "builds"
    (root / "lamp-arm").mkdir(parents=True)
    (root / "lamp-arm" / "lamp-arm.print.json").write_bytes(manifest_bytes())
    (root / "lamp-arm" / "lamp-arm.3mf").write_bytes(THREE_MF)
    outside = tmp_path / "elsewhere.print.json"
    outside.write_bytes(manifest_bytes())
    (tmp_path / "data").mkdir()
    harness = JobHarness(tmp_path / "data", jobs_root=root)
    with harness.client:
        created = harness.call("create_job", manifest_path=str(root / "lamp-arm" / "lamp-arm.print.json"))
        relative = harness.error("create_job", manifest_path="lamp-arm/lamp-arm.print.json")
        refused = harness.error("create_job", manifest_path=str(outside))
    jobs = harness.store.jobs(10)
    harness.store.close()
    assert created == {"job_id": jobs[0].id, "name": "lamp-arm", "kind": "part", "status": "new"}
    assert len(jobs) == 1
    assert "absolute" in relative
    assert "outside the allowed jobs root" in refused
    assert str(tmp_path) not in relative + refused


def test_propose_job_queues_the_proposal_and_returns_its_text(jh):
    job_id = jh.add_job()
    result = jh.call("propose_job", job_id=job_id)
    assert result["job_id"] == job_id
    assert "lamp-arm" in result["text"]
    assert jh.store.outbox_count() == 1
    assert jh.store.job(job_id).status == "new"
    assert jh.error("propose_job", job_id=999) == "job 999 not found"


def test_slice_job_validates_overrides(jh):
    job_id = jh.add_job()
    cases = {
        "wall_loops": ({"wall_loops": 99}, "1..10"),
        "layer_height": ({"layer_height": 0.3}, "0.12, 0.16, 0.2, 0.24, 0.28"),
        "curr_bed_type": ({"curr_bed_type": "Glass"}, "Textured PEI Plate"),
        "sparse_infill_density": ({"sparse_infill_density": "25%"}, "0..100"),
        "enable_support": ({"enable_support": "yes"}, "true or false"),
        "nozzle_temperature": ({"nozzle_temperature": 250}, "wall_loops"),
        "set": ({"set": "ultra"}, "standard, strong, draft"),
    }
    for settings in ({"set": ["strong"]}, {"set": 1}):
        assert "standard, strong, draft" in jh.error("slice_job", job_id=job_id, settings=settings)
    for key, (settings, allowed) in cases.items():
        text = jh.error("slice_job", job_id=job_id, settings=settings)
        assert key in text and allowed in text, (key, text)
    assert jh.error("slice_job", job_id=999) == "job 999 not found"
    assert jh.waker.calls == 0 and jh.slicer.requests == []
    assert jh.store.job(job_id).status == "new"
    assert jh.store.outbox_count() == 0


def test_slice_job_returns_at_once_and_refuses_a_second_start(jh):
    job_id = jh.add_job()
    jh.slicer.hold.set()
    started = jh.call("slice_job", job_id=job_id)
    assert started == {"job_id": job_id, "status": "slicing", "warning": None}
    assert jh.call("job_status", job_id=job_id)["status"] == "slicing"
    assert jh.error("slice_job", job_id=job_id) == f"job {job_id} is being sliced already"
    jh.slicer.hold.clear()
    jh.wait_status(job_id, "sliced")
    assert len(jh.slicer.requests) == 1 and jh.waker.calls == 1


def test_slice_job_named_set_outranks_the_last_jobs_settings(jh):
    # R6: a set the owner names is explicit, so it beats the part's frozen settings.
    first = jh.add_job(hints={})
    jh.call("slice_job", job_id=first, settings={"wall_loops": 2, "sparse_infill_density": 15})
    jh.wait_status(first, "sliced")
    second = jh.add_job(hints={})
    jh.call("slice_job", job_id=second, settings={"set": "strong"})
    jh.wait_status(second, "sliced")
    third = jh.add_job(hints={})
    jh.call("slice_job", job_id=third, settings={"set": "strong", "wall_loops": 6})
    jh.wait_status(third, "sliced")
    overrides = [dict(r.overrides) for r in jh.slicer.requests]
    assert (overrides[0]["wall_loops"], overrides[0]["sparse_infill_density"]) == (2, 15)
    assert (overrides[1]["wall_loops"], overrides[1]["sparse_infill_density"]) == (4, 25)
    assert (overrides[2]["wall_loops"], overrides[2]["sparse_infill_density"]) == (6, 25)
    assert json.loads(jh.store.job(second).result_json)["named_set"] == "strong"


def test_job_status_has_no_paths_outside_names(jh):
    job_id = jh.add_job()
    fresh = jh.call("job_status", job_id=job_id)
    assert fresh["status"] == "new" and fresh["plates"] == [] and fresh["sliced_file"] is None
    jh.call("slice_job", job_id=job_id, settings={"curr_bed_type": "Cool Plate"})
    jh.wait_status(job_id, "sliced")

    status = jh.call("job_status", job_id=job_id)
    assert status["job_id"] == job_id and status["name"] == "lamp-arm"
    assert status["status"] == "sliced" and status["error"] is None
    assert status["sliced_file"] == f"lamp-arm-{job_id}-p1.gcode.3mf"
    assert status["settings"]["curr_bed_type"] == "Cool Plate"
    assert status["settings"]["wall_loops"] == 4
    assert status["goal"] == {"primary": "balanced", "secondary": None, "source": "fallback", "reason": None}
    assert (status["plate_type"], status["nozzle"]) == ("Cool Plate", "0.4")
    assert status["slot"] == {
        "ams": 0, "slot": 0, "number": 1, "material": "PETG", "color": "white",
        "spool": "Elegoo PETG white",
    }
    (plate,) = status["plates"]
    assert (plate["index"], plate["minutes"], plate["grams"]) == (1, 100.0, 14.0)
    assert plate["cost_filament_uah"] == 7.7
    assert plate["cost_total_uah"] == round(plate["cost_filament_uah"] + plate["cost_power_uah"], 2)

    data_dir = str(jh.settings.data_dir)
    settings_values = set(_strings(status["settings"]))
    for text in _strings(status):
        assert data_dir not in text
        if text not in settings_values:
            assert "/" not in text and "\\" not in text, text
    assert jh.error("job_status", job_id=999) == "job 999 not found"


def test_jobs_lists_newest_first_with_a_capped_limit(jh):
    ids = [jh.add_job() for _ in range(3)]
    listed = jh.call("jobs")["jobs"]
    assert [j["job_id"] for j in listed] == ids[::-1]
    assert listed[0] == {
        "job_id": ids[-1], "name": "lamp-arm", "kind": "part", "status": "new",
        "created_at": listed[0]["created_at"], "sliced_file": None,
    }
    assert [j["job_id"] for j in jh.call("jobs", limit=2)["jobs"]] == ids[:0:-1]
    assert len(jh.call("jobs", limit=0)["jobs"]) == 1
    assert len(jh.call("jobs", limit=500)["jobs"]) == 3


def test_override_keys_match_the_slice_agent_whitelist():
    from bambu_mcp.mcp_server import OVERRIDE_KEYS, _allowed_values
    from bambu_mcp.proposal import NAMED_SETS
    from slice_agent.runner import Overrides

    for key in OVERRIDE_KEYS:
        with pytest.raises(ValueError, match="invalid value"):
            Overrides.parse({key: object()}, "0.4")
        assert _allowed_values(key, "0.4")
    assert {k for values in NAMED_SETS.values() for k in values} <= set(OVERRIDE_KEYS)


def test_mcp_allowed_values_from_runner():
    from bambu_mcp.mcp_server import OVERRIDE_KEYS, _allowed_values
    from slice_agent import runner

    new = ("top_shell_layers", "bottom_shell_layers", "outer_wall_speed", "seam_position", "ironing_type")
    assert set(new) <= set(OVERRIDE_KEYS)
    assert _allowed_values("top_shell_layers", "0.4") == "an integer {}..{}".format(*runner.SHELL_LAYERS_RANGE)
    assert _allowed_values("bottom_shell_layers", "0.4") == "an integer {}..{}".format(*runner.SHELL_LAYERS_RANGE)
    assert _allowed_values("outer_wall_speed", "0.4") == "a number {}..{} (mm/s)".format(*runner.OUTER_WALL_SPEED_RANGE)
    assert _allowed_values("seam_position", "0.4") == ", ".join(runner.SEAM_POSITIONS)
    assert _allowed_values("ironing_type", "0.4") == ", ".join(runner.IRONING_TYPES)
    assert _allowed_values("wall_generator", "0.4") == ", ".join(runner.WALL_GENERATORS)


def test_slice_job_settings_refuse_bad_new_values():
    from bambu_mcp.mcp_server import ToolError, explicit_settings

    explicit, _ = explicit_settings({"outer_wall_speed": 60, "seam_position": "back", "top_shell_layers": 6}, "0.4")
    assert explicit == {"outer_wall_speed": 60, "seam_position": "back", "top_shell_layers": 6}
    with pytest.raises(ToolError, match=r"seam_position: 'rear' is not allowed .*aligned, back, nearest, random"):
        explicit_settings({"seam_position": "rear"}, "0.4")
    with pytest.raises(ToolError, match=r"outer_wall_speed: 400 is not allowed .*20\.\.300"):
        explicit_settings({"outer_wall_speed": 400}, "0.4")


PATH_RUN = re.compile(r"[A-Za-z]:[\\/]|[^\s\\/<>]+[\\/]+[^\s\\/<>]+")


def test_job_status_of_a_failed_slice_hides_agent_paths(jh):
    detail = (
        r"Studio failed: C:\Users\owner\AppData\Local\bambu-mcp\profiles\x.json missing, "
        r"log in <sliced>\.work-lamp-arm-1-p1\log.txt and /home/owner/studio.log"
    )

    def agent(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "slice_failed", "detail": detail})

    http = httpx.AsyncClient(transport=httpx.MockTransport(agent))
    jh.service._slicer = SliceClient("http://192.0.2.20:11436", "slice-token", http)
    job_id = jh.add_job()
    jh.call("slice_job", job_id=job_id)
    jh.wait_status(job_id, "slice_failed")
    asyncio.run(http.aclose())

    status = jh.call("job_status", job_id=job_id)
    assert status["error"] == "Studio failed: <path> missing, log in <sliced><path> and <path>"
    assert status["sliced_file"] is None and status["plates"] == []
    settings_values = set(_strings(status["settings"]))
    for text in _strings(status):
        if text not in settings_values:
            assert not PATH_RUN.search(text), text


def test_job_status_with_an_unreadable_manifest_has_no_nozzle(jh):
    job_id = jh.add_job()
    with jh.store.transaction() as conn:
        conn.execute("UPDATE jobs SET manifest_json = '{' WHERE id = ?", (job_id,))
    status = jh.call("job_status", job_id=job_id)
    assert status["status"] == "new" and status["nozzle"] is None



def test_material_mismatch_warning_without_a_proposal(jh):
    # The part's hint says PETG, only PLA is loaded: Claude Code calls slice_job directly.
    jh.service._state = replace(PrinterState.empty(), trays=(Tray(0, 0, "PLA", "161616FF", None),))
    pla = jh.store.add_spool("Elegoo", "PLA", "black", "#161616", "Generic PLA @BBL P2S", 500.0, 1000.0)
    jh.store.map_slot(0, 0, pla)
    job_id = jh.add_job()
    warning = "⚠️ Нужен PETG, а в AMS его нет: выбран слот 1 PLA. Заправь PETG или подтверди печать из PLA"

    started = jh.call("slice_job", job_id=job_id)
    assert started == {"job_id": job_id, "status": "slicing", "warning": warning}
    jh.wait_status(job_id, "sliced")

    assert jh.call("job_status", job_id=job_id)["warning"] == warning
    assert jh.store.outbox_count() == 1  # no proposal was queued, only the summary
    (summary,) = [
        row["text"]
        for row in jh.store._conn().execute("SELECT kind, text FROM outbox WHERE kind = 'job_sliced'")
    ]
    assert summary.split("\n")[-1] == warning
    assert "слот 1 PLA" in summary


def test_job_status_without_a_mismatch_has_no_warning(jh):
    job_id = jh.add_job()
    assert jh.call("slice_job", job_id=job_id)["warning"] is None
    jh.wait_status(job_id, "sliced")
    assert jh.call("job_status", job_id=job_id)["warning"] is None


# -- the owner's slot choice (Task 12) --------------------------------------------------


def test_slice_job_with_a_slot_uses_its_spool_and_returns_the_mismatch(jh):
    # A PLA part printed from the blue PETG in slot 2, as the owner asked.
    jh.service._state = replace(
        PrinterState.empty(), trays=(Tray(0, 0, "PLA", "FFFFFFFF", None), Tray(0, 1, "PETG", "1E4FC8FF", None))
    )
    pla = jh.store.add_spool("Elegoo", "PLA", "white", "#FFFFFF", "Generic PLA @BBL P2S", 500.0, 1000.0)
    jh.store.map_slot(0, 0, pla)
    blue = jh.store.add_spool("Elegoo", "PETG", "синий", "#1E4FC8", "Generic PETG HF @BBL P2S", 600.0, 1000.0)
    jh.store.map_slot(0, 1, blue)
    job_id = jh.add_job(hints={}, profile={"material": "PLA", "nozzle_mm": 0.4})
    warning = "⚠️ Нужен PLA, а выбран слот 2 PETG: печать будет из PETG"

    started = jh.call("slice_job", job_id=job_id, slot=2)
    assert started == {"job_id": job_id, "status": "slicing", "warning": warning}
    jh.wait_status(job_id, "sliced")
    assert jh.slicer.requests[0].filament == "Generic PETG HF @BBL P2S"
    status = jh.call("job_status", job_id=job_id)
    assert status["chosen_slot"] == 2 and status["warning"] == warning
    assert status["slot"]["number"] == 2 and status["slot"]["spool"] == "Elegoo PETG синий"


def test_bad_slot_lists_the_usable_ones_and_wakes_nothing(jh):
    job_id = jh.add_job()
    # Slot 2 holds PLA but no spool is mapped to it, slot 3 is not in the report.
    for tool in ("slice_job", "propose_job"):
        for slot in (2, 3):
            text = jh.error(tool, job_id=job_id, slot=slot)
            assert text == (
                f"slot {slot} is empty or has no active spool with a filament preset, usable: 1 PETG white"
            )
        for slot in (0, 17):
            assert jh.error(tool, job_id=job_id, slot=slot) == f"slot must be 1..16, got {slot}"
    assert jh.waker.calls == 0 and jh.slicer.requests == []
    assert jh.store.outbox_count() == 0
    job = jh.store.job(job_id)
    assert (job.status, job.slot) == ("new", None)


def test_propose_job_with_a_slot_is_stored_and_shown_by_job_status(jh):
    job_id = jh.add_job()
    assert jh.call("job_status", job_id=job_id)["chosen_slot"] is None
    result = jh.call("propose_job", job_id=job_id, slot=1)
    assert "слот 1 PETG white" in result["text"]
    assert jh.call("job_status", job_id=job_id)["chosen_slot"] == 1

def test_propose_and_slice_job_take_the_owners_goal(jh):
    job_id = jh.add_job(hints={})
    text = jh.call("propose_job", job_id=job_id, goal="strength")["text"]
    assert "цель: прочно (выбрал ты)" in text and "4 стенки, 40% gyroid" in text
    assert json.loads(jh.store.job(job_id).goal_json)["primary"] == "strength"
    assert jh.waker.calls == 0 and jh.slicer.requests == []
    # slice_job: explicit settings win over the goal's base point, the goal is stored.
    jh.call("slice_job", job_id=job_id, settings={"wall_loops": 6}, goal=["watertight", "quality"])
    jh.wait_status(job_id, "sliced")
    sent = dict(jh.slicer.requests[0].overrides)
    assert sent["wall_loops"] == 6 and (sent["top_shell_layers"], sent["bottom_shell_layers"]) == (6, 6)
    assert sent["layer_height"] == 0.16  # the secondary goal fills what the primary leaves
    stored = json.loads(jh.store.job(job_id).goal_json)
    assert stored == {"primary": "watertight", "secondary": "quality", "source": "owner"}
    goal = jh.call("job_status", job_id=job_id)["goal"]
    assert goal == {"primary": "watertight", "secondary": "quality", "source": "owner", "reason": None}


def test_goal_parameter_is_checked(jh):
    job_id = jh.add_job()
    for tool in ("propose_job", "slice_job"):
        for goal in ("ultra", ["fit", "fit"], ["fit", "quality", "speed"], []):
            text = jh.error(tool, job_id=job_id, goal=goal)
            assert "goal" in text and "balanced, speed, strength" in text, (tool, goal)
    tools = {t["name"]: t for t in jh.rpc("tools/list")["tools"]}
    for name in ("propose_job", "slice_job"):
        assert "goal" in tools[name]["inputSchema"]["properties"], name
    assert jh.waker.calls == 0 and jh.slicer.requests == [] and jh.store.outbox_count() == 0
    assert jh.store.job(job_id).goal_json is None


def test_slot_tool_descriptions_name_the_owners_choice(jh):
    tools = {t["name"]: t for t in jh.rpc("tools/list")["tools"]}
    for name in ("propose_job", "slice_job"):
        description = tools[name]["description"]
        assert "owner" in description and "material" in description, name
        assert "slot" in tools[name]["inputSchema"]["properties"], name


def test_set_tariff_and_amortization_refuse_non_finite_numbers(h):
    # JSON has no infinity: a client sends it as a string, which the tool's lax validation takes.
    inf, nan = "inf", "nan"
    for args in ({"day_uah": inf}, {"day_uah": "-inf"}, {"day_uah": nan}, {"day_uah": 4.32, "night_uah": inf}):
        result = h.call_raw("set_tariff", valid_from="2027-01-01", **args)
        assert result["isError"], args
    assert h.store.tariff_on(datetime(2027, 2, 1).date()).valid_from.isoformat() == "2024-06-01"
    base = {"valid_from": "2027-01-01", "printer_price": 30000, "life_hours": 5000, "upkeep_per_hour": 2}
    for bad in ({"printer_price": inf}, {"life_hours": inf}, {"upkeep_per_hour": inf},
                {"printer_price": nan}, {"printer_price": 1e308, "life_hours": 1e-300}):
        result = h.call_raw("set_amortization", **{**base, **bad})
        assert result["isError"], bad
    assert h.store.amortization_on(datetime(2027, 2, 1).date()) is None


def test_set_country_without_a_timezone_says_so(h):
    result = h.call("set_country", code="US")
    assert result["country"] == "US" and result["timezone"] == h.settings.timezone
    assert result["note"] == "no default timezone for US: set timezone in config.toml"
