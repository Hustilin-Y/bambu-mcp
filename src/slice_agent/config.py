"""slice-agent configuration: TOML file under %LOCALAPPDATA% plus token env vars."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LISTEN = ("0.0.0.0", 11436)
DEFAULT_STUDIO_EXE = Path(r"C:\Program Files\Bambu Studio\bambu-studio.exe")
DEFAULT_STUDIO_RESOURCES = Path(r"C:\Program Files\Bambu Studio\resources\profiles\BBL")
DEFAULT_MACHINE = {"0.4": "Bambu Lab P2S 0.4 nozzle"}
DEFAULT_PROCESS_SETS = {"standard": "0.20mm Standard @BBL P2S"}
DEFAULT_FILAMENTS = ["Generic PETG @BBL P2S"]


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


def config_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env["LOCALAPPDATA"]) / "bambu-mcp" / "slice-agent.toml"


def load_config(path: Path | None = None, env: Mapping[str, str] | None = None) -> AgentConfig:
    """Load the TOML file (optional) and apply defaults; tokens come from the environment only."""
    env = os.environ if env is None else env
    path = path or config_path(env)
    raw: dict = {}
    if path.exists():
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    local = Path(env["LOCALAPPDATA"])
    appdata = Path(env["APPDATA"])
    home = Path(env.get("USERPROFILE") or Path.home())
    listen = raw.get("listen")
    return AgentConfig(
        studio_exe=Path(raw.get("studio_exe", DEFAULT_STUDIO_EXE)),
        studio_data=Path(raw.get("studio_data", appdata / "BambuStudio")),
        studio_resources=Path(raw.get("studio_resources", DEFAULT_STUDIO_RESOURCES)),
        profiles_dir=Path(raw.get("profiles_dir", local / "bambu-mcp" / "profiles")),
        sliced_dir=Path(raw.get("sliced_dir", home / "bambu-mcp" / "sliced")),
        listen=(str(listen[0]), int(listen[1])) if listen else DEFAULT_LISTEN,
        machine=dict(raw.get("machine", DEFAULT_MACHINE)),
        process_sets=dict(raw.get("process_sets", DEFAULT_PROCESS_SETS)),
        filaments=list(raw.get("filaments", DEFAULT_FILAMENTS)),
        slice_token=env.get("SLICE_AGENT_TOKEN", ""),
        pc_agent_token=env.get("PC_AGENT_TOKEN", ""),
    )
