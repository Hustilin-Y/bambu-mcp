"""Cost of a print: filament, electricity and amortization (unrounded).

Every amount carries its ISO currency. Nothing is converted: a cost whose inputs are in
different currencies is refused with the input named.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from bambu_mcp.config import Settings
from bambu_mcp.store.models import Amortization, Spool, Tariff

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
            point = point.astimezone(UTC)
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


# -- money: the full cost -----------------------------------------------------------


@dataclass(frozen=True)
class Money:
    amount: float  # unrounded
    currency: str  # ISO code


@dataclass(frozen=True)
class CostBreakdown:
    plastic: Money | None  # None: no spool (or no grams), the price is unknown
    power: Money
    amort: Money | None  # None: no amortization set, the total excludes it
    total: Money
    currency: str


@dataclass(frozen=True)
class CurrencyMismatch:
    """A cost refused because one input is priced in another currency."""

    input_name: str  # "spool 3", "tariff from 2024-06-01", "amortization from 2027-01-01"
    currency: str  # the input's currency
    expected: str  # the currency of the cost (the current one)

    @property
    def reason(self) -> str:
        return f"{self.input_name} is in {self.currency}, not {self.expected}: nothing is converted"


def amortization_cost(minutes: float, rate_entry: Amortization) -> Money:
    return Money(minutes / 60 * rate_entry.rate_per_hour, rate_entry.currency)


def full_cost(
    grams: float | None,
    spool: Spool | None,
    minutes: float,
    kwh: float,
    tariff: Tariff,
    amort: Amortization | None,
    *,
    currency: str,
    start: datetime | None = None,
    settings: Settings | None = None,
) -> CostBreakdown | CurrencyMismatch:
    """Plastic, electricity and amortization of ``minutes`` of printing, in ``currency``.

    A two-zone tariff needs ``start`` and ``settings`` (the night hours are local time).
    Predicted costs pass the sliced minutes, the journal the actual ones.
    """
    inputs: list[tuple[str, str]] = []
    if spool is not None:
        inputs.append((f"spool {spool.id}", spool.currency))
    inputs.append((f"tariff from {tariff.valid_from.isoformat()}", tariff.currency))
    if amort is not None:
        inputs.append((f"amortization from {amort.valid_from.isoformat()}", amort.currency))
    for name, input_currency in inputs:
        if input_currency != currency:
            return CurrencyMismatch(name, input_currency, currency)

    if tariff.night_uah is None:
        power = kwh * tariff.day_uah
    elif start is None or settings is None:
        raise ValueError("a two-zone tariff needs the start time and settings")
    else:
        power = power_cost(start, start + timedelta(minutes=minutes), kwh, tariff, settings)
    plastic = grams * spool.price_per_kg_uah / 1000 if spool is not None and grams is not None else None
    amort_money = amortization_cost(minutes, amort) if amort is not None else None
    total = power + (plastic or 0.0) + (amort_money.amount if amort_money else 0.0)
    return CostBreakdown(
        plastic=None if plastic is None else Money(plastic, currency),
        power=Money(power, currency),
        amort=amort_money,
        total=Money(total, currency),
        currency=currency,
    )


def tariff_reminder_enabled(country: str) -> bool:
    """The quarterly tariff reminder is for Ukrainian household tariffs only."""
    return country == "UA"
