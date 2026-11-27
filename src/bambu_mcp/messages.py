"""Russian Telegram texts and button layouts for print events."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import tzinfo
from zoneinfo import ZoneInfo

from bambu_mcp.cost import CostLine
from bambu_mcp.events import Event, EventKind
from bambu_mcp.notify import Button
from bambu_mcp.printer.hms import HmsTexts, describe, hms_code
from bambu_mcp.store.models import Spool

DEFAULT_TZ = ZoneInfo("Europe/Kyiv")  # the service passes settings.timezone
NO_NAME = "без названия"
END_UNKNOWN_TEXT = "Точное время окончания неизвестно: сервис не видел конец печати"
LOST_TITLE = "❌ Печать прервана, пока сервис не видел"
_BUTTON_TEXT_MAX = 64
_SPOOLS_PER_ROW = 2
# Characters the add-in's rich Markdown reads as formatting (multiclaude telegram.markdown_literal).
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_~=|\[\]$])")

_TITLES: dict[EventKind, str] = {
    EventKind.STARTED: "▶️ Печать началась",
    EventKind.FIRST_LAYER_DONE: "🧱 Первый слой готов",
    EventKind.PAUSED: "⏸ Печать на паузе",
    EventKind.RESUMED: "▶️ Печать продолжена",
    EventKind.FINISHED: "✅ Печать завершена",
    EventKind.FAILED: "❌ Печать прервана ошибкой",
    EventKind.CANCELLED: "🛑 Печать отменена",
}


def md_literal(text: str) -> str:
    """``text`` shown as written in the add-in's Markdown (job names, printer texts)."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def job_name(name: str | None) -> str:
    return md_literal(name) if name else NO_NAME


def fit_question(name: str | None) -> str:
    return f"Как села деталь «{job_name(name)}»?"


def spool_question(name: str | None) -> str:
    return (
        f"С какой катушки печаталась «{job_name(name)}»? "
        "Граммы спишутся, только когда ты их укажешь."
    )


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
    event: Event,
    texts: HmsTexts,
    cost: CostLine | None,
    duration_min: int | None = None,
    *,
    tz: tzinfo = DEFAULT_TZ,
) -> str:
    state = event.state
    name = job_name(state.subtask_name)
    when = event.at.astimezone(tz).strftime("%H:%M")

    if event.kind in (EventKind.HMS_ERROR, EventKind.HMS_WARNING) and event.hms is not None:
        icon = "🚨" if event.kind is EventKind.HMS_ERROR else "⚠️"
        title = "Ошибка принтера" if event.kind is EventKind.HMS_ERROR else "Предупреждение принтера"
        # describe() keeps the raw code in the text, so unknown codes stay searchable.
        detail = describe(event.hms, texts)
        if hms_code(event.hms) not in detail:  # defensive: never lose the code
            detail = f"{hms_code(event.hms)} {detail}"
        return "\n".join([f"{icon} {title}", f"{name}, {when}", md_literal(detail)])

    if event.lost:
        return "\n".join([LOST_TITLE, f"{name}, {when}"])
    lines = [_TITLES.get(event.kind, f"ℹ️ {event.kind}"), f"{name}, {when}"]
    if event.end_unknown:
        lines.append(END_UNKNOWN_TEXT)
        duration_min = cost = None  # neither is known without the end time
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
