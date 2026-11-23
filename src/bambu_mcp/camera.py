"""One JPEG frame from the printer camera, for Telegram notifications.

PyAV only (phase 0 verified it against the real P2S: 1920x1080 in 2-3 s,
about +20 MB RSS). The RTSPS URL embeds the access code, so it exists only
inside this module: it is passed to the worker thread as an argument, never put
in an argv (no ffmpeg subprocess) and never in an exception or log line that
leaves this module. Failures return None after a redacted warning.

TLS: ``tls_verify=0`` is accepted for phase 1. The stream is RTSPS on the LAN
and verifying it against the Bambu CA through ffmpeg is out of scope.

PyAV is blocking, so it runs in a worker thread under ``asyncio.wait_for``. A
timed-out worker cannot be cancelled and may keep running until PyAV's own
timeout fires. It cannot leak the URL: it catches and redacts everything itself
and only returns plain data, so nothing raised there reaches a caller or the
event loop's exception handler.
"""

from __future__ import annotations

import asyncio
import io
import logging

from bambu_mcp.config import Secrets, Settings
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


def _worker(url: str, secrets: tuple[str, ...]) -> tuple[bytes | None, str | None]:
    """Run ``_grab_jpeg`` and turn any failure into redacted text (never raises)."""
    try:
        return _grab_jpeg(url), None
    except Exception as exc:  # noqa: BLE001
        return None, redact(f"{type(exc).__name__}: {exc}", secrets)


async def snapshot(secrets: Secrets, settings: Settings, timeout_s: float = 15) -> bytes | None:
    """Return one JPEG frame from the camera, or None on failure or timeout."""
    del settings  # only the "pyav" backend exists, nothing to select
    url = f"rtsps://bblp:{secrets.access_code}@{secrets.host}:322/streaming/live/1"
    mask = (secrets.access_code, url)
    try:
        data, error = await asyncio.wait_for(
            asyncio.to_thread(_worker, url, mask), timeout=timeout_s
        )
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
