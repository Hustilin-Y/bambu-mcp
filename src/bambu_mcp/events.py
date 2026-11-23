"""Turn consecutive PrinterState snapshots into print events, deduplicated persistently."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from bambu_mcp.printer.hms import hms_code, severity
from bambu_mcp.printer.state import Hms, PrinterState

CANCEL_ERROR = 50348044
_START_FROM = frozenset({"IDLE", "FINISH", "FAILED", "UNKNOWN"})
_ACTIVE = frozenset({"PREPARE", "RUNNING", "PAUSE"})


class EventKind(StrEnum):
    STARTED = "started"
    FIRST_LAYER_DONE = "first_layer_done"
    PAUSED = "paused"
    RESUMED = "resumed"
    HMS_WARNING = "hms_warning"
    HMS_ERROR = "hms_error"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"


def event_identity(state: PrinterState) -> tuple[str, str, str]:
    """Job identity with None and "" treated as equal."""
    return (state.subtask_name or "", state.task_id or "", state.gcode_start_time or "")


@dataclass(frozen=True)
class Event:
    kind: EventKind
    identity: tuple
    at: datetime
    state: PrinterState
    hms: Hms | None = None
    end_unknown: bool = False  # the service did not see the print end (reconciled later)
    lost: bool = False  # ...and the printer no longer knows the job: interrupted unseen

    @property
    def dedupe_key(self) -> str:
        name, task_id, start = (part or "" for part in self.identity)
        key = f"{name}|{task_id}|{start}|{self.kind}"
        if self.hms is not None:
            key += f"|{hms_code(self.hms)}"
        if self.kind in (EventKind.PAUSED, EventKind.RESUMED):
            key += f"|L{self.state.layer or 0}"
        return key


class SeenStore(Protocol):
    def seen_contains(self, key: str) -> bool: ...

    def seen_add(self, key: str) -> None: ...


class EventDetector:
    """Detects events between two states. Keeps the last known state across UNKNOWN blips."""

    def __init__(self, seen: SeenStore) -> None:
        self._seen = seen
        self._last_known: PrinterState | None = None  # last state that was not UNKNOWN
        self._deferred_start = False  # a start whose job identity may still be the old job's

    def feed(self, prev: PrinterState, new: PrinterState, at: datetime) -> list[Event]:
        # An unrecognised gcode_state is not a state change: compare with the last known one.
        before = prev
        if prev.gcode_state == "UNKNOWN" and self._last_known is not None:
            before = self._last_known
        out: list[Event] = []
        for kind, hms, state in self._detect(before, prev, new):
            event = Event(kind, event_identity(state), at, state, hms)
            key = event.dedupe_key
            if self._seen.seen_contains(key):
                continue
            self._seen.seen_add(key)
            out.append(event)
        if new.gcode_state != "UNKNOWN":
            self._last_known = new
        return out

    def _detect(
        self, before_state: PrinterState, prev: PrinterState, new: PrinterState
    ) -> list[tuple[EventKind, Hms | None, PrinterState]]:
        found: list[tuple[EventKind, Hms | None, PrinterState]] = []
        before, now = before_state.gcode_state, new.gcode_state
        changed = before != now
        has_job = bool(new.subtask_name or new.task_id)
        new_identity = event_identity(new)
        identity_changed = new_identity != event_identity(before_state)

        start = False
        if changed and now in ("PREPARE", "RUNNING") and before in _START_FROM and has_job:
            if before in ("FINISH", "FAILED") and not identity_changed:
                # The printer may report RUNNING one report before the new job's name and
                # task id: wait one report rather than announce the old job again.
                self._deferred_start = True
            else:
                start = True
        elif now in _ACTIVE and has_job and (
            (before in _ACTIVE and identity_changed) or self._deferred_start
        ):
            start = True  # a new job while active, or the deferred start
        if start or now not in _ACTIVE:
            self._deferred_start = False
        if start:
            found.append((EventKind.STARTED, None, new))

        if (new.layer or 0) >= 2 and (prev.layer or 0) < 2 and now in _ACTIVE:
            found.append((EventKind.FIRST_LAYER_DONE, None, new))
        if changed and now == "PAUSE" and before in ("PREPARE", "RUNNING"):
            found.append((EventKind.PAUSED, None, new))
        if changed and before == "PAUSE" and now == "RUNNING":
            found.append((EventKind.RESUMED, None, new))

        known = set(prev.hms)
        for h in new.hms:
            if h in known:
                continue
            sev = severity(h)
            if sev in ("fatal", "serious"):
                found.append((EventKind.HMS_ERROR, h, new))
            elif sev == "common":
                found.append((EventKind.HMS_WARNING, h, new))

        # Terminal events only for transitions seen while a print was active, so a FINISH
        # seen as the first state after start/reconnect (an older job) is not "finished";
        # the service reconciles those against the journal.
        if changed and before in _ACTIVE:
            # The job is the one that was running when the printer no longer names it.
            ended = new if has_job and now != "IDLE" else before_state
            if now == "FINISH":
                found.append((EventKind.FINISHED, None, ended))
            elif now == "FAILED":
                cancelled = new.print_error == CANCEL_ERROR
                kind = EventKind.CANCELLED if cancelled else EventKind.FAILED
                found.append((kind, None, ended))
            elif now == "IDLE":
                found.append((EventKind.FAILED, None, ended))
        return found
