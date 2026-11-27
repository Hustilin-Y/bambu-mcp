"""Evaluation metrics for sliced variants.

The strength proxy compares variants of one part against the run's balanced baseline:
``S = V_outer + V_inner + V_top + V_bottom + V_internal_solid + k(pattern) * V_sparse``, from the
feature volumes (mm³) the slice-agent measures in the gcode. Support and other extrusion never count.
"""

from __future__ import annotations

import math

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
