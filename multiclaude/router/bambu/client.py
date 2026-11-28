"""The Print tab's way to bambu-service: its /v1/ API with the app token, stdlib only.

Runs inside multiclaude's router (installed as multiclaude.addons.bambu), so it never imports
bambu_mcp. The token and the service's address stay on this machine: they are never in an
error, a log line or the tab's state.
"""
import json
import os
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG = Path.home() / ".config" / "bambu" / "miniapp.json"  # {"url", "token"}, mode 600, written by setup
STATE_MAX = 60 * 1024  # the chat takes an add-in's state up to 64 KB
POLL_ACTIVE, POLL_IDLE = 5, 30  # seconds between reads while a print may change, and otherwise
DOWN_TEXT = "Сервис принтера не отвечает"
RESPONSE_MAX = 256 * 1024


class ServiceDown(Exception):
    """bambu-service didn't answer usefully. The message names no address and no token."""


def load_config(path=CONFIG):
    """(url, token), or None when the file is missing, unreadable, malformed or (POSIX) open to others."""
    try:
        if os.name == "posix" and stat.S_IMODE(os.stat(path).st_mode) & 0o077:
            return None
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url, token = (data.get("url"), data.get("token")) if isinstance(data, dict) else (None, None)
    if not (isinstance(url, str) and url.startswith("http") and isinstance(token, str) and token):
        return None
    return url.rstrip("/"), token


class AppClient:
    def __init__(self, url, token, timeout=5.0):
        self._url, self._token, self._timeout = url.rstrip("/"), token, timeout

    def snapshot(self):
        return self._get("/v1/snapshot")

    def capabilities(self):
        return self._get("/v1/capabilities")

    def post(self, path, body=None):
        """(HTTP status, JSON body) of a POST. ServiceDown on no answer or a 5xx."""
        data = json.dumps(body if body is not None else {}).encode()
        return self._call("POST", path, data)

    def _get(self, path):
        status, body = self._call("GET", path, None)
        if status != 200:
            raise ServiceDown(f"{path} answered {status}")
        return body

    def _call(self, method, path, data):
        request = urllib.request.Request(self._url + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310 - our own URL
                status, raw = response.status, response.read(RESPONSE_MAX + 1)
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read(RESPONSE_MAX + 1)
        except (OSError, ValueError) as e:
            raise ServiceDown(type(e).__name__) from None
        if status >= 500:
            raise ServiceDown(f"{path} answered {status}")
        if len(raw) > RESPONSE_MAX:
            raise ServiceDown(f"{path} answered too much")
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise ServiceDown(f"{path} answered something that isn't JSON") from None
        return status, body if isinstance(body, dict) else {}


def tab_state(snapshot, last_ok, now, down):
    """The tab's state: the last good snapshot, when it was read, and a line when the last read failed.
    Cut to STATE_MAX by dropping history, then jobs from the oldest."""
    line = None
    if down is not None:
        line = DOWN_TEXT if last_ok is None else f"{DOWN_TEXT}, данные на {time.strftime('%H:%M', time.localtime(last_ok))}"
    state = {"snapshot": snapshot, "updated": last_ok, "down": line}
    if snapshot is None:
        return state
    snapshot = dict(snapshot)
    state["snapshot"] = snapshot

    def size():
        return len(json.dumps(state, ensure_ascii=False).encode())

    for key in ("history", "jobs"):
        if size() <= STATE_MAX:
            break
        items = list(snapshot.get(key) or [])
        while items and size() > STATE_MAX:
            items.pop()
            snapshot[key] = items
    return state


def poll_every(snapshot):
    """Seconds until the next read: short while the printer is in a state its profile can stop."""
    try:
        active = snapshot["status"]["gcode_state"] in snapshot["printer"]["stoppable_states"]
    except (KeyError, TypeError):
        return POLL_IDLE
    return POLL_ACTIVE if active else POLL_IDLE
