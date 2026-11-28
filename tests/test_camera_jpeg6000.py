"""The A1-style camera: TLS to port 6000, an 80-byte auth packet, length-prefixed JPEG frames."""

import logging
import socket
import ssl
import struct
import threading
from pathlib import Path

import pytest

from bambu_mcp import camera
from bambu_mcp.config import Secrets, Settings

TLS = Path(__file__).parent / "fixtures" / "synthetic" / "tls"
CODE = "87654321"
SERIAL = "TESTSERIAL0001"
JPEG = b"\xff\xd8\xff\xe0tiny-jpeg\xff\xd9"


class FakeCamera:
    """One-connection TLS server: reads the auth packet, sends one frame."""

    def __init__(self, cert: str = "printer", frame: bytes = JPEG, length: int | None = None) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(TLS / f"{cert}.pem", TLS / f"{cert}.key")
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.auth: bytes | None = None
        self.frame = frame
        self.length = len(frame) if length is None else length
        self.thread = threading.Thread(target=self._serve, args=(ctx,), daemon=True)
        self.thread.start()

    def _serve(self, ctx: ssl.SSLContext) -> None:
        conn, _ = self.sock.accept()
        try:
            with ctx.wrap_socket(conn, server_side=True) as tls:
                data = b""
                while len(data) < 80:
                    chunk = tls.recv(80 - len(data))
                    if not chunk:
                        return
                    data += chunk
                self.auth = data
                tls.sendall(struct.pack("<IIII", self.length, 0, 0, 0) + self.frame)
        except (ssl.SSLError, OSError):
            pass
        finally:
            self.sock.close()


@pytest.fixture
def secrets():
    return Secrets(access_code=CODE, serial=SERIAL, host="127.0.0.1")


def _settings() -> Settings:
    return Settings(tls_mode="ca", ca_file=TLS / "ca.pem", printer="a1", camera_backend="jpeg_6000")


async def test_jpeg6000_reads_one_frame(monkeypatch, secrets):
    server = FakeCamera()
    monkeypatch.setattr(camera, "JPEG_PORT", server.port)
    data = await camera.snapshot(secrets, _settings(), timeout_s=5)
    assert data == JPEG
    server.thread.join(2)
    assert server.auth == (
        struct.pack("<IIII", 0x40, 0x3000, 0, 0) + b"bblp".ljust(32, b"\0") + CODE.encode().ljust(32, b"\0")
    )


async def test_jpeg6000_rejects_wrong_cn(monkeypatch, secrets):
    server = FakeCamera(cert="other")
    monkeypatch.setattr(camera, "JPEG_PORT", server.port)
    assert await camera.snapshot(secrets, _settings(), timeout_s=5) is None
    server.thread.join(2)
    assert server.auth is None  # the access code never left before the peer was verified


async def test_jpeg6000_oversize_frame_is_none(monkeypatch, secrets):
    server = FakeCamera(length=camera.JPEG_MAX_BYTES + 1)
    monkeypatch.setattr(camera, "JPEG_PORT", server.port)
    assert await camera.snapshot(secrets, _settings(), timeout_s=5) is None


async def test_jpeg6000_not_a_jpeg_is_none(monkeypatch, secrets):
    server = FakeCamera(frame=b"not a jpeg at all")
    monkeypatch.setattr(camera, "JPEG_PORT", server.port)
    assert await camera.snapshot(secrets, _settings(), timeout_s=5) is None


async def test_jpeg6000_access_code_not_logged(monkeypatch, secrets, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(camera, "JPEG_PORT", 1)  # nothing listens: the connect fails and is logged
    assert await camera.snapshot(secrets, _settings(), timeout_s=3) is None
    assert "camera snapshot failed" in caplog.text
    assert CODE not in caplog.text
