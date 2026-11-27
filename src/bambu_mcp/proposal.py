"""Slicer settings for a job, the AMS slot to print from, and its cost estimate.

Settings are slice-agent overrides (``slice_agent.runner.Overrides``): every dict
``resolve_settings`` returns passes ``Overrides.parse`` unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from bambu_mcp.config import Settings
from bambu_mcp.cost import CostBreakdown, CurrencyMismatch, full_cost, print_kwh
from bambu_mcp.manifest import Manifest
from bambu_mcp.printer.state import Tray
from bambu_mcp.store.models import Amortization, Job, Spool, Tariff
from slice_agent.runner import Overrides

DEFAULT_NOZZLE = "0.4"
SLOTS_PER_AMS = 4
AMS_UNITS_MAX = 4
MAX_SLOT_NUMBER = AMS_UNITS_MAX * SLOTS_PER_AMS  # the owner's slot numbers are 1..16

# Overrides on top of the slice-agent process set of the same name. The owner may edit them.
NAMED_SETS: dict[str, dict[str, object]] = {
    "standard": {},
    "strong": {"wall_loops": 4, "sparse_infill_density": 25},
    "draft": {"sparse_infill_density": 10, "layer_height": 0.28},
}

# Manifest hint -> override key, for hints whose value passes through unchanged.
_DIRECT_HINTS = {
    "walls": "wall_loops",
    "infill_pct": "sparse_infill_density",
    "infill_pattern": "sparse_infill_pattern",
}
SUPPORT_TYPES = {"tree": "tree(auto)", "normal": "normal(auto)"}
BRIM_TYPES = {"none": "no_brim", "auto": "auto_brim", "outer": "outer_only"}


def hint_overrides(hints: Mapping, checks: Mapping | None = None) -> dict[str, object]:
    """Manifest hints as overrides. Supports are on by default only when the checks suggest them."""
    out: dict[str, object] = {}
    for hint, key in _DIRECT_HINTS.items():
        if hint in hints:
            out[key] = hints[hint]
    if "brim" in hints:
        out["brim_type"] = BRIM_TYPES[hints["brim"]]
    supports = hints.get("supports", "auto")
    if supports == "none":
        out["enable_support"] = False
    elif supports == "auto":
        # The support type stays with the named set or the process preset.
        out["enable_support"] = bool((checks or {}).get("supports_suggested"))
    else:
        out["enable_support"] = True
        out["support_type"] = SUPPORT_TYPES[supports]
    return out


def _valid_entries(settings: Mapping | None, nozzle: str) -> dict[str, object]:
    """Entries of a stored settings dict that still pass the whitelist; the rest is dropped."""
    out: dict[str, object] = {}
    for key, value in (settings or {}).items():
        try:
            Overrides.parse({key: value}, nozzle)
        except ValueError:
            continue
        out[key] = value
    return out


def resolve_settings(
    explicit: Mapping,
    last_job: Mapping | None,
    hints: Mapping,
    named_set: str,
    *,
    checks: Mapping | None = None,
    nozzle: str = DEFAULT_NOZZLE,
) -> dict[str, object]:
    """Overrides for a slice: explicit > the last sliced job of the part > hints > named set.

    ``explicit`` is the owner's request: anything outside the whitelist raises ``ValueError``
    (the message names the key). ``last_job`` is that job's stored settings, entries that no
    longer pass are dropped. ``checks`` are the manifest (or kit plate) checks.
    """
    if named_set not in NAMED_SETS:
        raise ValueError(f"unknown named set: {named_set!r} (known: {', '.join(NAMED_SETS)})")
    explicit = dict(explicit)
    Overrides.parse(explicit, nozzle)
    if "support_type" in explicit and "enable_support" not in explicit:
        explicit["enable_support"] = True  # asking for a support type means wanting supports
    above = {**hint_overrides(hints, checks), **_valid_entries(last_job, nozzle), **explicit}
    for key, value in NAMED_SETS[named_set].items():
        if key in above:
            continue
        try:
            Overrides.parse({key: value}, nozzle)
        except ValueError:
            raise ValueError(
                f"named set {named_set!r}: {key}={value!r} is not valid for nozzle {nozzle}"
            ) from None
    merged = {**NAMED_SETS[named_set], **above}
    return dict(Overrides.parse(merged, nozzle).values)


def last_settings(jobs: Iterable[Job], name: str, exclude_id: int | None = None) -> dict | None:
    """Stored settings of the newest ``sliced`` job named ``name`` (``jobs`` newest first)."""
    for job in jobs:
        if job.name != name or job.status != "sliced" or job.id == exclude_id or not job.settings_json:
            continue
        try:
            settings = json.loads(job.settings_json)
        except (ValueError, RecursionError):
            continue
        if isinstance(settings, dict):
            return settings
    return None


def job_hints(manifest: Manifest) -> dict:
    """Hints for the whole job: a part's own, for a kit those every plate agrees on.

    A kit is sliced with one set of overrides, so a hint only one plate states is not
    applied, and a root hint applies only when no plate's own hint says otherwise.
    ``messages.render_proposal`` lists the plate hints the job does not follow.
    """
    if manifest.kind != "kit":
        return dict(manifest.plates[0].hints)
    first, *rest = manifest.plates
    agreed = {k: v for k, v in first.hints.items() if all(p.hints.get(k, object()) == v for p in rest)}
    root = {
        k: v for k, v in manifest.hints.items()
        if all(p.hints.get(k, v) == v for p in manifest.plates)
    }
    return {**root, **agreed}


def job_material(manifest: Manifest) -> tuple[str, str | None]:
    """(material, reason): the hinted material, else the profile material."""
    hints = job_hints(manifest)
    return str(hints.get("material") or manifest.material), hints.get("material_reason")


# -- AMS slot -----------------------------------------------------------------------


@dataclass(frozen=True)
class SlotChoice:
    """The slot to print from. ``ams``/``slot`` are None when nothing is loaded.

    ``warning`` is a Russian text ready for the message (Markdown escaped).
    """

    ams: int | None
    slot: int | None
    spool_id: int | None
    warning: str | None
    tray_type: str | None = None
    color_name: str | None = None
    remaining_g: float | None = None  # from the spool inventory, None when unknown


def slot_number(ams: int, slot: int) -> int:
    """The slot as the owner counts it: 1..4 on the first AMS, 5..8 on the second.

    With ``slot_position`` the only place that knows the numbering.
    """
    return ams * SLOTS_PER_AMS + slot + 1


def slot_position(number: int) -> tuple[int, int]:
    """(ams, slot) of the owner's slot ``number`` (1..16), the inverse of ``slot_number``."""
    if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= MAX_SLOT_NUMBER:
        raise ValueError(f"slot must be 1..{MAX_SLOT_NUMBER}, got {number!r}")
    return divmod(number - 1, SLOTS_PER_AMS)


@dataclass(frozen=True)
class UsableSlot:
    """A slot the owner can print from: loaded, with an active spool that has a preset."""

    number: int
    ams: int
    slot: int
    spool: Spool

    @property
    def label(self) -> str:
        """'2 PETG синий': plain text, for buttons and tool errors."""
        return f"{self.number} {self.spool.material} {self.spool.color_name}"


class SlotUnusable(ValueError):
    """The owner's slot cannot be printed from. ``usable`` lists the slots that can."""

    def __init__(self, number: int, usable: Sequence[UsableSlot]) -> None:
        self.number = number
        self.usable = tuple(usable)
        listed = ", ".join(u.label for u in self.usable) or "none"
        super().__init__(
            f"slot {number} is empty or has no active spool with a filament preset, usable: {listed}"
        )


def _loaded(trays: Sequence[Tray]) -> list[Tray]:
    # A tray type that is not a string (raw MQTT data) counts as not loaded.
    return sorted(
        (t for t in trays if isinstance(t.tray_type, str) and t.tray_type.strip()),
        key=lambda t: (t.ams, t.slot),
    )


def usable_slots(trays: Sequence[Tray], spools_by_slot: Mapping[tuple[int, int], Spool]) -> list[UsableSlot]:
    """Loaded slots mapped to an active spool with a filament preset, in slot order."""
    out = []
    for tray in _loaded(trays):
        spool = spools_by_slot.get((tray.ams, tray.slot))
        if spool is None or not spool.active or not (spool.filament_preset or "").strip():
            continue
        out.append(UsableSlot(slot_number(tray.ams, tray.slot), tray.ams, tray.slot, spool))
    return out


def _norm(text: str | None) -> str:
    return (text or "").strip().lstrip("#").casefold()


def _color_match(tray: Tray, spool: Spool | None, color: str) -> bool:
    wanted = _norm(color)
    if not wanted:
        return False
    names = [(tray.color_rgba or "")[:6]]
    if spool is not None:
        names += [spool.color_name, spool.color_hex]
    return any(_norm(n) == wanted for n in names if n)


def pick_slot(
    trays: Sequence[Tray],
    spools_by_slot: Mapping[tuple[int, int], Spool],
    material: str,
    last_color: str | None,
    *,
    forced: tuple[int, int] | None = None,
) -> SlotChoice:
    """A loaded slot with ``material`` (case-insensitive), the last colour first.

    No such slot: the first loaded slot and a warning naming both materials, so the
    owner sees the mismatch before slicing. Nothing loaded: no slot and a warning.
    ``last_color`` matches a spool colour name or hex, or the tray colour.

    ``forced`` (ams, slot) is the owner's choice: that slot, with a warning when its
    tray holds another material. ``SlotUnusable`` when it is not a ``usable_slots`` slot.
    """
    from bambu_mcp.messages import md_literal  # messages imports this module

    if forced is not None:
        return _forced_slot(trays, spools_by_slot, material, forced)
    loaded = _loaded(trays)
    if not loaded:
        return SlotChoice(None, None, None, f"⚠️ В AMS нет заправленных слотов, а нужен {md_literal(material)}")
    matching = [t for t in loaded if _norm(t.tray_type) == _norm(material)]
    warning = None
    if matching:
        preferred = [
            t for t in matching
            if last_color and _color_match(t, spools_by_slot.get((t.ams, t.slot)), last_color)
        ]
        tray = (preferred or matching)[0]
    else:
        tray = loaded[0]
        have = md_literal(tray.tray_type or "")
        want = md_literal(material)
        warning = (
            f"⚠️ Нужен {want}, а в AMS его нет: выбран слот {slot_number(tray.ams, tray.slot)} {have}. "
            f"Заправь {want} или подтверди печать из {have}"
        )
    spool = spools_by_slot.get((tray.ams, tray.slot))
    return SlotChoice(
        tray.ams,
        tray.slot,
        spool.id if spool else None,
        warning,
        tray_type=tray.tray_type,
        color_name=spool.color_name if spool else None,
        remaining_g=spool.remaining_g if spool else None,
    )


def _forced_slot(
    trays: Sequence[Tray],
    spools_by_slot: Mapping[tuple[int, int], Spool],
    material: str,
    forced: tuple[int, int],
) -> SlotChoice:
    from bambu_mcp.messages import md_literal  # messages imports this module

    usable = usable_slots(trays, spools_by_slot)
    chosen = next((u for u in usable if (u.ams, u.slot) == forced), None)
    if chosen is None:
        raise SlotUnusable(slot_number(*forced), usable)
    tray_type = next(t.tray_type for t in trays if (t.ams, t.slot) == forced)
    warning = None
    if _norm(tray_type) != _norm(material):
        have = md_literal(tray_type or "")
        warning = f"⚠️ Нужен {md_literal(material)}, а выбран слот {chosen.number} {have}: печать будет из {have}"
    spool = chosen.spool
    return SlotChoice(
        chosen.ams,
        chosen.slot,
        spool.id,
        warning,
        tray_type=tray_type,
        color_name=spool.color_name,
        remaining_g=spool.remaining_g,
    )


# -- cost ---------------------------------------------------------------------------


def predict_cost(
    grams: float | None,
    minutes: float | None,
    spool: Spool | None,
    material: str,
    tariff: Tariff,
    amort: Amortization | None,
    settings: Settings,
    start: datetime,
    *,
    currency: str,
) -> CostBreakdown | CurrencyMismatch:
    """Predicted cost of a plate started at ``start`` (now: nothing better is known).

    ``minutes`` None (not sliced yet): the plastic alone.
    """
    kwh = None if minutes is None else print_kwh(minutes, material, settings)
    return full_cost(grams, spool, minutes, kwh, tariff, amort, currency=currency, start=start, settings=settings)
