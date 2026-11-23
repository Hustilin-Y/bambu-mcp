from pathlib import Path

import pytest

from bambu_mcp.config import Secrets, Settings


def test_secrets_load_requires_access_code_and_serial(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BAMBU_SERIAL=SER\nBAMBU_HOST=10.0.0.2\n")
    with pytest.raises(ValueError) as exc:
        Secrets.load(env)
    assert "BAMBU_ACCESS_CODE" in str(exc.value)

    env.write_text("BAMBU_ACCESS_CODE=12345678\nBAMBU_HOST=10.0.0.2\n")
    with pytest.raises(ValueError) as exc:
        Secrets.load(env)
    assert "BAMBU_SERIAL" in str(exc.value)
    assert "12345678" not in str(exc.value)


def test_secrets_load_full(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "BAMBU_ACCESS_CODE=12345678\n"
        'BAMBU_SERIAL="SER1"\n'
        "BAMBU_HOST=10.0.0.2\n"
        "NOTIFY_TOKEN=ntok\n"
        "CLIENT_TOKEN_PC=tok-pc\n"
        "CLIENT_TOKEN_ASSISTANT=tok-as\n"
    )
    s = Secrets.load(env)
    assert s.access_code == "12345678"
    assert s.serial == "SER1"
    assert s.host == "10.0.0.2"
    assert s.notify_token == "ntok"
    assert s.client_tokens == {"pc": "tok-pc", "assistant": "tok-as"}


def test_secrets_optional_fields(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BAMBU_ACCESS_CODE=1\nBAMBU_SERIAL=S\nBAMBU_HOST=h\n")
    s = Secrets.load(env)
    assert s.notify_token is None
    assert s.client_tokens == {}
    assert s.gateway_key is None
    assert s.slice_agent_token is None


def test_secrets_phase2_tokens(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "BAMBU_ACCESS_CODE=1\nBAMBU_SERIAL=S\nBAMBU_HOST=h\n"
        "GATEWAY_KEY=gw-key-1\nSLICE_AGENT_TOKEN=slice-tok-2\n"
    )
    s = Secrets.load(env)
    assert s.gateway_key == "gw-key-1"
    assert s.slice_agent_token == "slice-tok-2"
    assert "gw-key-1" not in repr(s) and "slice-tok-2" not in repr(s)


def test_settings_defaults(tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text("")
    s = Settings.load(toml)
    assert s.power_w == {"PLA": 200.0}
    assert s.heatup_kwh == 0.05
    assert s.timezone == "Europe/Kyiv"
    assert s.tls_mode == "ca"
    assert s.ca_file is None
    assert s.cert_sha256 is None
    assert s.notify_url is None
    assert s.camera_backend == "pyav"
    assert s.data_dir == Path("data")
    assert ("127.0.0.1", 8765) in s.listen
    assert s.jobs_root_allowed is None
    assert s.gateway_url is None
    assert s.slice_agent_url is None
    assert s.service_url is None


def test_settings_phase2_keys(tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text(
        'jobs_root_allowed = "/srv/cad/out"\n'
        'gateway_url = "http://127.0.0.1:8800"\n'
        'slice_agent_url = "http://192.168.88.10:11436"\n'
        'service_url = "http://192.168.88.11:8765"\n'
    )
    s = Settings.load(toml)
    assert s.jobs_root_allowed == Path("/srv/cad/out")
    assert s.gateway_url == "http://127.0.0.1:8800"
    assert s.slice_agent_url == "http://192.168.88.10:11436"
    assert s.service_url == "http://192.168.88.11:8765"


def test_settings_overrides(tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text(
        'data_dir = "/var/lib/bambu"\n'
        'listen = ["127.0.0.1:9000"]\n'
        'tls_mode = "pin"\n'
        'cert_sha256 = "ab"\n'
        'ca_file = "/etc/ca.pem"\n'
        "[power_w]\nPLA = 180\nPETG = 220.5\n"
    )
    s = Settings.load(toml)
    assert s.data_dir == Path("/var/lib/bambu")
    assert s.listen == [("127.0.0.1", 9000)]
    assert s.tls_mode == "pin"
    assert s.ca_file == Path("/etc/ca.pem")
    assert s.power_w == {"PLA": 180.0, "PETG": 220.5}


@pytest.mark.parametrize(
    "line",
    [
        'tls_mode = "none"',
        'camera_backend = "ffmpeg"',
        "bogus_key = 1",
        'gateway_url = "ftp://x"',
        "slice_agent_url = 5",
        'service_url = ""',
        "jobs_root_allowed = 3",
    ],
)
def test_settings_rejects_bad_values(tmp_path, line):
    toml = tmp_path / "config.toml"
    toml.write_text(line + "\n")
    with pytest.raises(ValueError):
        Settings.load(toml)


def test_example_config_matches_defaults(tmp_path):
    example = Path(__file__).parent.parent / "config.example.toml"
    empty = tmp_path / "empty.toml"
    empty.write_text("")
    assert Settings.load(example) == Settings.load(empty)


def test_secrets_load_requires_host(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BAMBU_ACCESS_CODE=12345678\nBAMBU_SERIAL=SER\n")
    with pytest.raises(ValueError) as exc:
        Secrets.load(env)
    assert "BAMBU_HOST" in str(exc.value)
    assert "12345678" not in str(exc.value) and "SER" not in str(exc.value).replace("BAMBU_", "")
    env.write_text("BAMBU_ACCESS_CODE=12345678\nBAMBU_SERIAL=SER\nBAMBU_HOST=\n")
    with pytest.raises(ValueError, match="BAMBU_HOST"):
        Secrets.load(env)


def test_settings_power_w_must_have_pla(tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text("[power_w]\nPETG = 220\n")
    with pytest.raises(ValueError, match="PLA"):
        Settings.load(toml)
