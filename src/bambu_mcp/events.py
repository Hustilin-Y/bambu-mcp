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
    def __init__(self, seen: SeenStore) -> None:
        self._seen = seen

    def feed(self, prev: PrinterState, new: PrinterState, at: datetime) -> list[Event]:
        out: list[Event] = []
        for kind, hms in self._detect(prev, new):
            event = Event(kind, event_identity(new), at, new, hms)
            key = event.dedupe_key
            if self._seen.seen_contains(key):
                continue
            self._seen.seen_add(key)
            out.append(event)
        return out

    @staticmethod
    def _detect(prev: PrinterState, new: PrinterState) -> list[tuple[EventKind, Hms | None]]:
        found: list[tuple[EventKind, Hms | None]] = []
        before, now = prev.gcode_state, new.gcode_state
        changed = before != now

        if (
            changed
            and now in ("PREPARE", "RUNNING")
            and before in _START_FROM
            and (new.subtask_name or new.task_id)
        ):
            found.append((EventKind.STARTED, None))
        if (new.layer or 0) >= 2 and (prev.layer or 0) < 2 and now in _ACTIVE:
            found.append((EventKind.FIRST_LAYER_DONE, None))
        if changed and now == "PAUSE" and before in ("PREPARE", "RUNNING"):
            found.append((EventKind.PAUSED, None))
        if changed and before == "PAUSE" and now == "RUNNING":
            found.append((EventKind.RESUMED, None))

        known = set(prev.hms)
        for h in new.hms:
            if h in known:
                continue
            sev = severity(h)
            if sev in ("fatal", "serious"):
                found.append((EventKind.HMS_ERROR, h))
            elif sev == "common":
                found.append((EventKind.HMS_WARNING, h))

        # Terminal events only for transitions seen while a print was active, so a FINISH
        # seen as the first state after start/reconnect (an older job) is not "finished".
        if changed and before in _ACTIVE:
            if now == "FINISH":
                found.append((EventKind.FINISHED, None))
            elif now == "FAILED":
                cancelled = new.print_error == CANCEL_ERROR
                found.append((EventKind.CANCELLED if cancelled else EventKind.FAILED, None))
        return found
