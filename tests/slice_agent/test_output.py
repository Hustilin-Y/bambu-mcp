import io
import json
import math
import zipfile
from pathlib import Path

import pytest

from slice_agent import output
from slice_agent.output import FEATURES, OutputError, check_output, read_output

PNG = b"\x89PNG\r\n\x1a\nfake"
MB = 1024 * 1024


def slice_info(plates=((1, 2315, "5.31"),), nozzle="0.4") -> str:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>", '  <header><header_item key="X" value="y"/></header>']
    for idx, secs, weight in plates:
        parts += [
            "  <plate>",
            f'    <metadata key="index" value="{idx}"/>',
            '    <metadata key="printer_model_id" value=""/>',
            f'    <metadata key="nozzle_diameters" value="{nozzle}"/>',
            f'    <metadata key="prediction" value="{secs}"/>',
            f'    <metadata key="weight" value="{weight}"/>',
            f'    <filament id="1" type="PETG" color="#00AE42" used_m="1.7" used_g="{weight}"/>',
            "  </plate>",
        ]
    parts.append("</config>")
    return "\n".join(parts)


def make_3mf(
    path: Path,
    *,
    plates=((1, 2315, "5.31"),),
    model="Bambu Lab P2S",
    area=("0x0", "256x0", "256x256", "0x256"),
    nozzle="0.4",
    gcode=True,
) -> Path:
    settings = {"printable_area": list(area), "printer_model": model, "nozzle_diameter": [nozzle]}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/slice_info.config", slice_info(plates, nozzle))
        z.writestr("Metadata/project_settings.config", json.dumps(settings))
        for idx, _, _ in plates:
            if gcode:
                z.writestr(f"Metadata/plate_{idx}.gcode", "G1 X0\n")
            z.writestr(f"Metadata/plate_{idx}.png", PNG)
    return path


def test_read_output_time_and_grams(tmp_path):
    res = read_output(make_3mf(tmp_path / "a.gcode.3mf"), plate=1)
    assert res.printer_model == "Bambu Lab P2S"
    assert res.nozzle == "0.4"
    assert res.bed_xy == (256, 256)
    (p,) = res.plates
    assert p.index == 1
    assert p.minutes == pytest.approx(2315 / 60)
    assert p.grams_by_filament == {"1": pytest.approx(5.31)}
    assert p.png == PNG
    assert p.filament_types == ("PETG",)
    check_output(res, "0.4")  # the CLI leaves printer_model_id empty: must not be rejected


def test_read_output_kit_plates(tmp_path):
    path = make_3mf(tmp_path / "k.gcode.3mf", plates=((1, 600, "2.0"), (2, 1200, "4.5")))
    res = read_output(path, plate=2)
    assert [p.index for p in res.plates] == [1, 2]
    assert res.plates[1].minutes == pytest.approx(20)
    check_output(res, "0.4")


def test_check_output_rejects_200mm_bed(tmp_path):
    res = read_output(make_3mf(tmp_path / "a.gcode.3mf", area=("0x0", "200x0", "200x200", "0x200")), plate=1)
    with pytest.raises(OutputError, match="bed"):
        check_output(res, "0.4")


def test_check_output_rejects_wrong_nozzle(tmp_path):
    res = read_output(make_3mf(tmp_path / "a.gcode.3mf", nozzle="0.6"), plate=1)
    with pytest.raises(OutputError, match="nozzle"):
        check_output(res, "0.4")


def test_check_output_rejects_wrong_model(tmp_path):
    res = read_output(make_3mf(tmp_path / "a.gcode.3mf", model="Bambu Lab X1C"), plate=1)
    with pytest.raises(OutputError, match="printer_model"):
        check_output(res, "0.4")


def test_check_output_rejects_missing_gcode(tmp_path):
    res = read_output(make_3mf(tmp_path / "a.gcode.3mf", gcode=False), plate=1)
    with pytest.raises(OutputError, match="plate_1.gcode"):
        check_output(res, "0.4")


def test_zip_member_over_50mb_refused(tmp_path):
    path = tmp_path / "b.gcode.3mf"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Metadata/slice_info.config", slice_info())
        z.writestr("Metadata/project_settings.config", "{}")
        z.writestr("Metadata/plate_1.png", b"\0" * (51 * MB))
    with pytest.raises(OutputError, match="too large"):
        read_output(path, plate=1)


def test_oversized_xml_refused(tmp_path):
    path = tmp_path / "x.gcode.3mf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/slice_info.config", "<config>" + " " * (MB + 1) + "</config>")
        z.writestr("Metadata/project_settings.config", "{}")
    with pytest.raises(OutputError, match="slice_info"):
        read_output(path, plate=1)


def test_bad_zip_and_missing_members(tmp_path):
    p = tmp_path / "n.gcode.3mf"
    p.write_bytes(b"not a zip")
    with pytest.raises(OutputError):
        read_output(p, plate=1)
    q = tmp_path / "m.gcode.3mf"
    with zipfile.ZipFile(q, "w") as z:
        z.writestr("x", "y")
    with pytest.raises(OutputError, match="slice_info"):
        read_output(q, plate=1)


def _with_settings(tmp_path, settings_text):
    path = tmp_path / "u.gcode.3mf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/slice_info.config", slice_info())
        z.writestr("Metadata/project_settings.config", settings_text)
        z.writestr("Metadata/plate_1.gcode", "G1")
    return path


@pytest.mark.parametrize(
    "settings",
    [
        "[1, 2]",
        "42",
        '{"printable_area": ["0x0", "infx0"], "nozzle_diameter": ["0.4"]}',
        '{"printable_area": ["0x0", "nanx5"], "nozzle_diameter": ["0.4"]}',
        '{"printable_area": "0x0", "nozzle_diameter": ["0.4"]}',
        '{"printable_area": [], "nozzle_diameter": ["0.4"]}',
        '{"printable_area": ["0x0", "256x256"], "nozzle_diameter": "0.4"}',
        '{"printable_area": ["0x0", "256x256"], "nozzle_diameter": []}',
        '{"printable_area": ["0x0", "256x256"]}',
    ],
)
def test_untrusted_project_settings_raise_output_error(tmp_path, settings):
    with pytest.raises(OutputError):
        read_output(_with_settings(tmp_path, settings), plate=1)


def test_non_finite_prediction_refused(tmp_path):
    path = tmp_path / "p.gcode.3mf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/slice_info.config", slice_info(plates=((1, "inf", "1.0"),)))
        z.writestr("Metadata/project_settings.config", "{}")
    with pytest.raises(OutputError):
        read_output(path, plate=1)


def test_deeply_nested_project_settings_refused(tmp_path):
    with pytest.raises(OutputError):
        read_output(_with_settings(tmp_path, "[" * 100000 + "]" * 100000), plate=1)


@pytest.mark.parametrize("grams", ["nan", "inf", "-inf", "-1"])
def test_non_finite_or_negative_used_g_refused(tmp_path, grams):
    path = make_3mf(tmp_path / "g.gcode.3mf", plates=((1, 600, grams),))
    with pytest.raises(OutputError):
        read_output(path, plate=1)


def test_slicer_version_from_header(tmp_path):
    assert read_output(make_3mf(tmp_path / "a.gcode.3mf"), plate=1).slicer_version == ""
    path = tmp_path / "v.gcode.3mf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "Metadata/slice_info.config",
            slice_info().replace('key="X" value="y"', 'key="X-BBL-Client-Version" value="02.08.02.61"'),
        )
        z.writestr("Metadata/project_settings.config", json.dumps({"nozzle_diameter": ["0.4"], "printable_area": ["0x0"]}))
        z.writestr("Metadata/plate_1.gcode", "G1")
    assert read_output(path, plate=1).slicer_version == "02.08.02.61"


# --- feature volumes (evaluation, Task 4) ---------------------------------

AREA = math.pi * (1.75 / 2) ** 2  # mm² of 1.75 mm filament
ZERO = dict.fromkeys(FEATURES, 0.0)


def make_gcode_3mf(path: Path, gcode: str, *, diameter="1.75", layer_height="0.2", plates=(1,)) -> Path:
    settings = {"printable_area": ["0x0", "256x0", "256x256", "0x256"], "printer_model": "Bambu Lab P2S",
                "nozzle_diameter": ["0.4"]}
    if diameter is not None:
        settings["filament_diameter"] = [diameter]
    if layer_height is not None:
        settings["layer_height"] = layer_height
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Metadata/slice_info.config", slice_info(tuple((i, 600, "1.0") for i in plates)))
        z.writestr("Metadata/project_settings.config", json.dumps(settings))
        for i in plates:
            z.writestr(f"Metadata/plate_{i}.gcode", gcode)
    return path


def volumes(**mm_of_filament) -> dict:
    return {**ZERO, **{k: pytest.approx(v * AREA) for k, v in mm_of_filament.items()}}


SMALL_GCODE = """\
; HEADER_BLOCK_START
M83 ; relative extrusion
G1 X5 Y5 E2 ; purge before any feature marker counts as other
;TYPE:Outer wall
G1 X10 Y0 E1.0
G1 X20 Y0 E2.0 ; trailing comment
G1 E-0.8
G1 E0.8
G0 X30 Y30
; FEATURE: Inner wall
G1 X0 Y10 E3
;TYPE:Sparse infill
G2 X5 Y5 I1 J1 E0.5
G1 X1 Y1 E-0.1
;TYPE:Internal solid infill
G3 X6 Y6 I1 J1 E0.75
;TYPE:Top surface
G1 X1 E0.2
;TYPE:Bottom surface
G1 Y1 E0.3
;TYPE:Support interface
G1 X2 E0.25
;TYPE:Support
G1 X3 E0.25
;TYPE:Bridge
G1 X3 E0.1
"""


def test_feature_volumes_from_small_gcode(tmp_path):
    path = make_gcode_3mf(tmp_path / "f.gcode.3mf", SMALL_GCODE)
    res = read_output(path, plate=1, feature_plates=(1,))
    (p,) = res.plates
    assert p.features == volumes(
        outer_wall=3.0, inner_wall=3.0, sparse_infill=0.5, internal_solid_infill=0.75,
        top_surface=0.2, bottom_surface=0.3, support=0.5, other=2.1,
    )
    assert set(p.features) == set(FEATURES)
    assert p.layer_height == pytest.approx(0.2)


def test_features_only_for_the_asked_plates(tmp_path):
    path = make_gcode_3mf(tmp_path / "k.gcode.3mf", SMALL_GCODE, plates=(1, 2))
    res = read_output(path, plate=2, feature_plates=(2,))
    assert res.plates[0].features is None and res.plates[1].features is not None
    assert read_output(path, plate=1).plates[0].features is None  # not asked: not parsed


def test_relative_and_absolute_e(tmp_path):
    gcode = """\
M82
G92 E0
;TYPE:Outer wall
G1 X1 E1.0
G1 X2 E2.5
G1 E1.7
G1 E2.5
G1 X3 E3.0
G92 E0
;TYPE:Top surface
G1 X4 E0.4
M83
G1 X5 E0.6
M82
G92 E10
G1 X6 E10.5
g1 x7 e11.0
"""
    res = read_output(make_gcode_3mf(tmp_path / "e.gcode.3mf", gcode), plate=1, feature_plates=(1,))
    assert res.plates[0].features == volumes(outer_wall=3.0, top_surface=2.0)


def test_parse_stops_at_cap(tmp_path, monkeypatch):
    gcode = ";TYPE:Outer wall\nM83\n" + "G1 X1 Y1 E0.5\n" * 1000
    path = make_gcode_3mf(tmp_path / "c.gcode.3mf", gcode)
    monkeypatch.setattr(output, "MAX_GCODE_BYTES", 1000)
    res = read_output(path, plate=1, feature_plates=(1,))
    assert res.plates[0].features is None  # a partial count would mislead: unknown instead
    check_output(res, "0.4")  # the slice itself is still fine
    monkeypatch.setattr(output, "MAX_GCODE_BYTES", len(gcode))  # exactly at the cap is parsed
    assert read_output(path, plate=1, feature_plates=(1,)).plates[0].features == volumes(outer_wall=500.0)


@pytest.mark.parametrize(
    ("diameter", "layer_height"),
    [(None, "0.2"), ("nan", "0.2"), ("-1", "0.2"), ("abc", "0.2")],
)
def test_no_usable_filament_diameter_means_no_features(tmp_path, diameter, layer_height):
    path = make_gcode_3mf(tmp_path / "n.gcode.3mf", SMALL_GCODE, diameter=diameter, layer_height=layer_height)
    res = read_output(path, plate=1, feature_plates=(1,))
    assert res.plates[0].features is None
    check_output(res, "0.4")


@pytest.mark.parametrize("layer_height", [None, "abc", "inf", "0", 0.28])
def test_layer_height_is_optional(tmp_path, layer_height):
    path = make_gcode_3mf(tmp_path / "l.gcode.3mf", SMALL_GCODE, layer_height=layer_height)
    p = read_output(path, plate=1, feature_plates=(1,)).plates[0]
    assert p.features is not None
    assert p.layer_height == (pytest.approx(0.28) if layer_height == 0.28 else None)


@pytest.mark.parametrize(
    "gcode",
    [
        "M83\nG1 X1 Enan\n",
        "M83\nG1 X1 Einf\n",
        "M83\nG1 X1 Eabc\n",
        "M83\nG1 X1 E1e400\n",
        "\xff\xfe garbage \x00\nG1 X1 E1\n",
    ],
)
def test_garbage_extrusion_is_skipped_not_fatal(tmp_path, gcode):
    res = read_output(make_gcode_3mf(tmp_path / "g.gcode.3mf", gcode), plate=1, feature_plates=(1,))
    feats = res.plates[0].features
    assert feats is not None and all(math.isfinite(v) and v >= 0 for v in feats.values())


@pytest.mark.parametrize(
    "gcode",
    [
        "M83\nG1 X1 E1e308\nG1 X2 E1e308\n",
        "M82\nG92 E-1e308\nG1 X1 E1e308\n",
        "M83\nG1 X1 E1e308\n",
        "M83\n" + "G1 X1 E1e8\n" * 10,  # finite, but past any real print
    ],
)
def test_huge_extrusion_gives_no_features_and_a_good_slice(tmp_path, gcode):
    res = read_output(make_gcode_3mf(tmp_path / "h.gcode.3mf", gcode), plate=1, feature_plates=(1,))
    assert res.plates[0].features is None
    check_output(res, "0.4")


def test_streamed_byte_counter_stops_the_scan(monkeypatch):
    gcode = ("M83\n" + "G1 X1 Y1 E0.5\n" * 100).encode()
    monkeypatch.setattr(output, "MAX_GCODE_BYTES", len(gcode) - 1)  # no file_size check on this path
    assert output._extruded_lengths(io.BytesIO(gcode)) is None
    monkeypatch.setattr(output, "MAX_GCODE_BYTES", len(gcode))
    assert output._extruded_lengths(io.BytesIO(gcode))["other"] == pytest.approx(50.0)


def test_output_check_follows_profile(tmp_path):
    from bambu_profiles import load_profile

    res = read_output(make_3mf(tmp_path / "a.gcode.3mf"), plate=1)
    check_output(res, "0.4", load_profile("p2s"))
    with pytest.raises(OutputError, match="Bambu Lab A1"):
        check_output(res, "0.4", load_profile("a1"))
