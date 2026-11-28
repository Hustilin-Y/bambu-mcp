"""Read-only check of a printer against its profile, for a new model's first contact.

    bambu-printer-check [--env PATH] [--config PATH] [--out DIR] [--upload-test]

Connects the way the service does (same .env and config.toml), runs each check once and
writes ``report.md`` and ``pushall.redacted.json`` to ``--out`` (default ./printer-check).
The report has one line per check (OK, FAIL or SKIP) and lists the profile's unverified
fields the checks confirmed, ready to paste into a pull request.

It publishes only ``get_version`` and ``pushall`` (``SENDABLE``): never a print, a pause,
a stop or a tray setting. ``--upload-test`` adds one FTPS upload of a 16-byte
``printer-check.txt`` and a size check; the file stays (nothing here deletes).
Serials, the access code and the printer's address never reach the files or the output.
"""

from __future__ import annotations

import argparse
import ftplib
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import paho.mqtt.client as mqtt

from bambu_mcp import camera as camera_module
from bambu_mcp.config import Secrets, Settings
from bambu_mcp.printer.ftps import FTPS_PORT, FTPS_USER, ImplicitFTPS, build_ftps_context, upload
from bambu_mcp.printer.link import GET_VERSION, PORT, PUSH_ALL, USERNAME, _verify_peer, build_tls_context
from bambu_mcp.redact import redact

SENDABLE = (GET_VERSION, PUSH_ALL)
UPLOAD_NAME = "printer-check.txt"
_SCRUB_KEYS = frozenset({"sn", "serial", "dev_id", "ip", "ipcam_dev", "rtsp_url", "tag_uid", "tray_uuid"})


def _check_sendable(payload: dict) -> None:
    if payload not in SENDABLE:
        raise ValueError("printer_check publishes only get_version and pushall")


class Probe(Protocol):
    def mqtt(self) -> tuple[dict | None, dict | None]: ...
    def camera(self) -> bytes | None: ...
    def ftps_list(self) -> list[str]: ...
    def ftps_upload(self, local: Path, name: str) -> None: ...


class LiveProbe:
    """The real printer, over the service's own TLS checks."""

    def __init__(
        self,
        secrets: Secrets,
        settings: Settings,
        *,
        client_factory: Callable[..., Any] = mqtt.Client,
        wait_s: float = 20,
    ) -> None:
        self._secrets = secrets
        self._settings = settings
        self._factory = client_factory
        self._wait_s = wait_s

    def mqtt(self) -> tuple[dict | None, dict | None]:
        """(get_version reply, full pushall report); either None if it did not arrive in time."""
        s = self._secrets
        got: dict[str, dict] = {}
        done = threading.Event()
        client = self._factory(mqtt.CallbackAPIVersion.VERSION2, client_id="bambu-printer-check")
        client.username_pw_set(USERNAME, s.access_code)
        client.tls_set_context(build_tls_context(self._settings, serial=s.serial))

        def send(payload: dict) -> None:
            _check_sendable(payload)
            client.publish(f"device/{s.serial}/request", json.dumps(payload))

        def on_connect(c, userdata, flags, reason_code, properties) -> None:
            if reason_code != 0:
                done.set()
                return
            c.subscribe(f"device/{s.serial}/report")
            for payload in SENDABLE:
                send(payload)

        def on_message(c, userdata, msg) -> None:
            try:
                body = json.loads(msg.payload)
            except ValueError:
                return
            info, report = body.get("info"), body.get("print")
            if isinstance(info, dict) and info.get("command") == "get_version":
                got["version"] = body
            if isinstance(report, dict) and "gcode_state" in report and len(report) > 20:
                got["pushall"] = body
            if len(got) == 2:
                done.set()

        client.on_connect = on_connect
        client.on_message = on_message
        client.connect(s.host, PORT, 60)
        client.loop_start()
        try:
            done.wait(self._wait_s)
        finally:
            client.loop_stop()
            client.disconnect()
        if not got:
            raise ConnectionError("no reply from the printer over MQTT")
        return got.get("version"), got.get("pushall")

    def camera(self) -> bytes | None:
        import asyncio

        return asyncio.run(camera_module.snapshot(self._secrets, self._settings, timeout_s=20))

    def _ftps(self) -> ImplicitFTPS:
        s = self._secrets
        if self._settings.ca_file is None:
            raise ValueError("FTPS needs ca_file in config.toml")
        ftp = ImplicitFTPS(
            context=build_ftps_context(self._settings.ca_file, s.serial), server_hostname=s.serial, timeout=30
        )
        ftp.connect(s.host, FTPS_PORT)
        _verify_peer(ftp.sock, "ca", s.serial, None)
        ftp.login(FTPS_USER, s.access_code)
        ftp.prot_p()
        return ftp

    def ftps_list(self) -> list[str]:
        ftp = self._ftps()
        try:
            return sorted(ftp.nlst())
        finally:
            try:
                ftp.quit()
            except ftplib.all_errors:
                ftp.close()

    def ftps_upload(self, local: Path, name: str) -> None:
        s = self._secrets
        if self._settings.ca_file is None:
            raise ValueError("FTPS needs ca_file in config.toml")
        upload(s.host, s.serial, s.access_code, self._settings.ca_file, local, name)


def _scrub(value: Any, mask: tuple[str, ...]) -> Any:
    """A copy with identifying fields replaced and every secret string redacted."""
    if isinstance(value, dict):
        return {k: ("<redacted>" if k in _SCRUB_KEYS else _scrub(v, mask)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, mask) for v in value]
    if isinstance(value, str):
        return redact(value, mask)
    return value


def _external(report: dict) -> dict | None:
    raw = report.get("vt_tray")
    if raw is None:
        slots = report.get("vir_slot")
        raw = slots[0] if isinstance(slots, list) and slots else None
    return raw if isinstance(raw, dict) else None


def run_checks(probe: Probe, settings: Settings, secrets: Secrets, out_dir: Path, *, upload_test: bool) -> int:
    """Run every check, write the two files, return the exit code (0 when nothing failed)."""
    profile = settings.profile
    mask = tuple(v for v in (secrets.access_code, secrets.serial, secrets.host) if v)
    lines: list[str] = []
    confirmed: list[str] = []
    failed = False

    def add(status: str, name: str, detail: str = "") -> None:
        nonlocal failed
        failed |= status == "FAIL"
        lines.append(redact(f"{status} {name}" + (f": {detail}" if detail else ""), mask))

    def reason(exc: BaseException) -> str:
        return redact(f"{type(exc).__name__}: {exc}", mask)[:200]

    out_dir.mkdir(parents=True, exist_ok=True)
    version = pushall = None
    try:
        version, pushall = probe.mqtt()
        add("OK", "TLS and CN", "the printer's certificate passed the service's check")
    except Exception as exc:  # noqa: BLE001 - each check reports its own failure
        add("FAIL", "TLS and CN", reason(exc))

    if version is not None:
        modules = version.get("info", {}).get("module") or []
        names = ", ".join(sorted(str(m.get("name")) for m in modules if isinstance(m, dict)))
        add("OK", "get_version", names or "no modules listed")
    else:
        add("SKIP" if failed else "FAIL", "get_version", "no reply")

    report = (pushall or {}).get("print") if pushall else None
    if isinstance(report, dict):
        add("OK", "pushall", f"gcode_state {report.get('gcode_state')}")
        scrubbed = _scrub(pushall, mask)
        (out_dir / "pushall.redacted.json").write_text(
            json.dumps(scrubbed, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        ext = _external(report)
        if ext is not None:
            add("OK", "external spool reported", f"tray_type {ext.get('tray_type')!r}, id {ext.get('id')!r}")
        else:
            add("FAIL", "external spool reported", "no vt_tray or vir_slot in the report")
        units = report.get("ams", {}).get("ams") if isinstance(report.get("ams"), dict) else None
        unit_count = len(units) if isinstance(units, list) else 0
        if unit_count:
            slots = max(len(u.get("tray") or []) for u in units if isinstance(u, dict))
            add("OK", f"AMS: {unit_count} unit{'s' if unit_count > 1 else ''}", f"{slots} slots each")
            matches = (unit_count <= profile.ams_units, slots == profile.ams_slots)
        else:
            add("OK", "AMS: none")
            matches = (profile.ams_units == 0, profile.ams_slots == 0)
        if all(matches):
            confirmed += ["ams_units", "ams_slots"]
        sdcard = report.get("sdcard")
        if sdcard is True:
            add("OK", "storage present", f"sdcard true ({profile.storage} expected)")
        else:
            add("FAIL", "storage present", f"sdcard {sdcard!r}: insert {'a USB stick' if profile.storage == 'usb' else 'a microSD card'}")
    else:
        add("SKIP" if version is None else "FAIL", "pushall", "no full report")

    try:
        frame = probe.camera()
    except Exception as exc:  # noqa: BLE001
        frame = None
        add("FAIL", "camera frame", reason(exc))
    else:
        if frame:
            add("OK", "camera frame", f"{len(frame)} bytes over {settings.camera_backend}")
            confirmed.append("camera")
        else:
            add("FAIL", "camera frame", f"no frame over {settings.camera_backend}, see the log")

    try:
        names = probe.ftps_list()
        add("OK", "FTPS login and root listing", ", ".join(names) or "empty root")
        if isinstance(report, dict) and report.get("sdcard") is True:
            confirmed.append("storage")
    except Exception as exc:  # noqa: BLE001
        add("FAIL", "FTPS login and root listing", reason(exc))

    if upload_test:
        local = out_dir / UPLOAD_NAME
        local.write_bytes(b"bambu-mcp check\n")
        try:
            probe.ftps_upload(local, UPLOAD_NAME)
            add("OK", "FTPS upload", f"{UPLOAD_NAME}, 16 bytes, size matches (the file stays on the printer)")
        except Exception as exc:  # noqa: BLE001
            add("FAIL", "FTPS upload", reason(exc))
    else:
        add("SKIP", "FTPS upload", "run with --upload-test to try one")

    still = sorted(set(profile.unverified) - set(confirmed))
    text = "\n".join(
        [
            f"# Printer check: {profile.model_name} (profile {profile.name})",
            "",
            *[f"- {line}" for line in lines],
            "",
            "## Profile fields this confirms",
            "",
            *([f"- {name}" for name in sorted(set(confirmed) & set(profile.unverified))] or ["- none"]),
            "",
            "## Still unverified",
            "",
            *([f"- {name}" for name in still] or ["- none"]),
            "",
        ]
    )
    (out_dir / "report.md").write_text(text, encoding="utf-8")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bambu-printer-check", description=__doc__.split("\n\n")[0])
    parser.add_argument("--env", type=Path, default=Path(os.environ.get("BAMBU_ENV", ".env")))
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=Path("printer-check"))
    parser.add_argument("--upload-test", action="store_true")
    args = parser.parse_args(argv)
    secrets = Secrets.load(args.env)
    config = args.config or Path(os.environ.get("BAMBU_CONFIG", args.env.parent / "config.toml"))
    settings = Settings.load(config)
    code = run_checks(LiveProbe(secrets, settings), settings, secrets, args.out, upload_test=args.upload_test)
    print((args.out / "report.md").read_text(encoding="utf-8"))
    return code


if __name__ == "__main__":
    sys.exit(main())
