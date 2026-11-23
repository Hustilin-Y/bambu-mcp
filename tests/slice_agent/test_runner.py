import json
import subprocess
from pathlib import Path

import pytest

from slice_agent.config import AgentConfig
from slice_agent.runner import Overrides, SliceRun, build_command, run_slice, slice_isolated


def make_cfg(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        studio_exe=Path("C:/BS/bambu-studio.exe"),
        studio_data=tmp_path / "data",
        studio_resources=tmp_path / "res",
        profiles_dir=tmp_path / "profiles",
        sliced_dir=tmp_path / "sliced",
    )


def cmd(cfg, **kw):
    args = dict(
        input_3mf=Path("C:/in/part.3mf"), out_name="part-j1-p1", nozzle="0.4", process_set="standard",
        filament="Generic PETG @BBL P2S", plate_type="Textured PEI Plate", kit=False,
        overrides=Overrides.parse({}, "0.4"),
    )
    args.update(kw)
    return build_command(cfg, **args)


def test_overrides_whitelist_and_ranges():
    for raw in (
        {"nope": 1},
        {"sparse_infill_density": 101},
        {"sparse_infill_density": -1},
        {"layer_height": 0.3},
        {"wall_loops": 11},
        {"wall_loops": 0},
        {"wall_generator": "x"},
        {"enable_support": "yes"},
        {"curr_bed_type": "Glass"},
        {"sparse_infill_pattern": "spiral"},
        {"brim_type": "x"},
        {"support_type": "x"},
    ):
        with pytest.raises(ValueError, match=next(iter(raw))):
            Overrides.parse(raw, "0.4")
    with pytest.raises(ValueError, match="layer_height"):
        Overrides.parse({"layer_height": 0.2}, "0.6")  # no table for this nozzle


def test_overrides_values():
    o = Overrides.parse(
        {"sparse_infill_density": 15, "layer_height": 0.16, "enable_support": True, "wall_loops": 3,
         "brim_type": "auto_brim"}, "0.4")
    assert sorted(o.args()) == sorted([
        "--sparse-infill-density=15%", "--layer-height=0.16", "--enable-support=1", "--wall-loops=3",
        "--brim-type=auto_brim",
    ])
    assert Overrides.parse({"enable_support": False}, "0.4").args() == ["--enable-support=0"]


def test_build_command_matches_recipe(tmp_path):
    cfg = make_cfg(tmp_path)
    prof = cfg.profiles_dir
    assert cmd(cfg) == [
        str(cfg.studio_exe), "--slice", "0", "--arrange", "1", "--orient", "0",
        "--curr-bed-type", "Textured PEI Plate",
        "--load-settings", f"{prof / 'machine-0.4.json'};{prof / 'process-standard.json'}",
        "--load-filaments", str(prof / "filament-generic-petg-bbl-p2s.json"),
        "--export-3mf", "part-j1-p1.gcode.3mf", "--outputdir", str(cfg.sliced_dir), str(Path("C:/in/part.3mf")),
    ]
    kit = cmd(cfg, kit=True, out_name="k-j1-kit", overrides=Overrides.parse({"wall_loops": 4}, "0.4"))
    assert kit[kit.index("--arrange") + 1] == "0"
    assert kit.index("--wall-loops=4") < kit.index("--export-3mf")
    assert kit[-1] == str(Path("C:/in/part.3mf"))


@pytest.mark.parametrize(
    "bad", ["CON", "nul", "Com1", "lpt9.txt", "aux.gcode", "name.", "name ", "a/b", "a\\b", "", "..", "a b;c"]
)
def test_build_command_refuses_unsafe_out_name(tmp_path, bad):
    with pytest.raises(ValueError, match="out_name"):
        cmd(make_cfg(tmp_path), out_name=bad)


def test_build_command_refuses_unknown_names(tmp_path):
    cfg = make_cfg(tmp_path)
    for kw in ({"nozzle": "0.8"}, {"process_set": "../x"}, {"filament": "Other"}, {"plate_type": "Glass"}):
        with pytest.raises(ValueError):
            cmd(cfg, **kw)


class Done:
    def __init__(self, rc=0, stdout=b"", stderr=b""):
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr


def fake_runner(out_dir: Path, result: dict | None, rc=0):
    calls = {}

    def run(argv, **kw):
        calls["argv"], calls["kw"] = argv, kw
        if result is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "result.json").write_text(json.dumps(result))
        return Done(rc, stdout=b"a\nb\nlast line\n")

    return run, calls


def test_run_slice_ok(tmp_path):
    out = tmp_path / "o"
    run, calls = fake_runner(out, {"return_code": 0, "error_string": "success"})
    r = run_slice(["x"], out, runner=run)
    assert isinstance(r, SliceRun) and r.return_code == 0 and r.error_string == "success"
    assert not r.timed_out and "last line" in r.log_tail
    kw = calls["kw"]
    assert kw["timeout"] == 300 and not kw.get("shell")


def test_nonzero_return_reads_error_string(tmp_path):
    out = tmp_path / "o"
    run, _ = fake_runner(out, {"return_code": -50, "error_string": "object outside the plate"})
    r = run_slice(["x"], out, runner=run)
    assert r.return_code == -50 and "outside" in r.error_string


def test_stale_result_json_is_ignored(tmp_path):
    out = tmp_path / "o"
    out.mkdir()
    (out / "result.json").write_text(json.dumps({"return_code": 0, "error_string": "old"}))
    run, _ = fake_runner(out, None, rc=-17)
    r = run_slice(["x"], out, runner=run)
    assert r.return_code == -17 and r.error_string == ""


def test_run_slice_timeout_kills_and_reports(tmp_path):
    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"], output=b"line1\nline2\n")

    r = run_slice(["x"], tmp_path / "o", timeout_s=7, runner=run)
    assert r.timed_out is True and r.return_code != 0
    assert "timed out after 7" in r.error_string
    assert "line2" in r.log_tail


def iso_kwargs():
    return dict(
        input_3mf=Path("C:/in/part.3mf"), nozzle="0.4", process_set="standard",
        filament="Generic PETG @BBL P2S", plate_type="Textured PEI Plate", kit=False,
        overrides=Overrides.parse({}, "0.4"),
    )


def isolated_runner(write_output=True, result=None, raise_timeout=False):
    def run(argv, **kw):
        out = Path(argv[argv.index("--outputdir") + 1])
        assert out.name == ".work-part-j1-p1" and argv[argv.index("--export-3mf") + 1] == "part-j1-p1.gcode.3mf"
        (out / "plate_1.gcode").write_text("G1")
        (out / "result.json").write_text(json.dumps(result or {"return_code": 0, "error_string": "success"}))
        if write_output:
            (out / "part-j1-p1.gcode.3mf").write_bytes(b"zip")
        if raise_timeout:
            raise subprocess.TimeoutExpired(argv, 1)
        return Done(0)

    return run


def test_slice_isolated_success_leaves_only_output(tmp_path):
    cfg = make_cfg(tmp_path)
    run, final = slice_isolated(cfg, out_name="part-j1-p1", runner=isolated_runner(), **iso_kwargs())
    assert run.return_code == 0 and final == cfg.sliced_dir / "part-j1-p1.gcode.3mf"
    assert [p.name for p in cfg.sliced_dir.iterdir()] == ["part-j1-p1.gcode.3mf"]


def test_slice_isolated_failure_and_timeout_clean_up(tmp_path):
    cfg = make_cfg(tmp_path)
    bad = {"return_code": -50, "error_string": "outside"}
    run, final = slice_isolated(cfg, out_name="part-j1-p1", runner=isolated_runner(False, bad), **iso_kwargs())
    assert final is None and run.error_string == "outside" and list(cfg.sliced_dir.iterdir()) == []
    run, final = slice_isolated(cfg, out_name="part-j1-p1", runner=isolated_runner(raise_timeout=True), **iso_kwargs())
    assert final is None and run.timed_out and list(cfg.sliced_dir.iterdir()) == []
