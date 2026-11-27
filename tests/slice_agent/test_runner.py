import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from slice_agent.config import AgentConfig
from slice_agent import runner
from slice_agent.runner import Overrides, SliceRun, build_command, run_slice, slice_isolated, taskkill_exe


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


STUDIO_KEYS = Path(__file__).parent.parent / "fixtures" / "studio" / "project_settings_keys.json"


def test_whitelist_keys_and_values_match_studio_spelling():
    """Key names and the default values come from a real Studio 02.08 P2S project_settings.config."""
    studio = json.loads(STUDIO_KEYS.read_text(encoding="utf-8"))
    assert set(runner.OVERRIDE_KEYS) <= set(studio)
    assert studio["seam_position"] in runner.SEAM_POSITIONS
    assert studio["ironing_type"] in runner.IRONING_TYPES
    assert studio["wall_generator"] in runner.WALL_GENERATORS
    for key in ("top_shell_layers", "bottom_shell_layers"):
        Overrides.parse({key: int(studio[key])}, "0.4")


def test_new_overrides_parse_and_reject_out_of_range():
    ok = Overrides.parse(
        {"top_shell_layers": 0, "bottom_shell_layers": 20, "outer_wall_speed": 20, "seam_position": "back",
         "ironing_type": "topmost", "wall_generator": "classic"}, "0.4")
    assert ok.values["bottom_shell_layers"] == 20
    for value in (20, 60, 300, 45.5, 300.0):
        Overrides.parse({"outer_wall_speed": value}, "0.4")
    for seam in ("aligned", "back", "nearest", "random"):
        Overrides.parse({"seam_position": seam}, "0.4")
    for iron in ("no ironing", "top", "topmost"):
        Overrides.parse({"ironing_type": iron}, "0.4")
    for raw in (
        {"top_shell_layers": -1},
        {"top_shell_layers": 21},
        {"top_shell_layers": 5.0},
        {"top_shell_layers": True},
        {"bottom_shell_layers": 21},
        {"bottom_shell_layers": "5"},
        {"outer_wall_speed": 19.9},
        {"outer_wall_speed": 301},
        {"outer_wall_speed": float("nan")},
        {"outer_wall_speed": float("inf")},
        {"outer_wall_speed": True},
        {"outer_wall_speed": "60"},
        {"seam_position": "rear"},
        {"seam_position": "Back"},
        {"ironing_type": "solid"},
        {"ironing_type": "no_ironing"},
        {"wall_generator": "Arachne"},
    ):
        with pytest.raises(ValueError, match=next(iter(raw))):
            Overrides.parse(raw, "0.4")


def test_argv_for_new_keys():
    o = Overrides.parse(
        {"top_shell_layers": 6, "bottom_shell_layers": 4, "outer_wall_speed": 60, "seam_position": "back",
         "ironing_type": "no ironing", "wall_generator": "classic"}, "0.4")
    assert o.args() == [
        "--top-shell-layers=6", "--bottom-shell-layers=4", "--outer-wall-speed=60", "--seam-position=back",
        "--ironing-type=no ironing", "--wall-generator=classic",
    ]
    assert Overrides.parse({"outer_wall_speed": 45.5}, "0.4").args() == ["--outer-wall-speed=45.5"]
    argv = cmd(make_cfg(Path("C:/x")), overrides=Overrides.parse({"ironing_type": "top"}, "0.4"))
    assert "--ironing-type=top" in argv  # one argv item, no shell: the space in "no ironing" is safe


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


class FakePopen:
    """Popen stand-in: writes result.json and files into out_dir, log text to the stdout sink."""

    def __init__(self, out_dir: Path, result=None, rc=0, log=b"a\nb\nlast line\n", hang=False, files=()):
        self.out_dir, self.result, self.rc, self.log, self.hang, self.files = out_dir, result, rc, log, hang, files
        self.calls = {}
        self.killed = False
        self.pid = 1

    def __call__(self, argv, **kw):
        self.calls["argv"], self.calls["kw"] = argv, kw
        kw["stdout"].write(self.log)
        if self.result is not None:
            text = self.result if isinstance(self.result, str) else json.dumps(self.result)
            (self.out_dir / "result.json").write_text(text)
        for name in self.files:
            (self.out_dir / name).write_bytes(b"zip")
        self.returncode = self.rc
        return self

    def wait(self, timeout=None):
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("x", timeout)
        return self.rc

    def kill(self):
        self.killed = True


def mark_killed(proc):
    proc.killed = True


def test_run_slice_ok(tmp_path):
    out = tmp_path / "o"
    fake = FakePopen(out, {"return_code": 0, "error_string": "success"})
    out.mkdir()
    r = run_slice(["x"], out, popen=fake)
    assert isinstance(r, SliceRun) and r.return_code == 0 and r.error_string == "success"
    assert not r.timed_out and "last line" in r.log_tail
    kw = fake.calls["kw"]
    assert not kw["shell"] and kw["stdout"] is not subprocess.PIPE and kw["stdin"] is subprocess.DEVNULL
    assert kw.get("creationflags") or kw.get("start_new_session")


def test_nonzero_return_reads_error_string(tmp_path):
    out = tmp_path / "o"
    r = run_slice(["x"], out, popen=FakePopen(out, {"return_code": -50, "error_string": "object outside the plate"}))
    assert r.return_code == -50 and "outside" in r.error_string


def test_nonzero_exit_wins_over_result_json_zero(tmp_path):
    out = tmp_path / "o"
    r = run_slice(["x"], out, popen=FakePopen(out, {"return_code": 0, "error_string": "success"}, rc=3))
    assert r.return_code == 3 and r.error_string == "exit code 3"


def test_stale_result_json_is_ignored(tmp_path):
    out = tmp_path / "o"
    out.mkdir()
    (out / "result.json").write_text(json.dumps({"return_code": 0, "error_string": "old"}))
    r = run_slice(["x"], out, popen=FakePopen(out, None, rc=-17))
    assert r.return_code == -17 and r.error_string == "exit code -17"


@pytest.mark.parametrize(
    "bad",
    ["[1, 2]", "42", "not json", '{"return_code": "x"}', '{"return_code": 1e999}', "{}", "[" * 100000 + "]" * 100000],
)
def test_untrusted_result_json_never_raises(tmp_path, bad):
    out = tmp_path / "o"
    r = run_slice(["x"], out, popen=FakePopen(out, bad, rc=-5))
    assert r.return_code == -5


def test_run_slice_timeout_kills_tree_and_reports(tmp_path):
    out = tmp_path / "o"
    fake = FakePopen(out, None, hang=True)
    killed = []
    r = run_slice(["x"], out, timeout_s=7, popen=fake, killer=lambda p: (killed.append(p), mark_killed(p)))
    assert r.timed_out is True and r.return_code != 0
    assert "timed out after 7" in r.error_string and "last line" in r.log_tail
    assert killed == [fake]


CHILD = """
import os, subprocess, sys, time
hb = sys.argv[1]
grand = (
    "import sys, time\\n"
    "while True:\\n"
    "    open(sys.argv[1], 'a').write('x')\\n"
    "    time.sleep(0.1)\\n"
)
g = subprocess.Popen([sys.executable, '-I', '-c', grand, hb + '.grand'])
open(hb + '.pids', 'w').write(f"{os.getpid()} {g.pid}")
while True:
    open(hb + '.child', 'a').write('x')
    time.sleep(0.1)
"""


def _kill_pids(pid_file: Path) -> None:
    try:
        pids = [int(p) for p in pid_file.read_text().split()]
    except (OSError, ValueError):
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def test_run_slice_timeout_real_process_tree(tmp_path):
    """A child that spawns a sleeping grandchild: after the timeout both are gone, quickly."""
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    hb = tmp_path / "hb"
    out = tmp_path / "o"
    passed = False
    try:
        t0 = time.monotonic()
        r = run_slice([sys.executable, "-I", str(script), str(hb)], out, timeout_s=6)
        assert time.monotonic() - t0 < 20
        assert r.timed_out
        grand, child = tmp_path / "hb.grand", tmp_path / "hb.child"
        assert grand.exists() and child.exists()
        time.sleep(0.5)
        sizes = (grand.stat().st_size, child.stat().st_size)
        time.sleep(0.8)
        assert sizes == (grand.stat().st_size, child.stat().st_size)  # nobody writes any more
        shutil.rmtree(out)  # log files released: the work dir can be removed
        passed = True
    finally:
        if not passed:  # after success the PIDs are dead and may be recycled: never kill them then
            _kill_pids(tmp_path / "hb.pids")


def test_taskkill_by_absolute_path(monkeypatch):
    assert taskkill_exe({"SystemRoot": r"C:\Windows"}) == str(Path(r"C:\Windows") / "System32" / "taskkill.exe")
    assert taskkill_exe({}) == r"C:\Windows\System32\taskkill.exe"
    seen = []
    monkeypatch.setattr(runner.sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", r"D:\Win")
    monkeypatch.setattr(runner.subprocess, "run", lambda argv, **kw: seen.append((argv, kw)))

    class Proc:
        pid = 4242

        def kill(self):
            pass

    runner.kill_tree(Proc())
    (argv, kw), = seen
    assert argv == [str(Path(r"D:\Win") / "System32" / "taskkill.exe"), "/T", "/F", "/PID", "4242"]
    assert kw["shell"] is False


def iso_kwargs():
    return dict(
        input_3mf=Path("C:/in/part.3mf"), nozzle="0.4", process_set="standard",
        filament="Generic PETG @BBL P2S", plate_type="Textured PEI Plate", kit=False,
        overrides=Overrides.parse({}, "0.4"),
    )


def isolated(tmp_path, **kw):
    cfg = make_cfg(tmp_path)
    return cfg, FakePopen(cfg.sliced_dir / ".work-part-j1-p1", **kw)


def go(cfg, fake):
    return slice_isolated(cfg, out_name="part-j1-p1", popen=fake, killer=mark_killed, **iso_kwargs())


def test_slice_isolated_success_leaves_only_output(tmp_path):
    cfg, fake = isolated(
        tmp_path, result={"return_code": 0, "error_string": "success"},
        files=("part-j1-p1.gcode.3mf", "plate_1.gcode"),
    )
    run, final = go(cfg, fake)
    argv = fake.calls["argv"]
    assert argv[argv.index("--export-3mf") + 1] == "part-j1-p1.gcode.3mf"
    assert argv[argv.index("--outputdir") + 1].endswith(".work-part-j1-p1")
    assert run.return_code == 0 and final == cfg.sliced_dir / "part-j1-p1.gcode.3mf"
    assert [p.name for p in cfg.sliced_dir.iterdir()] == ["part-j1-p1.gcode.3mf"]


def test_slice_isolated_failure_and_timeout_clean_up(tmp_path):
    cfg, fake = isolated(tmp_path, result={"return_code": -50, "error_string": "outside"}, files=("plate_1.gcode",))
    run, final = go(cfg, fake)
    assert final is None and run.error_string == "outside" and list(cfg.sliced_dir.iterdir()) == []
    cfg, fake = isolated(tmp_path, hang=True)
    run, final = go(cfg, fake)
    assert final is None and run.timed_out and list(cfg.sliced_dir.iterdir()) == []


def test_slice_isolated_nonzero_exit_is_failure_even_with_output(tmp_path):
    cfg, fake = isolated(
        tmp_path, result={"return_code": 0, "error_string": "success"}, rc=1, files=("part-j1-p1.gcode.3mf",)
    )
    run, final = go(cfg, fake)
    assert final is None and run.return_code == 1 and list(cfg.sliced_dir.iterdir()) == []


def test_slice_isolated_removes_leftover_work_dir(tmp_path):
    cfg, fake = isolated(
        tmp_path, result={"return_code": 0, "error_string": "success"}, files=("part-j1-p1.gcode.3mf",)
    )
    stale = cfg.sliced_dir / ".work-part-j1-p1"
    stale.mkdir(parents=True)
    (stale / "old.gcode").write_text("old")
    run, final = go(cfg, fake)
    assert final is not None and [p.name for p in cfg.sliced_dir.iterdir()] == ["part-j1-p1.gcode.3mf"]


def test_slice_isolated_leftover_that_cannot_be_removed_fails_cleanly(tmp_path, monkeypatch):
    cfg, fake = isolated(tmp_path)
    (cfg.sliced_dir / ".work-part-j1-p1").mkdir(parents=True)

    def locked(path, *a, **kw):
        if not kw.get("ignore_errors"):
            raise PermissionError("locked")

    monkeypatch.setattr("slice_agent.runner.shutil.rmtree", locked)
    with pytest.raises(OSError):
        go(cfg, fake)
    assert "argv" not in fake.calls  # never started
