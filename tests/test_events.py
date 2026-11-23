import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bambu_mcp.events import EventDetector, EventKind
from bambu_mcp.printer.state import Hms, PrinterState, apply

FIXTURES = Path(__file__).parent / "fixtures" / "p2s"
AT = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
FATAL = Hms(attr=0x0C000300, code=0x00010001)
COMMON = Hms(attr=0x0C000300, code=0x00030001)
INFO = Hms(attr=0x0C000300, code=0x00040001)


class FakeSeen:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    def seen_contains(self, key: str) -> bool:
        return key in self.keys

    def seen_add(self, key: str) -> None:
        self.keys.add(key)


def fx(name: str) -> PrinterState:
    report = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return apply(PrinterState.empty(), report)


def run(detector: EventDetector, states: list[PrinterState], start=None) -> list:
    prev = start or PrinterState.empty()
    events = []
    for state in states:
        events += detector.feed(prev, state, AT)
        prev = state
    return events


def kinds(events) -> list[EventKind]:
    return [e.kind for e in events]


def test_print_lifecycle():
    names = ("pushall_idle", "running_layer0", "running_layer1", "running_layer2", "finish")
    events = run(EventDetector(FakeSeen()), [fx(n) for n in names])
    assert kinds(events) == [EventKind.STARTED, EventKind.FIRST_LAYER_DONE, EventKind.FINISHED]
    assert events[0].identity[0] == "cube"


def test_first_finish_state_is_not_an_event():
    # pushall_idle is FINISH of an older job: as the first state it must not emit "finished".
    assert run(EventDetector(FakeSeen()), [fx("pushall_idle")]) == []


def test_reconnect_does_not_repeat_started():
    d = EventDetector(FakeSeen())
    l2 = fx("running_layer2")
    run(d, [fx("pushall_idle"), fx("running_layer0"), fx("running_layer1"), l2])
    assert run(d, [PrinterState.empty(), l2], start=l2) == []


def test_restart_keeps_dedupe():
    seen = FakeSeen()
    seq = [fx("running_layer0"), fx("running_layer1"), fx("running_layer2")]
    first = run(EventDetector(seen), seq)
    assert EventKind.STARTED in kinds(first)
    assert run(EventDetector(seen), seq) == []


@pytest.mark.synthetic
def test_empty_string_and_none_identity_are_equal():
    seen = FakeSeen()
    base = fx("running_layer0")
    d = EventDetector(seen)
    a = run(d, [replace(base, task_id=None, gcode_start_time=None)])
    b = run(d, [replace(base, task_id="", gcode_start_time="")])
    assert kinds(a) == [EventKind.STARTED]
    assert b == []


@pytest.mark.synthetic
def test_started_requires_job_identity():
    nameless = replace(fx("running_layer0"), subtask_name="", task_id=None)
    assert run(EventDetector(FakeSeen()), [nameless]) == []


@pytest.mark.synthetic
@pytest.mark.parametrize(("error", "kind"), [(50348044, EventKind.CANCELLED), (0, EventKind.FAILED)])
def test_cancel_vs_fail(error, kind):
    running = fx("running_layer1")
    failed = replace(running, gcode_state="FAILED", print_error=error)
    assert kinds(run(EventDetector(FakeSeen()), [failed], start=running)) == [kind]


@pytest.mark.synthetic
def test_pause_and_resume():
    running = fx("running_layer1")
    paused = replace(running, gcode_state="PAUSE")
    events = run(EventDetector(FakeSeen()), [paused, running], start=running)
    assert kinds(events) == [EventKind.PAUSED, EventKind.RESUMED]


@pytest.mark.synthetic
def test_hms_error_once_per_code():
    running = fx("running_layer1")
    with_hms = replace(running, hms=(FATAL,))
    events = run(EventDetector(FakeSeen()), [with_hms] * 5, start=running)
    assert kinds(events) == [EventKind.HMS_ERROR]
    assert events[0].hms == FATAL
    assert events[0].dedupe_key.endswith("|hms_error|0C00_0300_0001_0001")


@pytest.mark.synthetic
def test_hms_severity_mapping():
    running = fx("running_layer1")
    state = replace(running, hms=(COMMON, INFO))
    assert kinds(run(EventDetector(FakeSeen()), [state], start=running)) == [EventKind.HMS_WARNING]


def test_unknown_state_yields_no_event():
    unknown = apply(PrinterState.empty(), {"print": {"gcode_state": "WEIRD"}})
    assert unknown.gcode_state == "UNKNOWN"
    assert run(EventDetector(FakeSeen()), [unknown]) == []
    assert EventDetector(FakeSeen()).feed(fx("running_layer1"), unknown, AT) == []


@pytest.mark.synthetic
def test_repeated_pause_resume_at_different_layers_emitted():
    seen = FakeSeen()
    l1, l5 = fx("running_layer1"), replace(fx("running_layer1"), layer=5)
    seq = [
        replace(l1, gcode_state="PAUSE"),
        l1,
        replace(l5, gcode_state="PAUSE"),
        l5,
    ]
    all_events = run(EventDetector(seen), seq, start=l1)
    events = [e for e in all_events if e.kind != EventKind.FIRST_LAYER_DONE]
    assert kinds(events) == [EventKind.PAUSED, EventKind.RESUMED] * 2
    assert events[0].dedupe_key.endswith("|paused|L1")
    assert events[3].dedupe_key.endswith("|resumed|L5")
    # Same layer after a restart (fresh detector, same store) stays deduped.
    again = run(EventDetector(seen), seq[:2], start=l1)
    assert again == []


@pytest.mark.synthetic
def test_reconnect_does_not_repeat_hms_error():
    seen = FakeSeen()
    running = fx("running_layer1")
    with_hms = replace(running, hms=(FATAL,))
    d = EventDetector(seen)
    assert kinds(run(d, [running, with_hms])) == [EventKind.STARTED, EventKind.HMS_ERROR]
    assert run(d, [PrinterState.empty(), with_hms], start=with_hms) == []
