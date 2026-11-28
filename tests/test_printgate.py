import json
import sqlite3
import threading
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bambu_mcp.printer.state import Hms, PrinterState, Tray
from bambu_mcp.printgate import (
    CONFIRMATION_TTL_S,
    Preflight,
    PrintFileError,
    PrintFileInfo,
    gate_hash,
    new_confirmation_id,
    preflight,
    print_file_info,
)
from bambu_mcp.proposal import SlotChoice
from bambu_mcp.store.db import Store
from bambu_mcp.store.models import Job, Spool

_BASE = dict(
    file_sha256="a" * 64,
    plate=1,
    ams=0,
    slot=2,
    serial="SERIAL01",
    nozzle="0.4",
    plate_type="Textured PEI Plate",
)
_OTHER = dict(
    file_sha256="b" * 64,
    plate=2,
    ams=1,
    slot=3,
    serial="SERIAL02",
    nozzle="0.6",
    plate_type="Cool Plate",
)


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "bambu.db")
    s.migrate()
    yield s
    s.close()


def test_gate_hash_is_stable_sha256_hex():
    h = gate_hash(**_BASE)
    assert h == gate_hash(**_BASE)
    assert len(h) == 64 and int(h, 16) >= 0


@pytest.mark.parametrize("field", sorted(_BASE))
def test_gate_hash_changes_with_each_field(field):
    changed = {**_BASE, field: _OTHER[field]}
    assert gate_hash(**changed) != gate_hash(**_BASE)


def test_gate_hash_does_not_mix_fields():
    # ams 1 slot 2 must not hash like ams 12 slot nothing, or swapped values.
    assert gate_hash(**{**_BASE, "ams": 1, "slot": 2}) != gate_hash(**{**_BASE, "ams": 2, "slot": 1})


def test_new_confirmation_id_is_128_bit_hex_and_random():
    ids = {new_confirmation_id() for _ in range(100)}
    assert len(ids) == 100
    for cid in ids:
        assert len(cid) == 32 and int(cid, 16) >= 0


def _add(store: Store, now: float = 1000.0, kind: str = "start"):
    return store.add_confirmation(
        new_confirmation_id(),
        kind,
        job_id=None,
        plate=1,
        gate_hash=gate_hash(**_BASE),
        payload_json='{"ams": 0, "slot": 2}',
        now=now,
    )


def test_add_confirmation_round_trip(store: Store):
    c = _add(store)
    assert c.state == "pending"
    assert c.outcome is None
    assert c.created_at == 1000.0
    assert c.expires_at == 1000.0 + CONFIRMATION_TTL_S == 1600.0
    assert store.confirmation(c.id) == c
    assert store.confirmation("nope") is None


def test_add_confirmation_makes_its_own_id(store: Store):
    c = store.add_confirmation(None, "start", job_id=None, plate=1, gate_hash="h", payload_json="{}", now=1.0)
    assert len(c.id) == 32 and int(c.id, 16) >= 0
    d = store.add_confirmation(None, "start", job_id=None, plate=1, gate_hash="h", payload_json="{}", now=1.0)
    assert d.id != c.id


@pytest.mark.parametrize("bad", ["", "abc", "0" * 31, "0" * 33, "G" * 32, "A" * 32, " " + "0" * 31, 7])
def test_add_confirmation_refuses_an_id_that_is_not_32_hex(store: Store, bad):
    with pytest.raises(ValueError):
        store.add_confirmation(bad, "start", job_id=None, plate=1, gate_hash="h", payload_json="{}", now=1.0)


@pytest.mark.parametrize("ttl", [0, -1, 600.001, float("nan"), float("inf")])
def test_add_confirmation_refuses_a_ttl_outside_ten_minutes(store: Store, ttl):
    with pytest.raises(ValueError):
        store.add_confirmation(None, "start", job_id=None, plate=1, gate_hash="h", payload_json="{}", now=1.0,
                               ttl_s=ttl)


def test_confirmation_outcome_is_recorded(store: Store):
    c = _add(store)
    assert store.claim_confirmation(c.id, now=1001.0) is not None
    store.set_confirmation_outcome(c.id, "refused:preflight")
    got = store.confirmation(c.id)
    assert got.outcome == "refused:preflight" and got.state == "used"
    with pytest.raises(KeyError):
        store.set_confirmation_outcome("f" * 32, "started")


def test_add_confirmation_rejects_unknown_kind(store: Store):
    with pytest.raises(sqlite3.IntegrityError):
        _add(store, kind="resume")


def test_claim_is_single_use(store: Store):
    c = _add(store)
    claimed = store.claim_confirmation(c.id, now=1001.0)
    assert claimed is not None
    assert claimed.id == c.id and claimed.state == "used"
    assert store.claim_confirmation(c.id, now=1002.0) is None
    assert store.confirmation(c.id).state == "used"


def test_claim_is_single_use_across_two_stores_on_one_file(tmp_path: Path):
    path = tmp_path / "shared.db"
    a, b = Store(path), Store(path)
    a.migrate()
    b.migrate()
    c = _add(a)
    results: list = []
    barrier = threading.Barrier(2)

    def claim(s: Store) -> None:
        barrier.wait()
        results.append(s.claim_confirmation(c.id, now=1001.0))

    threads = [threading.Thread(target=claim, args=(s,)) for s in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 2
    assert sum(r is not None for r in results) == 1
    # And sequentially: whichever store asks next gets nothing.
    assert a.claim_confirmation(c.id, now=1002.0) is None
    assert b.claim_confirmation(c.id, now=1002.0) is None
    a.close()
    b.close()


def test_claim_after_expiry_is_none_and_marks_expired(store: Store):
    c = _add(store)
    assert store.claim_confirmation(c.id, now=1000.0 + CONFIRMATION_TTL_S) is None
    assert store.confirmation(c.id).state == "expired"
    # Still None afterwards, even with an earlier clock.
    assert store.claim_confirmation(c.id, now=1001.0) is None
    assert store.confirmation(c.id).state == "expired"


def test_claim_just_before_expiry_wins(store: Store):
    c = _add(store)
    assert store.claim_confirmation(c.id, now=1000.0 + CONFIRMATION_TTL_S - 0.001) is not None


def test_claim_unknown_is_none(store: Store):
    assert store.claim_confirmation("0" * 32, now=1001.0) is None


def test_cancelled_cannot_be_claimed(store: Store):
    c = _add(store)
    assert store.cancel_confirmation(c.id) is True
    assert store.confirmation(c.id).state == "cancelled"
    assert store.claim_confirmation(c.id, now=1001.0) is None
    assert store.confirmation(c.id).state == "cancelled"


def test_cancel_does_not_touch_a_used_confirmation(store: Store):
    c = _add(store)
    assert store.claim_confirmation(c.id, now=1001.0) is not None
    assert store.cancel_confirmation(c.id) is False
    assert store.confirmation(c.id).state == "used"
    assert store.cancel_confirmation("unknown") is False


def test_claim_inside_an_outer_transaction_rolls_back_with_it(store: Store):
    c = _add(store)
    with pytest.raises(RuntimeError):
        with store.transaction():
            assert store.claim_confirmation(c.id, now=1001.0) is not None
            raise RuntimeError("boom")
    assert store.confirmation(c.id).state == "pending"


# -- the print file ------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "manifest"
MD5 = {1: "0123456789ABCDEF0123456789ABCDEF", 2: "FEDCBA9876543210FEDCBA9876543210"}


def _slice_info(plates) -> str:
    """slice_info.config in the shape of the phase 2 output fixtures (tests/slice_agent/test_output.py)."""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>", '  <header><header_item key="X" value="y"/></header>']
    for idx, secs, filaments in plates:
        parts += ["  <plate>", f'    <metadata key="index" value="{idx}"/>', f'    <metadata key="prediction" value="{secs}"/>']
        for fid, kind, grams in filaments:
            parts.append(f'    <filament id="{fid}" type="{kind}" color="#00AE42" used_m="1.7" used_g="{grams}"/>')
        parts.append("  </plate>")
    parts.append("</config>")
    return "\n".join(parts)


KIT_PLATES = ((1, 600, (("1", "PLA", "3.10"),)), (2, 3600, (("1", "PLA", "17.5"), ("2", "PLA", "0.5"))))


def sliced_file(path: Path, plates=KIT_PLATES, *, md5=MD5, gcode=True) -> Path:
    settings = {"printable_area": ["0x0", "256x0", "256x256", "0x256"], "printer_model": "Bambu Lab P2S",
                "nozzle_diameter": ["0.4"]}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/slice_info.config", _slice_info(plates))
        z.writestr("Metadata/project_settings.config", json.dumps(settings))
        for idx, _, _ in plates:
            if gcode:
                z.writestr(f"Metadata/plate_{idx}.gcode", f"; plate {idx}\nG1 X0\n")
            if idx in md5:
                z.writestr(f"Metadata/plate_{idx}.gcode.md5", md5[idx] + "\n")
    return path


def test_kit_plate_picks_its_md5_and_grams(tmp_path: Path):
    path = sliced_file(tmp_path / "desk-kit-9-kit.gcode.3mf")
    two = print_file_info(path, 2)
    assert two == PrintFileInfo(md5=MD5[2], grams=pytest.approx(18.0), minutes=pytest.approx(60.0),
                                filament_types=("PLA",), filament_count=2,
                                printer_model="Bambu Lab P2S", bed_xy=(256, 256))
    one = print_file_info(path, 1)
    assert one.md5 == MD5[1] and one.grams == pytest.approx(3.1) and one.minutes == pytest.approx(10.0)
    assert one.filament_count == 1


def test_part_plate_file_info(tmp_path: Path):
    path = sliced_file(tmp_path / "a.gcode.3mf", ((1, 2315, (("1", "PETG", "5.31"),)),))
    info = print_file_info(path, 1)
    assert info.filament_types == ("PETG",) and info.grams == pytest.approx(5.31)


@pytest.mark.parametrize(
    ("kwargs", "plate", "match"),
    [
        ({"md5": {1: MD5[1]}}, 2, "md5"),  # plate 2 has no .md5
        ({"md5": {1: "not-an-md5", 2: MD5[2]}}, 1, "md5"),
        ({"gcode": False}, 1, "gcode"),
        ({}, 3, "plate 3"),
    ],
)
def test_print_file_info_refuses_an_unusable_file(tmp_path: Path, kwargs, plate, match):
    path = sliced_file(tmp_path / "a.gcode.3mf", **kwargs)
    with pytest.raises(PrintFileError, match=match):
        print_file_info(path, plate)


def test_print_file_info_refuses_a_missing_or_broken_file(tmp_path: Path):
    with pytest.raises(PrintFileError) as info:
        print_file_info(tmp_path / "missing.gcode.3mf", 1)
    assert "missing.gcode.3mf" not in str(info.value)  # no server path in an error to Claude
    broken = tmp_path / "broken.gcode.3mf"
    broken.write_bytes(b"not a zip")
    with pytest.raises(PrintFileError):
        print_file_info(broken, 1)


# -- pre-flight ----------------------------------------------------------------------

SHA = "c" * 64
TRAY = Tray(0, 2, "PETG", "161616FF", 80)


def _job(manifest="part.print.json", color="161616", **result) -> Job:
    data = {"slot": {"ams": 0, "slot": 2, "spool_id": 1, "tray_type": "PETG", "color": color},
            "material": "PETG", "nozzle": "0.4", "file_sha256": SHA, **result}
    return Job(
        id=7, name="lamp-arm", kind="part", manifest_json=(FIXTURES / manifest).read_text(encoding="utf-8"),
        file_sha256="d" * 64, created_at=datetime(2026, 11, 27, tzinfo=UTC), status="sliced",
        settings_json="{}", result_json=json.dumps(data), sliced_file="lamp-arm-7-p1.gcode.3mf", error=None,
    )


def _state(**changes) -> PrinterState:
    base = dict(gcode_state="IDLE", sdcard=True, nozzle_diameter=0.4, trays=(TRAY,), hms=())
    return replace(PrinterState.empty(), **{**base, **changes})


def _spool(**changes) -> Spool:
    base = dict(id=1, brand="Elegoo", material="PETG", color_name="чёрный", color_hex="#161616",
                filament_preset="Generic PETG @BBL P2S", price_uah=550.0, net_g=1000.0, remaining_g=500.0,
                active=True)
    return Spool(**{**base, **changes})


def _check(**changes) -> Preflight:
    args = dict(
        state=_state(), job=_job(), plate=1,
        slot_choice=SlotChoice(0, 2, 1, None, tray_type="PETG", color_name="чёрный", remaining_g=500.0),
        spool=_spool(), info=PrintFileInfo(md5=MD5[1], grams=14.0, minutes=100.0, filament_types=("PETG",)),
        expected_sha256=SHA, file_sha256=SHA, nozzle="0.4", fresh=True,
    )
    args.update(changes)
    return preflight(**args)


def _only_blocking(result: Preflight, *words: str) -> None:
    assert len(result.blocking) == 1, result.blocking
    for word in words:
        assert word in result.blocking[0], result.blocking


def test_all_good_has_no_lines():
    assert _check() == Preflight(blocking=[], warnings=[])


@pytest.mark.parametrize("changes", [{"fresh": False}, {"state": _state(gcode_state="UNKNOWN")}])
def test_not_connected_or_no_fresh_report_blocks(changes):
    # What the printer last said is stale: only this line, not one per stale field.
    stale = {"state": _state(gcode_state="UNKNOWN", sdcard=None, hms=(Hms(0x0300_0100, 0x0001_0001),))}
    _only_blocking(_check(**{**stale, **changes}), "на связи")


@pytest.mark.parametrize("gcode_state", ["IDLE", "FINISH", "FAILED"])
def test_idle_finished_or_failed_printer_may_start(gcode_state):
    assert _check(state=_state(gcode_state=gcode_state)).blocking == []


@pytest.mark.parametrize("gcode_state", ["RUNNING", "PAUSE", "PREPARE", "SLICING", "INIT", "OFFLINE"])
def test_busy_printer_blocks(gcode_state):
    _only_blocking(_check(state=_state(gcode_state=gcode_state)), "занят", gcode_state)


@pytest.mark.parametrize(("level", "blocks"), [(1, True), (2, True), (3, False), (4, False), (9, False)])
def test_fatal_or_serious_hms_blocks(level, blocks):
    hms = (Hms(0x0300_0100, (level << 16) | 0x0002),)
    result = _check(state=_state(hms=hms))
    if blocks:
        _only_blocking(result, "HMS", "0300")
    else:
        assert result.blocking == []


def test_no_usb_storage_blocks():
    _only_blocking(_check(state=_state(sdcard=False)), "нет флешки")


def test_unknown_usb_storage_blocks_until_a_report():
    _only_blocking(_check(state=_state(sdcard=None)), "Нет данных о флешке", "подожди отчёт принтера")


@pytest.mark.parametrize(("diameter", "blocks"), [(0.6, True), (0.2, True), (0.4, False), (0.4000001, False)])
def test_nozzle_mismatch_blocks(diameter, blocks):
    result = _check(state=_state(nozzle_diameter=diameter))
    if blocks:
        _only_blocking(result, "опло", "0.4")
    else:
        assert result.blocking == []


def test_unknown_nozzle_blocks():
    _only_blocking(_check(state=_state(nozzle_diameter=None)), "сопл")


def test_no_slot_chosen_blocks():
    _only_blocking(_check(slot_choice=SlotChoice(None, None, None, None), spool=None), "лот")


@pytest.mark.parametrize("trays", [(), (Tray(0, 2, None, None, None),), (Tray(0, 2, "", None, None),)])
def test_empty_slot_blocks(trays):
    _only_blocking(_check(state=_state(trays=trays)), "Слот 3", "пуст")


def test_unmapped_slot_blocks():
    _only_blocking(_check(spool=None), "слоту 3", "катушк")


def test_slot_material_differs_from_the_sliced_preset_blocks():
    state = _state(trays=(Tray(0, 2, "PLA", "161616FF", 80),))
    assert _check(state=state).blocking == ["В слоте 3 PLA, а файл нарезан под PETG"]


def test_slot_material_match_ignores_case():
    assert _check(state=_state(trays=(Tray(0, 2, "petg", "161616FF", 80),))).blocking == []


def test_more_than_one_filament_on_the_plate_blocks():
    # Two filament ids of one material still need two AMS slots: project_file maps one.
    info = PrintFileInfo(md5=MD5[1], grams=14.0, minutes=100.0, filament_types=("PETG",), filament_count=2)
    _only_blocking(_check(info=info), "2", "филамент", "одного слота")


def test_inactive_spool_in_the_slot_blocks():
    _only_blocking(_check(spool=_spool(active=False)), "Катушка 1", "слоте 3", "списана")


@pytest.mark.parametrize(("level", "word"), [(1, "критическая"), (2, "серьёзная")])
def test_hms_line_names_the_severity_in_russian(level, word):
    hms = (Hms(0x0300_0100, (level << 16) | 0x0002),)
    (line,) = _check(state=_state(hms=hms)).blocking
    assert word in line and "fatal" not in line and "serious" not in line


def test_more_than_one_material_on_the_plate_blocks():
    info = PrintFileInfo(md5=MD5[1], grams=14.0, minutes=100.0, filament_types=("PETG", "PLA"))
    _only_blocking(_check(info=info), "PETG", "PLA")


@pytest.mark.parametrize(("remaining", "blocks"), [(16.0, True), (0.0, True), (16.1, False), (500.0, False)])
def test_spool_below_grams_times_1_15_blocks(remaining, blocks):
    result = _check(spool=_spool(remaining_g=remaining))  # 14 g x 1.15 = 16.1 g
    if blocks:
        _only_blocking(result, "катушке", "17 г")
    else:
        assert result.blocking == []


@pytest.mark.parametrize("file_sha", [None, ""])
def test_missing_sliced_file_blocks(file_sha):
    _only_blocking(_check(file_sha256=file_sha), "файла нет")


def test_changed_sliced_file_blocks():
    _only_blocking(_check(file_sha256="e" * 64), "изменился", "нарежь заново")


@pytest.mark.parametrize("expected", [None, ""])
def test_job_sliced_before_the_check_asks_for_a_reslice(expected):
    _only_blocking(_check(expected_sha256=expected), "нарежь заново")


def test_slot_colour_differs_from_slicing_warns():
    state = _state(trays=(Tray(0, 2, "PETG", "FFFFFFFF", 80),))
    result = _check(state=state, spool=_spool(color_name="белый", color_hex="#FFFFFF"))
    assert result.blocking == []
    assert len(result.warnings) == 1 and "Цвет" in result.warnings[0] and "161616" in result.warnings[0]


def test_slot_colour_matches_by_spool_name_or_tray_hex():
    assert _check(job=_job(color="чёрный")).warnings == []
    assert _check(job=_job(color="161616")).warnings == []
    assert _check(job=_job(color=None)).warnings == []  # nothing recorded: nothing to compare


def test_material_intent_differs_from_the_slot_warns():
    # The part asks for PETG near a lamp, the owner chose a PLA slot and sliced for PLA.
    state = _state(trays=(Tray(0, 2, "PLA", "161616FF", 80),))
    info = PrintFileInfo(md5=MD5[1], grams=14.0, minutes=100.0, filament_types=("PLA",))
    result = _check(state=state, info=info, spool=_spool(material="PLA"))
    assert result.blocking == []
    assert len(result.warnings) == 1
    assert "PETG" in result.warnings[0] and "near a lamp" in result.warnings[0] and "PLA" in result.warnings[0]


def test_hint_conflicts_on_the_plate_warn():
    kit = _job(manifest="kit.print.json")
    state = _state(trays=(Tray(0, 2, "PLA", "161616FF", 80),))
    info = PrintFileInfo(md5=MD5[2], grams=18.0, minutes=60.0, filament_types=("PLA",))
    args = dict(job=kit, state=state, info=info, spool=_spool(material="PLA"))
    two = _check(plate=2, **args)
    assert two.blocking == [] and len(two.warnings) == 1 and "стенки" in two.warnings[0]
    assert _check(plate=1, **args).warnings == []  # plate 1 has no conflicts


def test_lines_follow_the_constraint_order_and_style():
    state = _state(gcode_state="RUNNING", hms=(Hms(0x0300_0100, 0x0001_0001),), sdcard=False, nozzle_diameter=0.6,
                   trays=(Tray(0, 2, "PLA", "FFFFFFFF", 80),))
    result = _check(state=state, spool=_spool(remaining_g=1.0, color_name="белый", color_hex="#FFFFFF"),
                    file_sha256="e" * 64)
    words = ["занят", "HMS", "флешк", "опло", "PLA", "катушке", "изменился"]
    assert len(result.blocking) == len(words), result.blocking
    for line, word in zip(result.blocking, words, strict=True):
        assert word in line, (word, line)
    assert len(result.warnings) == 2
    assert "Цвет" in result.warnings[0] and "near a lamp" in result.warnings[1]
    for line in result.blocking + result.warnings:
        assert "—" not in line and ";" not in line


def test_sd_storage_wording():
    from bambu_profiles import load_profile

    result = _check(state=_state(sdcard=False), profile=load_profile("a1"))
    _only_blocking(result, "microSD")
    assert "флешк" not in result.blocking[0]


def test_usb_storage_wording_default():
    _only_blocking(_check(state=_state(sdcard=False)), "флешк")


def test_start_states_from_profile():
    _only_blocking(_check(state=_state(gcode_state="RUNNING")), "IDLE, FINISH или FAILED")


def test_wrong_printer_model_blocks():
    from bambu_profiles import load_profile

    info = PrintFileInfo(md5=MD5[1], grams=14.0, minutes=100.0, filament_types=("PETG",),
                         printer_model="Bambu Lab P2S", bed_xy=(256, 256))
    a1 = load_profile("a1")
    result = _check(info=info, profile=a1, state=_state(trays=(Tray(255, 254, "PETG", None, None),)),
                    slot_choice=SlotChoice(255, 254, 1, None, tray_type="PETG", color_name="чёрный", remaining_g=500.0))
    assert any("Bambu Lab P2S" in line and "Bambu Lab A1" in line for line in result.blocking), result.blocking
    assert _check(info=info).blocking == []  # the P2S profile accepts it


def test_wrong_bed_blocks():
    info = PrintFileInfo(md5=MD5[1], grams=14.0, minutes=100.0, filament_types=("PETG",),
                         printer_model="Bambu Lab P2S", bed_xy=(180, 180))
    _only_blocking(_check(info=info), "180x180")


def test_external_spool_lines_name_it():
    from bambu_profiles import load_profile

    a1 = load_profile("a1")
    ext_choice = SlotChoice(255, 254, 1, None, tray_type="PLA", color_name="белый", remaining_g=500.0)
    info = PrintFileInfo(md5=MD5[1], grams=14.0, minutes=100.0, filament_types=("PETG",))
    cases = [
        _check(profile=a1, slot_choice=ext_choice, info=info, state=_state(trays=(Tray(255, 254, None, None, None),))),
        _check(profile=a1, slot_choice=ext_choice, info=info, spool=None,
               state=_state(trays=(Tray(255, 254, "PLA", None, None),))),
        _check(profile=a1, slot_choice=ext_choice, info=info, spool=_spool(active=False),
               state=_state(trays=(Tray(255, 254, "PLA", None, None),))),
        _check(profile=a1, slot_choice=ext_choice, info=info, state=_state(trays=(Tray(255, 254, "PLA", None, None),))),
    ]
    lines = [line for c in cases for line in c.blocking + c.warnings]
    assert lines, "each case must produce a line"
    assert all("лот 0" not in line and "лоте 0" not in line and "лоту 0" not in line for line in lines), lines
    assert any("внешн" in line.lower() for line in lines)
