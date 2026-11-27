"""Settings (config.toml) and secrets (.env)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

TlsMode = Literal["ca", "ca-no-hostname", "pin"]
CameraBackend = Literal["pyav"]

_CLIENT_PREFIX = "CLIENT_TOKEN_"


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
            client_tokens={
                k[len(_CLIENT_PREFIX):].lower(): v
                for k, v in env.items()
                if k.startswith(_CLIENT_PREFIX) and len(k) > len(_CLIENT_PREFIX) and v
            },
        )


def _default_listen() -> list[tuple[str, int]]:
    return [("192.168.88.11", 8765), ("127.0.0.1", 8765)]


@dataclass
class Settings:
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

    @classmethod
    def load(cls, toml_path: Path) -> Settings:
        toml_path = Path(toml_path)
        raw: dict[str, Any] = (
            tomllib.loads(toml_path.read_text(encoding="utf-8")) if toml_path.exists() else {}
        )
        defaults = cls()
        unknown = set(raw) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown settings keys: {', '.join(sorted(unknown))}")

        tls_mode = raw.get("tls_mode", defaults.tls_mode)
        if tls_mode not in get_args(TlsMode):
            raise ValueError(f"tls_mode must be one of {get_args(TlsMode)}, got {tls_mode!r}")
        backend = raw.get("camera_backend", defaults.camera_backend)
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

        power_w = {k: float(v) for k, v in raw.get("power_w", defaults.power_w).items()}
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
            data_dir=Path(raw.get("data_dir", defaults.data_dir)),
            listen=listen,
            tls_mode=tls_mode,
            ca_file=Path(ca_file) if ca_file else None,
            cert_sha256=raw.get("cert_sha256"),
            notify_url=raw.get("notify_url"),
            camera_backend=backend,
            power_w=power_w,
            heatup_kwh=float(raw.get("heatup_kwh", defaults.heatup_kwh)),
            timezone=raw.get("timezone", defaults.timezone),
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
