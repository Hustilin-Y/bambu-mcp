import configparser
from pathlib import Path

from bambu_mcp.config import Settings

ROOT = Path(__file__).resolve().parent.parent


def _unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(ROOT / "deploy" / "bambu-service.service", encoding="utf-8")
    return parser


def test_unit_limits_memory_and_restarts():
    service = _unit()["Service"]
    assert service["MemoryMax"] == "150M"
    assert service["Restart"] == "on-failure"
    assert service["RestartSec"] == "10"
    assert service["User"] == "shiba-srv"
    assert service["ExecStart"] == "/srv/bambu/app/.venv/bin/bambu-service"


def test_unit_waits_for_network_online():
    unit = _unit()["Unit"]
    assert "network-online.target" in unit["After"].split()
    assert "network-online.target" in unit["Wants"].split()


def test_example_config_loads_with_defaults():
    settings = Settings.load(ROOT / "config.example.toml")
    assert settings == Settings()


# -- slice-agent on the PC (deploy/slice-agent) ------------------------------------

SLICE_DIR = ROOT / "deploy" / "slice-agent"
AGENT_KEYS = {
    "studio_exe", "studio_data", "studio_resources", "profiles_dir", "sliced_dir",
    "listen", "machine", "process_sets", "filaments",
}


def _agent_env(tmp_path: Path) -> dict[str, str]:
    return {
        "LOCALAPPDATA": str(tmp_path / "local"),
        "APPDATA": str(tmp_path / "roaming"),
        "USERPROFILE": str(tmp_path / "home"),
    }


def test_slice_agent_example_config_loads_with_defaults(tmp_path):
    from slice_agent.config import load_config

    env = _agent_env(tmp_path)
    path = SLICE_DIR / "slice-agent.example.toml"
    assert path.is_file()  # load_config falls back to the defaults for a missing file
    example = load_config(path, env)
    assert example == load_config(tmp_path / "missing.toml", env)
    assert example.listen == ("0.0.0.0", 11436)
    assert example.process_sets["standard"]


def test_slice_agent_example_config_has_only_known_keys_and_no_secrets():
    import tomllib

    text = (SLICE_DIR / "slice-agent.example.toml").read_text(encoding="utf-8")
    raw = tomllib.loads(text)
    assert set(raw) <= AGENT_KEYS  # load_config ignores unknown keys, so a typo would pass silently
    # Every commented-out key is one the agent reads too.
    commented = {
        line.lstrip("# ").split("=", 1)[0].strip()
        for line in text.splitlines()
        if line.startswith("# ") and "=" in line and not line.startswith("#  ")
    }
    assert commented <= AGENT_KEYS
    settings = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    assert not [line for line in settings if "token" in line.lower()]


def _install_script() -> str:
    return (SLICE_DIR / "install.ps1").read_text(encoding="utf-8")


def test_install_script_is_ascii_and_powershell_51_safe():
    text = _install_script()
    text.encode("ascii")  # Windows PowerShell 5.1 reads a BOM-less script as ANSI
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "&&" not in code and "||" not in code
    assert "??" not in code and "?." not in code


def test_install_script_registers_a_non_elevated_logon_task():
    text = _install_script()
    assert "-AtLogOn" in text
    assert "-RunLevel Limited" in text
    assert "Highest" not in text
    assert "run slice-agent serve" in text
    assert "-RestartCount" in text
    assert "SLICE_AGENT_TOKEN" in text  # checked as present, never put into the task


def test_install_script_firewall_rule_is_server_only():
    text = _install_script()
    assert '"192.168.88.11"' in text
    assert "11436" in text
    assert "New-NetFirewallRule" in text
    assert "-RemoteAddress $ServerAddress" in text
    assert "-FirewallOnly" in text  # the elevated one-liner printed for a non-admin run


def test_deploy_readmes_cover_phase2_keys():
    service = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    for key in (
        "gateway_url", "slice_agent_url", "service_url", "jobs_root_allowed",
        "GATEWAY_KEY", "SLICE_AGENT_TOKEN", "BAMBU_MCP_TOKEN", "bambu-mcp upload",
        "tray_info_idx", "nozzle_temp_min", "load_spool", "ams_filament_setting",
    ):
        assert key in service, key
    agent = (SLICE_DIR / "README.md").read_text(encoding="utf-8")
    for key in (
        "slice-agent flatten", "SLICE_AGENT_TOKEN", "PC_AGENT_TOKEN", "studio_exe",
        "studio_resources", "Smart App Control", "install.ps1", "3 %",
    ):
        assert key in agent, key


def test_deploy_readme_covers_phase3():
    service = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    phase3 = service[service.index("## 8. Phase 3"):]
    for key in (
        "USB stick", "print-cache", "30 s", "prepare_print", "pause_print", "request_stop",
        "never deletes", "project_file", "start_print", "resume", "990",
    ):
        assert key in phase3, key
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for tool in ("prepare_print", "pause_print", "request_stop", "load_spool", "printer_status"):
        assert tool in readme, tool
