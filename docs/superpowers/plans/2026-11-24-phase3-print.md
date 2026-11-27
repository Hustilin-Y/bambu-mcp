# Phase 3: print start behind a Telegram button — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** a sliced job is printed on the P2S after the owner presses a Telegram button, from Claude Code (`prepare_print`) and from Telegram (a Print button on the sliced summary) alike, with pause, a confirmed stop, and no way for a model to start or stop a print by itself.

**Architecture:** `prepare_print` runs the pre-flight checks, fetches the sliced file from the PC, binds a single-use confirmation to a hash of what will be printed and sends the confirmation message through the outbox. Only the owner's press on that message reaches the service (service-notify, router front desk). The press re-reads the printer, re-runs the checks, uploads the file over FTPS to the printer's USB stick, sends one `project_file` command and watches the reports: `started` or `unconfirmed`, never a resend. The printer link gains exactly three structurally checked writes: `project_file`, `pause`, `stop`.

**Tech Stack:** Python 3.12, uv, pytest `-W error`, paho-mqtt 2, `ftplib` (implicit FTPS, as in `spike/upload.py`), httpx, mcp 2.3.0, SQLite (schema v8).

**Spec:** `docs/superpowers/specs/2026-11-22-bambu-mcp-design.md` sections 9, 12 (phase 3), 13 and 16. Facts: `docs/phase0-report.md` (row 4 "Storage": the verified `project_file` payload and FTPS to the USB root), `spike/upload.py`, `spike/start_print.py`.

**Branch:** `phase3-print` from `phase2-slicing` (408e2ea or later).

## Global Constraints

- There is no `start_print` tool and no client-token path that starts or stops a print. Only a press on a service-notify button sent by the service starts (`start:`) or stops (`stop:`) a print (spec 16).
- **Never resend** `project_file`. One confirmation starts at most one `project_file`, ever, also across a service restart and a double press.
- The link accepts exactly three new writes, each checked structurally like `ams_filament_setting` (exact key set, exact types, rebuilt and compared): `project_file`, `pause`, `stop`. Still refused: `resume`, `gcode_line`, axis moves, heating, AMS load/unload, HMS clearing, file deletion, LAN/Developer Mode settings, anything else.
- Confirmation: random 128-bit id, valid 10 minutes, single use, bound to `sha256(gcode file sha256, plate, ams mapping, printer serial, nozzle, plate type)`.
- Pre-flight hard stops: printer not connected or no fresh report since connect; `gcode_state` not in `IDLE`/`FINISH`/`FAILED`; a fatal or serious HMS; no USB storage (`sdcard` false); nozzle diameter differs from the job's; slot empty, unmapped, or its tray type differs from the sliced filament preset's material; spool remainder below the plate's grams x 1.15; sliced file missing, or its sha256 differs from the one recorded at slicing. Warnings (shown, not blocking): colour of the slot differs from the colour at slicing; manifest material intent differs from the slot (the Task 12 warning); `hint_conflicts` on the plate.
- The owner confirms bed clear and plate type in the confirmation itself: the button label is "Стол чистый, печатать" and the text names the plate type.
- `project_file` payload exactly as verified in phase 0, with: `url "ftp:///<file>"`, `file <file>`, `param "Metadata/plate_<n>.gcode"`, `md5` from `Metadata/plate_<n>.gcode.md5` inside the file, `subtask_name` = the file stem (the job's `out_name`, so phase 2 Task 9 matching works), unique numeric `task_id`, `subtask_id "0"`, `project_id "0"`, `profile_id "0"`, `timelapse false`, `bed_type "auto"`, `bed_leveling true`, `bed_levelling true`, `auto_bed_leveling 2`, `flow_cali false`, `vibration_cali false`, `layer_inspect true`, `use_ams true`, `ams_mapping [ams*4+slot]`, `ams_mapping2 [{"ams_id": ams, "slot_id": slot}]`.
- FTPS: implicit TLS on 990, the same CA file and peer CN == serial check as MQTT, TLS session reuse on data sockets, upload to the USB root, `SIZE` must equal the local size before `project_file` is sent. Nothing is deleted from the stick.
- Watch window after the send: 30 s. `started` = a report with our `task_id` (or `subtask_name`) and `gcode_state` in `PREPARE`/`RUNNING`. Otherwise `unconfirmed`, told to the owner with what was last seen, and the confirmation stays used.
- Pause: tool `pause_print`, allowed only while `RUNNING`. Stop: tool `request_stop` and the "Остановить…" button only send a Telegram message with a "Да, остановить" button. The press sends `stop`. No resume anywhere.
- Russian Telegram texts, no em dash, no semicolon, escaped with the existing helpers. English code, docs, errors to Claude. No server paths or secrets in any text.
- Tests never touch the real printer, FTP server, PC or Telegram. Fakes only.
- Memory: the sliced file is streamed to `data_dir/print-cache/` and from there to FTPS, never held whole in RAM. The cache keeps the last 3 files.

## Review Focus

- The owner prints from Bambu Studio (or starts another job) between prepare and press: the press must refuse (state no longer idle, or the hash changed) and say why. Tested in Task 5.
- Two presses, or a press, a service restart and a replayed press: exactly one `project_file`. Tested in Tasks 2 and 5.
- `unconfirmed` (the printer acks nothing in 30 s): the owner is told and nothing is resent. A later report showing our `task_id` running is still matched by the journal. Tested in Task 5.
- The PC is asleep at `prepare_print`: it is woken (phase 2 waker) to fetch the file. A failed wake fails the prepare with a clear text, and no confirmation is sent. Tested in Task 3.
- A kit: the owner picks the plate (1..n). `param`, `md5` and grams come from that plate, and the confirmation names it. Tested in Tasks 3 and 5.

---

### Task 1: Link writes `project_file`, `pause`, `stop`

**Files:** Modify `src/bambu_mcp/printer/link.py`, `src/bambu_mcp/printer/state.py` (parse `sdcard`, `task_id`). Test `tests/printer/test_link.py`, `tests/printer/test_state.py`.

**Interfaces:**
- `PrinterLink.start_project(*, file: str, plate: int, md5: str, subtask_name: str, task_id: str, ams: int, slot: int) -> None`, `PrinterLink.pause() -> None`, `PrinterLink.stop() -> None`.
- `PrinterState.sdcard: bool | None`, `PrinterState.task_id: str | None`.
- The validators: `file` matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.gcode\.3mf$`, `md5` `^[0-9a-f]{32}$`, `plate` 1..50, `task_id` `^[0-9]{1,19}$`, `subtask_name` equals `file` minus `.gcode.3mf`, and ams/slot go through the existing `tray_ids` helper (one place).

- [ ] Step 1 tests: `test_start_project_payload_exact` (the whole dict equals the constraint's payload), `test_start_project_rejects_bad_fields` (parametrized), `test_pause_and_stop_payloads_exact`, `test_publish_refuses_everything_else` (extends the Task 10 list with `resume`, `gcode_line`, `project_file` with an extra key, with `url` not `ftp:///<file>`, with a second command key), `test_state_parses_sdcard_and_task_id` (real fixtures).
- [ ] Steps 2-4: RED, implement, GREEN.
- [ ] Step 5: commit `Link: project_file, pause and stop writes`.

### Task 2: Confirmations in the store and the gate hash

**Files:** Create `src/bambu_mcp/printgate.py`. Modify `src/bambu_mcp/store/db.py`, `models.py` (schema v8). Test `tests/test_printgate.py`, `tests/store/test_db.py`.

**Interfaces:**
- Table `confirmations(id TEXT PRIMARY KEY, kind TEXT CHECK(kind IN ('start','stop')), job_id INTEGER, plate INTEGER, gate_hash TEXT, payload_json TEXT, created_at, expires_at, state TEXT CHECK(state IN ('pending','used','expired','cancelled')), outcome TEXT)`.
- `gate_hash(file_sha256: str, plate: int, ams: int, slot: int, serial: str, nozzle: str, plate_type: str) -> str` (sha256 hex of a canonical JSON).
- `Store.add_confirmation(...) -> Confirmation`, `Store.claim_confirmation(id: str, now) -> Confirmation | None`: atomically moves `pending` and unexpired to `used` and returns it. Expired returns None after marking it `expired`. `used`, `cancelled` or unknown returns None. `Store.cancel_confirmation(id)`.
- `new_confirmation_id() -> str` (`secrets.token_hex(16)`).
- [ ] Step 1 tests: `test_gate_hash_changes_with_each_field`, `test_claim_is_single_use` (two claims, one wins, also from two `Store` objects on one file), `test_claim_after_expiry_is_none_and_marks_expired`, `test_migrate_v7_to_v8` (real v7 DB with rows).
- [ ] Steps 2-4: RED, implement, GREEN.
- [ ] Step 5: commit `Gate: confirmations and the gate hash`.

### Task 3: Pre-flight and the print file

**Files:** Modify `src/slice_agent/app.py` (the `/slice` 200 body gains `sha256` of the output file), `src/bambu_mcp/slicer.py` (read it, and stream `GET /sliced/<file>` to a path), `src/bambu_mcp/service.py` (store that sha256 in `result_json` at slice time), `src/bambu_mcp/printgate.py`. Test `tests/test_printgate.py`, `tests/test_slicer.py`, `tests/slice_agent/test_app.py`. A job sliced before this change has no recorded sha256, so `prepare_print` asks for a re-slice (a blocking line).

**Interfaces:**
- `SliceClient.fetch_file(name: str, dest: Path, max_bytes: int = 50 * 2**20) -> str` (streams to `dest`, returns its sha256, fails past the cap).
- `print_file_info(path: Path, plate: int) -> PrintFileInfo(md5, grams, minutes, filament_types)`, read with `slice_agent.output.read_output` and `Metadata/plate_<n>.gcode.md5`.
- `preflight(state: PrinterState, job: Job, plate: int, slot_choice, spool, info: PrintFileInfo, expected_sha256: str, file_sha256: str, nozzle: str) -> Preflight(blocking: list[str], warnings: list[str])`. Russian strings, one per problem, in the constraint's order.
- [ ] Step 1 tests: one test per hard stop and per warning in the Global Constraints. `test_fetch_file_streams_and_caps` (MockTransport, no full-body read), `test_kit_plate_picks_its_md5_and_grams` (fixture kit file built in the test from the phase 2 output fixtures).
- [ ] Steps 2-4: RED, implement, GREEN.
- [ ] Step 5: commit `Gate: pre-flight checks and the print file`.

### Task 4: FTPS uploader

**Files:** Create `src/bambu_mcp/printer/ftps.py`. Test `tests/printer/test_ftps.py`.

**Interfaces:**
- `upload(host: str, serial: str, access_code: str, ca_file: Path, local: Path, remote_name: str, *, timeout_s: float = 60, ftp_factory=ImplicitFTPS) -> None`: user `bblp`, `prot_p`, `STOR` to the root, then `SIZE` must equal the local size, else it raises `UploadError` with an English reason (no secrets).
- `ImplicitFTPS` from `spike/upload.py` (wrap at connect, reuse the TLS session on data sockets), plus the CN == serial check after the handshake.
- Run in a worker thread from the service (`anyio.to_thread.run_sync`), one upload at a time.
- [ ] Step 1 tests with an injected fake `ftp_factory`: `test_upload_stores_then_checks_size`, `test_size_mismatch_raises`, `test_cn_mismatch_refuses_before_login`, `test_553_no_storage_maps_to_clear_error`, `test_access_code_never_in_errors_or_logs`.
- [ ] Steps 2-4: RED, implement, GREEN.
- [ ] Step 5: commit `FTPS upload to the printer's USB stick`.

### Task 5: The start flow in the service

**Files:** Modify `src/bambu_mcp/service.py`, `messages.py`. Test `tests/test_service.py`.

**Interfaces:**
- `Service.prepare_print(job_id: int, plate: int = 1) -> PrepareResult(confirm_id, text, warnings)`:
  - the job must be `sliced`;
  - wake the PC and fetch the file to `data_dir/print-cache/<file>`, compare its sha256;
  - run the pre-flight with fresh printer state;
  - on blocking problems raise `PrintRefused(lines)` and send nothing;
  - otherwise store the confirmation and enqueue the Russian confirmation with buttons `start:<id>:go` "Стол чистый, печатать" and `start:<id>:cancel` "Отмена".
- The sliced summary (`render_sliced` path) gains a `job:<id>:print` button "Печатать…". Its press runs `prepare_print(job_id, 1)` for a part. For a kit it first sends one button per plate (`job:<id>:print:<n>`).
- On the `start:<id>:go` press:
  - `claim_confirmation`, then re-read the state, the re-run of the pre-flight and the gate hash must match;
  - upload, then `start_project` once, then watch 30 s;
  - enqueue "Печать началась" (with an "Остановить…" button `stop:ask:<task_id>`) or the `unconfirmed` text.
  - Any refusal answers in Russian with the reason, and the confirmation stays used.
  - `cancel` marks it cancelled and edits the message.
- One start at a time (an asyncio lock). A press while a start runs is answered "уже запускаю".
- [ ] Step 1 tests (fake link, fake uploader, fake waker and slicer, fake clock):
  - `test_prepare_sends_confirmation_and_starts_nothing`;
  - `test_prepare_refused_lists_reasons_and_sends_nothing`;
  - `test_press_starts_once_then_started_message`;
  - `test_double_press_and_restart_replay_start_once`;
  - `test_press_after_state_changed_refuses`;
  - `test_press_after_expiry_refuses`;
  - `test_unconfirmed_tells_owner_and_never_resends`;
  - `test_cancel`;
  - `test_kit_print_button_asks_for_plate`;
  - `test_pc_asleep_wake_fails_no_confirmation`;
  - `test_started_print_matched_by_journal` (Task 9 booking on the started report).
- [ ] Steps 2-4: RED, implement, GREEN.
- [ ] Step 5: commit `Service: prepare, confirm and start a print`.

### Task 6: Pause, confirmed stop, MCP tools and docs

**Files:** Modify `src/bambu_mcp/service.py`, `mcp_server.py`, `messages.py`, `deploy/README.md`, `README.md`. Test `tests/test_mcp_server.py`, `tests/test_service.py`.

**Interfaces:**
- MCP:
  - `prepare_print(job_id: int, plate: int = 1)` returns `{confirm_id, text, warnings}` and says the owner must press the button in Telegram;
  - `pause_print()` works only while `RUNNING`;
  - `request_stop()` sends the stop confirmation and stops nothing itself.
  - All three are annotated destructive.
  - The exact tool list test lists them, and `start_print` is absent.
- Stop: `request_stop` and the "Остановить…" press store a `stop` confirmation (10 min) and send "Остановить печать <name>?" with `stop:<id>:go` "Да, остановить" and `stop:<id>:cancel`. The go press, if the same `task_id` is still `RUNNING` or `PAUSE`, sends `stop` once.
- Docs:
  - deploy/README "Phase 3" covers: the USB stick is required, the cache folder, the watch window, what is never exposed, and that the stick is never cleaned by the service;
  - README tool list.
- [ ] Step 1 tests: `test_tool_list_has_no_start_print`, `test_pause_only_while_running`, `test_request_stop_sends_button_stops_nothing`, `test_stop_press_sends_stop_once`, `test_stop_press_after_print_changed_refuses`.
- [ ] Steps 2-4: RED, implement, GREEN.
- [ ] Step 5: commit `Pause, confirmed stop and the phase 3 tools`.

### Task 7: Live acceptance (controller, owner at the printer)

Not a subagent task. With the owner present, bed clear and USB stick in:
1. Deploy to workshop: code, `uv sync`, restart through `workshop-admin`, check migration v8.
2. From Telegram: the "Печатать…" button on a fresh `example-bracket` slice, then press confirm. Expect `started`, journal match, grams booked.
3. From Claude Code: `prepare_print`, press in Telegram, then `pause_print`. Resume on the printer screen. Then `request_stop` and confirm. Expect stopped and the printed share booked.
4. The expiry path: prepare, wait 10 min, press. Expect a refusal.
5. The PC side: restart the slice-agent task after the Task 3 change (the owner does it, or runs `install.ps1` again).
6. Record the results in `docs/phase3-notes.md`.
