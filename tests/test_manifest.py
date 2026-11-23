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
