"""pc-agent lease held for the whole slice, so the PC does not go to sleep under the slicer.

pc-agent API (http://127.0.0.1:11435, bearer PC_AGENT_TOKEN):
POST /lease {"holder", "ttl_s"} -> 201 {"id", ...}; POST /lease/{id}/renew {"ttl_s"}; DELETE /lease/{id} -> 204.
A pc-agent error never blocks slicing: it is logged and the lease is retried on the next tick.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx

PC_AGENT_URL = "http://127.0.0.1:11435"
HOLDER = "slice-agent"
TTL_S = 600
RENEW_S = 120
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")

log = logging.getLogger(__name__)


class Lease:
    """Async context manager: take the lease, renew it every `renew_s`, drop it on exit."""

    def __init__(
        self, http: httpx.AsyncClient, token: str, *, holder: str = HOLDER, ttl_s: int = TTL_S,
        renew_s: float = RENEW_S,
    ) -> None:
        self._http = http
        self._headers = {"Authorization": f"Bearer {token}"}
        self._holder, self._ttl_s, self._renew_s = holder, ttl_s, renew_s
        self.id: str | None = None
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> Lease:
        await self._take()
        self._task = asyncio.create_task(self._keep())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                log.warning("pc-agent lease renewal stopped with an error")
        await self._drop()

    async def _call(self, method: str, path: str, body: dict | None = None) -> httpx.Response | None:
        try:
            r = await self._http.request(method, path, json=body, headers=self._headers)
        except Exception as e:  # noqa: BLE001 - a pc-agent problem must never stop a slice
            log.warning("pc-agent lease %s %s failed: %s", method, path, type(e).__name__)
            return None
        if r.is_error:
            log.warning("pc-agent lease %s %s returned HTTP %s", method, path, r.status_code)
        return r

    async def _take(self) -> None:
        r = await self._call("POST", "/lease", {"holder": self._holder, "ttl_s": self._ttl_s})
        if r is None or r.is_error:
            return
        try:
            lease_id = r.json().get("id")
        except (ValueError, AttributeError, RecursionError):
            lease_id = None
        if isinstance(lease_id, str) and _ID_RE.fullmatch(lease_id):
            self.id = lease_id
            log.info("pc-agent lease taken")
        else:
            log.warning("pc-agent lease: unusable id in the response, slicing without a lease")

    async def _keep(self) -> None:
        while True:
            await asyncio.sleep(self._renew_s)
            if self.id is None:
                await self._take()
                continue
            r = await self._call("POST", f"/lease/{self.id}/renew", {"ttl_s": self._ttl_s})
            if r is not None and r.status_code == 404:
                self.id = None  # expired or pc-agent restarted: take a new one on the next tick

    async def _drop(self) -> None:
        if self.id is None:
            return
        lease_id, self.id = self.id, None
        await self._call("DELETE", f"/lease/{lease_id}")
