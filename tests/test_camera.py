import asyncio
import logging
import threading

import pytest

from bambu_mcp import camera
from bambu_mcp.config import Secrets, Settings

CODE = "12345678"


@pytest.fixture
def secrets():
    return Secrets(access_code=CODE, serial="SER1", host="10.0.0.2")


@pytest.fixture
def settings():
    return Settings()


async def test_snapshot_returns_jpeg_bytes(monkeypatch, secrets, settings):
    seen = []

    def fake_grab(url):
        seen.append(url)
        return b"\xff\xd8\xff fake jpeg"

    monkeypatch.setattr(camera, "_grab_jpeg", fake_grab)
    data = await camera.snapshot(secrets, settings)
    assert data == b"\xff\xd8\xff fake jpeg"
    assert seen == [f"rtsps://bblp:{CODE}@10.0.0.2:322/streaming/live/1"]


async def test_timeout_returns_none(monkeypatch, secrets, settings, caplog):
    release = threading.Event()

    def slow_grab(url):
        release.wait(5)
        return b"late"

    monkeypatch.setattr(camera, "_grab_jpeg", slow_grab)
    try:
        with caplog.at_level(logging.WARNING, logger="bambu_mcp.camera"):
            assert await camera.snapshot(secrets, settings, timeout_s=0.1) is None
    finally:
        release.set()
    assert "timed out" in caplog.text
    assert CODE not in caplog.text


async def test_error_with_url_is_redacted(monkeypatch, secrets, settings, caplog):
    def failing_grab(url):
        raise OSError(f"cannot open rtsps://bblp:{CODE}@10.0.0.2:322/streaming/live/1")

    monkeypatch.setattr(camera, "_grab_jpeg", failing_grab)
    with caplog.at_level(logging.WARNING, logger="bambu_mcp.camera"):
        assert await camera.snapshot(secrets, settings) is None
    assert "***" in caplog.text
    assert CODE not in caplog.text
    for record in caplog.records:
        assert CODE not in record.getMessage()
        assert record.exc_info is None  # traceback would carry the raw URL


async def test_no_frame_returns_none(monkeypatch, secrets, settings, caplog):
    monkeypatch.setattr(camera, "_grab_jpeg", lambda url: None)
    with caplog.at_level(logging.WARNING, logger="bambu_mcp.camera"):
        assert await camera.snapshot(secrets, settings) is None
    assert "no video frame" in caplog.text


async def test_concurrent_snapshots_run_one_grab(monkeypatch, secrets, settings):
    started = threading.Event()
    release = threading.Event()
    grabs = []

    def blocking_grab(url):
        grabs.append(url)
        started.set()
        release.wait(5)
        return b"jpeg"

    monkeypatch.setattr(camera, "_grab_jpeg", blocking_grab)
    first = asyncio.ensure_future(camera.snapshot(secrets, settings))
    await asyncio.to_thread(started.wait, 5)
    with pytest.raises(camera.CameraBusy):  # the MCP tool: fail fast
        await camera.snapshot(secrets, settings, wait=False)
    waiting = asyncio.ensure_future(camera.snapshot(secrets, settings))  # the service: wait
    await asyncio.sleep(0.2)
    assert len(grabs) == 1
    release.set()
    assert await first == b"jpeg"
    assert await waiting == b"jpeg"
    assert len(grabs) == 2


async def test_timed_out_grab_keeps_camera_busy(monkeypatch, secrets, settings, caplog):
    release = threading.Event()
    grabs = []

    def hung_grab(url):
        grabs.append(url)
        release.wait(5)
        return b"late"

    monkeypatch.setattr(camera, "_grab_jpeg", hung_grab)
    try:
        assert await camera.snapshot(secrets, settings, timeout_s=0.1) is None
        with pytest.raises(camera.CameraBusy):
            await camera.snapshot(secrets, settings, wait=False)
        with caplog.at_level(logging.WARNING, logger="bambu_mcp.camera"):
            assert await camera.snapshot(secrets, settings, timeout_s=0.2) is None
        assert "busy" in caplog.text
        assert len(grabs) == 1
    finally:
        release.set()
    monkeypatch.setattr(camera, "_grab_jpeg", lambda url: b"ok")
    assert await camera.snapshot(secrets, settings) == b"ok"  # freed once the thread ends
