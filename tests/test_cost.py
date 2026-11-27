import logging
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from bambu_mcp import cost
from bambu_mcp.config import Settings
from bambu_mcp.cost import CostLine, filament_cost, power_cost, print_kwh, tariff_check_due
from bambu_mcp.store.models import Spool, Tariff

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


def test_cost_line_total():
    assert CostLine(1.5, 2.0, 0.4, TWO_ZONE).total_uah == 3.5
    assert CostLine(None, 2.0, 0.4, TWO_ZONE).total_uah == 2.0


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
