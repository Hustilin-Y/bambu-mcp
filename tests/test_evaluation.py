import math

import pytest

from bambu_mcp.evaluation import FEATURE_KEYS, infill_k, strength_proxy

FEATS = {
    "outer_wall": 100.0, "inner_wall": 200.0, "top_surface": 30.0, "bottom_surface": 20.0,
    "sparse_infill": 500.0, "internal_solid_infill": 50.0, "support": 999.0, "other": 77.0,
}
SOLID = 100 + 200 + 30 + 20 + 50  # support and other never count


@pytest.mark.parametrize(
    ("pattern", "k"),
    [
        ("gyroid", 1.0), ("cubic", 1.0), ("adaptivecubic", 1.0),
        ("grid", 0.9), ("rectilinear", 0.9), ("zig-zag", 0.9),  # Bambu serializes Rectilinear as zig-zag
        ("honeycomb", 0.8),
        ("lightning", 0.6),
        ("triangles", 0.7), ("", 0.7), (None, 0.7),
        (" Gyroid ", 1.0),  # the slicer's value, case and spaces aside
    ],
)
def test_strength_proxy_k_values(pattern, k):
    assert infill_k(pattern) == k
    assert strength_proxy(FEATS, pattern) == pytest.approx(SOLID + k * 500)


def test_feature_keys_match_the_agent():
    from slice_agent.output import FEATURES

    assert set(FEATURE_KEYS) == set(FEATURES)


@pytest.mark.parametrize(
    "features",
    [
        None,
        {},
        [],
        {k: v for k, v in FEATS.items() if k != "outer_wall"},
        {**FEATS, "sparse_infill": "lots"},
        {**FEATS, "sparse_infill": -1.0},
        {**FEATS, "sparse_infill": math.nan},
        {**FEATS, "top_surface": math.inf},
        {**FEATS, "inner_wall": True},
    ],
)
def test_strength_proxy_unknown_without_usable_features(features):
    assert strength_proxy(features, "gyroid") is None


# -- goals, base points and limits (Task 5) -------------------------------------------

import json  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402

from bambu_mcp.evaluation import GOALS, goal_base, job_goal, limits_ok  # noqa: E402
from bambu_mcp.manifest import GOAL_KEYS, parse_manifest  # noqa: E402
from bambu_mcp.store.models import Job  # noqa: E402
from slice_agent.runner import Overrides  # noqa: E402

MANIFESTS = Path(__file__).parent / "fixtures" / "manifest"
LABELS = {
    "balanced": "Обычно", "speed": "Быстро", "strength": "Прочно", "quality": "Красиво",
    "cheap": "Дёшево", "light": "Легко", "fit": "Точно", "watertight": "Герметично",
}


def test_goal_table_keys_labels_and_points():
    assert list(GOALS) == list(LABELS) == list(GOAL_KEYS)
    for key, goal in GOALS.items():
        assert goal.key == key and goal.label == LABELS[key]
        assert goal.base in goal.grid, key  # the base point is one of the grid points
        assert 1 <= len(goal.grid) <= 5, key  # at most 6 slices per run with the baseline
        for point in goal.grid:
            Overrides.parse(point, "0.4")  # every point passes the whitelist
        assert goal.score, key
    # The owner's example (spec 5.3): walls 4, infill 40 % gyroid, top and bottom +2 on the
    # P2S 0.20 mm process (top 5, bottom 3).
    assert GOALS["strength"].base == {
        "wall_loops": 4, "sparse_infill_density": 40, "sparse_infill_pattern": "gyroid",
        "top_shell_layers": 7, "bottom_shell_layers": 5,
    }


def test_goal_base_primary_wins_secondary_fills_gaps():
    assert goal_base("strength", None) == GOALS["strength"].base
    both = goal_base("strength", "quality")
    assert both == {**GOALS["quality"].base, **GOALS["strength"].base}
    assert both["wall_loops"] == 4 and both["layer_height"] == GOALS["quality"].base["layer_height"]
    with pytest.raises(KeyError):
        goal_base("ultra", None)


def test_fit_and_watertight_limits():
    for key in GOALS:
        for point in GOALS[key].grid:
            assert limits_ok(GOALS[key], point, {}) == [], (key, point)
    fit = GOALS["fit"]
    good = {"wall_loops": 3, "wall_generator": "classic", "outer_wall_speed": 60}
    assert limits_ok(fit, good, {}) == []
    assert len(limits_ok(fit, {**good, "wall_loops": 2}, {})) == 1
    assert len(limits_ok(fit, {**good, "wall_generator": "arachne"}, {})) == 1
    assert len(limits_ok(fit, {**good, "outer_wall_speed": 61}, {})) == 1
    assert len(limits_ok(fit, {}, {})) == 3  # a setting left to the preset is not known to pass
    water = GOALS["watertight"]
    good = {"wall_loops": 4, "top_shell_layers": 5, "bottom_shell_layers": 5}
    assert limits_ok(water, good, {}) == []
    assert len(limits_ok(water, {**good, "wall_loops": 3}, {})) == 1
    assert len(limits_ok(water, {**good, "top_shell_layers": 4, "bottom_shell_layers": 4}, {})) == 2
    # Every goal keeps the manifest's checks: supports when the checks suggest them.
    checks = {"supports_suggested": True}
    assert len(limits_ok(GOALS["speed"], {"enable_support": False}, checks)) == 1
    assert limits_ok(GOALS["speed"], {"enable_support": True}, checks) == []
    assert limits_ok(GOALS["speed"], {}, {"supports_suggested": False}) == []


def _goal_job(hints: dict | None = None, goal_json: str | None = None, kind: str = "part") -> tuple[Job, object]:
    raw = json.loads((MANIFESTS / f"{kind}.print.json").read_text(encoding="utf-8"))
    if hints is not None:
        raw["hints"] = hints
    job = Job(
        id=1, name=raw["name"], kind=raw["kind"], manifest_json=json.dumps(raw),
        file_sha256=raw["file_sha256"], created_at=datetime(2026, 11, 27, tzinfo=UTC), status="new",
        settings_json=None, result_json=None, sliced_file=None, error=None, goal_json=goal_json,
    )
    return job, parse_manifest(raw)


def test_goal_from_print_hints_with_reason():
    job, manifest = _goal_job({"goal": "strength", "goal_reason": "несущий кронштейн"})
    assert job_goal(job, manifest) == ("strength", None, "assistant", "несущий кронштейн")
    job, manifest = _goal_job({"goal": ["fit", "quality"]})
    assert job_goal(job, manifest) == ("fit", "quality", "assistant", None)
    # The owner's goal, stored on the job, wins over the assistant's.
    owner = json.dumps({"primary": "speed", "secondary": None, "source": "owner"})
    job, manifest = _goal_job({"goal": "strength", "goal_reason": "несущий"}, owner)
    assert job_goal(job, manifest) == ("speed", None, "owner", None)


def test_fallback_is_balanced():
    job, manifest = _goal_job({})
    assert job_goal(job, manifest) == ("balanced", None, "fallback", None)
    # A stored goal that no longer reads (an old or damaged row) falls back the same way.
    for bad in ("{", json.dumps({"primary": "ultra"}), json.dumps(["speed"])):
        job, manifest = _goal_job({}, bad)
        assert job_goal(job, manifest) == ("balanced", None, "fallback", None)
