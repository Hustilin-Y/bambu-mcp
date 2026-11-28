"""Build and run the Bambu Studio CLI command (recipe from docs/phase0-report.md)."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from bambu_mcp.manifest import INFILL_PATTERNS
from bambu_profiles import PrinterProfile, load_profile
from slice_agent.config import AgentConfig
from slice_agent.profiles import _slug

DEFAULT_TIMEOUT_S = 300
OUT_SUFFIX = ".gcode.3mf"
# The P2S defaults; a printer's own come from its profile (bambu_profiles).
PLATE_TYPES = load_profile("p2s").plate_types
LAYER_HEIGHTS = load_profile("p2s").layer_heights
# Override whitelist values (global constraints); ranges are inclusive.
DENSITY_RANGE = (0, 100)  # percent, sent as "<n>%"
WALL_LOOPS_RANGE = (1, 10)
WALL_GENERATORS = ("arachne", "classic")
SHELL_LAYERS_RANGE = (0, 20)  # top_shell_layers and bottom_shell_layers
OUTER_WALL_SPEED_RANGE = (20, 300)  # mm/s, int or float
# Spellings checked against Studio 02.08 P2S project_settings.config (tests/fixtures/studio).
SEAM_POSITIONS = ("aligned", "back", "nearest", "random")
IRONING_TYPES = ("no ironing", "top", "topmost")
SUPPORT_TYPES = ("tree(auto)", "normal(auto)")
BRIM_TYPES = ("no_brim", "auto_brim", "outer_only")
_OUT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_RESERVED = re.compile(r"(con|prn|aux|nul|com[1-9]|lpt[1-9])", re.IGNORECASE)
_LOG_TAIL_LINES = 20
_LOG_TAIL_BYTES = 8192
_KILL_WAIT_S = 10
TASKKILL_FALLBACK = r"C:\Windows\System32\taskkill.exe"  # never a bare name: no PATH lookup


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _choice(options: tuple[str, ...]):
    return lambda v: isinstance(v, str) and v in options


def _in_range(bounds: tuple[int, int]):
    return lambda v: _is_int(v) and bounds[0] <= v <= bounds[1]


def _number_in_range(bounds: tuple[int, int]):
    # NaN and inf fail the comparison, so they are refused too.
    return lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and bounds[0] <= v <= bounds[1]


def _checks(nozzle: str, profile: PrinterProfile | None = None) -> dict:
    """The override whitelist: key -> validator. Its keys are ``OVERRIDE_KEYS``, in order.

    Plate types and layer heights come from ``profile`` (default P2S).
    """
    profile = profile or load_profile()
    heights = profile.layer_heights.get(nozzle, ())
    return {
        "sparse_infill_density": _in_range(DENSITY_RANGE),
        "sparse_infill_pattern": _choice(INFILL_PATTERNS),
        "wall_loops": _in_range(WALL_LOOPS_RANGE),
        "wall_generator": _choice(WALL_GENERATORS),
        "enable_support": lambda v: isinstance(v, bool),
        "support_type": _choice(SUPPORT_TYPES),
        "brim_type": _choice(BRIM_TYPES),
        "curr_bed_type": _choice(profile.plate_types),
        "layer_height": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v in heights,
        "top_shell_layers": _in_range(SHELL_LAYERS_RANGE),
        "bottom_shell_layers": _in_range(SHELL_LAYERS_RANGE),
        "outer_wall_speed": _number_in_range(OUTER_WALL_SPEED_RANGE),
        "seam_position": _choice(SEAM_POSITIONS),
        "ironing_type": _choice(IRONING_TYPES),
    }


OVERRIDE_KEYS = tuple(_checks(""))  # the keys do not depend on the nozzle


@dataclass(frozen=True)
class Overrides:
    """Validated whitelist of slicer settings: key -> value (python types)."""

    values: dict[str, object]

    @classmethod
    def parse(cls, raw: dict, nozzle: str, profile: PrinterProfile | None = None) -> Overrides:
        checks = _checks(nozzle, profile)
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


def check_out_name(name: str) -> None:
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
    out_dir: Path | None = None,
) -> list[str]:
    """argv for the Studio CLI. Names are checked against the config before they reach a path."""
    check_out_name(out_name)
    if nozzle not in cfg.machine:
        raise ValueError(f"unknown nozzle: {nozzle!r}")
    if process_set not in cfg.process_sets:
        raise ValueError(f"unknown process_set: {process_set!r}")
    if filament not in cfg.filaments:
        raise ValueError(f"unknown filament: {filament!r}")
    plate_type = str(overrides.values.get("curr_bed_type", plate_type))
    if plate_type not in cfg.profile.plate_types:
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
        "--export-3mf", f"{out_name}{OUT_SUFFIX}",  # the CLI adds no extension
        "--outputdir", str(out_dir or cfg.sliced_dir),
        str(input_3mf),
    ]


@dataclass(frozen=True)
class SliceRun:
    return_code: int
    error_string: str = ""
    log_tail: str = ""
    timed_out: bool = False


def _tail_file(path: Path) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - _LOG_TAIL_BYTES))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-_LOG_TAIL_LINES:])


def _popen_flags() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def taskkill_exe(env: Mapping[str, str] | None = None) -> str:
    """taskkill by absolute path so a planted taskkill.exe on PATH or in the cwd is never run."""
    env = os.environ if env is None else env
    root = env.get("SystemRoot")
    return str(Path(root) / "System32" / "taskkill.exe") if root else TASKKILL_FALLBACK


def kill_tree(proc) -> None:
    """Kill the process and everything it spawned. Fixed argv, no shell."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                [taskkill_exe(), "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, timeout=_KILL_WAIT_S, shell=False, check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()  # fallback for the direct child
    except OSError:
        pass


def _result_json(path: Path) -> tuple[int | None, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, ""
        return int(data["return_code"]), str(data.get("error_string", ""))
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return None, ""


def run_slice(
    cmd: list[str],
    out_dir: Path,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    popen=subprocess.Popen,
    killer=kill_tree,
) -> SliceRun:
    """Run the CLI (no shell, no pipes) and read result.json.

    Output goes to log files in out_dir so a surviving grandchild cannot block us on a pipe.
    On timeout the whole process tree is killed and the wait is bounded.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    result_file = out_dir / "result.json"
    result_file.unlink(missing_ok=True)  # a stale file must not pass for this run
    log = out_dir / "studio.log"
    with log.open("wb") as sink:
        proc = popen(cmd, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT, shell=False, **_popen_flags())
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            killer(proc)
            try:
                proc.wait(timeout=_KILL_WAIT_S)
            except subprocess.TimeoutExpired:
                pass
            sink.flush()
            return SliceRun(-1, f"slice timed out after {timeout_s} s", _tail_file(log), timed_out=True)
    code, err = _result_json(result_file)
    exit_code = proc.returncode
    if exit_code and not code:  # nonzero exit wins over a missing result.json or one saying 0
        code, err = exit_code, f"exit code {exit_code}"
    return SliceRun(code or 0, err, _tail_file(log))


def slice_isolated(
    cfg: AgentConfig,
    *,
    out_name: str,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    popen=subprocess.Popen,
    killer=kill_tree,
    **build_kwargs,
) -> tuple[SliceRun, Path | None]:
    """Slice in a temporary work dir under sliced_dir, move only the .gcode.3mf out on success.

    Returns (run, final_path). The work dir (plate gcode, result.json, log) is always removed.
    """
    work = cfg.sliced_dir / f".work-{out_name}"
    cmd = build_command(cfg, out_name=out_name, out_dir=work, **build_kwargs)  # validates out_name first
    try:
        if work.exists():
            shutil.rmtree(work)  # a leftover from a crashed run; failure propagates (OSError)
        work.mkdir(parents=True)
        run = run_slice(cmd, work, timeout_s, popen, killer)
        produced = work / f"{out_name}{OUT_SUFFIX}"
        if run.return_code != 0 or not produced.is_file():
            return run, None
        final = cfg.sliced_dir / produced.name
        produced.replace(final)
        return run, final
    finally:
        shutil.rmtree(work, ignore_errors=True)
