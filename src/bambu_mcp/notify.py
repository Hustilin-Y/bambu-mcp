"""Telegram notifications through the multiclaude service-notify add-in."""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from bambu_mcp.redact import redact

log = logging.getLogger(__name__)

SEND_ATTEMPTS = 3
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 30.0
PRESS_WAIT_S = 25
MAX_RETRY_AFTER_S = 60.0


@dataclass(frozen=True)
class Button:
    id: str
    text: str


@dataclass(frozen=True)
class Press:
    seq: int
    message_id: str
    button_id: str
    at: datetime


class Notifier(Protocol):
    async def send(
        self,
        text: str,
        buttons: list[list[Button]] | None = None,
        files: list[tuple[str, bytes]] | None = None,
        dedupe: str | None = None,
        expires_in: int | None = None,
        silent: bool = False,
    ) -> str | None: ...

    async def edit(
        self,
        message_id: str,
        text: str | None = None,
        buttons: list[list[Button]] | None = None,
    ) -> None: ...

    def presses(self) -> AsyncIterator[Press]: ...


class LogNotifier:
    """Notifier that only logs a one-line summary. Never logs the text."""

    async def send(
        self,
        text: str,
        buttons: list[list[Button]] | None = None,
        files: list[tuple[str, bytes]] | None = None,
        dedupe: str | None = None,
        expires_in: int | None = None,
        silent: bool = False,
    ) -> str | None:
        log.info(
            "notify (log only): %d chars, %d buttons, %d files",
            len(text),
            sum(len(row) for row in buttons or []),
            len(files or []),
        )
        return None

    async def edit(
        self,
        message_id: str,
        text: str | None = None,
        buttons: list[list[Button]] | None = None,
    ) -> None:
        log.info("notify edit (log only): message %s", message_id)

    async def presses(self) -> AsyncIterator[Press]:
        return
        yield  # pragma: no cover - makes this an async generator


def _buttons_json(rows: list[list[Button]]) -> list[list[dict[str, str]]]:
    return [[{"id": b.id, "text": b.text} for b in row] for row in rows]


class ServiceNotifyClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._token = token
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            timeout=httpx.Timeout(15.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _safe(self, value: object) -> str:
        return redact(str(value), [self._token])

    async def _request(
        self, method: str, path: str, *, attempts: int = SEND_ATTEMPTS, **kwargs: Any
    ) -> dict[str, Any] | None:
        """One API call with bounded retries. Returns the JSON body or None on failure."""
        for attempt in range(1, attempts + 1):
            last = attempt == attempts
            delay = min(BACKOFF_BASE_S * 2 ** (attempt - 1), BACKOFF_MAX_S)
            try:
                resp = await self._client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if last:
                    log.warning("notify %s %s failed: %s", method, path, self._safe(repr(exc)))
                    return None
                await self._sleep(delay)
                continue

            if resp.status_code < 400:
                try:
                    body = resp.json()
                except ValueError:
                    log.warning("notify %s %s: answer is not JSON", method, path)
                    return None
                return body if isinstance(body, dict) else None

            error, retry_after = self._error_info(resp)
            retryable = resp.status_code in (429, 503)
            if not retryable or last:
                log.warning(
                    "notify %s %s refused: %d %s", method, path, resp.status_code, self._safe(error)
                )
                return None
            if resp.status_code == 429 and retry_after is not None:
                delay = min(retry_after, MAX_RETRY_AFTER_S)
            await self._sleep(delay)
        return None

    @staticmethod
    def _error_info(resp: httpx.Response) -> tuple[str, float | None]:
        try:
            body = resp.json()
        except ValueError:
            return "", None
        if not isinstance(body, dict):
            return "", None
        retry_after = body.get("retry_after")
        if not isinstance(retry_after, (int, float)) or isinstance(retry_after, bool):
            retry_after = None
        return str(body.get("error", "")), retry_after

    async def send(
        self,
        text: str,
        buttons: list[list[Button]] | None = None,
        files: list[tuple[str, bytes]] | None = None,
        dedupe: str | None = None,
        expires_in: int | None = None,
        silent: bool = False,
    ) -> str | None:
        payload: dict[str, Any] = {"text": text}
        if buttons:
            payload["buttons"] = _buttons_json(buttons)
        if files:
            payload["files"] = [
                {"name": name, "data": base64.b64encode(data).decode("ascii")}
                for name, data in files
            ]
        if dedupe is not None:
            payload["dedupe"] = dedupe
        if expires_in is not None:
            payload["expires_in"] = expires_in
        if silent:
            payload["silent"] = True
        body = await self._request("POST", "/v1/messages", json=payload)
        if body is None or "message_id" not in body:
            return None
        return str(body["message_id"])

    async def edit(
        self,
        message_id: str,
        text: str | None = None,
        buttons: list[list[Button]] | None = None,
    ) -> None:
        payload: dict[str, Any] = {}
        if text is not None:
            payload["text"] = text
        if buttons is not None:
            payload["buttons"] = _buttons_json(buttons)
        await self._request("PATCH", f"/v1/messages/{message_id}", json=payload)

    async def presses(self) -> AsyncIterator[Press]:
        """Long-poll forever. Each press is yielded once, in seq order within a batch."""
        failures = 0
        while True:
            try:
                resp = await self._client.get(
                    "/v1/presses",
                    params={"wait": PRESS_WAIT_S},
                    timeout=httpx.Timeout(PRESS_WAIT_S + 10.0),
                )
                if resp.status_code >= 400:
                    raise httpx.HTTPStatusError(
                        f"status {resp.status_code}", request=resp.request, response=resp
                    )
                items = list(resp.json().get("presses", []))
            except (httpx.HTTPError, ValueError, AttributeError) as exc:
                failures += 1
                log.warning("notify presses poll failed: %s", self._safe(repr(exc)))
                await self._sleep(min(BACKOFF_BASE_S * 2 ** (failures - 1), BACKOFF_MAX_S))
                continue
            failures = 0
            parsed: list[Press] = []
            for item in items:
                try:
                    parsed.append(
                        Press(
                            seq=int(item["seq"]),
                            message_id=str(item["message_id"]),
                            button_id=str(item["button_id"]),
                            at=datetime.fromtimestamp(float(item["at"]), UTC),
                        )
                    )
                except (KeyError, TypeError, ValueError, OverflowError, OSError):
                    log.warning("malformed press skipped")
            for press in sorted(parsed, key=lambda p: p.seq):
                yield press
