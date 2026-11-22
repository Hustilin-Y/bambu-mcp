# bambu-mcp Phase 0 (spike) Runbook

> A spike, not product code: run it with the owner present, step by step. Scripts written here are throwaway and live in `spike/` (ignored by later phases except the fixtures and the report). Steps use checkbox (`- [ ]`) syntax.

**Goal:** prove the P2S protocol path, the storage question, TLS and CLI slicing on real hardware, and capture real payloads as test fixtures for phase 1.

**Spec:** `docs/superpowers/specs/2026-11-22-bambu-mcp-design.md` (section 12, phase 0).

## Global Constraints

- The access code never goes into chat, git, logs or a command line. It is typed by the owner into `E:\Projects\bambu-mcp\spike\.env` (git-ignored) and later into `/srv/bambu/.env` on workshop.
- Downloads (Node packages, a Bambu Studio copy) are asked for first: name, source, size.
- No print starts without the owner standing at the printer and saying go.
- npm global installs from Claude land in a virtual AppData on this PC: anything Node runs from a local folder (`npx` or `npm install` inside `spike/`), or the owner runs `npm.cmd` in their own terminal.
- Fixtures are redacted before they are saved: serial, access code, IPs, Wi-Fi names, user names replaced by fixed placeholders (`SERIAL`, `ACCESS`, `10.0.0.2`).

## Review Focus

- The storage test can look like a success when the upload lands but the print never starts: success means the printer reports `RUNNING` for the uploaded `subtask_name`.
- A TLS test that falls back to insecure silently: the probe must fail loudly when verification fails.
- Slicing that "works" on a 200 mm bed: check `printable_area` in the output, not the exit code.
- Payloads captured only while idle: capture during a print too (prepare, first layers, finish).
- The access code changing after toggling LAN mode: re-read it from the screen after enabling Developer Mode.

---

### Task 1: Printer into LAN-only + Developer Mode (owner)

- [ ] **Step 1:** Owner notes the current firmware version (printer screen, Settings, Firmware).
- [ ] **Step 2:** Owner enables LAN Only, then Developer Mode (Settings, WLAN), reads the access code and serial off the screen and types them into `spike/.env` as `BAMBU_ACCESS_CODE=`, `BAMBU_SERIAL=`, `BAMBU_HOST=`.
- [ ] **Step 3:** Owner pins the printer's IP in MikroTik (static DHCP lease, as done for workshop).
- [ ] **Step 4:** Check Bambu Studio on the PC still sees the printer over LAN. Expected: the device tab shows it with live status.

### Task 2: Protocol probe and fixture capture

**Files:** Create `spike/probe.py`, `tests/fixtures/p2s/*.json`, `spike/.gitignore` (`.env`, `raw/`).

- [ ] **Step 1:** `spike/probe.py` connects with paho-mqtt to `BAMBU_HOST:8883`, user `bblp`, password from env, subscribes to `device/<serial>/report`, publishes `{"info": {"sequence_id": "0", "command": "get_version"}}` and `{"pushing": {"sequence_id": "0", "command": "pushall"}}`, writes every message to `spike/raw/<n>.json`. It publishes nothing else.
- [ ] **Step 2:** Run idle for 2 minutes. Expected: a full report with `print.gcode_state` `IDLE`, AMS trays with `tray_type`, `tray_color`, `remain` (expect `-1` for non-RFID spools), a nozzle diameter (either `print.nozzle_diameter` or `print.device.nozzle.info[]`).
- [ ] **Step 3:** Keep it running through one short print started from Bambu Studio (Task 4 cube). Capture prepare, the first layers, pause and resume if the owner agrees, finish.
- [ ] **Step 4:** Redact and save representative messages as `tests/fixtures/p2s/{version,pushall_idle,prepare,running_layer1,running_layer2,paused,finish,partial_update}.json`, plus any `hms` non-empty sample seen. Note which top-level keys P2S sends that ha-bambulab's comments don't show.

### Task 3: TLS verification

- [ ] **Step 1:** Download ha-bambulab's `pybambu/certs/bambu_p2s_250626.cert` (MIT) into `spike/certs/` (ask first).
- [ ] **Step 2:** Probe with `ssl.create_default_context(cafile=...)`, `check_hostname` with `server_hostname=<serial>`, then without hostname check. Expected: record which one validates. If neither, record the chain (`openssl s_client -showcerts`) and the decision becomes "pin the printer's certificate fingerprint" for phase 1.

### Task 4: Storage test (no USB stick)

- [ ] **Step 1:** In Bambu Studio export a 20 mm calibration cube for the P2S as `.gcode.3mf` (plate 1).
- [ ] **Step 2:** Run DMontgomery40/bambu-printer-mcp from `spike/dm/` (local npm install, asked first) or a 30-line `spike/upload.py` with an implicit-FTPS `ftplib.FTP_TLS` subclass: upload to `/cache/cube.gcode.3mf` and to the FTP root. Record each server reply.
- [ ] **Step 3:** With the owner at the printer, send `project_file` for the uploaded file (payload per DMontgomery40 `README.md` "H2/P2S project_file" and `ams_mapping2`). Expected success: a report with `gcode_state` `PREPARE`/`RUNNING` and `subtask_name` matching. Record the result.
- [ ] **Step 4:** If upload or start fails without storage: owner decides between a USB stick in the printer (recommended) and postponing phase 3. Repeat Steps 2 and 3 with the stick if chosen.

### Task 5: Camera

- [ ] **Step 1:** Grab one frame from `rtsps://bblp:<code>@<host>:322/streaming/live/1` with ffmpeg (URL read from `spike/.env` by a Python wrapper, never typed in a shell). Expected: a JPEG. If it fails, check the printer's "LAN Only Liveview" setting.
- [ ] **Step 2:** Try the same with PyAV in-process (`av.open(url, options={"rtsp_transport": "tcp"})`) and record peak RSS. This decides phase 1's camera backend.

### Task 6: CLI slicing on the PC

- [ ] **Step 1:** Find a way to keep a second, pinned Bambu Studio for the CLI (a release zip if Bambu publishes one, else a copy of the installed folder). Record the version.
- [ ] **Step 2:** `spike/flatten.py` resolves `inherits` and `include` of the owner's P2S machine preset, one process preset and one PLA filament preset from `%APPDATA%\BambuStudio` and the install's `resources\profiles\BBL` into full JSON in `spike/profiles/` (git-ignored). Assert `printable_area` is 256 x 256 and `nozzle_diameter` is 0.4.
- [ ] **Step 3:** Slice one print-ready CAD Workshop 3MF: `bambu-studio.exe --slice 0 --load-settings "machine.json;process.json" --load-filaments "pla.json" --export-3mf out.gcode.3mf <part>.3mf`. Record time taken, exit code, and from the output: `Metadata/plate_1.gcode` present, `slice_info.config` time and weight, thumbnails, bed size.
- [ ] **Step 4:** Run the same command from a non-elevated process started by a scheduled task (as slice-agent would) and note whether Smart App Control or a missing desktop interferes.

### Task 7: Report

**Files:** Create `docs/phase0-report.md`.

- [ ] **Step 1:** Write the report: firmware version, each task's result, the fixture list, TLS decision, storage decision, camera backend decision, slicing timings, and go or no-go for phases 1, 2 and 3 with the reasons.
- [ ] **Step 2:** Owner reads it and confirms the decisions. Phase 1 starts from them.
