import base64
import json
from datetime import UTC, datetime

import httpx
import pytest

from bambu_mcp.notify import Button, LogNotifier, Press, SendError, ServiceNotifyClient

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


async def test_single_newlines_become_paragraph_breaks_on_send_and_edit():
    # The add-in renders rich Markdown: only a blank line splits paragraphs.
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"message_id": "5"})

    client = make(handler)
    await client.send("пластик 5.60 грн\nсвет 1.92 грн\nитого 7.52 грн")
    assert await client.edit("5", text="one\ntwo\n\nthree") is True
    assert seen[0]["text"] == "пластик 5.60 грн\n\nсвет 1.92 грн\n\nитого 7.52 грн"
    assert seen[1]["text"] == "one\n\ntwo\n\nthree"  # a break already double stays double


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

    assert await make(handler).edit("9", text="done", buttons=[]) is True
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


async def test_edit_retries_on_connection_error():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("router restarting")
        return httpx.Response(200, json={"message_id": "3"})

    sleeps: list[float] = []
    await make(handler, sleeps).edit("3", text="x")
    assert len(attempts) == 2 and len(sleeps) == 1


async def test_edit_retries_on_503_then_gives_up(caplog):
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(503, json={"error": "telegram_unreachable"})

    assert await make(handler).edit("1", text="x") is False
    assert len(attempts) == 3
    assert TOKEN not in caplog.text


async def test_edit_429_honours_retry_after():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, json={"error": "rate_limited", "retry_after": 7})
        return httpx.Response(200, json={"message_id": "5"})

    sleeps: list[float] = []
    await make(handler, sleeps).edit("5", text="x")
    assert sleeps == [7]


async def test_send_is_one_attempt_and_raises_retryable_on_connection_error():
    attempts = []

    def handler(request):
        attempts.append(1)
        raise httpx.ConnectError(f"cannot reach with {TOKEN}")

    sleeps: list[float] = []
    with pytest.raises(SendError) as exc:
        await make(handler, sleeps).send("private text body")
    assert len(attempts) == 1 and sleeps == []
    assert exc.value.status is None and exc.value.retryable
    assert TOKEN not in str(exc.value) and "private text body" not in str(exc.value)


@pytest.mark.parametrize(
    ("status", "body", "retryable"),
    [
        (503, {"error": "telegram_unreachable"}, True),
        (429, {"error": "rate_limited", "retry_after": 7}, True),
        (401, {"error": "unauthorized"}, True),
        (400, {"error": "bad_request", "detail": "text"}, False),
        (403, {"error": "recipient_not_allowed"}, False),
        (413, {"error": "too_large"}, False),
        (500, {"error": "not_kept"}, False),
        (502, {"error": "telegram_refused", "detail": "bad"}, False),
    ],
)
async def test_send_error_classification(status, body, retryable):
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(status, json=body)

    with pytest.raises(SendError) as exc:
        await make(handler).send("x", dedupe="k")
    assert len(attempts) == 1
    assert exc.value.status == status
    assert exc.value.code == body["error"]
    assert exc.value.retryable is retryable
    assert exc.value.retry_after == body.get("retry_after")


async def test_send_non_json_success_raises_retryable():
    def handler(request):
        return httpx.Response(200, content=b"oops")

    with pytest.raises(SendError) as exc:
        await make(handler).send("x")
    assert exc.value.retryable


async def test_presses_survives_non_dict_and_null_bodies(caplog):
    bodies = [{"presses": None}, [], "text", {"presses": [{"seq": 1, "message_id": "m", "button_id": "b", "at": 1700000000}]}]

    def handler(request):
        body = bodies.pop(0)
        return httpx.Response(200, json=body)

    sleeps: list[float] = []
    with caplog.at_level("WARNING"):
        async for press in make(handler, sleeps).presses():
            assert press.seq == 1
            break
    assert len(sleeps) == 3
    assert "TypeError" in caplog.text or "AttributeError" in caplog.text


async def test_presses_401_is_logged_once(caplog):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) > 5:
            return httpx.Response(200, json={"presses": [{"seq": 1, "message_id": "m", "button_id": "b", "at": 1700000000}]})
        return httpx.Response(401, json={"error": "unauthorized"})

    with caplog.at_level("WARNING"):
        async for _ in make(handler, []).presses():
            break
    assert sum("presses poll failed" in r.getMessage() for r in caplog.records) == 1
    assert TOKEN not in caplog.text


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


async def test_press_via_parsed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "presses": [
                    {"seq": 1, "message_id": "m", "button_id": "b", "at": 1700000000, "via": "miniapp"},
                    {"seq": 2, "message_id": "m", "button_id": "b", "at": 1700000000},
                    {"seq": 3, "message_id": "m", "button_id": "b", "at": 1700000000, "via": "carrier-pigeon"},
                ]
            },
        )

    got = []
    async for press in make(handler).presses():
        got.append(press)
        if len(got) == 3:
            break
    assert [p.via for p in got] == ["miniapp", "telegram", "telegram"]
