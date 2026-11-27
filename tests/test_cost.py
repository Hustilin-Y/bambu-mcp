import logging
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from bambu_mcp import cost
from bambu_mcp.config import Settings
from bambu_mcp.cost import (
    CostBreakdown,
    CurrencyMismatch,
    Money,
    amortization_cost,
    filament_cost,
    full_cost,
    plastic_cost,
    power_cost,
    print_kwh,
    sum_costs,
    tariff_check_due,
)
from bambu_mcp.store.models import Amortization, Spool, Tariff

KYIV = ZoneInfo("Europe/Kyiv")
SINGLE = Tariff(valid_from=date(2026, 1, 1), day_uah=4.32, night_uah=None)
TWO_ZONE = Tariff(valid_from=date(2026, 1, 1), day_uah=4.32, night_uah=2.16)


@pytest.fixture(autouse=True)
def _reset_warned():
    cost._warned_materials.clear()


def _spool(price=600.0, net=1000.0, material="PETG") -> Spool:
    return Spool(
        id=1, brand="X", material=material, color_name="c", color_hex="#000000",
        filament_preset=None, price_uah=price, net_g=net, remaining_g=net, active=True,
    )


def test_pla_two_hours_single_rate():
    s = Settings()
    kwh = print_kwh(120, "PLA", s)
    assert round(kwh, 3) == 0.45
    start = datetime(2026, 6, 1, 10, 0, tzinfo=KYIV)
    end = datetime(2026, 6, 1, 12, 0, tzinfo=KYIV)
    assert round(power_cost(start, end, kwh, SINGLE, s), 3) == 1.944


def test_two_zone_split():
    s = Settings()
    start = datetime(2026, 6, 1, 22, 0, tzinfo=KYIV)
    end = datetime(2026, 6, 2, 0, 0, tzinfo=KYIV)
    assert round(power_cost(start, end, 0.45, TWO_ZONE, s), 3) == 1.512


def test_two_zone_accepts_utc_input():
    s = Settings()
    start = datetime(2026, 6, 1, 22, 0, tzinfo=KYIV).astimezone(UTC)
    end = datetime(2026, 6, 2, 0, 0, tzinfo=KYIV).astimezone(UTC)
    assert round(power_cost(start, end, 0.45, TWO_ZONE, s), 3) == 1.512


def test_two_zone_multiple_boundaries():
    # 06:00 -> 08:00 next day: night 1h, day 16h, night 8h, day 1h. Heat-up at night rate.
    s = Settings()
    start = datetime(2026, 6, 1, 6, 0, tzinfo=KYIV)
    end = datetime(2026, 6, 2, 8, 0, tzinfo=KYIV)
    kwh = 0.05 + 2.6  # 0.1 kWh per hour
    expected = 0.05 * 2.16 + 1.7 * 4.32 + 0.9 * 2.16
    assert round(power_cost(start, end, kwh, TWO_ZONE, s), 3) == round(expected, 3)


def test_dst_fall_back_night_is_nine_real_hours():
    s = Settings()
    start = datetime(2026, 10, 24, 23, 0, tzinfo=KYIV)
    end = datetime(2026, 10, 25, 7, 0, tzinfo=KYIV)  # 9 real hours (clock goes back)
    assert (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() == 9 * 3600
    assert round(power_cost(start, end, 1.05, TWO_ZONE, s), 3) == round(1.05 * 2.16, 3)


def test_dst_spring_forward_day():
    s = Settings()
    start = datetime(2026, 3, 28, 22, 0, tzinfo=KYIV)
    end = datetime(2026, 3, 29, 7, 0, tzinfo=KYIV)  # 8 real hours: 1 day + 7 night
    kwh = 0.05 + 0.8
    expected = 0.05 * 4.32 + 0.1 * 4.32 + 0.7 * 2.16
    assert round(power_cost(start, end, kwh, TWO_ZONE, s), 3) == round(expected, 3)


def test_naive_datetime_refused():
    s = Settings()
    naive = datetime(2026, 6, 1, 10, 0)
    aware = datetime(2026, 6, 1, 12, 0, tzinfo=KYIV)
    with pytest.raises(ValueError):
        power_cost(naive, aware, 0.45, SINGLE, s)
    with pytest.raises(ValueError):
        power_cost(aware, naive, 0.45, TWO_ZONE, s)


def test_end_before_start_refused():
    s = Settings()
    a = datetime(2026, 6, 1, 12, 0, tzinfo=KYIV)
    b = datetime(2026, 6, 1, 10, 0, tzinfo=KYIV)
    with pytest.raises(ValueError):
        power_cost(a, b, 0.45, TWO_ZONE, s)


def test_zero_duration_all_at_start_rate():
    s = Settings()
    t = datetime(2026, 6, 1, 23, 30, tzinfo=KYIV)
    assert round(power_cost(t, t, 0.5, TWO_ZONE, s), 3) == round(0.5 * 2.16, 3)


def test_unknown_material_falls_back_to_pla_and_warns_once(caplog):
    s = Settings()
    with caplog.at_level(logging.WARNING, logger="bambu_mcp.cost"):
        a = print_kwh(120, "PETG", s)
        b = print_kwh(60, "PETG", s)
        print_kwh(60, "ABS", s)
    assert round(a, 3) == 0.45
    assert round(b, 3) == 0.25
    warned = [r.getMessage() for r in caplog.records]
    assert len(warned) == 2
    assert "PETG" in warned[0] and "ABS" in warned[1]


def test_known_non_pla_material_uses_own_power():
    s = Settings(power_w={"PLA": 200.0, "PETG": 250.0})
    assert round(print_kwh(60, "PETG", s), 3) == round(0.25 + 0.05, 3)


def test_filament_cost_from_spool():
    assert round(filament_cost(14.2, _spool(), {}, "PETG"), 3) == 8.52


def test_filament_cost_fallback_price():
    assert round(filament_cost(10, None, {"PETG": 500.0}, "PETG"), 3) == 5.0


def test_filament_cost_unknown_price():
    assert filament_cost(14.2, None, {}, "PETG") is None


def test_tariff_check_due():
    today = date(2026, 11, 22)
    assert tariff_check_due(None, today) is True
    assert tariff_check_due(date(2026, 8, 24), today) is False  # 90 days
    assert tariff_check_due(date(2026, 8, 23), today) is True  # 91 days


_EQUAL = Tariff(valid_from=date(2026, 1, 1), day_uah=1.0, night_uah=1.0)
_WINDOWS = [
    ((2026, 6, 1, 10, 0), (2026, 6, 1, 12, 0)),
    ((2026, 6, 1, 6, 0), (2026, 6, 2, 8, 0)),
    ((2026, 3, 28, 22, 0), (2026, 3, 29, 12, 0)),  # spring forward, 23:00 and 07:00 inside
    ((2026, 10, 24, 22, 0), (2026, 10, 25, 12, 0)),  # fall back, 23:00 and 07:00 inside
    ((2026, 3, 28, 6, 0), (2026, 3, 30, 8, 0)),
    ((2026, 10, 24, 6, 0), (2026, 10, 26, 8, 0)),
]


@pytest.mark.parametrize(("a", "b"), _WINDOWS)
def test_equal_rates_cost_is_kwh_times_rate(a, b):
    s = Settings()
    start = datetime(*a, tzinfo=KYIV)
    end = datetime(*b, tzinfo=KYIV)
    assert power_cost(start, end, 10.0, _EQUAL, s) == pytest.approx(10.0, abs=1e-9)
    assert power_cost(start, end, 10.0, _EQUAL, Settings(heatup_kwh=0.0)) == pytest.approx(
        10.0, abs=1e-9
    )


def test_dst_fall_back_two_boundaries_split():
    # 22:00 -> 12:00 on the fall-back night: 15 real hours.
    # day 22-23 (1h), night 23-07 (9h), day 07-12 (5h).
    s = Settings(heatup_kwh=0.0)
    start = datetime(2026, 10, 24, 22, 0, tzinfo=KYIV)
    end = datetime(2026, 10, 25, 12, 0, tzinfo=KYIV)
    expected = 15.0 * (1 * 4.32 + 9 * 2.16 + 5 * 4.32) / 15
    assert power_cost(start, end, 15.0, TWO_ZONE, s) == pytest.approx(expected)


# -- money: full cost, currencies, amortization ------------------------------------

AMORT = Amortization(
    valid_from=date(2026, 1, 1), printer_price=30000.0, life_hours=5000.0, upkeep_per_hour=2.0, currency="UAH"
)


def _uah_spool(currency="UAH") -> Spool:
    return Spool(
        id=3, brand="Bambu", material="PLA", color_name="Black", color_hex="#000000",
        filament_preset=None, price_uah=700.0, net_g=1000.0, remaining_g=1000.0, active=True,
        currency=currency,
    )


def test_amortization_cost_is_minutes_times_rate():
    assert AMORT.rate_per_hour == 8.0
    m = amortization_cost(52, AMORT)
    assert m.currency == "UAH" and m.amount == pytest.approx(52 / 60 * 8)


def test_full_cost_breakdown():
    b = full_cost(8, _uah_spool(), 52, 0.4444, SINGLE, AMORT, currency="UAH")
    assert isinstance(b, CostBreakdown)
    assert b.currency == "UAH"
    assert b.plastic == Money(pytest.approx(5.6), "UAH")
    assert round(b.power.amount, 2) == 1.92
    assert round(b.amort.amount, 2) == 6.93
    assert b.total.currency == "UAH"
    assert b.total.amount == pytest.approx(b.plastic.amount + b.power.amount + b.amort.amount)
    assert round(b.total.amount, 2) == 14.45


def test_full_cost_two_zone_uses_the_start_time():
    s = Settings()
    start = datetime(2026, 6, 1, 22, 0, tzinfo=KYIV)
    b = full_cost(8, _uah_spool(), 120, 0.45, TWO_ZONE, None, currency="UAH", start=start, settings=s)
    end = datetime(2026, 6, 2, 0, 0, tzinfo=KYIV)
    assert b.power.amount == pytest.approx(power_cost(start, end, 0.45, TWO_ZONE, s))
    with pytest.raises(ValueError, match="start"):
        full_cost(8, _uah_spool(), 120, 0.45, TWO_ZONE, None, currency="UAH")


def test_full_cost_without_a_spool_has_no_plastic():
    b = full_cost(8, None, 52, 0.4444, SINGLE, None, currency="UAH")
    assert b.plastic is None
    assert b.total.amount == pytest.approx(b.power.amount)


def test_no_amortization_line_when_unset():
    b = full_cost(8, _uah_spool(), 52, 0.4444, SINGLE, None, currency="UAH")
    assert b.amort is None
    assert b.total.amount == pytest.approx(5.6 + 0.4444 * 4.32)


def test_mixed_currency_refused_names_input():
    eur_tariff = Tariff(valid_from=date(2026, 1, 1), day_uah=0.25, night_uah=None, currency="EUR")
    eur_amort = Amortization(date(2026, 1, 1), 600.0, 3000.0, 0.0, "EUR")

    spool_off = full_cost(8, _uah_spool(), 52, 0.44, eur_tariff, eur_amort, currency="EUR")
    assert isinstance(spool_off, CurrencyMismatch)
    assert spool_off.input_name == "spool 3"
    assert (spool_off.currency, spool_off.expected) == ("UAH", "EUR")
    assert "spool 3" in spool_off.reason and "UAH" in spool_off.reason and "EUR" in spool_off.reason

    tariff_off = full_cost(8, _uah_spool("EUR"), 52, 0.44, SINGLE, eur_amort, currency="EUR")
    assert isinstance(tariff_off, CurrencyMismatch)
    assert tariff_off.input_name == "tariff from 2026-01-01"

    amort_off = full_cost(8, _uah_spool("EUR"), 52, 0.44, eur_tariff, AMORT, currency="EUR")
    assert isinstance(amort_off, CurrencyMismatch)
    assert amort_off.input_name == "amortization from 2026-01-01"

    ok = full_cost(8, _uah_spool("EUR"), 52, 0.44, eur_tariff, eur_amort, currency="EUR")
    assert isinstance(ok, CostBreakdown) and ok.currency == "EUR"


def test_full_cost_keeps_the_inputs_of_its_lines():
    b = full_cost(8, _uah_spool(), 52, 0.4444, SINGLE, AMORT, currency="UAH")
    assert (b.grams, b.price_per_kg, b.kwh, b.minutes) == (8, 700.0, 0.4444, 52)
    assert (b.power_rate, b.amort_rate, b.two_zone) == (4.32, 8.0, False)


def test_full_cost_two_zone_rate_is_the_average():
    start = datetime(2026, 6, 1, 22, 0, tzinfo=KYIV)
    b = full_cost(8, None, 120, 0.45, TWO_ZONE, None, currency="UAH", start=start, settings=Settings())
    assert b.two_zone is True
    assert b.power_rate == pytest.approx(b.power.amount / 0.45)
    assert 2.16 < b.power_rate < 4.32


def test_full_cost_with_the_time_unknown_has_plastic_only():
    b = full_cost(8, _uah_spool(), None, None, TWO_ZONE, AMORT, currency="UAH")
    assert b.power is None and b.amort is None and b.minutes is None
    assert b.total == Money(pytest.approx(5.6), "UAH")


def test_mismatch_names_kind_and_ref():
    eur_tariff = Tariff(valid_from=date(2026, 1, 1), day_uah=0.25, night_uah=None, currency="EUR")
    off = full_cost(8, _uah_spool(), 52, 0.44, eur_tariff, None, currency="EUR")
    assert (off.kind, off.ref) == ("spool", "3")
    off = full_cost(8, None, 52, 0.44, SINGLE, None, currency="EUR")
    assert (off.kind, off.ref) == ("tariff", "2026-01-01")
    off = full_cost(8, None, 52, 0.44, eur_tariff, AMORT, currency="EUR")
    assert (off.kind, off.ref) == ("amortization", "2026-01-01")


def test_plastic_cost_checks_the_spool_currency():
    assert plastic_cost(8, _uah_spool(), "UAH") == Money(pytest.approx(5.6), "UAH")
    assert plastic_cost(None, _uah_spool(), "UAH") is None
    assert plastic_cost(8, None, "UAH") is None
    off = plastic_cost(8, _uah_spool(), "EUR")
    assert isinstance(off, CurrencyMismatch) and off.input_name == "spool 3"


def test_sum_costs_adds_the_plates():
    a = full_cost(3, _uah_spool(), 20, 0.1, SINGLE, AMORT, currency="UAH")
    b = full_cost(18, _uah_spool(), 90, 0.35, SINGLE, AMORT, currency="UAH")
    total = sum_costs([a, b])
    assert total.plastic.amount == pytest.approx(a.plastic.amount + b.plastic.amount)
    assert total.power.amount == pytest.approx(a.power.amount + b.power.amount)
    assert total.amort.amount == pytest.approx(a.amort.amount + b.amort.amount)
    assert total.total.amount == pytest.approx(a.total.amount + b.total.amount)
    assert (total.grams, total.price_per_kg, total.minutes, total.amort_rate) == (21, 700.0, 110, 8.0)
    assert total.kwh == pytest.approx(0.45) and total.power_rate == pytest.approx(4.32)
    no_spool = full_cost(3, None, 20, 0.1, SINGLE, None, currency="UAH")
    mixed = sum_costs([a, no_spool])
    assert mixed.plastic is None and mixed.amort is None
    assert mixed.total.amount == pytest.approx(a.power.amount + no_spool.power.amount)
    off = CurrencyMismatch("spool 3", "UAH", "EUR")
    assert sum_costs([a, off]) is off
