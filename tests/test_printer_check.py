import json
from pathlib import Path

import pytest

from bambu_mcp import printer_check as pc
from bambu_mcp.config import Secrets, Settings

FIXTURES = Path(__file__).parent / "fixtures" / "p2s"
SERIAL = "TESTSERIAL0001"
CODE = "12345678"
SECRETS = Secrets(access_code=CODE, serial=SERIAL, host="192.0.2.30")
JPEG = b"\xff\xd8\xff\xe0frame\xff\xd9"


def _pushall() -> dict:
    report = json.loads((FIXTURES / "pushall_idle.json").read_text(encoding="utf-8"))
    report["print"]["sn"] = SERIAL  # make sure a serial in the report gets scrubbed
    report["print"]["sdcard"] = True  # captured before the USB stick went in
    return report


def _version() -> dict:
    return {"info": {"command": "get_version", "module": [{"name": "ota", "sn": SERIAL, "sw_ver": "01.03.00.00"}]}}


class FakeProbe:
    def __init__(self, *, version=None, pushall=None, frame=JPEG, listing=("model", "timelapse"), fail=None):
        self.version = _version() if version is None else version
        self.pushall = _pushall() if pushall is None else pushall
        self.frame = frame
        self.listing = list(listing)
        self.fail = fail or {}
        self.uploads: list[str] = []

    def mqtt(self):
        if "mqtt" in self.fail:
            raise self.fail["mqtt"]
        return self.version, self.pushall

    def camera(self):
        return self.frame

    def ftps_list(self):
        if "ftps" in self.fail:
            raise self.fail["ftps"]
        return self.listing

    def ftps_upload(self, local: Path, name: str):
        self.uploads.append(name)


def _run(tmp_path, probe, settings=None, upload_test=False):
    settings = settings or Settings(printer="p2s")
    return pc.run_checks(probe, settings, SECRETS, tmp_path, upload_test=upload_test)


def test_report_lines_from_fake_printer(tmp_path):
    code = _run(tmp_path, FakeProbe())
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    dump = (tmp_path / "pushall.redacted.json").read_text(encoding="utf-8")
    assert code == 0
    for line in (
        "OK TLS and CN",
        "OK get_version",
        "OK pushall",
        "OK external spool reported",
        "OK AMS: 1 unit",
        "OK storage present",
        "OK camera frame",
        "OK FTPS login and root listing",
        "SKIP FTPS upload",
    ):
        assert line in report, (line, report)
    for text in (report, dump):
        assert SERIAL not in text
        assert CODE not in text
        assert "192.0.2.30" not in text
    assert json.loads(dump)["print"]["gcode_state"]


def test_confirms_unverified_fields_for_a1(tmp_path):
    pushall = {"print": {"command": "push_status", "gcode_state": "IDLE", "sdcard": True,
                         "vt_tray": {"id": "254", "tray_type": "PLA"}}}
    code = _run(tmp_path, FakeProbe(pushall=pushall), settings=Settings(printer="a1", camera_backend="jpeg_6000"))
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert code == 0
    assert "OK AMS: none" in report
    confirmed = report.split("Profile fields this confirms", 1)[1]
    for name in ("camera", "storage", "ams_units", "ams_slots"):
        assert f"- {name}" in confirmed


def test_failures_are_reported_and_exit_1(tmp_path):
    probe = FakeProbe(frame=None, fail={"ftps": OSError(f"refused by {SERIAL}")})
    assert _run(tmp_path, probe) == 1
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "FAIL camera frame" in report
    assert "FAIL FTPS login and root listing" in report
    assert SERIAL not in report


def test_mqtt_failure_skips_report_checks(tmp_path):
    assert _run(tmp_path, FakeProbe(fail={"mqtt": ConnectionError("no route")})) == 1
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "FAIL TLS and CN" in report
    assert "SKIP pushall" in report


def test_upload_only_with_flag(tmp_path):
    probe = FakeProbe()
    _run(tmp_path, probe)
    assert probe.uploads == []
    _run(tmp_path, probe, upload_test=True)
    assert probe.uploads == ["printer-check.txt"]
    assert "OK FTPS upload" in (tmp_path / "report.md").read_text(encoding="utf-8")


class FakeClient:
    """Stands in for paho: connects at once, answers the two reads with canned reports."""

    def __init__(self, *args, **kwargs):
        self.published: list[dict] = []
        self.on_connect = self.on_message = None

    def username_pw_set(self, *a):
        pass

    def tls_set_context(self, ctx):
        pass

    def connect(self, host, port, keepalive):
        pass

    def loop_start(self):
        self.on_connect(self, None, None, 0, None)

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, topic):
        pass

    def publish(self, topic, payload):
        body = json.loads(payload)
        self.published.append(body)

        class Msg:
            pass

        msg = Msg()
        msg.payload = json.dumps(_version() if "info" in body else _pushall()).encode()
        self.on_message(self, None, msg)


def test_real_mqtt_probe_publishes_only_reads(tmp_path, monkeypatch):
    clients: list[FakeClient] = []

    def factory(*a, **k):
        clients.append(FakeClient())
        return clients[-1]

    monkeypatch.setattr(pc, "build_tls_context", lambda settings, serial: object())
    probe = pc.LiveProbe(SECRETS, Settings(printer="p2s"), client_factory=factory, wait_s=1)
    version, pushall = probe.mqtt()
    assert version["info"]["command"] == "get_version"
    assert pushall["print"]["gcode_state"]
    (client,) = clients
    assert client.published
    assert all(p in pc.SENDABLE for p in client.published)


def test_sendable_is_only_the_reads():
    from bambu_mcp.printer.link import GET_VERSION, PUSH_ALL

    assert pc.SENDABLE == (GET_VERSION, PUSH_ALL)
    with pytest.raises(ValueError):
        pc._check_sendable({"print": {"command": "project_file"}})
