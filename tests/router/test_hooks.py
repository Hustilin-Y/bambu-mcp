"""The Print tab's hooks in multiclaude's router (design 2026-11-28, 6.3 and 6.4), with multiclaude faked."""

import importlib
import sys
import threading
import time
import types
from types import SimpleNamespace

import pytest

from bambu import client as bambu_client


class FakeChat:
    def __init__(self):
        self.open = set()
        self.events = []
        self.accept = True

    def is_open(self, user):
        return user.name in self.open

    def feed_add(self, user, event):
        if not self.accept:
            return False
        self.events.append((user.name, event))
        return True


class FakeClient:
    def __init__(self, snapshot=None):
        self.snap = snapshot if snapshot is not None else SNAPSHOT
        self.down = False
        self.sleep = 0.0
        self.posts = []
        self.answers = {}

    def snapshot(self):
        if self.sleep:
            time.sleep(self.sleep)
        if self.down:
            raise bambu_client.ServiceDown("ConnectionRefusedError")
        return self.snap

    def capabilities(self):
        return {"tools": [{"name": "printer_status", "class": "read", "summary": "x"}]}

    def post(self, path, body=None):
        self.posts.append((path, body))
        return self.answers.get(path, (202, {"pending": True}))


SNAPSHOT = {
    "status": {"gcode_state": "RUNNING"},
    "printer": {"stoppable_states": ["PAUSE", "PREPARE", "RUNNING"]},
    "jobs": [],
    "history": [],
    "pending": [{"message_id": "901", "kind": "start", "text": "Print?", "labels": ["Стол чистый, печатать", "Отмена"],
                 "expires_at": None}],
}

OWNER = SimpleNamespace(name="alice", entry={"role": "owner"}, chat_id=100)
MEMBER = SimpleNamespace(name="bob", entry={"role": "member"}, chat_id=101)


@pytest.fixture
def mc(monkeypatch):
    """A fake multiclaude: chat on, service-notify's press_from_app recorded, logs kept."""
    chat = FakeChat()
    pressed, logs = [], []
    reason = {"value": None}

    def press_from_app(user, service, message_id, index, now):
        pressed.append((user.name, service, message_id, index))
        return reason["value"]

    root = types.ModuleType("multiclaude")
    addons = types.ModuleType("multiclaude.addons")
    addons.enabled = lambda name: name == "chat"
    chat_module = types.ModuleType("multiclaude.addons.chat")
    chat_module.is_open, chat_module.feed_add = chat.is_open, chat.feed_add
    sn = types.ModuleType("multiclaude.addons.service_notify")
    taps = types.ModuleType("multiclaude.addons.service_notify.taps")
    taps.press_from_app = press_from_app
    log_module = types.ModuleType("multiclaude.log")
    log_module.log = logs.append
    for name, module in {
        "multiclaude": root, "multiclaude.addons": addons, "multiclaude.addons.chat": chat_module,
        "multiclaude.addons.service_notify": sn, "multiclaude.addons.service_notify.taps": taps,
        "multiclaude.log": log_module,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    import bambu
    bambu = importlib.reload(bambu)
    fake = FakeClient()
    monkeypatch.setattr(bambu, "make_client", lambda: fake)
    return SimpleNamespace(bambu=bambu, chat=chat, client=fake, pressed=pressed, logs=logs, reason=reason)


def desk(*users):
    return SimpleNamespace(users={u.name: u for u in users})


def tick_and_wait(mc, now=None, users=(OWNER, MEMBER)):
    mc.bambu.on_tick(desk(*users), time.time() if now is None else now)
    deadline = time.time() + 3
    while mc.bambu.reading() and time.time() < deadline:
        time.sleep(0.01)


def states(mc, name="alice"):
    return [e["state"] for who, e in mc.chat.events if who == name and e["kind"] == "addon_state"]


def test_tab_only_for_owner(mc):
    assert mc.bambu.chat_tabs(OWNER) == [{"id": "print", "title": "Печать", "module": "print.js"}]
    assert mc.bambu.chat_tabs(MEMBER) == []


def test_non_owner_gets_no_state(mc):
    mc.chat.open = {"alice", "bob"}
    tick_and_wait(mc)
    assert states(mc, "bob") == [] and mc.bambu.chat_backfill(MEMBER) == []
    assert mc.bambu.chat_tab_action(MEMBER, "pause", {}) == {"error": "Только для владельца."}
    assert mc.client.posts == []


def test_state_put_only_on_change(mc):
    mc.chat.open = {"alice"}
    tick_and_wait(mc, now=0)
    tick_and_wait(mc, now=10**10)
    (state,) = states(mc)
    assert state["snapshot"] == SNAPSHOT and state["down"] is None
    assert mc.bambu.chat_backfill(OWNER) == [{"kind": "addon_state", "addon": "bambu", "state": state}]


def test_no_read_while_page_closed(mc):
    tick_and_wait(mc)
    assert states(mc) == []


def test_service_down_keeps_last_state(mc):
    mc.chat.open = {"alice"}
    tick_and_wait(mc, now=0)
    mc.client.down = True
    tick_and_wait(mc, now=10**10)
    last = states(mc)[-1]
    assert last["snapshot"] == SNAPSHOT and last["down"].startswith("Сервис принтера не отвечает, данные на ")
    mc.client.down = False
    tick_and_wait(mc, now=2 * 10**10)
    assert states(mc)[-1]["down"] is None


def test_tick_never_blocks(mc):
    mc.chat.open = {"alice"}
    mc.client.sleep = 0.5
    start = time.perf_counter()
    mc.bambu.on_tick(desk(OWNER), 0)
    mc.bambu.on_tick(desk(OWNER), 10**10)  # a read is running: not a second one
    assert time.perf_counter() - start < 0.05
    tick_and_wait(mc, now=10**10)
    assert len(states(mc)) == 1


def test_actions_post_to_the_service(mc):
    assert mc.bambu.chat_tab_action(OWNER, "pause", {}) == {"text": "Пауза отправлена."}
    assert mc.bambu.chat_tab_action(OWNER, "prepare", {"job_id": "7", "plate": "2"})["text"].startswith("Готовлю")
    assert mc.bambu.chat_tab_action(OWNER, "stop", {})["text"]
    assert mc.bambu.chat_tab_action(OWNER, "snapshot", {})["text"]
    assert [p for p, _ in mc.client.posts] == ["/v1/pause", "/v1/prepare", "/v1/request-stop", "/v1/snapshot-to-telegram"]
    assert mc.client.posts[1][1] == {"job_id": 7, "plate": 2}


def test_action_refusal_and_bad_values(mc):
    mc.client.answers["/v1/pause"] = (409, {"error": "the printer is IDLE: pause works only while RUNNING"})
    assert mc.bambu.chat_tab_action(OWNER, "pause", {}) == {"error": "the printer is IDLE: pause works only while RUNNING"}
    assert "error" in mc.bambu.chat_tab_action(OWNER, "prepare", {"job_id": "x", "plate": "1"})
    assert "error" in mc.bambu.chat_tab_action(OWNER, "nonsense", {})
    mc.client.down = True
    mc.client.post = lambda *a: (_ for _ in ()).throw(bambu_client.ServiceDown("x"))
    assert mc.bambu.chat_tab_action(OWNER, "pause", {}) == {"error": "Сервис принтера не отвечает."}


def test_capabilities_action(mc):
    answer = mc.bambu.chat_tab_action(OWNER, "capabilities", {})
    assert "printer_status" in answer["text"]


def test_confirm_presses_a_pending_button(mc):
    mc.chat.open = {"alice"}
    tick_and_wait(mc)
    assert mc.bambu.chat_tab_action(OWNER, "confirm", {"message_id": "901", "index": "0"}) == {
        "text": "Нажато: Стол чистый, печатать"}
    assert mc.pressed == [("alice", "bambu", 901, 0)]


def test_confirm_rejects_unknown_message(mc):
    mc.chat.open = {"alice"}
    tick_and_wait(mc)
    for values in ({"message_id": "902", "index": "0"}, {"message_id": "901", "index": "2"},
                   {"message_id": "901", "index": "-1"}, {"message_id": "901"}, {}):
        assert "error" in mc.bambu.chat_tab_action(OWNER, "confirm", values)
    assert mc.pressed == []


def test_confirm_maps_refusal_to_text(mc):
    mc.chat.open = {"alice"}
    tick_and_wait(mc)
    mc.reason["value"] = "answered"
    assert mc.bambu.chat_tab_action(OWNER, "confirm", {"message_id": "901", "index": "0"}) == {"error": "Уже нажато."}
    mc.reason["value"] = "expired"
    assert mc.bambu.chat_tab_action(OWNER, "confirm", {"message_id": "901", "index": "0"}) == {
        "error": "Кнопка истекла, нажми «Печать» снова."}
    mc.reason["value"] = "other_service"
    assert mc.bambu.chat_tab_action(OWNER, "confirm", {"message_id": "901", "index": "0"}) == {
        "error": "Не получилось нажать: other_service."}


def test_action_makes_the_next_read_due(mc):
    mc.chat.open = {"alice"}
    tick_and_wait(mc, now=0)
    mc.client.snap = {**SNAPSHOT, "status": {"gcode_state": "PAUSE"}}
    tick_and_wait(mc, now=1)  # not due yet
    assert len(states(mc)) == 1
    mc.bambu.chat_tab_action(OWNER, "pause", {})
    tick_and_wait(mc, now=1)
    assert states(mc)[-1]["snapshot"]["status"]["gcode_state"] == "PAUSE"


def test_threads_are_daemons(mc):
    mc.chat.open = {"alice"}
    mc.client.sleep = 0.2
    mc.bambu.on_tick(desk(OWNER), 0)
    readers = [t for t in threading.enumerate() if t.name.startswith("bambu-tab")]
    assert readers and all(t.daemon for t in readers)
    tick_and_wait(mc, now=0)



def test_action_during_a_read_still_makes_the_next_read_due(mc):
    mc.chat.open = {"alice"}
    mc.client.sleep = 0.3
    mc.client.snap = {**SNAPSHOT, "status": {"gcode_state": "PAUSE"}}
    mc.bambu.on_tick(desk(OWNER), time.time())  # a read starts
    mc.bambu.chat_tab_action(OWNER, "pause", {})  # while it runs
    tick_and_wait(mc, now=time.time())
    mc.client.sleep = 0
    mc.client.snap = {**SNAPSHOT, "status": {"gcode_state": "FINISH"}}
    tick_and_wait(mc, now=time.time())  # due at once, not in 5 or 30 s
    assert states(mc)[-1]["snapshot"]["status"]["gcode_state"] == "FINISH"


def test_reads_are_fast_for_a_minute_after_an_action(mc):
    idle = {**SNAPSHOT, "status": {"gcode_state": "IDLE"}}
    mc.client.snap = idle
    mc.chat.open = {"alice"}
    tick_and_wait(mc, now=0)
    person = mc.bambu._people["alice"]
    assert person.due - time.time() > 20  # idle: 30 s
    mc.bambu.chat_tab_action(OWNER, "prepare", {"job_id": "7", "plate": "1"})
    tick_and_wait(mc, now=time.time())
    assert person.due - time.time() <= 5.5  # just asked: 5 s
