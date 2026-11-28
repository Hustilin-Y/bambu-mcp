"""The multiclaude add-in (multiclaude/): install.sh, setup and teardown against a fake checkout.

Every outside command (uv, systemctl, loginctl, curl, the assistant CLI) is a stub on PATH that
logs its argv. A `sudo` stub fails the test if anything calls it.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("jq") is None, reason="POSIX shell and jq needed"
)

ROOT_DIR = Path(__file__).resolve().parent.parent
INSTALL = ROOT_DIR / "multiclaude" / "install.sh"
SECRET_INPUT = "192.0.2.30\nSERIAL01\nACCESS99\na1\n"

STUB = """#!/usr/bin/env bash
printf '%s\\n' "$(basename "$0") $*" >> "$STUB_LOG"
"""
STUBS = {
    "uv": STUB,
    "systemctl": STUB,
    "loginctl": STUB + 'echo "Linger=no"\n',
    "curl": STUB + 'out=""; while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
            'printf -- "-----BEGIN CERTIFICATE-----\\nx\\n-----END CERTIFICATE-----\\n" > "$out"\n',
    "sudo": STUB + "exit 99\n",
}
ASSISTANT = STUB + 'if [ "$1 $2" = "service key" ]; then echo "notify-token-$(date +%N)"; fi\n'


class Env:
    def __init__(self, tmp: Path, addons=("service-notify",)) -> None:
        self.home = tmp / "home"
        self.root = self.home / "assistant"
        self.stubs = tmp / "stubs"
        self.log = tmp / "stub.log"
        (self.root / "bin").mkdir(parents=True)
        (self.root / "addons").mkdir()
        self.stubs.mkdir()
        self._exe(self.root / "bin" / "assistant", ASSISTANT)
        for name, body in STUBS.items():
            self._exe(self.stubs / name, body)
        (self.root / "users.json").write_text(json.dumps({"owen": {"role": "owner", "display": "Owen"}}))
        self.set_addons(addons)
        self.log.write_text("")

    @staticmethod
    def _exe(path: Path, body: str) -> None:
        path.write_text(body)
        path.chmod(0o755)

    def set_addons(self, addons) -> None:
        (self.root / "config.json").write_text(json.dumps({"addons": list(addons)}))

    def env(self) -> dict:
        return {
            "HOME": str(self.home),
            "PATH": f"{self.stubs}:/usr/bin:/bin",
            "STUB_LOG": str(self.log),
            "LANG": "C.UTF-8",
        }

    def run(self, *args, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run(list(args), input=stdin, capture_output=True, text=True, env=self.env(), timeout=60)

    def install(self) -> subprocess.CompletedProcess:
        return self.run("bash", str(INSTALL), "--root", str(self.root))

    def setup(self, stdin: str = SECRET_INPUT) -> subprocess.CompletedProcess:
        return self.run("bash", str(self.root / "addons" / "bambu" / "setup"), stdin=stdin)

    def teardown(self) -> subprocess.CompletedProcess:
        return self.run("bash", str(self.root / "addons" / "bambu" / "teardown"))

    @property
    def env_file(self) -> Path:
        return self.home / ".config" / "bambu" / ".env"

    def calls(self, prog: str) -> list[str]:
        return [line for line in self.log.read_text().splitlines() if line.split(" ", 1)[0] == prog]


@pytest.fixture
def e(tmp_path) -> Env:
    env = Env(tmp_path)
    result = env.install()
    assert result.returncode == 0, result.stderr
    return env


def test_install_copies_and_fills(e):
    folder = e.root / "addons" / "bambu"
    for name in ("README.md", "rules.md", "next-steps", "setup", "teardown", "bambu-service.service"):
        text = (folder / name).read_text()
        assert "{{" not in text, name
    assert os.access(folder / "setup", os.X_OK) and os.access(folder / "teardown", os.X_OK)
    assert str(ROOT_DIR) in (folder / "bambu-service.service").read_text()
    assert "Owen" in (folder / "rules.md").read_text()


def test_setup_requires_service_notify(e):
    e.set_addons([])
    result = e.setup()
    assert result.returncode != 0
    assert "service-notify" in result.stderr
    assert not e.env_file.exists()


def test_setup_writes_env_600_and_config(e):
    result = e.setup()
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(e.env_file.stat().st_mode) == 0o600
    env = dict(line.split("=", 1) for line in e.env_file.read_text().splitlines())
    assert env["BAMBU_HOST"] == "192.0.2.30"
    assert env["BAMBU_SERIAL"] == "SERIAL01"
    assert env["BAMBU_ACCESS_CODE"] == "ACCESS99"
    assert env["NOTIFY_TOKEN"].startswith("notify-token-")
    assert len(env["CLIENT_TOKEN_ASSISTANT"]) >= 40
    config = (e.home / ".config" / "bambu" / "config.toml").read_text()
    assert 'printer = "a1"' in config
    assert 'listen = ["127.0.0.1:8765"]' in config
    assert 'notify_url = "http://127.0.0.1:8790"' in config
    # No secret in any argv, stdout or stderr.
    log = e.log.read_text()
    for secret in ("ACCESS99", env["NOTIFY_TOKEN"], env["CLIENT_TOKEN_ASSISTANT"]):
        assert secret not in log
        assert secret not in result.stdout + result.stderr
    # The user unit, started under the user's manager.
    assert (e.home / ".config" / "systemd" / "user" / "bambu-service.service").is_file()
    assert any("--user restart bambu-service" in c for c in e.calls("systemctl"))
    # The MCP server for the owner's workspace, with the assistant's token.
    claude = json.loads((e.home / ".claude.json").read_text())
    server = claude["projects"][str(e.home / "workspaces" / "owen")]["mcpServers"]["bambu"]
    assert server == {
        "type": "http",
        "url": "http://127.0.0.1:8765/mcp",
        "headers": {"Authorization": f"Bearer {env['CLIENT_TOKEN_ASSISTANT']}"},
    }
    assert stat.S_IMODE((e.home / ".claude.json").stat().st_mode) == 0o600


def test_setup_twice_is_idempotent(e):
    assert e.setup().returncode == 0
    before = e.env_file.read_text()
    config = (e.home / ".config" / "bambu" / "config.toml").read_text()
    second = e.setup(stdin="")  # nothing to ask: no input needed
    assert second.returncode == 0, second.stderr
    assert e.env_file.read_text() == before
    assert (e.home / ".config" / "bambu" / "config.toml").read_text() == config
    assert len([c for c in e.calls("assistant") if c.startswith("assistant service key")]) == 1
    claude = json.loads((e.home / ".claude.json").read_text())
    assert list(claude["projects"][str(e.home / "workspaces" / "owen")]["mcpServers"]) == ["bambu"]


def test_setup_keeps_other_mcp_servers(e):
    ws = str(e.home / "workspaces" / "owen")
    (e.home / ".claude.json").write_text(json.dumps({"projects": {ws: {"mcpServers": {"other": {"type": "stdio"}}}}, "x": 1}))
    assert e.setup().returncode == 0
    claude = json.loads((e.home / ".claude.json").read_text())
    assert claude["x"] == 1
    assert set(claude["projects"][ws]["mcpServers"]) == {"other", "bambu"}


def test_setup_prints_linger_command_without_sudo(e):
    result = e.setup()
    assert result.returncode == 0, result.stderr
    assert "sudo loginctl enable-linger" in result.stdout
    assert e.calls("sudo") == []


def test_setup_rejects_unknown_model(e):
    result = e.setup(stdin="192.0.2.30\nSERIAL01\nACCESS99\nx9\n")
    assert result.returncode != 0
    assert "no printer profile 'x9'" in result.stderr


def test_teardown_keeps_data(e):
    assert e.setup().returncode == 0
    result = e.teardown()
    assert result.returncode == 0, result.stderr
    assert any("service revoke bambu" in c for c in e.calls("assistant"))
    assert any("--user disable --now bambu-service" in c for c in e.calls("systemctl"))
    assert e.env_file.exists() and "BAMBU_ACCESS_CODE=ACCESS99" in e.env_file.read_text()
    assert "NOTIFY_TOKEN" not in e.env_file.read_text()
    claude = json.loads((e.home / ".claude.json").read_text())
    assert "bambu" not in claude["projects"][str(e.home / "workspaces" / "owen")]["mcpServers"]
    # Enabled again: a fresh notify key, no questions.
    assert e.setup(stdin="").returncode == 0
    assert "NOTIFY_TOKEN=" in e.env_file.read_text()


def test_addon_has_no_owner_values():
    import importlib.util

    spec = importlib.util.spec_from_file_location("owner_values", Path(__file__).parent / "test_no_owner_values.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    owner_value_hits = module.owner_value_hits

    assert [h for h in owner_value_hits() if h.startswith("multiclaude/")] == []


# -- the Mini App's Print tab (design 2026-11-28, 6.4) ---------------------------------------

ADDONS_INIT = (
    '"""The add-ins."""\n'
    'ALL = ("slack", "siri", "voice-notes", "stickers", "locations", "reminders", "quiet-hours", "model-fallback",\n'
    '       "skill-notes", "backup", "chat", "service-notify")\n'
    "\n\ndef names():\n    return ALL\n"
)
ALL_LINE = 'ALL += ("bambu",)  # bambu-mcp install.sh'


def _with_core(env: Env, init: str = ADDONS_INIT) -> Path:
    path = env.root / "lib" / "multiclaude" / "addons" / "__init__.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(init)
    return path


def test_install_adds_router_module_and_all_line(tmp_path):
    env = Env(tmp_path)
    init = _with_core(env)
    assert env.install().returncode == 0
    module = env.root / "lib" / "multiclaude" / "addons" / "bambu"
    assert (module / "__init__.py").is_file() and (module / "client.py").is_file()
    assert init.read_text().rstrip().endswith(ALL_LINE)
    assert (env.root / "addons" / "bambu" / "relay-public").is_dir()
    namespace: dict = {}
    exec(init.read_text(), namespace)
    assert namespace["ALL"][-1] == "bambu"


def test_install_twice_adds_line_once(tmp_path):
    env = Env(tmp_path)
    init = _with_core(env)
    env.install()
    env.install()
    assert init.read_text().count(ALL_LINE) == 1


def test_uninstall_removes_line_and_module(tmp_path):
    env = Env(tmp_path)
    init = _with_core(env)
    env.install()
    assert env.run("bash", str(INSTALL), "--root", str(env.root), "--uninstall").returncode == 0
    assert init.read_text() == ADDONS_INIT
    assert not (env.root / "lib" / "multiclaude" / "addons" / "bambu").exists()


def test_install_after_local_gpu_patch(tmp_path):
    env = Env(tmp_path)
    patched = ADDONS_INIT.replace('"chat", "service-notify")', '"chat", "service-notify", "local-gpu")')
    init = _with_core(env, patched)
    env.install()
    text = init.read_text()
    assert '"service-notify", "local-gpu")' in text and text.rstrip().endswith(ALL_LINE)
    env.run("bash", str(INSTALL), "--root", str(env.root), "--uninstall")
    assert init.read_text() == patched


def test_install_without_module_support_still_installs(e):
    assert not (e.root / "lib").exists()
    assert (e.root / "addons" / "bambu" / "setup").is_file()


def test_setup_writes_miniapp_json_600(e):
    assert e.setup().returncode == 0
    path = e.home / ".config" / "bambu" / "miniapp.json"
    data = json.loads(path.read_text())
    token = next(line.split("=", 1)[1] for line in e.env_file.read_text().splitlines() if line.startswith("APP_TOKEN_MINIAPP="))
    assert data == {"url": "http://127.0.0.1:8765", "token": token} and len(token) >= 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert e.setup().returncode == 0  # again: the same token
    assert json.loads(path.read_text())["token"] == token
    assert token not in e.log.read_text()


def test_teardown_removes_miniapp_json(e):
    e.setup()
    assert e.teardown().returncode == 0
    assert not (e.home / ".config" / "bambu" / "miniapp.json").exists()
    assert "APP_TOKEN_MINIAPP=" in e.env_file.read_text()
