# Print tab, phase 1 (printer tab) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An owner-only "Печать" tab in the multiclaude chat Mini App that shows the printer, slots, jobs, history and capabilities, pauses, and starts, resumes and stops prints through presses on the service's own buttons.

**Architecture:** bambu-service gains a small `/v1/` REST API behind its own token (read and control scopes, no start, resume or stop). A router add-in module `bambu` (copied into the multiclaude checkout by `multiclaude/install.sh`) polls `/v1/snapshot` while the owner's page is open and pushes it as `addon_state`. Start, resume and stop are presses on the same service-notify buttons the service sent to Telegram, made from the tab through a new generic `press_from_app` in service-notify.

**Tech Stack:** Python 3.12 + Starlette + SQLite (bambu-service), stdlib-only Python (router module, runs in multiclaude's interpreter), plain ES module (tab), pytest, `node --test`.

**Spec:** `docs/superpowers/specs/2026-11-28-miniapp-print-tab-design.md` (sections 3, 5, 6, 10, 11 phase 1). Read it first.

## Global Constraints

- No serial, IP, access code, token, camera URL or server path in any `/v1/` answer, `addon_state`, log line or tab module (PN3).
- No path through which a client token, the `miniapp` token included, starts, resumes or stops a print. `/v1/` endpoints never do.
- Everything from the printer profile: no P2S or AMS assumption (PN5). An `a1` profile test runs wherever slots are drawn.
- `/v1/snapshot` at most 48 KB, `addon_state` at most 60 KB (`STATE_MAX = 60 * 1024`).
- Router polling: every 5 s while `gcode_state` is in the profile's `stoppable_states`, every 30 s otherwise, only while `chat.is_open(user)`. The tick never waits on HTTP.
- `print.js` at most 60 KB gzip (PN4). No external imports.
- Router module: stdlib only, never imports `bambu_mcp`.
- Prose in code comments and docs: English, no em dash, no semicolon. UI strings in the tab and Telegram: Russian.
- Two repositories: tasks 1 to 6 and 8 to 11 in `E:\Projects\bambu-mcp`, task 7 in `E:\Projects\multiclaude` on its own branch off `memory-rework`. Commit steps run only after the owner says so explicitly in the conversation.
- Tests: `uv run pytest -q` in bambu-mcp, `python3 -m pytest tests/addons/test_service_notify.py -q` in multiclaude, `node --test tests/tab/` in bambu-mcp.

## Review Focus

1. The owner presses "Стол чистый, печатать" in the tab after the confirmation expired or after pressing it in Telegram: the tab shows "уже нажато" or "истекло" from `press_from_app`'s reason, never a crash or a silent nothing. Tests: task 7 `test_press_from_app_refuses_answered_and_expired`, task 9 `test_confirm_maps_refusal_to_text`.
2. bambu-service restarts or is down while the page is open: the tab shows "Сервис принтера не отвечает, данные на <time>" with the last good snapshot, and recovers on the next poll. Test: task 9 `test_service_down_keeps_last_state`.
3. Pause pressed while the printer link is down or before its first report: the tab shows the `ControlRefused` text, nothing is sent. Test: task 5 `test_pause_refused_when_not_connected`.
4. Many jobs with long names: the snapshot stays under 48 KB with `truncated: true`, the newest kept. Test: task 4 `test_snapshot_cut_to_limit`.
5. A print started on the printer's screen with an empty or non-numeric `task_id`: no control buttons in Telegram, `control` absent from `pending`, the tab hides Продолжить and keeps Стоп (via `/v1/request-stop`). Test: task 3 `test_started_without_task_id_gets_no_control_buttons`.

---

### Task 1: Scoped app tokens and the `/v1/` mount

**Files:**
- Modify: `src/bambu_mcp/config.py` (`Secrets`, `Secrets.load`)
- Modify: `src/bambu_mcp/mcp_server.py` (`BearerTokenMiddleware`, `build_app`, `serve`)
- Test: `tests/test_config.py`, `tests/test_mcp_server.py`

**Interfaces:**
- Produces: `Secrets.app_tokens: dict[str, str]` from env keys `APP_TOKEN_<NAME>` (name lower-cased, same rules as `CLIENT_TOKEN_`). `BearerTokenMiddleware(app, tokens, app_tokens)`: a client token passes only on paths not under `/v1/`, an app token only under `/v1/`, else 401 (unknown token) or 403 (known token, wrong area). Sets `scope["state"]["app_client"]` for app tokens. `APP_PREFIX = "/v1/"`. `build_app(..., app_routes: list[Route] | None = None)` appends them. `serve` starts when either token map is non-empty.

- [ ] **Step 1: Write the failing tests**

```python
def test_app_tokens_loaded(tmp_path):  # tests/test_config.py
    env = tmp_path / ".env"
    env.write_text("BAMBU_ACCESS_CODE=a\nBAMBU_SERIAL=s\nBAMBU_HOST=h\nAPP_TOKEN_MINIAPP=t1\nCLIENT_TOKEN_ASSISTANT=t2\n")
    s = Secrets.load(env)
    assert s.app_tokens == {"miniapp": "t1"}
    assert s.client_tokens == {"assistant": "t2"}

def test_app_token_only_reaches_v1(client_with_tokens):  # tests/test_mcp_server.py
    c = client_with_tokens(client={"assistant": "mcp"}, app={"miniapp": "app"})
    assert c.post("/mcp", headers=bearer("app")).status_code == 403
    assert c.post("/jobs", headers=bearer("app")).status_code == 403
    assert c.get("/v1/snapshot", headers=bearer("mcp")).status_code == 403
    assert c.get("/v1/snapshot", headers=bearer("nope")).status_code == 401
    assert c.get("/v1/snapshot", headers=bearer("app")).status_code != 401
```

`client_with_tokens` is a new fixture building `build_app` with a stub service and one stub `/v1/snapshot` route answering `{}`.

- [ ] **Step 2: Run them, expect FAIL** (`uv run pytest tests/test_config.py tests/test_mcp_server.py -q -k "app_token"`: `AttributeError: app_tokens`).
- [ ] **Step 3: Implement** the fields, the prefix rule in `BearerTokenMiddleware.__call__` (decide by `scope["path"].startswith(APP_PREFIX)`, compare against both maps with no early exit as today), the `app_routes` parameter. Log lines name the client, never the token.
- [ ] **Step 4: Run the whole suite, expect PASS** (`uv run pytest -q`).
- [ ] **Step 5: Commit** `feat(api): app tokens that reach only /v1/` (after the owner's OK).

### Task 2: Remember the Telegram message of each confirmation

**Files:**
- Modify: `src/bambu_mcp/store/db.py` (migration 13, `outbox_add`, `outbox_head`, new methods), `src/bambu_mcp/store/models.py` (`OutboxRow.ref`)
- Modify: `src/bambu_mcp/service.py` (`_enqueue`, `send_next`, `prepare_print`, `_ask_stop`)
- Test: `tests/store/test_db.py` (or the store test file that holds outbox tests), `tests/test_service.py`

**Interfaces:**
- Produces: migration 13 = `ALTER TABLE outbox ADD COLUMN ref TEXT;` plus `CREATE TABLE sent_refs (ref TEXT PRIMARY KEY, message_id TEXT NOT NULL, text TEXT NOT NULL, sent_at REAL NOT NULL);`, `_SCHEMA_VERSION = 13`. `Store.outbox_add(..., ref: str | None = None)`. `OutboxRow.ref: str | None = None`. `Store.record_sent(ref: str, message_id: str, text: str, now: float) -> None` (insert or replace, `text` as sent). `SentRef(message_id: str, text: str)` in `models.py`. `Store.sent_message(ref: str) -> SentRef | None`. `Store.pending_confirmations(now: float) -> list[Confirmation]` (state `pending`, `expires_at > now`, newest first). `Service._enqueue(..., ref: str | None = None)`. Refs: `confirm:<confirmation id>` (the id stays in the database, as today) and `control:<task id>` (task 3). `send_next` calls `record_sent(row.ref, message_id, text, now)` (the text actually sent) after a successful send when `row.ref` is set and the notifier returned an id.

- [ ] **Step 1: Write the failing tests**

```python
def test_sent_ref_recorded(service, notifier):  # tests/test_service.py
    notifier.next_id = "4242"
    result = await service.prepare_print(job_id, 1)   # existing happy-path fixture
    await service.send_next()
    assert service._store.sent_message(f"confirm:{result.confirm_id}").message_id == "4242"

def test_pending_confirmations_skip_used_and_expired(store):
    a = store.add_confirmation(None, "start", job_id=1, plate=1, gate_hash="h", payload_json="{}", now=0)
    b = store.add_confirmation(None, "stop", job_id=None, plate=None, gate_hash=None, payload_json="{}", now=0)
    store.claim_confirmation(b.id, 1)
    assert [c.id for c in store.pending_confirmations(1)] == [a.id]
    assert store.pending_confirmations(10_000) == []

def test_migration_13_keeps_outbox(tmp_path):  # a v12 database with one outbox row opens, the row has ref None
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** `prepare_print` and `_ask_stop` pass `ref=f"confirm:{confirmation.id}"`.
- [ ] **Step 4: Run the suite, expect PASS.**
- [ ] **Step 5: Commit** `feat(notify): remember which message carries a confirmation`.

### Task 3: Pause and Resume buttons, resume only on a press

**Files:**
- Modify: `src/bambu_mcp/printer/link.py` (`RESUME_PRINT`, `_is_allowed`, `Link.resume`)
- Modify: `src/bambu_mcp/messages.py` (`control_buttons`, texts)
- Modify: `src/bambu_mcp/service.py` (`_process`, `_handle_press`, `_handle_report`, new press handlers)
- Modify: `docs/superpowers/specs/2026-11-22-bambu-mcp-design.md` section 9, `multiclaude/addon/rules.md`
- Test: `tests/printer/test_link.py`, `tests/test_messages.py`, `tests/test_service.py`

**Interfaces:**
- Produces: `RESUME_PRINT = {"print": {"sequence_id": "0", "command": "resume", "param": ""}}`, `Link.resume() -> None` (raises `PrinterNotConnected` like `pause`). `PAUSE_BUTTON_TEXT = "⏸ Пауза"`, `RESUME_BUTTON_TEXT = "▶️ Продолжить"`. `control_buttons(task_id: str, paused: bool) -> list[list[Button]]`: one row, `[Button(f"pause:{task_id}", PAUSE_BUTTON_TEXT) or Button(f"resume:{task_id}", RESUME_BUTTON_TEXT), Button(f"stop:ask:{task_id}", STOP_ASK_TEXT)]`. Button index 0 is pause or resume, 1 is stop (task 9 relies on this).
- `Service._control: dict[str, bool]`: task id to the paused variant last put on its message.
- No MCP tool and no `/v1/` endpoint calls `Link.resume`. Only `Service._on_resume_press` does.

Behaviour:
- STARTED of any print whose `task_id` matches `TASK_ID_RE` is enqueued with `control_buttons(task_id, False)` and `ref=f"control:{task_id}"` (replaces `stop_ask_buttons` for own prints). Other prints: no buttons, as today.
- `pause:<task id>` press: refused with an edit-free reply line (enqueued `⏸ <reason>`) when the task is not the current print or not `RUNNING`, else `link.pause()`.
- `resume:<task id>` press: same checks against `PAUSE`, then `link.resume()` once. Refusal texts in Russian next to the stop ones.
- service-notify takes a message's buttons off on any press (`once` is per message) and shows "⏳ <label>…". So after every press on a control message (pause, resume or `stop:ask`, sent or refused), the service edits it back with `notifier.edit(message_id, text=<SentRef.text>, buttons=control_buttons(task, gcode_state == "PAUSE"))`, which makes the buttons pressable again, while the task is still the current print. Once the print is over it edits the message to its text with no buttons.
- `_handle_report`: when the current task has a sent control message (`store.sent_message(f"control:{task}")`) and `gcode_state == "PAUSE"` differs from `_control[task]`, edit its buttons to `control_buttons(task, paused)` (`notifier.edit(message_id, buttons=...)`, which also makes them pressable again). So a pause from the printer's screen or the MCP tool shows Resume too.

- [ ] **Step 1: Write the failing tests**

```python
def test_resume_payload_allowed_and_sent(link, broker):  # test_link.py
    link.resume()
    assert broker.last_payload == RESUME_PRINT

def test_control_buttons_order():
    assert [b.id for b in control_buttons("12", False)[0]] == ["pause:12", "stop:ask:12"]
    assert [b.id for b in control_buttons("12", True)[0]] == ["resume:12", "stop:ask:12"]

async def test_resume_press_sends_once(service, link, notifier):   # PAUSE, task 12
    await service._handle_press(press("resume:12"))
    await service._handle_press(press("resume:12"))
    assert link.sent == ["resume"]

async def test_resume_press_refused_while_running(service, link): ...  # nothing sent
async def test_pause_seen_in_report_switches_buttons(service, notifier): ...  # edit with resume:12 first
async def test_started_without_task_id_gets_no_control_buttons(service, store): ...
async def test_buttons_restored_after_refused_pause(service, notifier): ...  # edit with original text and pause:12
async def test_stop_ask_keeps_control_buttons(service, notifier): ...      # a new stop confirmation and the control message edited back
def test_no_tool_resumes():  # no registered MCP tool name or docstring calls resume
    assert "resume" not in {t.name for t in build_mcp(...)._tool_manager.list_tools()}
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**, update the comment above `PAUSE_PRINT` ("resume only on the owner's press"), `pause` docstrings that say "No resume exists", design section 9 ("Exposed physical controls: pause (while printing), resume (owner's press only) and stop …") and `rules.md` (the assistant cannot resume, the owner presses Продолжить).
- [ ] **Step 4: Run the suite, expect PASS.**
- [ ] **Step 5: Commit** `feat(control): pause and resume buttons, resume only on a press`.

### Task 4: Snapshot and capabilities

**Files:**
- Create: `src/bambu_mcp/app_api.py`
- Test: `tests/test_app_api.py`

**Interfaces:**
- Consumes: `Store.pending_confirmations`, `Store.sent_message` (task 2), `control_buttons` (task 3), `_status` and the serialisers in `mcp_server.py`, `settings.profile`.
- Produces: `SNAPSHOT_MAX = 48 * 1024`. `build_snapshot(service, store, settings, now: float) -> dict` with keys `status`, `link`, `printer` (`model`, `profile`, `bed_mm`, `ams_units`, `ams_slots`, `external_spool`, `camera`, `storage`, `stoppable_states`, `unverified`), `slots` (one entry per profile slot, or one `external` entry without AMS, each with the mapped spool's material, color hex, color name, remaining g), `jobs` (newest 20: `id`, `name`, `kind`, `status`, `plates`, `grams`, `minutes`, `cost`), `history` (newest 12 journal rows: `name`, `ended_at`, `outcome`, `grams`, `cost`), `pending`, `notes`, `currency` (`code`, `symbol`), `truncated`. `pending` entries: `{"message_id": str, "kind": "start" | "stop" | "control", "text": str, "labels": [str], "expires_at": float | None}`, only those with a known message id. `notes`: the service's last 5 app notes (task 5). `build_capabilities(mcp, settings) -> dict`: `tools` (`name`, `summary` = first docstring line, `class` = `read` | `change` | `destructive` from the annotations), `buttons` (from `BUTTONS`, a table in `messages.py` that names each Telegram button kind and what it does, and which the senders use for their texts), `printer` (as in the snapshot).
- Cutting order when over 48 KB: history to 5, then jobs from the oldest, then `truncated: True`.

- [ ] **Step 1: Write the failing tests**

```python
def test_snapshot_has_no_secrets(api_env):
    blob = json.dumps(build_snapshot(*api_env, now=NOW))
    for secret in (SERIAL, HOST, ACCESS_CODE, CLIENT_TOKEN, APP_TOKEN, str(api_env.settings.data_dir)):
        assert secret not in blob

def test_snapshot_cut_to_limit(api_env_with_300_long_jobs):
    snap = build_snapshot(*api_env_with_300_long_jobs, now=NOW)
    assert len(json.dumps(snap, ensure_ascii=False).encode()) <= SNAPSHOT_MAX
    assert snap["truncated"] and snap["jobs"][0]["id"] == NEWEST_ID

def test_a1_profile_one_external_slot(api_env_a1):
    assert [s["slot"] for s in build_snapshot(*api_env_a1, now=NOW)["slots"]] == ["external"]

def test_pending_lists_sent_unexpired_confirmations(api_env): ...  # start with message id, labels from start_buttons
def test_pending_skips_unsent(api_env): ...                         # no sent_refs row: absent
def test_capabilities_lists_every_tool(api_env):
    caps = build_capabilities(build_mcp(...), settings)
    assert {t["name"] for t in caps["tools"]} == {t.name for t in mcp._tool_manager.list_tools()}
    assert {t["class"] for t in caps["tools"]} <= {"read", "change", "destructive"}
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Reuse the MCP serialisers rather than new shapes where one exists. `pending` for `control` comes from the current task's `control:<task>` ref with labels of `control_buttons(task, paused)`.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat(api): snapshot and capabilities for the Mini App`.

### Task 5: `/v1/` endpoints

**Files:**
- Modify: `src/bambu_mcp/app_api.py` (routes), `src/bambu_mcp/service.py` (`start_prepare`, `app_notes`, `send_snapshot`), `src/bambu_mcp/mcp_server.py` (`build_app` passes `app_routes(...)`)
- Test: `tests/test_app_api.py`

**Interfaces:**
- Consumes: task 1 mount, task 4 builders, `Service.pause_print`, `Service.request_stop`, `Service.prepare_print`.
- Produces: `app_routes(service, store, settings, mcp) -> list[Route]`:
  - `GET /v1/snapshot` -> 200 snapshot.
  - `GET /v1/capabilities` -> 200 capabilities.
  - `POST /v1/pause` -> 200 `{"sent": "pause"}` or 409 `{"error": <ControlRefused text>}`.
  - `POST /v1/request-stop` -> 202 `{"asked": true}` or 409.
  - `POST /v1/prepare` body `{"job_id": int, "plate": int}` -> 202 `{"pending": true}`, 422 on a bad body. Runs `service.start_prepare(job_id, plate)`: a background task, one at a time (a second gets 409 "уже готовлю печать"), its refusal or error text added to `service.app_notes`.
  - `POST /v1/snapshot-to-telegram` -> 202, or 409 while one is queued. `Service.send_snapshot()` enqueues kind `snapshot` with `snapshot=True`, text "📷 Снимок", dedupe `snap|<minute>`.
- `Service.app_notes: deque[dict]` (maxlen 5) of `{"at": float, "text": str}`, Russian texts only, never exception details.

- [ ] **Step 1: Write the failing tests**

```python
def test_pause_refused_when_not_connected(app_client, service):
    service.link_status = LinkStatus.DISCONNECTED
    r = app_client.post("/v1/pause", headers=APP)
    assert r.status_code == 409 and "not connected" in r.json()["error"]

def test_prepare_runs_in_background(app_client, service):
    r = app_client.post("/v1/prepare", json={"job_id": 7, "plate": 1}, headers=APP)
    assert r.status_code == 202 and r.json() == {"pending": True}
    # a second while the first runs
    assert app_client.post("/v1/prepare", json={"job_id": 7, "plate": 1}, headers=APP).status_code == 409

def test_prepare_refusal_becomes_note(...): ...   # PrintRefused -> note with its Russian line
def test_v1_has_no_start_resume_stop_route(app):
    paths = {r.path for r in app.router.routes}
    assert not paths & {"/v1/start", "/v1/resume", "/v1/stop"}
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the suite, expect PASS.**
- [ ] **Step 5: Commit** `feat(api): /v1/ endpoints for the Mini App`.

### Task 6: `via` on presses (bambu side)

**Files:**
- Modify: `src/bambu_mcp/notify.py` (`Press.via`, parsing), `src/bambu_mcp/service.py` (start and stop outcome log lines)
- Test: `tests/test_notify.py`

**Interfaces:**
- Produces: `Press.via: str = "telegram"`, read from the press JSON's `via` when it is `"telegram"` or `"miniapp"`, else `"telegram"`. Start and stop log lines add `via=<value>`. Behaviour is otherwise identical: a press is a press.

- [ ] **Step 1: Test** `test_press_via_parsed` (a press JSON with `"via": "miniapp"` gives `Press.via == "miniapp"`, a missing or odd value gives `"telegram"`).
- [ ] **Step 2: FAIL. Step 3: implement. Step 4: PASS. Step 5: Commit** `feat(notify): note where a press came from`.

### Task 7: service-notify `press_from_app` (multiclaude, generic)

Repository `E:\Projects\multiclaude`, branch `feat/service-notify-press-from-app` off `memory-rework`.

**Files:**
- Modify: `lib/multiclaude/addons/service_notify/taps.py`, `lib/multiclaude/addons/service_notify/api.py` (press fields), `docs/service-notify/DESIGN.md`
- Test: `tests/addons/test_service_notify.py`

**Interfaces:**
- Produces: `taps.press_from_app(user, service: str, message_id: int, index: int, now: float) -> str | None`: None when queued, else the reason `"unknown"`, `"other_service"`, `"other_chat"`, `"no_button"`, `"answered"`, `"expired"`. Same checks and the same lock as `press_for`, chat is `str(user.chat_id)`. The queued press carries `"via": "miniapp"` (and `take_tap` presses `"via": "telegram"`). The Telegram edit text is `pending(label) + " (Mini App)"`. Then `notify(service)`. Runs synchronously (the caller is already in a background thread). `GET /v1/presses` includes `via`.
- `DESIGN.md`: a section "Presses from the Mini App" saying who may call it (router add-ins for their own person), that button ids never leave the router, and that the Telegram message shows the press.

- [ ] **Step 1: Write the failing tests**

```python
def test_press_from_app_queues_with_via(svc_message):
    assert taps.press_from_app(owner, "bambu", svc_message.id, 0, NOW) is None
    press = store.Service("bambu").take_presses(NOW)[0]
    assert press["via"] == "miniapp" and press["button_id"] == svc_message.buttons[0]["id"]

def test_press_from_app_refuses_answered_and_expired(svc_message):
    taps.press_from_app(owner, "bambu", svc_message.id, 0, NOW)
    assert taps.press_from_app(owner, "bambu", svc_message.id, 1, NOW) == "answered"
    assert taps.press_from_app(owner, "bambu", other_message.id, 0, LATER_THAN_EXPIRY) == "expired"

def test_press_from_app_refuses_other_service_and_chat(...): ...
def test_tap_after_app_press_is_ignored(...): ...   # Telegram tap after an app press: no second press queued
```

- [ ] **Step 2: Run, expect FAIL** (`python3 -m pytest tests/addons/test_service_notify.py -q -k from_app`).
- [ ] **Step 3: Implement.** Factor the shared checks of `press_for` into one function both use, so they cannot drift.
- [ ] **Step 4: Run the multiclaude suite, expect PASS** (`python3 -m pytest -q`).
- [ ] **Step 5: Commit** on the branch `service-notify: presses from the Mini App` (after the owner's OK).

### Task 8: Router module client (pure)

**Files:**
- Create: `multiclaude/router/bambu/client.py`
- Test: `tests/router/test_client.py`, `tests/router/conftest.py` (puts `multiclaude/router` on `sys.path`)

**Interfaces:**
- Produces: `CONFIG = Path.home() / ".config/bambu/miniapp.json"` (`{"url": "http://127.0.0.1:8765", "token": "..."}`, mode 600). `load_config(path: Path = CONFIG) -> tuple[str, str] | None` (None when missing, unreadable or not 600). `class AppClient(url: str, token: str, timeout: float = 5.0)`: `snapshot() -> dict`, `capabilities() -> dict`, `post(path: str, body: dict | None = None) -> tuple[int, dict]`. Raises `ServiceDown(str)` on connection errors and 5xx, with no URL or token in the message. stdlib `urllib.request` only.
- `STATE_MAX = 60 * 1024`. `tab_state(snapshot: dict | None, last_ok: float | None, now: float, down: str | None) -> dict`: `{"snapshot", "updated", "down"}` where `down` is a Russian line "Сервис принтера не отвечает, данные на HH:MM" when the last read failed. Cut to `STATE_MAX` by dropping `history` then `jobs` from the end.
- `poll_every(snapshot: dict | None) -> int`: 5 when `snapshot["status"]["gcode_state"]` is in `snapshot["printer"]["stoppable_states"]`, else 30.

- [ ] **Step 1: Tests** with a local `http.server` stub: `test_snapshot_sends_bearer`, `test_service_down_message_has_no_token`, `test_load_config_refuses_group_readable`, `test_tab_state_cut`, `test_poll_every_follows_profile` (an `a1`-like printer block with its own `stoppable_states`).
- [ ] **Step 2: FAIL. Step 3: implement. Step 4: PASS. Step 5: Commit** `feat(router): client for the Mini App tab`.

### Task 9: Router module hooks

**Files:**
- Create: `multiclaude/router/bambu/__init__.py`
- Test: `tests/router/test_hooks.py` (a fake `multiclaude` package in `conftest.py`: `multiclaude.addons.enabled`, `multiclaude.addons.chat.is_open/feed_add`, `multiclaude.addons.service_notify.taps.press_from_app`, `multiclaude.log.log`)

**Interfaces:**
- Consumes: task 8, task 7's `press_from_app`, the chat add-in's `is_open(user)` and `feed_add(user, event)`.
- Produces (hooks named as the chat add-in calls them):
  - `chat_tabs(user) -> list[dict]`: `[{"id": "print", "title": "Печать", "module": "print.js"}]` for `role == "owner"`, else `[]`.
  - `on_tick(desk, now)`: for each owner whose page is open, start one background read when due (`poll_every`), never twice at once. Put `{"kind": "addon_state", "addon": "bambu", "state": tab_state(...)}` only when changed (ignoring `updated`). Keep the last good snapshot per person.
  - `chat_backfill(user) -> list[dict]`: the last state as one `addon_state`, or `[]`.
  - `chat_tab_action(user, action, values) -> dict`: owner only, else `{"error": "Только для владельца."}`. Actions: `pause`, `snapshot`, `stop` (`/v1/request-stop`), `prepare` (`job_id`, `plate` as ints), `capabilities` (returns `{"text": json}` of the capabilities for the tab), `confirm` (`message_id`, `index`). Answers `{"text": <Russian line>}` or `{"error": <Russian line>}` within the client's 5 s timeout. After a successful action the next read is due at once.
  - `confirm` accepts only a `message_id` present in the person's last snapshot's `pending` and an index within its `labels`, then calls `press_from_app(user, "bambu", int(message_id), index, time.time())` and maps reasons: `answered` "Уже нажато.", `expired` "Кнопка истекла, нажми «Печать» снова.", others "Не получилось нажать: <reason>.".
- Memory: per person one state and one snapshot, nothing else.

- [ ] **Step 1: Tests**: `test_tab_only_for_owner`, `test_non_owner_gets_no_state` (AC1.10), `test_state_put_only_on_change`, `test_service_down_keeps_last_state`, `test_confirm_rejects_unknown_message`, `test_confirm_maps_refusal_to_text`, `test_tick_never_blocks` (a stub client that sleeps 2 s: `on_tick` returns in under 50 ms).
- [ ] **Step 2: FAIL. Step 3: implement** following `multiclaude-local-gpu/lib/local_gpu/__init__.py` (`chat()`, `_open`, `_looking`, `_look`). **Step 4: PASS. Step 5: Commit** `feat(router): the Print tab's hooks`.

### Task 10: Install, setup and teardown

**Files:**
- Modify: `multiclaude/install.sh`, `multiclaude/addon/setup`, `multiclaude/addon/teardown`, `multiclaude/addon/README.md`
- Create: `multiclaude/addon/relay-public/` (task 11 fills it)
- Test: `tests/test_addon.py`

**Interfaces:**
- install: copies `multiclaude/router/bambu/*.py` to `$ROOT/lib/multiclaude/addons/bambu/`, `addon/relay-public/` to `$FOLDER/relay-public/`, adds both to `.git/info/exclude`, and appends `ALL += ("bambu",)  # bambu-mcp install.sh` at the end of `$ROOT/lib/multiclaude/addons/__init__.py` when no line ends with that comment. `--uninstall` removes the files and that line. The line sits at the end of the file, far from local-gpu's `core.patch` hunk, so both apply in any order.
- setup: when `.env` lacks `APP_TOKEN_MINIAPP`, adds a random one, and writes `$CONF_DIR/miniapp.json` (`{"url": "http://127.0.0.1:8765", "token": ...}`, mode 600) from it, every run. Restarts the service as it does today.
- teardown: removes `miniapp.json`. The token in `.env` stays (as other settings do).

- [ ] **Step 1: Tests**: `test_install_adds_router_module_and_all_line`, `test_install_twice_adds_line_once`, `test_uninstall_removes_line_and_module`, `test_install_after_local_gpu_patch` (a fixture `__init__.py` with local-gpu's changed `ALL` line keeps it), `test_setup_writes_miniapp_json_600`, `test_teardown_removes_miniapp_json`.
- [ ] **Step 2: FAIL. Step 3: implement. Step 4: PASS. Step 5: Commit** `feat(addon): install the Print tab`.

### Task 11: The tab module `print.js`

**Files:**
- Create: `multiclaude/addon/relay-public/print.js`
- Test: `tests/tab/print.test.mjs`

**Interfaces:**
- Consumes: the chat page's tab API as `github.js` uses it: `mount(root, api)` with `api.el`, `api.onState(fn)`, `api.request(action, values) -> Promise<{text} | {error}>`, `api.toast(text)`. State shape: task 8's `tab_state` around task 4's snapshot.
- Produces: `export function mount(root, api)` and, for tests, pure `export function view(state) -> ViewModel` where `ViewModel` lists the sections and the visible buttons: `buttons` ⊆ `["snapshot", "pause", "resume", "stop"]`, `confirm` (the newest `pending` of kind `start` or `stop`, else null), `slots`, `jobs`, `history`, `down`.
- Rules in `view`: `pause` only while `gcode_state == "RUNNING"`. `resume` only while `PAUSE` and a `control` pending exists (its index 0). `stop` only while `gcode_state` is in `printer.stoppable_states`. A `start` or `stop` pending opens the confirmation sheet with its `text` and `labels`. Pressing a label sends `confirm {message_id, index}`. Продолжить sends `confirm` on the control message with index 0. Печать on a job plate sends `prepare`, then the sheet opens when the pending appears. Capabilities opens a sheet from `capabilities`.
- Russian UI strings as in the spec's sketch (6.6). No colors or icons from external URLs.

- [ ] **Step 1: Tests** (`node --test tests/tab/`): `pause only while running`, `resume needs a control pending`, `a1 state shows one external slot`, `start pending opens the sheet with its labels`, `down state shows the banner and keeps the data`.
- [ ] **Step 2: FAIL. Step 3: implement. Step 4: PASS**, and `gzip -c multiclaude/addon/relay-public/print.js | wc -c` is under 61440.
- [ ] **Step 5: Commit** `feat(tab): the Print tab`.

### Task 12: Docs and the owner's check on the phone

**Files:**
- Modify: `docs/superpowers/specs/2026-11-22-bambu-mcp-design.md` section 16 (the spec's 6.3 wording), `README.md` (the tab), `multiclaude/addon/rules.md` (the tab exists, the assistant still cannot start, resume or stop)
- Create: `docs/miniapp-phase1-check.md`: the owner's steps for AC1.3, AC1.4, AC1.5, AC1.7 on workshop (install, `assistant addon enable bambu`, `assistant relay deploy`, open the tab, each check and what to see)

- [ ] **Step 1:** Write the docs. Grep them for `—` and `;` in prose: none.
- [ ] **Step 2:** Run all suites: `uv run pytest -q`, `node --test tests/tab/`, and task 7's in multiclaude. All PASS.
- [ ] **Step 3: Commit** `docs: the Print tab, section 16 updated` (after the owner's OK). Deploying to workshop is a separate step with the owner's OK (workshop-server skill).
