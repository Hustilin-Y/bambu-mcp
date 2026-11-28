import pytest

from bambu_profiles import PROFILE_NAMES, _parse, load_profile

P2S_PLATES = ("Textured PEI Plate", "Cool Plate", "Engineering Plate", "High Temp Plate", "Smooth PEI Plate")


def test_profile_names():
    assert PROFILE_NAMES == ("a1", "p2s")


def test_p2s_matches_current_constants():
    p = load_profile("p2s")
    assert p.name == "p2s"
    assert p.model_name == "Bambu Lab P2S"
    assert p.bed_mm == (256, 256, 256)
    assert p.bed_area == "256x256"
    assert p.bed_height == "256"
    assert p.hms_prefix == "22E"
    assert p.hms_file == "hms_en_22E.json"
    assert p.camera == "rtsps_322"
    assert p.storage == "usb"
    assert (p.ams_units, p.ams_slots) == (4, 4)
    assert p.has_ams is True
    assert p.max_slot_number == 16
    assert p.external_spool is False  # on for the P2S after its own live check (spec 5.8)
    assert p.machine_presets == {"0.4": "Bambu Lab P2S 0.4 nozzle"}
    assert p.process_sets == {"standard": "0.20mm Standard @BBL P2S"}
    assert p.filaments == ("Generic PETG @BBL P2S",)
    assert p.plate_types == P2S_PLATES
    assert p.layer_heights == {"0.4": (0.12, 0.16, 0.20, 0.24, 0.28)}
    assert p.nozzle_temp_range == (150, 320)
    assert p.power_w == {"PLA": 200.0}
    assert p.heatup_kwh == 0.05
    assert p.print_flags == {
        "timelapse": False,
        "bed_type": "auto",
        "bed_leveling": True,
        "bed_levelling": True,
        "auto_bed_leveling": 2,
        "flow_cali": False,
        "vibration_cali": False,
        "layer_inspect": True,
    }
    assert p.idle_states == frozenset({"IDLE", "FINISH", "FAILED"})
    assert p.stoppable_states == frozenset({"PREPARE", "RUNNING", "PAUSE"})
    assert p.unverified == frozenset()


def test_a1_profile():
    p = load_profile("a1")
    assert p.model_name == "Bambu Lab A1"
    assert p.has_ams is False
    assert p.max_slot_number == 0
    assert p.external_spool is True
    assert p.camera == "jpeg_6000"
    assert p.storage == "sd"
    assert p.nozzle_temp_range == (150, 300)
    assert p.print_flags["layer_inspect"] is False
    assert p.machine_presets == {"0.4": "Bambu Lab A1 0.4 nozzle"}
    assert "camera" in p.unverified
    assert "hms_prefix" in p.unverified
    assert {"name", "model_name", "bed_mm"}.isdisjoint(p.unverified)


def test_default_is_p2s():
    assert load_profile().name == "p2s"


def test_unknown_profile_names_known():
    with pytest.raises(ValueError, match="a1, p2s"):
        load_profile("x1c")


def _p2s_text():
    from importlib.resources import files

    return files("bambu_profiles").joinpath("p2s.toml").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "edit, match",
    [
        (lambda t: 'bogus = 1\n' + t, "unknown"),
        (lambda t: t.replace('PLA = 200.0', 'PETG = 220.0'), "PLA"),
        (lambda t: t.replace('unverified = []', 'unverified = ["nope"]'), "nope"),
        (lambda t: t.replace('layer_inspect = true', 'layer_inspect = true\nextra_flag = 1'), "print_flags"),
        (lambda t: t.replace('camera = "rtsps_322"', 'camera = "hdmi"'), "camera"),
        (lambda t: t.replace('ams_units = 4', 'ams_units = "4"'), "ams_units"),
    ],
)
def test_bad_profile_rejected(edit, match):
    text = _p2s_text()
    edited = edit(text)
    assert edited != text
    with pytest.raises(ValueError, match=match):
        _parse(edited, "p2s")


def test_hms_file_none_without_prefix():
    text = _p2s_text().replace('hms_prefix = "22E"', 'hms_prefix = ""')
    assert _parse(text, "p2s").hms_file is None
