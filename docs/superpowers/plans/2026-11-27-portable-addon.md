# Portable add-in (printer profiles, external spool, A1, multiclaude add-in) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a friend with multiclaude and an A1 without an AMS install bambu-mcp as `assistant addon enable bambu`, with printer models as profiles, while the owner's P2S setup keeps working unchanged.

**Architecture:** A new package `bambu_profiles` holds one TOML per printer model and a loader. The service and the slice-agent read the profile named by `printer` (default `p2s`) instead of module constants. The external spool becomes slot 0 (`ams 255 / tray 254`). The A1 camera gets a JPEG-over-TLS backend. A multiclaude add-in folder in this repo installs the service as a systemd user unit. Part B, in the multiclaude fork, takes service-notify upstream as two reviewed PRs.

**Tech Stack:** Python 3.12, uv, pytest, paho-mqtt, tomllib, bash (add-in), systemd user units, jq.

**Spec:** `docs/superpowers/specs/2026-11-27-portable-addon-design.md` (decisions in 5.8). Background table of P2S-specific places: `docs/superpowers/specs/2026-11-22-bambu-mcp-design.md`, "Later: printer model as configuration".

## Global Constraints

- Branch `portable` in bambu-mcp. Implementers stage only, the controller commits. Never push without the owner's word.
- Run tests with `uv run --no-sync pytest -q`. The PC slice-agent runs from this venv, so a sync fails with "Access is denied" on `slice-agent.exe`. Baseline: 1361 passed, 1 skipped.
- Backward compatible: a `config.toml` or `slice-agent.toml` without `printer` means `p2s`, and every existing test passes with P2S values unchanged.
- No owner values in code, tests, docs or the add-in: no `192.168.88.*`, `shiba-srv`, `Shiba`, Telegram ids, serials, tokens. Documentation addresses are `192.0.2.x` (RFC 5737). `/srv/bambu` may stay as an example path.
- A guessed profile value is listed in that profile's `unverified`. Nothing guessed is presented as verified.
- Never commit Bambu presets, flattened profiles or HMS tables.
- Secrets never in argv, logs or chat. The add-in reads them from a hidden prompt or stdin and writes `.env` with mode 600.
- The add-in never calls `sudo`. A step that needs root is printed for the friend to run.
- Code, comments, docs and commits in English. User-facing Telegram text stays Russian.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- A P2S owner config with no `printer` and no `listen`: after Task 8 the default `listen` is loopback only. The service must still start, and the owner's deploy must add `listen` explicitly (Task 8 test, plus the deploy note).
- An A1 report with an AMS-less `vt_tray` but no `ams` block at all: trays must still show the external spool, and an absent `ams` must not wipe it (Task 4 test).
- The external spool's `vt_tray` reporting `tray_type: ""` (nothing loaded): slot 0 counts as empty, not usable (Task 4 test).
- A payload for `ams 255 / tray 254` on a P2S profile whose AMS is present: `load_spool` to slot 0 is allowed there too (spec 5.2, last line), and the allowlist accepts exactly that pair, nothing like `255/0` or `0/254` (Task 5 test).
- Running `setup` twice: the second run keeps `.env` secrets, does not issue a second notify key and does not duplicate the MCP entry (Task 10 test).

---

## Part A: bambu-mcp (branch `portable`)

### Task 1: Printer profiles package

**Files:**
- Create: `src/bambu_profiles/__init__.py`, `src/bambu_profiles/p2s.toml`, `src/bambu_profiles/a1.toml`
- Modify: `pyproject.toml` (`[tool.hatch.build.targets.wheel] packages` adds `"src/bambu_profiles"`; the TOMLs ship as package data)
- Test: `tests/test_profiles.py`

**Interfaces:**
- Produces:
  - `PROFILE_NAMES: tuple[str, ...]` = `("a1", "p2s")`, from the TOML files present.
  - `@dataclass(frozen=True) class PrinterProfile` with fields:
    `name: str`, `model_name: str`, `bed_mm: tuple[int, int, int]`, `hms_prefix: str` (may be `""`), `camera: Literal["rtsps_322", "jpeg_6000"]`, `storage: Literal["usb", "sd"]`, `ams_units: int` (0 for no AMS), `ams_slots: int` (0 for no AMS), `external_spool: bool`, `machine_presets: dict[str, str]` (nozzle -> preset), `process_sets: dict[str, str]`, `filaments: tuple[str, ...]`, `plate_types: tuple[str, ...]`, `layer_heights: dict[str, tuple[float, ...]]`, `nozzle_temp_range: tuple[int, int]`, `power_w: dict[str, float]` (must hold `PLA`), `heatup_kwh: float`, `print_flags: dict[str, object]` (exactly the keys `bed_type`, `bed_leveling`, `bed_levelling`, `auto_bed_leveling`, `flow_cali`, `vibration_cali`, `layer_inspect`, `timelapse`), `idle_states: frozenset[str]`, `stoppable_states: frozenset[str]`, `unverified: frozenset[str]` (field names).
  - Properties: `has_ams -> bool` (`ams_units > 0`), `max_slot_number -> int` (`ams_units * ams_slots`), `bed_area -> str` (`"256x256"`), `bed_height -> str` (`"256"`), `hms_file -> str | None` (`f"hms_en_{hms_prefix}.json"` or None when empty).
  - `load_profile(name: str = "p2s") -> PrinterProfile`. `ValueError` naming the known profiles for an unknown name, and for a profile with a missing or unknown key, a wrong type, an `unverified` entry that is not a field name, `power_w` without `PLA`, a `has_ams` profile without `external_spool` key, or `print_flags` keys other than the eight above. Read with `importlib.resources`.

Values. `p2s.toml` copies today's constants exactly:
- `model_name = "Bambu Lab P2S"`, `bed_mm = [256, 256, 256]`, `hms_prefix = "22E"`, `camera = "rtsps_322"`, `storage = "usb"`, `ams_units = 4`, `ams_slots = 4`, `external_spool = true`
- `machine_presets = {"0.4" = "Bambu Lab P2S 0.4 nozzle"}`, `process_sets = {standard = "0.20mm Standard @BBL P2S"}`, `filaments = ["Generic PETG @BBL P2S"]` (the slice-agent's current defaults)
- `plate_types` = the 5 in `slice_agent/runner.py:22`, `layer_heights = {"0.4" = [0.12, 0.16, 0.20, 0.24, 0.28]}`, `nozzle_temp_range = [150, 320]`, `power_w = {PLA = 200.0}`, `heatup_kwh = 0.05`
- `print_flags` = the values in `printer/link.py:263-270`
- `idle_states = ["IDLE", "FINISH", "FAILED"]`, `stoppable_states = ["PREPARE", "RUNNING", "PAUSE"]`
- `unverified = ["external_spool"]`

`a1.toml`:
- `model_name = "Bambu Lab A1"`, bed 256³, `hms_prefix = "N2S"`, `camera = "jpeg_6000"`, `storage = "sd"`, `ams_units = 0`, `ams_slots = 0`, `external_spool = true`
- presets `"Bambu Lab A1 0.4 nozzle"`, `"0.20mm Standard @BBL A1"`, `["Generic PLA @BBL A1", "Generic PETG @BBL A1"]`
- the P2S plate types, layer heights and power values, `nozzle_temp_range = [150, 300]`
- `print_flags` as P2S but `layer_inspect = false` (no lidar)
- the P2S state sets
- `unverified` = every field except `name`, `model_name` and `bed_mm`

- [ ] **Step 1: Write the failing tests** in `tests/test_profiles.py`: `test_p2s_matches_current_constants` (each value above, plus `max_slot_number == 16`, `hms_file == "hms_en_22E.json"`, `unverified == {"external_spool"}`); `test_a1_profile` (`has_ams is False`, `max_slot_number == 0`, `camera == "jpeg_6000"`, `"camera" in unverified`, `"model_name" not in unverified`); `test_unknown_profile_names_known` (`pytest.raises(ValueError, match="a1, p2s")`); `test_bad_profile_rejected` parametrised over a tmp TOML with an unknown key, `power_w` without PLA, `unverified = ["nope"]`, an extra `print_flags` key (load through a private `_parse(text: str, name: str)` the loader uses).
- [ ] **Step 2: Run** `uv run --no-sync pytest tests/test_profiles.py -q`. Expected: FAIL, `ModuleNotFoundError: bambu_profiles`.
- [ ] **Step 3: Implement** the package and both TOMLs. No re-sync is needed: the editable install puts `src/` on the path, so `bambu_profiles` imports at once.
- [ ] **Step 4: Run** the same command. Expected: PASS. Then the full suite: 1361 passed plus the new ones.
- [ ] **Step 5: Commit** `feat: printer profiles (P2S verified, A1 unverified)`.

### Task 2: Service reads the profile

**Files:**
- Modify: `src/bambu_mcp/config.py` (`Settings.printer: str = "p2s"`, `Settings.profile` property, `power_w`/`heatup_kwh`/`camera_backend` defaults from the profile), `src/bambu_mcp/service.py` (`HMS_FILE`, `STOPPABLE_STATES`, `_default_plate_type` via `PLATE_TYPES`), `src/bambu_mcp/printgate.py` (`START_STATES`, storage wording), `src/bambu_mcp/mcp_server.py` (`LOADABLE_STATES`, server instructions string, `LAYER_HEIGHTS`/`PLATE_TYPES` in the settings help), `src/bambu_mcp/evaluation.py:417` (`_DEFAULT_LAYER` stays 0.2 but the comment drops "P2S"), `src/bambu_mcp/events.py` only if it uses the idle set, `config.example.toml`
- Test: `tests/test_config.py`, `tests/test_printgate.py`, `tests/test_service.py`, `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `load_profile`, `PrinterProfile` (Task 1).
- Produces:
  - `Settings.printer: str` (validated against `PROFILE_NAMES` in `Settings.load`).
  - `Settings.profile -> PrinterProfile` (cached, `load_profile(self.printer)`).
  - `Settings.power_w` and `Settings.heatup_kwh`: the config's value when given, else the profile's. `CameraBackend = Literal["pyav", "jpeg_6000"]`. Default `camera_backend`: `"pyav"` for a `rtsps_322` profile, `"jpeg_6000"` for a `jpeg_6000` one.
  - `printgate.preflight` (the existing function taking `state`) gains `profile: PrinterProfile`. Its storage lines name «USB-флешка» for `usb` and «microSD-карта» for `sd`. `state.sdcard` stays the one flag.
  - `printer_status` adds `"printer": {"model": ..., "profile": ..., "unverified": [...]}`. The MCP server instructions say "Bambu Lab printer" plus the model from the profile.

- [ ] **Step 1: Failing tests.**
  - `test_printer_defaults_to_p2s` (no key: `settings.profile.name == "p2s"`, `power_w == {"PLA": 200.0}`, `camera_backend == "pyav"`).
  - `test_printer_a1_defaults` (`printer = "a1"`: `camera_backend == "jpeg_6000"`).
  - `test_unknown_printer_rejected`.
  - `test_power_w_config_overrides_profile`.
  - In test_printgate, `test_sd_storage_wording` (an `sd` profile with `sdcard False` gives a line with «microSD»).
  - In test_mcp_server, `test_printer_status_names_profile` (has `"printer"` with `"unverified"`).
- [ ] **Step 2: Run** those tests. Expected: FAIL.
- [ ] **Step 3: Implement.** Replace each module constant with a read of `settings.profile`. Keep the old names as aliases only if a test imports them. The HMS table path is `data_dir / profile.hms_file`. With no prefix, HMS codes show raw, as they do today when the file is missing.
- [ ] **Step 4: Run** the full suite. Expected: all pass.
- [ ] **Step 5: Commit** `feat: service reads the printer profile`.

### Task 3: Slice-agent reads the profile, OS-neutral paths

**Files:**
- Modify: `src/slice_agent/config.py`, `src/slice_agent/output.py:22-23,315-322`, `src/slice_agent/profiles.py:18-19`, `src/slice_agent/runner.py:22-23,58-82,146`, `src/slice_agent/app.py` (passes the profile through), `src/bambu_mcp/service.py:144` and `src/bambu_mcp/mcp_server.py:84-87` (stop importing `PLATE_TYPES`/`LAYER_HEIGHTS` from the runner, use `settings.profile`), `deploy/slice-agent/slice-agent.example.toml`
- Test: `tests/slice_agent/test_*.py`

**Interfaces:**
- Consumes: `load_profile`.
- Produces:
  - `AgentConfig.printer: str = "p2s"` and `AgentConfig.profile -> PrinterProfile`.
  - `machine`, `process_sets` and `filaments` default to the profile's when the TOML omits them.
  - `config_path(env, platform=sys.platform) -> Path`:
    - Windows: `%LOCALAPPDATA%\bambu-mcp\slice-agent.toml`, as today.
    - Other platforms: `$XDG_CONFIG_HOME/bambu-mcp/slice-agent.toml`, with `~/.config` as the fallback.
  - `load_config(path=None, env=None, platform=sys.platform) -> AgentConfig`. On non-Windows:
    - `studio_exe` and `studio_resources` are required (`ValueError` naming the key);
    - `studio_data` defaults to `~/.config/BambuStudio`;
    - `profiles_dir` defaults to `$XDG_DATA_HOME/bambu-mcp/profiles` (fallback `~/.local/share`);
    - `sliced_dir` defaults to `~/bambu-mcp/sliced`.
  - `output.check(result, nozzle, profile)` uses `profile.model_name` and `profile.bed_mm[:2]`.
  - `profiles.flatten` asserts `profile.bed_area`/`bed_height`.
  - `runner._checks(nozzle, profile)`, `Overrides.parse(raw, nozzle, profile=None)`: `None` means `load_profile("p2s")`, so existing callers keep working. `build_command` reads `cfg.profile.plate_types`. `OVERRIDE_KEYS` stays nozzle- and profile-independent.

- [ ] **Step 1: Failing tests.**
  - `test_linux_paths_from_xdg` (env without LOCALAPPDATA, `platform="linux"`, XDG vars set: the paths above).
  - `test_linux_requires_studio_exe`.
  - `test_windows_paths_unchanged`.
  - `test_a1_agent_defaults` (`printer = "a1"`: machine `{"0.4": "Bambu Lab A1 0.4 nozzle"}`).
  - `test_output_check_a1_model` (an A1 result passes with the a1 profile and fails with p2s).
  - `test_overrides_layer_from_profile`.
- [ ] **Step 2: Run** `uv run --no-sync pytest tests/slice_agent -q`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement.** The POSIX kill path in `runner.py:199-205` already uses `killpg`. Leave it and add no new kill code.
- [ ] **Step 4: Run** the full suite. Expected: all pass.
- [ ] **Step 5: Commit** `feat: slice-agent reads the printer profile, runs on Linux paths`.

### Task 4: External spool in state, slots and proposals

**Files:**
- Modify: `src/bambu_mcp/printer/state.py` (`_parse_trays`, `apply`), `src/bambu_mcp/proposal.py:21-24,177-236`, `src/bambu_mcp/messages.py` (slot labels and the slot button row), `src/bambu_mcp/mcp_server.py:570-571,865` (slot range and `map_slot`/`load_spool` checks), `src/bambu_mcp/service.py` (wherever trays or slot numbers are rebuilt)
- Test: `tests/printer/test_state.py`, `tests/test_proposal.py`, `tests/test_messages.py`, `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `PrinterProfile.has_ams`, `max_slot_number`, `external_spool`.
- Produces:
  - `EXTERNAL_AMS = 255`, `EXTERNAL_TRAY = 254` in `printer/state.py`.
  - `_parse_vt_tray(raw) -> Tray | None` gives `Tray(ams=255, slot=254, ...)` with the same field parsing as AMS trays. `apply` merges it into `trays`. A report with `vt_tray` and no `ams` keeps the AMS trays, a report with `ams` and no `vt_tray` keeps the external one.
  - `EXTERNAL_SLOT_NUMBER = 0` in `proposal.py`.
  - `slot_number(ams, slot) -> int`: 0 for (255, 254).
  - `slot_position(number, max_slot: int = MAX_SLOT_NUMBER) -> tuple[int, int]`: 0 gives (255, 254). Otherwise the range is 1..`max_slot`.
  - `MAX_SLOT_NUMBER` stays 16 as the P2S default. Callers with a settings object pass `settings.profile.max_slot_number`.
  - `EXTERNAL_LABEL = "внешняя катушка"`. `UsableSlot.label` reads «внешняя катушка PLA белый» for slot 0.
  - MCP `slot` arguments accept 0..`max_slot_number`, and 0 only when `profile.external_spool`.

- [ ] **Step 1: Failing tests.**
  - `test_vt_tray_parsed_as_external` (fixture dict `{"print": {"vt_tray": {"id": "254", "tray_type": "PLA", "tray_color": "FFFFFFFF", "remain": 80}}}`).
  - `test_vt_tray_kept_when_ams_absent`, `test_ams_kept_when_vt_tray_absent`.
  - `test_empty_vt_tray_not_usable` (`tray_type ""`).
  - `test_slot_number_external_roundtrip`.
  - `test_slot_position_rejects_17_on_p2s_and_1_on_a1` (`max_slot=0`: only 0 is valid).
  - `test_pick_slot_external_only` (one external tray mapped to a PLA spool is picked).
  - `test_external_label_in_slot_buttons`.
  - `test_map_slot_accepts_zero`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.** Mark the new hand-built payload tests with `@pytest.mark.synthetic`.
- [ ] **Step 4: Run** the full suite. Expected: all pass.
- [ ] **Step 5: Commit** `feat: external spool as slot 0`.

### Task 5: External spool and profile flags in printer payloads

**Files:**
- Modify: `src/bambu_mcp/printer/link.py:46-49,121-300,509` (`AMS_UNITS`, `AMS_SLOTS`, `NOZZLE_TEMP_RANGE`, `tray_setting`, `project_file`, `_is_tray_setting`, `_is_project_file`, the link's start method), `src/bambu_mcp/service.py` (passes the profile into the link)
- Test: `tests/printer/test_link.py`

**Interfaces:**
- Consumes: `PrinterProfile` (`ams_units`, `ams_slots`, `external_spool`, `nozzle_temp_range`, `print_flags`); `EXTERNAL_AMS`, `EXTERNAL_TRAY` (Task 4).
- Produces:
  - `tray_setting(*, ams, slot, ..., profile: PrinterProfile)`. Valid targets: AMS pairs within the profile's geometry, plus exactly `(255, 254)` when `profile.external_spool`. Temperatures are checked against `profile.nozzle_temp_range`.
  - `project_file(*, file, plate, md5, subtask_name, task_id, ams, slot, profile)`:
    - For an AMS slot: today's payload, with the flag values from `profile.print_flags`.
    - For `(255, 254)`: `use_ams: False`, `ams_mapping: [-1]`, and no `ams_mapping2` key.
  - `_is_tray_setting(payload, profile)` and `_is_project_file(payload, profile)` rebuild from the same profile.
  - `PrinterLink(...)` takes `profile` once at construction. Its `publish` allowlist uses it.

- [ ] **Step 1: Failing tests.**
  - `test_p2s_project_file_unchanged` (the exact dict from `docs/phase0-report.md` row 4 fields, built with the p2s profile).
  - `test_external_project_file` (`use_ams False`, `ams_mapping == [-1]`, `"ams_mapping2" not in body`).
  - `test_a1_flags_from_profile` (`layer_inspect False`).
  - `test_tray_setting_external_allowed`.
  - `test_tray_setting_rejects_mixed_pairs` (parametrised `(255, 0)`, `(0, 254)`, `(254, 255)`).
  - `test_a1_rejects_ams_slot` (`ams=0, slot=0` with the a1 profile raises).
  - `test_a1_temp_cap_300`.
  - `test_allowlist_rejects_external_on_profile_without_external` (a test profile with `external_spool = False`).
- [ ] **Step 2: Run** `uv run --no-sync pytest tests/printer/test_link.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** The docstring of `project_file` states that the external form is unverified on any printer we have (spec section 3) and is listed in each profile's `unverified` as `external_spool`.
- [ ] **Step 4: Run** the full suite. Expected: all pass.
- [ ] **Step 5: Commit** `feat: external spool and per-profile flags in printer payloads`.

### Task 6: A1 camera backend

**Files:**
- Modify: `src/bambu_mcp/camera.py` (backend dispatch in `snapshot`; new `_jpeg6000_frame`)
- Test: `tests/test_camera.py`

**Interfaces:**
- Consumes: `Settings.camera_backend` (Task 2), the TLS context builder the MQTT link uses (`printer/link.py`, the function that builds the context for `settings.tls_mode`: reuse it, do not copy it).
- Produces: `snapshot(secrets, settings, timeout_s=15, *, wait=True) -> bytes | None`, as today, dispatching on `settings.camera_backend`. `_jpeg6000_frame(secrets, settings, timeout_s) -> bytes | None`. The protocol (as ha-bambulab's `chamber_image`):
  1. TLS to `host:6000`, with the same CA and CN-or-pin check as MQTT.
  2. Send an 80-byte auth packet: `struct.pack("<IIII", 0x40, 0x3000, 0, 0)`, then `b"bblp"` padded with NUL to 32 bytes, then the access code padded to 32 bytes.
  3. Read a 16-byte header. Its first 4 bytes are the little-endian payload length.
  4. Read that many bytes. Return them if they start with `FF D8` and end with `FF D9`, else None.
  5. Cap the payload at 4 MiB. The access code never appears in logs: reuse the existing mask.

- [ ] **Step 1: Failing tests.**
  - `test_jpeg6000_reads_one_frame`: a local TLS server on the synthetic certs from `tests/fixtures/synthetic/tls`. It checks the 80-byte auth packet and replies with a header plus a tiny JPEG (`b"\xff\xd8...\xff\xd9"`).
  - `test_jpeg6000_rejects_wrong_cn`.
  - `test_jpeg6000_oversize_frame_none`.
  - `test_jpeg6000_access_code_not_logged` (caplog).
- [ ] **Step 2: Run** `uv run --no-sync pytest tests/test_camera.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement**, as above.
- [ ] **Step 4: Run** the full suite. Expected: all pass.
- [ ] **Step 5: Commit** `feat: JPEG port 6000 camera backend (A1, unverified)`.

### Task 7: Printer check script

**Files:**
- Create: `scripts/printer_check.py`
- Test: `tests/test_printer_check.py`

**Interfaces:**
- Consumes:
  - `Secrets.from_env` and `Settings.load` from `config.py` (use the names that exist there);
  - the MQTT TLS context builder;
  - `camera.snapshot`;
  - the FTPS client in `printer/ftps.py`;
  - `redact.py` for the pushall dump.
- Produces: `python scripts/printer_check.py [--env PATH] [--config PATH] [--out DIR] [--upload-test]`. Exit 0 when every check ran, 1 if any failed. It writes `report.md` and `pushall.redacted.json` into `--out` (default `./printer-check`).

  The report has one line per check, with `OK`, `FAIL <reason>` or `SKIP`:
  - TLS and CN;
  - `get_version` (the module list, serial redacted);
  - pushall received;
  - `vt_tray` present, with its fields;
  - `ams` present or absent;
  - `sdcard`;
  - camera frame (byte count, via the profile's backend);
  - FTPS login and a root listing (names only);
  - with `--upload-test` only: an upload of a 16-byte `printer-check.txt` and a size check. The file is not deleted.

  At the end it adds a "Profile fields this confirms" list of `unverified` names that the checks confirmed. It never sends `project_file`, `ams_filament_setting`, `pause` or `stop`. A module-level `SENDABLE = (GET_VERSION, PUSH_ALL)` is the only set it publishes.

- [ ] **Step 1: Failing tests.**
  - `test_report_lines_from_fake_printer`: monkeypatch the MQTT, camera and FTPS calls with fakes returning a captured P2S pushall (`tests/fixtures/p2s/pushall_idle.json`) and a small JPEG. Assert the `OK` lines and that the serial does not appear in either file.
  - `test_never_publishes_writes`: the fake MQTT client records published payloads, and each must be in `SENDABLE`.
  - `test_upload_only_with_flag`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full suite. Expected: all pass.
- [ ] **Step 5: Commit** `feat: read-only printer check script for new models`.

### Task 8: Owner values out of the tree

**Files:**
- Modify: `src/bambu_mcp/config.py:103` (`_default_listen` -> `[("127.0.0.1", 8765)]`), `src/bambu_mcp/upload.py:64` (help example), `config.example.toml`, `deploy/README.md`, `deploy/bambu-service.service` (`User=<bot user>` placeholder and comment), `deploy/slice-agent/README.md`, `deploy/slice-agent/install.ps1`, `deploy/slice-agent/slice-agent.example.toml`, `README.md`, `pyproject.toml` (description: "Bambu Lab printers"), `tests/test_config.py`, `tests/test_deploy.py`, `tests/test_slicer.py`, `tests/test_upload.py`
- Test: `tests/test_no_owner_values.py`

**Interfaces:** none new.

- [ ] **Step 1: Failing test** `test_tree_has_no_owner_values`. It runs `git ls-files`, excluding `docs/` (history and specs stay as written) and `spike/`. It greps the rest for `192\.168\.`, `shiba-srv`, `\bShiba\b` and `Hustilin` (case-insensitive) and asserts no hits. Allowed exception: `README.md` may link the repo URL, so the `Hustilin` check skips lines containing `github.com/`.
- [ ] **Step 2: Run** it. Expected: FAIL, listing the files above.
- [ ] **Step 3: Replace** the values with `192.0.2.10` (server), `192.0.2.20` (PC) and `<bot user>`. Then `test_default_listen_loopback`: `Settings()` listen is `[("127.0.0.1", 8765)]`. The LAN address is now set in config. The `deploy/README.md` upgrade note says: "Since the portable release the default `listen` is loopback only. If your config.toml has no `listen`, add the LAN entry before updating."
- [ ] **Step 4: Run** the full suite. Expected: all pass.
- [ ] **Step 5: Commit** `chore: example values instead of the owner's in code, tests and deploy docs`.

### Task 9: Slicing options doc and Linux slice-agent examples

**Files:**
- Create: `deploy/slicing-options.md`, `deploy/slice-agent/slice-agent.service` (systemd user unit example), `deploy/slice-agent/Dockerfile.example`, `deploy/slice-agent/compose.example.yaml`
- Modify: `deploy/slice-agent/README.md` (link to the options doc)
- Test: `tests/test_deploy.py` (`test_slice_agent_user_unit_example`: the unit has `ExecStart` with `xvfb-run -a`, `Environment=SLICE_AGENT_TOKEN` absent (the token comes from `EnvironmentFile=%h/.config/bambu-mcp/slice-agent.env`), and `WantedBy=default.target`)

**Interfaces:** consumes `load_config` Linux paths (Task 3).

Content of `slicing-options.md`. Each of (a) AppImage plus `xvfb-run`, (b) Docker and (c) the PC slice-agent gets:
- steps;
- packages (`xvfb`, `libgl1`, `libwebkit2gtk-4.1-0`, `libgstreamer1.0-0`, plus the AppImage's FUSE need or `--appimage-extract`);
- the `slice-agent.toml` keys: `printer`, `studio_exe`, `studio_resources`, `listen`;
- `slice_agent_url` and `SLICE_AGENT_TOKEN` on the service side;
- risks:
  - pin the Studio version the owner uses, 02.08.02.61;
  - run the golden slice once and compare `printer_model` and the bed;
  - Studio's Linux CLI has headless quirks;
  - the Docker image is not built or published by us.

On one server, `listen` is `127.0.0.1` and `gateway_url` stays unset.

- [ ] **Step 1: Failing test** as above. **Step 2: Run** it, FAIL. **Step 3: Write** the files. **Step 4: Run** the full suite, PASS. **Step 5: Commit** `docs: slicing options for a Linux server`.

### Task 10: The multiclaude add-in

**Files:**
- Create:
  - `multiclaude/install.sh` (modelled on `cad-workshop/multiclaude/install.sh`);
  - `multiclaude/addon/README.md`, `setup`, `teardown`, `rules.md`, `next-steps`;
  - `multiclaude/addon/bambu-service.service` (user unit template).
- Test: `tests/test_addon.py` (skipped on Windows: `pytest.mark.skipif(sys.platform == "win32")`. CI runs Linux.)

**Interfaces:**
- Consumes: Settings keys `printer`, `listen`, `notify_url`, `data_dir`, `ca_file`, `jobs_root_allowed`. Env names `BAMBU_HOST`, `BAMBU_SERIAL`, `BAMBU_ACCESS_CODE`, `NOTIFY_TOKEN`, and the service's client token variable (read its exact name from `config.py`/`service.py`).
- Produces:
  - **`install.sh [--root DIR] [--user NAME] [--uninstall]`.** It copies the add-in into `$ROOT/addons/bambu`. It fills `{{BAMBU_HOME}}` (this checkout), `{{BAMBU_USER}}` and `{{BAMBU_DISPLAY}}` as the cad-workshop script does, and excludes the folder in `.git/info/exclude`.
  - **`setup`**, idempotent and without sudo. Paths:
    - config: `~/.config/bambu/config.toml`;
    - secrets: `~/.config/bambu/.env` (600);
    - data: `~/.local/share/bambu`;
    - unit: `~/.config/systemd/user/bambu-service.service`.

    Steps:
    1. Check `uv`, `jq`, `systemctl`, the checkout, and that `service-notify` is in `$ROOT/config.json` `.addons`. Without service-notify it stops with: "bambu needs the service-notify add-in. Upstream multiclaude does not have it yet: see https://github.com/korsicheg/multiclaude/pulls".
    2. Run `uv sync --frozen --no-dev` in the checkout.
    3. Ask for each missing `.env` value: host, serial, access code (hidden), and model (`p2s` or `a1`, default `a1`). Use `read -rs` from `/dev/tty` when it exists, else from stdin, which is how the tests feed it. Never echo a secret.
    4. If `.env` has no `NOTIFY_TOKEN`: `assistant service key bambu`, with its stdout captured straight into `.env`.
    5. If there is no client token: `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` into `.env`.
    6. Write `config.toml`, only if absent:
       - `printer`;
       - `listen = ["127.0.0.1:8765"]`;
       - `data_dir`;
       - `ca_file`;
       - `notify_url = "http://127.0.0.1:<service-notify port from $ROOT/config.json, default 8790>"`;
       - `jobs_root_allowed` = `~/workspaces/<user>/cad-workshop`, when `cad-workshop` is on.
    7. Download the CA bundle if absent: the same URL and check as `deploy/README.md`.
    8. Install the user unit (`systemctl --user daemon-reload`, `enable --now`). If `loginctl show-user` says `Linger=no`, print `sudo loginctl enable-linger <user>` and say the service runs only while the user is logged in until then.
    9. Register the MCP server for the owner's workspace `~/workspaces/<user>`. Edit `~/.claude.json` `.projects[<workspace>].mcpServers.bambu` with `jq --rawfile token <file>`, writing to a temp file (mode 600) and renaming over the original after a backup, so the token never reaches argv. The entry is `{"type": "http", "url": "http://127.0.0.1:8765/mcp", "headers": {"Authorization": "Bearer <token>"}}`; check the service's MCP path and auth scheme in `mcp_server.py`.
  - **`teardown`**: `systemctl --user disable --now bambu-service`, `assistant service revoke bambu`, remove the `bambu` MCP entry the same way. It keeps `~/.config/bambu` and `~/.local/share/bambu` and prints where they are.
  - **`rules.md`**: the bambu part of the assistant rules.
    - Printing goes only through `prepare_print` and the owner's button. There is no start tool.
    - Jobs come from CAD Workshop manifests: `create_job`, `propose_job`, `slice_job`, `evaluate_job`, `choose_variant`.
    - Goals and «Подобрать».
    - Spools: `add_spool`, `map_slot`, `load_spool`. Slot 0 is the external spool.
    - Costs: `set_tariff`, `set_currency`, `set_country`, `set_amortization`.
    - It notes that the profile's unverified fields are shown in `printer_status`.
  - **`next-steps`**:
    - slicing: link `deploy/slicing-options.md` and set `slice_agent_url` and `SLICE_AGENT_TOKEN`;
    - add spools and map slot 0;
    - set the tariff and amortization;
    - run `scripts/printer_check.py` and send the report as a PR;
    - the trust paragraph, verbatim from spec 5.8 "Trust on the friend's machine".
  - **`README.md`**: what the add-in does, the install commands, the trust paragraph, and the root commands it may print.

- [ ] **Step 1: Failing tests.**
  - `test_install_copies_and_fills` (a fake `$ROOT` with `bin/assistant`, `addons/`, `users.json` with an owner, `config.json`).
  - `test_setup_requires_service_notify`.
  - `test_setup_writes_env_600_and_config`. It runs with stubs on `PATH`: `uv`, `systemctl`, `loginctl`, `curl` and `assistant` as scripts that log their args. It feeds stdin `192.0.2.30\nSERIAL01\nACCESS99\na1\n` and asserts the `.env` mode is 600, that `ACCESS99` is not in any stub's logged args, and that `config.toml` has `printer = "a1"`.
  - `test_setup_twice_is_idempotent`: one `assistant service key` call, one MCP entry, and the `.env` unchanged.
  - `test_setup_prints_linger_command_without_sudo`: no stub named `sudo` is ever called. A `sudo` stub on `PATH` fails the test if run.
  - `test_teardown_keeps_data`.
  - `test_addon_has_no_owner_values`, which reuses Task 8's grep.
- [ ] **Step 2: Run** `uv run --no-sync pytest tests/test_addon.py -q` on Linux, via WSL or the CI run. On Windows the tests skip. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests on Linux. Expected: PASS. Run `bash -n` on each script and `shellcheck`, when installed.
- [ ] **Step 5: Commit** `feat: multiclaude add-in for bambu-mcp`.

### Task 11: Docs and final pass

**Files:**
- Modify: `README.md` (printer profiles, A1 status, the add-in install path), `docs/superpowers/specs/2026-11-22-bambu-mcp-design.md` ("Later: printer model as configuration": add "Done in the portable release; see the portable design")

- [ ] **Step 1:** Run the full suite: `uv run --no-sync pytest -q`. Expected: all pass, with none newly skipped except the Linux-only add-in tests on Windows.
- [ ] **Step 2:** Re-run Task 8's grep test and `git diff main --stat`. Check that no file under `src/bambu_profiles` or `multiclaude/` holds a secret-looking string.
- [ ] **Step 3: Commit** `docs: printer profiles and the add-in in the README`.

---

## Part B: service-notify upstream (multiclaude fork, separate repo)

Runs after Part A, or in parallel since it touches a different repo. Nothing is opened upstream without the owner's explicit approval of each PR.

### Task 12: Security and leak review

- [ ] **Step 1:** In the multiclaude fork, list the commits PR 1 and PR 2 would carry. PR 1 is `"frontdesk": "router"` plus the core hooks: `question_tapped`, its routing, and the add-in loader changes. PR 2 is `addons/service-notify/`. Use `git log upstream/main..memory-rework -- <paths>`.
- [ ] **Step 2:** Dispatch a fresh reviewer with spec 5.6's checklist:
  - token handling;
  - the press path;
  - only the owner's presses count;
  - the HTTP API bound to loopback;
  - size caps;
  - dedupe;
  - no owner ids, names, paths or tokens in code, tests, docs or commit messages;
  - `git log -p` of those commits checked for secrets.

  Give it the code, not our conclusions.
- [ ] **Step 3:** Write the findings to `docs/upstream/service-notify-review.md` in the fork, with each one's severity and a proposed fix.

### Task 13: Fixes and PR branches

- [ ] **Step 1:** Fix the findings in the fork, with tests, on `memory-rework`.
- [ ] **Step 2:** Build `upstream/frontdesk-router` from `upstream/main` with only PR 1's changes. Then build `upstream/service-notify` on top of it. Run the fork's test suite on each.
- [ ] **Step 3:** Show the owner both diffs' summaries and the review file. **Stop for approval.**
- [ ] **Step 4:** Only on the owner's yes, for each PR separately: push the branch and open the PR against korsicheg/multiclaude. The description says what the feature is for and that bambu-mcp's add-in depends on it.
