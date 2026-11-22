# Phase 0 report (spike, 2026-11-22/23)

Hardware: Bambu Lab P2S, firmware `ota 01.03.00.00`, AMS 2 Pro `05.00.22.22`, Filament Buffer `01.01.16.19`. Nozzle 0.4 `HS01`. Printer at `192.168.88.14` (static DHCP lease in MikroTik since 2026-11-22). LAN Only on, Developer Mode on, LAN Only Liveview on. Serial is the CN of the printer's TLS certificate.

Spike scripts: `spike/` (throwaway). Secrets in `spike/.env` (git-ignored).

## Results

| Task | Result |
|---|---|
| 1. LAN-only + Developer Mode | Done. The access code did not change when LAN Only was enabled. |
| 2. MQTT read | Works, and also worked **before** LAN Only (cloud mode): read-only monitoring does not need Developer Mode. The printer pushes a full report (~99 `print` keys) about once a second without `pushall`. |
| 3. TLS | Full verification works: chain `printer (CN=serial)` <- `BBL Device CA N7-V2` <- `BBL CA2 RSA` (root in ha-bambulab `bambu.cert`). The printer sends the intermediate, the root alone is enough. MQTT 8883 TLS 1.3, FTPS 990 TLS 1.2. paho passes the IP as hostname, so phase 1 uses `check_hostname=False` plus an explicit CN == serial check after the handshake (chain still verified). |
| 4. Storage | **Without a USB drive FTPS cannot write** (`553 Could not create file`, empty root), although `fun2` (`0x3973`) advertises internal-storage printing: that path is Bambu's closed network plug-in. **With a USB stick** (owner inserted one): `sdcard: true`, upload to the FTP root works (`/cache` does not exist on the stick, `/timelapse` is created by the printer), size check matches. `project_file` with `url: "ftp:///<name>"`, `param: "Metadata/plate_1.gcode"`, md5 from `plate_1.gcode.md5`, `ams_mapping: [slot]`, `ams_mapping2: [{"ams_id": 0, "slot_id": slot}]`, unique `task_id`: ack `result: SUCCESS`, then `gcode_state RUNNING` with our `subtask_name` and `task_id` within 2 s. |
| 5. Camera | PyAV in-process: one 1920x1080 frame in 2.8 s, process RSS +20 MB. The URL never leaves the process. Phase 1 uses PyAV (wheel ~27 MB). |
| 6. CLI slicing | Works with the installed Bambu Studio `02.08.02.61` (see recipe below). A CAD Workshop part (example-bracket) sliced in about 1 s: 5.31 g PETG, prediction 2315 s, Textured PEI, bed 256 x 256. Not yet done: a second pinned copy, running from a non-elevated scheduled task, printing a CLI-sliced file. |

## Fixtures

`tests/fixtures/p2s/` (redacted by `spike/make_fixtures.py`: serial, access code, LAN IP, module and AMS serials, integer IPs in `net.info`): `version`, `pushall_idle` (FINISH of an earlier print), `running_layer0` (heating and levelling show as `RUNNING` with `layer_num 0`, there was no `PREPARE` state), `running_layer1`, `running_layer2`, `running_mid`, `finish`, `gcode_line`.

Observed on the P2S: every status message is a full report (about 99 `print` keys, about once a second), no partial `push_status` was seen. The printer also sends `{"print": {"command": "gcode_line", ...}}` messages, which must be ignored. Not captured: pause, HMS errors, a failed or cancelled print: phase 1 tests for those use synthetic fixtures built from these.

The 20 mm test cube (PETG on Textured PEI, 28 min predicted) printed to the end.

## CLI recipe (verified)

Presets flattened by `spike/flatten.py`:
1. Resolve `inherits` recursively, then `include` templates in order, then the preset's own keys (without `include`, the P2S start G-code is the generic one).
2. Set `from: "User"`, `inherits: <the system preset's own name>`, and `printer_settings_id` / `print_settings_id` / `filament_settings_id` (empty ids give `-17 printer not compatible with the process preset`).
3. Remove from process and filament every key the machine preset defines.
4. Assert `printable_area` 256 x 256, `printable_height` 256, `nozzle_diameter` `["0.4"]`.

Command (run with `Start-Process -Wait`, the exe is a GUI-subsystem binary and returns at once otherwise):

```
bambu-studio.exe --slice 0 --arrange 1 --orient 0 --curr-bed-type "Textured PEI Plate"
  --load-settings "machine.json;process.json" --load-filaments "filament.json"
  --export-3mf <bare file name> --outputdir <dir> <input.3mf>
```

Return codes met: `-17` incompatible presets, `-50` object outside the plate, `-61` filament not allowed on the plate type (default bed type), `-13` export failed (a full path in `--export-3mf`; pass a bare name with `--outputdir`). `result.json` in the output dir carries `return_code`, `error_string`, per-plate time and grams.

## Findings that change the design

- **USB stick required for print start (phase 3).** Decided by the owner: a stick stays in the printer. Upload goes to the FTP root, not `/cache`.
- **CAD Workshop parts sit at the origin**, not on the bed: `--arrange 1 --orient 0` places them in XY without turning them, which keeps `PRINT_DOWN`. Kits keep `--arrange 0` (already laid out in plate coordinates), to be verified in phase 2.
- **Bed type must be passed** (`--curr-bed-type`), the default fails PETG with `-61`.
- **CLI output has an empty `printer_model_id`** (the GUI writes `N7`). Phase 2 must check whether the printer accepts such a file, and set the id if not.
- Monitoring works in cloud mode too, so LAN Only is needed only for starting prints from the connector.
- The owner's AMS holds PETG (white, green, black), all `tray_info_idx GFG99` (Generic PETG), `remain -1`. CAD Workshop's default material is PLA: phase 2 slot preselection must not assume PLA.

## Go / no-go

- Phase 1 (monitor): **go**. Fixtures from this spike in `tests/fixtures/p2s/` (after redaction).
- Phase 2 (slicing): **go**, with the open items above (pinned copy, non-elevated run, model id, kits).
- Phase 3 (print start): **go** with the USB stick.
