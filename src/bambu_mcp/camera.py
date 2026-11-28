"""One JPEG frame from the printer camera, for Telegram notifications.

Two backends, picked by ``settings.camera_backend`` (default from the printer profile):
``pyav``, RTSPS on port 322 (phase 0 verified it against the real P2S: 1920x1080 in
2-3 s, about +20 MB RSS), and ``jpeg_6000``, the A1 and P1 series JPEG stream on port
6000 behind the MQTT link's TLS check (unverified on any printer we have). The RTSPS URL embeds the access code, so it exists only
inside this module: it is passed to the worker thread as an argument, never put
in an argv (no ffmpeg subprocess) and never in an exception or log line that
leaves this module. Failures return None after a redacted warning.

TLS: ``tls_verify=0`` is accepted for phase 1. The stream is RTSPS on the LAN
and verifying it against the Bambu CA through ffmpeg is out of scope.

PyAV is blocking, so it runs in a worker thread under ``asyncio.wait_for``. A
timed-out worker cannot be cancelled and may keep running until PyAV's own
timeout fires. Until it ends the camera counts as busy, so grabs never stack. It cannot leak the URL: it catches and redacts everything itself
and only returns plain data, so nothing raised there reaches a caller or the
event loop's exception handler.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import socket
import struct
import threading
from collections.abc import Callable

from bambu_mcp.config import Secrets, Settings
from bambu_mcp.printer.link import build_tls_context
from bambu_mcp.redact import redact

log = logging.getLogger(__name__)

_JPEG_QUALITY = 85
_OPEN_OPTIONS = {"rtsp_transport": "tcp", "tls_verify": "0", "timeout": "10000000"}  # us
_OPEN_TIMEOUT_S = 15


def _grab_jpeg(url: str) -> bytes | None:
    """Blocking: open the stream, decode the first video frame, encode it as JPEG.

    Returns None if the stream yields no frame. Tests replace this function.
    """
    import av  # imported lazily: heavy, and tests never need it

    with av.open(url, options=_OPEN_OPTIONS, timeout=_OPEN_TIMEOUT_S) as container:
        for frame in container.decode(video=0):
            buf = io.BytesIO()
            frame.to_image().save(buf, format="JPEG", quality=_JPEG_QUALITY)
            return buf.getvalue()
    return None


# The A1 and P1 series camera (ha-bambulab's chamber_image): TLS to port 6000, an 80-byte
# auth packet, then frames each behind a 16-byte header whose first 4 bytes are the
# little-endian JPEG length. Unverified on any printer we have.
JPEG_PORT = 6000
JPEG_MAX_BYTES = 4 * 1024 * 1024
_JPEG_HEADER = 16
_AUTH_HEAD = struct.pack("<IIII", 0x40, 0x3000, 0, 0)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = bytearray()
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError(f"camera closed the stream after {len(data)} of {n} bytes")
        data += chunk
    return bytes(data)


def _grab_jpeg6000(secrets: Secrets, settings: Settings, timeout_s: float) -> bytes | None:
    """Blocking: one JPEG frame from the port 6000 stream, None when it is not a JPEG.

    The TLS context is the MQTT link's: the same CA and CN (or pin) check runs inside the
    handshake, so the access code is sent only to the verified printer.
    """
    context = build_tls_context(settings, serial=secrets.serial)
    with socket.create_connection((secrets.host, JPEG_PORT), timeout=timeout_s) as raw:
        with context.wrap_socket(raw, server_hostname=None) as tls:
            tls.sendall(
                _AUTH_HEAD + b"bblp".ljust(32, b"\0") + secrets.access_code.encode("ascii").ljust(32, b"\0")
            )
            length = struct.unpack("<I", _recv_exact(tls, _JPEG_HEADER)[:4])[0]
            if not 0 < length <= JPEG_MAX_BYTES:
                raise ValueError(f"camera frame of {length} bytes is outside 1..{JPEG_MAX_BYTES}")
            frame = _recv_exact(tls, length)
    if not (frame.startswith(b"\xff\xd8") and frame.endswith(b"\xff\xd9")):
        return None
    return frame


class CameraBusy(Exception):
    """Another snapshot is still running (``wait=False`` callers only)."""


# Held from the start of a grab until its worker thread returns, even after the
# caller timed out: a hung PyAV decoder (~20 MB) is never joined by a second one.
# A threading lock, not an asyncio one: the worker thread releases it.
_busy = threading.Lock()
_BUSY_POLL_S = 0.1


def _worker(grab: Callable[[], bytes | None], secrets: tuple[str, ...]) -> tuple[bytes | None, str | None]:
    """Run ``grab`` and turn any failure into redacted text (never raises)."""
    try:
        return grab(), None
    except Exception as exc:  # noqa: BLE001
        return None, redact(f"{type(exc).__name__}: {exc}", secrets)


def _start_grab(grab: Callable[[], bytes | None], mask: tuple[str, ...]) -> asyncio.Future:
    """Run the worker in its own daemon thread; it releases ``_busy`` when it ends.

    The caller holds ``_busy``. Once ``start()`` returns, the release is guaranteed
    whatever happens to the awaiting task.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def deliver(result: tuple[bytes | None, str | None]) -> None:
        if not future.done():
            future.set_result(result)

    def run() -> None:
        try:
            result = _worker(grab, mask)
        finally:
            _busy.release()
        with contextlib.suppress(RuntimeError):  # loop closed after a timeout
            loop.call_soon_threadsafe(deliver, result)

    threading.Thread(target=run, name="bambu-camera", daemon=True).start()
    return future


async def _acquire(wait: bool, deadline: float) -> bool:
    loop = asyncio.get_running_loop()
    while not _busy.acquire(blocking=False):
        if not wait:
            raise CameraBusy
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(_BUSY_POLL_S)
    return True


async def snapshot(
    secrets: Secrets, settings: Settings, timeout_s: float = 15, *, wait: bool = True
) -> bytes | None:
    """Return one JPEG frame from the camera, or None on failure or timeout.

    Only one grab runs at a time. While one is running, ``wait=True`` (the
    service) waits for it within ``timeout_s``, ``wait=False`` (the MCP tool)
    raises ``CameraBusy`` at once.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    if not await _acquire(wait, deadline):
        log.warning("camera snapshot skipped: camera busy for %.1f s", timeout_s)
        return None
    if settings.camera_backend == "jpeg_6000":
        mask: tuple[str, ...] = (secrets.access_code,)
        budget = max(deadline - loop.time(), 1.0)

        def grab_fn() -> bytes | None:
            return _grab_jpeg6000(secrets, settings, budget)
    else:
        url = f"rtsps://bblp:{secrets.access_code}@{secrets.host}:322/streaming/live/1"
        mask = (secrets.access_code, url)

        def grab_fn() -> bytes | None:
            return _grab_jpeg(url)

    try:
        grab = _start_grab(grab_fn, mask)
    except BaseException:
        _busy.release()
        raise
    try:
        data, error = await asyncio.wait_for(grab, timeout=max(deadline - loop.time(), 0))
    except TimeoutError:
        log.warning("camera snapshot timed out after %.1f s", timeout_s)
        return None
    if error is not None:
        log.warning("camera snapshot failed: %s", error)
        return None
    if not data:
        log.warning("camera snapshot failed: no video frame")
        return None
    return data
