"""Settings (config.toml) and secrets (.env)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, get_args

from bambu_profiles import PROFILE_NAMES, PrinterProfile, load_profile

TlsMode = Literal["ca", "ca-no-hostname", "pin"]
CameraBackend = Literal["pyav", "jpeg_6000"]
# The camera backend a profile's camera kind uses unless config.toml names one.
_PROFILE_CAMERA = {"rtsps_322": "pyav", "jpeg_6000": "jpeg_6000"}

_CLIENT_PREFIX = "CLIENT_TOKEN_"
_APP_PREFIX = "APP_TOKEN_"  # tokens that reach only /v1/ (the Mini App tab)

# The default timezone of a country (store meta "country"), when config.toml names none.
# One zone per country: countries with several zones set `timezone` in config.toml.
COUNTRY_TIMEZONES = {
    "AT": "Europe/Vienna",
    "BE": "Europe/Brussels",
    "BG": "Europe/Sofia",
    "CH": "Europe/Zurich",
    "CZ": "Europe/Prague",
    "DE": "Europe/Berlin",
    "DK": "Europe/Copenhagen",
    "EE": "Europe/Tallinn",
    "ES": "Europe/Madrid",
    "FI": "Europe/Helsinki",
    "FR": "Europe/Paris",
    "GB": "Europe/London",
    "GE": "Asia/Tbilisi",
    "GR": "Europe/Athens",
    "HU": "Europe/Budapest",
    "IE": "Europe/Dublin",
    "IT": "Europe/Rome",
    "LT": "Europe/Vilnius",
    "LV": "Europe/Riga",
    "MD": "Europe/Chisinau",
    "NL": "Europe/Amsterdam",
    "NO": "Europe/Oslo",
    "PL": "Europe/Warsaw",
    "PT": "Europe/Lisbon",
    "RO": "Europe/Bucharest",
    "SE": "Europe/Stockholm",
    "SK": "Europe/Bratislava",
    "TR": "Europe/Istanbul",
    "UA": "Europe/Kyiv",
}


def _parse_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value
    return env


@dataclass(frozen=True)
class Secrets:
    access_code: str
    serial: str
    host: str
    notify_token: str | None = None
    client_tokens: dict[str, str] = field(default_factory=dict)
    app_tokens: dict[str, str] = field(default_factory=dict)  # reach only /v1/, never /mcp or /jobs
    gateway_key: str | None = None  # orchestra-gateway key, to wake the PC
    slice_agent_token: str | None = None  # bearer for slice-agent on the PC

    def __repr__(self) -> str:  # never leak values through repr or tracebacks
        return "Secrets(***)"

    @classmethod
    def load(cls, env_path: Path) -> Secrets:
        env = _parse_env(Path(env_path))
        for name in ("BAMBU_ACCESS_CODE", "BAMBU_SERIAL", "BAMBU_HOST"):
            if not env.get(name):
                raise ValueError(f"missing required variable {name}")
        return cls(
            access_code=env["BAMBU_ACCESS_CODE"],
            serial=env["BAMBU_SERIAL"],
            host=env["BAMBU_HOST"],
            notify_token=env.get("NOTIFY_TOKEN") or None,
            gateway_key=env.get("GATEWAY_KEY") or None,
            slice_agent_token=env.get("SLICE_AGENT_TOKEN") or None,
            client_tokens=_prefixed(env, _CLIENT_PREFIX),
            app_tokens=_prefixed(env, _APP_PREFIX),
        )


def _prefixed(env: dict[str, str], prefix: str) -> dict[str, str]:
    """``PREFIX_NAME=value`` lines as ``{"name": value}``, empty names and values skipped."""
    return {k[len(prefix):].lower(): v for k, v in env.items() if k.startswith(prefix) and len(k) > len(prefix) and v}


def _default_listen() -> list[tuple[str, int]]:
    # Loopback only: a LAN address is set in config.toml (deploy/README.md).
    return [("127.0.0.1", 8765)]


@dataclass
class Settings:
    printer: str = "p2s"  # a bambu_profiles name
    data_dir: Path = Path("data")
    listen: list[tuple[str, int]] = field(default_factory=_default_listen)
    tls_mode: TlsMode = "ca"
    ca_file: Path | None = None
    cert_sha256: str | None = None
    notify_url: str | None = None
    camera_backend: CameraBackend = "pyav"
    power_w: dict[str, float] = field(default_factory=lambda: {"PLA": 200.0})
    heatup_kwh: float = 0.05
    timezone: str = "Europe/Kyiv"
    jobs_root_allowed: Path | None = None  # None: create_job from a path is off
    gateway_url: str | None = None
    slice_agent_url: str | None = None
    service_url: str | None = None  # default --url of `bambu-mcp upload`
    # True when config.toml names the timezone: the country's default then does not apply.
    timezone_explicit: bool = field(default=False, compare=False, repr=False)

    @property
    def profile(self) -> PrinterProfile:
        """The printer model's profile (``load_profile`` caches it)."""
        return load_profile(self.printer)

    @classmethod
    def load(cls, toml_path: Path) -> Settings:
        toml_path = Path(toml_path)
        raw: dict[str, Any] = (
            tomllib.loads(toml_path.read_text(encoding="utf-8")) if toml_path.exists() else {}
        )
        defaults = cls()
        unknown = set(raw) - (set(cls.__dataclass_fields__) - {"timezone_explicit"})
        if unknown:
            raise ValueError(f"unknown settings keys: {', '.join(sorted(unknown))}")

        printer = raw.get("printer", defaults.printer)
        if printer not in PROFILE_NAMES:
            raise ValueError(f"unknown printer {printer!r}, known: {', '.join(PROFILE_NAMES)}")
        profile = load_profile(printer)

        tls_mode = raw.get("tls_mode", defaults.tls_mode)
        if tls_mode not in get_args(TlsMode):
            raise ValueError(f"tls_mode must be one of {get_args(TlsMode)}, got {tls_mode!r}")
        backend = raw.get("camera_backend", _PROFILE_CAMERA[profile.camera])
        if backend not in get_args(CameraBackend):
            raise ValueError(
                f"camera_backend must be one of {get_args(CameraBackend)}, got {backend!r}"
            )

        listen = defaults.listen
        if "listen" in raw:
            listen = []
            for item in raw["listen"]:
                host, sep, port = str(item).rpartition(":")
                if not sep or not host or not port.isdigit():
                    raise ValueError(f"listen entries must look like 'host:port', got {item!r}")
                listen.append((host, int(port)))

        power_w = {k: float(v) for k, v in raw.get("power_w", profile.power_w).items()}
        if "PLA" not in power_w:
            raise ValueError("power_w must have a PLA entry (the fallback for other materials)")

        urls = {key: _url(raw, key) for key in ("gateway_url", "slice_agent_url", "service_url")}
        jobs_root = raw.get("jobs_root_allowed")
        if jobs_root is not None and (not isinstance(jobs_root, str) or not jobs_root):
            raise ValueError("jobs_root_allowed must be a non-empty path string")
        if jobs_root is not None and not Path(jobs_root).is_absolute():
            raise ValueError("jobs_root_allowed must be an absolute path")

        ca_file = raw.get("ca_file")
        return cls(
            printer=printer,
            data_dir=Path(raw.get("data_dir", defaults.data_dir)),
            listen=listen,
            tls_mode=tls_mode,
            ca_file=Path(ca_file) if ca_file else None,
            cert_sha256=raw.get("cert_sha256"),
            notify_url=raw.get("notify_url"),
            camera_backend=backend,
            power_w=power_w,
            heatup_kwh=float(raw.get("heatup_kwh", profile.heatup_kwh)),
            timezone=raw.get("timezone", defaults.timezone),
            timezone_explicit="timezone" in raw,
            jobs_root_allowed=Path(jobs_root) if jobs_root else None,
            **urls,
        )


def _url(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
        raise ValueError(f"{key} must be an http:// or https:// URL")
    return value


def country_timezone(country: str) -> str | None:
    """The country's default timezone, None for a country not in the table."""
    return COUNTRY_TIMEZONES.get(country)


def with_country(settings: Settings, country: str) -> Settings:
    """Settings with the country's default timezone, unless config.toml names one."""
    tz = country_timezone(country)
    if settings.timezone_explicit or tz is None:
        return settings
    return replace(settings, timezone=tz)
