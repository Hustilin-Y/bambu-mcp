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


# -- the evaluation run: candidates and score (Task 6) ---------------------------------

from bambu_mcp.evaluation import MAX_VARIANTS, VariantMetrics, candidates, score  # noqa: E402

HINTED = {
    "wall_loops": 4, "sparse_infill_density": 25, "sparse_infill_pattern": "gyroid", "enable_support": False,
}


def test_candidates_baseline_first_at_most_six_all_valid():
    base = {**HINTED, **GOALS["strength"].base}
    out = candidates("strength", {}, base, {}, "0.4")
    assert out[0] == base
    assert 2 <= len(out) <= MAX_VARIANTS == 6
    assert len({json.dumps(c, sort_keys=True) for c in out}) == len(out)  # no duplicates
    for c in out:
        Overrides.parse(c, "0.4")
        assert c["enable_support"] is False  # the hint the grid does not touch stays
    assert {c["wall_loops"] for c in out} >= {3, 4, 5, 6}


def test_explicit_keys_fixed_in_every_variant():
    explicit = {"wall_loops": 2, "sparse_infill_pattern": "honeycomb"}
    base = {**HINTED, **explicit}
    for key in GOALS:
        for c in candidates(key, explicit, base, {}, "0.4"):
            assert c["wall_loops"] == 2 and c["sparse_infill_pattern"] == "honeycomb", key


def test_candidates_keep_the_goal_rules_and_the_checks():
    fit = candidates("fit", {}, dict(HINTED), {}, "0.4")
    assert fit[0] == HINTED  # the baseline is the reference, whatever its rules
    for c in fit[1:]:
        assert limits_ok(GOALS["fit"], c, {}) == []
    base = {**HINTED, "enable_support": True}
    for c in candidates("cheap", {}, base, {"supports_suggested": True}, "0.4"):
        assert c["enable_support"] is True


def _m(minutes, grams=10.0, cost=10.0, strength=1000.0, **settings) -> VariantMetrics:
    return VariantMetrics(settings=settings, minutes=minutes, grams=grams, cost=cost, strength=strength)


def test_winner_by_goal_score_and_limits():
    # speed: the fastest whose strength is at least 0.6 x the baseline's.
    runs = [_m(100), _m(50, strength=500), _m(60, strength=700), _m(80, strength=900)]
    assert score(runs, ("speed", None)) == [2, 3, 0]
    # strength: the strongest within 2.5 x the baseline's time.
    runs = [_m(100), _m(300, strength=3000), _m(200, strength=1800), _m(150, strength=1800)]
    assert score(runs, ("strength", None))[:2] == [3, 2]
    # light: grams. cheap: total cost.
    assert score([_m(100, grams=10), _m(100, grams=7), _m(100, grams=8)], ("light", None))[0] == 1
    assert score([_m(100, cost=10), _m(100, cost=12), _m(100, cost=9)], ("cheap", None))[0] == 2
    # A failed variant is never ranked.
    failed = VariantMetrics(settings={}, minutes=None, grams=None, cost=None, strength=None, ok=False)
    assert score([_m(100), failed], ("speed", None)) == [0]


def test_secondary_goal_breaks_ties():
    runs = [_m(100, cost=10), _m(80, cost=10), _m(120, cost=10)]
    assert score(runs, ("cheap", None))[0] == 0  # a tie: the earlier one
    assert score(runs, ("cheap", "speed")) == [1, 0, 2]


def test_score_without_strength_uses_the_other_metrics():
    runs = [_m(100, strength=None), _m(50, strength=None), _m(70, strength=None)]
    # The strength limit cannot be checked: no variant is dropped for it.
    assert score(runs, ("speed", None)) == [1, 2, 0]
    assert sorted(score(runs, ("balanced", None))) == [0, 1, 2]


# -- final review fixes: the balanced baseline, rules and the unknown first metric -------

from bambu_mcp.evaluation import base_variant  # noqa: E402


def test_candidates_with_a_balanced_baseline_keep_the_goal_base_point():
    balanced = {**HINTED, **GOALS["balanced"].base}
    goal_settings = {**HINTED, **GOALS["strength"].base}
    out = candidates("strength", {}, goal_settings, {}, "0.4", baseline=balanced)
    assert out[0] == balanced and len(out) == 6
    assert base_variant("strength", {}, goal_settings, "0.4") in out[1:]  # the base point is a variant
    explicit = {"wall_loops": 4}
    out = candidates("strength", explicit, {**goal_settings, **explicit}, {}, "0.4", baseline={**balanced, **explicit})
    assert {c["wall_loops"] for c in out} == {4}


def test_rule_breaking_variants_never_win_unless_fixed():
    fit_ok = {"wall_loops": 3, "wall_generator": "classic", "outer_wall_speed": 50}
    runs = [_m(50, **HINTED), _m(80, **fit_ok), _m(70, **{**fit_ok, "outer_wall_speed": 80})]
    assert score(runs, ("fit", None)) == [1]  # the faster baseline and variant 3 break the rules
    # A rule the owner's explicit setting breaks does not count against the variants.
    assert score(runs, ("fit", None), fixed={"outer_wall_speed", "wall_generator", "wall_loops"}) == [1, 2, 0]


def test_strength_unknown_falls_back_to_the_base_point():
    runs = [_m(100, strength=None), _m(50, strength=None), _m(150, strength=None), _m(120, strength=None)]
    assert score(runs, ("strength", None), base_index=3)[0] == 3
    assert score(runs, ("strength", None))[0] == 1  # no base point given: the second metric
    # Known strength: the base point gets no favour.
    known = [_m(100), _m(50, strength=2000), _m(150, strength=1500), _m(120, strength=1200)]
    assert score(known, ("strength", None), base_index=3)[0] == 1
