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
        ("grid", 0.9), ("rectilinear", 0.9),
        ("honeycomb", 0.8),
        ("lightning", 0.6),
        ("triangles", 0.7), ("zig-zag", 0.7), ("", 0.7), (None, 0.7),
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
