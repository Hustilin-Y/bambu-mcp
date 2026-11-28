"""The bambu add-in in multiclaude's router: the Print tab of the chat Mini App, owner only.

Installed by bambu-mcp's multiclaude/install.sh as multiclaude.addons.bambu. The chat add-in asks
for these hooks by name, and without it none is ever called. The tab's data comes only through the
chat's signed feed (addon_state). The token and the service's address stay in this process.

Starting, resuming and stopping a print are never requests to bambu-service: they are presses on
the buttons the service itself sent to Telegram, made through service-notify's press_from_app
(design 2026-11-28, section 6.3).
"""
import importlib
import json
import threading
import time

from . import client

TAB = {"id": "print", "title": "Печать", "module": "print.js"}
ADDON = "bambu"
SERVICE = "bambu"  # the service's name in service-notify
DOWN_ERROR = "Сервис принтера не отвечает."
OWNER_ONLY = "Только для владельца."
PRESS_REFUSALS = {"answered": "Уже нажато.", "expired": "Кнопка истекла, нажми «Печать» снова."}
POSTS = {  # action -> (path, what the tab shows when the service took it)
    "pause": ("/v1/pause", "Пауза отправлена."),
    "stop": ("/v1/request-stop", "Подтверди остановку: кнопки появятся здесь и в Telegram."),
    "snapshot": ("/v1/snapshot-to-telegram", "Снимок придёт в Telegram."),
    "prepare": ("/v1/prepare", "Готовлю печать. Подтверждение появится здесь и в Telegram."),
}

_people = {}  # person -> _Person, the owner's only in practice
_lock = threading.Lock()


class _Person:
    def __init__(self):
        self.snapshot = None   # the last good snapshot
        self.last_ok = None    # when it was read
        self.down = None       # why the last read failed, None when it didn't
        self.state = None      # the state last put on the page
        self.due = 0.0         # when to read again
        self.reading = False


def make_client():
    """The service's client from the file setup writes, None when it's missing (the tests replace this)."""
    config = client.load_config()
    return client.AppClient(*config) if config else None


def is_owner(user):
    return (getattr(user, "entry", None) or {}).get("role") == "owner"


def chat_tabs(user):
    return [dict(TAB)] if is_owner(user) else []


def chat_backfill(user):
    person = _people.get(user.name)
    if not is_owner(user) or person is None or person.state is None:
        return []
    return [_event(person.state)]


def on_tick(desk, now):
    """While the owner's page is open, read the service's snapshot when due, in the background."""
    chat = _chat()
    if chat is None:
        return
    for user in list(desk.users.values()):
        if not is_owner(user) or not chat.is_open(user):
            continue
        with _lock:
            person = _people.setdefault(user.name, _Person())
            if now < person.due or person.reading:
                continue
            person.reading = True
        threading.Thread(target=_read, args=(chat, user, person), name="bambu-tab-read", daemon=True).start()


def reading():
    """Whether a read is running (for the tests)."""
    return any(p.reading for p in _people.values())


def chat_tab_action(user, action, values):
    """A request from the Print tab: {"text"} or {"error"}, within the client's timeout."""
    if not is_owner(user):
        return {"error": OWNER_ONLY}
    values = values if isinstance(values, dict) else {}
    try:
        if action == "confirm":
            answer = _confirm(user, values)
        elif action == "capabilities":
            service = _service()
            answer = {"text": json.dumps(service.capabilities(), ensure_ascii=False)}
        elif action in POSTS:
            answer = _post(action, values)
        else:
            return {"error": f"Вкладка попросила {action!r}, такого действия нет."}
    except client.ServiceDown:
        return {"error": DOWN_ERROR}
    if "text" in answer:
        _read_soon(user)
    return answer


def _post(action, values):
    path, done = POSTS[action]
    body = None
    if action == "prepare":
        job_id, plate = _number(values.get("job_id")), _number(values.get("plate"))
        if job_id is None or plate is None:
            return {"error": "Не понял, какое задание и какой стол."}
        body = {"job_id": job_id, "plate": plate}
    status, answer = _service().post(path, body)
    if status in (200, 202):
        return {"text": done}
    return {"error": str(answer.get("error") or f"Сервис ответил {status}.")}


def _confirm(user, values):
    """A press on a button of a message the last snapshot listed as pending, by its position."""
    message_id, index = str(values.get("message_id") or ""), _number(values.get("index"), minimum=0)
    person = _people.get(user.name)
    pending = ((person.snapshot or {}).get("pending") or []) if person else []
    entry = next((p for p in pending if str(p.get("message_id")) == message_id), None)
    if entry is None or index is None or index >= len(entry.get("labels") or []) or not message_id.isdigit():
        return {"error": "Эта кнопка уже неактуальна, обнови вкладку."}
    from multiclaude.addons.service_notify import taps
    reason = taps.press_from_app(user, SERVICE, int(message_id), index, time.time())
    if reason is not None:
        return {"error": PRESS_REFUSALS.get(reason, f"Не получилось нажать: {reason}.")}
    return {"text": f"Нажато: {entry['labels'][index]}"}


def _read(chat, user, person):
    try:
        service = make_client()
        if service is None:
            raise client.ServiceDown("not configured")
        snapshot = service.snapshot()
        person.snapshot, person.last_ok, person.down = snapshot, time.time(), None
    except client.ServiceDown as e:
        person.down = str(e)
    except Exception as e:  # noqa: BLE001 - a background read: logged, tried again later
        person.down = type(e).__name__
        _log(f"bambu: reading the printer's snapshot failed: {e!r}")
    try:
        state = client.tab_state(person.snapshot, person.last_ok, time.time(), person.down)
        same = person.state is not None and {**person.state, "updated": 0} == {**state, "updated": 0}
        again = time.time() + client.poll_every(person.snapshot)
        if not same:
            if chat.feed_add(user, _event(state)):
                person.state = state
            else:
                again = 0.0  # the page closed meanwhile: read again when it opens
        person.due = again
    finally:
        person.reading = False


def _read_soon(user):
    person = _people.get(user.name)
    if person is not None:
        person.due = 0.0


def _event(state):
    return {"kind": "addon_state", "addon": ADDON, "state": state}


def _service():
    service = make_client()
    if service is None:
        raise client.ServiceDown("not configured")
    return service


def _number(value, minimum=1):
    """A whole number at least minimum from a tab's value (strings, as the relay passes them), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        number = int(value.strip())
    else:
        return None
    return number if number >= minimum else None


def _chat():
    """The chat add-in's module while it's on, else None."""
    from multiclaude import addons
    if not addons.enabled("chat"):
        return None
    return importlib.import_module("multiclaude.addons.chat")


def _log(message):
    from multiclaude.log import log
    log(message)
