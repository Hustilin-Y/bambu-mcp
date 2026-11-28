from pathlib import Path

import pytest

from slice_agent.config import config_path, load_config


def _win_env(tmp_path: Path) -> dict[str, str]:
    return {
        "LOCALAPPDATA": str(tmp_path / "local"),
        "APPDATA": str(tmp_path / "roaming"),
        "USERPROFILE": str(tmp_path / "home"),
    }


def _linux_env(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }


def test_windows_paths_unchanged(tmp_path):
    env = _win_env(tmp_path)
    assert config_path(env, platform="win32") == tmp_path / "local" / "bambu-mcp" / "slice-agent.toml"
    cfg = load_config(tmp_path / "missing.toml", env, platform="win32")
    assert cfg.profiles_dir == tmp_path / "local" / "bambu-mcp" / "profiles"
    assert cfg.studio_data == tmp_path / "roaming" / "BambuStudio"
    assert cfg.sliced_dir == tmp_path / "home" / "bambu-mcp" / "sliced"
    assert cfg.printer == "p2s"
    assert cfg.machine == {"0.4": "Bambu Lab P2S 0.4 nozzle"}


def test_linux_paths_from_xdg(tmp_path):
    env = _linux_env(tmp_path)
    assert config_path(env, platform="linux") == tmp_path / "cfg" / "bambu-mcp" / "slice-agent.toml"
    toml = tmp_path / "agent.toml"
    toml.write_text('studio_exe = "/opt/studio/bambu-studio"\nstudio_resources = "/opt/studio/resources/profiles/BBL"\n')
    cfg = load_config(toml, env, platform="linux")
    assert cfg.studio_exe == Path("/opt/studio/bambu-studio")
    assert cfg.profiles_dir == tmp_path / "data" / "bambu-mcp" / "profiles"
    assert cfg.studio_data == tmp_path / "home" / ".config" / "BambuStudio"
    assert cfg.sliced_dir == tmp_path / "home" / "bambu-mcp" / "sliced"


def test_linux_xdg_fallbacks(tmp_path):
    env = {"HOME": str(tmp_path / "home")}
    assert config_path(env, platform="linux") == tmp_path / "home" / ".config" / "bambu-mcp" / "slice-agent.toml"
    toml = tmp_path / "agent.toml"
    toml.write_text('studio_exe = "/x"\nstudio_resources = "/y"\n')
    cfg = load_config(toml, env, platform="linux")
    assert cfg.profiles_dir == tmp_path / "home" / ".local" / "share" / "bambu-mcp" / "profiles"


@pytest.mark.parametrize("missing", ["studio_exe", "studio_resources"])
def test_linux_requires_studio_paths(tmp_path, missing):
    toml = tmp_path / "agent.toml"
    keys = {"studio_exe": "/x", "studio_resources": "/y"}
    del keys[missing]
    toml.write_text("".join(f'{k} = "{v}"\n' for k, v in keys.items()))
    with pytest.raises(ValueError, match=missing):
        load_config(toml, _linux_env(tmp_path), platform="linux")


def test_a1_agent_defaults(tmp_path):
    toml = tmp_path / "agent.toml"
    toml.write_text('printer = "a1"\n')
    cfg = load_config(toml, _win_env(tmp_path), platform="win32")
    assert cfg.profile.model_name == "Bambu Lab A1"
    assert cfg.machine == {"0.4": "Bambu Lab A1 0.4 nozzle"}
    assert cfg.process_sets == {"standard": "0.20mm Standard @BBL A1"}
    assert cfg.filaments == ["Generic PLA @BBL A1", "Generic PETG @BBL A1"]


def test_unknown_agent_printer(tmp_path):
    toml = tmp_path / "agent.toml"
    toml.write_text('printer = "x1c"\n')
    with pytest.raises(ValueError, match="a1, p2s"):
        load_config(toml, _win_env(tmp_path), platform="win32")
