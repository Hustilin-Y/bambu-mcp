"""Cost of a print: filament, electricity and amortization (unrounded).

Every amount carries its ISO currency. Nothing is converted: a cost whose inputs are in
different currencies is refused with the input named.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
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


def tariff_check_due(last_reminded: date | None, today: date) -> bool:
    return last_reminded is None or (today - last_reminded).days >= TARIFF_CHECK_DAYS


# -- money: the full cost -----------------------------------------------------------


@dataclass(frozen=True)
class Money:
    amount: float  # unrounded
    currency: str  # ISO code


@dataclass(frozen=True)
class CostBreakdown:
    """A cost in one currency, with the inputs its Russian lines show (messages.render_cost)."""

    plastic: Money | None  # None: no spool (or no grams), the price is unknown
    power: Money | None  # None: the time is unknown (a proposal before the first slice)
    amort: Money | None  # None: no amortization set (or the time is unknown), the total excludes it
    total: Money
    currency: str
    grams: float | None = None
    price_per_kg: float | None = None
    kwh: float | None = None
    power_rate: float | None = None  # per kWh: the average one under a two-zone tariff
    two_zone: bool = False
    minutes: float | None = None
    amort_rate: float | None = None  # per hour of printing


@dataclass(frozen=True)
class CurrencyMismatch:
    """A cost refused because one input is priced in another currency."""

    input_name: str  # "spool 3", "tariff from 2024-06-01", "amortization from 2027-01-01"
    currency: str  # the input's currency
    expected: str  # the currency of the cost (the current one)
    kind: str = ""  # "spool", "tariff" or "amortization": for the Russian line
    ref: str = ""  # the spool id, or the entry's valid_from (ISO date)

    @property
    def reason(self) -> str:
        return f"{self.input_name} is in {self.currency}, not {self.expected}: nothing is converted"


def _spool_mismatch(spool: Spool, currency: str) -> CurrencyMismatch | None:
    if spool.currency == currency:
        return None
    return CurrencyMismatch(f"spool {spool.id}", spool.currency, currency, "spool", str(spool.id))


def _entry_mismatch(kind: str, entry: Tariff | Amortization, currency: str) -> CurrencyMismatch | None:
    if entry.currency == currency:
        return None
    ref = entry.valid_from.isoformat()
    return CurrencyMismatch(f"{kind} from {ref}", entry.currency, currency, kind, ref)


def plastic_cost(grams: float | None, spool: Spool | None, currency: str) -> Money | CurrencyMismatch | None:
    """``grams`` at the spool's price, None when either is unknown. Refused for another currency."""
    if spool is None:
        return None
    mismatch = _spool_mismatch(spool, currency)
    if mismatch is not None:
        return mismatch
    if grams is None:
        return None
    return Money(grams * spool.price_per_kg_uah / 1000, currency)


def amortization_cost(minutes: float, rate_entry: Amortization) -> Money:
    return Money(minutes / 60 * rate_entry.rate_per_hour, rate_entry.currency)


def full_cost(
    grams: float | None,
    spool: Spool | None,
    minutes: float | None,
    kwh: float | None,
    tariff: Tariff,
    amort: Amortization | None,
    *,
    currency: str,
    start: datetime | None = None,
    settings: Settings | None = None,
) -> CostBreakdown | CurrencyMismatch:
    """Plastic, electricity and amortization of ``minutes`` of printing, in ``currency``.

    A two-zone tariff needs ``start`` and ``settings`` (the night hours are local time).
    Predicted costs pass the sliced minutes, the journal the actual ones. ``minutes`` or
    ``kwh`` None (not sliced yet): plastic only, and the tariff and amortization are unused.
    """
    plastic = plastic_cost(grams, spool, currency)
    if isinstance(plastic, CurrencyMismatch):
        return plastic
    time_known = minutes is not None and kwh is not None
    if time_known:
        for kind, entry in (("tariff", tariff), ("amortization", amort)):
            mismatch = _entry_mismatch(kind, entry, currency) if entry is not None else None
            if mismatch is not None:
                return mismatch

    power = power_rate = amort_money = amort_rate = None
    if minutes is not None and kwh is not None:
        if tariff.night_uah is None:
            power = kwh * tariff.day_uah
        elif start is None or settings is None:
            raise ValueError("a two-zone tariff needs the start time and settings")
        else:
            power = power_cost(start, start + timedelta(minutes=minutes), kwh, tariff, settings)
        power_rate = tariff.day_uah if tariff.night_uah is None or kwh <= 0 else power / kwh
        if amort is not None:
            amort_money = amortization_cost(minutes, amort)
            amort_rate = amort.rate_per_hour
    total = (plastic.amount if plastic else 0.0) + (power or 0.0) + (amort_money.amount if amort_money else 0.0)
    return CostBreakdown(
        plastic=plastic,
        power=None if power is None else Money(power, currency),
        amort=amort_money,
        total=Money(total, currency),
        currency=currency,
        grams=grams if spool is not None else None,
        price_per_kg=spool.price_per_kg_uah if spool is not None else None,
        kwh=kwh if time_known else None,
        power_rate=power_rate,
        two_zone=time_known and tariff.night_uah is not None,
        minutes=minutes if time_known else None,
        amort_rate=amort_rate,
    )


def _sum_money(parts: list[Money | None], currency: str) -> Money | None:
    if any(p is None for p in parts):
        return None
    return Money(sum(p.amount for p in parts if p is not None), currency)


def _same(values: list[float | None]) -> float | None:
    """The one value all share, else None (the line then shows no rate)."""
    first = values[0] if values else None
    return first if all(v == first for v in values) else None


def sum_costs(costs: Sequence[CostBreakdown | CurrencyMismatch]) -> CostBreakdown | CurrencyMismatch:
    """The plates of a kit added up. A part a plate lacks is left out of the sum (as for one plate)."""
    if not costs:
        raise ValueError("sum_costs needs at least one cost")
    for c in costs:
        if isinstance(c, CurrencyMismatch):
            return c
    parts = [c for c in costs if isinstance(c, CostBreakdown)]
    currency = parts[0].currency
    plastic = _sum_money([c.plastic for c in parts], currency)
    power = _sum_money([c.power for c in parts], currency)
    amort = _sum_money([c.amort for c in parts], currency) if power is not None else None
    total = sum(m.amount for m in (plastic, power, amort) if m is not None)

    def total_of(values: list[float | None]) -> float | None:
        return None if any(v is None for v in values) else sum(v for v in values if v is not None)

    kwh = total_of([c.kwh for c in parts]) if power is not None else None
    power_rate = _same([c.power_rate for c in parts])
    if power is not None and kwh and (power_rate is None or any(c.two_zone for c in parts)):
        power_rate = power.amount / kwh
    return CostBreakdown(
        plastic=plastic,
        power=power,
        amort=amort,
        total=Money(total, currency),
        currency=currency,
        grams=total_of([c.grams for c in parts]) if plastic is not None else None,
        price_per_kg=_same([c.price_per_kg for c in parts]) if plastic is not None else None,
        kwh=kwh,
        power_rate=power_rate if power is not None else None,
        two_zone=power is not None and any(c.two_zone for c in parts),
        minutes=total_of([c.minutes for c in parts]) if power is not None else None,
        amort_rate=_same([c.amort_rate for c in parts]) if amort is not None else None,
    )


def tariff_reminder_enabled(country: str) -> bool:
    """The quarterly tariff reminder is for Ukrainian household tariffs only."""
    return country == "UA"
