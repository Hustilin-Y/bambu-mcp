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
from bambu_mcp.printer.link import (
    GET_VERSION,
    PUSH_ALL,
    READ_COMMANDS,
    LinkStatus,
    PrinterLink,
    build_tls_context,
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
