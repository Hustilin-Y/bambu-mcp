"""Print journal: one row per print, opened and closed from events, updated from presses."""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from bambu_mcp.config import Settings
from bambu_mcp.cost import CostLine, filament_cost, power_cost, print_kwh
from bambu_mcp.events import Event, EventKind
from bambu_mcp.messages import post_print_buttons
from bambu_mcp.names import slug
from bambu_mcp.notify import Press
from bambu_mcp.printer.state import PrinterState
from bambu_mcp.store.db import Store
from bambu_mcp.store.models import Job, PrintRow, Spool

log = logging.getLogger(__name__)

TERMINAL_KINDS = frozenset({EventKind.FINISHED, EventKind.FAILED, EventKind.CANCELLED})
FIT_VALUES = frozenset({"ok", "tight", "loose", "failed"})
DEFAULT_MATERIAL = "PLA"
NO_SPOOL = "none"

Slot = tuple[int, int]  # (AMS unit, slot in the unit)

# Sliced files are ``<slug>-<job id>-p1`` (a part) or ``<slug>-<job id>-kit`` (ruling R2).
# Parsed from the right: the slug has dashes of its own. 18 digits stay inside SQLite's int.
_SLICED_NAME = re.compile(r"(?P<slug>.+)-(?P<job>[1-9][0-9]{0,17})-(?P<tag>p1|kit)", re.ASCII)
_SLICED_SUFFIXES = (".gcode.3mf", ".3mf")  # Studio usually sends the name without either
_KIND_TAGS = {"part": "p1", "kit": "kit"}
_UNSLICED = frozenset({"new", "slicing", "slice_failed"})  # every later status has a result


@dataclass(frozen=True)
class JobMatch:
    job_id: int
    plate: int | None  # None: a kit whose plate the printer did not report (ruling R2)
    grams: float  # the plate's predicted grams from the job's current result
    spool_id: int | None  # the spool the job was sliced for


def _utc(at: datetime) -> datetime:
    if at.tzinfo is None:
        raise ValueError("journal timestamps must be timezone-aware")
    return at.astimezone(UTC)


def spool_label(brand: str, material: str, color_name: str) -> str:
    return f"{brand} {material} {color_name}"


class Journal:
    def __init__(self, store: Store, settings: Settings) -> None:
        self._store = store
        self._settings = settings
        self._tz = ZoneInfo(settings.timezone)

    # -- events --------------------------------------------------------------

    def on_event(
        self, event: Event, slot: Slot | None = None, first_seen: datetime | None = None
    ) -> int | None:
        """Open a row on ``started``, close it on a terminal event. Returns the row id.

        ``slot`` is the AMS slot the print fed from, if known: its mapped spool is
        recorded on the row and its material picks the average power. ``first_seen``
        is when the service first saw this job, the start time of a row opened late.
        """
        if event.kind is EventKind.STARTED:
            return self._open(event)
        if event.kind in TERMINAL_KINDS:
            return self._close(event, slot, first_seen)
        return None

    def _open(self, event: Event) -> int:
        row = self._store.print_by_identity(event.identity)
        if row is not None and row.ended_at is None:
            return row.id  # already open (dedupe key expired, or a repeated start)
        return self._open_row(event, event.at)

    def _open_row(self, event: Event, started_at: datetime) -> int:
        """A new row, matched to the sliced job its file name names (booked to the job's spool)."""
        print_id = self._store.open_print(event.identity, _utc(started_at), "external")
        try:
            with self._store.transaction():
                match = match_job(self._store, event.state)
                if match is not None:
                    self._book(print_id, match)
        except Exception:
            log.exception("matching a print to its sliced job failed, journalled as an external print")
        return print_id

    def _book(self, print_id: int, match: JobMatch) -> None:
        self._store.set_print_fields(print_id, source="job", job_id=match.job_id, plate=match.plate)
        spool = None
        if match.spool_id is not None:
            spool = self._spool_by_id(match.spool_id)
            if spool is None or not spool.active:
                log.warning(
                    "job %d: its spool %d is gone or retired, asked after the print",
                    match.job_id,
                    match.spool_id,
                )
                spool = None
        # Without the spool the grams wait for the slot's spool or the owner's answer (phase 1).
        rebook_grams(self._store, print_id, spool, match.grams)

    def _spool_by_id(self, spool_id: int | None) -> Spool | None:
        if spool_id is None:
            return None
        return next((s for s in self._store.spools(active_only=False) if s.id == spool_id), None)

    def _settle_job(self, row: PrintRow, event: Event) -> bool:
        """Fix a matched row's booking at its end. True when it was refunded (ask the owner).

        A kit plate learnt only now takes that plate's grams. A print that did not finish
        keeps the share it printed, or, without a percent, nothing: the spool question decides.
        Runs once per row: the caller only gets here for a row it is closing.
        """
        assert row.job_id is not None
        grams = row.grams
        plate = event.state.plate
        if row.plate is None and plate is not None:
            job = self._store.job(row.job_id)
            found = _result_plate(job, plate) if job is not None and job.kind == "kit" else None
            if found is not None:
                self._store.set_print_fields(row.id, plate=plate)
                grams = found[0]
        if event.kind is not EventKind.FINISHED and grams is not None:
            percent = event.state.percent
            if percent is None or not 0 <= percent <= 100:
                rebook_grams(self._store, row.id, None, grams)
                return True
            grams = round(grams * percent / 100, 1)
        if grams != row.grams:
            rebook_grams(self._store, row.id, self._spool_by_id(row.spool_id), grams)
        return False

    def _close(self, event: Event, slot: Slot | None, first_seen: datetime | None) -> int | None:
        row = self._store.print_by_identity(event.identity)
        if row is None:
            # The start was missed (e.g. a stale job identity): record it from the first sight.
            log.warning("no journal row for a %s print, opening one now", event.kind)
            self._open_row(event, first_seen or event.at)
            row = self._store.print_by_identity(event.identity)
            assert row is not None
        if row.ended_at is not None:
            return row.id
        ended = _utc(event.at)
        started = _utc(row.started_at)
        minutes = None if event.end_unknown else max((ended - started).total_seconds() / 60, 0.0)
        self._store.close_print(row.id, str(event.kind), ended, minutes)

        refunded = False
        if row.job_id is not None:
            try:
                with self._store.transaction():
                    refunded = self._settle_job(row, event)
            except Exception:
                log.exception("settling the grams of a %s job print failed", event.kind)
            row = self._store.print_by_id(row.id) or row
        spool = self._store.slot_spool(*slot) if slot is not None else None
        if spool is not None and row.spool_id is None and not refunded:
            # Grams given earlier without a spool are deducted from this one now.
            rebook_grams(self._store, row.id, spool, row.grams)
        if minutes is None:
            return row.id  # no duration, so no energy or cost either
        try:
            self._record_power(row.id, started, ended, minutes, spool, slot, event)
        except Exception:
            log.exception("print cost for a %s print failed, recorded without cost", event.kind)
        return row.id

    def _record_power(
        self,
        print_id: int,
        started: datetime,
        ended: datetime,
        minutes: float,
        spool: Spool | None,
        slot: Slot | None,
        event: Event,
    ) -> None:
        material = DEFAULT_MATERIAL
        if spool is not None:
            material = spool.material
        elif slot is not None:
            tray = next((t for t in event.state.trays if (t.ams, t.slot) == slot), None)
            if tray is not None and tray.tray_type:
                material = tray.tray_type
        kwh = print_kwh(minutes, material, self._settings)
        tariff = self._store.tariff_on(started.astimezone(self._tz).date())
        cost = power_cost(started, max(ended, started), kwh, tariff, self._settings)
        self._store.set_print_fields(print_id, kwh=kwh, cost_power_uah=cost)

    def cost_line(self, row: PrintRow) -> CostLine | None:
        """Power cost of a closed row as computed at close (filament unknown in phase 1)."""
        if row.kwh is None or row.cost_power_uah is None:
            return None
        tariff = self._store.tariff_on(_utc(row.started_at).astimezone(self._tz).date())
        return CostLine(filament_uah=None, power_uah=row.cost_power_uah, kwh=row.kwh, tariff=tariff)

    # -- presses -------------------------------------------------------------

    def on_press(self, press: Press) -> str | None:
        """Apply a ``fit:`` or ``spool:`` press. Returns the new message text, or None."""
        kind, sep, rest = press.button_id.partition(":")
        raw_id, sep2, value = rest.partition(":")
        if not sep or not sep2:
            return None
        try:
            print_id = int(raw_id)
        except ValueError:
            return None
        if kind == "fit":
            return self._fit(print_id, value)
        if kind == "spool":
            return self._spool(print_id, value)
        return None

    def _fit(self, print_id: int, value: str) -> str | None:
        if value not in FIT_VALUES:
            return None
        try:
            self._store.set_print_fields(print_id, fit=value)
        except KeyError:
            log.warning("fit press for unknown print %d ignored", print_id)
            return None
        labels = {b.id: b.text for row in post_print_buttons(print_id) for b in row}
        return f"Посадка: {labels[f'fit:{print_id}:{value}']}"

    def _spool(self, print_id: int, value: str) -> str | None:
        row = self._store.print_by_id(print_id)
        if row is None:
            log.warning("spool press for unknown print %d ignored", print_id)
            return None
        if value == NO_SPOOL:
            rebook_grams(self._store, print_id, None, row.grams)
            return "Катушка: не списывать"
        try:
            spool_id = int(value)
        except ValueError:
            return None
        spool = next((s for s in self._store.spools(active_only=False) if s.id == spool_id), None)
        if spool is None:
            log.warning("spool press for unknown spool %d ignored", spool_id)
            return None
        rebook_grams(self._store, print_id, spool, row.grams)
        return f"Катушка: {spool_label(spool.brand, spool.material, spool.color_name)}"


def match_job(store: Store, state: PrinterState) -> JobMatch | None:
    """The sliced job and plate the print's file name names, or None (an external print).

    ``subtask_name`` is the file name, with or without ``.gcode.3mf`` / ``.3mf``. A part is
    plate 1; a kit's plate is the report's, and must be one the job's current result has.
    """
    name = state.subtask_name or ""
    for suffix in _SLICED_SUFFIXES:
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    parsed = _SLICED_NAME.fullmatch(name)
    if parsed is None:
        return None
    job = store.job(int(parsed["job"]))
    if (
        job is None
        or job.status in _UNSLICED
        or _KIND_TAGS.get(job.kind) != parsed["tag"]
        or slug(job.name) != parsed["slug"]
    ):
        return None
    plate = 1 if job.kind == "part" else state.plate
    found = _result_plate(job, plate or 1)
    if found is None:
        return None
    grams, spool_id = found
    return JobMatch(job.id, plate, grams, spool_id)


def _result_plate(job: Job, plate: int) -> tuple[float, int | None] | None:
    """(predicted grams of ``plate``, the job's spool id) from its result, None if absent."""
    try:
        result = json.loads(job.result_json or "null")
    except (ValueError, RecursionError):
        return None
    if not isinstance(result, dict) or not isinstance(result.get("plates"), list):
        return None
    entry = next(
        (
            p
            for p in result["plates"]
            if isinstance(p, dict) and type(p.get("index")) is int and p["index"] == plate
        ),
        None,
    )
    grams = entry.get("grams") if entry is not None else None
    if not isinstance(grams, dict) or not all(_grams_ok(g) for g in grams.values()):
        return None
    slot = result.get("slot")
    spool_id = slot.get("spool_id") if isinstance(slot, dict) else None
    return float(sum(grams.values())), spool_id if type(spool_id) is int else None


def _grams_ok(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def rebook_grams(store: Store, print_id: int, spool: Spool | None, grams: float | None) -> None:
    """Point a print at ``spool`` with ``grams`` and move its spool deduction to match."""
    cost = filament_cost(grams, spool, {}, spool.material) if spool and grams is not None else None
    store.rebook_print_grams(print_id, spool.id if spool else None, grams, cost)
