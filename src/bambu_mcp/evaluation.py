"""Print goals and the evaluation metrics for sliced variants.

The strength proxy compares variants of one part against the run's balanced baseline:
``S = V_outer + V_inner + V_top + V_bottom + V_internal_solid + k(pattern) * V_sparse``, from the
feature volumes (mm³) the slice-agent measures in the gcode. Support and other extrusion never count.
"""

from __future__ import annotations

import json
import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field

from bambu_mcp.manifest import GOAL_KEYS, Manifest, is_goal
from bambu_mcp.proposal import job_hints
from bambu_mcp.store.models import Job
from slice_agent.runner import Overrides

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


# -- the evaluation run: candidates and score ----------------------------------------

MAX_VARIANTS = 6  # slices per run, the baseline included


def _fixed_by(explicit: Mapping, keys: list[str]) -> bool:
    return all(k in explicit for k in keys)


def candidates(
    goal: str,
    explicit: Mapping,
    base: Mapping,
    checks: Mapping | None,
    nozzle: str,
    *,
    baseline: Mapping | None = None,
) -> list[dict[str, object]]:
    """The settings an evaluation run slices: the baseline first, then the goal's grid.

    ``baseline`` is the job resolved with the balanced base point (spec 5.1: the ratio limits
    compare with it), ``base`` the job resolved with the goal's own base point, both without a
    chosen variant (``baseline`` None: ``base``). Each grid point goes over ``base`` and
    ``explicit`` (the owner's words, ruling R2) over the point, so explicit keys are the same
    in every variant. The goal's base point is one of the grid points, so it is sliced too.
    A point that breaks the goal's rules or the manifest's checks (unless explicit keys are
    what breaks them), that fails the whitelist for ``nozzle``, or that repeats an earlier one
    is dropped. At most ``MAX_VARIANTS``.
    """
    target = GOALS[goal]
    first = {**(base if baseline is None else baseline), **explicit}
    out = [first]
    seen = {json.dumps(first, sort_keys=True)}
    for point in target.grid:
        settings = {**base, **point, **explicit}
        try:
            settings = dict(Overrides.parse(settings, nozzle).values)
        except ValueError:
            continue
        broken = broken_rules(target, settings)
        if broken and not _fixed_by(explicit, broken):
            continue
        if (checks or {}).get("supports_suggested") and not settings.get("enable_support"):
            if "enable_support" not in explicit:
                continue
        key = json.dumps(settings, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out.append(settings)
        if len(out) == MAX_VARIANTS:
            break
    return out


def base_variant(goal: str, explicit: Mapping, base: Mapping, nozzle: str) -> dict[str, object]:
    """The candidate ``candidates`` builds from the goal's base point (to find it in a run)."""
    settings = {**base, **GOALS[goal].base, **explicit}
    return dict(Overrides.parse(settings, nozzle).values)


@dataclass(frozen=True)
class VariantMetrics:
    """One sliced variant of a run: ``ok`` False when it failed to slice (never ranked).

    ``cost`` is the predicted total (None: unknown), ``strength`` the raw ``strength_proxy``.
    """

    settings: Mapping
    minutes: float | None
    grams: float | None
    cost: float | None
    strength: float | None
    ok: bool = True


# Rounding before comparing, so close results tie and the secondary goal decides.
_ROUND = {"time": 0, "cost": 2, "grams": 1, "strength": 2, "layer": 2, "risk": 2, "shell": 0}
_DEFAULT_LAYER = 0.2  # the 0.20 mm standard process when the settings leave it to the preset


def _setting(settings: Mapping, key: str, default: float) -> float:
    value = _number(settings.get(key))
    return default if value is None else value


def _metric(name: str, v: VariantMetrics, base: VariantMetrics, known: set[str]) -> float:
    """A metric where lower is better. Unknown metrics count 0 for every variant."""
    s = v.settings
    if name == "time":
        return v.minutes or 0.0
    if name == "cost":
        return v.cost if "cost" in known else 0.0  # type: ignore[return-value]
    if name == "grams":
        return v.grams or 0.0
    if name == "strength":
        return -(v.strength / base.strength) if "strength" in known else 0.0  # type: ignore[operator]
    if name == "layer":
        return _setting(s, "layer_height", _DEFAULT_LAYER)
    if name == "risk":
        # Dimensional risk: a fast outer wall, a thick layer and few walls, equal weights.
        speed = _setting(s, "outer_wall_speed", 200.0)
        walls = max(_setting(s, "wall_loops", 2.0), 1.0)
        return speed / 60 + _setting(s, "layer_height", _DEFAULT_LAYER) / 0.2 + 3 / walls
    if name == "shell":
        walls = _setting(s, "wall_loops", 2.0)
        shells = min(_setting(s, "top_shell_layers", 3.0), _setting(s, "bottom_shell_layers", 3.0))
        return -(walls + shells)
    raise KeyError(name)


def _goal_key(goal: Goal, v: VariantMetrics, base: VariantMetrics, known: set[str]) -> tuple[float, ...]:
    if goal.key == "balanced":
        # Equal weights: time, cost and strength, each relative to the baseline.
        total = (v.minutes or 0.0) / (base.minutes or 1.0)
        if "cost" in known and base.cost:
            total += v.cost / base.cost  # type: ignore[operator]
        if "strength" in known:
            total -= v.strength / base.strength  # type: ignore[operator]
        return (round(total, 2),)
    return tuple(round(_metric(m, v, base, known), _ROUND[m]) for m in goal.score)


def _within_limits(goal: Goal, v: VariantMetrics, base: VariantMetrics, known: set[str]) -> bool:
    lim = goal.limits
    if lim.max_time is not None and base.minutes and (v.minutes or 0.0) > lim.max_time * base.minutes:
        return False
    if lim.min_strength is not None and "strength" in known:
        return v.strength >= lim.min_strength * base.strength  # type: ignore[operator]
    return True


def known_metrics(variants: Sequence[VariantMetrics]) -> set[str]:
    """"strength" and "cost" when every sliced variant (and the baseline) has them."""
    if not variants or not variants[0].ok:
        return set()
    ok = [v for v in variants if v.ok]
    known = set()
    if all(v.strength is not None for v in ok) and variants[0].strength:
        known.add("strength")
    if all(v.cost is not None for v in ok):
        known.add("cost")
    return known


def first_metric_unknown(variants: Sequence[VariantMetrics], goal: str) -> bool:
    """True when the goal's first score metric is "?" in this run (an agent without features)."""
    first = GOALS[goal].score[0]
    return first in ("strength", "cost") and first not in known_metrics(variants)


def score(
    variants: Sequence[VariantMetrics],
    goal: tuple[str, str | None],
    *,
    fixed: Collection[str] = (),
    base_index: int | None = None,
) -> list[int]:
    """Indexes of the variants that pass the primary goal's limits, best first.

    ``variants[0]`` is the baseline the ratio limits compare with. A variant that breaks the
    primary goal's settings rules (fit, watertight) is not ranked, except for rules on the
    ``fixed`` keys (the owner's explicit settings). The primary goal's score ranks, the
    secondary's breaks ties, then the slicing order. Strength counts only when every sliced
    variant has it (else it is "?": the strength limits are not checked), the same for the
    cost. When the primary's first metric is "?", ``base_index`` (the goal's base point
    variant) comes first: the next metric alone would pick, say, the weakest variant for
    ``strength``. A failed variant is never ranked. Empty when the baseline failed.
    """
    if not variants or not variants[0].ok:
        return []
    base = variants[0]
    known = known_metrics(variants)
    primary = GOALS[goal[0]]
    secondary = GOALS[goal[1]] if goal[1] is not None else None
    eligible = [
        i
        for i, v in enumerate(variants)
        if v.ok
        and _within_limits(primary, v, base, known)
        and not set(broken_rules(primary, v.settings)) - set(fixed)
    ]

    def rank(i: int) -> tuple:
        v = variants[i]
        tie = _goal_key(secondary, v, base, known) if secondary is not None else ()
        return (*_goal_key(primary, v, base, known), *tie, i)

    ranking = sorted(eligible, key=rank)
    if base_index is not None and base_index in ranking and first_metric_unknown(variants, goal[0]):
        ranking.remove(base_index)
        ranking.insert(0, base_index)
    return ranking
