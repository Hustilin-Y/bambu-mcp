"""Electricity and filament cost of a print, in UAH (unrounded)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from bambu_mcp.config import Settings
from bambu_mcp.store.models import Spool, Tariff

log = logging.getLogger(__name__)

NIGHT_START = time(23, 0)
NIGHT_END = time(7, 0)
TARIFF_CHECK_DAYS = 91

_warned_materials: set[str] = set()


def print_kwh(minutes: float, material: str, settings: Settings) -> float:
    power_w = settings.power_w.get(material)
    if power_w is None:
        if material not in _warned_materials:
            _warned_materials.add(material)
            log.warning("no power_w entry for material %r, using the PLA value", material)
        power_w = settings.power_w["PLA"]
    return minutes / 60 * power_w / 1000 + settings.heatup_kwh


def _is_night(local: datetime) -> bool:
    t = local.timetz().replace(tzinfo=None)
    return t >= NIGHT_START or t < NIGHT_END


def _boundaries(start: datetime, end: datetime, tz: ZoneInfo) -> list[datetime]:
    """Local 07:00 and 23:00 instants strictly inside (start, end)."""
    first = start.astimezone(tz).date() - timedelta(days=1)
    last = end.astimezone(tz).date() + timedelta(days=1)
    points: list[datetime] = []
    day = first
    while day <= last:
        for t in (NIGHT_END, NIGHT_START):
            point = datetime.combine(day, t, tzinfo=tz)
            if start < point < end:
                points.append(point)
        day += timedelta(days=1)
    return sorted(points)


def power_cost(
    start: datetime, end: datetime, kwh: float, tariff: Tariff, settings: Settings
) -> float:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware")

    tz = ZoneInfo(settings.timezone)
    # Same-tzinfo subtraction ignores UTC offsets, so do all arithmetic in UTC.
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if end < start:
        raise ValueError("end is before start")
    if tariff.night_uah is None:
        return kwh * tariff.day_uah
    day_rate, night_rate = tariff.day_uah, tariff.night_uah
    start_rate = night_rate if _is_night(start.astimezone(tz)) else day_rate

    heatup = min(settings.heatup_kwh, kwh)
    rest = kwh - heatup
    total_s = (end - start).total_seconds()
    if total_s == 0:
        return kwh * start_rate

    cost = heatup * start_rate
    edges = [start, *_boundaries(start, end, tz), end]
    for a, b in zip(edges, edges[1:]):
        seconds = (b - a).total_seconds()
        rate = night_rate if _is_night((a + (b - a) / 2).astimezone(tz)) else day_rate
        cost += rest * seconds / total_s * rate
    return cost


def filament_cost(
    grams: float,
    spool: Spool | None,
    fallback_per_kg: dict[str, float],
    material: str,
) -> float | None:
    if spool is not None:
        return grams * spool.price_per_kg_uah / 1000
    per_kg = fallback_per_kg.get(material)
    if per_kg is None:
        return None
    return grams * per_kg / 1000


@dataclass(frozen=True)
class CostLine:
    filament_uah: float | None
    power_uah: float
    kwh: float
    tariff: Tariff

    @property
    def total_uah(self) -> float:
        return self.power_uah + (self.filament_uah or 0.0)


def tariff_check_due(last_reminded: date | None, today: date) -> bool:
    return last_reminded is None or (today - last_reminded).days >= TARIFF_CHECK_DAYS
