"""Clients for slicing on the owner's PC: wake it through orchestra-gateway, then talk to slice-agent.

slice-agent API (``slice_agent.app``): ``POST /slice`` (multipart ``file`` + ``job``),
``GET /sliced/<file>``, ``GET /sliced/<file>/plate/<n>.png`` and ``GET /health``, bearer token on all but /health.
Outcome details are safe to show to the owner: they never carry a token or a server path
(the agent redacts its own paths before answering).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx

log = logging.getLogger(__name__)

WAKE_BUDGET_S = 120.0
POLL_S = 5.0
OUT_SUFFIX = ".gcode.3mf"
DETAIL_MAX = 200
MAX_JSON_BYTES = 256 * 1024
MAX_PNG_BYTES = 5 * 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
UNEXPECTED = "slice-agent answered with an unexpected result"
OVERSIZED = "slice-agent answered with an oversized body"
# A Studio slice may take 300 s, plus the upload and the pc-agent lease.
SLICE_TIMEOUT = httpx.Timeout(connect=10.0, read=420.0, write=120.0, pool=10.0)
HEALTH_TIMEOUT = httpx.Timeout(3.0)
WAKE_TIMEOUT = httpx.Timeout(10.0)
PNG_TIMEOUT = httpx.Timeout(30.0)
FILE_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
MAX_FILE_BYTES = 50 * 2**20
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SLICED_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}" + re.escape(OUT_SUFFIX))
# A path left in an agent detail: a drive letter, or two or more separated segments.
# Placeholders such as <sliced> are not segments, so they survive.
_SEP = r"[\\/]"
_SEGMENT = r"""[^\s\\/<>"',;]+"""
_PATH_RE = re.compile(
    rf"""[A-Za-z]:{_SEP}[^\s<>"',;]*|{_SEP}*{_SEGMENT}(?:{_SEP}+{_SEGMENT})+{_SEP}*"""
)
PATH_PLACEHOLDER = "<path>"

Probe = Callable[[], Awaitable[bool]]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _TooLarge(Exception):
    pass


async def _read_capped(resp: httpx.Response, cap: int) -> bytes:
    """The body of a streamed response, read only up to ``cap`` bytes (``_TooLarge`` past it)."""
    declared = resp.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap:
        raise _TooLarge
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        buf += chunk
        if len(buf) > cap:
            raise _TooLarge
    return bytes(buf)


def _json(data: bytes) -> object:
    try:
        return json.loads(data)
    except (ValueError, RecursionError):
        return None


class FetchError(Exception):
    """A sliced file could not be fetched. The message is English, safe to show: no token, no URL."""


class WakeError(Exception):
    """The PC could not be asked to wake. ``kind``: ``refused``, ``unreachable`` or ``not_configured``.

    The message is English, safe to store and show: no key, no URL.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class GatewayWaker:
    """Wakes the PC through orchestra-gateway and waits until slice-agent answers.

    ``ready`` is the readiness probe (``SliceClient.health``). ``wake_and_wait`` is True
    when slice-agent answers, False when the budget ran out, and raises ``WakeError`` at
    once when the gateway refuses, does not answer or is not configured.
    """

    def __init__(
        self,
        url: str | None,
        key: str | None,
        http: httpx.AsyncClient,
        *,
        ready: Probe,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_s: float = POLL_S,
    ) -> None:
        self._url = url.rstrip("/") if url else None
        self._key = key
        self._http = http
        self._ready = ready
        self._clock = clock
        self._sleep = sleep
        self._poll_s = poll_s

    async def wake_and_wait(self, budget_s: float = WAKE_BUDGET_S) -> bool:
        if await self._ready():
            return True
        if not self._url or not self._key:
            log.warning("slice-agent is down and no gateway is configured to wake the PC")
            raise WakeError("not_configured", "the PC is down and no gateway is configured to wake it")
        try:
            # Only the status matters: the body is never read.
            async with self._http.stream(
                "POST", f"{self._url}/wake", headers=_bearer(self._key), timeout=WAKE_TIMEOUT
            ) as resp:
                pass
        except httpx.HTTPError as exc:
            log.warning("gateway /wake failed: %s", type(exc).__name__)
            raise WakeError(
                "unreachable", f"the gateway did not answer the wake request ({type(exc).__name__})"
            ) from None
        if resp.status_code >= 400:
            log.warning("gateway /wake refused: HTTP %d", resp.status_code)
            raise WakeError("refused", f"the gateway refused the wake request (HTTP {resp.status_code})")
        deadline = self._clock() + budget_s
        while (left := deadline - self._clock()) > 0:
            await self._sleep(min(self._poll_s, left))
            if await self._ready():
                return True
        log.warning("the PC did not wake within %.0f s", budget_s)
        return False


@dataclass(frozen=True)
class SliceRequest:
    """The ``job`` part of ``POST /slice`` (``slice_agent.app.parse_job``)."""

    job_id: int
    name: str  # names.slug of the manifest name
    kit: bool
    plates: tuple[int, ...]
    nozzle: str
    process_set: str
    filament: str
    plate_type: str
    overrides: Mapping[str, object] = field(default_factory=dict)

    @property
    def out_file(self) -> str:
        """The file the agent writes: ``slice_agent.app.Job.out_name`` plus the suffix."""
        return f"{self.name}-{self.job_id}-{'kit' if self.kit else 'p1'}{OUT_SUFFIX}"

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "job_id": self.job_id,
                "name": self.name,
                "kit": self.kit,
                "plates": list(self.plates),
                "nozzle": self.nozzle,
                "process_set": self.process_set,
                "filament": self.filament,
                "plate_type": self.plate_type,
                "overrides": dict(self.overrides),
            }
        ).encode("utf-8")


@dataclass(frozen=True)
class SliceOutcome:
    """``ok``: the file and its plates (``{"index", "minutes", "grams": {filament: g}}``).

    ``sha256``: of the sliced file, None from an agent older than phase 3 (such a job is
    sliced again before it is printed).
    ``busy``: another slice runs on the PC. ``failed``: the agent refused or the slice
    failed, ``detail`` says why. ``unreachable``: no answer (``detail`` is the error type).
    """

    status: Literal["ok", "busy", "failed", "unreachable"]
    file: str | None = None
    plates: tuple[dict, ...] = ()
    slicer_version: str | None = None
    detail: str = ""
    sha256: str | None = None


def _cap(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= DETAIL_MAX else text[: DETAIL_MAX - 3] + "..."


def _grams(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def _plates(raw: object, wanted: tuple[int, ...]) -> tuple[dict, ...] | None:
    """The agent's plates, checked: exactly the plates asked for, sane numbers."""
    if not isinstance(raw, list):
        return None
    out = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        index, minutes, grams = item.get("index"), _grams(item.get("minutes")), item.get("grams")
        if isinstance(index, bool) or not isinstance(index, int) or minutes is None or not isinstance(grams, dict):
            return None
        by_filament = {}
        for key, value in grams.items():
            g = _grams(value)
            if not isinstance(key, str) or g is None:
                return None
            by_filament[key] = g
        out.append({"index": index, "minutes": minutes, "grams": by_filament})
    if sorted(p["index"] for p in out) != sorted(wanted):
        return None
    return tuple(out)


def _error_detail(status: int, body: object) -> str:
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str) and detail.strip():
        return _cap(_PATH_RE.sub(PATH_PLACEHOLDER, detail))
    return f"slice-agent answered HTTP {status}"


class SliceClient:
    """Every answer body is read with a cap: JSON 256 KB, a PNG 5 MB."""

    def __init__(self, url: str, token: str, http: httpx.AsyncClient) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._http = http

    async def health(self) -> bool:
        try:
            async with self._http.stream("GET", f"{self._url}/health", timeout=HEALTH_TIMEOUT) as resp:
                if resp.status_code != 200:
                    return False
                return _json(await _read_capped(resp, MAX_JSON_BYTES)) == {"ok": True}
        except (httpx.HTTPError, _TooLarge):
            return False

    async def slice(self, job: SliceRequest, file_bytes: bytes) -> SliceOutcome:
        files = {
            "job": ("job.json", job.to_json(), "application/json"),
            "file": ("job.3mf", file_bytes, "application/octet-stream"),
        }
        try:
            async with self._http.stream(
                "POST", f"{self._url}/slice", files=files, headers=_bearer(self._token), timeout=SLICE_TIMEOUT
            ) as resp:
                status = resp.status_code
                if status in (401, 409):
                    data = b""  # the answer is in the status
                else:
                    data = await _read_capped(resp, MAX_JSON_BYTES)
        except _TooLarge:
            log.warning("slice-agent answered with a body over %d bytes", MAX_JSON_BYTES)
            return SliceOutcome("failed", detail=OVERSIZED)
        except httpx.HTTPError as exc:
            log.warning("slice-agent /slice failed: %s", type(exc).__name__)
            return SliceOutcome("unreachable", detail=type(exc).__name__)
        if status == 409:
            return SliceOutcome("busy")
        if status == 401:
            return SliceOutcome("failed", detail="slice-agent refused the token")
        if status != 200:
            return SliceOutcome("failed", detail=_error_detail(status, _json(data)))
        return self._ok(job, _json(data))

    @staticmethod
    def _ok(job: SliceRequest, body: object) -> SliceOutcome:
        if not isinstance(body, dict) or body.get("file") != job.out_file:
            log.warning("slice-agent answered 200 without the expected file")
            return SliceOutcome("failed", detail=UNEXPECTED)
        plates = _plates(body.get("plates"), job.plates)
        if plates is None:
            log.warning("slice-agent answered 200 with unusable plates")
            return SliceOutcome("failed", detail=UNEXPECTED)
        sha256 = body.get("sha256")
        if sha256 is not None and not (isinstance(sha256, str) and _SHA256_RE.fullmatch(sha256)):
            log.warning("slice-agent answered 200 with a malformed sha256")
            return SliceOutcome("failed", detail=UNEXPECTED)
        version = body.get("slicer_version")
        return SliceOutcome(
            "ok",
            file=job.out_file,
            plates=plates,
            slicer_version=version if isinstance(version, str) else None,
            sha256=sha256,
        )

    async def fetch_png(self, name: str, plate: int) -> bytes | None:
        """The plate preview inside a sliced file, or None when there is none (or it is over 5 MB)."""
        if not _SLICED_RE.fullmatch(name) or isinstance(plate, bool) or not isinstance(plate, int) or plate < 1:
            return None
        try:
            async with self._http.stream(
                "GET", f"{self._url}/sliced/{name}/plate/{plate}.png", headers=_bearer(self._token),
                timeout=PNG_TIMEOUT,
            ) as resp:
                if resp.status_code != 200:
                    return None
                data = await _read_capped(resp, MAX_PNG_BYTES)
        except _TooLarge:
            log.warning("slice-agent plate picture is over %d bytes, sending the text alone", MAX_PNG_BYTES)
            return None
        except httpx.HTTPError as exc:
            log.warning("slice-agent plate picture failed: %s", type(exc).__name__)
            return None
        return data if data.startswith(PNG_MAGIC) else None

    async def fetch_file(self, name: str, dest: Path, max_bytes: int = MAX_FILE_BYTES) -> str:
        """Stream the sliced file ``name`` to ``dest`` and return its sha256 (lower-case hex).

        Never holds the whole body: chunks go to a temporary file next to ``dest`` (its own
        name per fetch, so two fetches of one file never mix), renamed over it once complete.
        ``FetchError`` past ``max_bytes`` or on any failure, and then nothing of this fetch is
        left beside ``dest``.
        """
        if not _SLICED_RE.fullmatch(name):
            raise FetchError("not a sliced file name")
        tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(8)}.part")
        digest = hashlib.sha256()
        try:
            async with self._http.stream(
                "GET", f"{self._url}/sliced/{name}", headers=_bearer(self._token), timeout=FILE_TIMEOUT
            ) as resp:
                if resp.status_code != 200:
                    raise FetchError(f"slice-agent answered HTTP {resp.status_code} for the sliced file")
                declared = resp.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise FetchError(f"the sliced file is over {max_bytes} bytes")
                size = 0
                with tmp.open("wb") as f:
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise FetchError(f"the sliced file is over {max_bytes} bytes")
                        digest.update(chunk)
                        f.write(chunk)
            os.replace(tmp, dest)
        except httpx.HTTPError as exc:
            log.warning("slice-agent sliced file fetch failed: %s", type(exc).__name__)
            raise FetchError(f"the sliced file could not be fetched ({type(exc).__name__})") from None
        except OSError as exc:
            log.warning("writing the sliced file failed: %s", type(exc).__name__)
            raise FetchError(f"the sliced file could not be written ({type(exc).__name__})") from None
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
        return digest.hexdigest()
