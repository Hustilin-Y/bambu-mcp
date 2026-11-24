"""MQTT link to the printer: reads, plus one validated write.

The only network path to the printer. It sends the two read commands in
``READ_COMMANDS`` and, since phase 2, exactly one write: ``ams_filament_setting``
built by ``tray_setting`` (AMS slot material and colour, every field checked).
Nothing else is ever published, and only after the peer is verified as the
configured printer.

Peer identity is checked twice: in the TLS handshake (so the access code is
never sent to an unverified peer) and again in the connect callback, before
subscribing or publishing, in case the handshake hook is ever bypassed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets as _secrets
import ssl
import threading
from collections.abc import Callable
from enum import Enum
from typing import Any, Final

import paho.mqtt.client as mqtt

from bambu_mcp.config import Secrets, Settings
from bambu_mcp.logthrottle import Throttle
from bambu_mcp.redact import redact

log = logging.getLogger(__name__)

GET_VERSION: Final = {"info": {"sequence_id": "0", "command": "get_version"}}
PUSH_ALL: Final = {"pushing": {"sequence_id": "0", "command": "pushall"}}
READ_COMMANDS: Final = (GET_VERSION, PUSH_ALL)

# The one write: field set of ha-bambulab's AMS_FILAMENT_SETTING_TEMPLATE.
AMS_FILAMENT_SETTING: Final = "ams_filament_setting"
# Bambu filament_type spellings; support material is PLA-S (no bare "SUPPORT" type exists).
TRAY_TYPES: Final = ("PLA", "PETG", "ABS", "ASA", "TPU", "PC", "PA", "PLA-CF", "PETG-CF", "PVA", "PLA-S")
AMS_UNITS: Final = range(4)
AMS_SLOTS: Final = range(4)
NOZZLE_TEMP_RANGE: Final = (150, 320)
_TRAY_KEYS: Final = frozenset(
    {
        "sequence_id",
        "command",
        "ams_id",
        "tray_id",
        "tray_info_idx",
        "tray_color",
        "nozzle_temp_min",
        "nozzle_temp_max",
        "tray_type",
    }
)
_COLOR_RE: Final = re.compile(r"[0-9A-F]{8}", re.ASCII)
TRAY_INFO_IDX_RE: Final = re.compile(r"[A-Z0-9]{3,8}", re.ASCII)

PORT: Final = 8883
USERNAME: Final = "bblp"
KEEPALIVE_S: Final = 60
BACKOFF_START_S: Final = 5.0
BACKOFF_MAX_S: Final = 300.0
# CONNACK reason codes that mean the access code was rejected.
_AUTH_FAILURE_CODES: Final = frozenset({134, 135})  # bad user name or password, not authorized
_PIN_RE: Final = re.compile(r"[0-9a-f]{64}")

Schedule = Callable[[float, Callable[[], None]], None]


class PrinterNotConnected(RuntimeError):
    """A write could not be handed to the printer: nothing was sent."""


def _is_int(value: Any) -> bool:
    return type(value) is int  # not bool, not float


def tray_ids(ams: int, slot: int) -> tuple[int, int]:
    """(``ams_id``, ``tray_id``) for an AMS unit and a slot within it.

    As ha-bambulab does for current firmware: the unit index and the slot index
    0..3 within that unit. Unverified on the P2S: the first live ``load_spool``
    must confirm the printer reads them this way.
    """
    return ams, slot


def tray_setting(
    *,
    ams: int,
    slot: int,
    tray_info_idx: str,
    tray_type: str,
    color_rgba: str,
    nozzle_temp_min: int,
    nozzle_temp_max: int,
) -> dict:
    """The ``ams_filament_setting`` payload for one AMS slot. ``ValueError`` on any bad field."""
    if not (_is_int(ams) and ams in AMS_UNITS):
        raise ValueError("ams must be an integer 0..3")
    if not (_is_int(slot) and slot in AMS_SLOTS):
        raise ValueError("slot must be an integer 0..3")
    if not (isinstance(tray_info_idx, str) and TRAY_INFO_IDX_RE.fullmatch(tray_info_idx)):
        raise ValueError("tray_info_idx must be 3..8 upper-case letters or digits")
    if not isinstance(tray_type, str) or tray_type not in TRAY_TYPES:
        raise ValueError(f"tray_type must be one of {', '.join(TRAY_TYPES)}")
    if not (isinstance(color_rgba, str) and _COLOR_RE.fullmatch(color_rgba)):
        raise ValueError("color_rgba must be RRGGBBAA in upper-case hex")
    low, high = NOZZLE_TEMP_RANGE
    for name, temp in (("nozzle_temp_min", nozzle_temp_min), ("nozzle_temp_max", nozzle_temp_max)):
        if not (_is_int(temp) and low <= temp <= high):
            raise ValueError(f"{name} must be an integer {low}..{high}")
    if nozzle_temp_min > nozzle_temp_max:
        raise ValueError("nozzle_temp_min must not be above nozzle_temp_max")
    ams_id, tray_id = tray_ids(ams, slot)
    return {
        "print": {
            "sequence_id": "0",
            "command": AMS_FILAMENT_SETTING,
            "ams_id": ams_id,
            "tray_id": tray_id,
            "tray_info_idx": tray_info_idx,
            "tray_color": color_rgba,
            "nozzle_temp_min": nozzle_temp_min,
            "nozzle_temp_max": nozzle_temp_max,
            "tray_type": tray_type,
        }
    }


def _is_tray_setting(payload: Any) -> bool:
    """True only for a payload ``tray_setting`` builds itself, key for key and type for type."""
    if not isinstance(payload, dict) or set(payload) != {"print"}:
        return False
    body = payload["print"]
    if not isinstance(body, dict) or set(body) != _TRAY_KEYS:
        return False
    if body["command"] != AMS_FILAMENT_SETTING or body["sequence_id"] != "0":
        return False
    ids = (body["ams_id"], body["tray_id"])
    if not all(_is_int(i) for i in ids):  # True would hash like 1 in the lookup below
        return False
    # The inverse of tray_ids, so whatever mapping it holds is read back the same way.
    unit_slot = {tray_ids(a, s): (a, s) for a in AMS_UNITS for s in AMS_SLOTS}.get(ids)
    if unit_slot is None:
        return False
    try:
        rebuilt = tray_setting(
            ams=unit_slot[0],
            slot=unit_slot[1],
            tray_info_idx=body["tray_info_idx"],
            tray_type=body["tray_type"],
            color_rgba=body["tray_color"],
            nozzle_temp_min=body["nozzle_temp_min"],
            nozzle_temp_max=body["nozzle_temp_max"],
        )
    except (ValueError, TypeError):
        return False
    return rebuilt == payload


class LinkStatus(Enum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    AUTH_FAILED = "auth_failed"
    DISCONNECTED = "disconnected"


def _timer_schedule(delay: float, fn: Callable[[], None]) -> None:
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    timer.start()


def _subject_cn(cert: dict[str, Any]) -> str | None:
    for rdn in cert.get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return None


def _verify_peer(sock: Any, tls_mode: str, serial: str | None, pin: str | None) -> None:
    """Raises ``ssl.SSLCertVerificationError`` unless the peer is the printer."""
    if tls_mode == "pin":
        der = sock.getpeercert(binary_form=True)
        digest = hashlib.sha256(der).hexdigest() if der else ""
        if not hmac.compare_digest(digest, pin or "-"):
            raise ssl.SSLCertVerificationError("peer certificate does not match the pinned SHA-256")
    elif tls_mode == "ca":
        if not serial or _subject_cn(sock.getpeercert() or {}) != serial:
            raise ssl.SSLCertVerificationError("peer certificate CN is not the printer serial")


def _normalise_pin(value: str | None) -> str:
    pin = (value or "").strip().lower()
    if not _PIN_RE.fullmatch(pin):
        raise ValueError("tls_mode 'pin' needs cert_sha256 as 64 hex characters")
    return pin


def build_tls_context(settings: Settings, serial: str | None = None) -> ssl.SSLContext:
    """TLS context for the printer's MQTT port.

    ``ca``: chain verified against ``settings.ca_file``, hostname check off
    (paho passes the IP), peer CN must equal ``serial`` (required, a ``ca``
    context without it would silently be ``ca-no-hostname``). ``ca-no-hostname``:
    chain only. ``pin``: no CA, the DER certificate's SHA-256 must equal
    ``settings.cert_sha256``. The identity check runs inside the handshake.
    """
    mode = settings.tls_mode
    pin: str | None = None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # no system CAs loaded
    ctx.check_hostname = False
    if mode in ("ca", "ca-no-hostname"):
        if settings.ca_file is None:
            raise ValueError(f"tls_mode {mode!r} needs ca_file")
        if mode == "ca" and not serial:
            raise ValueError("tls_mode 'ca' needs the printer serial for the CN check")
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cafile=str(settings.ca_file))
    elif mode == "pin":
        pin = _normalise_pin(settings.cert_sha256)
        ctx.verify_mode = ssl.CERT_NONE
    else:
        raise ValueError(f"unknown tls_mode {mode!r}")

    class _VerifyingSSLSocket(ssl.SSLSocket):
        def do_handshake(self, block: bool = False) -> None:
            super().do_handshake(block)
            try:
                _verify_peer(self, mode, serial, pin)
            except ssl.SSLCertVerificationError:
                self.close()
                raise

    ctx.sslsocket_class = _VerifyingSSLSocket
    return ctx


class PrinterLink:
    """Keeps a verified MQTT session to the printer: reads and the one tray write."""

    def __init__(
        self,
        secrets: Secrets,
        settings: Settings,
        on_report: Callable[[dict], None],
        on_status: Callable[[LinkStatus], None],
        client_factory: Callable[..., Any] = mqtt.Client,
        schedule: Schedule = _timer_schedule,
    ) -> None:
        # Build the context first: a bad TLS setting must fail before anything connects.
        context = build_tls_context(settings, serial=secrets.serial)
        self._tls_mode = settings.tls_mode
        self._pin = _normalise_pin(settings.cert_sha256) if settings.tls_mode == "pin" else None
        self._serial = secrets.serial
        self._host = secrets.host
        self._secret_values = (secrets.access_code, secrets.serial)
        # While the printer is off every attempt fails the same way: log the first, then hourly.
        self._quiet = Throttle(log)
        self._report_topic = f"device/{secrets.serial}/report"
        self._request_topic = f"device/{secrets.serial}/request"
        self._on_report = on_report
        self._on_status = on_status
        self._schedule = schedule
        self._lock = threading.Lock()
        self._started = False
        self._stopped = False
        self._verified = False
        self._delay = BACKOFF_START_S

        client = client_factory(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"bambu-mcp-{_secrets.token_hex(4)}",
            reconnect_on_failure=False,  # reconnects are ours, with our backoff
        )
        client.username_pw_set(USERNAME, secrets.access_code)
        client.tls_set_context(context)
        client.on_connect = self._handle_connect
        client.on_disconnect = self._handle_disconnect
        client.on_message = self._handle_message
        self._client = client

    # --- public ------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        self._schedule(0, self._attempt)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._verified = False
        self._client.disconnect()
        self._client.loop_stop()
        self._emit(LinkStatus.DISCONNECTED)

    def request_full_report(self) -> None:
        self._publish(PUSH_ALL)

    def set_tray(
        self,
        ams: int,
        slot: int,
        tray_info_idx: str,
        tray_type: str,
        color_rgba: str,
        nozzle_temp_min: int,
        nozzle_temp_max: int,
    ) -> dict:
        """Set an AMS slot's material, colour and nozzle temperatures (``ams_filament_setting``).

        Returns the payload published. ``ValueError`` on a bad field, ``PrinterNotConnected``
        when the message was not handed to the connected printer. Never retried.
        """
        payload = tray_setting(
            ams=ams,
            slot=slot,
            tray_info_idx=tray_info_idx,
            tray_type=tray_type,
            color_rgba=color_rgba,
            nozzle_temp_min=nozzle_temp_min,
            nozzle_temp_max=nozzle_temp_max,
        )
        if not self._publish(payload):
            raise PrinterNotConnected("printer link is not connected, nothing was sent")
        log.info(
            "sent ams_filament_setting: AMS %d slot %d %s %s %s %d..%d",
            ams,
            slot,
            tray_type,
            color_rgba,
            tray_info_idx,
            nozzle_temp_min,
            nozzle_temp_max,
        )
        return payload

    # --- internals ---------------------------------------------------------

    def _publish(self, payload: dict) -> bool:
        """Publish an allowed payload. False when the link is not verified or paho refuses it."""
        if payload not in READ_COMMANDS and not _is_tray_setting(payload):
            raise ValueError("only the read commands and a valid tray setting may be sent to the printer")
        if not self._verified:
            log.debug("printer link not verified, request dropped")
            return False
        info = self._client.publish(self._request_topic, json.dumps(payload), qos=0)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def _emit(self, status: LinkStatus) -> None:
        try:
            self._on_status(status)
        except Exception:
            log.exception("link status handler failed")

    def _attempt(self) -> None:
        if self._stopped:
            return
        self._emit(LinkStatus.CONNECTING)
        try:
            self._client.loop_stop()  # the previous network thread, if any, has ended
            self._client.connect(self._host, PORT, keepalive=KEEPALIVE_S)
            self._client.loop_start()
        except Exception as exc:  # OSError (TLS, peer identity) and anything else, e.g. a bad host
            self._quiet.warning(
                "connect",
                "printer connection failed: %s: %s",
                type(exc).__name__,
                redact(str(exc), self._secret_values),
            )
            self._emit(LinkStatus.DISCONNECTED)
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        with self._lock:
            if self._stopped:
                return
            delay = self._delay
            self._delay = min(delay * 2, BACKOFF_MAX_S)
        self._quiet.info("reconnect", "reconnecting to the printer in %.0f s", delay)
        self._schedule(delay, self._attempt)

    def _peer_ok(self, client: Any) -> bool:
        try:
            _verify_peer(client.socket(), self._tls_mode, self._serial, self._pin)
        except Exception:  # unreadable certificate counts as a mismatch
            return False
        return True

    def _handle_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            if reason_code.value in _AUTH_FAILURE_CODES:
                log.error("printer rejected the access code (%s), not reconnecting", reason_code)
                with self._lock:
                    self._stopped = True
                self._emit(LinkStatus.AUTH_FAILED)
                client.loop_stop()
                client.disconnect()
                return
            log.warning("printer refused the connection: %s", reason_code)
            client.disconnect()
            return
        if not self._peer_ok(client):
            log.error("printer certificate does not match the configured printer, disconnecting")
            client.disconnect()
            return
        with self._lock:
            self._verified = True
            self._delay = BACKOFF_START_S
        for key in ("connect", "reconnect", "lost"):
            self._quiet.reset(key)
        self._emit(LinkStatus.CONNECTED)
        client.subscribe(self._report_topic)
        for payload in READ_COMMANDS:
            self._publish(payload)

    def _handle_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        with self._lock:
            self._verified = False
            stopped = self._stopped
        if stopped:
            return
        self._quiet.warning("lost", "printer link lost: %s", reason_code)
        self._emit(LinkStatus.DISCONNECTED)
        self._schedule_reconnect()

    def _handle_message(self, client, userdata, msg) -> None:
        if not self._verified:
            return
        try:
            report = json.loads(msg.payload)
        except ValueError:  # JSONDecodeError and UnicodeDecodeError
            report = None
        if not isinstance(report, dict):
            log.warning("skipping malformed printer report (%d bytes)", len(msg.payload))
            return
        try:
            self._on_report(report)
        except Exception:
            log.exception("printer report handler failed")
