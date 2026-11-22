# bambu-mcp Phase 1 (monitor) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** an always-on `bambu-service` on workshop that watches the P2S read-only, sends deduplicated Telegram notifications with camera snapshots, keeps a print journal, spool/plate/tariff inventory with cost calculation, and exposes MCP read and inventory tools to the owner only.

**Architecture:** one asyncio Python process. A read-only MQTT link feeds a merged `PrinterState`. An event detector turns state changes into deduplicated events. Events go to a notifier (the multiclaude service-notify add-in, or a logging stand-in), the journal (SQLite) and the camera. A FastMCP server over Streamable HTTP with per-client bearer tokens exposes read and inventory tools.

**Tech Stack:** Python >= 3.12, uv, pytest, paho-mqtt >= 2, httpx, mcp (official SDK, FastMCP), uvicorn, sqlite3 (stdlib), PyAV or ffmpeg for the camera (decided by the phase 0 report).

**Spec:** `docs/superpowers/specs/2026-11-22-bambu-mcp-design.md`. Inputs: `docs/phase0-report.md` and `tests/fixtures/p2s/*.json` from phase 0. Notify API: `E:\Projects\multiclaude\.claude\worktrees\chat-miniapp-8e21d2\docs\service-notify\DESIGN.md` ("The API", "Python client").

## Global Constraints

- Everything in the repo in English. Telegram message texts in Russian.
- MIT licence. Never commit Bambu Studio profiles, HMS text tables, `.env`, `data/`, or real payloads that were not redacted.
- Phase 1 never sends a printer command except `{"info": {"sequence_id": "0", "command": "get_version"}}` and `{"pushing": {"sequence_id": "0", "command": "pushall"}}`.
- The access code, serial, tokens and camera URL never appear in logs, exceptions surfaced to clients, MCP results, Telegram text, or a process command line.
- Owner only: MCP clients are the owner's Claude Code on the PC and the owner's multiclaude assistant. Notifications go to the owner.
- Memory: steady RSS about 100 MB, systemd `MemoryMax=150M`.
- Listen on `192.168.88.11` (PC client) and `127.0.0.1` (assistant) only.
- Prices and costs in UAH, rounded to 2 decimals for display, stored unrounded.
- Default tariff 4.32 UAH/kWh. Two-zone night hours 23:00 to 07:00 local time (Europe/Kyiv). PLA average power 200 W. Heat-up allowance 0.05 kWh per print.
- Commits: the owner's CLAUDE.md says never commit without an explicit OK in the session. Each "Commit" step means: stage, then ask the owner (batching several tasks into one ask is fine).

## Review Focus

- MQTT reconnect after the printer reboots or Wi-Fi drops mid-print: monitoring resumes, no second "started" and no lost "finished". (Test in Task 6.)
- Service restart mid-print: the running print keeps its journal row and identity, and dedupe survives the restart. (Test in Task 6.)
- The access code inside an exception text (paho, PyAV or ffmpeg error messages carry URLs): redacted before logging. (Test in Task 2 and Task 9.)
- Partial reports carrying only a few fields: merged into state, never resetting other fields to defaults. (Test in Task 3.)
- An unknown `gcode_state` or an HMS code with no known text: notified with the raw code, no crash. (Tests in Task 3 and Task 4.)

---

### Task 1: Repository skeleton and CI

**Files:**
- Create: `pyproject.toml`, `LICENSE`, `README.md`, `.gitignore`, `src/bambu_mcp/__init__.py`, `tests/test_smoke.py`, `.github/workflows/tests.yml`

**Interfaces:**
- Produces: package `bambu_mcp` (src layout), console script `bambu-service = bambu_mcp.service:main`.

- [ ] **Step 1:** `git init` in `E:\Projects\bambu-mcp` (keep the existing `docs/`). `.gitignore`: `.env`, `data/`, `spike/.env`, `spike/raw/`, `spike/profiles/`, `spike/certs/`, `.venv/`, `__pycache__/`.
- [ ] **Step 2:** Write `tests/test_smoke.py::test_package_imports` asserting `bambu_mcp.__version__ == "0.1.0"`.
- [ ] **Step 3:** Run `uv run pytest -q`. Expected: FAIL (no package).
- [ ] **Step 4:** Create `pyproject.toml` (hatchling, `requires-python = ">=3.12"`, deps `paho-mqtt>=2`, `httpx`, `mcp`, `uvicorn`, dev group `pytest`, `pytest-asyncio`), `src/bambu_mcp/__init__.py` with `__version__ = "0.1.0"`, MIT `LICENSE` (Copyright (c) 2026 Hustilin-Y), a short `README.md` (what it is, phases, link to the spec).
- [ ] **Step 5:** Run `uv run pytest -q`. Expected: PASS.
- [ ] **Step 6:** `.github/workflows/tests.yml`: ubuntu-24.04, `astral-sh/setup-uv`, `uv run pytest -q`.
- [ ] **Step 7:** Commit (ask first): `Skeleton: package, MIT licence, CI`.

### Task 2: Settings and secret redaction

**Files:**
- Create: `src/bambu_mcp/config.py`, `src/bambu_mcp/redact.py`, `config.example.toml`, `tests/test_config.py`, `tests/test_redact.py`

**Interfaces:**
- Produces:
  - `Secrets` dataclass: `access_code: str`, `serial: str`, `host: str`, `notify_token: str | None`, `client_tokens: dict[str, str]` (client name -> token). `Secrets.load(env_path: Path) -> Secrets`.
  - `Settings` dataclass: `data_dir: Path`, `listen: list[tuple[str, int]]`, `tls_mode: Literal["ca", "ca-no-hostname", "pin"]`, `ca_file: Path | None`, `cert_sha256: str | None`, `notify_url: str | None`, `camera_backend: Literal["pyav", "ffmpeg"]`, `power_w: dict[str, float]`, `heatup_kwh: float`, `timezone: str`. `Settings.load(toml_path: Path) -> Settings`.
  - `redact(text: str, secrets: Iterable[str]) -> str` and `RedactingFilter(logging.Filter)` built from the secrets, installed on the root logger by `install_redaction(secrets: Secrets) -> None`.

- [ ] **Step 1:** Tests:
  - `test_redact_replaces_every_secret`: `redact("rtsps://bblp:12345678@10.0.0.2:322", ["12345678"]) == "rtsps://bblp:***@10.0.0.2:322"`.
  - `test_redact_ignores_empty_secret`: an empty string in the list changes nothing.
  - `test_filter_redacts_exception_text`: log `logger.exception` of `RuntimeError("auth failed for 12345678")` with the filter installed; captured output contains `***` and not `12345678` (covers `exc_info` formatting, not only `msg`).
  - `test_secrets_load_requires_access_code_and_serial`: missing `BAMBU_ACCESS_CODE` raises `ValueError` naming the variable, not its value.
  - `test_settings_defaults`: `power_w == {"PLA": 200.0}`, `heatup_kwh == 0.05`, `timezone == "Europe/Kyiv"`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement. Env keys: `BAMBU_ACCESS_CODE`, `BAMBU_SERIAL`, `BAMBU_HOST`, `NOTIFY_TOKEN`, `CLIENT_TOKEN_<NAME>`. The filter rewrites `record.msg`, `record.args` and the formatted `exc_text`.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit (ask first).

### Task 3: Printer state from reports

**Files:**
- Create: `src/bambu_mcp/printer/state.py`, `tests/printer/test_state.py`
- Uses: `tests/fixtures/p2s/*.json` from phase 0 (`version`, `pushall_idle`, `running_layer0/1/2`, `running_mid`, `finish`, `gcode_line`). The P2S sends full reports only. Pause, HMS, failed, cancelled and partial-update cases were not captured: build them under `tests/fixtures/synthetic/` by editing a copy of a real fixture, and mark those tests `@pytest.mark.synthetic`. Heating and levelling appear as `RUNNING` with `layer_num 0` (no `PREPARE`). `gcode_line` messages leave state unchanged.

**Interfaces:**
- Produces:
  - `Tray(ams: int, slot: int, tray_type: str | None, color_rgba: str | None, remain_pct: int | None)` (`remain -1` -> `None`).
  - `Hms(attr: int, code: int)`.
  - `PrinterState` (frozen dataclass): `gcode_state: str` (upper case, `"UNKNOWN"` when absent or not in the known list), `percent: int | None`, `remaining_min: int | None`, `layer: int | None`, `total_layers: int | None`, `subtask_name: str | None`, `task_id: str | None`, `gcode_start_time: str | None`, `print_error: int`, `hms: tuple[Hms, ...]`, `nozzle_diameter: float | None`, `trays: tuple[Tray, ...]`, `raw_unknown_state: str | None`.
  - `PrinterState.empty() -> PrinterState`, `apply(state: PrinterState, report: dict) -> PrinterState` (pure).
  - `identity(state) -> tuple[str | None, str | None, str | None]` = (`subtask_name`, `task_id`, `gcode_start_time`).

- [ ] **Step 1:** Tests:
  - `test_full_report_idle`: `apply(empty, pushall_idle)` gives `gcode_state == "IDLE"`, trays parsed, every non-RFID tray `remain_pct is None`, `nozzle_diameter == 0.4`.
  - `test_partial_report_keeps_other_fields`: apply `pushall_idle`, then `{"print": {"mc_percent": 5}}`: `percent == 5` and trays and nozzle unchanged.
  - `test_nozzle_from_device_info_or_legacy_field`: `{"print": {"device": {"nozzle": {"info": [{"id": 0, "diameter": 0.6}]}}}}` -> 0.6, and `{"print": {"nozzle_diameter": "0.4"}}` -> 0.4.
  - `test_unknown_gcode_state`: `{"print": {"gcode_state": "WEIRD"}}` -> `gcode_state == "UNKNOWN"`, `raw_unknown_state == "WEIRD"`, no exception.
  - `test_identity_from_running_fixture`: from `running_layer1` the identity's `subtask_name` equals the fixture's.
  - `test_non_dict_or_info_messages_ignored`: a `{"info": {...}}` version reply leaves state unchanged.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Known states: `FAILED, FINISH, IDLE, INIT, OFFLINE, PAUSE, PREPARE, RUNNING, SLICING`. Nozzle: `print.device.nozzle.info` entry with `id == 0` first, then `print.nozzle_diameter`.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit (ask first).

### Task 4: HMS decoding

**Files:**
- Create: `src/bambu_mcp/printer/hms.py`, `scripts/extract_hms.py`, `tests/printer/test_hms.py`

**Interfaces:**
- Consumes: `Hms` from Task 3.
- Produces:
  - `hms_code(h: Hms) -> str` -> `"0300_0100_0001_0007"` format (`attr` high and low 16 bits, `code` high and low 16 bits, 4 hex digits each, upper case).
  - `severity(h: Hms) -> Literal["fatal", "serious", "common", "info", "unknown"]` from `code >> 16` (1, 2, 3, 4).
  - `HmsTexts.load(path: Path | None) -> HmsTexts`, `HmsTexts.text(code: str) -> str | None`.
  - `describe(h: Hms, texts: HmsTexts) -> str` -> `"HMS_0300_0100_0001_0007 (fatal): <text>"` or `"HMS_0300_0100_0001_0007 (fatal): no description"`.
  - `scripts/extract_hms.py <bambu studio install dir> <out json>`: reads the installed Studio's HMS resource for the P2S into `{code: english text}`. Output lives in `data_dir`, never in git.

- [ ] **Step 1:** Tests:
  - `test_code_format`: `Hms(attr=50331904, code=65543)` -> `"0300_0100_0001_0007"`.
  - `test_severity_levels`: `code=0x00010007` -> fatal, `0x00020001` -> serious, `0x00030001` -> common, `0x00040001` -> info, `0x00090001` -> unknown.
  - `test_describe_without_texts`: `HmsTexts.load(None)` -> text ends with `no description`.
  - `test_describe_with_texts`: a tmp JSON `{"0300_0100_0001_0007": "The heatbed temperature is abnormal"}` -> text contains it.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. For `extract_hms.py`, locate the HMS file in the install by searching `resources` for `hms` JSON (record the found path in the script's docstring when run against the real install).
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit (ask first).

### Task 5: Read-only MQTT link

**Files:**
- Create: `src/bambu_mcp/printer/link.py`, `tests/printer/test_link.py`

**Interfaces:**
- Consumes: `Secrets`, `Settings` (Task 2).
- Produces:
  - `READ_COMMANDS: Final = (GET_VERSION, PUSH_ALL)` (the two payloads from Global Constraints).
  - `class LinkStatus(Enum): CONNECTING, CONNECTED, AUTH_FAILED, DISCONNECTED`.
  - `class PrinterLink`: `__init__(secrets, settings, on_report: Callable[[dict], None], on_status: Callable[[LinkStatus], None], client_factory=paho.mqtt.client.Client)`, `start() -> None`, `stop() -> None`, `request_full_report() -> None`. Private `_publish(payload: dict)` raises `ValueError` for any payload not in `READ_COMMANDS`.
  - `build_tls_context(settings) -> ssl.SSLContext` for the three `tls_mode`s (`pin`: no CA, verify by comparing the peer certificate's SHA-256 with `cert_sha256` in the connect callback and disconnect on mismatch).

- [ ] **Step 1:** Tests with a fake paho client object:
  - `test_subscribes_and_requests_full_report_on_connect`: on connect, subscribes to `device/SERIAL/report`, publishes `GET_VERSION` then `PUSH_ALL` to `device/SERIAL/request`.
  - `test_refuses_non_read_command`: `_publish({"print": {"command": "stop"}})` raises `ValueError`.
  - `test_auth_failure_stops_reconnecting`: connect result "not authorised" -> `on_status(AUTH_FAILED)`, `loop_stop` called, no further connect attempts.
  - `test_network_error_reconnects_with_backoff`: disconnect with a network reason -> reconnect delays follow 5, 10, 20 ... capped at 300 s (inject a fake clock or sleep).
  - `test_malformed_json_is_logged_and_skipped`: a non-JSON payload does not call `on_report` and does not raise.
  - `test_tls_pin_mismatch_disconnects`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement with paho-mqtt 2 (`CallbackAPIVersion.VERSION2`), username `bblp`, password access code, port 8883.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit (ask first).

### Task 6: Event detection with persistent dedupe

**Files:**
- Create: `src/bambu_mcp/events.py`, `tests/test_events.py`

**Interfaces:**
- Consumes: `PrinterState`, `identity`, `Hms`, `severity` (Tasks 3, 4).
- Produces:
  - `EventKind(StrEnum)`: `started, first_layer_done, paused, resumed, hms_warning, hms_error, finished, failed, cancelled`.
  - `Event(kind: EventKind, identity: tuple, at: datetime, state: PrinterState, hms: Hms | None)`, `Event.dedupe_key -> str` = `f"{subtask_name}|{task_id}|{gcode_start_time}|{kind}"` plus `|{hms_code}` for HMS events.
  - `EventDetector(seen: SeenStore)`, `feed(prev: PrinterState, new: PrinterState, at: datetime) -> list[Event]`, which drops events whose `dedupe_key` is in `seen` and records new ones.
  - `SeenStore` protocol: `seen_contains(key: str) -> bool`, `seen_add(key: str) -> None`. Implemented by `Store` in Task 7. Tests use a small in-memory fake.
- Rules: `started` when the state enters `PREPARE` or `RUNNING` from `IDLE/FINISH/FAILED/UNKNOWN` with a non-empty identity. `first_layer_done` when `layer` reaches 2. `paused`/`resumed` on `PAUSE` in/out. `finished` on `FINISH`. `cancelled` on `FAILED` with `print_error == 50348044`, `failed` on other `FAILED`. `hms_error` for severity fatal or serious, `hms_warning` for common. Info-level HMS ignored.

- [ ] **Step 1:** Tests:
  - `test_print_lifecycle`: feed fixture sequence idle -> prepare -> running_layer1 -> running_layer2 -> finish: events `started, first_layer_done, finished` in order.
  - `test_reconnect_does_not_repeat_started`: after `running_layer2`, feed `PrinterState.empty()` (reconnect) then `running_layer2` again: no events.
  - `test_restart_keeps_dedupe`: two detectors sharing one `SeenStore`, the second fed the same running sequence after the first: no repeated `started`.
  - `test_cancel_vs_fail`: `FAILED` with `print_error=50348044` -> `cancelled`, with `print_error=0` -> `failed`.
  - `test_hms_error_once_per_code`: the same fatal HMS in five consecutive reports -> one `hms_error`.
  - `test_unknown_state_yields_no_event`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement `feed` as a pure comparison of `prev` and `new` plus the dedupe store.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit (ask first).

### Task 7: SQLite store

**Files:**
- Create: `src/bambu_mcp/store/db.py`, `src/bambu_mcp/store/schema.sql`, `tests/store/test_db.py`

**Interfaces:**
- Produces `Store(path: Path)` with `migrate() -> None` (versioned via `PRAGMA user_version`) and:
  - `seen_contains(key) -> bool`, `seen_add(key) -> None` (satisfies `SeenStore`, keys older than 30 days pruned on start).
  - `get_meta(key: str) -> str | None`, `set_meta(key: str, value: str) -> None` (small key-value table, e.g. `tariff_reminded_on`).
  - Spools: `add_spool(brand, material, color_name, color_hex, filament_preset: str | None, price_uah: float, net_g: float) -> int`, `update_spool(id, **fields)`, `spools(active_only=True) -> list[Spool]`, `adjust_spool(id, used_g: float) -> None`, `set_spool_weight(id, remaining_g: float) -> None`.
  - Slots: `map_slot(ams: int, slot: int, spool_id: int | None) -> None`, `slot_spool(ams, slot) -> Spool | None`.
  - Plates: `add_plate(name, bed_type) -> int`, `plates() -> list[Plate]`, `set_default_plate(id)`.
  - Tariffs: `add_tariff(valid_from: date, day_uah: float, night_uah: float | None) -> None`, `tariff_on(d: date) -> Tariff`. `migrate()` seeds `4.32` valid from `2024-06-01`, no night rate.
  - Prints: `open_print(identity, started_at, source: Literal["external", "job"]) -> int`, `close_print(id, outcome, ended_at, actual_min)`, `set_print_fields(id, **fields)` (spool_id, grams, kwh, cost_filament_uah, cost_power_uah, fit), `print_by_identity(identity) -> PrintRow | None`, `history(part: str | None, limit: int) -> list[PrintRow]`.
- Dataclasses `Spool`, `Plate`, `Tariff`, `PrintRow` in `store/models.py`.

- [ ] **Step 1:** Tests:
  - `test_migrate_is_idempotent_and_seeds_tariff`: `migrate()` twice, `tariff_on(date(2026, 11, 22)).day_uah == 4.32`.
  - `test_tariff_history`: add `4.80` from `2027-01-01`: `tariff_on(2026-12-31) == 4.32`, `tariff_on(2027-01-01) == 4.80`.
  - `test_spool_remaining`: add a 1000 g spool, `adjust_spool(id, 14.2)` -> remaining 985.8, `set_spool_weight(id, 900)` -> 900.
  - `test_slot_mapping_replaces`.
  - `test_print_open_close_by_identity`.
  - `test_seen_survives_reopen`: add a key, open a new `Store` on the same file, `seen_contains` is true.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement with stdlib `sqlite3`, WAL mode, one connection per thread.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit (ask first).

### Task 8: Cost calculation

**Files:**
- Create: `src/bambu_mcp/cost.py`, `tests/test_cost.py`

**Interfaces:**
- Consumes: `Tariff`, `Spool` (Task 7), `Settings.power_w`, `heatup_kwh`, `timezone` (Task 2).
- Produces:
  - `print_kwh(minutes: float, material: str, settings) -> float` = `minutes / 60 * power_w[material] / 1000 + heatup_kwh`. Unknown material falls back to `power_w["PLA"]` and logs a warning.
  - `power_cost(start: datetime, end: datetime, kwh: float, tariff: Tariff, settings) -> float`: single-rate tariff -> `kwh * day_uah`. Two-zone: heat-up kWh at the start's rate, the rest split by minutes inside 23:00 to 07:00 (night) and outside (day), in `settings.timezone`.
  - `filament_cost(grams: float, spool: Spool | None, fallback_per_kg: dict[str, float], material: str) -> float | None` (None when no price is known).
  - `CostLine(filament_uah: float | None, power_uah: float, kwh: float, tariff: Tariff)` dataclass with `total_uah -> float` (power only when filament is None).
  - `tariff_check_due(last_reminded: date | None, today: date) -> bool`: true when never reminded or 91 days or more have passed.

- [ ] **Step 1:** Tests:
  - `test_pla_two_hours_single_rate`: `print_kwh(120, "PLA") == 0.45`, cost with 4.32 == `1.944` (`round(..., 3)`).
  - `test_two_zone_split`: 22:00 to 00:00 Kyiv time, PLA, tariff day 4.32 night 2.16: day part `0.25 * 4.32 = 1.08`, night `0.2 * 2.16 = 0.432`, total `1.512`.
  - `test_filament_cost_from_spool`: 14.2 g at 600 UAH per 1000 g -> `8.52`.
  - `test_filament_cost_unknown_price`: no spool, no fallback -> `None`.
  - `test_tariff_check_due`: never -> true, 90 days -> false, 91 days -> true.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit (ask first).

### Task 9: Camera snapshot

**Files:**
- Create: `src/bambu_mcp/camera.py`, `tests/test_camera.py`

**Interfaces:**
- Consumes: `Secrets`, `Settings.camera_backend`, `redact`.
- Produces: `async snapshot(secrets, settings, timeout_s: float = 15) -> bytes | None` (JPEG bytes, or None with a redacted warning logged). URL `rtsps://bblp:<code>@<host>:322/streaming/live/1`.
  - `pyav` backend: in-process, one frame, encoded to JPEG.
  - `ffmpeg` backend: the URL is never in argv. Pass it through a temporary file readable by the service user only (`-protocol_whitelist file,rtsp,rtsps,tcp,tls -f ...` reading the URL from a file, or an equivalent the phase 0 report recorded), deleted after.

- [ ] **Step 1:** Tests (backend replaced by a fake):
  - `test_snapshot_returns_jpeg_bytes`.
  - `test_timeout_returns_none`.
  - `test_error_with_url_is_redacted`: the fake raises `OSError("cannot open rtsps://bblp:12345678@...")`, captured log has `***`, not the code.
  - `test_ffmpeg_argv_has_no_secret`: build the ffmpeg command for a fake secret, assert the secret is not in any argv element.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement the backend the phase 0 report chose, and the other only if it is cheap.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit (ask first).

### Task 10: Notifier

**Files:**
- Create: `src/bambu_mcp/notify.py`, `src/bambu_mcp/messages.py`, `tests/test_notify.py`, `tests/test_messages.py`

**Interfaces:**
- Produces:
  - `Button(id: str, text: str)`, `Press(seq: int, message_id: str, button_id: str, at: datetime)`.
  - `Notifier` protocol: `async send(text: str, buttons: list[list[Button]] | None = None, files: list[tuple[str, bytes]] | None = None, dedupe: str | None = None, expires_in: int | None = None, silent: bool = False) -> str | None`, `async edit(message_id: str, text: str | None = None, buttons: list[list[Button]] | None = None) -> None`, `presses() -> AsyncIterator[Press]`.
  - `LogNotifier` (logs a one-line summary, never text with secrets, `presses()` yields nothing).
  - `ServiceNotifyClient(base_url: str, token: str)` implementing the add-in's API: `POST /v1/messages`, `PATCH /v1/messages/<id>`, `GET /v1/presses?wait=25`, files as base64, retries connection errors with backoff (router restarting).
  - `messages.py`: `render_event(event, texts: HmsTexts, cost: CostLine | None) -> str` (Russian), `post_print_buttons(print_id) -> list[list[Button]]` with ids `fit:<print_id>:ok|tight|loose|failed`, `spool_buttons(print_id, spools) -> list[list[Button]]` with ids `spool:<print_id>:<spool_id>` and `spool:<print_id>:none`.

- [ ] **Step 1:** Tests:
  - `test_service_notify_send` against an `httpx.MockTransport`: request JSON has `text`, `buttons` as rows of `{"id","text"}`, `files` base64, `dedupe`, header `Authorization: Bearer <token>`. Returns the `message_id`.
  - `test_service_notify_duplicate`: response `{"message_id": "7", "duplicate": true}` -> returns `"7"`.
  - `test_presses_long_poll`: two presses in one response -> yielded in `seq` order.
  - `test_retry_on_connection_error`: first call raises `httpx.ConnectError`, second succeeds.
  - `test_render_finished_ru`: a finished event with cost renders `"✅ Печать завершена"`, the part name, time, `"Пластик 8.52 грн, электричество 1.94 грн, всего 10.46 грн"` and the tariff used, `"тариф 4.32 грн/кВт·ч с 01.06.2024"`.
  - `test_render_hms_unknown_text`: contains the raw HMS code.
  - `test_post_print_button_ids`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement with `httpx.AsyncClient`.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit (ask first).

### Task 11: Service wiring

**Files:**
- Create: `src/bambu_mcp/service.py`, `src/bambu_mcp/journal.py`, `tests/test_service.py`

**Interfaces:**
- Consumes: Tasks 2 to 10.
- Produces:
  - `Journal(store, settings)`: `on_event(event) -> int | None` (opens a row on `started` with `source="external"`, closes on `finished/failed/cancelled` with actual minutes and kWh and power cost), `on_press(press) -> str | None` (applies `fit:` and `spool:` presses, returns the text to edit the message to).
  - `class Service`: `__init__(secrets, settings, store, notifier, link_factory, camera)`, `async run() -> None`, `async stop() -> None`, `state -> PrinterState`, `link_status -> LinkStatus`.
  - `main() -> None`: loads `/srv/bambu/.env` (path from `BAMBU_ENV`, default `/srv/bambu/.env`) and `config.toml`, installs redaction, builds the notifier (`ServiceNotifyClient` when `notify_url` and `notify_token` are set, else `LogNotifier`), runs the service and the MCP server (Task 12) in one event loop.
- Behaviour: `first_layer_done`, `finished`, `failed` attach a camera snapshot. After `finished/failed/cancelled` one message asks for the fit (buttons) and, when the slot has no mapped spool, which spool (buttons) with "не списывать" as the default. Phase 1 deducts grams only when the owner gives them (`update_print`, Task 12). `AUTH_FAILED` sends one message "Принтер отклонил код доступа: проверь, не сменился ли он" and nothing more until restart. Once a day the service checks `tariff_check_due` (last reminder date kept in the store) and, when due, sends "Проверь тариф на электроэнергию: сейчас 4.32 грн/кВт·ч с 01.06.2024" (current values).

- [ ] **Step 1:** Tests with a fake link fed from fixtures, `LogNotifier` replaced by a recording fake, in-memory `Store`:
  - `test_lifecycle_notifies_once_each`: started, first layer (with a file), finished (with a file and the fit question).
  - `test_restart_mid_print`: stop the service after `running_layer2`, start a new one on the same store, feed `running_layer2` and `finish`: no second `started`, the journal row closes.
  - `test_auth_failed_message_once`.
  - `test_fit_press_updates_journal_and_edits_message`.
  - `test_tariff_reminder_quarterly`: with a fake clock, one reminder on day 0, none on day 90, one on day 91.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. paho callbacks run in its own thread: hand reports to the loop with `loop.call_soon_threadsafe`.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit (ask first).

### Task 12: MCP server, owner only

**Files:**
- Create: `src/bambu_mcp/mcp_server.py`, `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `Service`, `Store`, `camera.snapshot`, `cost`.
- Produces: `build_app(service, store, secrets, settings) -> Starlette` (FastMCP `streamable_http_app()` wrapped in a bearer-token middleware that accepts only `secrets.client_tokens` values and records the client name), served by uvicorn on each `settings.listen` address.
- Tools (names exact): `printer_status`, `ams_status` (trays with their mapped spools), `camera_snapshot` (image content), `print_history(part: str | None = None, limit: int = 20)`, `spools`, `plates`, `add_spool(brand, material, color_name, color_hex, price_uah, net_g, filament_preset=None)`, `update_spool(spool_id, ...)`, `set_spool_weight(spool_id, remaining_g)`, `map_slot(ams, slot, spool_id)` (records the mapping only, sends nothing to the printer in phase 1), `add_plate(name, bed_type)`, `set_default_plate(plate_id)`, `set_tariff(valid_from, day_uah, night_uah=None)`, `update_print(print_id, grams=None, spool_id=None, fit=None)`. Write tools carry `destructiveHint=False`, `readOnlyHint=False`. No tool returns or accepts the access code, serial or tokens.

- [ ] **Step 1:** Tests with Starlette's `TestClient`:
  - `test_rejects_missing_or_wrong_token` (401).
  - `test_lists_exact_tool_names`: the tool list equals the set above.
  - `test_printer_status_has_no_secrets`: the JSON result contains neither the access code nor the serial.
  - `test_set_tariff_then_history_cost`: `set_tariff("2027-01-01", 4.80)`, then a print closed on 2027-01-02 is costed at 4.80.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit (ask first).

### Task 13: Deployment files and acceptance

**Files:**
- Create: `deploy/bambu-service.service`, `deploy/README.md`, `config.example.toml` (complete it)

- [ ] **Step 1:** Unit file: `User=shiba-srv`, `WorkingDirectory=/srv/bambu/app`, `Environment=BAMBU_ENV=/srv/bambu/.env`, `ExecStart=/srv/bambu/app/.venv/bin/bambu-service`, `MemoryMax=150M`, `Restart=on-failure`, `RestartSec=10`.
- [ ] **Step 2:** `deploy/README.md`: copy code to `/srv/bambu/app` (tar over ssh, as orchestra-gateway), `uv sync --frozen`, create `/srv/bambu/.env` mode 600 (owner types the access code), run `scripts/extract_hms.py` on the PC and copy the JSON to `/srv/bambu/data/`, `assistant service key bambu` for the notify token, install and enable the unit, register the MCP in the owner's Claude Code (`claude mcp add --transport http bambu http://192.168.88.11:<port>/mcp --header "Authorization: Bearer <pc token>"`, typed by the owner) and for the owner's multiclaude assistant only.
- [ ] **Step 3:** Deploy only with the owner's OK (workshop skill rules: ask before installing units and restarting anything). After deploy run the workshop status snapshot. Expected: `bambu-service active`, its cgroup memory under 120 MB, nothing else changed.
- [ ] **Step 4:** Acceptance: over a week of the owner's real prints, every print produced started, first-layer and finished (or failed) messages exactly once, with snapshots, and journal rows with power cost. Record the result in `docs/phase1-acceptance.md`.
- [ ] **Step 5:** Commit (ask first).
