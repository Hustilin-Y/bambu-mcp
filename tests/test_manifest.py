import json
from pathlib import Path

import pytest

from bambu_mcp.manifest import ManifestError, load_manifest, verify_file

FIX = Path(__file__).parent / "fixtures" / "manifest"


def _mutated(tmp_path: Path, name: str, **changes) -> Path:
    data = json.loads((FIX / name).read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is KeyError:
            del data[key]
        else:
            data[key] = value
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_load_part_manifest():
    m = load_manifest(FIX / "part.print.json")
    assert (m.schema, m.kind, m.name, m.file) == (1, "part", "lamp-arm", "lamp-arm.3mf")
    assert m.print_ready is True
    assert m.material == "PETG" and m.nozzle_mm == 0.4
    assert m.estimate_g == 14.2
    assert m.checks["failed"] is False
    assert m.hints["walls"] == 4 and m.hints["infill_pattern"] == "gyroid"
    assert m.source["overrides"] == {"WIDTH": "45"}
    (plate,) = m.plates
    assert plate.index == 1 and plate.objects[0]["name"] == "lamp-arm"
    # a part plate without its own data inherits the manifest level values
    assert plate.hints == m.hints and plate.estimate_g == 14.2 and plate.checks == m.checks


def test_load_kit_manifest_plates():
    m = load_manifest(FIX / "kit.print.json")
    assert m.kind == "kit" and len(m.plates) == 2
    p1, p2 = m.plates
    assert [o["name"] for o in p1.objects] == ["clip#1", "clip#2"]
    assert p1.hints == {"walls": 3} and p1.estimate_g == 3.1
    assert p2.checks["islands"] == 2 and p2.checks["supports_suggested"] is True
    assert p2.hints == {} and p2.estimate_g == 18.0
    assert m.checks["supports_suggested"] is True


def test_rejects_schema_2(tmp_path):
    with pytest.raises(ManifestError, match="schema"):
        load_manifest(_mutated(tmp_path, "part.print.json", schema=2))


def test_rejects_not_print_ready(tmp_path):
    with pytest.raises(ManifestError, match="--print-ready"):
        load_manifest(_mutated(tmp_path, "part.print.json", print_ready=False))


def test_sha_mismatch_rejected():
    m = load_manifest(FIX / "part.print.json")
    verify_file(m, b"part-3mf-bytes")
    with pytest.raises(ManifestError, match="sha256"):
        verify_file(m, b"changed after the build")


def test_unknown_hint_key_rejected(tmp_path):
    with pytest.raises(ManifestError, match="wall"):
        load_manifest(_mutated(tmp_path, "part.print.json", hints={"wall": 3}))


def test_bad_hint_value_rejected(tmp_path):
    with pytest.raises(ManifestError, match="supports"):
        load_manifest(_mutated(tmp_path, "part.print.json", hints={"supports": "yes"}))


def test_missing_required_field_rejected(tmp_path):
    with pytest.raises(ManifestError, match="file_sha256"):
        load_manifest(_mutated(tmp_path, "part.print.json", file_sha256=KeyError))


def test_unreadable_or_non_json(tmp_path):
    bad = tmp_path / "x.print.json"
    bad.write_text("{nope", encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(bad)
    with pytest.raises(ManifestError):
        load_manifest(tmp_path / "missing.print.json")


def test_file_name_must_be_bare(tmp_path):
    with pytest.raises(ManifestError, match="file"):
        load_manifest(_mutated(tmp_path, "part.print.json", file="../evil.3mf"))


def _raw(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "raw.print.json"
    path.write_text(text, encoding="utf-8")
    return path


def _part(**changes) -> dict:
    data = json.loads((FIX / "part.print.json").read_text(encoding="utf-8"))
    data.update(changes)
    return data


def test_deeply_nested_json_is_manifest_error(tmp_path):
    with pytest.raises(ManifestError):
        load_manifest(_raw(tmp_path, "[" * 100000))


def test_huge_numbers_are_manifest_error(tmp_path):
    big = 10**400
    bad_profile = {"material": "PETG", "nozzle_mm": big}
    with pytest.raises(ManifestError):
        load_manifest(_raw(tmp_path, json.dumps(_part(profile=bad_profile))))
    with pytest.raises(ManifestError):
        load_manifest(_raw(tmp_path, json.dumps(_part(estimate={"grams": big}))))


def test_oversized_manifest_rejected(tmp_path):
    with pytest.raises(ManifestError, match="large"):
        load_manifest(_raw(tmp_path, json.dumps(_part(pad="x" * (1024 * 1024)))))


def test_name_and_file_length_caps(tmp_path):
    with pytest.raises(ManifestError, match="name"):
        load_manifest(_raw(tmp_path, json.dumps(_part(name="a" * 101))))
    with pytest.raises(ManifestError, match="file"):
        load_manifest(_raw(tmp_path, json.dumps(_part(file="a" * 101 + ".3mf"))))


def test_plate_and_object_count_caps(tmp_path):
    obj = {"name": "o", "bbox_mm": [1, 1, 1], "volume_mm3": 1.0}
    many_plates = [{"index": i + 1, "objects": [obj]} for i in range(51)]
    kit = json.loads((FIX / "kit.print.json").read_text(encoding="utf-8"))
    kit["plates"] = [dict(p, estimate={"grams": 1}) for p in many_plates]
    with pytest.raises(ManifestError, match="plates"):
        load_manifest(_raw(tmp_path, json.dumps(kit)))
    kit["plates"] = [{"index": 1, "objects": [obj] * 201, "estimate": {"grams": 1}}]
    with pytest.raises(ManifestError, match="objects"):
        load_manifest(_raw(tmp_path, json.dumps(kit)))


@pytest.mark.parametrize(
    "name", ["../../evil", "a/b", "a\b", "a\x00b", ".hidden", "", "caf\u00e9", "a\nb"]
)
def test_unsafe_name_rejected(tmp_path, name):
    with pytest.raises(ManifestError, match="name"):
        load_manifest(_raw(tmp_path, json.dumps(_part(name=name))))


@pytest.mark.parametrize("file", ["a\x00b.3mf", "a b/c.3mf", "x\n.3mf", ""])
def test_unsafe_file_rejected(tmp_path, file):
    with pytest.raises(ManifestError, match="file"):
        load_manifest(_raw(tmp_path, json.dumps(_part(file=file))))


def test_safe_name_with_space_and_dots_ok(tmp_path):
    m = load_manifest(_raw(tmp_path, json.dumps(_part(name="lamp arm v1.2_b-3"))))
    assert m.name == "lamp arm v1.2_b-3"


def test_kit_plate_hint_conflicts_kept():
    m = load_manifest(FIX / "kit.print.json")
    p1, p2 = m.plates
    assert p1.hint_conflicts == () and p2.hint_conflicts == ("walls",)


@pytest.mark.parametrize("conflicts", ["walls", ["colour"], [1], [["walls"]]])
def test_bad_hint_conflicts_rejected(tmp_path, conflicts):
    data = json.loads((FIX / "kit.print.json").read_text(encoding="utf-8"))
    data["plates"][1]["hint_conflicts"] = conflicts
    path = tmp_path / "kit.print.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ManifestError, match="hint_conflicts"):
        load_manifest(path)
