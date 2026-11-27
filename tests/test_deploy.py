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
