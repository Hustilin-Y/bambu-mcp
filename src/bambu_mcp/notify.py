"""Telegram notifications through the multiclaude service-notify add-in."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from bambu_mcp.logthrottle import Throttle
from bambu_mcp.redact import redact

log = logging.getLogger(__name__)

# A newline that is not part of a blank line.
_SINGLE_NEWLINE = re.compile(r"(?<!\n)\n(?!\n)")


def paragraphs(text: str) -> str:
    """Every single newline as a blank line. The add-in sends rich Markdown, where only a
    blank line splits paragraphs and a lone newline collapses into a space."""
    return _SINGLE_NEWLINE.sub("\n\n", text)

EDIT_ATTEMPTS = 3
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 30.0
PRESS_WAIT_S = 25
MAX_RETRY_AFTER_S = 60.0


@dataclass(frozen=True)
class Button:
    id: str
    text: str


class SendError(Exception):
    """A message was not sent. ``status`` is None when the add-in could not be reached.

    Never carries the message text or the token.
    """

    # 500 not_kept: the message went out without its buttons, sending it again changes nothing.
    # 502 telegram_refused: Telegram's verdict on this message, the next try gets the same.
    _FINAL_5XX = frozenset({500, 502})

    def __init__(
        self,
        status: int | None,
        code: str,
        *,
        detail: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"{status if status is not None else 'no answer'} {code}".strip())
        self.status = status
        self.code = code
        self.detail = detail
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        """Worth sending again later: no answer, 401 (token not set up yet), 429 or a 5xx outage."""
        s = self.status
        return s is None or s in (401, 429) or (s >= 500 and s not in self._FINAL_5XX)


@dataclass(frozen=True)
class Press:
    seq: int
    message_id: str
    button_id: str
    at: datetime
    via: str = "telegram"  # or "miniapp": the same button pressed in the Mini App (service-notify says)


class Notifier(Protocol):
    """``send`` makes one attempt: the message id (None when only logged) or ``SendError``.

    ``edit`` returns False when the add-in did not take the edit (after its retries).
    """

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
    ) -> bool: ...

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
    ) -> bool:
        log.info("notify edit (log only): message %s", message_id)
        return True  # nothing to deliver, so nothing to send again

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
        self._quiet = Throttle(log)
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
        self, method: str, path: str, *, attempts: int = EDIT_ATTEMPTS, **kwargs: Any
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
    def _error_body(resp: httpx.Response) -> dict[str, Any]:
        try:
            body = resp.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    @classmethod
    def _error_info(cls, resp: httpx.Response) -> tuple[str, float | None]:
        body = cls._error_body(resp)
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
        payload: dict[str, Any] = {"text": paragraphs(text)}
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
        # One attempt: the caller's outbox retries, safely thanks to ``dedupe``.
        try:
            resp = await self._client.post("/v1/messages", json=payload)
        except httpx.TransportError as exc:
            raise SendError(None, type(exc).__name__) from None
        if resp.status_code >= 400:
            error, retry_after = self._error_info(resp)
            detail = self._safe(self._error_body(resp).get("detail", ""))
            raise SendError(resp.status_code, self._safe(error), detail=detail, retry_after=retry_after)
        body = self._error_body(resp)
        if "message_id" not in body:
            raise SendError(None, "no_message_id")  # no usable answer: try again
        return str(body["message_id"])

    async def edit(
        self,
        message_id: str,
        text: str | None = None,
        buttons: list[list[Button]] | None = None,
    ) -> bool:
        payload: dict[str, Any] = {}
        if text is not None:
            payload["text"] = paragraphs(text)
        if buttons is not None:
            payload["buttons"] = _buttons_json(buttons)
        return await self._request("PATCH", f"/v1/messages/{message_id}", json=payload) is not None

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
            except Exception as exc:  # any bad answer: log, back off, poll again
                failures += 1
                reason = type(exc).__name__
                if isinstance(exc, httpx.HTTPStatusError):
                    reason = f"{reason} {exc.response.status_code}"
                self._quiet.warning(f"presses {reason}", "notify presses poll failed: %s", reason)
                await self._sleep(min(BACKOFF_BASE_S * 2 ** (failures - 1), BACKOFF_MAX_S))
                continue
            if failures:
                self._quiet = Throttle(log)  # recovered: the next failure is logged at once
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
                            via="miniapp" if item.get("via") == "miniapp" else "telegram",
                        )
                    )
                except (KeyError, TypeError, ValueError, OverflowError, OSError):
                    log.warning("malformed press skipped")
            for press in sorted(parsed, key=lambda p: p.seq):
                yield press
