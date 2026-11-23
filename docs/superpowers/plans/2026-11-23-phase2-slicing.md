# bambu-mcp Phase 2 (slicing) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** from a CAD Workshop print manifest to a sliced plate on the PC with a one-message summary (slot, key settings, time, grams, cost in UAH) from Claude Code or Telegram, plus spool loading and matching of the owner's prints to sliced jobs. The owner still starts prints in Bambu Studio (phase 3 adds print start).

**Architecture:** a second small service, `slice-agent`, runs on the PC under the owner's normal account and wraps a pinned Bambu Studio CLI behind a fixed HTTP API. `bambu-service` on workshop keeps jobs, wakes the PC through orchestra-gateway, sends the job to slice-agent, stores the result and reports it through the outbox. MCP gains job tools and `load_spool`.

**Tech Stack:** Python >= 3.12, uv, pytest, Starlette + uvicorn (slice-agent), httpx, existing bambu_mcp modules.

**Spec:** `docs/superpowers/specs/2026-11-22-bambu-mcp-design.md` (sections 5, 7, 8, 11, 12 phase 2). Facts: `docs/phase0-report.md` ("CLI recipe (verified)"). Manifest contract: `E:\Projects\cad-workshop\docs\superpowers\plans\2026-11-23-print-manifest.md` (schema 1, executed separately in the CAD Workshop repo). Phase 1 notes: `docs/phase1-notes.md`.

## Global Constraints

- Branch `phase2-slicing` from `phase1-monitor` (PR #1 not merged yet). Everything in the repo in English, Telegram texts in Russian, money in UAH.
- Never commit Bambu Studio presets, flattened profiles or HMS tables (AGPL): they are generated on the PC into `%LOCALAPPDATA%\bambu-mcp\profiles\`.
- slice-agent runs as the owner's standard (non-elevated) user, listens on `0.0.0.0:11436`, accepts requests only with its bearer token, and only from `192.168.88.11` (Windows firewall rule, documented). It never accepts paths, CLI arguments or code from a request: only a 3MF body, a profile set name from its config, and whitelisted overrides.
- Override whitelist with ranges: `sparse_infill_density` (0..100 %, sent as `"<n>%"`), `sparse_infill_pattern` (same list as PRINT_HINTS `infill_pattern`), `wall_loops` (1..10), `wall_generator` (`arachne`, `classic`), `enable_support` (bool), `support_type` (`tree(auto)`, `normal(auto)`), `brim_type` (`no_brim`, `auto_brim`, `outer_only`), `curr_bed_type` (`Textured PEI Plate`, `Cool Plate`, `Engineering Plate`, `High Temp Plate`, `Smooth PEI Plate`), `layer_height` (per nozzle: 0.4 -> 0.12, 0.16, 0.20, 0.24, 0.28).
- CLI recipe from phase 0, verbatim: run with `--slice 0 --orient 0`, `--arrange 1` for parts and `--arrange 0` for kits, `--curr-bed-type "<plate>"`, `--load-settings "machine.json;process.json"`, `--load-filaments "filament.json"`, `--export-3mf <bare name> --outputdir <dir>`; presets flattened (inherits, then include templates, then own keys; `from: "User"`, `inherits: <own system name>`, `*_settings_id` set; machine keys removed from process and filament); assert bed 256 x 256, height 256, nozzle matches.
- Slice timeout 300 s; one slice at a time on the PC (a second request gets 409 `busy`).
- Sliced output naming: `<part>-<job_id>-p<plate>.gcode.3mf` in `%USERPROFILE%\bambu-mcp\sliced\`; the owner prints these from Bambu Studio, and the service matches the print by `subtask_name` (Studio sends the file name without `.gcode.3mf`).
- Waking the PC: `POST {gateway}/wake` then poll `GET {gateway}/status` until `pc.state == "ready"`, 120 s budget, gateway key as bearer. slice-agent holds a pc-agent lease (`POST http://127.0.0.1:11435/lease`, holder `slice-agent`, ttl 600, renewed every 120 s, dropped at the end) for the whole slice.
- Only one new printer write in phase 2: `ams_filament_setting` for `load_spool`, with validated fields. Everything else stays read-only.
- Owner only: the job tools follow the same MCP auth as phase 1.

## Review Focus

- A manifest whose 3MF changed after the build (sha mismatch): refused with a clear message, never sliced. (Test in Task 2.)
- The PC does not wake within 120 s, or slice-agent is down: the job ends `slice_failed` with a Russian message saying the PC did not wake, nothing retried silently. (Test in Task 7.)
- A slice that falls back to a 200 mm bed or another nozzle: the output check rejects it even with exit code 0. (Test in Task 3.)
- The owner prints a file sliced by the service but renamed in Studio: no match, the phase 1 unmatched-print question is asked as before. (Test in Task 9.)
- A material mismatch between the manifest (PLA by default) and the loaded slots (PETG): the summary warns and preselects nothing silently wrong. (Test in Task 6.)

---

### Task 1: slice-agent package and profile flattening

**Files:**
- Create: `src/slice_agent/__init__.py`, `src/slice_agent/config.py`, `src/slice_agent/profiles.py`
- Modify: `pyproject.toml` (second package in the wheel, console script `slice-agent = slice_agent.app:main`)
- Test: `tests/slice_agent/test_profiles.py`

**Interfaces:**
- Produces:
  - `AgentConfig` dataclass loaded from `%LOCALAPPDATA%\bambu-mcp\slice-agent.toml` + env `SLICE_AGENT_TOKEN`, `PC_AGENT_TOKEN`: `studio_exe: Path`, `studio_data: Path` (default `%APPDATA%\BambuStudio`), `studio_resources: Path`, `profiles_dir: Path`, `sliced_dir: Path`, `listen: tuple[str, int]` (`0.0.0.0`, 11436), `machine: dict[str, str]` (nozzle "0.4" -> machine preset name), `process_sets: dict[str, str]` (`standard` -> `0.20mm Standard @BBL P2S`, `strong`, `draft` names set by the owner), `filaments: list[str]` (preset names the owner uses).
  - `resolve_preset(kind: str, name: str, roots: list[Path]) -> dict` (inherits, then includes, then own keys, cycle -> `ValueError`).
  - `flatten_all(cfg: AgentConfig) -> list[Path]` writes `machine-<nozzle>.json`, `process-<set>.json`, `filament-<slug>.json` with the CLI recipe's fixes and asserts; returns the paths. CLI: `uv run slice-agent flatten`.
- [ ] **Step 1: Tests** with a tiny fake Studio tree in `tmp_path` (`system/BBL/{machine,process,filament}` and `user/<id>/...`): `test_inherits_then_include_then_own`, `test_include_template_keys_override_parent`, `test_cycle_raises`, `test_settings_ids_and_inherits_set`, `test_machine_keys_removed_from_process_and_filament`, `test_bed_assertion_fails_on_200mm_bed`, `test_user_preset_found_after_system`.
- [ ] **Step 2:** Run `uv run pytest tests/slice_agent -q`. Expected: FAIL.
- [ ] **Step 3:** Implement, porting `spike/flatten.py`.
- [ ] **Step 4:** Run. Expected: PASS. Then on the PC: `uv run slice-agent flatten` against the real install, expect `assertions OK` for the P2S 0.4 machine (owner's machine, no printer needed).
- [ ] **Step 5:** Commit: `slice-agent: config and preset flattening`.

### Task 2: Manifest reader (service side)

**Files:**
- Create: `src/bambu_mcp/manifest.py`
- Test: `tests/test_manifest.py`, fixtures `tests/fixtures/manifest/{part,kit}.print.json` (hand-written per the CAD plan's schema 1)

**Interfaces:**
- Produces: `Manifest` (frozen dataclass: `schema`, `kind: Literal["part","kit"]`, `name`, `file`, `file_sha256`, `print_ready`, `material`, `nozzle_mm`, `plates: tuple[PlateInfo, ...]`, `checks`, `hints`, `estimate_g`, `source: dict`), `PlateInfo(index, objects, checks, hints, estimate_g)`; `load_manifest(path: Path) -> Manifest`; `verify_file(manifest, file_bytes: bytes) -> None` (raises `ManifestError`).
- [ ] **Step 1: Tests:** `test_load_part_manifest`, `test_load_kit_manifest_plates`, `test_rejects_schema_2`, `test_rejects_not_print_ready` (message contains `--print-ready`), `test_sha_mismatch_rejected`, `test_unknown_hint_key_rejected`.
- [ ] **Step 2-4:** RED, implement, GREEN.
- [ ] **Step 5:** Commit: `Manifest reader`.

### Task 3: Slicer runner (PC side)

**Files:**
- Create: `src/slice_agent/runner.py`, `src/slice_agent/output.py`
- Test: `tests/slice_agent/test_runner.py`, `tests/slice_agent/test_output.py`, fixture `tests/fixtures/sliced/example-bracket.gcode.3mf` (the phase 0 output from `spike/out/`, it contains no presets beyond the project config: check and drop it if it embeds full Bambu presets, then build a minimal synthetic one instead)

**Interfaces:**
- Produces:
  - `Overrides` (validated mapping of the whitelist; `Overrides.parse(raw: dict, nozzle: str) -> Overrides`, raises `ValueError` naming the key).
  - `build_command(cfg, *, input_3mf: Path, out_name: str, nozzle: str, process_set: str, filament: str, plate_type: str, kit: bool, overrides: Overrides) -> list[str]` (overrides as `--<key-with-hyphens>=<value>`).
  - `run_slice(cmd: list[str], out_dir: Path, timeout_s: int = 300, runner=subprocess.run) -> SliceRun` (`return_code`, `error_string` from `result.json`, `log_tail`).
  - `read_output(path: Path, plate: int) -> SliceResult` (`plates: list[PlateResult(index, minutes, grams_by_filament: dict[str, float], png: bytes | None)]`, `printer_model`, `nozzle`, `bed_xy`) and `check_output(result, nozzle: str) -> None` raising `OutputError` for missing `Metadata/plate_<n>.gcode`, `printer_model != "Bambu Lab P2S"`, bed other than 256 x 256, nozzle mismatch.
- [ ] **Step 1: Tests:** `test_overrides_whitelist_and_ranges` (unknown key, 101 %, layer 0.3 on 0.4 nozzle -> ValueError), `test_build_command_matches_recipe` (exact argv for a part and for a kit), `test_run_slice_timeout_kills_and_reports`, `test_nonzero_return_reads_error_string`, `test_read_output_time_and_grams`, `test_check_output_rejects_200mm_bed`, `test_check_output_rejects_wrong_nozzle`.
- [ ] **Step 2-4:** RED, implement (`subprocess.run(..., timeout=)` waits for the GUI-subsystem exe correctly; no shell), GREEN.
- [ ] **Step 5:** Commit: `slice-agent: CLI runner and output checks`.

### Task 4: slice-agent HTTP API

**Files:**
- Create: `src/slice_agent/app.py`, `src/slice_agent/lease.py`
- Test: `tests/slice_agent/test_app.py`

**Interfaces:**
- Produces HTTP API (bearer `SLICE_AGENT_TOKEN`, constant-time compare, 401 otherwise):
  - `POST /slice` multipart: part `file` (3MF, max 50 MB), part `job` JSON `{"job_id", "name", "kit": bool, "plates": [int], "nozzle", "process_set", "filament", "plate_type", "overrides": {}}` -> 200 `{"file": "<out name>", "plates": [{"index", "minutes", "grams": {...}}], "slicer_version"}`, 409 `{"error": "busy"}`, 422 `{"error": "bad_request", "detail"}`, 500 `{"error": "slice_failed", "detail": <error_string>, "log_tail"}`.
  - `GET /sliced/{name}` (only names matching the output pattern, from `sliced_dir`), `GET /sliced/{name}/plate/{n}.png`, `GET /health` (no auth, `{"ok": true}` only).
  - `Lease` context manager: takes, renews and drops the pc-agent lease; a pc-agent error is logged and slicing continues.
- [ ] **Step 1: Tests** with a fake runner and fake pc-agent (`httpx.MockTransport`): `test_rejects_missing_token`, `test_slice_happy_path_names_output`, `test_second_concurrent_slice_gets_409`, `test_bad_override_422`, `test_slice_failure_500_with_detail`, `test_sliced_path_traversal_rejected`, `test_lease_taken_and_dropped`, `test_lease_failure_does_not_block_slice`.
- [ ] **Step 2-4:** RED, implement, GREEN.
- [ ] **Step 5:** Commit: `slice-agent: HTTP API with lease`.

### Task 5: Jobs in the store and file intake

**Files:**
- Modify: `src/bambu_mcp/store/db.py` (schema v4: `jobs(id, name, kind, manifest_json, file_sha256, created_at, status, settings_json, result_json, sliced_file, error)`), `src/bambu_mcp/store/models.py` (`Job`)
- Create: `src/bambu_mcp/jobs.py` (job files under `data_dir/jobs/<id>/`), `src/bambu_mcp/upload.py` (CLI)
- Modify: `src/bambu_mcp/mcp_server.py` (route `POST /jobs` multipart behind the same bearer middleware), `pyproject.toml` (script `bambu-mcp = bambu_mcp.upload:main`)
- Test: `tests/test_jobs.py`, `tests/store/test_db.py` (migration v3 -> v4), `tests/test_upload.py`

**Interfaces:**
- Produces: `create_job_from_bytes(store, settings, manifest_bytes, file_bytes) -> Job`, `create_job_from_path(store, settings, manifest_path: Path, allowed_root: Path) -> Job` (path must resolve inside `allowed_root`, no symlink escape), `Job` statuses `new`, `slicing`, `sliced`, `slice_failed`; CLI `uv run bambu-mcp upload <manifest> [--url] [--token-env BAMBU_MCP_TOKEN]` prints the job id.
- Settings additions: `jobs_root_allowed: Path | None` (default `None` = path intake off), `gateway_url`, `slice_agent_url`; secrets `GATEWAY_KEY`, `SLICE_AGENT_TOKEN`.
- [ ] **Step 1: Tests:** `test_create_job_stores_files_and_row`, `test_sha_mismatch_refused`, `test_path_outside_root_refused`, `test_symlink_escape_refused`, `test_upload_cli_posts_multipart` (MockTransport), `test_jobs_route_requires_token`, `test_migrate_v3_to_v4`.
- [ ] **Step 2-4:** RED, implement, GREEN.
- [ ] **Step 5:** Commit: `Jobs: store, file intake, upload CLI`.

### Task 6: Settings resolution, slot preselection and the summary

**Files:**
- Create: `src/bambu_mcp/proposal.py`
- Modify: `src/bambu_mcp/messages.py`
- Test: `tests/test_proposal.py`

**Interfaces:**
- Produces:
  - `NAMED_SETS = {"standard": {}, "strong": {"wall_loops": 4, "sparse_infill_density": 25}, "draft": {"sparse_infill_density": 10, "layer_height": 0.28}}` (owner may edit later).
  - `resolve_settings(explicit: dict, last_job: dict | None, hints: dict, named_set: str) -> dict` (precedence explicit > last successful job of the same part > hints > named set), mapping hint keys to override keys (`walls` -> `wall_loops`, `infill_pct` -> `sparse_infill_density`, `infill_pattern` -> `sparse_infill_pattern`, `supports` -> `enable_support`/`support_type`, `brim` -> `brim_type`); supports enabled by default only when `checks.supports_suggested`.
  - `pick_slot(trays, spools_by_slot, material: str, last_color: str | None) -> SlotChoice(ams, slot, spool_id, warning: str | None)`: a loaded slot whose tray_type equals the material, preferring the last colour; none matching -> the first loaded slot plus a Russian warning naming both materials.
  - `messages.render_proposal(job, settings, slot_choice, plate_type, estimate) -> str` and `render_sliced(job, plate_results, cost_lines) -> str` (Russian, format: "<name>, пластина 1: слот 3 PETG чёрный (~310 г), 4 стенки, 25% gyroid, без поддержек, Textured PEI. ~1 ч 40 мин, 14 г, ~9.40 грн").
- [ ] **Step 1: Tests:** `test_precedence_explicit_over_last_over_hints`, `test_supports_only_when_suggested`, `test_pick_slot_material_match_prefers_last_color`, `test_pick_slot_mismatch_warns`, `test_render_sliced_ru_format`.
- [ ] **Step 2-4:** RED, implement, GREEN.
- [ ] **Step 5:** Commit: `Job settings, slot choice and summaries`.

### Task 7: Slice client and job flow in the service

**Files:**
- Create: `src/bambu_mcp/slicer.py`
- Modify: `src/bambu_mcp/service.py` (job flow, proposal buttons `job:<id>:slice` and `job:<id>:change`)
- Test: `tests/test_slicer.py`, `tests/test_service.py` (extend)

**Interfaces:**
- Produces: `GatewayWaker(url, key, http)` with `async wake_and_wait(budget_s: float = 120) -> bool`; `SliceClient(url, token, http)` with `async slice(job, settings, file_bytes) -> SliceOutcome` and `async fetch_png(name, plate) -> bytes | None`; `Service.propose(job_id)` (sends the proposal via the outbox), `Service.slice_job(job_id, explicit: dict | None) -> Job` (wake, slice, store result, compute cost per plate with phase 1 `cost.py` using the chosen spool's price and `print_kwh` of the predicted minutes, enqueue `render_sliced` with the plate PNG).
- A press on `job:<id>:slice` starts `slice_job` in the background; `job:<id>:change` edits the message to "Скажи ассистенту, что поменять: стенки, заполнение, поддержки, пластину".
- [ ] **Step 1: Tests:** `test_wake_polls_until_ready`, `test_wake_timeout_returns_false`, `test_slice_job_happy_path_stores_result_and_enqueues_summary`, `test_pc_not_woken_marks_failed_and_tells_owner`, `test_slice_agent_busy_retries_once_after_30s`, `test_slice_failure_detail_in_message`, `test_slice_press_starts_job_once` (a second press is ignored while slicing).
- [ ] **Step 2-4:** RED, implement, GREEN.
- [ ] **Step 5:** Commit: `Service: wake the PC, slice jobs, report summaries`.

### Task 8: MCP job tools

**Files:**
- Modify: `src/bambu_mcp/mcp_server.py`
- Test: `tests/test_mcp_server.py` (extend)

**Interfaces:**
- Tools (exact names): `create_job(manifest_path: str)` (path intake, only when `jobs_root_allowed` is set), `propose_job(job_id: int)`, `slice_job(job_id: int, settings: dict | None = None)`, `job_status(job_id: int)` (status, settings, per-plate minutes/grams/cost, sliced file name), `jobs(limit: int = 10)`. Errors as ToolError with Russian-free English text.
- [ ] **Step 1: Tests:** exact tool list, `test_slice_job_validates_overrides`, `test_job_status_has_no_paths_outside_names`, `test_create_job_disabled_without_root`.
- [ ] **Step 2-4:** RED, implement, GREEN.
- [ ] **Step 5:** Commit: `MCP: job tools`.

### Task 9: Match prints to sliced jobs

**Files:**
- Modify: `src/bambu_mcp/journal.py`, `src/bambu_mcp/store/db.py` (`prints.job_id`, `prints.plate`)
- Test: `tests/test_journal.py` (extend)

**Interfaces:**
- On `started`, if `subtask_name` matches `<part>-<job_id>-p<plate>` for a `sliced` job, the row gets `job_id`, `plate`, the job's predicted grams for that plate, and the spool chosen for the job (via `rebook_grams`); the unmatched-spool question is skipped. A non-matching name keeps phase 1 behaviour.
- [ ] **Step 1: Tests:** `test_match_by_subtask_name`, `test_renamed_file_falls_back_to_question`, `test_unknown_job_id_ignored`, `test_match_deducts_once_across_restart`.
- [ ] **Step 2-4:** RED, implement, GREEN.
- [ ] **Step 5:** Commit: `Journal: match prints to sliced jobs`.

### Task 10: `load_spool` (one printer write)

**Files:**
- Modify: `src/bambu_mcp/printer/link.py` (a second allowlist for `ams_filament_setting`), `src/bambu_mcp/mcp_server.py` (tool `load_spool(spool_id: int, ams: int, slot: int)`)
- Test: `tests/printer/test_link.py`, `tests/test_mcp_server.py`

**Interfaces:**
- `PrinterLink.set_tray(ams: int, slot: int, tray_info_idx: str, tray_type: str, color_rgba: str, nozzle_temp_min: int, nozzle_temp_max: int) -> None` builds `{"print": {"sequence_id": "0", "command": "ams_filament_setting", "ams_id", "tray_id", "tray_info_idx", "tray_color", "nozzle_temp_min", "nozzle_temp_max", "tray_type"}}` (field set from ha-bambulab's `AMS_FILAMENT_SETTING_TEMPLATE`), validates every field (ams 0..3, slot 0..3, colour `^[0-9A-F]{8}$`, temps 150..320, type from a closed list), and is the only non-read command `_publish` accepts. The tool maps the spool's filament preset to `tray_info_idx` and temperatures from the flattened filament preset metadata stored with the spool (`filament_id`, `nozzle_temperature_range_low/high`); then `map_slot`.
- [ ] **Step 1: Tests:** `test_set_tray_payload_exact`, `test_set_tray_rejects_bad_colour_and_ids`, `test_publish_still_refuses_other_commands`, `test_load_spool_maps_slot_and_sends_once`.
- [ ] **Step 2-4:** RED, implement, GREEN.
- [ ] **Step 5:** Commit: `load_spool: set AMS slot material and colour`.
- [ ] **Step 6 (owner present, printer idle):** one live `load_spool` on an empty-or-known slot, check the printer screen and the next report.

### Task 11: PC deployment of slice-agent

**Files:**
- Create: `deploy/slice-agent/README.md`, `deploy/slice-agent/install.ps1` (registers a scheduled task "at logon", current user, non-elevated; adds the firewall rule for TCP 11436 from 192.168.88.11 only; both idempotent, printing what they change), `deploy/slice-agent/slice-agent.example.toml`
- Modify: `deploy/README.md` (service side: `gateway_url`, `slice_agent_url`, `GATEWAY_KEY`, `SLICE_AGENT_TOKEN`, a new gateway key in `gateway.toml` for bambu-service)
- Test: `tests/test_deploy.py` (example config loads)

- [ ] **Step 1:** README covers: the pinned Bambu Studio copy (copy `C:\Program Files\Bambu Studio` to `%LOCALAPPDATA%\bambu-mcp\studio-<version>\` after closing Studio, record the version, upgrade deliberately with the golden test: slice `example-bracket` and compare minutes and grams within 3 %), `uv run slice-agent flatten`, the Smart App Control caveat (run through `uv run`, like pc-agent; if blocked see the project memory note), token hygiene as in phase 1.
- [ ] **Step 2:** `install.ps1` is not run by the implementer. The owner runs it.
- [ ] **Step 3:** Commit: `Deploy: slice-agent on the PC`.
