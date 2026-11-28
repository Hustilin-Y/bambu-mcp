"""The Mini App tab's API (/v1/): snapshot, capabilities, endpoints. Spec 2026-11-28 sections 6.1 and 6.5."""

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import pytest

from bambu_mcp.app_api import SNAPSHOT_MAX, build_capabilities, build_snapshot
from bambu_mcp.config import Secrets, Settings
from bambu_mcp.mcp_server import build_mcp
from bambu_mcp.printer.link import LinkStatus
from bambu_mcp.printer.state import EXTERNAL_AMS, EXTERNAL_TRAY, PrinterState, Tray
from bambu_mcp.store.db import Store

NOW = 1_800_000_000.0
T0 = datetime(2026, 11, 28, 10, 0, tzinfo=UTC)
SERIAL, HOST, ACCESS_CODE = "01P00SERIAL77", "192.0.2.55", "87654321"
CLIENT_TOKEN, APP_TOKEN = "client-token-123", "app-token-456"
SECRETS = Secrets(
    access_code=ACCESS_CODE, serial=SERIAL, host=HOST,
    client_tokens={"assistant": CLIENT_TOKEN}, app_tokens={"miniapp": APP_TOKEN},
)


@dataclass
class StubService:
    state: PrinterState
    link_status: LinkStatus = LinkStatus.CONNECTED
    app_notes: list = field(default_factory=list)


@dataclass
class Env:
    service: StubService
    store: Store
    settings: Settings

    def __iter__(self):
        return iter((self.service, self.store, self.settings))


def _env(tmp_path, printer="p2s", **state) -> Env:
    settings = Settings(data_dir=tmp_path, printer=printer)
    store = Store(tmp_path / "bambu.db")
    store.migrate()
    base = replace(PrinterState.empty(), gcode_state="RUNNING", task_id="777", subtask_name="bracket", **state)
    return Env(StubService(base), store, settings)


@pytest.fixture
def api_env(tmp_path):
    env = _env(tmp_path, trays=(Tray(0, 0, "PLA", "FF0000FF", 80), Tray(0, 1, "PETG", "00FF00FF", None)))
    yield env
    env.store.close()


def test_snapshot_has_no_secrets(api_env):
    api_env.store.add_job("bracket", "part", "{}", "a" * 64, T0)
    blob = json.dumps(build_snapshot(*api_env, now=NOW), ensure_ascii=False)
    for secret in (SERIAL, HOST, ACCESS_CODE, CLIENT_TOKEN, APP_TOKEN, str(api_env.settings.data_dir)):
        assert secret not in blob


def test_snapshot_shape(api_env):
    snap = build_snapshot(*api_env, now=NOW)
    assert snap["status"]["gcode_state"] == "RUNNING"
    assert snap["printer"]["ams_units"] == 4 and "PAUSE" in snap["printer"]["stoppable_states"]
    assert [s["slot"] for s in snap["slots"]] == [1, 2]
    assert snap["slots"][0]["tray_type"] == "PLA"
    assert snap["currency"] == {"code": "UAH", "symbol": "грн"}
    assert snap["truncated"] is False and snap["pending"] == [] and snap["notes"] == []


def test_snapshot_cut_to_limit(tmp_path):
    env = _env(tmp_path)
    for i in range(300):
        env.store.add_job(f"job-{i:03d}-" + "x" * 120, "part", "{}", "a" * 64, T0)
    for i in range(30):
        env.store.open_print((f"print-{i}-" + "y" * 200, str(i), None), T0, "external")
    snap = build_snapshot(*env, now=NOW)
    assert len(json.dumps(snap, ensure_ascii=False).encode()) <= SNAPSHOT_MAX
    assert snap["jobs"][0]["name"].startswith("job-299-")
    env.store.close()


def test_snapshot_truncates_when_over_limit(tmp_path, monkeypatch):
    env = _env(tmp_path)
    for i in range(20):
        env.store.add_job(f"job-{i:03d}-" + "x" * 400, "part", "{}", "a" * 64, T0)
    monkeypatch.setattr("bambu_mcp.app_api.SNAPSHOT_MAX", 6 * 1024)
    snap = build_snapshot(*env, now=NOW)
    assert snap["truncated"] is True
    assert len(json.dumps(snap, ensure_ascii=False).encode()) <= 6 * 1024
    assert snap["jobs"][0]["name"].startswith("job-019-")
    env.store.close()


def test_a1_profile_one_external_slot(tmp_path):
    env = _env(tmp_path, printer="a1")
    assert [s["slot"] for s in build_snapshot(*env, now=NOW)["slots"]] == ["external"]
    env.store.close()


def test_a1_reported_external_tray(tmp_path):
    env = _env(tmp_path, printer="a1", trays=(Tray(EXTERNAL_AMS, EXTERNAL_TRAY, "PLA", "FFFFFFFF", None),))
    slots = build_snapshot(*env, now=NOW)["slots"]
    assert [(s["slot"], s["tray_type"]) for s in slots] == [("external", "PLA")]
    env.store.close()


def test_pending_lists_sent_unexpired_confirmations(api_env):
    store = api_env.store
    c = store.add_confirmation(None, "start", job_id=None, plate=1, gate_hash="h", payload_json="{}", now=NOW)
    store.record_sent(f"confirm:{c.id}", "4242", "🖨 Печатать «bracket»?", NOW)
    (pending,) = build_snapshot(*api_env, now=NOW + 1)["pending"]
    assert pending == {
        "message_id": "4242", "kind": "start", "text": "🖨 Печатать «bracket»?",
        "labels": ["Стол чистый, печатать", "Отмена"], "expires_at": c.expires_at,
    }
    assert c.id not in json.dumps(build_snapshot(*api_env, now=NOW + 1))


def test_pending_skips_unsent(api_env):
    api_env.store.add_confirmation(None, "stop", job_id=None, plate=None, gate_hash=None, payload_json="{}", now=NOW)
    assert build_snapshot(*api_env, now=NOW + 1)["pending"] == []


def test_pending_control_follows_the_state(api_env):
    api_env.store.record_sent("control:777", "m9", "▶️ Печать началась", NOW)
    (control,) = build_snapshot(*api_env, now=NOW)["pending"]
    assert control["kind"] == "control" and control["labels"] == ["⏸ Пауза", "Остановить…"]
    api_env.service.state = replace(api_env.service.state, gcode_state="PAUSE")
    assert build_snapshot(*api_env, now=NOW)["pending"][0]["labels"][0] == "▶️ Продолжить"
    api_env.service.state = replace(api_env.service.state, gcode_state="FINISH")
    assert build_snapshot(*api_env, now=NOW)["pending"] == []


def test_notes_are_listed(api_env):
    api_env.service.app_notes = [{"at": NOW, "text": "Печать не подготовлена: принтер занят"}]
    assert build_snapshot(*api_env, now=NOW)["notes"] == [{"at": NOW, "text": "Печать не подготовлена: принтер занят"}]


async def test_capabilities_lists_every_tool(api_env):
    _service, store, settings = api_env
    mcp = build_mcp(api_env.service, store, settings, camera=None)
    caps = await build_capabilities(mcp, settings)
    registered = await mcp.list_tools()
    assert {t["name"] for t in caps["tools"]} == {t.name for t in registered}
    assert {t["class"] for t in caps["tools"]} == {"read", "change", "destructive"}
    by_name = {t["name"]: t for t in caps["tools"]}
    assert by_name["printer_status"]["class"] == "read" and by_name["pause_print"]["class"] == "destructive"
    assert by_name["printer_status"]["summary"].startswith("Current printer state")
    assert any(b["label"] == "▶️ Продолжить" for b in caps["buttons"])
    assert caps["printer"]["model"] == settings.profile.model_name
