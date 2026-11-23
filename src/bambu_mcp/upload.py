"""`bambu-mcp upload <manifest>`: send a CAD Workshop job to the service.

Reads the manifest and the 3MF next to it, checks the sha256 locally, posts both
to ``<url>/jobs`` and prints the new job id. The bearer token comes only from the
environment variable named by ``--token-env`` (never from argv). ``--url``
defaults to ``service_url`` in the config file named by ``BAMBU_CONFIG``.
On any error: one English line on stderr and exit status 1.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

import httpx

from bambu_mcp.config import Settings
from bambu_mcp.jobs import MAX_FILE_BYTES
from bambu_mcp.manifest import (
    MAX_MANIFEST_BYTES,
    ManifestError,
    load_manifest,
    verify_file,
)

DEFAULT_TOKEN_ENV = "BAMBU_MCP_TOKEN"
TIMEOUT_S = 120.0
_DETAIL_MAX = 200
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class CliError(Exception):
    """A one-line message for stderr."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        if message.startswith("unrecognized arguments"):
            # never echo them: one could be a token typed by mistake
            message = "unrecognized arguments (the token is read only from the environment, see --help)"
        raise CliError(message)


def _parser() -> argparse.ArgumentParser:
    # No abbreviations: `--token <value>` must not silently become `--token-env <value>`.
    parser = _Parser(prog="bambu-mcp", description="bambu-mcp client commands", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    up = sub.add_parser(
        "upload", help="send a print manifest and its 3MF to the service", allow_abbrev=False
    )
    up.add_argument("manifest", type=Path, help="the <name>.print.json written by CAD Workshop")
    up.add_argument("--url", help="service base URL, e.g. http://192.168.88.11:8765")
    up.add_argument(
        "--token-env",
        default=DEFAULT_TOKEN_ENV,
        help=f"environment variable holding the bearer token (default {DEFAULT_TOKEN_ENV})",
    )
    return parser


def _configured_url() -> str | None:
    path = os.environ.get("BAMBU_CONFIG")
    if not path or not Path(path).is_file():
        return None
    try:
        return Settings.load(Path(path)).service_url
    except (OSError, ValueError) as exc:
        raise CliError(f"cannot read the config file: {exc}") from None


def _read(path: Path, cap: int) -> bytes:
    try:
        with path.open("rb") as fh:
            data = fh.read(cap + 1)
    except OSError as exc:
        raise CliError(f"cannot read {path.name}: {exc.strerror or type(exc).__name__}") from None
    if len(data) > cap:
        raise CliError(f"{path.name} is too large")
    return data


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        text = str(body.get("detail") or body.get("error") or "")
    except (ValueError, AttributeError):
        text = ""
    text = " ".join(text.split())[:_DETAIL_MAX]
    return f"HTTP {response.status_code}" + (f": {text}" if text else "")


def _upload(args: argparse.Namespace, transport: httpx.BaseTransport | None) -> int:
    token = os.environ.get(args.token_env, "").strip()
    if not token:
        # Echo the name only when it looks like one, in case a token was passed by mistake.
        name = args.token_env if _ENV_NAME.fullmatch(args.token_env) else "named by --token-env"
        raise CliError(f"set the service token in the environment variable {name}")
    url = args.url or _configured_url()
    if not url:
        raise CliError("--url is required (no service_url in the config named by BAMBU_CONFIG)")

    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        raise CliError(str(exc)) from None
    manifest_bytes = _read(args.manifest, MAX_MANIFEST_BYTES)
    file_bytes = _read(args.manifest.parent / manifest.file, MAX_FILE_BYTES)
    try:
        verify_file(manifest, file_bytes)
    except ManifestError as exc:
        raise CliError(str(exc)) from None

    files = {
        "manifest": ("manifest.json", manifest_bytes, "application/json"),
        "file": (manifest.file, file_bytes, "application/octet-stream"),
    }
    try:
        with httpx.Client(transport=transport, timeout=TIMEOUT_S) as client:
            response = client.post(
                url.rstrip("/") + "/jobs",
                files=files,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        raise CliError(f"cannot reach the service: {type(exc).__name__}") from None
    if response.status_code != 201:
        raise CliError(f"the service refused the job: {_detail(response)}")
    try:
        job_id = response.json()["job_id"]
    except (ValueError, KeyError, TypeError):
        raise CliError("the service answered without a job id") from None
    if not isinstance(job_id, int) or isinstance(job_id, bool):
        raise CliError("the service answered without a job id")
    print(job_id)
    return 0


def main(argv: Sequence[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        return _upload(args, transport)
    except CliError as exc:
        print(f"bambu-mcp: error: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
