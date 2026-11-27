"""Print goals and the evaluation metrics for sliced variants.

The strength proxy compares variants of one part against the run's balanced baseline:
``S = V_outer + V_inner + V_top + V_bottom + V_internal_solid + k(pattern) * V_sparse``, from the
feature volumes (mm³) the slice-agent measures in the gcode. Support and other extrusion never count.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from bambu_mcp.manifest import GOAL_KEYS, Manifest, is_goal
from bambu_mcp.proposal import job_hints
from bambu_mcp.store.models import Job

# The keys of a plate's `features` from the slice-agent (slice_agent.output.FEATURES).
FEATURE_KEYS = (
    "outer_wall",
    "inner_wall",
    "top_surface",
    "bottom_surface",
    "sparse_infill",
    "internal_solid_infill",
    "support",
    "other",
)
SOLID_KEYS = ("outer_wall", "inner_wall", "top_surface", "bottom_surface", "internal_solid_infill")

# UNCALIBRATED: the builder's starting k per sparse_infill_pattern (spec 5.4), not measured on
# printed samples. Recalibrate from break tests before trusting small differences.
INFILL_K = {
    "gyroid": 1.0,
    "cubic": 1.0,
    "adaptivecubic": 1.0,
    "grid": 0.9,
    "rectilinear": 0.9,
    "zig-zag": 0.9,  # Bambu Studio serializes its "Rectilinear" as zig-zag
    "honeycomb": 0.8,
    "lightning": 0.6,
}
DEFAULT_K = 0.7


def infill_k(pattern: str | None) -> float:
    """k for a sparse_infill_pattern value, DEFAULT_K for any other or none."""
    return INFILL_K.get(pattern.strip().lower(), DEFAULT_K) if isinstance(pattern, str) else DEFAULT_K


def _volume(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def strength_proxy(features: object, pattern: str | None) -> float | None:
    """S for one variant, None when the features are missing or unusable (shown as "?")."""
    if not isinstance(features, dict):
        return None
    volumes = {k: _volume(features.get(k)) for k in FEATURE_KEYS}
    if any(v is None for v in volumes.values()):
        return None
    solid = sum(volumes[k] for k in SOLID_KEYS)  # type: ignore[misc]
    return solid + infill_k(pattern) * volumes["sparse_infill"]  # type: ignore[operator]


# -- goals --------------------------------------------------------------------------


@dataclass(frozen=True)
class Limits:
    """A goal's hard limits (spec 5.1 and 5.8).

    The ratios compare a variant with the run's balanced baseline: the evaluation checks them,
    it has the metrics. ``limits_ok`` checks the settings rules.
    """

    min_strength: float | None = None  # strength >= this x the baseline's
    max_time: float | None = None  # minutes <= this x the baseline's
    min_walls: int | None = None
    wall_generator: str | None = None
    max_outer_wall_speed: float | None = None  # mm/s
    min_top_bottom: int | None = None  # both top_shell_layers and bottom_shell_layers


@dataclass(frozen=True)
class Goal:
    """A print goal: its Russian label, the candidate grid, the base point, the score and limits.

    ``base`` is the point a goal applies at once, without slicing (the owner's press, the CAD
    agent's goal or the fallback): the middle point of ``grid``. ``score`` names the metrics
    in priority order, from "time", "cost", "strength", "grams", "layer", "risk" (dimensional
    risk) and "shell" (shell thickness). Balanced weighs its three equally.
    """

    key: str
    label: str
    base: dict[str, object]
    grid: tuple[dict[str, object], ...]
    score: tuple[str, ...]
    limits: Limits = field(default_factory=Limits)


# Shell counts are absolute. On the P2S 0.20 mm process (top 5, bottom 3) strength's 7/5 is the
# owner's "top/bottom +2". No base point sets the brim: cheap's "no brim unless the checks ask"
# needs the checks, so the brim stays with the hints. No base point irons: ironing pays only on
# flat tops, which the manifest does not describe (the quality grid tries it once).
_STRENGTH_SHELLS = {"top_shell_layers": 7, "bottom_shell_layers": 5}
_MIN_SHELLS = {"top_shell_layers": 3, "bottom_shell_layers": 2}
_BALANCED = {"wall_loops": 3, "sparse_infill_density": 15, "sparse_infill_pattern": "gyroid"}
_SPEED = {"layer_height": 0.24, "wall_loops": 2, "sparse_infill_density": 10, "sparse_infill_pattern": "grid"}
_STRENGTH = {"wall_loops": 4, "sparse_infill_density": 40, "sparse_infill_pattern": "gyroid", **_STRENGTH_SHELLS}
_QUALITY = {"layer_height": 0.16, "outer_wall_speed": 100, "seam_position": "back"}
_CHEAP = {
    "wall_loops": 2, "sparse_infill_density": 8, "sparse_infill_pattern": "lightning",
    "top_shell_layers": 4, "bottom_shell_layers": 3,
}
_LIGHT = {
    "wall_loops": 2, "sparse_infill_density": 5, "sparse_infill_pattern": "lightning",
    "top_shell_layers": 4, "bottom_shell_layers": 3,
}
_FIT = {"wall_loops": 3, "wall_generator": "classic", "outer_wall_speed": 50, "layer_height": 0.16}
_WATERTIGHT = {"wall_loops": 5, "top_shell_layers": 6, "bottom_shell_layers": 6}

GOALS: dict[str, Goal] = {
    "balanced": Goal(
        "balanced", "Обычно", _BALANCED,
        (
            {"wall_loops": 2, "sparse_infill_density": 10, "sparse_infill_pattern": "grid"},
            {"wall_loops": 2, "sparse_infill_density": 15, "sparse_infill_pattern": "gyroid"},
            _BALANCED,
            {"wall_loops": 3, "sparse_infill_density": 20, "sparse_infill_pattern": "grid"},
            {"wall_loops": 3, "sparse_infill_density": 25, "sparse_infill_pattern": "gyroid"},
        ),
        ("time", "cost", "strength"),
    ),
    "speed": Goal(
        "speed", "Быстро", _SPEED,
        (
            {**_SPEED, "sparse_infill_density": 5, "sparse_infill_pattern": "lightning"},
            {**_SPEED, "layer_height": 0.28, "sparse_infill_density": 5, "sparse_infill_pattern": "lightning"},
            _SPEED,
            {**_SPEED, "layer_height": 0.28},
            {**_SPEED, "layer_height": 0.28, "sparse_infill_density": 15},
        ),
        ("time",),
        Limits(min_strength=0.6),
    ),
    "strength": Goal(
        "strength", "Прочно", _STRENGTH,
        (
            {**_STRENGTH, "wall_loops": 3, "sparse_infill_density": 25},
            {**_STRENGTH, "sparse_infill_density": 30, "sparse_infill_pattern": "cubic"},
            _STRENGTH,
            {**_STRENGTH, "wall_loops": 5, "sparse_infill_density": 50, "sparse_infill_pattern": "adaptivecubic"},
            {**_STRENGTH, "wall_loops": 6, "sparse_infill_density": 60},
        ),
        ("strength", "time"),
        Limits(max_time=2.5),
    ),
    "quality": Goal(
        "quality", "Красиво", _QUALITY,
        (
            {"layer_height": 0.12, "outer_wall_speed": 80, "seam_position": "aligned"},
            {"layer_height": 0.12, "outer_wall_speed": 100, "seam_position": "back"},
            _QUALITY,
            {"layer_height": 0.16, "outer_wall_speed": 80, "seam_position": "aligned"},
            {**_QUALITY, "ironing_type": "top"},
        ),
        ("layer", "time"),
        Limits(max_time=3.0),
    ),
    "cheap": Goal(
        "cheap", "Дёшево", _CHEAP,
        (
            {**_CHEAP, "sparse_infill_density": 0, **_MIN_SHELLS},
            {**_CHEAP, "sparse_infill_density": 5},
            _CHEAP,
            {**_CHEAP, "sparse_infill_density": 12},
            {**_CHEAP, "sparse_infill_density": 12, **_MIN_SHELLS},
        ),
        ("cost",),
        Limits(min_strength=0.5),
    ),
    "light": Goal(
        "light", "Легко", _LIGHT,
        (
            {**_LIGHT, "sparse_infill_density": 0, **_MIN_SHELLS},
            {**_LIGHT, "sparse_infill_pattern": "honeycomb"},
            _LIGHT,
            {**_LIGHT, "sparse_infill_density": 10},
            {**_LIGHT, "sparse_infill_density": 10, "sparse_infill_pattern": "honeycomb"},
        ),
        ("grams",),
        Limits(min_strength=0.4),
    ),
    "fit": Goal(
        "fit", "Точно", _FIT,
        (
            {**_FIT, "outer_wall_speed": 60, "layer_height": 0.2},
            {**_FIT, "outer_wall_speed": 40, "layer_height": 0.12},
            _FIT,
            {**_FIT, "wall_loops": 4},
            {**_FIT, "wall_loops": 4, "outer_wall_speed": 40, "layer_height": 0.12},
        ),
        ("risk", "time"),
        Limits(min_walls=3, wall_generator="classic", max_outer_wall_speed=60),
    ),
    "watertight": Goal(
        "watertight", "Герметично", _WATERTIGHT,
        (
            {"wall_loops": 4, "top_shell_layers": 5, "bottom_shell_layers": 5},
            {"wall_loops": 4, "top_shell_layers": 6, "bottom_shell_layers": 6},
            _WATERTIGHT,
            {"wall_loops": 6, "top_shell_layers": 6, "bottom_shell_layers": 6},
            {"wall_loops": 6, "top_shell_layers": 7, "bottom_shell_layers": 7},
        ),
        ("shell",),
        Limits(min_walls=4, min_top_bottom=5),
    ),
}
DEFAULT_GOAL = "balanced"


def goal_base(primary: str, secondary: str | None = None) -> dict[str, object]:
    """The goal settings layer: the primary's base point, the secondary's fills its gaps.

    The primary's keys win, so its limits hold. ``KeyError`` for an unknown goal.
    """
    base = dict(GOALS[primary].base)
    if secondary is not None:
        base = {**GOALS[secondary].base, **base}
    return base


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def broken_rules(goal: Goal, settings: Mapping) -> list[str]:
    """The setting keys that break the goal's settings rules (fit, watertight), in rule order.

    A setting a rule needs but ``settings`` leave to the process preset breaks it: the
    preset's value is not known here.
    """
    lim = goal.limits
    out = []
    walls = _number(settings.get("wall_loops"))
    if lim.min_walls is not None and (walls is None or walls < lim.min_walls):
        out.append("wall_loops")
    if lim.wall_generator is not None and settings.get("wall_generator") != lim.wall_generator:
        out.append("wall_generator")
    speed = _number(settings.get("outer_wall_speed"))
    if lim.max_outer_wall_speed is not None and (speed is None or speed > lim.max_outer_wall_speed):
        out.append("outer_wall_speed")
    if lim.min_top_bottom is not None:
        for key in ("top_shell_layers", "bottom_shell_layers"):
            layers = _number(settings.get(key))
            if layers is None or layers < lim.min_top_bottom:
                out.append(key)
    return out


def limits_ok(goal: Goal, settings: Mapping, checks: Mapping | None) -> list[str]:
    """The goal's settings rules and manifest checks that ``settings`` break, in English.

    Empty: they pass. See ``broken_rules`` for a setting left to the process preset.
    """
    lim = goal.limits
    rules = {
        "wall_loops": f"wall_loops must be at least {lim.min_walls}",
        "wall_generator": f"wall_generator must be {lim.wall_generator}",
        "outer_wall_speed": f"outer_wall_speed must be at most {lim.max_outer_wall_speed or 0:g}",
        "top_shell_layers": f"top_shell_layers must be at least {lim.min_top_bottom}",
        "bottom_shell_layers": f"bottom_shell_layers must be at least {lim.min_top_bottom}",
    }
    out = [f"{goal.key}: {rules[key]}" for key in broken_rules(goal, settings)]
    if (checks or {}).get("supports_suggested") and not settings.get("enable_support"):
        out.append("the checks suggest supports, enable_support is off")
    return out


def parse_goal(value: object) -> tuple[str, str | None]:
    """(primary, secondary) from a goal key or a list of one or two different keys.

    ``ValueError`` naming the allowed keys otherwise (the manifest hint's rule).
    """
    if not is_goal(value):
        raise ValueError(
            f"goal must be one of {', '.join(GOAL_KEYS)}, or a list of two different ones, got {value!r}"
        )
    keys = [value] if isinstance(value, str) else list(value)  # type: ignore[call-overload]
    return keys[0], (keys[1] if len(keys) > 1 else None)


def goal_record(primary: str, secondary: str | None) -> str:
    """``jobs.goal_json`` for a goal the owner chose."""
    return json.dumps({"primary": primary, "secondary": secondary, "source": "owner"})


def _stored_goal(goal_json: str | None) -> tuple[str, str | None] | None:
    try:
        data = json.loads(goal_json or "null")
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    secondary = data.get("secondary")
    try:
        return parse_goal([data.get("primary")] + ([secondary] if secondary is not None else []))
    except ValueError:
        return None


def job_goal(job: Job, manifest: Manifest) -> tuple[str, str | None, str, str | None]:
    """(primary, secondary, source, reason) of a job, the first that applies (spec 5.2, 5.8):

    the owner's goal stored on the job (source ``owner``), the CAD agent's ``goal`` hint with
    its ``goal_reason`` (``assistant``), else ``balanced`` (``fallback``). A stored goal that
    does not read is ignored.
    """
    stored = _stored_goal(job.goal_json)
    if stored is not None:
        return stored[0], stored[1], "owner", None
    hints = job_hints(manifest)
    if "goal" in hints:
        primary, secondary = parse_goal(hints["goal"])  # checked at intake
        reason = hints.get("goal_reason")
        return primary, secondary, "assistant", reason if isinstance(reason, str) and reason else None
    return DEFAULT_GOAL, None, "fallback", None
