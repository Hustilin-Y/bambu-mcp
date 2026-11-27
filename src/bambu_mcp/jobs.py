"""Print jobs: intake of a CAD Workshop manifest and its 3MF, and the job's files.

A job's files live in ``data_dir/jobs/<id>/`` as ``manifest.json`` and the 3MF under
the manifest's ``file`` name. They are written into a staging folder under
``data_dir/jobs/.incoming/`` and renamed into place in the same transaction that
inserts the row, so neither a row without files nor a half written file under the
final name survives a failure.

Error messages (``JobError``) are shown to clients: they never contain server paths.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from python_multipart.multipart import MultipartParser, parse_options_header

from bambu_mcp.config import Settings
from bambu_mcp.manifest import (
    MAX_MANIFEST_BYTES,
    Manifest,
    ManifestError,
    load_manifest,
    verify_file,
)
from bambu_mcp.store.db import Store
from bambu_mcp.store.models import Job

log = logging.getLogger(__name__)

# The service holds a job's 3MF in memory through a slice (MemoryMax 150M): 20 MB, while
# the slice-agent on the PC keeps its own 50 MB cap.
MAX_FILE_BYTES = 20 * 1024 * 1024
MANIFEST_NAME = "manifest.json"
JOBS_DIR = "jobs"
INCOMING = ".incoming"
STALE_INCOMING_S = 3600  # older staging entries belong to no upload in progress


class JobError(ValueError):
    """The job cannot be created. The message is safe to show to the client."""


class JobTooLarge(JobError):
    """The manifest or the 3MF is over its size cap."""


class JobBusy(Exception):
    """The job is being sliced (or evaluated) already."""


class EvaluationFailed(Exception):
    """An evaluation run failed at its baseline. The message is English, safe to show."""


@dataclass(frozen=True)
class SliceStart:
    """A claimed job whose slice runs in the background, and the slot warning it runs with."""

    job: Job
    warning: str | None  # proposal.SlotChoice.warning: a material mismatch, Russian, Markdown-escaped


def _size(n: int) -> str:
    return f"{n / (1024 * 1024):g} MB" if n >= 1024 * 1024 else f"{n} bytes"


def jobs_root(settings: Settings) -> Path:
    return Path(settings.data_dir) / JOBS_DIR


def incoming_dir(settings: Settings) -> Path:
    """Staging area for uploads and new job folders (same file system as the job folders)."""
    path = jobs_root(settings) / INCOMING
    path.mkdir(parents=True, exist_ok=True)
    return path


def job_dir(settings: Settings, job_id: int) -> Path:
    return jobs_root(settings) / str(int(job_id))


def load_job_manifest(settings: Settings, job: Job) -> Manifest:
    return load_manifest(job_dir(settings, job.id) / MANIFEST_NAME)


def read_job_file(settings: Settings, job: Job) -> bytes:
    """The job's 3MF, as checked against the manifest sha256 at intake."""
    return (job_dir(settings, job.id) / load_job_manifest(settings, job).file).read_bytes()


def _clear_stale(incoming: Path, now: float) -> None:
    for entry in incoming.iterdir():
        with contextlib.suppress(OSError):
            if now - entry.stat().st_mtime < STALE_INCOMING_S:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)


def _create(
    store: Store, settings: Settings, manifest_bytes: bytes, read_file: Callable[[Manifest], bytes]
) -> Job:
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise JobTooLarge(f"manifest is too large (max {_size(MAX_MANIFEST_BYTES)})")
    incoming = incoming_dir(settings)
    _clear_stale(incoming, time.time())
    staging = Path(tempfile.mkdtemp(prefix="job-", dir=incoming))
    final: Path | None = None
    try:
        manifest_path = staging / MANIFEST_NAME
        manifest_path.write_bytes(manifest_bytes)
        try:
            manifest = load_manifest(manifest_path)
        except ManifestError as exc:
            raise JobError(str(exc)) from None
        # Windows drops trailing dots and spaces and ignores case.
        if manifest.file.rstrip(". ").lower() == MANIFEST_NAME:
            raise JobError(f"manifest file may not be named {MANIFEST_NAME}")
        file_bytes = read_file(manifest)
        if len(file_bytes) > MAX_FILE_BYTES:
            raise JobTooLarge(f"{manifest.file} is too large (max {_size(MAX_FILE_BYTES)})")
        try:
            verify_file(manifest, file_bytes)
        except ManifestError as exc:
            raise JobError(str(exc)) from None
        (staging / manifest.file).write_bytes(file_bytes)

        with store.transaction():
            job_id = store.add_job(
                manifest.name,
                manifest.kind,
                manifest_bytes.decode("utf-8"),  # load_manifest has decoded it already
                manifest.file_sha256,
                datetime.now(UTC),
            )
            final = job_dir(settings, job_id)
            if final.exists():
                # No row owns it: left by a crash between the rename and the commit.
                log.warning("job %d: replacing an orphaned job folder", job_id)
                shutil.rmtree(final)
            os.replace(staging, final)
    except BaseException:
        # Any failure here means the transaction did not commit: no row owns `final`.
        shutil.rmtree(staging, ignore_errors=True)
        if final is not None:
            shutil.rmtree(final, ignore_errors=True)
        raise
    log.info("job %d created: %s (%s, %d bytes)", job_id, manifest.name, manifest.kind, len(file_bytes))
    job = store.job(job_id)
    assert job is not None
    return job


def create_job_from_bytes(
    store: Store, settings: Settings, manifest_bytes: bytes, file_bytes: bytes
) -> Job:
    """A job from an uploaded manifest and 3MF. Raises ``JobError`` (safe message)."""
    return _create(store, settings, manifest_bytes, lambda _m: file_bytes)


def _read_capped(path: Path, cap: int, what: str) -> bytes:
    try:
        if not path.is_file():
            raise JobError(f"{what} is not a regular file")
        with path.open("rb") as fh:
            data = fh.read(cap + 1)
    except OSError as exc:
        raise JobError(f"cannot read {what}: {type(exc).__name__}") from None
    if len(data) > cap:
        raise JobTooLarge(f"{what} is too large (max {_size(cap)})")
    return data


def _inside(path: Path, root: Path, what: str) -> Path:
    """``path`` with every link resolved, refused unless it stays inside ``root``."""
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise JobError(f"{what} not found") from None
    if not resolved.is_relative_to(root):
        raise JobError(f"{what} is outside the allowed jobs root")
    return resolved


def create_job_from_path(
    store: Store, settings: Settings, manifest_path: Path, allowed_root: Path
) -> Job:
    """A job from a manifest on this machine and the 3MF next to it.

    Both must resolve (links followed) inside ``allowed_root``. A relative
    ``manifest_path`` is taken relative to ``allowed_root``.
    """
    try:
        root = Path(allowed_root).resolve(strict=True)
    except (OSError, RuntimeError):
        raise JobError("the allowed jobs root does not exist") from None
    manifest_file = _inside(root / Path(manifest_path), root, "manifest")
    manifest_bytes = _read_capped(manifest_file, MAX_MANIFEST_BYTES, "manifest")

    def read_file(m: Manifest) -> bytes:
        file_path = _inside(manifest_file.parent / m.file, root, m.file)
        return _read_capped(file_path, MAX_FILE_BYTES, m.file)

    return _create(store, settings, manifest_bytes, read_file)


# -- HTTP upload ---------------------------------------------------------------------


class JobUpload:
    """Streaming parser for ``POST /jobs``: parts ``manifest`` and ``file``, each exactly once.

    ``manifest`` goes into a capped buffer, ``file`` straight to a temp file in
    ``dest``. Client file names are ignored. Raises ``JobTooLarge`` past a cap and
    ``JobError`` for anything else wrong with the body.
    """

    def __init__(self, boundary: bytes, dest: Path, max_file: int, max_manifest: int) -> None:
        self.dest, self.max_file, self.max_manifest = dest, max_file, max_manifest
        self.manifest: bytearray | None = None
        self.path: Path | None = None
        self.size = 0
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
        if kind != b"form-data" or name not in (b"manifest", b"file"):
            raise JobError("only the parts `manifest` and `file` are accepted")
        if name == b"file":
            if self.path is not None:
                raise JobError("more than one `file` part")
            fd, path = tempfile.mkstemp(prefix="upload-", suffix=".part", dir=self.dest)
            self.path = Path(path)
            self._fh = os.fdopen(fd, "wb")
            self._part = "file"
        else:
            if self.manifest is not None:
                raise JobError("more than one `manifest` part")
            self.manifest = bytearray()
            self._part = "manifest"

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if self._part == "file":
            self.size += len(chunk)
            if self.size > self.max_file:
                raise JobTooLarge(f"file is too large (max {_size(self.max_file)})")
            self._fh.write(chunk)
        elif self._part == "manifest":
            if len(self.manifest) + len(chunk) > self.max_manifest:
                raise JobTooLarge(f"manifest is too large (max {_size(self.max_manifest)})")
            self.manifest.extend(chunk)

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
        except JobError:
            raise
        except ValueError:  # python_multipart parse errors; an OSError here is ours (500)
            raise JobError("malformed multipart body") from None

    def finish(self) -> tuple[bytes, Path]:
        try:
            self._parser.finalize()
        except ValueError:
            raise JobError("malformed multipart body") from None
        if not self.ended:
            raise JobError("incomplete multipart body")
        if self.manifest is None:
            raise JobError("missing `manifest` part")
        if self.path is None:
            raise JobError("missing `file` part")
        return bytes(self.manifest), self.path

    def discard(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        if self.path is not None:
            with contextlib.suppress(OSError):  # stale entries are cleared at the next intake
                self.path.unlink(missing_ok=True)
