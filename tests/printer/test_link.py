"""PrinterLink tests. A fake paho client stands in for the network.

The TLS fixtures under tests/fixtures/synthetic/tls are a throwaway test CA and
two leaves (CN TESTSERIAL0001 and OTHERSERIAL999) generated for these tests
only. The handshake tests run over a local socketpair, never a real network.
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import ssl
import threading
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from bambu_mcp.config import Secrets, Settings
from bambu_mcp.printer import link as link_module
from bambu_mcp.printer.link import (
    GET_VERSION,
    PAUSE_PRINT,
    PUSH_ALL,
    READ_COMMANDS,
    RESUME_PRINT,
    STOP_PRINT,
    TRAY_TYPES,
    LinkStatus,
    PrinterLink,
    PrinterNotConnected,
    build_tls_context,
    project_file,
    tray_setting,
)

TLS = Path(__file__).parent.parent / "fixtures" / "synthetic" / "tls"
SERIAL = "TESTSERIAL0001"
ACCESS_CODE = "12345678"
DER = ssl.PEM_cert_to_DER_cert((TLS / "printer.pem").read_text(encoding="ascii"))
PIN = hashlib.sha256(DER).hexdigest()


def _subject(cn: str) -> dict:
    return {"subject": ((("commonName", cn),),)}


class FakeSocket:
    def __init__(self, cn: str = SERIAL, der: bytes = DER) -> None:
        self.cn, self.der = cn, der

    def getpeercert(self, binary_form: bool = False):
        return self.der if binary_form else _subject(self.cn)


class FakeClient:
    """Records what PrinterLink asks of paho."""

    def __init__(self, *args, **kwargs) -> None:
        self.args, self.kwargs = args, kwargs
        self.calls: list[tuple] = []
        self.sock = FakeSocket()
        self.connect_error: OSError | None = None
        self.publish_rc = mqtt.MQTT_ERR_SUCCESS
        self.on_connect = self.on_disconnect = self.on_message = None

    def username_pw_set(self, username, password=None):
        self.calls.append(("username_pw_set", username, password))

    def tls_set_context(self, context):
        self.calls.append(("tls_set_context", context))

    def connect(self, host, port=1883, keepalive=60, **kwargs):
        self.calls.append(("connect", host, port))
        if self.connect_error:
            raise self.connect_error

    def loop_start(self):
        self.calls.append(("loop_start",))

    def loop_stop(self):
        self.calls.append(("loop_stop",))

    def disconnect(self, *args, **kwargs):
        self.calls.append(("disconnect",))

    def subscribe(self, topic, qos=0, **kwargs):
        self.calls.append(("subscribe", topic))

    def publish(self, topic, payload=None, qos=0, retain=False, **kwargs):
        self.calls.append(("publish", topic, json.loads(payload)))
        info = mqtt.MQTTMessageInfo(len(self.calls))
        info.rc = self.publish_rc
        return info

    def socket(self):
        return self.sock

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    # helpers that play the broker side
    def fire_connect(self, value: int = 0) -> None:
        name = "Success" if value == 0 else None
        rc = ReasonCode(PacketTypes.CONNACK, name) if name else ReasonCode(PacketTypes.CONNACK, identifier=value)
        self.on_connect(self, None, mqtt.ConnectFlags(session_present=False), rc, None)

    def fire_disconnect(self) -> None:
        rc = ReasonCode(PacketTypes.DISCONNECT, "Unspecified error")
        self.on_disconnect(self, None, mqtt.DisconnectFlags(is_disconnect_packet_from_server=False), rc, None)

    def fire_message(self, payload: bytes) -> None:
        msg = mqtt.MQTTMessage(topic=f"device/{SERIAL}/report".encode())
        msg.payload = payload
        self.on_message(self, None, msg)


class Harness:
    def __init__(self, settings: Settings) -> None:
        self.client: FakeClient | None = None
        self.reports: list[dict] = []
        self.statuses: list[LinkStatus] = []
        self.scheduled: list[tuple[float, object]] = []

        def factory(*args, **kwargs):
            self.client = FakeClient(*args, **kwargs)
            return self.client

        self.link = PrinterLink(
            Secrets(access_code=ACCESS_CODE, serial=SERIAL, host="192.0.2.10"),
            settings,
            on_report=self.reports.append,
            on_status=self.statuses.append,
            client_factory=factory,
            schedule=lambda delay, fn: self.scheduled.append((delay, fn)),
        )

    def run_next(self) -> float:
        delay, fn = self.scheduled.pop(0)
        fn()
        return delay


def pin_settings(pin: str = PIN) -> Settings:
    return Settings(tls_mode="pin", cert_sha256=pin)


def ca_settings(mode: str = "ca") -> Settings:
    return Settings(tls_mode=mode, ca_file=TLS / "ca.pem")


@pytest.fixture
def h() -> Harness:
    harness = Harness(ca_settings())
    harness.link.start()
    harness.run_next()
    return harness


def test_read_commands_are_exactly_the_two_allowed_payloads():
    assert GET_VERSION == {"info": {"sequence_id": "0", "command": "get_version"}}
    assert PUSH_ALL == {"pushing": {"sequence_id": "0", "command": "pushall"}}
    assert READ_COMMANDS == (GET_VERSION, PUSH_ALL)


def test_client_is_configured_for_the_printer(h):
    c = h.client
    assert c.args[0] == mqtt.CallbackAPIVersion.VERSION2
    assert c.kwargs.get("reconnect_on_failure") is False
    assert ("username_pw_set", "bblp", ACCESS_CODE) in c.calls
    assert ("connect", "192.0.2.10", 8883) in c.calls
    assert c.names().index("tls_set_context") < c.names().index("connect")
    assert c.names()[-1] == "loop_start"
    assert h.statuses == [LinkStatus.CONNECTING]


def test_subscribes_and_requests_full_report_on_connect(h):
    h.client.calls.clear()
    h.client.fire_connect()
    assert h.client.calls == [
        ("subscribe", f"device/{SERIAL}/report"),
        ("publish", f"device/{SERIAL}/request", GET_VERSION),
        ("publish", f"device/{SERIAL}/request", PUSH_ALL),
    ]
    assert h.statuses[-1] == LinkStatus.CONNECTED


def test_request_full_report_publishes_pushall(h):
    h.client.fire_connect()
    h.client.calls.clear()
    h.link.request_full_report()
    assert h.client.calls == [("publish", f"device/{SERIAL}/request", PUSH_ALL)]


def test_nothing_is_published_before_the_peer_is_verified(h):
    h.client.calls.clear()
    h.link.request_full_report()
    assert "publish" not in h.client.names()


def test_refuses_non_read_command(h):
    h.client.fire_connect()
    h.client.calls.clear()
    for payload in (
        {"print": {"command": "stop"}},
        {"print": {"sequence_id": "0", "command": "pushall"}},
        {"pushing": {"sequence_id": "0", "command": "pushall", "extra": 1}},
        "pushall",
    ):
        with pytest.raises(ValueError):
            h.link._publish(payload)
    assert "publish" not in h.client.names()


# -- the one write: ams_filament_setting ---------------------------------------------

TRAY = dict(
    ams=1,
    slot=2,
    tray_info_idx="GFA00",
    tray_type="PLA",
    color_rgba="FF8800FF",
    nozzle_temp_min=190,
    nozzle_temp_max=230,
)
TRAY_PAYLOAD = {
    "print": {
        "sequence_id": "0",
        "command": "ams_filament_setting",
        "ams_id": 1,
        "tray_id": 2,
        "tray_info_idx": "GFA00",
        "tray_color": "FF8800FF",
        "nozzle_temp_min": 190,
        "nozzle_temp_max": 230,
        "tray_type": "PLA",
    }
}


def test_set_tray_payload_exact(h):
    assert tray_setting(**TRAY) == TRAY_PAYLOAD
    h.client.fire_connect()
    h.client.calls.clear()
    assert h.link.set_tray(**TRAY) == TRAY_PAYLOAD  # the payload it published
    assert h.client.calls == [("publish", f"device/{SERIAL}/request", TRAY_PAYLOAD)]


def test_set_tray_type_list_is_closed_and_upper_case():
    assert TRAY_TYPES == ("PLA", "PETG", "ABS", "ASA", "TPU", "PC", "PA", "PLA-CF", "PETG-CF", "PVA", "PLA-S")
    for tray_type in TRAY_TYPES:
        assert tray_setting(**{**TRAY, "tray_type": tray_type})["print"]["tray_type"] == tray_type
    with pytest.raises(ValueError):
        tray_setting(**{**TRAY, "tray_type": "SUPPORT"})


def _global_index(ams: int, slot: int) -> tuple[int, int]:
    return ams, ams * 4 + slot


@pytest.mark.parametrize("mapping", [None, _global_index], ids=["unit-slot", "global-index"])
def test_every_slot_round_trips_through_publish(h, monkeypatch, mapping):
    """_is_tray_setting inverts tray_ids: a Step 6 change to tray_ids keeps every slot sendable."""
    if mapping is not None:
        monkeypatch.setattr(link_module, "tray_ids", mapping)
    h.client.fire_connect()
    h.client.calls.clear()
    pairs = [(ams, slot) for ams in range(4) for slot in range(4)]
    for ams, slot in pairs:
        h.link.set_tray(**{**TRAY, "ams": ams, "slot": slot})
    sent = [c[2]["print"] for c in h.client.calls if c[0] == "publish"]
    assert [(p["ams_id"], p["tray_id"]) for p in sent] == [link_module.tray_ids(a, s) for a, s in pairs]
    assert len(set(map(json.dumps, sent))) == 16
    # An id pair outside the image of tray_ids is refused.
    bad = {"print": {**TRAY_PAYLOAD["print"], "ams_id": 0, "tray_id": 15 if mapping is None else 16}}
    with pytest.raises(ValueError):
        h.link._publish(bad)


@pytest.mark.parametrize(
    "change",
    [
        {"ams": -1},
        {"ams": 4},
        {"ams": 255},
        {"slot": -1},
        {"slot": 4},
        {"slot": 254},
        {"ams": True},
        {"slot": 1.0},
        {"ams": "1"},
        {"color_rgba": "ff8800ff"},
        {"color_rgba": "#FF8800"},
        {"color_rgba": "FF8800"},
        {"color_rgba": "FF8800FF0"},
        {"color_rgba": "GG8800FF"},
        {"color_rgba": "FF8800F\n"},
        {"color_rgba": None},
        {"tray_info_idx": "gfa00"},
        {"tray_info_idx": "GF"},
        {"tray_info_idx": "GFA000000"},
        {"tray_info_idx": "GF A0"},
        {"tray_info_idx": "GFA00\n"},
        {"tray_info_idx": ""},
        {"tray_type": "pla"},
        {"tray_type": "PLA+"},
        {"tray_type": "PA-CF"},
        {"tray_type": ""},
        {"nozzle_temp_min": 149},
        {"nozzle_temp_max": 321},
        {"nozzle_temp_min": 240, "nozzle_temp_max": 230},
        {"nozzle_temp_min": 190.0},
        {"nozzle_temp_max": False},
    ],
)
def test_set_tray_rejects_bad_colour_and_ids(h, change):
    h.client.fire_connect()
    h.client.calls.clear()
    with pytest.raises(ValueError):
        tray_setting(**{**TRAY, **change})
    with pytest.raises(ValueError):
        h.link.set_tray(**{**TRAY, **change})
    assert "publish" not in h.client.names()


def test_set_tray_accepts_the_bounds(h):
    h.client.fire_connect()
    h.client.calls.clear()
    h.link.set_tray(**{**TRAY, "ams": 0, "slot": 0, "nozzle_temp_min": 150, "nozzle_temp_max": 150})
    h.link.set_tray(**{**TRAY, "ams": 3, "slot": 3, "nozzle_temp_min": 320, "nozzle_temp_max": 320})
    sent = [c[2]["print"] for c in h.client.calls if c[0] == "publish"]
    assert [(p["ams_id"], p["tray_id"], p["nozzle_temp_min"]) for p in sent] == [(0, 0, 150), (3, 3, 320)]


def test_set_tray_raises_when_not_verified(h):
    h.client.calls.clear()
    with pytest.raises(PrinterNotConnected):
        h.link.set_tray(**TRAY)
    assert "publish" not in h.client.names()


def test_set_tray_raises_when_paho_refuses_the_publish(h):
    h.client.fire_connect()
    h.client.publish_rc = mqtt.MQTT_ERR_NO_CONN
    with pytest.raises(PrinterNotConnected) as exc:
        h.link.set_tray(**TRAY)
    assert SERIAL not in str(exc.value) and ACCESS_CODE not in str(exc.value)


def _nested(payload: dict) -> dict:
    return {"print": {**payload["print"], "inner": {"command": "stop"}}}


@pytest.mark.parametrize(
    "payload",
    [
        {"print": {"sequence_id": "0", "command": "stop"}},
        {"print": {"sequence_id": "0", "command": "pause"}},
        {"print": {"sequence_id": "0", "command": "resume"}},
        {"print": {"sequence_id": "0", "command": "gcode_line", "param": "M104 S0"}},
        {"print": {"sequence_id": "0", "command": "project_file", "url": "file:///x.3mf"}},
        {"print": {**TRAY_PAYLOAD["print"], "command": "project_file"}},
        {"print": {**TRAY_PAYLOAD["print"], "command": "gcode_line", "param": "M104 S0"}},
        {"print": {**TRAY_PAYLOAD["print"], "url": "file:///x.3mf"}},
        {"print": {**TRAY_PAYLOAD["print"], "param": "G28"}},
        {"print": {**TRAY_PAYLOAD["print"], "sequence_id": "1"}},
        {"print": {**TRAY_PAYLOAD["print"], "tray_color": "ff8800ff"}},
        {"print": {**TRAY_PAYLOAD["print"], "ams_id": 7}},
        {"print": {**TRAY_PAYLOAD["print"], "ams_id": True}},
        {"print": {**TRAY_PAYLOAD["print"], "nozzle_temp_max": 400}},
        {"print": {k: v for k, v in TRAY_PAYLOAD["print"].items() if k != "tray_type"}},
        _nested(TRAY_PAYLOAD),
        {**TRAY_PAYLOAD, "pushing": {"sequence_id": "0", "command": "pushall"}},
        {**TRAY_PAYLOAD, "system": {"sequence_id": "0", "command": "ledctrl"}},
        {"system": TRAY_PAYLOAD["print"]},
        {"print": [TRAY_PAYLOAD["print"]]},
        [TRAY_PAYLOAD],
        json.dumps(TRAY_PAYLOAD),
        None,
    ],
)
def test_publish_still_refuses_other_commands(h, payload):
    h.client.fire_connect()
    h.client.calls.clear()
    with pytest.raises(ValueError):
        h.link._publish(payload)
    assert "publish" not in h.client.names()


def test_publish_accepts_exactly_the_reads_and_a_valid_tray_setting(h):
    h.client.fire_connect()
    h.client.calls.clear()
    for payload in (GET_VERSION, PUSH_ALL, TRAY_PAYLOAD):
        h.link._publish(payload)
    assert [c[2] for c in h.client.calls] == [GET_VERSION, PUSH_ALL, TRAY_PAYLOAD]


# -- phase 3 writes: project_file, pause, stop -----------------------------------------

PROJECT = dict(
    file="cube_v2.gcode.3mf",
    plate=2,
    md5="0123456789abcdef0123456789abcdef",
    subtask_name="cube_v2",
    task_id="1795385330",
    ams=0,
    slot=2,
)
PROJECT_PAYLOAD = {
    "print": {
        "sequence_id": "0",
        "command": "project_file",
        "param": "Metadata/plate_2.gcode",
        "url": "ftp:///cube_v2.gcode.3mf",
        "file": "cube_v2.gcode.3mf",
        "md5": "0123456789abcdef0123456789abcdef",
        "subtask_name": "cube_v2",
        "project_id": "0",
        "profile_id": "0",
        "task_id": "1795385330",
        "subtask_id": "0",
        "timelapse": False,
        "bed_type": "auto",
        "bed_leveling": True,
        "bed_levelling": True,
        "auto_bed_leveling": 2,
        "flow_cali": False,
        "vibration_cali": False,
        "layer_inspect": True,
        "use_ams": True,
        "ams_mapping": [2],
        "ams_mapping2": [{"ams_id": 0, "slot_id": 2}],
    }
}


def _sent(h) -> list[dict]:
    return [c[2] for c in h.client.calls if c[0] == "publish"]


def test_start_project_payload_exact(h):
    assert project_file(**PROJECT) == PROJECT_PAYLOAD
    h.client.fire_connect()
    h.client.calls.clear()
    assert h.link.start_project(**PROJECT) is None
    assert h.client.calls == [("publish", f"device/{SERIAL}/request", PROJECT_PAYLOAD)]
    # Booleans stay booleans and integers stay integers (dict equality alone would hide it).
    body = project_file(**PROJECT)["print"]
    assert json.dumps(body, sort_keys=True) == json.dumps(PROJECT_PAYLOAD["print"], sort_keys=True)


def test_start_project_ams_mapping_is_global_slot_index(h):
    h.client.fire_connect()
    h.client.calls.clear()
    pairs = [(ams, slot) for ams in range(4) for slot in range(4)]
    for ams, slot in pairs:
        h.link.start_project(**{**PROJECT, "ams": ams, "slot": slot})
    sent = [p["print"] for p in _sent(h)]
    assert [p["ams_mapping"] for p in sent] == [[a * 4 + s] for a, s in pairs]
    assert [p["ams_mapping2"] for p in sent] == [[{"ams_id": a, "slot_id": s}] for a, s in pairs]


def test_start_project_accepts_the_bounds(h):
    h.client.fire_connect()
    h.client.calls.clear()
    longest = "a" * 121 + ".gcode.3mf"
    h.link.start_project(**{**PROJECT, "file": "A.gcode.3mf", "subtask_name": "A", "plate": 1, "task_id": "0"})
    h.link.start_project(
        **{**PROJECT, "file": longest, "subtask_name": "a" * 121, "plate": 50, "task_id": "9" * 19}
    )
    sent = [p["print"] for p in _sent(h)]
    assert [(p["param"], p["task_id"]) for p in sent] == [
        ("Metadata/plate_1.gcode", "0"),
        ("Metadata/plate_50.gcode", "9" * 19),
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"file": "cube_v2.3mf", "subtask_name": "cube_v2"},
        {"file": "cube_v2.gcode", "subtask_name": "cube_v2"},
        {"file": ".cube.gcode.3mf", "subtask_name": ".cube"},
        {"file": "_cube.gcode.3mf", "subtask_name": "_cube"},
        {"file": "dir/cube.gcode.3mf", "subtask_name": "dir/cube"},
        {"file": "../cube.gcode.3mf", "subtask_name": "../cube"},
        {"file": "cube v2.gcode.3mf", "subtask_name": "cube v2"},
        {"file": "куб.gcode.3mf", "subtask_name": "куб"},
        {"file": "cube.gcode.3mf\n", "subtask_name": "cube"},
        {"file": "a" * 122 + ".gcode.3mf", "subtask_name": "a" * 122},
        {"file": ".gcode.3mf", "subtask_name": ""},
        {"file": None},
        {"subtask_name": "other"},
        {"subtask_name": "cube_v2.gcode.3mf"},
        {"subtask_name": None},
        {"md5": "0123456789ABCDEF0123456789ABCDEF"},
        {"md5": "0123456789abcdef0123456789abcde"},
        {"md5": "0123456789abcdef0123456789abcdef0"},
        {"md5": "0123456789abcdef0123456789abcdeg"},
        {"md5": "0123456789abcdef0123456789abcde\n"},
        {"md5": None},
        {"plate": 0},
        {"plate": 51},
        {"plate": -1},
        {"plate": True},
        {"plate": 2.0},
        {"plate": "2"},
        {"task_id": ""},
        {"task_id": "1" * 20},
        {"task_id": "-1"},
        {"task_id": "12a"},
        {"task_id": "123\n"},
        {"task_id": "１２３"},
        {"task_id": 123},
        {"ams": -1},
        {"ams": 4},
        {"ams": True},
        {"ams": "0"},
        {"slot": 4},
        {"slot": -1},
        {"slot": 2.0},
        {"slot": None},
    ],
)
def test_start_project_rejects_bad_fields(h, change):
    h.client.fire_connect()
    h.client.calls.clear()
    with pytest.raises(ValueError):
        project_file(**{**PROJECT, **change})
    with pytest.raises(ValueError):
        h.link.start_project(**{**PROJECT, **change})
    assert "publish" not in h.client.names()


def test_pause_and_stop_payloads_exact(h):
    assert PAUSE_PRINT == {"print": {"sequence_id": "0", "command": "pause", "param": ""}}
    assert STOP_PRINT == {"print": {"sequence_id": "0", "command": "stop", "param": ""}}
    h.client.fire_connect()
    h.client.calls.clear()
    assert h.link.pause() is None
    assert h.link.stop_print() is None
    assert h.client.calls == [
        ("publish", f"device/{SERIAL}/request", PAUSE_PRINT),
        ("publish", f"device/{SERIAL}/request", STOP_PRINT),
    ]
    assert LinkStatus.DISCONNECTED not in h.statuses  # stopping the print is not closing the link


@pytest.mark.parametrize(
    "send",
    [
        lambda link: link.start_project(**PROJECT),
        lambda link: link.pause(),
        lambda link: link.stop_print(),
    ],
    ids=["project_file", "pause", "stop"],
)
def test_new_writes_raise_when_not_sent(h, send):
    h.client.calls.clear()
    with pytest.raises(PrinterNotConnected):
        send(h.link)  # peer not verified yet
    assert "publish" not in h.client.names()
    h.client.fire_connect()
    h.client.calls.clear()
    h.client.publish_rc = mqtt.MQTT_ERR_NO_CONN
    with pytest.raises(PrinterNotConnected) as exc:
        send(h.link)
    assert SERIAL not in str(exc.value) and ACCESS_CODE not in str(exc.value)
    assert len(_sent(h)) == 1  # one attempt, no retry


def test_new_writes_are_never_resent_on_reconnect(h):
    h.client.fire_connect()
    h.client.publish_rc = mqtt.MQTT_ERR_NO_CONN
    with pytest.raises(PrinterNotConnected):
        h.link.start_project(**PROJECT)
    h.client.publish_rc = mqtt.MQTT_ERR_SUCCESS
    h.client.calls.clear()
    h.client.fire_disconnect()
    h.run_next()
    h.client.fire_connect()  # a reconnect sends only the two reads
    assert _sent(h) == [GET_VERSION, PUSH_ALL]


def _pf(**changes) -> dict:
    return {"print": {**PROJECT_PAYLOAD["print"], **changes}}


def _pf_without(key: str) -> dict:
    return {"print": {k: v for k, v in PROJECT_PAYLOAD["print"].items() if k != key}}


@pytest.mark.parametrize(
    "payload",
    [
        {"print": {"sequence_id": "0", "command": "resume", "param": ""}},
        {"print": {"sequence_id": "0", "command": "resume"}},
        {"print": {"sequence_id": "0", "command": "gcode_line", "param": "G28"}},
        {"print": {"sequence_id": "0", "command": "gcode_line", "param": ""}},
        {"print": {"sequence_id": "0", "command": "pause", "param": "", "extra": 1}},
        {"print": {"sequence_id": "0", "command": "stop", "param": "G28"}},
        {"print": {"sequence_id": "1", "command": "stop", "param": ""}},
        {"print": {"sequence_id": "0", "command": "pause"}},
        {"print": {"sequence_id": "0", "command": "stop", "param": None}},
        {**STOP_PRINT, "pushing": PUSH_ALL["pushing"]},
        {"system": STOP_PRINT["print"]},
        {"print": [PAUSE_PRINT["print"]]},
        json.dumps(STOP_PRINT),
        # project_file with an extra key, a missing key, or a second command key
        _pf(extra=1),
        _pf(gcode_line="G28"),
        _pf(command="gcode_line"),
        _pf(command2="stop"),
        _pf_without("md5"),
        _pf_without("ams_mapping2"),
        {**PROJECT_PAYLOAD, "pushing": PUSH_ALL["pushing"]},
        {**PROJECT_PAYLOAD, "system": {"sequence_id": "0", "command": "ledctrl"}},
        _pf(inner={"command": "stop"}),
        # url not ftp:///<file>
        _pf(url="ftp:///other.gcode.3mf"),
        _pf(url="ftp://192.0.2.1/cube_v2.gcode.3mf"),
        _pf(url="file:///sdcard/cube_v2.gcode.3mf"),
        _pf(url="https://example.com/cube_v2.gcode.3mf"),
        _pf(url="ftp:///cube_v2.gcode.3mf "),
        # every field must be what project_file builds
        _pf(file="other.gcode.3mf"),
        _pf(file="other.gcode.3mf", url="ftp:///other.gcode.3mf"),
        _pf(subtask_name="other"),
        _pf(param="Metadata/plate_0.gcode"),
        _pf(param="Metadata/plate_51.gcode"),
        _pf(param="Metadata/plate_02.gcode"),
        _pf(param="G28"),
        _pf(param=2),
        _pf(md5="0123456789ABCDEF0123456789ABCDEF"),
        _pf(task_id=1795385330),
        _pf(task_id="abc"),
        _pf(subtask_id="1"),
        _pf(project_id="1"),
        _pf(profile_id="1"),
        _pf(sequence_id="1"),
        _pf(timelapse=True),
        _pf(timelapse=0),
        _pf(use_ams=1),
        _pf(bed_type="textured_plate"),
        _pf(bed_leveling=False),
        _pf(bed_levelling=1),
        _pf(auto_bed_leveling=True),
        _pf(auto_bed_leveling=2.0),
        _pf(flow_cali=True),
        _pf(vibration_cali=True),
        _pf(layer_inspect=False),
        _pf(ams_mapping=[3]),
        _pf(ams_mapping=[True]),
        _pf(ams_mapping=[2.0]),
        _pf(ams_mapping=[2, 3]),
        _pf(ams_mapping=2),
        _pf(ams_mapping2=[{"ams_id": 0, "slot_id": 3}]),
        _pf(ams_mapping2=[{"ams_id": 0, "slot_id": 2, "x": 1}]),
        _pf(ams_mapping2=[{"ams_id": False, "slot_id": 2}]),
        _pf(ams_mapping2=[{"ams_id": 0, "slot_id": 2.0}]),
        _pf(ams_mapping2=[{"ams_id": 0, "slot_id": 9}]),
        _pf(ams_mapping2=[{"ams_id": 0, "slot_id": 2}, {"ams_id": 0, "slot_id": 2}]),
        _pf(ams_mapping2=[]),
        _pf(ams_mapping2={"ams_id": 0, "slot_id": 2}),
        _pf(ams_mapping2=["x"]),
        None,
    ],
)
def test_publish_refuses_everything_else(h, payload):
    h.client.fire_connect()
    h.client.calls.clear()
    with pytest.raises(ValueError):
        h.link._publish(payload)
    assert "publish" not in h.client.names()


def test_publish_accepts_exactly_the_allowed_payloads(h):
    h.client.fire_connect()
    h.client.calls.clear()
    allowed = (GET_VERSION, PUSH_ALL, TRAY_PAYLOAD, PROJECT_PAYLOAD, PAUSE_PRINT, STOP_PRINT)
    for payload in allowed:
        h.link._publish(payload)
    assert _sent(h) == list(allowed)


def test_auth_failure_stops_reconnecting(h, caplog):
    caplog.set_level(logging.DEBUG)
    h.client.fire_connect(135)  # Not authorized
    assert h.statuses[-1] == LinkStatus.AUTH_FAILED
    assert "loop_stop" in h.client.names()
    h.client.fire_disconnect()
    assert h.scheduled == []
    assert h.client.names().count("connect") == 1
    assert h.statuses[-1] == LinkStatus.AUTH_FAILED
    assert ACCESS_CODE not in caplog.text and SERIAL not in caplog.text


def test_bad_username_or_password_is_auth_failure(h):
    h.client.fire_connect(134)
    assert h.statuses[-1] == LinkStatus.AUTH_FAILED
    assert h.scheduled == []


def test_network_error_reconnects_with_backoff(h):
    delays = []
    for _ in range(9):
        h.client.fire_disconnect()
        assert h.statuses[-1] == LinkStatus.DISCONNECTED
        delays.append(h.run_next())
    assert delays == [5, 10, 20, 40, 80, 160, 300, 300, 300]
    assert h.client.names().count("connect") == 10


def test_backoff_resets_after_a_verified_connect(h):
    h.client.fire_disconnect()
    h.run_next()
    h.client.fire_disconnect()
    h.run_next()
    h.client.fire_connect()
    h.client.fire_disconnect()
    assert h.scheduled[0][0] == 5


def test_connect_error_schedules_backoff(h):
    h.client.connect_error = ConnectionRefusedError("refused")
    h.client.fire_disconnect()
    h.run_next()
    assert h.statuses[-2:] == [LinkStatus.CONNECTING, LinkStatus.DISCONNECTED]
    assert [d for d, _ in h.scheduled] == [10]


def test_stop_disconnects_and_cancels_reconnect(h):
    h.client.fire_connect()
    h.client.fire_disconnect()
    h.link.stop()
    assert {"disconnect", "loop_stop"} <= set(h.client.names())
    h.client.calls.clear()
    h.run_next()  # the pending reconnect fires after stop
    assert "connect" not in h.client.names()
    assert h.statuses[-1] == LinkStatus.DISCONNECTED


def test_reports_are_delivered(h):
    h.client.fire_connect()
    h.client.fire_message(b'{"print": {"gcode_state": "IDLE"}}')
    assert h.reports == [{"print": {"gcode_state": "IDLE"}}]


def test_malformed_json_is_logged_and_skipped(h, caplog):
    h.client.fire_connect()
    with caplog.at_level(logging.WARNING):
        h.client.fire_message(b"{not json")
        h.client.fire_message(b"\xff\xfe")
        h.client.fire_message(b"[1, 2]")
    assert h.reports == []
    assert caplog.text.count("malformed") == 3
    assert SERIAL not in caplog.text


def test_report_handler_error_does_not_escape(h, caplog):
    def boom(report):
        raise RuntimeError("handler bug")

    h.link._on_report = boom
    h.client.fire_connect()
    h.client.fire_message(b"{}")
    assert "handler bug" in caplog.text


def test_tls_pin_mismatch_disconnects():
    h = Harness(pin_settings())
    h.link.start()
    h.run_next()
    h.client.sock = FakeSocket(der=b"not the pinned certificate")
    h.client.calls.clear()
    h.client.fire_connect()
    assert h.client.names() == ["disconnect"]
    assert LinkStatus.CONNECTED not in h.statuses


def test_tls_pin_match_connects():
    h = Harness(pin_settings(PIN.upper()))
    h.link.start()
    h.run_next()
    h.client.fire_connect()
    assert h.statuses[-1] == LinkStatus.CONNECTED


def test_ca_peer_cn_mismatch_disconnects_then_backs_off(h):
    h.client.sock = FakeSocket(cn="OTHERSERIAL999")
    h.client.calls.clear()
    h.client.fire_connect()
    assert h.client.names() == ["disconnect"]
    assert LinkStatus.CONNECTED not in h.statuses
    h.client.fire_disconnect()
    assert h.statuses[-1] == LinkStatus.DISCONNECTED
    assert h.scheduled[0][0] == 5


def test_ca_unreadable_peer_cert_disconnects(h):
    class Broken:
        def getpeercert(self, binary_form=False):
            raise ValueError("no handshake")

    h.client.sock = Broken()
    h.client.calls.clear()
    h.client.fire_connect()
    assert h.client.names() == ["disconnect"]


def test_ca_no_hostname_skips_cn_check():
    h = Harness(ca_settings("ca-no-hostname"))
    h.link.start()
    h.run_next()
    h.client.sock = FakeSocket(cn="OTHERSERIAL999")
    h.client.fire_connect()
    assert h.statuses[-1] == LinkStatus.CONNECTED


# --- build_tls_context -------------------------------------------------------


@pytest.mark.parametrize("mode", ["ca", "ca-no-hostname"])
def test_ca_modes_verify_chain_without_hostname(mode):
    ctx = build_tls_context(ca_settings(mode), serial=SERIAL)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is False


def test_ca_mode_requires_ca_file():
    with pytest.raises(ValueError):
        build_tls_context(Settings(tls_mode="ca"), serial=SERIAL)


@pytest.mark.parametrize("serial", [None, ""])
def test_ca_mode_requires_serial(serial):
    with pytest.raises(ValueError):
        build_tls_context(ca_settings(), serial=serial)
    with pytest.raises(ValueError):
        build_tls_context(ca_settings())


def test_ca_no_hostname_needs_no_serial():
    _handshake(build_tls_context(ca_settings("ca-no-hostname")), "other")


@pytest.mark.parametrize("serial", [None, ""])
def test_ca_peer_check_without_serial_fails_closed(serial):
    from bambu_mcp.printer.link import _verify_peer

    with pytest.raises(ssl.SSLCertVerificationError):
        _verify_peer(FakeSocket(cn=""), "ca", serial, None)


def test_pin_mode_uses_no_ca():
    ctx = build_tls_context(pin_settings())
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


@pytest.mark.parametrize("pin", [None, "", "abc", "g" * 64, PIN + "0", PIN[:-1]])
def test_pin_mode_refuses_missing_or_malformed_pin(pin):
    with pytest.raises(ValueError):
        build_tls_context(pin_settings(pin))
    with pytest.raises(ValueError):
        Harness(pin_settings(pin))


# --- real TLS handshakes over a local socketpair -------------------------------


def _handshake(ctx: ssl.SSLContext, leaf: str) -> None:
    """Runs a TLS handshake against a local server presenting ``leaf``."""
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(TLS / f"{leaf}.pem", TLS / f"{leaf}.key")
    a, b = socket.socketpair()

    def serve():
        try:
            with server_ctx.wrap_socket(b, server_side=True) as s:
                s.recv(1)
        except (OSError, ssl.SSLError):
            pass

    t = threading.Thread(target=serve)
    t.start()
    try:
        # Same call shape as paho: IP as server_hostname, handshake afterwards.
        with ctx.wrap_socket(a, server_hostname="192.0.2.10", do_handshake_on_connect=False) as tls:
            tls.do_handshake()
    finally:
        a.close()
        b.close()
        t.join(5)


def test_handshake_ca_accepts_the_printer():
    _handshake(build_tls_context(ca_settings(), serial=SERIAL), "printer")


def test_handshake_ca_rejects_other_cn_before_any_mqtt_bytes():
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(build_tls_context(ca_settings(), serial=SERIAL), "other")


def test_handshake_ca_no_hostname_accepts_other_cn():
    _handshake(build_tls_context(ca_settings("ca-no-hostname"), serial=SERIAL), "other")


def test_handshake_ca_rejects_untrusted_chain():
    settings = Settings(tls_mode="ca", ca_file=TLS / "printer.pem")  # not a CA
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(build_tls_context(settings, serial=SERIAL), "other")


def test_handshake_pin_accepts_pinned_cert():
    _handshake(build_tls_context(pin_settings()), "printer")


def test_handshake_pin_rejects_other_cert():
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(build_tls_context(pin_settings()), "other")


def test_non_oserror_on_connect_reschedules_with_backoff(h, caplog):
    h.client.connect_error = ValueError(f"Invalid host. {ACCESS_CODE} {SERIAL}")
    h.client.fire_disconnect()
    with caplog.at_level(logging.WARNING):
        h.run_next()
    assert h.statuses[-2:] == [LinkStatus.CONNECTING, LinkStatus.DISCONNECTED]
    assert [d for d, _ in h.scheduled] == [10]
    assert "ValueError" in caplog.text
    assert ACCESS_CODE not in caplog.text and SERIAL not in caplog.text


def test_repeated_connect_failures_are_logged_once(h, caplog):
    h.client.connect_error = ConnectionRefusedError("refused")
    h.client.fire_disconnect()
    with caplog.at_level(logging.INFO, logger="bambu_mcp.printer.link"):
        for _ in range(6):
            h.run_next()
    failed = [r for r in caplog.records if "printer connection failed" in r.getMessage()]
    reconnecting = [r for r in caplog.records if "reconnecting to the printer" in r.getMessage()]
    assert len(failed) == 1
    assert len(reconnecting) <= 1
    assert len(h.scheduled) == 1  # still retrying


# -- profiles and the external spool ---------------------------------------------------

from dataclasses import replace as _replace  # noqa: E402

from bambu_mcp.printer.link import _is_allowed  # noqa: E402
from bambu_profiles import load_profile  # noqa: E402

P2S = load_profile("p2s")
P2S_EXT = _replace(P2S, external_spool=True)  # the P2S once its external holder is checked live
A1 = load_profile("a1")
EXT = {"ams": 255, "slot": 254}


def test_p2s_project_file_unchanged_with_profile():
    assert project_file(**PROJECT, profile=P2S) == PROJECT_PAYLOAD


@pytest.mark.synthetic
def test_external_project_file():
    body = project_file(**{**PROJECT, **EXT}, profile=A1)["print"]
    assert body["use_ams"] is False
    assert body["ams_mapping"] == [-1]
    assert "ams_mapping2" not in body
    assert body["layer_inspect"] is False  # the A1 profile's flag
    assert _is_allowed({"print": body}, A1)
    assert _is_allowed({"print": body}, P2S) is False  # P2S flags differ (layer_inspect)
    p2s_body = project_file(**{**PROJECT, **EXT}, profile=P2S_EXT)
    assert _is_allowed(p2s_body, P2S_EXT)


@pytest.mark.synthetic
def test_external_project_file_tampered_refused():
    body = project_file(**{**PROJECT, **EXT}, profile=A1)["print"]
    for change in ({"use_ams": True}, {"ams_mapping": [0]}, {"ams_mapping2": [{"ams_id": 255, "slot_id": 254}]}):
        assert not _is_allowed({"print": {**body, **change}}, A1)


@pytest.mark.synthetic
def test_tray_setting_external_allowed():
    payload = tray_setting(**{**TRAY, **EXT}, profile=A1)
    assert (payload["print"]["ams_id"], payload["print"]["tray_id"]) == (255, 254)
    assert _is_allowed(payload, A1)
    assert _is_allowed(tray_setting(**{**TRAY, **EXT}, profile=P2S_EXT), P2S_EXT)


def test_p2s_refuses_the_external_spool_until_checked():
    with pytest.raises(ValueError, match="no external spool"):
        tray_setting(**{**TRAY, **EXT})
    with pytest.raises(ValueError, match="no external spool"):
        project_file(**{**PROJECT, **EXT})
    assert not _is_allowed(tray_setting(**{**TRAY, **EXT}, profile=P2S_EXT), P2S)
    assert not _is_allowed(project_file(**{**PROJECT, **EXT}, profile=P2S_EXT), P2S)


@pytest.mark.synthetic
@pytest.mark.parametrize("ams, slot", [(255, 0), (0, 254), (254, 255), (255, 255)])
def test_mixed_pairs_rejected(ams, slot):
    with pytest.raises(ValueError):
        tray_setting(**{**TRAY, "ams": ams, "slot": slot}, profile=P2S_EXT)
    payload = tray_setting(**{**TRAY, **EXT}, profile=P2S_EXT)
    payload["print"].update(ams_id=ams, tray_id=slot)
    assert not _is_allowed(payload, P2S_EXT)


def test_a1_rejects_ams_slots():
    with pytest.raises(ValueError):
        tray_setting(**{**TRAY, "ams": 0, "slot": 0}, profile=A1)
    with pytest.raises(ValueError):
        project_file(**{**PROJECT, "ams": 0, "slot": 0}, profile=A1)
    assert not _is_allowed(tray_setting(**{**TRAY, "ams": 0, "slot": 0}), A1)


def test_a1_temp_cap_300():
    tray_setting(**{**TRAY, **EXT, "nozzle_temp_max": 300}, profile=A1)
    with pytest.raises(ValueError, match="150..300"):
        tray_setting(**{**TRAY, **EXT, "nozzle_temp_max": 310}, profile=A1)


def test_no_external_on_a_profile_without_one():
    no_ext = _replace(A1, external_spool=False)
    with pytest.raises(ValueError):
        tray_setting(**{**TRAY, **EXT}, profile=no_ext)
    assert not _is_allowed(tray_setting(**{**TRAY, **EXT}, profile=A1), no_ext)


def test_link_uses_settings_profile():
    harness = Harness(Settings(tls_mode="ca", ca_file=TLS / "ca.pem", printer="a1"))
    with pytest.raises(ValueError):
        harness.link.set_tray(ams=0, slot=0, tray_info_idx="GFA00", tray_type="PLA", color_rgba="FF8800FF",
                              nozzle_temp_min=190, nozzle_temp_max=230)


def test_resume_payload_exact_and_sent(h):
    assert RESUME_PRINT == {"print": {"sequence_id": "0", "command": "resume", "param": ""}}
    h.client.fire_connect()
    h.client.calls.clear()
    assert h.link.resume() is None
    assert h.client.calls == [("publish", f"device/{SERIAL}/request", RESUME_PRINT)]
