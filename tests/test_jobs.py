import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from bambu_mcp import jobs
from bambu_mcp.config import Secrets, Settings
from bambu_mcp.jobs import (
    JobError,
    JobTooLarge,
    create_job_from_bytes,
    create_job_from_path,
    job_dir,
    load_job_manifest,
    read_job_file,
)
from bambu_mcp.mcp_server import build_app
from bambu_mcp.store.db import Store

FIX = Path(__file__).parent / "fixtures" / "manifest"
THREE_MF = b"PK\x03\x04fake-3mf-body" + bytes(range(256))
SECRETS = Secrets(
    access_code="87654321",
    serial="01P00A000000001",
    host="192.0.2.7",
    client_tokens={"pc": "pc-token-abc"},
)
BASE_URL = "http://127.0.0.1:8765"


def manifest_bytes(body: bytes = THREE_MF, **changes) -> bytes:
    data = json.loads((FIX / "part.print.json").read_text(encoding="utf-8"))
    data["file_sha256"] = hashlib.sha256(body).hexdigest()
    data.update(changes)
    return json.dumps(data).encode("utf-8")


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "data" / "bambu.db")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


def _job_dirs(settings: Settings) -> list[str]:
    root = settings.data_dir / "jobs"
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if not p.name.startswith("."))


def _incoming_left(settings: Settings) -> list[Path]:
    incoming = settings.data_dir / "jobs" / ".incoming"
    return list(incoming.iterdir()) if incoming.exists() else []


# -- bytes intake --------------------------------------------------------------------


def test_create_job_stores_files_and_row(store, settings):
    raw = manifest_bytes()
    job = create_job_from_bytes(store, settings, raw, THREE_MF)

    assert job.id >= 1
    assert (job.name, job.kind, job.status) == ("lamp-arm", "part", "new")
    assert job.file_sha256 == hashlib.sha256(THREE_MF).hexdigest()
    assert json.loads(job.manifest_json)["name"] == "lamp-arm"
    assert isinstance(job.created_at, datetime) and job.created_at.tzinfo is not None
    assert (job.settings_json, job.result_json, job.sliced_file, job.error) == (None, None, None, None)

    folder = job_dir(settings, job.id)
    assert folder == settings.data_dir / "jobs" / str(job.id)
    assert sorted(p.name for p in folder.iterdir()) == ["lamp-arm.3mf", "manifest.json"]
    assert (folder / "manifest.json").read_bytes() == raw
    assert read_job_file(settings, job) == THREE_MF
    assert load_job_manifest(settings, job).name == "lamp-arm"
    assert store.job(job.id) == job
    assert _incoming_left(settings) == []


def test_job_name_is_the_manifest_name_unchanged(store, settings):
    job = create_job_from_bytes(store, settings, manifest_bytes(name="Lamp Arm v2"), THREE_MF)
    assert job.name == "Lamp Arm v2"


def test_sha_mismatch_refused(store, settings):
    with pytest.raises(JobError) as exc:
        create_job_from_bytes(store, settings, manifest_bytes(), THREE_MF + b"changed")
    assert "sha256" in str(exc.value)
    assert str(settings.data_dir) not in str(exc.value)
    assert store.jobs(10) == []
    assert _job_dirs(settings) == []
    assert _incoming_left(settings) == []


@pytest.mark.parametrize(
    "raw",
    [b"{not json", b"\xff\xfe", json.dumps({"schema": 2}).encode()],
)
def test_bad_manifest_refused_without_paths(store, settings, raw):
    with pytest.raises(JobError) as exc:
        create_job_from_bytes(store, settings, raw, THREE_MF)
    assert str(settings.data_dir) not in str(exc.value)
    assert ".incoming" not in str(exc.value)
    assert store.jobs(10) == []
    assert _incoming_left(settings) == []


def test_manifest_too_large_refused(store, settings):
    raw = manifest_bytes()
    padded = raw[:-1] + b"," + b'"pad": "' + b"x" * (1024 * 1024) + b'"}'
    with pytest.raises(JobTooLarge):
        create_job_from_bytes(store, settings, padded, THREE_MF)


def test_file_too_large_refused(store, settings, monkeypatch):
    monkeypatch.setattr(jobs, "MAX_FILE_BYTES", 100)
    with pytest.raises(JobTooLarge):
        create_job_from_bytes(store, settings, manifest_bytes(), THREE_MF)
    assert store.jobs(10) == []


@pytest.mark.parametrize("name", ["manifest.json", "Manifest.JSON", "manifest.json. "])
def test_manifest_file_may_not_shadow_manifest_json(store, settings, name):
    with pytest.raises(JobError, match="manifest.json"):
        create_job_from_bytes(store, settings, manifest_bytes(file=name), THREE_MF)


def test_rename_failure_leaves_no_row_and_no_files(store, settings, monkeypatch):
    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(jobs.os, "replace", boom)
    with pytest.raises(OSError):
        create_job_from_bytes(store, settings, manifest_bytes(), THREE_MF)
    assert store.jobs(10) == []
    assert _job_dirs(settings) == []
    assert _incoming_left(settings) == []


def test_write_failure_leaves_no_row(store, settings, monkeypatch):
    real = Path.write_bytes

    def failing(self, data):
        if self.suffix == ".3mf":
            raise OSError("disk full")
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", failing)
    with pytest.raises(OSError):
        create_job_from_bytes(store, settings, manifest_bytes(), THREE_MF)
    assert store.jobs(10) == []
    assert _incoming_left(settings) == []


def test_orphan_job_folder_is_replaced(store, settings):
    # A folder under the next id with no row (crash between rename and commit).
    orphan = settings.data_dir / "jobs" / "1"
    orphan.mkdir(parents=True)
    (orphan / "stale.bin").write_bytes(b"old")
    job = create_job_from_bytes(store, settings, manifest_bytes(), THREE_MF)
    assert job.id == 1
    assert sorted(p.name for p in job_dir(settings, 1).iterdir()) == ["lamp-arm.3mf", "manifest.json"]


def test_stale_incoming_entries_are_cleared(store, settings):
    incoming = settings.data_dir / "jobs" / ".incoming"
    incoming.mkdir(parents=True)
    old = incoming / "upload-old.3mf"
    old.write_bytes(b"x")
    fresh = incoming / "upload-fresh.3mf"
    fresh.write_bytes(b"y")
    long_ago = old.stat().st_mtime - 2 * 3600
    os.utime(old, (long_ago, long_ago))
    create_job_from_bytes(store, settings, manifest_bytes(), THREE_MF)
    assert not old.exists()
    assert fresh.exists()  # may belong to an upload in progress


# -- path intake ---------------------------------------------------------------------


def _write_job(folder: Path, body: bytes = THREE_MF) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "lamp-arm.3mf").write_bytes(body)
    path = folder / "lamp-arm.print.json"
    path.write_bytes(manifest_bytes(body))
    return path


def test_path_intake_reads_file_next_to_manifest(store, settings, tmp_path):
    root = tmp_path / "cad" / "out"
    path = _write_job(root / "lamp")
    job = create_job_from_path(store, settings, path, root)
    assert job.status == "new" and read_job_file(settings, job) == THREE_MF
    # a path relative to the allowed root works too
    job2 = create_job_from_path(store, settings, Path("lamp/lamp-arm.print.json"), root)
    assert job2.id == job.id + 1


def test_path_outside_root_refused(store, settings, tmp_path):
    root = tmp_path / "cad" / "out"
    root.mkdir(parents=True)
    outside = _write_job(tmp_path / "elsewhere")
    for candidate in (outside, root / ".." / ".." / "elsewhere" / "lamp-arm.print.json"):
        with pytest.raises(JobError) as exc:
            create_job_from_path(store, settings, candidate, root)
        assert "outside" in str(exc.value)
        assert str(tmp_path) not in str(exc.value)
    assert store.jobs(10) == []


def test_missing_manifest_refused_without_path(store, settings, tmp_path):
    root = tmp_path / "cad"
    root.mkdir()
    with pytest.raises(JobError) as exc:
        create_job_from_path(store, settings, root / "nope.print.json", root)
    assert str(tmp_path) not in str(exc.value)


def _link_dir(link: Path, target: Path) -> None:
    """A directory symlink, or a junction on Windows without the symlink privilege."""
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        import _winapi

        _winapi.CreateJunction(str(target), str(link))


def test_symlink_escape_refused(store, settings, tmp_path):
    root = tmp_path / "cad"
    root.mkdir()
    _write_job(tmp_path / "secret")
    _link_dir(root / "link", tmp_path / "secret")
    with pytest.raises(JobError, match="outside"):
        create_job_from_path(store, settings, root / "link" / "lamp-arm.print.json", root)
    assert store.jobs(10) == []


def test_file_symlink_escape_refused(store, settings, tmp_path):
    root = tmp_path / "cad"
    folder = root / "lamp"
    folder.mkdir(parents=True)
    (tmp_path / "secret.3mf").write_bytes(THREE_MF)
    try:
        os.symlink(tmp_path / "secret.3mf", folder / "lamp-arm.3mf")
    except OSError:
        pytest.skip("no file symlink privilege on this machine")
    (folder / "lamp-arm.print.json").write_bytes(manifest_bytes())
    with pytest.raises(JobError, match="outside"):
        create_job_from_path(store, settings, folder / "lamp-arm.print.json", root)


def test_link_inside_root_is_allowed(store, settings, tmp_path):
    root = tmp_path / "cad"
    _write_job(root / "real")
    _link_dir(root / "alias", root / "real")
    job = create_job_from_path(store, settings, root / "alias" / "lamp-arm.print.json", root)
    assert job.status == "new"


# -- store -----------------------------------------------------------------------------


def test_update_job_fields_and_status_values(store, settings):
    job = create_job_from_bytes(store, settings, manifest_bytes(), THREE_MF)
    store.update_job(job.id, status="slicing", settings_json='{"wall_loops": 4}')
    store.update_job(job.id, status="sliced", result_json="{}", sliced_file="lamp-arm-1-p1.gcode.3mf")
    got = store.job(job.id)
    assert (got.status, got.settings_json, got.sliced_file) == ("sliced", '{"wall_loops": 4}', "lamp-arm-1-p1.gcode.3mf")
    store.update_job(job.id, status="slice_failed", error="PC did not wake")
    assert store.job(job.id).error == "PC did not wake"
    with pytest.raises(ValueError):
        store.update_job(job.id, status="printing")
    with pytest.raises(ValueError):
        store.update_job(job.id, name="other")
    with pytest.raises(KeyError):
        store.update_job(999, status="new")
    assert store.job(999) is None


def test_jobs_newest_first(store, settings):
    a = create_job_from_bytes(store, settings, manifest_bytes(), THREE_MF)
    b = create_job_from_bytes(store, settings, manifest_bytes(name="other"), THREE_MF)
    assert [j.id for j in store.jobs(10)] == [b.id, a.id]
    assert [j.id for j in store.jobs(1)] == [b.id]


# -- POST /jobs -------------------------------------------------------------------------


class _Service:
    state = None
    link_status = None


@pytest.fixture
def client(store, settings):
    app = build_app(_Service(), store, SECRETS, settings, camera=_no_camera, max_job_file_bytes=4096)
    with TestClient(app, base_url=BASE_URL) as c:
        yield c


async def _no_camera():
    return None


def _post(client, files, token: str | None = "pc-token-abc"):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return client.post("/jobs", files=files, headers=headers)


def _parts(raw: bytes | None = None, body: bytes = THREE_MF) -> list:
    return [
        ("manifest", ("whatever.json", manifest_bytes(body) if raw is None else raw, "application/json")),
        ("file", ("../../evil name.3mf", body, "application/octet-stream")),
    ]


def test_jobs_route_requires_token(client, store):
    for token in (None, "wrong", ""):
        r = _post(client, _parts(), token=token)
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"
    assert store.jobs(10) == []
    r = _post(client, _parts())
    assert r.status_code == 201, r.text
    assert r.json() == {"job_id": 1, "name": "lamp-arm", "kind": "part", "status": "new"}


def test_jobs_route_stores_under_manifest_file_name(client, store, settings):
    r = _post(client, _parts())
    assert r.status_code == 201
    job = store.job(r.json()["job_id"])
    assert sorted(p.name for p in job_dir(settings, job.id).iterdir()) == ["lamp-arm.3mf", "manifest.json"]
    assert _incoming_left(settings) == []


def test_jobs_route_sha_mismatch_is_422_without_paths(client, store, settings):
    r = _post(client, _parts(raw=manifest_bytes(b"other bytes")))
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "bad_request" and "sha256" in body["detail"]
    assert str(settings.data_dir) not in r.text and ".incoming" not in r.text
    assert store.jobs(10) == []
    assert _incoming_left(settings) == []


def test_jobs_route_bad_manifest_is_422(client, store):
    r = _post(client, _parts(raw=b"{broken"))
    assert r.status_code == 422 and r.json()["error"] == "bad_request"


def test_jobs_route_file_too_large_is_413(client, store, settings):
    big = b"PK\x03\x04" + b"x" * 5000
    r = _post(client, _parts(body=big))
    assert r.status_code == 413
    assert r.json()["error"] == "too_large"
    assert store.jobs(10) == []
    assert _incoming_left(settings) == []


def test_jobs_route_declared_length_too_large_is_413(client, store):
    r = client.post(
        "/jobs",
        content=b"x",
        headers={
            "Authorization": "Bearer pc-token-abc",
            "Content-Type": "multipart/form-data; boundary=abc",
            "Content-Length": str(10**9),
        },
    )
    assert r.status_code == 413


def test_jobs_route_manifest_too_large_is_413(client, store):
    raw = b'{"pad": "' + b"x" * (1024 * 1024 + 10) + b'"}'
    r = _post(client, _parts(raw=raw))
    assert r.status_code == 413


@pytest.mark.parametrize(
    "parts",
    [
        lambda p: p[:1],  # no file
        lambda p: p[1:],  # no manifest
        lambda p: p + p[:1],  # manifest twice
        lambda p: p + p[1:],  # file twice
        lambda p: p + [("extra", ("x", b"1", "text/plain"))],
    ],
    ids=["no-file", "no-manifest", "manifest-twice", "file-twice", "extra-part"],
)
def test_jobs_route_parts_exactly_once(client, store, settings, parts):
    r = _post(client, parts(_parts()))
    assert r.status_code == 422, r.text
    assert r.json()["error"] == "bad_request"
    assert store.jobs(10) == []
    assert _incoming_left(settings) == []


def test_jobs_route_needs_multipart(client):
    r = client.post("/jobs", json={"a": 1}, headers={"Authorization": "Bearer pc-token-abc"})
    assert r.status_code == 422
