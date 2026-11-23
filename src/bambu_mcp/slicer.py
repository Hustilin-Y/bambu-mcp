"""Clients for slicing on the owner's PC: wake it through orchestra-gateway, then talk to slice-agent.

slice-agent API (``slice_agent.app``): ``POST /slice`` (multipart ``file`` + ``job``),
``GET /sliced/<file>/plate/<n>.png`` and ``GET /health``, bearer token on all but /health.
Outcome details are safe to show to the owner: they never carry a token or a server path
(the agent redacts its own paths before answering).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

import httpx

log = logging.getLogger(__name__)

WAKE_BUDGET_S = 120.0
POLL_S = 5.0
OUT_SUFFIX = ".gcode.3mf"
DETAIL_MAX = 200
MAX_PNG_BYTES = 5 * 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
UNEXPECTED = "slice-agent answered with an unexpected result"
# A Studio slice may take 300 s, plus the upload and the pc-agent lease.
SLICE_TIMEOUT = httpx.Timeout(connect=10.0, read=420.0, write=120.0, pool=10.0)
HEALTH_TIMEOUT = httpx.Timeout(3.0)
WAKE_TIMEOUT = httpx.Timeout(10.0)
PNG_TIMEOUT = httpx.Timeout(30.0)
_SLICED_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}" + re.escape(OUT_SUFFIX))

Probe = Callable[[], Awaitable[bool]]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class GatewayWaker:
    """Wakes the PC through orchestra-gateway and waits until slice-agent answers.

    ``ready`` is the readiness probe (``SliceClient.health``). Without a gateway URL or
    key the PC can only be found already awake.
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
            return False
        try:
            resp = await self._http.post(f"{self._url}/wake", headers=_bearer(self._key), timeout=WAKE_TIMEOUT)
        except httpx.HTTPError as exc:
            log.warning("gateway /wake failed: %s", type(exc).__name__)
            return False
        if resp.status_code >= 400:
            log.warning("gateway /wake refused: HTTP %d", resp.status_code)
            return False
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

    ``busy``: another slice runs on the PC. ``failed``: the agent refused or the slice
    failed, ``detail`` says why. ``unreachable``: no answer (``detail`` is the error type).
    """

    status: Literal["ok", "busy", "failed", "unreachable"]
    file: str | None = None
    plates: tuple[dict, ...] = ()
    slicer_version: str | None = None
    detail: str = ""


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


def _error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str) and detail.strip():
        return _cap(detail)
    return f"slice-agent answered HTTP {resp.status_code}"


class SliceClient:
    def __init__(self, url: str, token: str, http: httpx.AsyncClient) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._http = http

    async def health(self) -> bool:
        try:
            resp = await self._http.get(f"{self._url}/health", timeout=HEALTH_TIMEOUT)
            return resp.status_code == 200 and resp.json() == {"ok": True}
        except (httpx.HTTPError, ValueError):
            return False

    async def slice(self, job: SliceRequest, file_bytes: bytes) -> SliceOutcome:
        files = {
            "job": ("job.json", job.to_json(), "application/json"),
            "file": ("job.3mf", file_bytes, "application/octet-stream"),
        }
        try:
            resp = await self._http.post(
                f"{self._url}/slice", files=files, headers=_bearer(self._token), timeout=SLICE_TIMEOUT
            )
        except httpx.HTTPError as exc:
            log.warning("slice-agent /slice failed: %s", type(exc).__name__)
            return SliceOutcome("unreachable", detail=type(exc).__name__)
        status = resp.status_code
        if status == 409:
            return SliceOutcome("busy")
        if status == 401:
            return SliceOutcome("failed", detail="slice-agent refused the token")
        if status != 200:
            return SliceOutcome("failed", detail=_error_detail(resp))
        return self._ok(job, resp)

    @staticmethod
    def _ok(job: SliceRequest, resp: httpx.Response) -> SliceOutcome:
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or body.get("file") != job.out_file:
            log.warning("slice-agent answered 200 without the expected file")
            return SliceOutcome("failed", detail=UNEXPECTED)
        plates = _plates(body.get("plates"), job.plates)
        if plates is None:
            log.warning("slice-agent answered 200 with unusable plates")
            return SliceOutcome("failed", detail=UNEXPECTED)
        version = body.get("slicer_version")
        return SliceOutcome(
            "ok", file=job.out_file, plates=plates, slicer_version=version if isinstance(version, str) else None
        )

    async def fetch_png(self, name: str, plate: int) -> bytes | None:
        """The plate preview inside a sliced file, or None when there is none."""
        if not _SLICED_RE.fullmatch(name) or isinstance(plate, bool) or not isinstance(plate, int) or plate < 1:
            return None
        try:
            resp = await self._http.get(
                f"{self._url}/sliced/{name}/plate/{plate}.png", headers=_bearer(self._token), timeout=PNG_TIMEOUT
            )
        except httpx.HTTPError as exc:
            log.warning("slice-agent plate picture failed: %s", type(exc).__name__)
            return None
        data = resp.content
        if resp.status_code != 200 or not data.startswith(PNG_MAGIC) or len(data) > MAX_PNG_BYTES:
            return None
        return data
