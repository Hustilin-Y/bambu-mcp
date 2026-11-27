# bambu-mcp: design

Date: 2026-11-22. Status: draft for the owner's approval.
Inputs: the brainstorming conversation of 2026-11-22, the independent review `E:\Projects\bambu-mcp-design-review.md` (referred to as "the review", findings by number), CAD Workshop (`E:\Projects\cad-workshop`), llm-orchestra `TZ.md` (pc-agent, orchestra-gateway).

Marks: **[owner]** = decided by the owner, **[call]** = my call, the owner may override, **[builder]** = left to whoever builds it, **[verify]** = a fact to confirm on the real printer in phase 0.

## 1. Intent

The owner designs parts as code in CAD Workshop and prints them on a Bambu Lab P2S with an AMS. Today the last mile is manual: open the 3MF in Bambu Studio, pick filament and settings, slice, send. The goal is that Claude (Claude Code on the PC, or the multiclaude assistant in Telegram) takes a built part all the way to a running print, asking the owner only what matters, and then watches the print and reports back.

Why now: the owner wants to order and print parts from Telegram (the CAD Workshop add-in already builds parts on the server), and switching the printer to LAN-only to allow that removes Bambu Handy, so Telegram has to become the phone interface anyway.

## 2. Goals, non-goals, anti-goals

Goals:
- From a built CAD Workshop part or kit to a sliced plate with a one-message summary (slot, key settings, time, grams, cost in UAH), from Claude Code or Telegram.
- Monitoring with Telegram notifications and camera snapshots, replacing Bambu Handy at home.
- Start a print after an explicit confirmation that the model cannot fake.
- Spool inventory (all spools are without RFID), the owner's build plates, prices per kg, a print journal with fit feedback.

Non-goals (v1 and v1.1):
- Multi-colour or multi-material prints (CAD Workshop kits use one extruder per object today).
- A print queue, print farms, more than one printer.
- Automating the Bambu Studio GUI. Using Bambu Cloud or Bambu Connect.
- Remote access from outside the home LAN.

Anti-goals (would make it a failure even if it "works"):
- A print starting without the owner's deliberate confirmation of that exact sliced file, or starting twice.
- Printing onto a part still on the bed, with the wrong nozzle, plate or plastic.
- The access code leaking into logs, chat, commits or process lists.
- CAD Workshop losing its "never online, never talks to the printer" property.
- The server running out of memory and taking the assistant or Lecture Bot down.

## 3. Constraints

- Printer: Bambu Lab P2S + AMS, nozzle 0.4 by default (0.2/0.6/0.8 profiles exist in `printer.toml`), 256 x 256 x 256 mm bed.
- **[owner]** Printer in LAN-only + Developer Mode. Third-party print start on current firmware needs it. Bambu Handy and cloud stop working. Bambu Studio on the PC still works over LAN (manual fallback).
- **[owner]** Slicing runs on the PC (Windows 11, sleeps or hibernates, woken by WoL through `orchestra-gateway`). Never on the server.
- Server "workshop": Ubuntu, ~3 GB RAM shared with the multiclaude assistant (up to 2.1 GB), Lecture Bot, orchestra-gateway and CAD builds (up to 1.2 GB). 21 GB root SSD, bulky data on `/srv`. New services get a `MemoryMax`.
- **[owner]** Telegram messages and buttons from services go through a generic multiclaude add-in, built in its own session (branch `claude/service-notify-addon-e6c6a2` in `E:\Projects\multiclaude`). This design consumes its API, it does not define it.
- **[owner]** All spools are without RFID. The AMS reports `remain = -1` for them, so remaining filament is tracked by the connector.
- **[owner]** The owner does not want an SD card or USB drive in the printer. **[verify]** The P2S can print from internal eMMC, but Bambu Studio reaches it through Bambu's closed network plug-in. A plain FTPS upload probably needs a USB stick. Phase 0 decides (section 12).
- Smart App Control on the PC randomly blocks unsigned uv venv `python.exe`. Any Python service on the PC must use the same workaround as pc-agent.
- Licences: Bambu Studio is AGPL-3.0 (calling the binary is fine, its profiles and HMS tables are not copied into the repo), DMontgomery40/bambu-printer-mcp is GPL-2.0 (read as documentation only), ha-bambulab and bambulabs_api are MIT (code may be copied with attribution).
- Owner's conventions: everything in repos in English, talk in Russian, prices in UAH, print files as 3MF with all parts of a set in one file on separate plates.

## 4. Approaches considered

1. **Fork an existing MCP server (DMontgomery40/bambu-printer-mcp, TypeScript, GPL-2.0).** Most complete, P2S routing known. Rejected: it starts prints without a prompt by design (the opposite safety model), brings Node to a 3 GB server, assumes slicer and printer client on one machine, and a fork drifts from a very active upstream.
2. **One MCP server on the PC with the slicer.** Simple, but monitoring dies when the PC hibernates and Telegram would need the PC awake for everything. Rejected.
3. **[owner + review] Chosen: a small Python service on the server that owns the printer, a fixed slicer agent on the PC, MCP as a thin facade.** Matches pc-agent, orchestra-gateway and CAD Workshop (Python, uv, pytest, same deploy style). Protocol code written against ha-bambulab (MIT). DMontgomery40 is run once in phase 0 as a reference and its docs are used as a protocol reference.

Phasing **[owner]**: monitor first, then slicing, print start last (review finding 4).

## 5. Architecture

```
Claude Code (PC) ──┐ MCP, Streamable HTTP, LAN, bearer token
multiclaude asst. ─┴──► bambu-service (workshop, systemd, Python)
                          ├─ printer owner: the only MQTT client of the connector, FTPS, camera
                          ├─ job store: SQLite + /srv/bambu/jobs/<id>/
                          ├─ inventory: spools, slots, plates, prices
                          ├─ journal
                          ├─ notifier ──► multiclaude service-notify add-in ──► Telegram
                          ├─ MCP facade (stateless, all state in the store)
                          └─ slice client ──► orchestra-gateway /wake ──► PC
                                                                         └─ slice-agent (fixed CLI wrapper)
```

- **bambu-service** on workshop. One process. Budget about 100 MB **[call]**, `MemoryMax=150M`. Listens on `192.168.88.11` for the PC's MCP client and on `127.0.0.1` for the assistant. Bearer token per client. Secrets (access code, serial, tokens) in `/srv/bambu/.env`, mode 600.
- **slice-agent** on the PC. A separate small service started at logon like pc-agent, but as a standard user (not "highest rights"), because it parses semi-trusted 3MF files from downloaded remixes **[call]**. Firewall: only `192.168.88.11`. Bearer token. Input: a 3MF, a profile set name from a fixed list, whitelisted overrides. Output: `.gcode.3mf`, plate PNGs, parsed slice info (time, grams per filament), log tail. Never takes paths, arguments or code. Whether it is its own process or a fixed endpoint inside pc-agent is decided in phase 0 by how Smart App Control treats it **[builder]**.
- **Getting files to the service** **[call]**: a 3MF is too big to pass through the model as a tool argument, and the service cannot read the PC's disk. Two intake paths, both ending in the same `create_job`:
  - From the PC: a small CLI in this repo, `uv run bambu-mcp upload <path to out/<name>.print.json>`, which Claude Code runs. It reads the manifest and the 3MF it names, uploads both over HTTP with the PC's token, and prints the job id. It lives in bambu-mcp, not in CAD Workshop, so CAD Workshop stays offline.
  - From the Telegram assistant on workshop: `create_job` takes a path, accepted only under an allowlisted root (the CAD Workshop checkout on workshop, `~/workspaces/shiba/cad-workshop/**/out/`), read with group permissions, no symlinks followed outside the root.
- **Notifier interface** **[call]**: the service talks to Telegram through one small interface. Until the service-notify add-in is ready, a stand-in implementation only logs, so phase 1 does not wait for the add-in. Its API is defined in the add-in's own session.
- **Waking the PC**: through `orchestra-gateway` (`POST /wake`, `GET /status`), and a pc-agent lease held for the whole slice so the PC does not hibernate mid-slice. No second WoL implementation.
- **Repository** **[call]**: `E:\Projects\bambu-mcp`, public on GitHub, MIT, one repo with two packages (`service/`, `slice_agent/`), shared protocol models, `uv`, `pytest`, GitHub Actions on Ubuntu (Windows-only parts of slice-agent tested with a fake slicer).
- Deploy on workshop like orchestra-gateway: code under `/srv/bambu/app`, data under `/srv/bambu/{jobs,db}`, systemd unit. Every deploy is a separate approval by the owner.

## 6. CAD Workshop contract

CAD Workshop keeps its rules: no network, no printer. It only writes one more file (review finding 1):

- `out/<name>.print.json` next to the print-ready 3MF, for parts and kits. Fields: schema version, kind, name, file and its sha256, `print_ready`, source (model file, its hash, git commit, `--set` overrides), profile (nozzle, material, tolerances), plates with objects and bounding boxes, check results (islands, bodies above bed, overhang share, base ratio, `supports_suggested`), CAD weight estimate, hints.
- Hints come from an optional `PRINT_HINTS` dict in `model.py`, validated against a closed key list (walls, infill %, infill pattern, supports, brim, material and the reason for it). A typo fails the build.
- The manifest is written for `build --print-ready` and `kit`. The connector refuses a manifest with `print_ready: false`, so the orientation the checks judged is the orientation that gets printed.
- CAD Workshop's `CLAUDE.md`, cad skill and add-in `rules.md` change from "don't slice, don't touch the printer" to "CAD Workshop itself never does it. When the owner asks to print, hand the manifest to the bambu-mcp tools".
- The connector never writes into the CAD Workshop repo. A CAD session can ask `print_history(part)` and write a part README history line itself.

## 7. User flow and settings policy

1. A part is built (`--print-ready`) or a kit is made. Claude calls `create_job` with the manifest and 3MF. The service checks the file hash.
2. The service proposes everything in **one message** (review finding 7):
   > lamp-arm, plate 1: slot 3 PETG black (~310 g left), 4 walls, 25% gyroid, no supports (no islands found), textured plate. [Slice] [Change settings]
3. Defaults, in order of precedence: what the owner said in this conversation, the last successful job of the same part, `PRINT_HINTS`, a named setting set ("standard", "strong", "draft"), the process preset.
4. Asked only when they matter **[owner + call]**:
   - Slot: preselected as the loaded slot whose material matches the manifest, the colour used last time for this part preferred. Always shown.
   - Infill % and pattern, wall count: always shown, changeable.
   - Supports: shown only when the CAD checks suggest them (default tree, auto).
   - Wall generator (Arachne default), layer height (from the nozzle profile), brim, seams, top and bottom layers, speeds, temperatures, plate type: not asked. Changeable under "Change settings" or by saying so.
5. Slice (section 8). The result message: plate image, time, grams, cost in UAH, slot, plate type, warnings.
6. Phase 3 only: [Print plate 1] with the confirmation gate (section 9). Before phase 3 the owner starts the print from Bambu Studio on the PC.
7. Monitoring and the "how did it come out" question (sections 10, 11).

A kit is sliced once, each plate is its own print, confirmation and journal row. Kits never continue to the next plate on their own.

## 8. Slicing

- Bambu Studio CLI on the PC, a **separate version-pinned copy** for the CLI, apart from the auto-updating GUI install **[call, owner may refuse]**. The slicer version goes into each job. Upgrades are deliberate, followed by a golden test (slice a known part, compare time and grams within a few percent).
- Profiles: a setup script on the PC flattens the owner's machine, process and filament presets (system + user, resolving `inherits` and `include`) into full JSON files kept on the PC, not committed (AGPL and they follow the installed Studio) **[call]**. Assertions after flattening: `printable_area` 256 x 256, `printable_height` 256, nozzle diameter matches the profile name. This guards the known "falls back to a 200 mm bed" bug.
- Overrides are a closed list with validated ranges: `sparse_infill_density`, `sparse_infill_pattern`, `wall_loops`, `wall_generator`, `enable_support`, `support_type`, `brim_type`, `curr_bed_type`, `layer_height` from a fixed set per nozzle. Nothing else, ever.
- No arranging or reorienting: print-ready parts and kits are already placed.
- Output check before anything else: the `.gcode.3mf` has `Metadata/plate_<n>.gcode`, the printer model is P2S, the bed is 256 x 256, the nozzle matches.
- Timeout and log tail on failure. Never fall back to another profile silently.

## 9. Printing and the confirmation gate (phase 3)

- `prepare_print(job_id, plate)` runs the pre-flight checks and returns the summary plus a `confirm_id`. The service stores a hash over (gcode sha256, plate, AMS mapping, printer serial, nozzle, plate type), valid for 10 minutes.
- On the button press the service re-reads printer and AMS state and refuses if anything in the hash changed, the confirmation expired, or a pre-flight check now fails.
- The confirmation is always a Telegram button sent by the service through the notify add-in, for requests from Claude Code and from Telegram alike, and the press goes to the service, never to Claude (section 16).
- Pre-flight (review finding 5): printer idle or finished, no fatal or serious HMS, nozzle diameter matches, plate type confirmed, slot material equals the filament preset's (hard stop), colour difference is a warning, the spool's tracked remainder covers grams x 1.15, the manifest's material intent (for example PETG near a lamp) is respected or warned about, the bed is confirmed clear by the owner in the confirmation itself.
- Upload by FTPS, size check, then the `project_file` command with a unique `subtask_name`/`task_id`. Then watch reports for 15 to 30 s and answer `started` or `unconfirmed`. **Never resend** on `unconfirmed`: tell the owner.
- Exposed physical controls: pause (while printing) and stop (while preparing, printing or paused; stop asks for a confirmation). Not exposed at all: resume, raw G-code, axis moves, heating, AMS load or unload, HMS clearing, file deletion, LAN and Developer Mode settings.

## 10. Monitoring and notifications (phase 1)

- The service holds one MQTT session with verified TLS (ha-bambulab ships a P2S CA, **[verify]** that the chain validates). It asks for a full report on connect and after reconnects.
- One event state machine: `started`, `first_layer_done`, `paused`, `hms_warning`, `hms_error`, `finished`, `failed`, `cancelled`. Notifications only on transitions, deduplicated per (job, event).
- Camera: one frame from RTSPS port 322 with ffmpeg on demand, after the first layer and at the end. The camera URL embeds the access code, so it is passed through a file or environment, never the command line. No live stream.
- HMS texts in English, taken from the installed Bambu Studio or ha-bambulab at setup time **[builder]**, so a message says "Filament runout in AMS slot 3", not a hex code.
- Prints started outside the connector (Bambu Studio, the printer screen) are monitored too and get a journal row marked "external". **[call]** Before phase 3 every print is external, so the service matches it to a sliced job by `subtask_name` (Bambu Studio sends the file or plate name, and slice-agent names its output `<part>-<job id>-p<plate>.gcode.3mf`). A matched print takes the job's grams, spool and part. An unmatched one gets one Telegram question with buttons: which spool (the slot's mapped spool preselected) and "deduct an estimate" or "don't deduct".
- Where a sliced file waits for the owner before phase 3: slice-agent keeps its output on the PC in a fixed folder (`%USERPROFILE%\bambu-mcp\sliced\`), so the owner opens it in Bambu Studio there, whether the slice was asked from Claude Code or Telegram.
- Auth failures are reported as "access code changed?" with no retry loop. Connection errors use backoff.

## 11. Inventory, plates, prices, journal

- **Spools** **[owner]**: id, brand, material, colour (name and hex), the Bambu Studio filament preset it prints with **[call]** (chosen from the flattened presets the setup script found on the PC), price in UAH, net weight at start, current estimate. The preset decides `--load-filaments` and the material hard stop. Remaining = start weight minus slicer grams of finished prints (a failed print counts the grams up to its progress, an estimate). The owner can correct it by weighing the spool.
- **Loading a spool**: "spool 7 in slot 2" through Claude or Telegram. The service records the mapping and sets the slot's material and colour on the printer (the same MQTT command Bambu Studio sends) **[call]**. Without RFID this is what keeps the AMS report and the inventory consistent. This is a printer write without motion, so it arrives in phase 2, not phase 1.
- **Plates** **[owner]**: the owner's plates (type, name), one default. The plate type is part of every summary and confirmation.
- **Prices**: per spool. Fallback: price per kg per material in the service config. Cost = filament + electricity, shown as two lines and a total.
  - Filament = slicer grams (including purge and supports) x the spool's price.
  - **Electricity [owner]: included, tied to the current tariff.** kWh = print time x average power for the material + a fixed heat-up allowance. Defaults **[call]**: PLA 200 W (Bambu's P2S FAQ, steady state at 220 V), other materials and the heat-up allowance set in config and refined once from a smart-plug measurement if the owner has one. Actual kWh in the journal uses the actual print time.
  - Tariff in the service's data, not hard-coded: value in UAH/kWh with the date it applies from, a history kept so old journal rows keep the price they were printed at. Default 4.32 UAH/kWh (household tariff, unchanged as of 2026-11-22). A two-zone meter is supported: day and night rates, and the print's kWh split by its start and end time. The owner changes the tariff with one command ("тариф 4.80 с 1 января") **[call]**. The service does not fetch tariffs from the web: Ukrainian tariffs change by Cabinet decision a few times a year and no stable machine-readable source exists. Instead the summary says which tariff and date it used, and the service reminds the owner once a quarter to check it.
- **Journal**: one row per plate print: part, kit, plate, model hash, git commit, overrides, tolerances from the manifest, slicer version, profile hashes, spool and slot, predicted and actual time and grams, outcome, failure reason, fit feedback, photos. After `finished` one question with buttons: ok, tight, loose, failed. Photos and gcode kept for N days or the last M jobs per part, rows forever **[builder: N, M]**.
- The journal suggests tolerance changes per material after enough feedback. It never edits `printer.toml`.

## 12. Phases and what "done" means

**Phase 0, spike (an afternoon, owner present).**
- Owner: enable LAN-only + Developer Mode, note the firmware version, pin the printer's IP in the MikroTik, hand the access code over a safe path (it goes straight into `/srv/bambu/.env` on workshop).
- Run DMontgomery40/bambu-printer-mcp on the PC as a throwaway reference: status, AMS, camera snapshot.
- **Storage test**: try an FTPS upload and a `project_file` start of a GUI-exported calibration cube **without** a USB stick. If it fails, the owner decides: a USB stick in the printer (recommended) or phase 3 waits.
- TLS verification with the ha-bambulab P2S CA.
- Pinned Bambu Studio CLI with flattened presets on one print-ready CAD Workshop 3MF. Check bed size, thumbnails, slice info, and how long a slice takes.
- Smart App Control check for a slice-agent process.
- Done = a written phase 0 report in the repo with redacted real payloads saved as test fixtures and a go or no-go for each later phase.

**Phase 1, monitor (v1, part 1).** Repo, CI, bambu-service with the read-only printer owner, state machine, notifications through the add-in, camera snapshots, journal of external prints, spool, plate and price inventory (data only), MCP read tools. Done = a week of real prints reported correctly in Telegram, memory under budget on workshop.

**Phase 2, slicing (v1, part 2).** CAD Workshop manifest and `PRINT_HINTS` (in the CAD Workshop repo, with tests and the rule changes), slice-agent on the PC, wake and lease, profiles and assertions, whitelisted overrides, kits, summary with cost, spool loading command. The owner starts prints from Bambu Studio. Done = parts and a kit sliced from Claude Code and from Telegram with time and grams within a few percent of the GUI.

**Phase 3, print start (v1.1).** Section 9 in full, pause and stop. Done = confirmed prints from both entry points, `unconfirmed` and expiry paths tested against fixtures and once on the printer with the owner present.

**Later.** Multi-colour, queue, tolerance suggestions, OrcaSlicer, live camera, timelapse download, AMS drying.

**Later, asked by the owner (2026-11-27): printer model as configuration.** Today the code assumes the owner's P2S. The owner wants a printer model to be a setting: a "model profile" that holds everything printer-specific, so that another Bambu model (or a second printer) is a new profile, not a code change. What is hard-coded now, file by file (2026-11-27, branch `phase3-print`):

| Where | What is P2S-specific |
|---|---|
| `slice_agent/config.py` | Default presets: machine `Bambu Lab P2S 0.4 nozzle`, process `0.20mm Standard @BBL P2S`, filament `Generic PETG @BBL P2S`, resources folder `profiles\BBL` |
| `slice_agent/profiles.py` | Flatten assertions: bed `256x256`, height `256` |
| `slice_agent/output.py` | Output check: `printer_model == "Bambu Lab P2S"`, bed `(256, 256)` |
| `slice_agent/runner.py` | Plate types list (5 Bambu plates), layer heights per nozzle (only `0.4`: 0.12-0.28), override ranges |
| `bambu_mcp/config.py`, `cost.py` | Power per material (`PLA` 200 W from the P2S FAQ), heat-up kWh |
| `bambu_mcp/service.py`, `scripts/extract_hms.py` | HMS table `hms_en_22E.json` (`22E` is the P2S device prefix) |
| `bambu_mcp/printer/link.py` | AMS geometry: 4 units × 4 slots, `tray_ids` mapping and `ams_mapping = ams*4+slot`, tray types list, nozzle temp range 150-320 °C, the verified `project_file` payload fields (`bed_type`, levelling, `layer_inspect`) |
| `bambu_mcp/proposal.py`, `mcp_server.py` | Slot numbers 1-16 (`MAX_SLOT_NUMBER`), "ams and slot 0..3" checks |
| `bambu_mcp/printgate.py` | Nozzle diameter check against one nozzle, USB storage requirement (P2S without the closed plug-in needs a stick) |
| `bambu_mcp/printer/state.py`, `events.py` | `gcode_state` names and the idle/stoppable sets as the P2S firmware 01.03 reports them |
| Spec, README, phase 0 report | Bed 256³, firmware 01.03.00.00, the TLS chain and CN = serial (true for current Bambu printers) |

Target shape: a `printers/<model>.toml` (or a section of the service config) with the model name, bed size, device prefix for HMS, preset names per nozzle, plate types, layer-height and temperature ranges, power per material, AMS geometry and the payload flags. A `printer` key in the config picks it, and the slice-agent reads the same profile name. The P2S becomes the first profile with today's values, and the tests run against it. Several printers at once (more than one printer section, jobs bound to a printer) is a step after that.

**Later, asked by the owner (2026-11-27): evaluation mode.** On request ("оцени", or a button on the proposal) the service picks the best settings for the part and sets them on the job: infill density and pattern, wall loops, top and bottom shells, supports, brim, and layer height where the whitelist allows it. It slices a few candidate variants on the PC (within the existing override whitelist), compares time, grams and cost, and weighs them against the part's purpose: the manifest's checks, its `PRINT_HINTS`, and the load or strength intent when one is given. Then it proposes the winner with a short comparison table, and the owner keeps it, picks another variant, or edits it. The chosen settings are stored as the job's explicit settings (R6). The scoring follows a goal preset the owner names, "сделай прочно" for example. When none is named, it is taken from `PRINT_HINTS` or else defaults to Balanced. Each preset lists which settings vary, what the score weighs, and which hard limits a variant must keep. Strength is a proxy computed from the sliced file: perimeter and shell volume plus infill volume times a per-pattern factor. Time, grams and cost come from the slice itself.

| Preset (owner's word) | Varies | Score weighs | Hard limits |
|---|---|---|---|
| Balanced, «обычно» (default) | infill 10-25 %, walls 2-3, pattern grid/gyroid | equal weights on time, cost and strength | the manifest's checks pass |
| Speed, «быстро» | layer 0.24-0.28, infill 5-15 %, walls 2, lightning/grid, support only where needed | time | strength at least 0.6 of Balanced |
| Strength, «прочно» | walls 3-6, infill 25-60 %, gyroid/cubic/adaptive, top/bottom +2 | strength, then time | time at most 2.5 times Balanced |
| Quality, «красиво» | layer 0.12-0.16, outer wall first, slower outer wall, seam aligned or back, ironing on top if flat | surface: smallest layer, fewest visible seams | time at most 3 times Balanced |
| Cheap, «дёшево» | infill 0-12 %, walls 2, lightning, thinner shells, no brim unless the checks ask | cost (filament plus power) | strength at least 0.5 of Balanced, the checks pass |
| Light, «легко» | infill 0-10 %, lightning or honeycomb, walls 2, minimum shells | grams | strength at least 0.4 of Balanced |
| Fit, «точно» (holes, snaps, threads from CAD Workshop) | classic wall generator, walls 3+, slow outer wall, no fuzzy skin, layer 0.12-0.2 | dimensional risk (outer wall speed, layer, wall count), then time | none of the part's tolerance features are thinned by infill or shells |
| Watertight, «герметично» (jars, boxes, liquids) | walls 4-6, top/bottom 5+, 100 % infill below a size cap | shell thickness | no sparse layer reaches an outer surface |

Who picks the preset, in this order:
1. **The owner, in words or with a button.** The proposal gets a row of preset buttons, with the chosen one marked, like the slot buttons from Task 12.
2. **The agent that designed the part.** CAD Workshop's `PRINT_HINTS` gains `goal` (a closed list of the preset keys, a primary and an optional secondary) and `goal_reason` (at most 200 characters). The cad skill and the CAD Workshop rules tell the agent to set them from the task: what the part does, the load, heat, outdoor use, fits, liquids, and whether it is a test print. The agent says why in the reason. Examples: a lamp arm under load gives `strength`, a jar or wax box gives `watertight`, a part with snaps, threads or a press fit gives `fit`, a fit test or calibration piece gives `speed` plus `cheap`, a visible decor part gives `quality`.
3. **The service, as a fallback, from the manifest.** Tolerance features (holes, threads or snaps listed by CAD Workshop's checks) give `fit`. A closed cavity open at the top gives `watertight`. Otherwise it is `balanced`.

The proposal always names the preset and where it came from ("цель: прочно, от ассистента: несущий кронштейн лампы"), so a wrong guess is one tap to change. The MCP tools take `goal` on `propose_job`, `slice_job` and the evaluation call, so Claude Code and the multiclaude assistant can pass a goal they inferred from the conversation. Whatever the owner names in words is explicit (R6) and wins.

Presets can be combined as primary and secondary, "прочно и быстро" for example: the primary sets the hard limits, the secondary breaks ties. Settings outside the current override whitelist, such as ironing, seam position, the wall generator and outer wall speed, need the whitelist extended first, each with its own range. The table above lists the target set.

Still open for the design: the number of variants per run (each is a slice on the PC, so 3 to 6 is a sane cap), whether the PC stays awake through the batch under one lease, and calibrating the strength proxy against a few real prints (break test or flex test).

## 13. MCP tools

- Read: `printer_status`, `ams_status`, `camera_snapshot`, `job_status`, `print_history(part)`, `spools`, `plates`.
- Write without physical effect: `create_job(manifest, file)`, `slice_job(job_id, settings)`, `add_spool`, `update_spool`, `add_plate`.
- Printer writes: `load_spool(spool, slot)` (phase 2), `prepare_print` (sends the Telegram confirmation, starts nothing itself), `pause_print`, `request_stop` (sends a Telegram stop button) (phase 3). Each marked destructive in its tool annotations. There is no `start_print` tool.

## 14. Left to the builder

Library choices inside Python (MCP SDK, MQTT and FTPS clients), the SQLite schema, file layout, ports, retention numbers, HMS table source, process vs pc-agent endpoint for slice-agent, exact message wording (in Russian for Telegram), CI layout.

## 15. Calls I made (the owner may override)

- Repo `bambu-mcp`, public, MIT.
- Flattened profiles and HMS tables stay local, never committed.
- slice-agent runs as a standard user, separate from pc-agent's elevated task.
- A separate pinned Bambu Studio copy for the CLI.
- Spool loading writes the slot's material and colour to the printer.
- Electricity: average power per material (PLA 200 W) + heat-up allowance, tariff kept with history and changed by command, no web fetching, quarterly reminder.
- bambu-service memory budget about 100 MB, `MemoryMax=150M`.
- File intake: an upload CLI on the PC, an allowlisted path on workshop.
- A logging stand-in notifier until the service-notify add-in is ready.
- External prints matched by `subtask_name`, unmatched ones asked about once in Telegram.
- Sliced files wait on the PC in `%USERPROFILE%\bambu-mcp\sliced\`.
- Each spool records its Bambu Studio filament preset.

## 16. Access and the gate (answered by the owner, 2026-11-22)

- **[owner]** Owner only. The connector's MCP is registered for the owner's multiclaude assistant and the owner's Claude Code on the PC, never as a shared MCP for every assistant (unlike PAL). Notifications go to the owner only. Each client has its own token.
- **[owner]** Every print start is confirmed by a Telegram button, whichever entry point asked for it. Claude Code runs in auto mode, so a tool permission prompt is not a gate. `start_print` is therefore not an MCP tool at all: `prepare_print` sends the summary with the button through the notify add-in, the press reaches the service, and the service starts the print. No client token can start a print. Stop also needs a button press. Pause is allowed from a tool.
- Threat model, said plainly: the gate stops a model that misreads or invents the owner's consent. It does not stop an assistant set on forging one: on workshop the assistants and services run as the same Linux user with passwordless sudo, so an assistant could read the service's tokens or the access code. Closing that needs separate users and no sudo for assistants, out of scope here. The service-notify add-in's design (`docs/service-notify/DESIGN.md` in multiclaude) states the same.
