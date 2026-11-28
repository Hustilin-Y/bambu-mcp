import hashlib
import json
from pathlib import Path

import httpx
import pytest

from bambu_mcp import upload

FIX = Path(__file__).parent / "fixtures" / "manifest"
THREE_MF = b"PK\x03\x04fake-3mf-body"
TOKEN = "secret-upload-token"


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    data = json.loads((FIX / "part.print.json").read_text(encoding="utf-8"))
    data["file_sha256"] = hashlib.sha256(THREE_MF).hexdigest()
    (tmp_path / "lamp-arm.3mf").write_bytes(THREE_MF)
    path = tmp_path / "lamp-arm.print.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.delenv("BAMBU_CONFIG", raising=False)
    monkeypatch.setenv("BAMBU_MCP_TOKEN", TOKEN)


class Recorder:
    def __init__(self, status: int = 201, body: dict | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status
        self.body = {"job_id": 42, "name": "lamp-arm", "kind": "part", "status": "new"} if body is None else body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _one_line(text: str) -> str:
    lines = text.strip().splitlines()
    assert len(lines) == 1, text
    return lines[0]


def test_upload_cli_posts_multipart(manifest, capsys):
    rec = Recorder()
    code = upload.main(
        ["upload", str(manifest), "--url", "http://192.0.2.10:8765/"], transport=rec.transport
    )
    out = capsys.readouterr()
    assert code == 0
    assert out.out == "42\n"
    (req,) = rec.requests
    assert req.method == "POST"
    assert str(req.url) == "http://192.0.2.10:8765/jobs"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert req.headers["content-type"].startswith("multipart/form-data; boundary=")
    body = req.content
    assert b'name="manifest"' in body and b'name="file"' in body
    assert manifest.read_bytes() in body
    assert THREE_MF in body
    assert TOKEN not in out.out + out.err


def test_upload_cli_token_env_name(manifest, monkeypatch, capsys):
    monkeypatch.setenv("OTHER_TOKEN", "other-value")
    rec = Recorder()
    code = upload.main(
        ["upload", str(manifest), "--url", "http://h:1", "--token-env", "OTHER_TOKEN"],
        transport=rec.transport,
    )
    assert code == 0
    assert rec.requests[0].headers["authorization"] == "Bearer other-value"


def test_upload_cli_missing_token_fails(manifest, monkeypatch, capsys):
    monkeypatch.delenv("BAMBU_MCP_TOKEN")
    rec = Recorder()
    code = upload.main(["upload", str(manifest), "--url", "http://h:1"], transport=rec.transport)
    assert code == 1
    assert "BAMBU_MCP_TOKEN" in _one_line(capsys.readouterr().err)
    assert rec.requests == []


def test_upload_cli_has_no_token_argument(manifest, capsys):
    code = upload.main(["upload", str(manifest), "--url", "http://h:1", "--token", TOKEN])
    assert code == 1
    err = capsys.readouterr().err
    _one_line(err)
    assert TOKEN not in err


def test_upload_cli_token_passed_as_env_name_is_not_echoed(manifest, capsys):
    code = upload.main(["upload", str(manifest), "--url", "http://h:1", "--token-env", TOKEN])
    assert code == 1
    assert TOKEN not in _one_line(capsys.readouterr().err)


def test_upload_cli_token_as_subcommand_is_not_echoed(capsys):
    code = upload.main([TOKEN])
    assert code == 1
    line = _one_line(capsys.readouterr().err)
    assert TOKEN not in line and "invalid choice" in line


def test_upload_cli_bad_option_value_is_not_echoed(capsys):
    code = upload.main(["upload", TOKEN, "--url"])  # --url without its value
    assert code == 1
    assert TOKEN not in _one_line(capsys.readouterr().err)


@pytest.mark.parametrize(
    "url", ["http://[::1", "ftp://h:1", "http://", "not a url", "http://h:99999"]
)
def test_upload_cli_malformed_url_one_line(manifest, capsys, url):
    rec = Recorder()
    code = upload.main(["upload", str(manifest), "--url", url], transport=rec.transport)
    assert code == 1
    assert "URL" in _one_line(capsys.readouterr().err)
    assert rec.requests == []


def test_upload_cli_malformed_configured_url_one_line(manifest, tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.toml"
    config.write_text('service_url = "http://[::1"\n', encoding="utf-8")
    monkeypatch.setenv("BAMBU_CONFIG", str(config))
    code = upload.main(["upload", str(manifest)], transport=Recorder().transport)
    assert code == 1
    _one_line(capsys.readouterr().err)


def test_upload_cli_url_required_without_config(manifest, capsys):
    rec = Recorder()
    code = upload.main(["upload", str(manifest)], transport=rec.transport)
    assert code == 1
    assert "--url" in _one_line(capsys.readouterr().err)
    assert rec.requests == []


def test_upload_cli_url_from_config(manifest, tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.toml"
    config.write_text('service_url = "http://127.0.0.1:8765"\n', encoding="utf-8")
    monkeypatch.setenv("BAMBU_CONFIG", str(config))
    rec = Recorder()
    assert upload.main(["upload", str(manifest)], transport=rec.transport) == 0
    assert str(rec.requests[0].url) == "http://127.0.0.1:8765/jobs"


def test_upload_cli_refuses_changed_file_before_sending(manifest, capsys):
    (manifest.parent / "lamp-arm.3mf").write_bytes(b"changed")
    rec = Recorder()
    code = upload.main(["upload", str(manifest), "--url", "http://h:1"], transport=rec.transport)
    assert code == 1
    assert "sha256" in _one_line(capsys.readouterr().err)
    assert rec.requests == []


def test_upload_cli_missing_3mf(manifest, capsys):
    (manifest.parent / "lamp-arm.3mf").unlink()
    code = upload.main(["upload", str(manifest), "--url", "http://h:1"], transport=Recorder().transport)
    assert code == 1
    assert "lamp-arm.3mf" in _one_line(capsys.readouterr().err)


def test_upload_cli_server_error_one_line(manifest, capsys):
    rec = Recorder(status=422, body={"error": "bad_request", "detail": "lamp-arm.3mf does not match"})
    code = upload.main(["upload", str(manifest), "--url", "http://h:1"], transport=rec.transport)
    assert code == 1
    line = _one_line(capsys.readouterr().err)
    assert "422" in line and "does not match" in line


def test_upload_cli_unauthorized(manifest, capsys):
    rec = Recorder(status=401, body={"error": "unauthorized"})
    code = upload.main(["upload", str(manifest), "--url", "http://h:1"], transport=rec.transport)
    assert code == 1
    err = _one_line(capsys.readouterr().err)
    assert "401" in err and TOKEN not in err


def test_upload_cli_connection_error(manifest, capsys):
    def refuse(request):
        raise httpx.ConnectError("connection refused")

    code = upload.main(
        ["upload", str(manifest), "--url", "http://h:1"], transport=httpx.MockTransport(refuse)
    )
    assert code == 1
    _one_line(capsys.readouterr().err)


def test_upload_cli_bad_response_body(manifest, capsys):
    rec = Recorder(status=201, body={"unexpected": True})
    code = upload.main(["upload", str(manifest), "--url", "http://h:1"], transport=rec.transport)
    assert code == 1
    _one_line(capsys.readouterr().err)
