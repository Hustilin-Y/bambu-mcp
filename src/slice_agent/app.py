"""slice-agent entry point and HTTP API.

Subcommands: `flatten` (write the flattened presets) and `serve` (HTTP API on cfg.listen).

API (bearer SLICE_AGENT_TOKEN on everything but /health):
- POST /slice: multipart with a `file` part (3MF, max 50 MB) and a `job` part (JSON, max 64 KB).
- GET /sliced/{name}: a sliced file from sliced_dir, only names the agent itself produces.
- GET /sliced/{name}/plate/{n}.png: the plate preview inside that file.
- GET /health: {"ok": true}.
Nothing from a request reaches a path or the CLI except validated names and whitelisted overrides.
"""

from __future__ import annotations

import contextlib
import copy
import hmac
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import anyio.to_thread
import httpx
import uvicorn
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from bambu_mcp.manifest import NAME_RE
from slice_agent.config import AgentConfig, load_config
from slice_agent.lease import PC_AGENT_URL, RENEW_S, Lease
from slice_agent.output import OutputError, check_output, read_output, read_plate_png
from slice_agent.profiles import filament_tray_info, flatten_all
from slice_agent.runner import OUT_SUFFIX, Overrides, build_command, check_out_name, slice_isolated

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_JOB_BYTES = 64 * 1024
MAX_PLATES = 50
MAX_JOB_ID = 10**12
MAX_NAME = 60  # keeps sliced_dir\.work-<out>\<out>.gcode.3mf well under MAX_PATH
INCOMING = ".incoming"
ZIP_MAGIC = b"PK\x03\x04"
_BODY_SLACK = 64 * 1024  # multipart boundaries and part headers
_DETAIL_MAX = 200
_REQUIRED = ("job_id", "name", "kit", "plates", "nozzle", "process_set", "filament", "plate_type")
_KNOWN = {*_REQUIRED, "overrides"}
SLICED_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}-[0-9]{1,13}-(?:p1|kit)" + re.escape(OUT_SUFFIX))

log = logging.getLogger(__name__)

Slicer = Callable[..., tuple]


class BadRequest(Exception):
    """A request problem reported to the client as 422 with a short detail."""


def _detail(text: str) -> str:
    return text if len(text) <= _DETAIL_MAX else text[: _DETAIL_MAX - 3] + "..."


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


@dataclass(frozen=True)
class Job:
    job_id: int
    name: str
    kit: bool
    plates: tuple[int, ...]
    nozzle: str
    process_set: str
    filament: str
    plate_type: str
    overrides: Overrides

    @property
    def out_name(self) -> str:
        return f"{self.name}-{self.job_id}-{'kit' if self.kit else 'p1'}"


def parse_job(raw: bytes, cfg: AgentConfig) -> Job:
    """Validate the `job` part. Raises BadRequest with a short, client-safe detail."""
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise BadRequest("job is not valid JSON") from None
    if not isinstance(data, dict):
        raise BadRequest("job must be a JSON object")
    unknown = sorted(str(k) for k in data if k not in _KNOWN)
    if unknown:
        raise BadRequest(f"unknown job field: {unknown[0]}")
    missing = [k for k in _REQUIRED if k not in data]
    if missing:
        raise BadRequest(f"missing job field: {missing[0]}")

    job_id = data["job_id"]
    if not _is_int(job_id) or not 0 < job_id < MAX_JOB_ID:
        raise BadRequest("job_id must be a positive integer")
    name = data["name"]
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name.startswith("."):
        raise BadRequest("name must be 1..100 characters of A-Z a-z 0-9 . _ - space, not starting with a dot")
    if len(name) > MAX_NAME:
        raise BadRequest(f"name must be at most {MAX_NAME} characters")
    try:
        check_out_name(name)  # no spaces, no reserved Windows names, no trailing dot
    except ValueError:
        raise BadRequest("name is not usable as a file name on the PC") from None
    kit = data["kit"]
    if not isinstance(kit, bool):
        raise BadRequest("kit must be true or false")
    plates = data["plates"]
    if (
        not isinstance(plates, list)
        or not 0 < len(plates) <= MAX_PLATES
        or not all(_is_int(p) and 1 <= p <= MAX_PLATES for p in plates)
        or len(set(plates)) != len(plates)
    ):
        raise BadRequest(f"plates must be a list of 1..{MAX_PLATES} distinct plate numbers 1..{MAX_PLATES}")
    if not kit and plates != [1]:
        raise BadRequest("a part is sliced onto plate 1: plates must be [1]")
    names = {}
    for key in ("nozzle", "process_set", "filament", "plate_type"):
        value = data[key]
        if not isinstance(value, str) or not 0 < len(value) <= 200:
            raise BadRequest(f"{key} must be a non-empty string")
        names[key] = value
    raw_overrides = data.get("overrides", {})
    if not isinstance(raw_overrides, dict):
        raise BadRequest("overrides must be a JSON object")
    try:
        overrides = Overrides.parse(raw_overrides, names["nozzle"])
        job = Job(job_id, name, kit, tuple(plates), overrides=overrides, **names)
        # The runner's own checks: out name, nozzle/process_set/filament/plate_type against the config.
        build_command(cfg, input_3mf=Path("upload.3mf"), out_name=job.out_name, kit=kit, overrides=overrides, **names)
    except ValueError as e:
        raise BadRequest(_detail(str(e))) from None
    return job


class _Upload:
    """Streaming multipart parser: `file` goes straight to a temp file, `job` into a capped buffer."""

    def __init__(self, boundary: bytes, dest: Path, max_file: int, max_job: int) -> None:
        self.dest, self.max_file, self.max_job = dest, max_file, max_job
        self.job: bytearray | None = None
        self.path: Path | None = None
        self.size = 0
        self.head = b""
        self.ended = False
        self._fh = None
        self._part: str | None = None
        self._hname = b""
        self._hvalue = b""
        self._disposition = b""
        self._parser = MultipartParser(
            boundary,
            {
                "on_part_begin": self._part_begin,
                "on_header_field": self._header_field,
                "on_header_value": self._header_value,
                "on_header_end": self._header_end,
                "on_headers_finished": self._headers_finished,
                "on_part_data": self._part_data,
                "on_part_end": self._part_end,
                "on_end": self._end,
            },
            max_header_count=8,
            max_header_size=1024,
        )

    def _part_begin(self) -> None:
        self._part, self._disposition = None, b""

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._hname += data[start:end]

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._hvalue += data[start:end]

    def _header_end(self) -> None:
        if self._hname.lower() == b"content-disposition":
            self._disposition = self._hvalue
        self._hname, self._hvalue = b"", b""

    def _headers_finished(self) -> None:
        kind, options = parse_options_header(self._disposition)
        name = options.get(b"name")
        if kind != b"form-data" or name not in (b"file", b"job"):
            raise BadRequest("only the parts `file` and `job` are accepted")
        if name == b"file":
            if self.path is not None:
                raise BadRequest("more than one `file` part")
            fd, path = tempfile.mkstemp(prefix="upload-", suffix=".3mf", dir=self.dest)
            self.path = Path(path)
            self._fh = os.fdopen(fd, "wb")
            self._part = "file"
        else:
            if self.job is not None:
                raise BadRequest("more than one `job` part")
            self.job = bytearray()
            self._part = "job"

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if self._part == "file":
            self.size += len(chunk)
            if self.size > self.max_file:
                raise BadRequest(f"file is too large (max {self.max_file // (1024 * 1024) or 1} MB)")
            if len(self.head) < len(ZIP_MAGIC):
                self.head += chunk[: len(ZIP_MAGIC) - len(self.head)]
            self._fh.write(chunk)
        elif self._part == "job":
            if len(self.job) + len(chunk) > self.max_job:
                raise BadRequest(f"job part is too large (max {self.max_job // 1024} KB)")
            self.job.extend(chunk)

    def _part_end(self) -> None:
        if self._part == "file" and self._fh is not None:
            self._fh.close()
            self._fh = None
        self._part = None

    def _end(self) -> None:
        self.ended = True

    def write(self, chunk: bytes) -> None:
        try:
            self._parser.write(chunk)
        except BadRequest:
            raise
        except ValueError:  # python_multipart parse errors; an OSError here is ours (500)
            raise BadRequest("malformed multipart body") from None

    def finish(self) -> tuple[bytes, Path]:
        try:
            self._parser.finalize()
        except ValueError:
            raise BadRequest("malformed multipart body") from None
        if not self.ended:
            raise BadRequest("incomplete multipart body")
        if self.job is None:
            raise BadRequest("missing `job` part")
        if self.path is None:
            raise BadRequest("missing `file` part")
        if self.size == 0 or self.head != ZIP_MAGIC:
            raise BadRequest("file is not a 3MF (zip) file")
        return bytes(self.job), self.path

    def discard(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        if self.path is not None:
            with contextlib.suppress(OSError):  # e.g. a scanner still holds it: .incoming is cleared next time
                self.path.unlink(missing_ok=True)


def _clear_incoming(incoming: Path) -> None:
    """Remove uploads left by a crashed run. Called only while holding the slice lock."""
    incoming.mkdir(parents=True, exist_ok=True)
    for stale in incoming.iterdir():
        if stale.is_file():
            with contextlib.suppress(OSError):
                stale.unlink(missing_ok=True)


class Redactor:
    """Replace the PC's own paths in CLI text with placeholders before it leaves the agent."""

    def __init__(self, cfg: AgentConfig, home: Path | None = None, ignore_case: bool | None = None) -> None:
        home = home or Path(os.environ.get("USERPROFILE") or Path.home())
        ignore_case = sys.platform == "win32" if ignore_case is None else ignore_case
        named = [
            (cfg.profiles_dir, "<profiles>"),
            (cfg.studio_data, "<studio-data>"),
            (cfg.studio_resources, "<studio-resources>"),
            (cfg.studio_exe.parent, "<studio>"),
            (cfg.sliced_dir, "<sliced>"),
            (home, "~"),
        ]
        self._rules = []
        for path, placeholder in sorted(named, key=lambda item: len(str(item[0])), reverse=True):
            parts = [p for p in re.split(r"[\\/]+", str(path)) if p]
            if len(parts) < 2:
                continue  # ".", "/" or "C:" alone would match far too much
            lead = r"[\\/]+" if re.match(r"[\\/]", str(path)) else ""
            pattern = lead + r"[\\/]+".join(re.escape(p) for p in parts) + r"(?![A-Za-z0-9_.-])"
            self._rules.append((re.compile(pattern, re.IGNORECASE if ignore_case else 0), placeholder))

    def __call__(self, text: str) -> str:
        for pattern, placeholder in self._rules:
            text = pattern.sub(lambda _m, p=placeholder: p, text)
        return text


def _error(status: int, error: str, **extra) -> JSONResponse:
    return JSONResponse({"error": error, **extra}, status_code=status)


class SliceAgent:
    def __init__(
        self,
        cfg: AgentConfig,
        *,
        slicer: Slicer,
        pc_agent: httpx.AsyncClient,
        lease_renew_s: float,
        max_file_bytes: int,
        max_job_bytes: int,
    ) -> None:
        self.cfg = cfg
        self.slicer = slicer
        self.pc_agent = pc_agent
        self.lease_renew_s = lease_renew_s
        self.max_file_bytes = max_file_bytes
        self.max_job_bytes = max_job_bytes
        self.busy = threading.Lock()
        self.redact = Redactor(cfg)

    def authorized(self, request: Request) -> bool:
        expected = self.cfg.slice_token
        scheme, _, given = request.headers.get("authorization", "").partition(" ")
        if not expected or scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(given.strip().encode(), expected.encode())

    @staticmethod
    def unauthorized() -> JSONResponse:
        return JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})

    async def health(self, request: Request) -> Response:
        return JSONResponse({"ok": True})

    async def slice(self, request: Request) -> Response:
        if not self.authorized(request):
            return self.unauthorized()
        if not self.busy.acquire(blocking=False):
            return _error(409, "busy")
        try:
            return await self._slice_locked(request)
        finally:
            self.busy.release()

    async def _read_upload(self, request: Request) -> _Upload:
        kind, options = parse_options_header(request.headers.get("content-type", ""))
        boundary = options.get(b"boundary")
        if kind != b"multipart/form-data" or not boundary:
            raise BadRequest("expected multipart/form-data")
        limit = self.max_file_bytes + self.max_job_bytes + _BODY_SLACK
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            raise BadRequest("request is too large")
        incoming = self.cfg.sliced_dir / INCOMING
        _clear_incoming(incoming)
        upload = _Upload(boundary, incoming, self.max_file_bytes, self.max_job_bytes)
        total = 0
        try:
            async for chunk in request.stream():
                total += len(chunk)
                if total > limit:
                    raise BadRequest("request is too large")
                upload.write(chunk)
        except BaseException:
            upload.discard()
            raise
        return upload

    async def _slice_locked(self, request: Request) -> Response:
        upload = None
        try:
            upload = await self._read_upload(request)
            raw_job, path = upload.finish()
            job = parse_job(raw_job, self.cfg)
        except BadRequest as e:
            if upload is not None:
                upload.discard()
            log.info("slice request refused: %s", _detail(str(e)))
            return _error(422, "bad_request", detail=_detail(str(e)))
        except Exception:
            if upload is not None:
                upload.discard()
            log.exception("slice upload failed unexpectedly")
            return _error(500, "slice_failed", detail="internal error in slice-agent", log_tail="")
        try:
            t0 = time.monotonic()
            log.info("slice job %s -> %s started (%d bytes)", job.job_id, job.out_name, upload.size)
            async with Lease(self.pc_agent, self.cfg.pc_agent_token, renew_s=self.lease_renew_s):
                status, body = await anyio.to_thread.run_sync(self._slice_sync, job, path)
            log.info(
                "slice job %s finished: HTTP %s in %.1f s (%s)",
                job.job_id, status, time.monotonic() - t0, body.get("file") or body.get("detail"),
            )
            return JSONResponse(body, status_code=status)
        except Exception:
            log.exception("slice job %s failed unexpectedly", job.job_id)
            return _error(500, "slice_failed", detail="internal error in slice-agent", log_tail="")
        finally:
            upload.discard()

    def _failed(self, detail: str, log_tail: str) -> dict:
        return {"error": "slice_failed", "detail": _detail(self.redact(detail)), "log_tail": self.redact(log_tail)}

    def _slice_sync(self, job: Job, upload: Path) -> tuple[int, dict]:
        run, final = self.slicer(
            self.cfg,
            out_name=job.out_name,
            input_3mf=upload,
            nozzle=job.nozzle,
            process_set=job.process_set,
            filament=job.filament,
            plate_type=job.plate_type,
            kit=job.kit,
            overrides=job.overrides,
        )
        if final is None:
            if run.error_string and run.error_string.lower() != "success":
                detail = run.error_string
            elif run.return_code:
                detail = f"exit code {run.return_code}"
            else:
                detail = "the slicer produced no output file"
            return 500, self._failed(detail, run.log_tail)
        try:
            result = read_output(final, job.plates[0])
            check_output(result, job.nozzle)
            present = {p.index for p in result.plates}
            for plate in job.plates:
                if plate not in present:
                    raise OutputError(f"plate {plate} is not in the sliced file")
        except OutputError as e:
            final.unlink(missing_ok=True)  # never leave a file that failed the checks to be printed
            return 500, self._failed(str(e), run.log_tail)
        wanted = set(job.plates)
        plates = [
            {
                "index": p.index,
                "minutes": round(p.minutes, 2),
                "grams": {k: round(v, 2) for k, v in p.grams_by_filament.items()},
            }
            for p in result.plates
            if p.index in wanted
        ]
        return 200, {"file": final.name, "plates": plates, "slicer_version": result.slicer_version}

    def _sliced_path(self, name: str) -> Path | None:
        if not SLICED_RE.fullmatch(name):
            return None
        root = self.cfg.sliced_dir.resolve()
        path = (root / name).resolve()
        if path.parent != root or not path.is_file():
            return None
        return path

    async def sliced(self, request: Request) -> Response:
        if not self.authorized(request):
            return self.unauthorized()
        path = self._sliced_path(request.path_params["name"])
        if path is None:
            return _error(404, "not_found")
        return FileResponse(path, media_type="application/octet-stream")

    async def plate_png(self, request: Request) -> Response:
        if not self.authorized(request):
            return self.unauthorized()
        path = self._sliced_path(request.path_params["name"])
        n = request.path_params["n"]
        if path is None or not 1 <= n <= MAX_PLATES:
            return _error(404, "not_found")
        try:
            png = await anyio.to_thread.run_sync(read_plate_png, path, n)
        except OutputError:
            png = None
        if png is None:
            return _error(404, "not_found")
        return Response(png, media_type="image/png")


def create_app(
    cfg: AgentConfig,
    *,
    slicer: Slicer = slice_isolated,
    pc_agent: httpx.AsyncClient | None = None,
    lease_renew_s: float = RENEW_S,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_job_bytes: int = MAX_JOB_BYTES,
) -> Starlette:
    http = pc_agent or httpx.AsyncClient(base_url=PC_AGENT_URL, timeout=5.0, trust_env=False)
    agent = SliceAgent(
        cfg, slicer=slicer, pc_agent=http, lease_renew_s=lease_renew_s,
        max_file_bytes=max_file_bytes, max_job_bytes=max_job_bytes,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await http.aclose()

    return Starlette(
        routes=[
            Route("/health", agent.health, methods=["GET"]),
            Route("/slice", agent.slice, methods=["POST"]),
            Route("/sliced/{name}", agent.sliced, methods=["GET"]),
            Route("/sliced/{name}/plate/{n:int}.png", agent.plate_png, methods=["GET"]),
        ],
        lifespan=lifespan,
    )


def _log_config() -> dict:
    config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    config["loggers"]["slice_agent"] = {"handlers": ["default"], "level": "INFO", "propagate": False}
    return config


def serve(cfg: AgentConfig) -> int:
    if not cfg.slice_token:
        print("SLICE_AGENT_TOKEN is not set: refusing to start", file=sys.stderr)
        return 2
    if not cfg.pc_agent_token:
        print("warning: PC_AGENT_TOKEN is not set, the PC may sleep during a slice", file=sys.stderr)
    host, port = cfg.listen
    uvicorn.run(
        create_app(cfg),
        host=host,
        port=port,
        log_config=_log_config(),
        server_header=False,
        proxy_headers=False,
        limit_concurrency=16,
    )
    return 0


def flatten(cfg: AgentConfig) -> int:
    paths = flatten_all(cfg)
    for path in paths:
        print(f"wrote {path}")
    print("assertions OK")
    # The owner copies these into the server's spool records (add_spool / update_spool).
    for path in paths:
        if not path.name.startswith("filament-"):
            continue
        preset = json.loads(path.read_text(encoding="utf-8"))
        info = filament_tray_info(preset)
        fields = " ".join(f"{k}={'?' if v is None else v}" for k, v in info.items())
        print(f"filament {preset.get('name')}: {fields}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    commands = {"flatten": flatten, "serve": serve}
    if len(args) != 1 or args[0] not in commands:
        print("usage: slice-agent flatten | serve", file=sys.stderr)
        return 2
    return commands[args[0]](load_config())


if __name__ == "__main__":
    raise SystemExit(main())
