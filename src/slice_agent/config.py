"""slice-agent configuration: a TOML file plus token env vars.

Windows (the owner's PC): the file is %LOCALAPPDATA%\\bambu-mcp\\slice-agent.toml and Studio
has default install paths. Elsewhere (a Linux server): $XDG_CONFIG_HOME/bambu-mcp/slice-agent.toml,
and ``studio_exe`` and ``studio_resources`` must be set, since Studio has no standard place there.
"""

from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from bambu_profiles import PROFILE_NAMES, PrinterProfile, load_profile

DEFAULT_LISTEN = ("0.0.0.0", 11436)
DEFAULT_STUDIO_EXE = Path(r"C:\Program Files\Bambu Studio\bambu-studio.exe")
DEFAULT_STUDIO_RESOURCES = Path(r"C:\Program Files\Bambu Studio\resources\profiles\BBL")
_P2S = load_profile("p2s")
DEFAULT_MACHINE = dict(_P2S.machine_presets)
DEFAULT_PROCESS_SETS = dict(_P2S.process_sets)
DEFAULT_FILAMENTS = list(_P2S.filaments)


@dataclass(frozen=True)
class AgentConfig:
    studio_exe: Path
    studio_data: Path
    studio_resources: Path
    profiles_dir: Path
    sliced_dir: Path
    listen: tuple[str, int] = DEFAULT_LISTEN
    machine: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MACHINE))
    process_sets: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PROCESS_SETS))
    filaments: list[str] = field(default_factory=lambda: list(DEFAULT_FILAMENTS))
    slice_token: str = field(default="", repr=False)
    pc_agent_token: str = field(default="", repr=False)
    printer: str = "p2s"  # a bambu_profiles name

    @property
    def profile(self) -> PrinterProfile:
        return load_profile(self.printer)


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or Path.home())


def _xdg(env: Mapping[str, str], key: str, fallback: str) -> Path:
    value = env.get(key)
    return Path(value) if value else _home(env) / fallback


def config_path(env: Mapping[str, str] | None = None, platform: str = sys.platform) -> Path:
    env = os.environ if env is None else env
    if platform == "win32":
        return Path(env["LOCALAPPDATA"]) / "bambu-mcp" / "slice-agent.toml"
    return _xdg(env, "XDG_CONFIG_HOME", ".config") / "bambu-mcp" / "slice-agent.toml"


def load_config(
    path: Path | None = None, env: Mapping[str, str] | None = None, platform: str = sys.platform
) -> AgentConfig:
    """Load the TOML file (optional) and apply defaults; tokens come from the environment only."""
    env = os.environ if env is None else env
    path = path or config_path(env, platform)
    raw: dict = {}
    if path.exists():
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    printer = raw.get("printer", "p2s")
    if printer not in PROFILE_NAMES:
        raise ValueError(f"unknown printer {printer!r}, known: {', '.join(PROFILE_NAMES)}")
    profile = load_profile(printer)
    if platform == "win32":
        local = Path(env["LOCALAPPDATA"])
        home = Path(env.get("USERPROFILE") or Path.home())
        studio_exe = Path(raw.get("studio_exe", DEFAULT_STUDIO_EXE))
        studio_resources = Path(raw.get("studio_resources", DEFAULT_STUDIO_RESOURCES))
        studio_data = Path(raw.get("studio_data", Path(env["APPDATA"]) / "BambuStudio"))
        profiles_dir = Path(raw.get("profiles_dir", local / "bambu-mcp" / "profiles"))
    else:
        for key in ("studio_exe", "studio_resources"):
            if not raw.get(key):
                raise ValueError(f"{path}: {key} is required outside Windows (where Studio is installed)")
        home = _home(env)
        studio_exe = Path(raw["studio_exe"])
        studio_resources = Path(raw["studio_resources"])
        studio_data = Path(raw.get("studio_data", home / ".config" / "BambuStudio"))
        profiles_dir = Path(raw.get("profiles_dir", _xdg(env, "XDG_DATA_HOME", ".local/share") / "bambu-mcp" / "profiles"))
    listen = raw.get("listen")
    return AgentConfig(
        studio_exe=studio_exe,
        studio_data=studio_data,
        studio_resources=studio_resources,
        profiles_dir=profiles_dir,
        sliced_dir=Path(raw.get("sliced_dir", home / "bambu-mcp" / "sliced")),
        listen=(str(listen[0]), int(listen[1])) if listen else DEFAULT_LISTEN,
        machine=dict(raw.get("machine", profile.machine_presets)),
        process_sets=dict(raw.get("process_sets", profile.process_sets)),
        filaments=list(raw.get("filaments", profile.filaments)),
        slice_token=env.get("SLICE_AGENT_TOKEN", ""),
        pc_agent_token=env.get("PC_AGENT_TOKEN", ""),
        printer=printer,
    )
