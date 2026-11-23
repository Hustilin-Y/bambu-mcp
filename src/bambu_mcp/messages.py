"""Russian Telegram texts and button layouts for print events."""

from __future__ import annotations

from collections.abc import Sequence
from zoneinfo import ZoneInfo

from bambu_mcp.cost import CostLine
from bambu_mcp.events import Event, EventKind
from bambu_mcp.notify import Button
from bambu_mcp.printer.hms import HmsTexts, describe, hms_code
from bambu_mcp.store.models import Spool

TIMEZONE = ZoneInfo("Europe/Kyiv")
_BUTTON_TEXT_MAX = 64
_SPOOLS_PER_ROW = 2

_TITLES: dict[EventKind, str] = {
    EventKind.STARTED: "▶️ Печать началась",
    EventKind.FIRST_LAYER_DONE: "🧱 Первый слой готов",
    EventKind.PAUSED: "⏸ Печать на паузе",
    EventKind.RESUMED: "▶️ Печать продолжена",
    EventKind.FINISHED: "✅ Печать завершена",
    EventKind.FAILED: "❌ Печать прервана ошибкой",
    EventKind.CANCELLED: "🛑 Печать отменена",
}


def _money(value: float) -> str:
    return f"{value:.2f} грн"


def _cost_lines(cost: CostLine) -> list[str]:
    if cost.filament_uah is None:
        costs = f"Электричество {_money(cost.power_uah)}"
    else:
        costs = (
            f"Пластик {_money(cost.filament_uah)}, электричество {_money(cost.power_uah)}, "
            f"всего {_money(cost.total_uah)}"
        )
    t = cost.tariff
    if t.night_uah is None:
        rate = f"{t.day_uah:.2f} грн/кВт·ч"
    else:
        rate = f"день {t.day_uah:.2f} / ночь {t.night_uah:.2f} грн/кВт·ч"
    return [costs, f"({cost.kwh:.2f} кВт·ч, тариф {rate} с {t.valid_from:%d.%m.%Y})"]


def _duration(minutes: int) -> str:
    hours, mins = divmod(max(minutes, 0), 60)
    return f"{hours} ч {mins:02d} мин" if hours else f"{mins} мин"


def render_event(
    event: Event, texts: HmsTexts, cost: CostLine | None, duration_min: int | None = None
) -> str:
    state = event.state
    name = state.subtask_name or "без названия"
    when = event.at.astimezone(TIMEZONE).strftime("%H:%M")

    if event.kind in (EventKind.HMS_ERROR, EventKind.HMS_WARNING) and event.hms is not None:
        icon = "🚨" if event.kind is EventKind.HMS_ERROR else "⚠️"
        title = "Ошибка принтера" if event.kind is EventKind.HMS_ERROR else "Предупреждение принтера"
        # describe() keeps the raw code in the text, so unknown codes stay searchable.
        detail = describe(event.hms, texts)
        if hms_code(event.hms) not in detail:  # defensive: never lose the code
            detail = f"{hms_code(event.hms)} {detail}"
        return "\n".join([f"{icon} {title}", f"{name}, {when}", detail])

    lines = [_TITLES.get(event.kind, f"ℹ️ {event.kind}"), f"{name}, {when}"]
    if event.kind in (EventKind.PAUSED, EventKind.RESUMED, EventKind.FIRST_LAYER_DONE):
        if state.layer is not None and state.total_layers:
            lines.append(f"Слой {state.layer} из {state.total_layers}")
    if duration_min is not None and event.kind in (
        EventKind.FINISHED,
        EventKind.FAILED,
        EventKind.CANCELLED,
    ):
        lines.append(f"Длительность: {_duration(duration_min)}")
    if event.kind is EventKind.FINISHED and cost is not None:
        lines.extend(_cost_lines(cost))
    return "\n".join(lines)


def post_print_buttons(print_id: int) -> list[list[Button]]:
    return [
        [
            Button(f"fit:{print_id}:ok", "✅ Села отлично"),
            Button(f"fit:{print_id}:tight", "🔒 Туго"),
        ],
        [
            Button(f"fit:{print_id}:loose", "🔓 Люфт"),
            Button(f"fit:{print_id}:failed", "❌ Не подошла"),
        ],
    ]


def _spool_label(spool: Spool) -> str:
    return f"{spool.brand} {spool.material} {spool.color_name}"[:_BUTTON_TEXT_MAX]


def spool_buttons(print_id: int, spools: Sequence[Spool]) -> list[list[Button]]:
    buttons = [Button(f"spool:{print_id}:{s.id}", _spool_label(s)) for s in spools]
    rows = [buttons[i : i + _SPOOLS_PER_ROW] for i in range(0, len(buttons), _SPOOLS_PER_ROW)]
    rows.append([Button(f"spool:{print_id}:none", "Другая / не знаю")])
    return rows
