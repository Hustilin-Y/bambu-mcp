"""The Print tab's client of bambu-service, stdlib only (design 2026-11-28, 6.4)."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bambu.client import STATE_MAX, AppClient, ServiceDown, load_config, poll_every, tab_state

TOKEN = "app-token-secret-123"


@pytest.fixture
def server():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _answer(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            seen.append(("GET", self.path, self.headers.get("Authorization"), None))
            if self.path == "/v1/snapshot":
                self._answer(200, {"status": {"gcode_state": "IDLE"}})
            elif self.path == "/v1/boom":
                self._answer(500, {"error": "internal"})
            else:
                self._answer(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"null")
            seen.append(("POST", self.path, self.headers.get("Authorization"), body))
            self._answer(409 if self.path == "/v1/pause" else 202, {"error": "busy"} if self.path == "/v1/pause" else {"pending": True})

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    httpd.seen = seen
    httpd.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield httpd
    httpd.shutdown()


def test_snapshot_sends_bearer(server):
    assert AppClient(server.url, TOKEN).snapshot() == {"status": {"gcode_state": "IDLE"}}
    assert server.seen[0] == ("GET", "/v1/snapshot", f"Bearer {TOKEN}", None)


def test_post_returns_status_and_body(server):
    client = AppClient(server.url, TOKEN)
    assert client.post("/v1/prepare", {"job_id": 7, "plate": 1}) == (202, {"pending": True})
    assert client.post("/v1/pause") == (409, {"error": "busy"})
    assert server.seen[0][3] == {"job_id": 7, "plate": 1}


def test_service_down_message_has_no_token(server):
    with pytest.raises(ServiceDown) as info:
        AppClient("http://127.0.0.1:1", TOKEN, timeout=1).snapshot()
    assert TOKEN not in str(info.value) and "127.0.0.1" not in str(info.value)
    with pytest.raises(ServiceDown):
        AppClient(server.url, TOKEN)._get("/v1/boom")


def test_load_config(tmp_path):
    path = tmp_path / "miniapp.json"
    assert load_config(path) is None
    path.write_text(json.dumps({"url": "http://127.0.0.1:8765", "token": TOKEN}))
    os.chmod(path, 0o600)
    assert load_config(path) == ("http://127.0.0.1:8765", TOKEN)
    path.write_text("not json")
    assert load_config(path) is None


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")
def test_load_config_refuses_group_readable(tmp_path):
    path = tmp_path / "miniapp.json"
    path.write_text(json.dumps({"url": "http://127.0.0.1:8765", "token": TOKEN}))
    os.chmod(path, 0o640)
    assert load_config(path) is None


def _snapshot(n_jobs=3, n_history=3, name_len=10):
    return {
        "status": {"gcode_state": "RUNNING"},
        "printer": {"stoppable_states": ["PAUSE", "PREPARE", "RUNNING"]},
        "jobs": [{"id": i, "name": "j" * name_len} for i in range(n_jobs)],
        "history": [{"name": "h" * name_len} for _ in range(n_history)],
    }


def test_tab_state_ok_and_down():
    snap = _snapshot()
    ok = tab_state(snap, 100.0, 101.0, None)
    assert ok == {"snapshot": snap, "updated": 100.0, "down": None}
    down = tab_state(snap, 100.0, 200.0, "timeout")
    assert down["snapshot"] == snap and down["down"].startswith("Сервис принтера не отвечает, данные на ")
    assert tab_state(None, None, 200.0, "timeout")["down"] == "Сервис принтера не отвечает"


def test_tab_state_cut():
    state = tab_state(_snapshot(n_jobs=200, n_history=200, name_len=400), 1.0, 1.0, None)
    assert len(json.dumps(state, ensure_ascii=False).encode()) <= STATE_MAX
    assert state["snapshot"]["history"] == [] and state["snapshot"]["jobs"][0]["id"] == 0


def test_poll_every_follows_profile():
    assert poll_every(_snapshot()) == 5
    idle = _snapshot()
    idle["status"]["gcode_state"] = "IDLE"
    assert poll_every(idle) == 30
    a1_like = {"status": {"gcode_state": "PREPARE"}, "printer": {"stoppable_states": ["RUNNING"]}}
    assert poll_every(a1_like) == 30
    assert poll_every(None) == 30
