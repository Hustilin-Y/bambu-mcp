import base64
import json
from datetime import UTC, datetime

import httpx

from bambu_mcp.notify import Button, LogNotifier, Press, ServiceNotifyClient

TOKEN = "tok-secret-123"


def make(handler, sleeps=None):
    async def sleep(s: float) -> None:
        if sleeps is not None:
            sleeps.append(s)

    return ServiceNotifyClient(
        "http://127.0.0.1:8790",
        TOKEN,
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )


async def test_service_notify_send():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["req"] = request
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"message_id": "42"})

    client = make(handler)
    mid = await client.send(
        "hello",
        buttons=[[Button("a:1", "One"), Button("a:2", "Two")], [Button("a:3", "Three")]],
        files=[("snap.jpg", b"\xff\xd8data")],
        dedupe="key-1",
        expires_in=600,
        silent=True,
    )
    assert mid == "42"
    req = seen["req"]
    assert req.method == "POST" and req.url.path == "/v1/messages"
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    body = seen["json"]
    assert body["text"] == "hello"
    assert body["buttons"] == [
        [{"id": "a:1", "text": "One"}, {"id": "a:2", "text": "Two"}],
        [{"id": "a:3", "text": "Three"}],
    ]
    assert body["files"] == [
        {"name": "snap.jpg", "data": base64.b64encode(b"\xff\xd8data").decode()}
    ]
    assert body["dedupe"] == "key-1"
    assert body["expires_in"] == 600
    assert body["silent"] is True


async def test_send_minimal_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"message_id": "1"})

    await make(handler).send("hi")
    assert seen["json"] == {"text": "hi"}


async def test_service_notify_duplicate():
    def handler(request):
        return httpx.Response(200, json={"message_id": "7", "duplicate": True})

    assert await make(handler).send("x", dedupe="k") == "7"


async def test_edit_patch():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["req"] = request
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"message_id": "9"})

    await make(handler).edit("9", text="done", buttons=[])
    assert seen["req"].method == "PATCH"
    assert seen["req"].url.path == "/v1/messages/9"
    assert seen["json"] == {"text": "done", "buttons": []}


async def test_presses_long_poll():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "presses": [
                    {"seq": 6, "message_id": "m", "button_id": "b2", "label": "L", "by": "o", "at": 1700000100},
                    {"seq": 5, "message_id": "m", "button_id": "b1", "label": "L", "by": "o", "at": 1700000000},
                ]
            },
        )

    got = []
    async for press in make(handler).presses():
        got.append(press)
        if len(got) == 2:
            break
    assert [p.seq for p in got] == [5, 6]
    assert got[0] == Press(5, "m", "b1", datetime.fromtimestamp(1700000000, UTC))
    assert got[0].at.tzinfo is not None
    assert calls[0].url.path == "/v1/presses"
    assert calls[0].url.params["wait"] == "25"


async def test_presses_survives_errors():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("router restarting")
        return httpx.Response(
            200,
            json={"presses": [{"seq": 1, "message_id": "m", "button_id": "b", "label": "L", "by": "o", "at": 1700000000}]},
        )

    sleeps: list[float] = []
    async for press in make(handler, sleeps).presses():
        assert press.seq == 1
        break
    assert len(sleeps) == 1


async def test_retry_on_connection_error():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("router restarting")
        return httpx.Response(200, json={"message_id": "3"})

    sleeps: list[float] = []
    assert await make(handler, sleeps).send("x") == "3"
    assert len(attempts) == 2 and len(sleeps) == 1


async def test_retry_on_503_then_gives_up(caplog):
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(503, json={"error": "telegram_unreachable"})

    assert await make(handler).send("x") is None
    assert len(attempts) == 3
    assert TOKEN not in caplog.text


async def test_429_honours_retry_after():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, json={"error": "rate_limited", "retry_after": 7})
        return httpx.Response(200, json={"message_id": "5"})

    sleeps: list[float] = []
    assert await make(handler, sleeps).send("x") == "5"
    assert sleeps == [7]


async def test_4xx_not_retried():
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(400, json={"error": "bad_request", "detail": "text"})

    assert await make(handler).send("x") is None
    assert len(attempts) == 1


async def test_edit_failure_is_swallowed():
    def handler(request):
        return httpx.Response(404, json={"error": "not_found"})

    assert await make(handler).edit("1", text="t") is None


async def test_failure_log_has_no_token_or_text(caplog):
    def handler(request):
        raise httpx.ConnectError(f"cannot reach with {TOKEN}")

    with caplog.at_level("WARNING"):
        assert await make(handler).send("private text body") is None
    assert TOKEN not in caplog.text
    assert "private text body" not in caplog.text
    assert caplog.records


async def test_log_notifier(caplog):
    notifier = LogNotifier()
    with caplog.at_level("INFO"):
        mid = await notifier.send("secret-ish text", dedupe="k")
    assert mid is None
    assert "secret-ish text" not in caplog.text
    assert [p async for p in notifier.presses()] == []


async def test_presses_skips_malformed_items(caplog):
    good = {"message_id": "m", "button_id": "b", "label": "L", "by": "o", "at": 1700000000}

    def handler(request):
        return httpx.Response(
            200,
            json={
                "presses": [
                    {**good, "seq": 1},
                    {"seq": 2, "message_id": "m", "button_id": "b", "at": None},
                    {"message_id": "m", "button_id": "b", "at": 1700000000},
                    "garbage",
                    {**good, "seq": 3},
                ]
            },
        )

    got = []
    with caplog.at_level("WARNING"):
        async for press in make(handler).presses():
            got.append(press.seq)
            if len(got) == 2:
                break
    assert got == [1, 3]
    assert "malformed press skipped" in caplog.text
