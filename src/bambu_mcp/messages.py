"""Russian Telegram texts and button layouts for print events."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import tzinfo
from zoneinfo import ZoneInfo

from bambu_mcp.cost import CostLine
from bambu_mcp.events import Event, EventKind
from bambu_mcp.manifest import Manifest, PlateInfo, parse_manifest
from bambu_mcp.notify import Button
from bambu_mcp.printer.hms import HmsTexts, describe, hms_code
from bambu_mcp.proposal import SlotChoice, hint_overrides, job_material, slot_number
from bambu_mcp.store.models import Job, Spool

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


# -- job proposal and slice summary ------------------------------------------------

_HINT_WORDS = {
    "walls": "стенки",
    "infill_pct": "заполнение",
    "infill_pattern": "рисунок заполнения",
    "supports": "поддержки",
    "brim": "кайма",
    "material": "материал",
    "material_reason": "причина материала",
}
_BRIM_WORDS = {"no_brim": "без каймы", "auto_brim": "кайма авто", "outer_only": "кайма снаружи"}
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
    return parts


def _slot_text(choice: SlotChoice) -> str:
    if choice.ams is None or choice.slot is None:
        return "слот не выбран"
    words = [f"слот {slot_number(choice.ams, choice.slot)}"]
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
    return f"{job_name(manifest.name)}, {where}: {body}."


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
) -> str:
    """What the service is about to slice. ``estimate``: grams per plate index (None: the manifest's).

    Plain text, no buttons. ``plate_type`` is the bed when ``settings`` has no ``curr_bed_type``.
    """
    manifest = _job_manifest(job)
    grams = dict(estimate) if estimate is not None else {p.index: p.estimate_g for p in manifest.plates}
    head = _job_head(manifest, settings, slot_choice, plate_type)
    if manifest.kind == "kit":
        lines = [head, *(f"Пластина {i}: {_about(_grams(g))}" for i, g in sorted(grams.items()))]
    else:
        lines = [f"{head} {_about(_grams(sum(grams.values())))}"]
    material, reason = job_material(manifest)
    if reason:
        lines.append(f"Почему {md_literal(material)}: {md_literal(reason)}")
    lines += _plate_notes(manifest, settings)
    if slot_choice.warning:
        lines.append(slot_choice.warning)
    lines += _spool_warnings(manifest, slot_choice, grams)
    return "\n".join(lines)


def _result_text(minutes: float, grams: float, cost: CostLine) -> str:
    money = _about(_money(cost.total_uah))
    if cost.filament_uah is None:
        money += " без пластика"
    return f"{_about(_duration(round(minutes)))}, {_grams(grams)}, {money}"


def render_sliced(
    job: Job,
    plate_results: Sequence[Mapping],
    cost_lines: Sequence[CostLine],
    *,
    settings: Mapping,
    slot_choice: SlotChoice,
    plate_type: str,
) -> str:
    """The slice summary, plain text.

    ``plate_results`` are the slice-agent's plates (``{"index", "minutes", "grams":
    {filament: g}}``), ``cost_lines`` one per plate in the same order.
    """
    if not plate_results or len(plate_results) != len(cost_lines):
        raise ValueError("render_sliced needs one cost line per plate result")
    manifest = _job_manifest(job)
    head = _job_head(manifest, settings, slot_choice, plate_type)
    rows = [
        (int(p["index"]), float(p["minutes"]), sum(float(g) for g in p["grams"].values()), c)
        for p, c in zip(plate_results, cost_lines, strict=True)
    ]
    if manifest.kind != "kit":
        _index, minutes, grams, cost = rows[0]
        return f"{head} {_result_text(minutes, grams, cost)}"
    costs = [c for *_, c in rows]
    known = all(c.filament_uah is not None for c in costs)
    total = CostLine(
        filament_uah=sum(c.filament_uah or 0.0 for c in costs) if known else None,
        power_uah=sum(c.power_uah for c in costs),
        kwh=sum(c.kwh for c in costs),
        tariff=costs[0].tariff,
    )
    lines = [head, *(f"Пластина {i}: {_result_text(m, g, c)}" for i, m, g, c in rows)]
    lines.append(f"Всего: {_result_text(sum(r[1] for r in rows), sum(r[2] for r in rows), total)}")
    return "\n".join(lines)
