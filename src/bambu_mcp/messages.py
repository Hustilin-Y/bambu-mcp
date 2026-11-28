"""Russian Telegram texts and button layouts for print events."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, tzinfo
from zoneinfo import ZoneInfo

from bambu_mcp.cost import CostBreakdown, CurrencyMismatch, sum_costs
from bambu_mcp.evaluation import GOALS, broken_rules
from bambu_mcp.events import Event, EventKind
from bambu_mcp.manifest import Manifest, PlateInfo, parse_manifest
from bambu_mcp.notify import Button
from bambu_mcp.printer.hms import HmsTexts, describe, hms_code
from bambu_mcp.proposal import (
    EXTERNAL_LABEL,
    EXTERNAL_SLOT_NUMBER,
    MAX_SLOT_NUMBER,
    SlotChoice,
    UsableSlot,
    hint_overrides,
    job_material,
    slot_number,
)
from bambu_mcp.store.db import DEFAULT_CURRENCY
from bambu_mcp.store.models import Job, Spool, Tariff

DEFAULT_TZ = ZoneInfo("Europe/Kyiv")  # the service passes settings.timezone
DEFAULT_SYMBOL = DEFAULT_CURRENCY[1]  # the service passes the store's currency symbol
NO_NAME = "без названия"
END_UNKNOWN_TEXT = "Точное время окончания неизвестно: сервис не видел конец печати"
LOST_TITLE = "❌ Печать прервана, пока сервис не видел"
_BUTTON_TEXT_MAX = 64
_SPOOLS_PER_ROW = 2
_SLOTS_PER_ROW = 4
_GOALS_PER_ROW = 4
MAX_BUTTONS = 24  # per Telegram message (global constraints)
# The owner's emoji markup, one table: marks, line prefixes, goal buttons and the keycap
# digits that number the variants and the AMS slots (above 10: plain digits).
EMOJI: dict[str, str] = {
    "chosen": "✅",
    "failed": "❌",
    "printer": "🖨",
    "goal": "🎯",
    "explicit": "✍️",
    "variants": "📊",
    "time": "⏱",
    "weight": "⚖️",
    "money": "💰",
    "strength": "💪",
    "plastic": "🧵",
    "power": "⚡",
    "amort": "🛠",
    "evaluate": "🔍",
    "reset": "♻️",
    # goal buttons
    "balanced": "⚖️",
    "speed": "⚡",
    "quality": "✨",
    "cheap": "💸",
    "light": "🪶",
    "fit": "🎯",
    "watertight": "💧",
    # keycap digits
    "1": "1️⃣",
    "2": "2️⃣",
    "3": "3️⃣",
    "4": "4️⃣",
    "5": "5️⃣",
    "6": "6️⃣",
    "7": "7️⃣",
    "8": "8️⃣",
    "9": "9️⃣",
    "10": "🔟",
}
_GOAL_EMOJI = {"strength": EMOJI["strength"]}  # "strength" is also the table's strength mark
EVAL_BUTTON_TEXT = f"{EMOJI['evaluate']} Подобрать"
RESET_BUTTON_TEXT = f"{EMOJI['reset']} Сбросить мои настройки"
CHOSEN_MARK = f"{EMOJI['chosen']} "


def keycap(n: int) -> str:
    """1️⃣ .. 🔟 for 1..10, the plain number above."""
    return EMOJI.get(str(n), str(n)) if 1 <= n <= 10 else str(n)


def goal_emoji(key: str) -> str:
    return _GOAL_EMOJI.get(key) or EMOJI[key]
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


# -- money: the one cost formatter ------------------------------------------------

_MISMATCH_WORDS = {"spool": "катушка", "tariff": "тариф", "amortization": "амортизация"}


def money_text(amount: float, symbol: str) -> str:
    """``amount`` with two decimals and the escaped currency symbol: '14.45 €'."""
    return f"{amount:.2f} {md_literal(symbol)}"


def _number(value: float) -> str:
    """At most two decimals, no trailing zeros: 700, 733.33."""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _mismatch_input(m: CurrencyMismatch) -> str:
    word = _MISMATCH_WORDS.get(m.kind)
    if word is None:
        return md_literal(m.input_name)
    if m.kind == "spool":
        return f"{word} {md_literal(m.ref)}"
    try:
        when = date.fromisoformat(m.ref).strftime("%d.%m.%Y")
    except ValueError:
        when = md_literal(m.ref)
    return f"{word} с {when}"


def render_cost_refused(m: CurrencyMismatch) -> str:
    return (
        f"Стоимость не посчитана: {_mismatch_input(m)} в {md_literal(m.currency)}, "
        f"а валюта сейчас {md_literal(m.expected)}. Пересчёта нет, задай это заново в {md_literal(m.expected)}"
    )


def render_cost_refused_short(m: CurrencyMismatch) -> str:
    """A refusal at a print's close: 'Стоимость не посчитана: тариф с 01.06.2024 в UAH, а печать в EUR'."""
    return (
        f"Стоимость не посчитана: {_mismatch_input(m)} в {md_literal(m.currency)}, "
        f"а печать в {md_literal(m.expected)}"
    )


def currency_label(currency: str | None, symbol: str, code: str | None) -> str:
    """The symbol to show for an amount in ``currency``: the current ``symbol``, or the amount's
    own ISO code when it is not the current currency ``code`` (nothing is converted)."""
    if code is not None and currency and currency != code:
        return currency
    return symbol


def render_cost(
    cost: CostBreakdown | CurrencyMismatch, symbol: str, *, start_now: bool = False, code: str | None = None
) -> list[str]:
    """The Russian cost lines: plastic, electricity, amortization, total. Markdown-escaped.

    ``start_now``: a predicted cost priced as if the print started now, which the
    electricity line says under a two-zone tariff. ``code``: the current currency. A cost
    recorded in another one shows its own ISO code, never the current ``symbol``.
    """
    if isinstance(cost, CurrencyMismatch):
        return [render_cost_refused(cost)]
    symbol = currency_label(cost.currency, symbol, code)
    sym = md_literal(symbol)
    lines = []
    if cost.plastic is None:
        lines.append(f"{EMOJI['plastic']} пластик: цена неизвестна")
    elif cost.grams is not None and cost.price_per_kg is not None:
        lines.append(
            f"{EMOJI['plastic']} пластик {money_text(cost.plastic.amount, symbol)} "
            f"({_grams(cost.grams)} × {_number(cost.price_per_kg)} {sym}/кг)"
        )
    else:
        lines.append(f"{EMOJI['plastic']} пластик {money_text(cost.plastic.amount, symbol)}")
    if cost.power is None:
        lines.append(f"{EMOJI['time']} время неизвестно до нарезки")
    else:
        power = f"{EMOJI['power']} свет {money_text(cost.power.amount, symbol)}"
        if cost.kwh is not None and cost.power_rate is not None:
            rate = f"{cost.power_rate:.2f}" + (" в среднем" if cost.two_zone else "")
            when = ", если начать сейчас" if cost.two_zone and start_now else ""
            power += f" ({cost.kwh:.2f} кВт·ч × {rate}{when})"
        lines.append(power)
        if cost.amort is None:
            lines.append(f"{EMOJI['amort']} амортизация: не задана")
        elif cost.minutes is not None and cost.amort_rate is not None:
            lines.append(
                f"{EMOJI['amort']} амортизация {money_text(cost.amort.amount, symbol)} "
                f"({round(cost.minutes)} мин × {cost.amort_rate:.2f} {sym}/ч)"
            )
        else:
            lines.append(f"{EMOJI['amort']} амортизация {money_text(cost.amort.amount, symbol)}")
    lines.append(f"{EMOJI['money']} итого {money_text(cost.total.amount, symbol)}")
    return lines


def tariff_reminder(tariff: Tariff, symbol: str) -> str:
    sym = md_literal(symbol)
    if tariff.night_uah is None:
        rate = f"{tariff.day_uah:.2f} {sym}/кВт·ч"
    else:
        rate = f"день {tariff.day_uah:.2f} / ночь {tariff.night_uah:.2f} {sym}/кВт·ч"
    return f"Проверь тариф на электроэнергию: сейчас {rate} с {tariff.valid_from:%d.%m.%Y}"


def _duration(minutes: int) -> str:
    hours, mins = divmod(max(minutes, 0), 60)
    return f"{hours} ч {mins:02d} мин" if hours else f"{mins} мин"


def render_event(
    event: Event,
    texts: HmsTexts,
    cost: CostBreakdown | CurrencyMismatch | None,
    duration_min: int | None = None,
    *,
    tz: tzinfo = DEFAULT_TZ,
    symbol: str = DEFAULT_SYMBOL,
    code: str | None = None,
) -> str:
    """``cost``: the closed print's, a ``CurrencyMismatch`` when it was refused at the close.
    ``code``: the current currency (a cost recorded in another shows its ISO code)."""
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
    if event.kind is EventKind.FINISHED and isinstance(cost, CurrencyMismatch):
        lines.append(render_cost_refused_short(cost))
    elif event.kind is EventKind.FINISHED and cost is not None:
        lines.extend(render_cost(cost, symbol, code=code))
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


def slot_buttons(
    job_id: int, usable: Sequence[UsableSlot], chosen: int | None, limit: int = MAX_SLOT_NUMBER
) -> list[list[Button]]:
    """The proposal's slot rows: ``job:<id>:slot:<n>``, the ``chosen`` number marked.

    At most ``limit`` slots, in slot order. The chosen one is always among them.
    """
    shown = list(usable[: max(limit, 0)])
    if limit > 0 and chosen is not None and all(u.number != chosen for u in shown):
        extra = [u for u in usable if u.number == chosen]
        if extra:
            shown = sorted([*shown[:-1], extra[0]], key=lambda u: u.number)
    buttons = [
        Button(
            f"job:{job_id}:slot:{u.number}",
            f"{CHOSEN_MARK if u.number == chosen else ''}"
            f"{keycap(u.number) if u.number != EXTERNAL_SLOT_NUMBER else 'внешняя'} "
            f"{u.spool.material} {u.spool.color_name}"[:_BUTTON_TEXT_MAX],
        )
        for u in shown
    ]
    return [buttons[i : i + _SLOTS_PER_ROW] for i in range(0, len(buttons), _SLOTS_PER_ROW)]


def goal_buttons(job_id: int, current: str) -> list[list[Button]]:
    """The proposal's goal rows: ``job:<id>:goal:<key>``, two rows of 4, the ``current`` one marked."""
    buttons = [
        Button(
            f"job:{job_id}:goal:{g.key}",
            f"{CHOSEN_MARK if g.key == current else ''}{goal_emoji(g.key)} {g.label}",
        )
        for g in GOALS.values()
    ]
    return [buttons[i : i + _GOALS_PER_ROW] for i in range(0, len(buttons), _GOALS_PER_ROW)]


def reset_buttons(job_id: int) -> list[list[Button]]:
    """«♻️ Сбросить мои настройки» (``job:<id>:reset``): clears the owner's explicit settings."""
    return [[Button(f"job:{job_id}:reset", RESET_BUTTON_TEXT)]]


def explicit_line(explicit: Mapping) -> str | None:
    """'твои настройки: 4 стенки, 25% gyroid': what «Сбросить мои настройки» clears. None: none."""
    if not explicit:
        return None
    parts = _setting_parts(explicit)
    covered = {
        "wall_loops", "sparse_infill_density", "sparse_infill_pattern", "enable_support", "brim_type",
        "layer_height", "wall_generator", "top_shell_layers", "bottom_shell_layers", "outer_wall_speed",
        "seam_position", "ironing_type",
    }
    if "enable_support" in explicit and explicit.get("enable_support"):
        covered.add("support_type")
    parts += [md_literal(f"{k} {v}") for k, v in explicit.items() if k not in covered]
    return f"{EMOJI['explicit']} твои настройки: {', '.join(parts)}"


def eval_buttons(job_id: int) -> list[list[Button]]:
    """«🔍 Подобрать»: a part's evaluation run (``job:<id>:eval``). A kit has none."""
    return [[Button(f"job:{job_id}:eval", EVAL_BUTTON_TEXT)]]


_VARIANTS_PER_ROW = 3
EVALUATING_TEXT = "🔍 Подбираю варианты…"
VARIANTS_TITLE = f"{EMOJI['variants']} Варианты:"
# Settings a table line names only when they differ from the run's baseline.
_SHORT_EXTRA_KEYS = (
    "layer_height", "wall_generator", "top_shell_layers", "bottom_shell_layers", "outer_wall_speed",
    "seam_position", "ironing_type",
)


def variant_buttons(job_id: int, variants: Sequence[Mapping], chosen: int | None) -> list[list[Button]]:
    """«1️⃣», «✅ 2️⃣», … for each variant that sliced (``job:<id>:variant:<n>``), the chosen one marked."""
    buttons = [
        Button(f"job:{job_id}:variant:{v['n']}", f"{CHOSEN_MARK if v['n'] == chosen else ''}{keycap(v['n'])}")
        for v in variants
        if v.get("status") == "ok"
    ]
    return [buttons[i : i + _VARIANTS_PER_ROW] for i in range(0, len(buttons), _VARIANTS_PER_ROW)]


def _short_settings(settings: Mapping, baseline: Mapping) -> str:
    """'3 стенки 25% gyroid, верх/низ 7/5': walls and infill, then the settings this variant
    changes against the baseline."""
    core = ["wall_loops", "sparse_infill_density", "sparse_infill_pattern"]
    extra = [k for k in _SHORT_EXTRA_KEYS if settings.get(k) != baseline.get(k) and k in settings]
    if "top_shell_layers" in extra or "bottom_shell_layers" in extra:
        extra += [k for k in ("top_shell_layers", "bottom_shell_layers") if k in settings and k not in extra]
    head = " ".join(_setting_parts({k: settings[k] for k in core if k in settings}))
    tail = _setting_parts({k: settings[k] for k in extra if k in settings})
    return ", ".join([head, *tail]) if head else ", ".join(tail)


ONLY_BASELINE_TEXT = "Нарезался только базовый вариант, остальные не нарезались"
STRENGTH_UNKNOWN_TEXT = "Прочность неизвестна: агент на ПК её не измерил, выбран базовый вариант цели"


def render_variants(
    variants: Sequence[Mapping],
    chosen: int | None,
    symbol: str,
    *,
    code: str | None = None,
    strength_unknown: bool = False,
) -> list[str]:
    """The evaluation table, at most 6 lines after its title:
    '2️⃣ ✅ 3 стенки 25% gyroid, верх/низ 7/5 · ⏱ 49 мин · ⚖️ 8 г · 💰 12.80 <symbol> · 💪 1.2×'.

    Strength is against the first (the balanced baseline). A variant priced in another
    currency than ``code`` (the current one) shows its ISO code. Then a sentence when only
    the baseline sliced, and one when ``strength_unknown`` kept the goal's base point.
    """
    if not variants:
        return []
    baseline = variants[0].get("settings") or {}
    lines = [VARIANTS_TITLE]
    for v in variants[:6]:
        n, short = v["n"], _short_settings(v.get("settings") or {}, baseline)
        if v.get("status") != "ok":
            lines.append(f"{keycap(n)} {EMOJI['failed']} {short} · не нарезан")
            continue
        mark = CHOSEN_MARK if n == chosen else ""
        cost = v.get("cost")
        label = currency_label(v.get("currency") if isinstance(v.get("currency"), str) else None, symbol, code)
        money = money_text(cost, label) if isinstance(cost, int | float) else f"? {md_literal(label)}"
        ratio = v.get("strength_ratio")
        strength = f"{ratio:.1f}×" if isinstance(ratio, int | float) else "?"
        lines.append(
            f"{keycap(n)} {mark}{short} · {EMOJI['time']} {round(v['minutes'])} мин · "
            f"{EMOJI['weight']} {v['grams']:.0f} г · {EMOJI['money']} {money} · {EMOJI['strength']} {strength}"
        )
    shown = variants[:6]
    if len(shown) > 1 and shown[0].get("status") == "ok" and all(v.get("status") != "ok" for v in shown[1:]):
        lines.append(ONLY_BASELINE_TEXT)
    if strength_unknown:
        lines.append(STRENGTH_UNKNOWN_TEXT)
    return lines


_GOAL_SOURCES = {"owner": "выбрал ты", "fallback": "по умолчанию"}


def goal_warning(primary: str, settings: Mapping) -> str | None:
    """A line when ``settings`` (an explicit setting wins over the base point) break the
    goal's rules: '⚠️ Цель «Точно» требует: стенок не меньше 3, ...'. None when they hold."""
    goal = GOALS[primary]
    lim = goal.limits
    words = {
        "wall_loops": f"стенок не меньше {lim.min_walls}",
        "wall_generator": f"генератор стенок {lim.wall_generator}",
        "outer_wall_speed": f"внешняя стенка не быстрее {lim.max_outer_wall_speed or 0:g} мм/с",
        "top_shell_layers": f"верх не меньше {lim.min_top_bottom} слоёв",
        "bottom_shell_layers": f"низ не меньше {lim.min_top_bottom} слоёв",
    }
    broken = broken_rules(goal, settings)
    if not broken:
        return None
    return f"⚠️ Цель «{goal.label}» требует: {', '.join(words[k] for k in broken)}"


def goal_line(primary: str, secondary: str | None, source: str, reason: str | None) -> str:
    """'цель: прочно, затем красиво (от ассистента: <reason>)'."""
    words = GOALS[primary].label.lower()
    if secondary is not None:
        words += f", затем {GOALS[secondary].label.lower()}"
    if source == "assistant":
        why = f"от ассистента: {md_literal(reason)}" if reason else "от ассистента"
    else:
        why = _GOAL_SOURCES[source]
    return f"{EMOJI['goal']} цель: {words} ({why})"


# -- job proposal and slice summary ------------------------------------------------

_HINT_WORDS = {
    "walls": "стенки",
    "infill_pct": "заполнение",
    "infill_pattern": "рисунок заполнения",
    "supports": "поддержки",
    "brim": "кайма",
    "material": "материал",
    "material_reason": "причина материала",
    "goal": "цель",
    "goal_reason": "причина цели",
}
_BRIM_WORDS = {"no_brim": "без каймы", "auto_brim": "кайма авто", "outer_only": "кайма снаружи"}
_SEAM_WORDS = {"back": "шов сзади", "aligned": "шов по линии", "nearest": "шов ближайший", "random": "шов случайный"}
_IRONING_WORDS = {"no ironing": "без разглаживания", "top": "разглаживание", "topmost": "разглаживание верха"}
_PLATE_SUFFIX = " Plate"


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _about(text: str) -> str:
    """'~' for "about", escaped: a pair of tildes is strikethrough in the add-in's Markdown."""
    return f"\\~{text}"


def _grams(value: float) -> str:
    return f"{value:.0f} г"


def _infill(density: object, pattern: object) -> str:
    if density is None:
        return f"заполнение {md_literal(str(pattern))}"
    return f"{density}% {md_literal(str(pattern))}" if pattern else f"{density}% заполнение"


def _setting_parts(settings: Mapping) -> list[str]:
    """The settings in words, in a fixed order. Keys left to the process preset are omitted."""
    parts = []
    if "wall_loops" in settings:
        n = int(settings["wall_loops"])
        parts.append(f"{n} {_plural(n, 'стенка', 'стенки', 'стенок')}")
    density, pattern = settings.get("sparse_infill_density"), settings.get("sparse_infill_pattern")
    if density is not None or pattern is not None:
        parts.append(_infill(density, pattern))
    if "enable_support" in settings:
        if not settings["enable_support"]:
            parts.append("без поддержек")
        elif "support_type" in settings:
            parts.append(f"поддержки {md_literal(str(settings['support_type']).removesuffix('(auto)'))}")
        else:
            parts.append("с поддержками")
    if "brim_type" in settings:
        brim = str(settings["brim_type"])
        parts.append(_BRIM_WORDS.get(brim, md_literal(brim)))
    if "layer_height" in settings:
        parts.append(f"слой {settings['layer_height']} мм")
    if "wall_generator" in settings:
        parts.append(f"стенки {md_literal(str(settings['wall_generator']))}")
    parts += _shell_parts(settings)
    if "outer_wall_speed" in settings:
        speed = settings["outer_wall_speed"]
        shown = f"{speed:g}" if isinstance(speed, int | float) else md_literal(str(speed))
        parts.append(f"внешняя стенка {shown} мм/с")
    if "seam_position" in settings:
        seam = str(settings["seam_position"])
        parts.append(_SEAM_WORDS.get(seam, f"шов {md_literal(seam)}"))
    if "ironing_type" in settings:
        ironing = str(settings["ironing_type"])
        parts.append(_IRONING_WORDS.get(ironing, f"разглаживание {md_literal(ironing)}"))
    return parts


def _shell_parts(settings: Mapping) -> list[str]:
    top, bottom = settings.get("top_shell_layers"), settings.get("bottom_shell_layers")
    if top is not None and bottom is not None:
        return [f"верх/низ {md_literal(str(top))}/{md_literal(str(bottom))}"]
    for word, value in (("верх", top), ("низ", bottom)):
        if value is not None:
            n = int(value)
            return [f"{word} {n} {_plural(n, 'слой', 'слоя', 'слоёв')}"]
    return []


def _slot_text(choice: SlotChoice) -> str:
    if choice.ams is None or choice.slot is None:
        return "слот не выбран"
    number = slot_number(choice.ams, choice.slot)
    words = [EXTERNAL_LABEL if number == EXTERNAL_SLOT_NUMBER else f"слот {number}"]
    if choice.tray_type:
        words.append(md_literal(choice.tray_type))
    if choice.color_name:
        words.append(md_literal(choice.color_name))
    left = _grams(choice.remaining_g) if choice.remaining_g is not None else "? г"
    return f"{' '.join(words)} ({_about(left)})"


def _job_head(manifest: Manifest, settings: Mapping, choice: SlotChoice, plate_type: str) -> str:
    """'<name>, пластина 1: <slot>, <settings>, <bed>.' (a kit: '<name>, N пластин: ...')."""
    bed = str(settings.get("curr_bed_type", plate_type)).removesuffix(_PLATE_SUFFIX)
    body = ", ".join([_slot_text(choice), *_setting_parts(settings), md_literal(bed)])
    if manifest.kind == "kit":
        n = len(manifest.plates)
        where = f"{n} {_plural(n, 'пластина', 'пластины', 'пластин')}"
    else:
        where = f"пластина {manifest.plates[0].index}"
    return f"{EMOJI['printer']} {job_name(manifest.name)}, {where}: {body}."


def _support_words(settings: Mapping) -> str:
    keys = ("enable_support", "support_type") if settings.get("enable_support") else ("enable_support",)
    return ", ".join(_setting_parts({k: settings[k] for k in keys if k in settings}))


def _plate_support_note(plate: PlateInfo, settings: Mapping) -> str | None:
    """A plate's supports wish the kit's one setting does not follow, in words.

    A stated ``none``/``tree``/``normal`` hint is compared as is. Without one (or with
    ``auto``) only a plate whose checks suggest supports is noted when the kit has none:
    the kit's auto supports add nothing where a plate does not need them.
    """
    hint = plate.hints.get("supports", "auto")
    wish = hint_overrides({"supports": hint}, plate.checks)
    enabled = bool(settings.get("enable_support"))
    if hint == "auto":
        if not wish["enable_support"] or enabled:
            return None
    elif wish["enable_support"] == enabled and (
        not enabled or "support_type" not in wish or wish["support_type"] == settings.get("support_type")
    ):
        return None
    have = _support_words(settings) or "по профилю"
    return f"в CAD {_support_words(wish)}, в настройках {have}"


def _plate_notes(manifest: Manifest, settings: Mapping) -> list[str]:
    """Kit plates: hints their parts disagree on, and plate hints the job settings differ from.

    A kit is sliced with one set of overrides, so the owner sees what a plate asked for.
    """
    if manifest.kind != "kit":
        return []
    material = job_material(manifest)[0]
    notes = []
    for plate in manifest.plates:
        if plate.hint_conflicts:
            words = ", ".join(_HINT_WORDS.get(k, md_literal(k)) for k in plate.hint_conflicts)
            notes.append(f"Пластина {plate.index}: у деталей разные подсказки ({words}), взяты общие настройки")
        hinted = {k: v for k, v in plate.hints.items() if k in ("walls", "infill_pct", "infill_pattern", "brim")}
        for key, value in hint_overrides(hinted).items():
            if key == "enable_support" or settings.get(key) == value:
                continue  # supports are compared below
            have = ", ".join(_setting_parts({key: settings[key]})) if key in settings else "по профилю"
            want = ", ".join(_setting_parts({key: value}))
            notes.append(f"Пластина {plate.index}: в CAD {want}, в настройках {have}")
        support_note = _plate_support_note(plate, settings)
        if support_note:
            notes.append(f"Пластина {plate.index}: {support_note}")
        plate_material = plate.hints.get("material")
        if plate_material and plate_material.casefold() != material.casefold():
            notes.append(
                f"Пластина {plate.index}: в CAD материал {md_literal(plate_material)}, "
                f"а набор печатается одним слотом"
            )
    return notes


def _spool_warnings(manifest: Manifest, choice: SlotChoice, grams: Mapping[int, float]) -> list[str]:
    if choice.remaining_g is None:
        return []
    left = _about(_grams(choice.remaining_g))
    over = [i for i in sorted(grams) if grams[i] > choice.remaining_g]
    out = [f"⚠️ На катушке {left}, а пластине {i} нужно {_about(_grams(grams[i]))}" for i in over]
    total = sum(grams.values())
    if not over and manifest.kind == "kit" and total > choice.remaining_g:
        out.append(f"⚠️ На катушке {left}, а набору нужно {_about(_grams(total))}")
    return out


def _job_manifest(job: Job) -> Manifest:
    return parse_manifest(json.loads(job.manifest_json))  # checked at intake


def render_proposal(
    job: Job,
    settings: Mapping,
    slot_choice: SlotChoice,
    plate_type: str,
    estimate: Mapping[int, float] | None,
    cost: CostBreakdown | CurrencyMismatch | None = None,
    *,
    symbol: str = DEFAULT_SYMBOL,
    goal: tuple[str, str | None, str, str | None] | None = None,
    stale_minutes: bool = False,
    explicit: Mapping | None = None,
) -> str:
    """What the service is about to slice. ``estimate``: grams per plate index (None: the manifest's).

    Plain text, no buttons. ``plate_type`` is the bed when ``settings`` has no ``curr_bed_type``.
    ``cost``: the predicted cost of the whole job (its lines follow the grams), priced as
    if the print started now. ``stale_minutes``: its time comes from a slice made with other
    settings, which a line says. ``goal``: (primary, secondary, source, reason) from
    ``evaluation.job_goal``, its line follows the cost. ``explicit``: the owner's stored
    settings, named on their own line after the goal.
    """
    manifest = _job_manifest(job)
    grams = dict(estimate) if estimate is not None else {p.index: p.estimate_g for p in manifest.plates}
    head = _job_head(manifest, settings, slot_choice, plate_type)
    if manifest.kind == "kit":
        lines = [head, *(f"Пластина {i}: {_about(_grams(g))}" for i, g in sorted(grams.items()))]
    else:
        lines = [f"{head} {_about(_grams(sum(grams.values())))}"]
    if cost is not None:
        lines += render_cost(cost, symbol, start_now=True)
        if stale_minutes:
            lines.append(STALE_MINUTES_TEXT)
    if goal is not None:
        lines.append(goal_line(*goal))
    owner_line = explicit_line(explicit or {})
    if owner_line:
        lines.append(owner_line)
    material, reason = job_material(manifest)
    if reason:
        lines.append(f"Почему {md_literal(material)}: {md_literal(reason)}")
    lines += _plate_notes(manifest, settings)
    if slot_choice.warning:
        lines.append(slot_choice.warning)
    lines += _spool_warnings(manifest, slot_choice, grams)
    return "\n".join(lines)


STALE_MINUTES_TEXT = "время и свет по прошлой нарезке, настройки с тех пор изменились"


def _result_text(minutes: float, grams: float) -> str:
    return f"{_about(_duration(round(minutes)))}, {_grams(grams)}"


def render_sliced(
    job: Job,
    plate_results: Sequence[Mapping],
    costs: Sequence[CostBreakdown | CurrencyMismatch],
    *,
    settings: Mapping,
    slot_choice: SlotChoice,
    plate_type: str,
    symbol: str = DEFAULT_SYMBOL,
) -> str:
    """The slice summary, plain text.

    ``plate_results`` are the slice-agent's plates (``{"index", "minutes", "grams":
    {filament: g}}``), ``costs`` one per plate in the same order, priced as if the print
    started now. A kit lists each plate with its total, then the cost lines of the sum.
    The slot's warning (a material mismatch) ends the text, as in the proposal: a job
    sliced without a proposal shows it only here.
    """
    if not plate_results or len(plate_results) != len(costs):
        raise ValueError("render_sliced needs one cost per plate result")
    manifest = _job_manifest(job)
    head = _job_head(manifest, settings, slot_choice, plate_type)
    rows = [
        (int(p["index"]), float(p["minutes"]), sum(float(g) for g in p["grams"].values()), c)
        for p, c in zip(plate_results, costs, strict=True)
    ]
    warning = [slot_choice.warning] if slot_choice.warning else []
    if manifest.kind != "kit":
        _index, minutes, grams, cost = rows[0]
        cost_lines = render_cost(cost, symbol, start_now=True)
        return "\n".join([f"{head} {_result_text(minutes, grams)}", *cost_lines, *warning])
    lines = [head]
    for i, m, g, c in rows:
        money = f", {_about(money_text(c.total.amount, symbol))}" if isinstance(c, CostBreakdown) else ""
        lines.append(f"Пластина {i}: {_result_text(m, g)}{money}")
    lines.append(f"Всего: {_result_text(sum(r[1] for r in rows), sum(r[2] for r in rows))}")
    lines += render_cost(sum_costs(costs), symbol, start_now=True)
    return "\n".join([*lines, *warning])


# -- printing (phase 3) ----------------------------------------------------------------

PRINT_BUTTON_TEXT = "Печатать…"
START_BUTTON_TEXT = "Стол чистый, печатать"  # the owner confirms the bed in the press itself
START_CANCEL_TEXT = "Отмена"
STOP_ASK_TEXT = "Остановить…"
STOP_GO_TEXT = "Да, остановить"
_PLATES_PER_ROW = 4


def print_buttons(job_id: int) -> list[list[Button]]:
    """The sliced summary's button: ``job:<id>:print``."""
    return [[Button(f"job:{job_id}:print", PRINT_BUTTON_TEXT)]]


def plate_question(name: str | None) -> str:
    return f"Какую пластину «{job_name(name)}» печатать?"


def plate_buttons(job_id: int, plates: Sequence[int]) -> list[list[Button]]:
    """One ``job:<id>:print:<n>`` button per plate of a kit."""
    buttons = [Button(f"job:{job_id}:print:{n}", f"Пластина {n}") for n in plates]
    return [buttons[i : i + _PLATES_PER_ROW] for i in range(0, len(buttons), _PLATES_PER_ROW)]


def start_buttons(confirm_id: str) -> list[list[Button]]:
    return [[Button(f"start:{confirm_id}:go", START_BUTTON_TEXT), Button(f"start:{confirm_id}:cancel", START_CANCEL_TEXT)]]


def stop_ask_buttons(task_id: str) -> list[list[Button]]:
    return [[Button(f"stop:ask:{task_id}", STOP_ASK_TEXT)]]


def stop_buttons(confirm_id: str) -> list[list[Button]]:
    return [[Button(f"stop:{confirm_id}:go", STOP_GO_TEXT), Button(f"stop:{confirm_id}:cancel", START_CANCEL_TEXT)]]


def render_stop_confirmation(name: str | None) -> str:
    """The question the stop button answers. A stopped print cannot be resumed."""
    return "\n".join(
        [
            f"⏹ Остановить печать «{job_name(name)}»?",
            "Продолжить её потом будет нельзя. Кнопка действует 10 минут.",
        ]
    )


def render_start_confirmation(
    name: str | None,
    *,
    plate: int | None,
    slot_choice: SlotChoice,
    minutes: float,
    grams: float,
    plate_type: str,
    warnings: Sequence[str],
    cost: CostBreakdown | CurrencyMismatch | None = None,
    symbol: str = DEFAULT_SYMBOL,
) -> str:
    """The question the start button answers. ``plate`` None for a part (always plate 1).

    ``cost``: the plate's predicted cost, priced as if the print started now.
    """
    where = f", пластина {plate}" if plate is not None else ""
    lines = [
        f"🖨 Печатать «{job_name(name)}»{where}?",
        f"{_slot_text(slot_choice)}, {_about(_duration(round(minutes)))}, {_grams(grams)}",
        *(render_cost(cost, symbol, start_now=True) if cost is not None else ()),
        f"На столе должна лежать {md_literal(plate_type)}",
        *(f"⚠️ {w}" for w in warnings),
        "Проверь, что стол чистый и пустой. Кнопка действует 10 минут.",
    ]
    return "\n".join(lines)


def render_print_refused(name: str | None, lines: Sequence[str], *, started: bool) -> str:
    """Why a print was not prepared (``started`` False) or not started. ``lines`` are escaped."""
    what = "не запущена" if started else "не подготовлена"
    return "\n".join([f"❌ Печать «{job_name(name)}» {what}:", *lines])


def render_print_started(name: str | None, plate: int | None) -> str:
    where = f", пластина {plate}" if plate is not None else ""
    return f"▶️ Печать началась\n«{job_name(name)}»{where}"


def render_start_unconfirmed(name: str | None, watch_s: float, gcode_state: str, subtask_name: str | None) -> str:
    """The printer did not report our task in the watch window. The command is never sent again."""
    job = f"«{job_name(subtask_name)}»" if subtask_name else "без задания"
    return "\n".join(
        [
            f"⚠️ Принтер не подтвердил старт «{job_name(name)}» за {watch_s:.0f} с",
            f"Последнее, что он прислал: {md_literal(gcode_state)}, {job}",
            "Команда повторно не отправляется. Посмотри на принтер, и если печати нет, подготовь её заново.",
        ]
    )
