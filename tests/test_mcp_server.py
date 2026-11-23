import base64
import json
import logging
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from starlette.testclient import TestClient

from bambu_mcp.config import Secrets, Settings
from bambu_mcp.events import Event, EventKind
from bambu_mcp.journal import Journal
from bambu_mcp.mcp_server import build_app
from bambu_mcp.printer.link import LinkStatus
from bambu_mcp.printer.state import Hms, PrinterState, Tray
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
    "update_print",
}
WRITE_TOOLS = TOOL_NAMES - {
    "printer_status",
    "ams_status",
    "camera_snapshot",
    "print_history",
    "spools",
    "plates",
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
        if tool["name"] in WRITE_TOOLS:
            assert hints["readOnlyHint"] is False
            assert hints["destructiveHint"] is False
        else:
            assert hints["readOnlyHint"] is True
    text = json.dumps(tools).lower()
    for word in ("access_code", "serial", "token", "host"):
        assert word not in text


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
    assert tariff == {"valid_from": "2027-01-01", "day_uah": 4.80, "night_uah": None}
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
