import json
from pathlib import Path

import pytest

from slice_agent.config import AgentConfig
from slice_agent.profiles import flatten_all, preset_roots, resolve_preset

BED = ["0x0", "256x0", "256x256", "0x256"]
BED_200 = ["0x0", "200x0", "200x200", "0x200"]
MACHINE = "Bambu Lab P2S 0.4 nozzle"
PROCESS = "0.20mm Standard @BBL P2S"
FILAMENT = "Generic PETG @BBL P2S"


def put(root: Path, kind: str, name: str, **keys) -> None:
    d = root / kind
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps({"name": name, **keys}), encoding="utf-8")


def build_tree(tmp_path: Path, bed=BED) -> Path:
    data = tmp_path / "BambuStudio"
    sysroot = data / "system" / "BBL"
    put(sysroot, "machine", "fdm_machine_common", type="machine", printable_area=bed, printable_height="256",
        nozzle_diameter=["0.4"], machine_start_gcode="generic", from_="x")
    put(sysroot, "machine", MACHINE, inherits="fdm_machine_common", type="machine", from_="system",
        machine_start_gcode="p2s", retraction_length="0.8")
    put(sysroot, "process", "fdm_process_common", type="process", layer_height="0.2", wall_loops="2",
        retraction_length="9", print_settings_id="")
    put(sysroot, "process", PROCESS, inherits="fdm_process_common", type="process", wall_loops="3")
    put(sysroot, "filament", "fdm_filament_pet", type="filament", filament_type=["PETG"],
        nozzle_temperature=["240"], retraction_length=["7"])
    put(sysroot, "filament", FILAMENT, inherits="fdm_filament_pet", type="filament", nozzle_temperature=["250"])
    return data


def make_cfg(tmp_path: Path, data: Path) -> AgentConfig:
    return AgentConfig(
        studio_exe=tmp_path / "bambu-studio.exe",
        studio_data=data,
        studio_resources=tmp_path / "resources",
        profiles_dir=tmp_path / "profiles",
        sliced_dir=tmp_path / "sliced",
        machine={"0.4": MACHINE},
        process_sets={"standard": PROCESS},
        filaments=[FILAMENT],
    )


def roots_of(data: Path) -> list[Path]:
    return [data / "system" / "BBL"]


def test_inherits_then_include_then_own(tmp_path):
    root = tmp_path / "r"
    put(root, "process", "base", a="base", b="base", c="base", type="process")
    put(root, "process", "tpl", b="tpl", c="tpl", type="template", setting_id="T")
    put(root, "process", "leaf", inherits="base", include=["tpl"], c="own")
    out = resolve_preset("process", "leaf", [root])
    assert out["a"] == "base"
    assert out["b"] == "tpl"
    assert out["c"] == "own"
    assert out["name"] == "leaf"
    assert "inherits" not in out and "include" not in out
    assert out.get("type") == "process"
    assert "setting_id" not in out


def test_include_template_keys_override_parent(tmp_path):
    root = tmp_path / "r"
    put(root, "machine", "parent", machine_start_gcode="generic", x="1")
    put(root, "machine", "tpl", machine_start_gcode="p2s")
    put(root, "machine", "leaf", inherits="parent", include=["tpl"])
    out = resolve_preset("machine", "leaf", [root])
    assert out["machine_start_gcode"] == "p2s"
    assert out["x"] == "1"


def test_cycle_raises(tmp_path):
    root = tmp_path / "r"
    put(root, "process", "a", inherits="b")
    put(root, "process", "b", inherits="a")
    with pytest.raises(ValueError, match="loop"):
        resolve_preset("process", "a", [root])


def test_user_preset_found_after_system(tmp_path):
    data = build_tree(tmp_path)
    put(data / "user" / "123", "process", "My process", inherits=PROCESS, wall_loops="5")
    put(data / "user" / "123", "process", PROCESS, wall_loops="99")  # shadowed by system
    roots = preset_roots(make_cfg(tmp_path, data))
    assert resolve_preset("process", "My process", roots)["wall_loops"] == "5"
    assert resolve_preset("process", PROCESS, roots)["wall_loops"] == "3"
    with pytest.raises(KeyError):
        resolve_preset("process", "missing", roots)


def test_settings_ids_and_inherits_set(tmp_path):
    cfg = make_cfg(tmp_path, build_tree(tmp_path))
    paths = flatten_all(cfg)
    names = {p.name for p in paths}
    assert names == {"machine-0.4.json", "process-standard.json", "filament-generic-petg-bbl-p2s.json"}
    load = lambda n: json.loads((cfg.profiles_dir / n).read_text(encoding="utf-8"))  # noqa: E731
    m, p, f = load("machine-0.4.json"), load("process-standard.json"), load("filament-generic-petg-bbl-p2s.json")
    assert (m["from"], p["from"], f["from"]) == ("User",) * 3
    assert m["inherits"] == MACHINE and p["inherits"] == PROCESS and f["inherits"] == FILAMENT
    assert m["printer_settings_id"] == MACHINE
    assert p["print_settings_id"] == PROCESS
    assert f["filament_settings_id"] == [FILAMENT]
    assert m["machine_start_gcode"] == "p2s"
    assert f["nozzle_temperature"] == ["250"]


def test_machine_keys_removed_from_process_and_filament(tmp_path):
    cfg = make_cfg(tmp_path, build_tree(tmp_path))
    flatten_all(cfg)
    p = json.loads((cfg.profiles_dir / "process-standard.json").read_text(encoding="utf-8"))
    f = json.loads((cfg.profiles_dir / "filament-generic-petg-bbl-p2s.json").read_text(encoding="utf-8"))
    for d in (p, f):
        assert "retraction_length" not in d
        assert "printable_area" not in d
        assert d["name"] and d["type"]
    assert p["layer_height"] == "0.2"
    assert f["filament_type"] == ["PETG"]


def test_bed_assertion_fails_on_200mm_bed(tmp_path):
    cfg = make_cfg(tmp_path, build_tree(tmp_path, bed=BED_200))
    with pytest.raises(AssertionError, match="bed"):
        flatten_all(cfg)


def test_nozzle_mismatch_fails(tmp_path):
    cfg = make_cfg(tmp_path, build_tree(tmp_path))
    bad = AgentConfig(**{**cfg.__dict__, "machine": {"0.6": MACHINE}})
    with pytest.raises(AssertionError, match="nozzle"):
        flatten_all(bad)
