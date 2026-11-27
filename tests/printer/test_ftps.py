"""FTPS uploader tests. A fake FTP client stands in for the printer.

The real-handshake tests reuse the synthetic TLS fixtures of test_link.py over
a local socketpair, never a real network.
"""

from __future__ import annotations

import ftplib
import logging
import socket
import ssl
import threading
import time
from pathlib import Path

import pytest

from bambu_mcp.printer import ftps as ftps_module
from bambu_mcp.printer.ftps import ImplicitFTPS, UploadError, build_ftps_context, upload

TLS = Path(__file__).parent.parent / "fixtures" / "synthetic" / "tls"
CA = TLS / "ca.pem"
SERIAL = "TESTSERIAL0001"
ACCESS_CODE = "87654321"
HOST = "192.0.2.10"


class FakeSocket:
    def __init__(self, cn: str = SERIAL) -> None:
        self.cn = cn

    def getpeercert(self, binary_form: bool = False):
        return {"subject": ((("commonName", self.cn),),)}


class FakeFTP:
    """Records the calls upload() makes, scripted by class-level knobs."""

    instances: list[FakeFTP] = []
    cn = SERIAL
    connect_error: BaseException | None = None
    login_error: BaseException | None = None
    stor_error: BaseException | None = None
    size_override: int | None | str = "auto"
    stor_hook = None

    def __init__(self, *, context, server_hostname, timeout) -> None:
        self.context, self.server_hostname, self.timeout = context, server_hostname, timeout
        self.calls: list[tuple] = []
        self.sock = None
        self.stored: dict[str, bytes] = {}
        FakeFTP.instances.append(self)

    def connect(self, host, port):
        self.calls.append(("connect", host, port))
        if self.connect_error:
            raise self.connect_error
        self.sock = FakeSocket(self.cn)
        return "220 ready"

    def login(self, user, passwd):
        self.calls.append(("login", user, passwd))
        if self.login_error:
            raise self.login_error
        return "230 ok"

    def prot_p(self):
        self.calls.append(("prot_p",))
        return "200 ok"

    def storbinary(self, cmd, fp):
        self.calls.append(("storbinary", cmd))
        if self.stor_hook:
            type(self).stor_hook()
        if self.stor_error:
            raise self.stor_error
        verb, _, name = cmd.partition(" ")
        assert verb == "STOR"
        self.stored[name] = fp.read()
        return "226 done"

    def size(self, name):
        self.calls.append(("size", name))
        if self.size_override != "auto":
            return self.size_override
        return len(self.stored[name])

    def quit(self):
        self.calls.append(("quit",))
        return "221 bye"

    def close(self):
        self.calls.append(("close",))

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def fake(monkeypatch):
    FakeFTP.instances = []

    def make(**knobs):
        cls = type("ScriptedFTP", (FakeFTP,), knobs)
        return cls

    return make


@pytest.fixture
def local(tmp_path) -> Path:
    path = tmp_path / "cube.gcode.3mf"
    path.write_bytes(b"PK\x03\x04" + b"x" * 20000)
    return path


def run(local: Path, factory, remote: str = "cube.gcode.3mf", **kw) -> None:
    upload(HOST, SERIAL, ACCESS_CODE, CA, local, remote, ftp_factory=factory, **kw)


# --- upload() ------------------------------------------------------------------


def test_upload_stores_then_checks_size(fake, local):
    run(local, fake(), timeout_s=12)
    (ftp,) = FakeFTP.instances
    assert ftp.calls == [
        ("connect", HOST, 990),
        ("login", "bblp", ACCESS_CODE),
        ("prot_p",),
        ("storbinary", "STOR /cube.gcode.3mf"),
        ("size", "/cube.gcode.3mf"),
        ("quit",),
    ]
    assert ftp.stored["/cube.gcode.3mf"] == local.read_bytes()
    assert ftp.server_hostname == SERIAL
    assert ftp.timeout == 12
    assert isinstance(ftp.context, ssl.SSLContext)
    assert ftp.context.verify_mode == ssl.CERT_REQUIRED
    assert ftp.context.maximum_version == ssl.TLSVersion.TLSv1_2


@pytest.mark.parametrize("remote_size", [0, 19999, 20005, None])
def test_size_mismatch_raises(fake, local, remote_size):
    with pytest.raises(UploadError, match="size"):
        run(local, fake(size_override=remote_size))
    (ftp,) = FakeFTP.instances
    assert ftp.names()[-1] in ("quit", "close")


def test_size_command_refused_raises(fake, local):
    class NoSize(fake()):
        def size(self, name):
            raise ftplib.error_perm("550 no such file")

    with pytest.raises(UploadError, match="size"):
        run(local, NoSize)


def test_cn_mismatch_refuses_before_login(fake, local):
    with pytest.raises(UploadError, match="certificate"):
        run(local, fake(cn="OTHERSERIAL999"))
    (ftp,) = FakeFTP.instances
    assert "login" not in ftp.names()
    assert "storbinary" not in ftp.names()


def test_handshake_failure_on_connect_refuses_before_login(fake, local):
    err = ssl.SSLCertVerificationError("peer certificate CN is not the printer serial")
    with pytest.raises(UploadError, match="certificate"):
        run(local, fake(connect_error=err))
    (ftp,) = FakeFTP.instances
    assert "login" not in ftp.names()


def test_553_no_storage_maps_to_clear_error(fake, local):
    err = ftplib.error_perm("553 Could not create file.")
    with pytest.raises(UploadError) as info:
        run(local, fake(stor_error=err))
    message = str(info.value)
    assert "553" in message and "USB" in message


def test_other_stor_refusal_reports_code_only(fake, local):
    err = ftplib.error_perm("550 Permission denied for /secret/path")
    with pytest.raises(UploadError) as info:
        run(local, fake(stor_error=err))
    assert "550" in str(info.value)
    assert "/secret/path" not in str(info.value)


def test_login_refused_maps_to_clear_error(fake, local):
    with pytest.raises(UploadError, match="login"):
        run(local, fake(login_error=ftplib.error_perm("530 Login incorrect.")))
    (ftp,) = FakeFTP.instances
    assert "storbinary" not in ftp.names()


@pytest.mark.parametrize("err", [ConnectionRefusedError("refused"), TimeoutError("timed out")])
def test_network_errors_map_to_upload_error(fake, local, err):
    with pytest.raises(UploadError, match="reach"):
        run(local, fake(connect_error=err))


def test_connection_lost_during_transfer(fake, local):
    with pytest.raises(UploadError, match="upload"):
        run(local, fake(stor_error=ConnectionResetError("reset")))


def test_quit_failure_falls_back_to_close(fake, local):
    class BadQuit(fake()):
        def quit(self):
            self.calls.append(("quit",))
            raise EOFError

    run(local, BadQuit)
    assert FakeFTP.instances[0].names()[-2:] == ["quit", "close"]


def test_missing_local_file_raises_before_connecting(fake, tmp_path):
    with pytest.raises(UploadError, match="local"):
        run(tmp_path / "nope.gcode.3mf", fake())
    assert FakeFTP.instances == []


@pytest.mark.parametrize(
    "remote", ["", ".", "..", "a/b.3mf", "a\\b.3mf", "x\r\nDELE y", "x\x00", "n" * 256]
)
def test_unsafe_remote_name_is_refused(fake, local, remote):
    with pytest.raises(ValueError):
        run(local, fake(), remote=remote)
    assert FakeFTP.instances == []


def test_missing_serial_or_ca_refuses(fake, local, tmp_path):
    with pytest.raises(UploadError):
        upload(HOST, "", ACCESS_CODE, CA, local, "a.3mf", ftp_factory=fake())
    with pytest.raises(UploadError):
        upload(HOST, SERIAL, ACCESS_CODE, tmp_path / "none.pem", local, "a.3mf", ftp_factory=fake())
    assert FakeFTP.instances == []


SECRET_ERRORS = [
    {"connect_error": ConnectionRefusedError(f"refused {ACCESS_CODE} {SERIAL}")},
    {"connect_error": ssl.SSLCertVerificationError(f"bad {SERIAL}")},
    {"login_error": ftplib.error_perm(f"530 bad password {ACCESS_CODE}")},
    {"stor_error": ftplib.error_perm(f"553 {ACCESS_CODE} {SERIAL}")},
    {"stor_error": OSError(f"broken {ACCESS_CODE}")},
    {"size_override": 1},
    {"cn": f"CN-{ACCESS_CODE}"},
]


@pytest.mark.parametrize("knobs", SECRET_ERRORS)
def test_access_code_never_in_errors_or_logs(fake, local, caplog, knobs):
    with caplog.at_level(logging.DEBUG), pytest.raises(UploadError) as info:
        run(local, fake(**knobs))
    texts = [str(info.value), repr(info.value), caplog.text]
    cause = info.value.__cause__ or info.value.__context__
    if cause is not None and not info.value.__suppress_context__:
        texts.append(str(cause))
    for text in texts:
        assert ACCESS_CODE not in text
        assert SERIAL not in text


def test_success_log_has_no_secrets(fake, local, caplog):
    with caplog.at_level(logging.DEBUG):
        run(local, fake())
    assert "cube.gcode.3mf" in caplog.text
    assert ACCESS_CODE not in caplog.text and SERIAL not in caplog.text


def test_one_upload_at_a_time(fake, local):
    active = []
    peak = []
    lock = threading.Lock()

    def hook():
        with lock:
            active.append(1)
            peak.append(len(active))
        time.sleep(0.05)
        with lock:
            active.pop()

    factory = fake(stor_hook=staticmethod(hook))
    threads = [threading.Thread(target=run, args=(local, factory)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(FakeFTP.instances) == 3
    assert max(peak) == 1


# --- ImplicitFTPS and the TLS context --------------------------------------------


def _serve(leaf: str, sock: socket.socket) -> threading.Thread:
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(TLS / f"{leaf}.pem", TLS / f"{leaf}.key")

    def serve():
        try:
            with server_ctx.wrap_socket(sock, server_side=True) as s:
                s.recv(1)
        except (OSError, ssl.SSLError):
            pass

    t = threading.Thread(target=serve)
    t.start()
    return t


def _wrap_control(leaf: str) -> tuple[bool, str | None]:
    ftp = ImplicitFTPS(context=build_ftps_context(CA, SERIAL), server_hostname=SERIAL, timeout=5)
    a, b = socket.socketpair()
    t = _serve(leaf, b)
    try:
        ftp.sock = a  # what ftplib.FTP.connect does: the setter wraps it
        return isinstance(ftp.sock, ssl.SSLSocket), ftp.sock.version()
    finally:
        if ftp.sock is not None:
            ftp.sock.close()
        a.close()
        b.close()
        t.join(5)


def test_control_socket_is_wrapped_and_verified_at_connect():
    wrapped, version = _wrap_control("printer")
    assert wrapped
    assert version == "TLSv1.2"


def test_control_handshake_rejects_other_cn():
    with pytest.raises(ssl.SSLCertVerificationError):
        _wrap_control("other")


def test_ftps_context_reuses_link_tls_and_caps_tls12():
    ctx = build_ftps_context(CA, SERIAL)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is False
    assert ctx.maximum_version == ssl.TLSVersion.TLSv1_2


def test_none_socket_is_not_wrapped():
    ftp = ImplicitFTPS(context=build_ftps_context(CA, SERIAL), server_hostname=SERIAL)
    ftp.sock = None
    assert ftp.sock is None


def test_data_socket_reuses_the_control_session(monkeypatch):
    class RecordingContext:
        def __init__(self):
            self.calls = []

        def wrap_socket(self, sock, **kwargs):
            self.calls.append((sock, kwargs))
            return sock

    class Control:
        session = object()

    ctx = RecordingContext()
    ftp = ImplicitFTPS(context=ctx, server_hostname=SERIAL)
    ftp._sock = Control()
    ftp._prot_p = True
    raw = object()
    monkeypatch.setattr(ftplib.FTP, "ntransfercmd", lambda self, cmd, rest=None: (raw, 42))
    conn, size = ftp.ntransfercmd("STOR /x")
    assert conn is raw and size == 42
    ((sock, kwargs),) = ctx.calls
    assert sock is raw
    assert kwargs == {"server_hostname": SERIAL, "session": Control.session}


def test_module_exports():
    assert ftps_module.FTPS_PORT == 990
    assert ftps_module.FTPS_USER == "bblp"
