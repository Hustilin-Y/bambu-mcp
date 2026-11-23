"""Build and run the Bambu Studio CLI command (recipe from docs/phase0-report.md)."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from bambu_mcp.manifest import INFILL_PATTERNS
from slice_agent.config import AgentConfig
from slice_agent.profiles import _slug

DEFAULT_TIMEOUT_S = 300
PLATE_TYPES = ("Textured PEI Plate", "Cool Plate", "Engineering Plate", "High Temp Plate", "Smooth PEI Plate")
LAYER_HEIGHTS = {"0.4": (0.12, 0.16, 0.20, 0.24, 0.28)}
_OUT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_RESERVED = re.compile(r"(con|prn|aux|nul|com[1-9]|lpt[1-9])", re.IGNORECASE)
_LOG_TAIL_LINES = 20


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _choice(options: tuple[str, ...]):
    return lambda v: isinstance(v, str) and v in options


@dataclass(frozen=True)
class Overrides:
    """Validated whitelist of slicer settings: key -> value (python types)."""

    values: dict[str, object]

    @classmethod
    def parse(cls, raw: dict, nozzle: str) -> Overrides:
        checks = {
            "sparse_infill_density": lambda v: _is_int(v) and 0 <= v <= 100,
            "sparse_infill_pattern": _choice(INFILL_PATTERNS),
            "wall_loops": lambda v: _is_int(v) and 1 <= v <= 10,
            "wall_generator": _choice(("arachne", "classic")),
            "enable_support": lambda v: isinstance(v, bool),
            "support_type": _choice(("tree(auto)", "normal(auto)")),
            "brim_type": _choice(("no_brim", "auto_brim", "outer_only")),
            "curr_bed_type": _choice(PLATE_TYPES),
            "layer_height": lambda v: (
                isinstance(v, (int, float)) and not isinstance(v, bool) and v in LAYER_HEIGHTS.get(nozzle, ())
            ),
        }
        for key, value in raw.items():
            if key not in checks:
                raise ValueError(f"unknown override: {key}")
            if not checks[key](value):
                raise ValueError(f"invalid value for {key}: {value!r}")
        return cls(dict(raw))

    def args(self) -> list[str]:
        """CLI flags. curr_bed_type is handled by build_command (it replaces plate_type)."""
        out = []
        for key, value in self.values.items():
            if key == "curr_bed_type":
                continue
            if key == "sparse_infill_density":
                text = f"{value}%"
            elif isinstance(value, bool):
                text = "1" if value else "0"
            else:
                text = str(value)
            out.append(f"--{key.replace('_', '-')}={text}")
        return out


def _check_out_name(name: str) -> None:
    if not _OUT_NAME_RE.fullmatch(name) or name[-1] in ". ":
        raise ValueError(f"invalid out_name: {name!r}")
    if _RESERVED.fullmatch(name.split(".", 1)[0]):
        raise ValueError(f"invalid out_name (reserved Windows name): {name!r}")


def build_command(
    cfg: AgentConfig,
    *,
    input_3mf: Path,
    out_name: str,
    nozzle: str,
    process_set: str,
    filament: str,
    plate_type: str,
    kit: bool,
    overrides: Overrides,
) -> list[str]:
    """argv for the Studio CLI. Names are checked against the config before they reach a path."""
    _check_out_name(out_name)
    if nozzle not in cfg.machine:
        raise ValueError(f"unknown nozzle: {nozzle!r}")
    if process_set not in cfg.process_sets:
        raise ValueError(f"unknown process_set: {process_set!r}")
    if filament not in cfg.filaments:
        raise ValueError(f"unknown filament: {filament!r}")
    plate_type = str(overrides.values.get("curr_bed_type", plate_type))
    if plate_type not in PLATE_TYPES:
        raise ValueError(f"unknown plate_type: {plate_type!r}")
    prof = cfg.profiles_dir
    return [
        str(cfg.studio_exe),
        "--slice", "0",
        "--arrange", "0" if kit else "1",
        "--orient", "0",
        "--curr-bed-type", plate_type,
        "--load-settings", f"{prof / f'machine-{nozzle}.json'};{prof / f'process-{process_set}.json'}",
        "--load-filaments", str(prof / f"filament-{_slug(filament)}.json"),
        *overrides.args(),
        "--export-3mf", out_name,
        "--outputdir", str(cfg.sliced_dir),
        str(input_3mf),
    ]


@dataclass(frozen=True)
class SliceRun:
    return_code: int
    error_string: str = ""
    log_tail: str = ""
    timed_out: bool = False


def _tail(*chunks: bytes | str | None) -> str:
    text = "".join(c.decode("utf-8", "replace") if isinstance(c, bytes) else (c or "") for c in chunks)
    return "\n".join(text.splitlines()[-_LOG_TAIL_LINES:])


def run_slice(cmd: list[str], out_dir: Path, timeout_s: int = DEFAULT_TIMEOUT_S, runner=subprocess.run) -> SliceRun:
    """Run the CLI (no shell) and read result.json. subprocess kills the child on timeout."""
    result_file = out_dir / "result.json"
    result_file.unlink(missing_ok=True)  # a stale file must not pass for this run
    try:
        proc = runner(cmd, capture_output=True, timeout=timeout_s, shell=False)
    except subprocess.TimeoutExpired as e:
        return SliceRun(-1, f"slice timed out after {timeout_s} s", _tail(e.stdout, e.stderr), timed_out=True)
    code, err = proc.returncode, ""
    try:
        data = json.loads(result_file.read_text(encoding="utf-8"))
        code, err = int(data.get("return_code", code)), str(data.get("error_string", ""))
    except (OSError, ValueError, TypeError):
        pass
    return SliceRun(code, err, _tail(proc.stdout, proc.stderr))
