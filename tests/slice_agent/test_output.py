import json
import zipfile
from pathlib import Path

import pytest

from slice_agent.output import OutputError, check_output, read_output

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
