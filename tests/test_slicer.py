import json
import zipfile
from pathlib import Path

import httpx
import pytest

from bambu_mcp.names import slug
from bambu_mcp.slicer import (
    MAX_JSON_BYTES,
    MAX_PNG_BYTES,
    GatewayWaker,
    SliceClient,
    SliceOutcome,
    SliceRequest,
    WakeError,
)
from slice_agent.app import create_app
from slice_agent.config import AgentConfig
from slice_agent.runner import SliceRun

GATEWAY = "http://192.168.88.11:8780"
GATEWAY_KEY = "gateway-secret-key"
AGENT = "http://192.168.88.20:11436"
TOKEN = "slice-secret-token"
PNG = b"\x89PNG\r\n\x1a\nplate"
UPLOAD = b"PK\x03\x04" + b"model-bytes" * 10


# -- names.slug ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("lamp-arm", "lamp-arm"),
        ("Lamp Arm v2", "lamp-arm-v2"),
        ("  odd__name..here  ", "odd-name-here"),
        ("a" * 60, "a" * 40),
        ("x" * 39 + " y", "x" * 39),  # cut at 40, then trimmed again
        ("...", "job"),
        ("___", "job"),
        ("CON", "con-job"),  # a reserved Windows name alone is refused by the slice-agent
        ("com1", "com1-job"),
        ("console", "console"),
    ],
)
def test_slug(name, expected):
    assert slug(name) == expected


# -- GatewayWaker -------------------------------------------------------------------


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Gateway:
    def __init__(self, status: int = 202) -> None:
        self.status = status
        self.requests: list[tuple[str, str, str | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path, request.headers.get("authorization")))
        if self.status == 0:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(self.status, json={"ok": True})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def probe(answers: list[bool]):
    calls: list[int] = []

    async def ready() -> bool:
        calls.append(1)
        return answers.pop(0) if len(answers) > 1 else answers[0]

    return ready, calls


async def test_wake_polls_until_ready():
    gw, t = Gateway(), FakeTime()
    ready, calls = probe([False, False, False, True])
    async with gw.client() as http:
        waker = GatewayWaker(GATEWAY, GATEWAY_KEY, http, ready=ready, clock=t.clock, sleep=t.sleep)
        assert await waker.wake_and_wait() is True
    assert gw.requests == [("POST", "/wake", f"Bearer {GATEWAY_KEY}")]
    assert len(calls) == 4  # once before /wake, then every 5 s
    assert t.sleeps == [5.0, 5.0, 5.0]


async def test_wake_skips_the_gateway_when_already_ready():
    gw, t = Gateway(), FakeTime()
    ready, _ = probe([True])
    async with gw.client() as http:
        waker = GatewayWaker(GATEWAY, GATEWAY_KEY, http, ready=ready, clock=t.clock, sleep=t.sleep)
        assert await waker.wake_and_wait() is True
    assert gw.requests == [] and t.sleeps == []


async def test_wake_timeout_returns_false():
    gw, t = Gateway(), FakeTime()
    ready, calls = probe([False])
    async with gw.client() as http:
        waker = GatewayWaker(GATEWAY, GATEWAY_KEY, http, ready=ready, clock=t.clock, sleep=t.sleep)
        assert await waker.wake_and_wait(budget_s=120) is False
    assert sum(t.sleeps) == pytest.approx(120)
    assert len(calls) == 1 + 24
    assert len(gw.requests) == 1  # /wake is posted once, never again


@pytest.mark.parametrize(
    ("status", "kind", "error"),
    [
        (401, "refused", "the gateway refused the wake request (HTTP 401)"),
        (500, "refused", "the gateway refused the wake request (HTTP 500)"),
        (0, "unreachable", "the gateway did not answer the wake request (ConnectError)"),
    ],
)
async def test_wake_refused_or_unreachable_gateway_raises_at_once(status, kind, error, caplog):
    gw, t = Gateway(status), FakeTime()
    ready, calls = probe([False])
    async with gw.client() as http:
        waker = GatewayWaker(GATEWAY, GATEWAY_KEY, http, ready=ready, clock=t.clock, sleep=t.sleep)
        with pytest.raises(WakeError) as exc:
            await waker.wake_and_wait()
    assert exc.value.kind == kind and str(exc.value) == error
    assert t.sleeps == [] and len(calls) == 1
    assert GATEWAY_KEY not in caplog.text and GATEWAY_KEY not in str(exc.value)


async def test_wake_without_gateway_only_probes():
    t = FakeTime()
    ready, calls = probe([False])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as http:
        waker = GatewayWaker(None, None, http, ready=ready, clock=t.clock, sleep=t.sleep)
        with pytest.raises(WakeError) as exc:
            await waker.wake_and_wait()
    assert exc.value.kind == "not_configured"
    assert len(calls) == 1 and t.sleeps == []


# -- SliceClient against the real slice-agent app -------------------------------------


def make_cfg(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        studio_exe=Path("C:/BS/bambu-studio.exe"),
        studio_data=tmp_path / "data",
        studio_resources=tmp_path / "res",
        profiles_dir=tmp_path / "profiles",
        sliced_dir=tmp_path / "sliced",
        slice_token=TOKEN,
    )


def write_sliced(path: Path, plates=(1,)) -> Path:
    """A minimal synthetic .gcode.3mf (ruling R1: never a real sliced file)."""
    info = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>", "<header>",
            '<header_item key="X-BBL-Client-Version" value="02.08.02.61"/>', "</header>"]
    for i in plates:
        info += [f'<plate><metadata key="index" value="{i}"/><metadata key="prediction" value="{600 * i}"/>',
                 f'<filament id="1" type="PETG" used_g="{1.5 * i}"/></plate>']
    info.append("</config>")
    settings = {"printable_area": ["0x0", "256x0", "256x256", "0x256"], "printer_model": "Bambu Lab P2S",
                "nozzle_diameter": ["0.4"]}
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/slice_info.config", "\n".join(info))
        z.writestr("Metadata/project_settings.config", json.dumps(settings))
        for i in plates:
            z.writestr(f"Metadata/plate_{i}.gcode", "G1 X0\n")
            z.writestr(f"Metadata/plate_{i}.png", PNG + bytes([i]))
    return path


class FakeStudio:
    def __init__(self, plates=(1,)) -> None:
        self.plates = plates
        self.calls: list[dict] = []

    def __call__(self, cfg, *, out_name, input_3mf, **kw):
        self.calls.append({"out_name": out_name, "upload": input_3mf.read_bytes(), **kw})
        return SliceRun(0, "success"), write_sliced(cfg.sliced_dir / f"{out_name}.gcode.3mf", self.plates)


def no_pc_agent() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))


def request(**changes) -> SliceRequest:
    data = dict(
        job_id=7, name="lamp-arm", kit=False, plates=(1,), nozzle="0.4", process_set="standard",
        filament="Generic PETG @BBL P2S", plate_type="Textured PEI Plate",
        overrides={"wall_loops": 4, "sparse_infill_density": 25, "enable_support": False},
    )
    data.update(changes)
    return SliceRequest(**data)


async def test_slice_client_against_the_agent(tmp_path):
    studio = FakeStudio()
    app = create_app(make_cfg(tmp_path), slicer=studio, pc_agent=no_pc_agent())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        client = SliceClient(AGENT, TOKEN, http)
        assert await client.health() is True
        outcome = await client.slice(request(), UPLOAD)
        assert outcome == SliceOutcome(
            "ok",
            file="lamp-arm-7-p1.gcode.3mf",
            plates=({"index": 1, "minutes": 10.0, "grams": {"1": 1.5}},),  # keyed by filament id
            slicer_version="02.08.02.61",
        )
        assert await client.fetch_png(outcome.file, 1) == PNG + b"\x01"
        assert await client.fetch_png(outcome.file, 2) is None
    call = studio.calls[0]
    assert call["out_name"] == "lamp-arm-7-p1" and call["upload"] == UPLOAD
    assert call["nozzle"] == "0.4" and call["process_set"] == "standard"
    assert call["filament"] == "Generic PETG @BBL P2S" and call["plate_type"] == "Textured PEI Plate"
    assert call["kit"] is False
    assert call["overrides"].values == {"wall_loops": 4, "sparse_infill_density": 25, "enable_support": False}


async def test_slice_client_kit_names_the_kit_file(tmp_path):
    app = create_app(make_cfg(tmp_path), slicer=FakeStudio(plates=(1, 2)), pc_agent=no_pc_agent())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        outcome = await SliceClient(AGENT, TOKEN, http).slice(request(kit=True, plates=(1, 2)), UPLOAD)
    assert outcome.status == "ok" and outcome.file == "lamp-arm-7-kit.gcode.3mf"
    assert [p["index"] for p in outcome.plates] == [1, 2]


async def test_slice_client_422_from_the_agent_is_a_failure_with_detail(tmp_path):
    app = create_app(make_cfg(tmp_path), slicer=FakeStudio(), pc_agent=no_pc_agent())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        outcome = await SliceClient(AGENT, TOKEN, http).slice(request(filament="Unknown PLA"), UPLOAD)
    assert outcome.status == "failed"
    assert outcome.detail == "unknown filament: 'Unknown PLA'"


# -- SliceClient answers, MockTransport ------------------------------------------------


class Agent:
    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def client(self) -> SliceClient:
        return SliceClient(AGENT, TOKEN, httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))


OK_BODY = {
    "file": "lamp-arm-7-p1.gcode.3mf",
    "plates": [{"index": 1, "minutes": 100.5, "grams": {"PETG": 14.25}}],
    "slicer_version": "02.08.02.61",
}


async def test_slice_sends_bearer_and_multipart():
    agent = Agent(httpx.Response(200, json=OK_BODY))
    outcome = await agent.client().slice(request(), UPLOAD)
    assert outcome.status == "ok" and outcome.plates[0]["minutes"] == 100.5
    req = agent.requests[0]
    assert req.method == "POST" and str(req.url) == f"{AGENT}/slice"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b'name="job"' in req.content and b'name="file"' in req.content


@pytest.mark.parametrize(
    ("response", "status", "detail"),
    [
        (httpx.Response(409, json={"error": "busy"}), "busy", ""),
        (httpx.Response(422, json={"error": "bad_request", "detail": "plates must be [1]"}), "failed",
         "plates must be [1]"),
        (httpx.Response(500, json={"error": "slice_failed", "detail": "plate 2 is not in the sliced file",
                                   "log_tail": "x"}), "failed", "plate 2 is not in the sliced file"),
        (httpx.Response(500, text="<html>oops</html>"), "failed", "slice-agent answered HTTP 500"),
        (httpx.Response(401, json={"error": "unauthorized"}), "failed", "slice-agent refused the token"),
        (httpx.Response(404, json={"error": "nope"}), "failed", "slice-agent answered HTTP 404"),
        (httpx.ConnectError("refused"), "unreachable", "ConnectError"),
        (httpx.ReadTimeout("slow"), "unreachable", "ReadTimeout"),
    ],
)
async def test_slice_answers(response, status, detail):
    outcome = await Agent(response).client().slice(request(), UPLOAD)
    assert outcome.status == status
    assert outcome.detail == detail
    assert TOKEN not in outcome.detail


@pytest.mark.parametrize(
    ("raw", "detail"),
    [
        (r"cannot open C:\Users\owner\AppData\Local\bambu-mcp\profiles\x.json", "cannot open <path>"),
        ("missing D:/work/x.3mf", "missing <path>"),
        (r"log at <sliced>\.work-lamp-arm-7-p1\log.txt", "log at <sliced><path>"),
        ("see /home/owner/studio.log, then retry", "see <path>, then retry"),
        ("bad file profiles/process-standard.json", "bad file <path>"),
        (r"\\pc\share\sliced", "<path>"),
        ("<profiles> missing, support_type tree(auto), 25%", "<profiles> missing, support_type tree(auto), 25%"),
        ("plates must be [1], not /2", "plates must be [1], not /2"),
    ],
)
async def test_slice_failure_detail_hides_paths(raw, detail):
    agent = Agent(httpx.Response(500, json={"error": "slice_failed", "detail": raw}))
    outcome = await agent.client().slice(request(), UPLOAD)
    assert outcome.detail == detail


async def test_slice_failure_detail_is_capped():
    agent = Agent(httpx.Response(500, json={"error": "slice_failed", "detail": "x" * 500}))
    outcome = await agent.client().slice(request(), UPLOAD)
    assert outcome.status == "failed" and len(outcome.detail) <= 200


@pytest.mark.parametrize(
    "body",
    [
        [],
        {**OK_BODY, "file": "other-7-p1.gcode.3mf"},  # not the file this job asked for
        {**OK_BODY, "file": "../lamp-arm-7-p1.gcode.3mf"},
        {**OK_BODY, "plates": []},
        {**OK_BODY, "plates": [{"index": 2, "minutes": 1, "grams": {}}]},  # a part is plate 1
        {**OK_BODY, "plates": [{"index": 1, "minutes": "long", "grams": {}}]},
        {**OK_BODY, "plates": [{"index": 1, "minutes": -1, "grams": {}}]},
        {**OK_BODY, "plates": [{"index": 1, "minutes": 1, "grams": {"PETG": "a lot"}}]},
        {**OK_BODY, "plates": [{"index": 1, "minutes": 1, "grams": []}]},
    ],
)
async def test_slice_unexpected_ok_body_is_a_failure(body):
    outcome = await Agent(httpx.Response(200, json=body)).client().slice(request(), UPLOAD)
    assert outcome.status == "failed"
    assert outcome.detail == "slice-agent answered with an unexpected result"


async def test_health_false_when_unreachable_or_not_ok():
    for response in (httpx.ConnectError("down"), httpx.Response(200, json={"ok": False}),
                     httpx.Response(503), httpx.Response(200, text="not json")):
        assert await Agent(response).client().health() is False


async def test_fetch_png():
    agent = Agent(httpx.Response(200, content=PNG, headers={"content-type": "image/png"}))
    client = agent.client()
    assert await client.fetch_png("lamp-arm-7-p1.gcode.3mf", 1) == PNG
    req = agent.requests[0]
    assert str(req.url) == f"{AGENT}/sliced/lamp-arm-7-p1.gcode.3mf/plate/1.png"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    "response",
    [httpx.Response(404), httpx.Response(200, content=b"not a png"), httpx.ConnectError("down")],
)
async def test_fetch_png_missing_is_none(response):
    assert await Agent(response).client().fetch_png("lamp-arm-7-p1.gcode.3mf", 1) is None


@pytest.mark.parametrize("name", ["../secret.gcode.3mf", "a/b.gcode.3mf", "lamp-arm.3mf", ""])
async def test_fetch_png_refuses_odd_names_without_a_request(name):
    agent = Agent(httpx.Response(200, content=PNG))
    assert await agent.client().fetch_png(name, 1) is None
    assert agent.requests == []


# -- response size caps --------------------------------------------------------------


async def test_oversized_ok_body_is_a_clean_failure():
    body = {**OK_BODY, "padding": "x" * MAX_JSON_BYTES}
    outcome = await Agent(httpx.Response(200, json=body)).client().slice(request(), UPLOAD)
    assert outcome == SliceOutcome("failed", detail="slice-agent answered with an oversized body")


async def test_oversized_error_body_is_a_clean_failure():
    body = {"error": "slice_failed", "detail": "x" * (MAX_JSON_BYTES + 1)}
    outcome = await Agent(httpx.Response(500, json=body)).client().slice(request(), UPLOAD)
    assert outcome == SliceOutcome("failed", detail="slice-agent answered with an oversized body")


async def test_oversized_health_body_is_not_healthy():
    body = {"ok": True, "padding": "x" * MAX_JSON_BYTES}
    assert await Agent(httpx.Response(200, json=body)).client().health() is False


async def test_streamed_body_over_the_cap_is_cut_off():
    chunks_read = []

    async def stream():
        for _ in range(100):
            chunks_read.append(1)
            yield b"x" * (64 * 1024)

    agent = Agent(httpx.Response(200, content=stream()))
    outcome = await agent.client().slice(request(), UPLOAD)
    assert outcome.detail == "slice-agent answered with an oversized body"
    assert len(chunks_read) < 100  # stopped reading at the cap


async def test_png_over_the_cap_is_none():
    big = PNG + b"\0" * MAX_PNG_BYTES
    agent = Agent(httpx.Response(200, content=big, headers={"content-type": "image/png"}))
    assert await agent.client().fetch_png("lamp-arm-7-p1.gcode.3mf", 1) is None
    exact = PNG + b"\0" * (MAX_PNG_BYTES - len(PNG))
    agent = Agent(httpx.Response(200, content=exact, headers={"content-type": "image/png"}))
    assert await agent.client().fetch_png("lamp-arm-7-p1.gcode.3mf", 1) == exact
