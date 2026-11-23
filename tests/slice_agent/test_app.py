import asyncio
import dataclasses
import json
import threading
import zipfile
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from slice_agent import app as app_mod
from slice_agent.app import MAX_FILE_BYTES, create_app
from slice_agent.config import AgentConfig
from slice_agent.lease import Lease
from slice_agent.runner import Overrides, SliceRun

TOKEN = "slice-secret-token"
PC_TOKEN = "pc-agent-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PNG = b"\x89PNG\r\n\x1a\nplate"
UPLOAD = b"PK\x03\x04" + b"model-bytes" * 10


def make_cfg(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        studio_exe=Path("C:/BS/bambu-studio.exe"),
        studio_data=tmp_path / "data",
        studio_resources=tmp_path / "res",
        profiles_dir=tmp_path / "profiles",
        sliced_dir=tmp_path / "sliced",
        slice_token=TOKEN,
        pc_agent_token=PC_TOKEN,
    )


def write_sliced(path: Path, plates=(1,), nozzle="0.4", version="02.08.02.61") -> Path:
    info = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>", "<header>",
            f'<header_item key="X-BBL-Client-Version" value="{version}"/>', "</header>"]
    for i in plates:
        info += [f'<plate><metadata key="index" value="{i}"/><metadata key="prediction" value="{600 * i}"/>',
                 f'<filament id="1" type="PETG" used_g="{1.5 * i}"/></plate>']
    info.append("</config>")
    settings = {"printable_area": ["0x0", "256x0", "256x256", "0x256"], "printer_model": "Bambu Lab P2S",
                "nozzle_diameter": [nozzle]}
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/slice_info.config", "\n".join(info))
        z.writestr("Metadata/project_settings.config", json.dumps(settings))
        for i in plates:
            z.writestr(f"Metadata/plate_{i}.gcode", "G1 X0\n")
            z.writestr(f"Metadata/plate_{i}.png", PNG + bytes([i]))
    return path


class FakeSlicer:
    """slice_isolated stand-in: records the call, checks the upload, writes a synthetic output."""

    def __init__(self, run=None, plates=(1,), nozzle="0.4", produce=True, gate=None):
        run = run or SliceRun(0, "success")
        self.run, self.plates, self.nozzle, self.produce, self.gate = run, plates, nozzle, produce, gate
        self.calls = []
        self.started = threading.Event()

    def __call__(self, cfg, *, out_name, input_3mf, **kw):
        self.calls.append({"out_name": out_name, "input_3mf": input_3mf, "upload": input_3mf.read_bytes(), **kw})
        self.started.set()
        if self.gate is not None:
            assert self.gate.wait(10)
        if self.run.return_code != 0 or not self.produce:
            return self.run, None
        final = write_sliced(cfg.sliced_dir / f"{out_name}.gcode.3mf", self.plates, self.nozzle)
        return self.run, final


class PcAgent:
    """Fake pc-agent lease API behind httpx.MockTransport."""

    def __init__(self, fail=False):
        self.fail = fail
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body, request.headers.get("authorization")))
        if self.fail == "connect":
            raise httpx.ConnectError("refused", request=request)
        if self.fail:
            return httpx.Response(500, json={"error": "boom"})
        if request.method == "POST" and request.url.path == "/lease":
            return httpx.Response(201, json={"id": "lease-1", "holder": body["holder"], "expires_in_s": body["ttl_s"]})
        if request.method == "POST" and request.url.path.endswith("/renew"):
            return httpx.Response(200, json={"id": "lease-1", "expires_in_s": body["ttl_s"]})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url="http://127.0.0.1:11435", transport=httpx.MockTransport(self.handler))


def job(**kw):
    data = {"job_id": 7, "name": "example-bracket", "kit": False, "plates": [1], "nozzle": "0.4",
            "process_set": "standard", "filament": "Generic PETG @BBL P2S", "plate_type": "Textured PEI Plate",
            "overrides": {"wall_loops": 3}}
    data.update(kw)
    return data


def files(job_data=None, upload=UPLOAD):
    raw = job_data if isinstance(job_data, (str, bytes)) else json.dumps(job_data or job())
    return {"job": (None, raw, "application/json"), "file": ("model.3mf", upload, "application/octet-stream")}


def harness(tmp_path, slicer=None, pc=None, **kw):
    cfg = make_cfg(tmp_path)
    slicer = slicer or FakeSlicer()
    pc = pc or PcAgent()
    app = create_app(cfg, slicer=slicer, pc_agent=pc.client(), **kw)
    return cfg, slicer, pc, TestClient(app, raise_server_exceptions=False)


def test_rejects_missing_token(tmp_path):
    cfg, slicer, pc, client = harness(tmp_path)
    write_sliced(cfg.sliced_dir / "example-bracket-7-p1.gcode.3mf")
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}, {"Authorization": "Basic x"}):
        assert client.post("/slice", files=files(), headers=headers).status_code == 401
        assert client.get("/sliced/example-bracket-7-p1.gcode.3mf", headers=headers).status_code == 401
        assert client.get("/sliced/example-bracket-7-p1.gcode.3mf/plate/1.png", headers=headers).status_code == 401
    assert slicer.calls == [] and pc.requests == []
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_empty_configured_token_refuses_everything(tmp_path):
    cfg = dataclasses.replace(make_cfg(tmp_path), slice_token="")
    client = TestClient(create_app(cfg, slicer=FakeSlicer(), pc_agent=PcAgent().client()))
    assert client.post("/slice", files=files(), headers={"Authorization": "Bearer "}).status_code == 401


def test_slice_happy_path_names_output(tmp_path):
    cfg, slicer, pc, client = harness(tmp_path)
    r = client.post("/slice", files=files(), headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "file": "example-bracket-7-p1.gcode.3mf",
        "plates": [{"index": 1, "minutes": 10.0, "grams": {"1": 1.5}}],
        "slicer_version": "02.08.02.61",
    }
    (call,) = slicer.calls
    assert call["out_name"] == "example-bracket-7-p1"
    assert call["upload"] == UPLOAD
    assert call["kit"] is False and call["nozzle"] == "0.4" and call["process_set"] == "standard"
    assert call["filament"] == "Generic PETG @BBL P2S" and call["plate_type"] == "Textured PEI Plate"
    assert isinstance(call["overrides"], Overrides) and call["overrides"].values == {"wall_loops": 3}
    upload = call["input_3mf"]
    assert upload.parent != cfg.sliced_dir and cfg.sliced_dir in upload.parents
    assert not upload.exists()  # deleted after the slice
    assert sorted(p.name for p in cfg.sliced_dir.iterdir() if p.is_file()) == ["example-bracket-7-p1.gcode.3mf"]


def test_slice_kit_names_output_and_reports_requested_plates(tmp_path):
    slicer = FakeSlicer(plates=(1, 2, 3))
    cfg, slicer, pc, client = harness(tmp_path, slicer=slicer)
    r = client.post("/slice", files=files(job(kit=True, plates=[1, 3], name="Kit 2.v")), headers=AUTH)
    # a space is valid for the manifest but not for an output file name
    assert r.status_code == 422 and r.json()["error"] == "bad_request"
    r = client.post("/slice", files=files(job(kit=True, plates=[1, 3], name="kit_2.v")), headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file"] == "kit_2.v-7-kit.gcode.3mf"
    assert [p["index"] for p in body["plates"]] == [1, 3]
    assert slicer.calls[-1]["kit"] is True and slicer.calls[-1]["out_name"] == "kit_2.v-7-kit"


async def test_second_concurrent_slice_gets_409(tmp_path):
    gate = threading.Event()
    slicer = FakeSlicer(gate=gate)
    cfg = make_cfg(tmp_path)
    app = create_app(cfg, slicer=slicer, pc_agent=PcAgent().client())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
        first = asyncio.create_task(client.post("/slice", files=files(), headers=AUTH))
        assert await asyncio.to_thread(slicer.started.wait, 5)
        r2 = await asyncio.wait_for(client.post("/slice", files=files(), headers=AUTH), 2)
        assert r2.status_code == 409 and r2.json() == {"error": "busy"}
        health = await asyncio.wait_for(client.get("/health"), 2)  # not blocked by the running slice
        assert health.json() == {"ok": True}
        gate.set()
        r1 = await asyncio.wait_for(first, 10)
        assert r1.status_code == 200
        r3 = await client.post("/slice", files=files(), headers=AUTH)  # lock released
        assert r3.status_code == 200
    assert len(slicer.calls) == 2


def test_bad_override_422(tmp_path):
    cfg, slicer, pc, client = harness(tmp_path)
    r = client.post("/slice", files=files(job(overrides={"wall_loops": 99})), headers=AUTH)
    assert r.status_code == 422
    assert r.json()["error"] == "bad_request" and "wall_loops" in r.json()["detail"]
    r = client.post("/slice", files=files(job(overrides={"post_process": "calc.exe"})), headers=AUTH)
    assert r.status_code == 422 and "post_process" in r.json()["detail"]
    assert slicer.calls == [] and pc.requests == []
    assert not any(p.is_file() for p in cfg.sliced_dir.rglob("*"))  # upload removed


@pytest.mark.parametrize(
    "changes",
    [
        {"job_id": 0}, {"job_id": -1}, {"job_id": True}, {"job_id": "7"}, {"job_id": 1.5},
        {"name": ".hidden"}, {"name": "a/b"}, {"name": "..\\x"}, {"name": ""}, {"name": "x" * 101},
        {"name": "CON"}, {"name": "nul.part"}, {"name": "name."}, {"name": 5},
        {"kit": "no"}, {"plates": []}, {"plates": [0]}, {"plates": [51]}, {"plates": list(range(1, 52))},
        {"plates": ["1"]}, {"plates": [True]}, {"plates": 1}, {"plates": [1, 1]}, {"plates": [2]},
        {"nozzle": "0.8"}, {"nozzle": ["0.4"]}, {"process_set": "../x"}, {"process_set": {}},
        {"filament": "Other"}, {"plate_type": "Glass"}, {"overrides": []}, {"overrides": {"layer_height": 0.3}},
        {"extra": 1},
    ],
)
def test_invalid_job_fields_422(tmp_path, changes):
    cfg, slicer, pc, client = harness(tmp_path)
    r = client.post("/slice", files=files(job(**changes)), headers=AUTH)
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"] == "bad_request" and 0 < len(body["detail"]) <= 200
    assert "Traceback" not in r.text
    assert slicer.calls == []


@pytest.mark.parametrize(
    "missing", ["job_id", "name", "kit", "plates", "nozzle", "process_set", "filament", "plate_type"]
)
def test_missing_job_field_422(tmp_path, missing):
    cfg, slicer, pc, client = harness(tmp_path)
    data = job()
    del data[missing]
    r = client.post("/slice", files=files(data), headers=AUTH)
    assert r.status_code == 422 and missing in r.json()["detail"]


def test_malformed_requests_422(tmp_path):
    cfg, slicer, pc, client = harness(tmp_path)
    cases = [
        dict(files=files("not json")),
        dict(files=files("[1, 2]")),
        dict(files=files("[" * 100000 + "]" * 100000)),
        dict(files={"job": (None, json.dumps(job()))}),  # no file
        dict(files={"file": ("m.3mf", UPLOAD)}),  # no job
        dict(files=files(upload=b"")),
        dict(files=files(upload=b"not a zip")),
        dict(files={**files(), "other": (None, "x")}),
        dict(data={"job": json.dumps(job())}),  # urlencoded, not multipart
        dict(content=b"garbage", headers={"Content-Type": "multipart/form-data; boundary=zz"}),
    ]
    for kw in cases:
        headers = {**AUTH, **kw.pop("headers", {})}
        r = client.post("/slice", headers=headers, **kw)
        assert r.status_code == 422, (kw, r.text)
        assert r.json()["error"] == "bad_request"
    assert slicer.calls == []
    assert not any(p.is_file() for p in cfg.sliced_dir.rglob("*"))


def test_upload_size_caps(tmp_path):
    assert MAX_FILE_BYTES == 50 * 1024 * 1024
    cfg, slicer, pc, client = harness(tmp_path, max_file_bytes=1024)
    r = client.post("/slice", files=files(upload=b"PK\x03\x04" + b"x" * 2000), headers=AUTH)
    assert r.status_code == 422 and "large" in r.json()["detail"]
    r = client.post("/slice", files=files(json.dumps({**job(), "pad": "x" * 70_000})), headers=AUTH)
    assert r.status_code == 422 and "large" in r.json()["detail"]
    assert slicer.calls == []
    assert not any(p.is_file() for p in cfg.sliced_dir.rglob("*"))
    r = client.post("/slice", files=files(upload=b"PK\x03\x04" + b"x" * 1000), headers=AUTH)
    assert r.status_code == 200  # the lock was released after the rejections


def test_slice_failure_500_with_detail(tmp_path):
    slicer = FakeSlicer(run=SliceRun(-50, "object outside the plate", "line a\nline b"))
    cfg, slicer, pc, client = harness(tmp_path, slicer=slicer)
    r = client.post("/slice", files=files(), headers=AUTH)
    assert r.status_code == 500
    assert r.json() == {"error": "slice_failed", "detail": "object outside the plate", "log_tail": "line a\nline b"}
    slicer.run = SliceRun(3, "", "")
    r = client.post("/slice", files=files(), headers=AUTH)
    assert r.status_code == 500 and r.json()["detail"] == "exit code 3"
    slicer.run, slicer.produce = SliceRun(0, "success", "tail"), False
    r = client.post("/slice", files=files(), headers=AUTH)
    assert r.status_code == 500 and r.json()["error"] == "slice_failed" and r.json()["detail"]
    assert not any(p.is_file() for p in cfg.sliced_dir.rglob("*"))


def test_output_check_failure_500_and_file_removed(tmp_path):
    slicer = FakeSlicer(nozzle="0.6")
    cfg, slicer, pc, client = harness(tmp_path, slicer=slicer)
    r = client.post("/slice", files=files(), headers=AUTH)
    assert r.status_code == 500 and "nozzle" in r.json()["detail"]
    assert not (cfg.sliced_dir / "example-bracket-7-p1.gcode.3mf").exists()
    slicer.nozzle, slicer.plates = "0.4", (1, 2)
    r = client.post("/slice", files=files(job(kit=True, plates=[1, 3])), headers=AUTH)
    assert r.status_code == 500 and "plate 3" in r.json()["detail"]


def test_unexpected_error_is_500_without_trace(tmp_path):
    def boom(cfg, **kw):
        raise OSError("C:\\secret\\path locked")

    cfg, slicer, pc, client = harness(tmp_path, slicer=boom)
    r = client.post("/slice", files=files(), headers=AUTH)
    assert r.status_code == 500 and r.json()["error"] == "slice_failed"
    assert "secret" not in r.text and "Traceback" not in r.text
    assert client.post("/slice", files=files(), headers=AUTH).status_code == 500  # not stuck busy


def test_sliced_serves_file_and_png(tmp_path):
    cfg, slicer, pc, client = harness(tmp_path)
    path = write_sliced(cfg.sliced_dir / "kit_2-12-kit.gcode.3mf", plates=(1, 2))
    r = client.get("/sliced/kit_2-12-kit.gcode.3mf", headers=AUTH)
    assert r.status_code == 200 and r.content == path.read_bytes()
    r = client.get("/sliced/kit_2-12-kit.gcode.3mf/plate/2.png", headers=AUTH)
    assert r.status_code == 200 and r.content == PNG + bytes([2]) and r.headers["content-type"] == "image/png"
    assert client.get("/sliced/kit_2-12-kit.gcode.3mf/plate/3.png", headers=AUTH).status_code == 404
    assert client.get("/sliced/kit_2-12-kit.gcode.3mf/plate/0.png", headers=AUTH).status_code == 404
    assert client.get("/sliced/kit_2-12-kit.gcode.3mf/plate/51.png", headers=AUTH).status_code == 404
    assert client.get("/sliced/other-1-p1.gcode.3mf", headers=AUTH).status_code == 404
    (cfg.sliced_dir / "bad-1-p1.gcode.3mf").write_bytes(b"not a zip")
    assert client.get("/sliced/bad-1-p1.gcode.3mf/plate/1.png", headers=AUTH).status_code == 404


def test_sliced_path_traversal_rejected(tmp_path):
    cfg, slicer, pc, client = harness(tmp_path)
    cfg.sliced_dir.mkdir(parents=True)
    (tmp_path / "secret-1-p1.gcode.3mf").write_bytes(b"secret")
    (cfg.sliced_dir / "notes.txt").write_text("x")
    work = cfg.sliced_dir / ".work-a-1-p1"
    work.mkdir()
    (work / "a-1-p1.gcode.3mf").write_bytes(b"partial")
    for name in (
        "..%2Fsecret-1-p1.gcode.3mf", "..%5Csecret-1-p1.gcode.3mf", "%2E%2E%2Fsecret-1-p1.gcode.3mf",
        "notes.txt", ".work-a-1-p1", ".work-a-1-p1%2Fa-1-p1.gcode.3mf", "a-1-p2.gcode.3mf", "a-x-p1.gcode.3mf",
        "a-1-p1.gcode.3mf%00", "a-1-p1.gcode.3mf::$DATA", "C:%5Cx-1-p1.gcode.3mf", "-1-p1.gcode.3mf",
    ):
        r = client.get(f"/sliced/{name}", headers=AUTH)
        assert r.status_code == 404, name
        assert b"secret" not in r.content and b"partial" not in r.content
    r = client.get("/sliced/../secret-1-p1.gcode.3mf", headers=AUTH)
    assert b"secret" not in r.content


def test_lease_taken_and_dropped(tmp_path):
    cfg, slicer, pc, client = harness(tmp_path)
    assert client.post("/slice", files=files(), headers=AUTH).status_code == 200
    assert [(m, p) for m, p, _, _ in pc.requests] == [("POST", "/lease"), ("DELETE", "/lease/lease-1")]
    assert pc.requests[0][2] == {"holder": "slice-agent", "ttl_s": 600}
    assert all(auth == f"Bearer {PC_TOKEN}" for *_, auth in pc.requests)


@pytest.mark.parametrize("fail", [True, "connect"])
def test_lease_failure_does_not_block_slice(tmp_path, fail, caplog):
    cfg, slicer, pc, client = harness(tmp_path, pc=PcAgent(fail=fail))
    r = client.post("/slice", files=files(), headers=AUTH)
    assert r.status_code == 200
    assert pc.requests and pc.requests[0][1] == "/lease"
    assert all(m == "POST" for m, *_ in pc.requests)  # nothing to drop
    assert "lease" in caplog.text
    assert PC_TOKEN not in caplog.text and TOKEN not in caplog.text


async def test_lease_renews_while_held():
    pc = PcAgent()
    async with pc.client() as http:
        async with Lease(http, PC_TOKEN, renew_s=0.02):
            await asyncio.sleep(0.1)
    methods = [(m, p) for m, p, _, _ in pc.requests]
    assert methods[0] == ("POST", "/lease") and methods[-1] == ("DELETE", "/lease/lease-1")
    renews = [r for r in pc.requests if r[1] == "/lease/lease-1/renew"]
    assert renews and all(body == {"ttl_s": 600} for _, _, body, _ in renews)


async def test_lease_retakes_after_failed_take():
    calls = {"n": 0}
    pc = PcAgent()
    real = pc.handler

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return real(request)

    pc.handler = flaky
    async with pc.client() as http:
        async with Lease(http, PC_TOKEN, renew_s=0.02):
            await asyncio.sleep(0.1)
    assert ("DELETE", "/lease/lease-1") in [(m, p) for m, p, _, _ in pc.requests]


async def test_lease_ignores_unsafe_id():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(201, json={"id": "../../x", "holder": "slice-agent", "expires_in_s": 600})
        return httpx.Response(204)

    seen = []
    transport = httpx.MockTransport(lambda r: (seen.append((r.method, r.url.path)), handler(r))[1])
    async with httpx.AsyncClient(base_url="http://127.0.0.1:11435", transport=transport) as http:
        async with Lease(http, PC_TOKEN, renew_s=10):
            pass
    assert all(m == "POST" for m, _ in seen)


def test_serve_binds_listen(monkeypatch):
    seen = {}
    cfg = AgentConfig(Path("x"), Path("d"), Path("r"), Path("p"), Path("s"), slice_token=TOKEN)
    monkeypatch.setattr(app_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(app_mod.uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    assert app_mod.main(["serve"]) == 0
    assert seen["host"] == "0.0.0.0" and seen["port"] == 11436
    assert app_mod.main(["bogus"]) == 2


def test_serve_refuses_without_token(monkeypatch):
    cfg = AgentConfig(Path("x"), Path("d"), Path("r"), Path("p"), Path("s"))
    monkeypatch.setattr(app_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(app_mod.uvicorn, "run", lambda *a, **kw: pytest.fail("must not start"))
    assert app_mod.main(["serve"]) == 2


def test_part_order_does_not_matter(tmp_path):
    cfg, slicer, pc, client = harness(tmp_path)
    parts = files()
    r = client.post("/slice", files={"file": parts["file"], "job": parts["job"]}, headers=AUTH)
    assert r.status_code == 200 and slicer.calls[0]["upload"] == UPLOAD
