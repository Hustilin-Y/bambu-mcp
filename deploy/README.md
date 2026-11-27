# Deploying bambu-service to workshop

Target: the home Ubuntu server "workshop", user `shiba-srv`, about 3 GB RAM.

| What | Where |
| --- | --- |
| Code and venv | `/srv/bambu/app` |
| Data (SQLite, HMS table, CA bundle) | `/srv/bambu/data` |
| Secrets | `/srv/bambu/.env` (mode 600, never overwritten from the PC) |
| Settings | `/srv/bambu/config.toml` |
| Unit | `/etc/systemd/system/bambu-service.service` |

Run the commands from the repo root on the PC unless stated otherwise.

## 1. Code

```
ssh workshop 'sudo mkdir -p /srv/bambu/app /srv/bambu/data && sudo chown -R shiba-srv: /srv/bambu'
tar --exclude=.venv --exclude=__pycache__ --exclude=.git --exclude=.superpowers \
    --exclude=spike --exclude=data --exclude=.env --exclude=.pytest_cache \
    -czf - . | ssh workshop 'tar -xzf - -C /srv/bambu/app'
ssh workshop 'cd /srv/bambu/app && uv sync --frozen --no-dev'
```

Repeat the `tar` and `uv sync` lines for an update, then restart the unit.

## 2. Secrets: `/srv/bambu/.env`

Create it on the server, typing the values yourself (do not pass them on a command line):

```
ssh workshop
install -m 600 /dev/null /srv/bambu/.env
nano /srv/bambu/.env
```

```
BAMBU_ACCESS_CODE=<printer LAN access code>
BAMBU_SERIAL=<printer serial>
BAMBU_HOST=<printer IP>
CLIENT_TOKEN_PC=<random token for Claude Code on the PC>
CLIENT_TOKEN_ASSISTANT=<random token for the multiclaude assistant>
# NOTIFY_TOKEN=<from `assistant service key bambu`, step 5>
# GATEWAY_KEY=<orchestra-gateway key of bambu-service, step 7>
# SLICE_AGENT_TOKEN=<the PC's SLICE_AGENT_TOKEN, step 7>
```

`BAMBU_ACCESS_CODE`, `BAMBU_SERIAL` and `BAMBU_HOST` are required: without any of them the service refuses to start, and the error names the missing variable (never a value). Each `CLIENT_TOKEN_<NAME>` defines one MCP client (the name is only used in logs). Without any, the MCP server does not start. The same tokens authorise `POST /jobs` (`bambu-mcp upload`, step 7). Generate a token with `openssl rand -hex 32`. `GATEWAY_KEY` and `SLICE_AGENT_TOKEN` are optional: without them the service runs and only slicing is off (step 7).

## 3. Settings: `/srv/bambu/config.toml`

Start from `config.example.toml`. The deployed file needs at least:

```toml
data_dir = "/srv/bambu/data"
tls_mode = "ca"
ca_file = "/srv/bambu/data/bambu.cert"
notify_url = "http://127.0.0.1:8790"   # only once step 5 is done
```

`data_dir` must be absolute: the default is relative to the app dir. Every other key (`listen`, `camera_backend`, `heatup_kwh`, `timezone`, `[power_w]`, and the phase 2 keys `jobs_root_allowed`, `gateway_url`, `slice_agent_url`, `service_url` described in step 7) has a default, see `config.example.toml`. Unknown keys fail the start. A `[power_w]` table must keep a `PLA` entry: it is the fallback for materials without their own.

### CA bundle

The Bambu root certificates come from ha-bambulab (MIT), `custom_components/bambu_lab/pybambu/certs/bambu.cert`. Fetch it on the server, never commit it:

```
curl -fL -o /srv/bambu/data/bambu.cert \
  https://raw.githubusercontent.com/greghesp/ha-bambulab/main/custom_components/bambu_lab/pybambu/certs/bambu.cert
```

`tls_mode = "ca"` checks the chain against this file, then checks that the peer CN equals `BAMBU_SERIAL`.

### HMS texts

The service reads `/srv/bambu/data/hms_en_22E.json` (fixed name). Without it HMS messages show codes only. Build it from a local Bambu Studio install, then copy (PowerShell, output outside the repo so the next `tar` cannot ship it):

```
uv run python scripts/extract_hms.py "$env:APPDATA\BambuStudio" -o "$env:TEMP\hms_en_22E.json"
scp "$env:TEMP\hms_en_22E.json" workshop:/srv/bambu/data/hms_en_22E.json
```

Options: `-o/--out` (required), `--prefix` (default `22E`, the P2S). Never commit the output.

## 4. Unit

```
scp deploy/bambu-service.service workshop:/tmp/
ssh workshop 'sudo install -m 644 /tmp/bambu-service.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now bambu-service'
```

The unit orders itself after `network-online.target` because the MCP listener binds `192.168.88.11` and does not retry a failed bind. `MemoryMax=150M`, `Restart=on-failure`, `RestartSec=10`.

## 5. Notifications (optional)

Needs the multiclaude `service-notify` add-in deployed and enabled on the server. Then:

```
assistant service key bambu
```

Put the printed key into `.env` as `NOTIFY_TOKEN`, set `notify_url` in `config.toml` (the add-in listens on `127.0.0.1:8790` by default) and restart the unit. Without both, the service runs with the logging notifier and the log says so.

Messages wait in an outbox table in `bambu.db` and are sent in order by one task: a router restart, a Telegram or ISP outage or a service restart only delays them. Retries back off from 30 s to 30 min, and every message carries a dedupe key the add-in honours for 7 days, so a retry never shows twice. A message still not delivered after 24 h is dropped with a warning in the log, as is one the add-in refuses for good (400, 403, 413, 500 `not_kept`, 502 `telegram_refused`). A 401 is retried, so pending messages go out once the token is fixed. Repeated identical warnings (presses poll failures, printer reconnects, outbox retries) are logged once, then at most once an hour with a count.

## 6. Register the MCP

Clients connect by IP: the Host allowlist accepts only the `listen` addresses, so a hostname is rejected. The owner types the token.

Claude Code on the PC:

```
claude mcp add --transport http bambu http://192.168.88.11:8765/mcp --header "Authorization: Bearer <CLIENT_TOKEN_PC>"
```

The multiclaude assistant (owner only), through loopback, on the server:

```
claude mcp add --transport http bambu http://127.0.0.1:8765/mcp --header "Authorization: Bearer <CLIENT_TOKEN_ASSISTANT>"
```

Register it for the owner's assistant only, no other assistant or project.

Token hygiene, the owner types these commands:

- The token ends up in the shell history and in `ps` argv while the command runs. In bash start the line with a space (needs `HISTCONTROL=ignorespace`) or run `set +o history` first. In PowerShell run `Clear-History` afterwards and remove the line from the PSReadLine history file (`(Get-PSReadLineOption).HistorySavePath`).
- `claude mcp add` stores the token in plaintext in `~/.claude.json`. Keep that file private.

## 7. Phase 2: jobs, slicing and spools

Phase 2 takes print jobs from CAD Workshop, slices them on the PC through slice-agent (deploy/slice-agent/README.md) and books the predicted grams. Every part is optional: without the slicing keys the service runs and jobs can still be created, but `slice_job` fails with "slice-agent is not configured".

### Jobs in

Two ways, both owner only (the `CLIENT_TOKEN_<NAME>` tokens of step 2). The service copies the manifest and its 3MF into `data_dir/jobs/<job id>/`.

- MCP `create_job(manifest_path)` reads a manifest on workshop itself. Off by default. Turn it on with `jobs_root_allowed`, an absolute path (a relative one fails the start). On workshop set it to the cad-workshop folder that holds the print-ready output, so multiclaude's `create_job` works. The manifest and its 3MF must both resolve inside it, links included, and `shiba-srv` must be able to read them.

  ```toml
  jobs_root_allowed = "/<cad-workshop checkout on workshop>"
  ```

- `bambu-mcp upload` sends a manifest and the 3MF next to it from the PC (`POST /jobs`, 3MF up to 20 MB) and prints the new job id. The token comes only from the environment variable named by `--token-env` (default `BAMBU_MCP_TOKEN`), never from argv. Use the value of `CLIENT_TOKEN_PC`. In PowerShell, typed at a hidden prompt:

  ```
  [Environment]::SetEnvironmentVariable("BAMBU_MCP_TOKEN", [Net.NetworkCredential]::new("", (Read-Host -AsSecureString "token")).Password, "User")
  ```

  Then, in a new shell, from the repo root:

  ```
  uv run bambu-mcp upload <path>\<name>.print.json --url http://192.168.88.11:8765 --token-env BAMBU_MCP_TOKEN
  ```

  `--url` can come from `service_url` instead: put `service_url = "http://192.168.88.11:8765"` in a small TOML file on the PC and point the `BAMBU_CONFIG` environment variable at it. The file is read with the service's own loader, so only service keys are allowed in it.

### Slicing: workshop side

`config.toml`:

```toml
gateway_url = "http://192.168.88.11:<gateway port>"   # [server] host and port of gateway.toml
slice_agent_url = "http://<PC address>:11436"
```

`.env`: `SLICE_AGENT_TOKEN` (the same value as the PC's user variable, deploy/slice-agent/README.md, "Tokens") and `GATEWAY_KEY`. Without `slice_agent_url` and `SLICE_AGENT_TOKEN` nothing is sliced. Without `gateway_url` and `GATEWAY_KEY` the PC is not woken: slicing works only while it is awake. The log says which at start.

The gateway key. orchestra-gateway keeps people's and programs' keys as SHA-256 hashes in `/srv/orchestra/keys.toml` (not in `gateway.toml`), made with its `new-key` command (check the gateway's README if this changed):

```
G="/srv/orchestra/app/gateway/.venv/bin/orchestra-gateway --config /srv/orchestra/gateway.toml"
$G new-key bambu-service --aliases none --out /srv/orchestra/keys/bambu-service.key
sudo systemctl restart orchestra-gateway
printf 'GATEWAY_KEY=%s\n' "$(cat /srv/orchestra/keys/bambu-service.key)" >> /srv/bambu/.env
rm /srv/orchestra/keys/bambu-service.key
sudo systemctl restart bambu-service
```

`--aliases none` names no real alias: the key can call `/wake` and `/status` but routes no model requests. `printf` is a shell builtin, so the key never shows in `ps`.

How a slice runs: the service sends `POST /wake` to the gateway with `GATEWAY_KEY`, polls slice-agent's `/health` for up to 120 s, then posts the job. One slice at a time: on 409 `busy` it waits 30 s and tries once more. The agent's CLI timeout is 300 s.

- The PC needs only the `standard` process set: the service always asks for it, and a named set from the conversation travels as overrides of the whitelisted settings.
- The filament preset sent is the `filament_preset` of the spool in the chosen AMS slot. It must be one of the agent's `filaments`, or the slice is refused.
- The sliced summary goes to Telegram with the first plate's picture (fetched from the agent). A summary longer than a photo caption (1024 characters) is sent as a separate message right after the picture. The picture waits in `data_dir/outbox-files/` until its message is sent or dropped.
- The owner prints the sliced file from Bambu Studio on the PC, under the agent's name for it, or from Telegram (step 8).

### Booking a sliced print

A print whose file name matches a sliced job (`<name>-<job id>-p1` or `-kit`, as Studio reports it) books that job's predicted grams to the spool chosen at slicing, **without asking** the spool question. A failed or cancelled one keeps the printed share of the grams when the printer reported a percentage, and otherwise refunds the booking and asks the usual phase 1 question. A print under any other name is handled as in phase 1.

### Spools and `load_spool`

Spool records have three new fields for `load_spool`: `tray_info_idx`, `nozzle_temp_min` and `nozzle_temp_max`. Copy them from the `slice-agent flatten` output on the PC (one line per filament preset) with `add_spool` or `update_spool`. `update_spool` can set or change them but **cannot clear** them.

`load_spool(spool_id, ams, slot)` sets the AMS slot's material, colour and nozzle range on the printer and maps the spool to the slot. In phase 2 it is the only printer write: the link sends exactly one kind of write, `ams_filament_setting`, besides the phase 1 `get_version` and `pushall` requests. Phase 3 adds `project_file`, `pause` and `stop` (step 8). It refuses, sending nothing, unless all of these hold:

- the printer is connected and has sent a fresh report since it (re)connected,
- the printer is idle (`IDLE`, `FINISH` or `FAILED`),
- `ams` and `slot` are 0..3 and that AMS unit is in the last report,
- the spool is active and has `material` (an AMS type), `color_hex` (`#RRGGBB`), `tray_info_idx`, `nozzle_temp_min` and `nozzle_temp_max`.

It sends once and does not retry: check the slot on the printer screen. Nothing checks that the spool's material belongs to the `tray_info_idx` filament family, so a wrong id sets the wrong filament profile on the printer.

Checked live on 2026-11-24 (plan Task 10, Step 6): `ams_id` is the AMS unit and `tray_id` the slot within it (slot 2 of AMS 0 is `tray_id` 1), the printer takes the type, colour and nozzle range, and the next report shows them while the other slots stay as they were. `PLA-S` is not checked yet.

## 8. Phase 3: printing, pause and stop

Phase 3 starts prints on the printer, pauses them and stops them. There is no `start_print` tool, and no client token starts or stops a print: only the owner's press on a button the service sent to Telegram does. MCP clients can only ask for that button.

### Requirements

- **A USB stick in the printer.** The sliced file is uploaded to the root of the stick over implicit FTPS (port 990). The pre-flight refuses while the printer reports no USB storage (`sdcard` false), or has not reported it since it last reconnected.
- `tls_mode = "ca"` and `ca_file` (step 3). The FTPS upload checks the printer's certificate against the CA bundle and the peer CN against `BAMBU_SERIAL`, as MQTT does. With another TLS mode `prepare_print` refuses.
- Slicing set up (step 7): the sliced file is fetched from the PC, which is woken first.
- The printer in LAN-only mode with Developer Mode on, as for the earlier phases.

### Updating from phase 2

1. Back up the database before the first restart on phase 3 code: that start migrates `bambu.db` to schema v8 (one new table, nothing else changes). For example `cp /srv/bambu/data/bambu.db /srv/bambu/data/bambu.db.v7` with the service stopped.
2. Update the slice-agent on the PC to the same version and restart it (deploy/slice-agent/README.md, or run `install.ps1` again). Its `/slice` answer now carries the sliced file's `sha256`. With the old agent every new slice lacks it and every print is refused with "нарежь заново".
3. Jobs sliced before the update have no recorded `sha256`: slice it again before printing such a job.

### Starting a print

1. `prepare_print(job_id, plate)` or the "Печатать…" button under the sliced summary (a kit asks for the plate first) wakes the PC, fetches the sliced file into the print cache, asks the printer for a full report and runs the pre-flight. Hard stops: printer not connected or no fresh report, printer not `IDLE`/`FINISH`/`FAILED`, a fatal or serious HMS, no USB stick, another nozzle diameter, the slot empty or unmapped or holding another material, a plate that uses more than one filament, a retired spool, a spool remainder below the plate's grams x 1.15, the sliced file missing or changed since slicing. Warnings (shown, not blocking): another colour in the slot than at slicing, the part's material intent, the plate's hints. Every blocking reason is listed, and nothing is stored or sent.
2. Otherwise the service sends a confirmation that names the plate type, with "Стол чистый, печатать" and "Отмена". The owner confirms the bed and the plate in the press itself. The button works for 10 minutes and once. It is bound to the file's sha256, the plate, the AMS slot, the printer serial, the nozzle and the plate type.
3. The press runs the pre-flight again, uploads the file (5 minute deadline), checks once more and sends `project_file` once. Then it watches for **30 s** for a report with the new task id in `PREPARE` or `RUNNING`. Seen: "Печать началась" with a "Остановить…" button. Not seen: the owner is told what the printer last reported, and the start counts as `unconfirmed`.

`project_file` is never sent twice for one confirmation: not on a double press, not on a press replayed after a service restart, not after an `unconfirmed` start or a failed upload. Prepare the print again instead.

### The print cache

Sliced files are streamed from the PC to `data_dir/print-cache/` (`/srv/bambu/data/print-cache/`) and from there to the printer, never held whole in memory. The cache keeps the last 3 `*.gcode.3mf` files. Names starting with a dot (`.part` from a fetch, `.upload` from a press) are temporary. A crash can leave one behind: delete it while the service is stopped.

### The USB stick

The service never deletes anything from the stick. Every printed file stays at its root under its sliced name (a reprint of the same job overwrites it). Clean the stick from the printer screen or on a PC now and then.

### Pause and stop

- `pause_print()` sends `pause` once, only while the printer is `RUNNING`. There is no resume tool and no resume button: resume on the printer screen or in Bambu Studio.
- `request_stop()` and the "Остановить…" button stop nothing themselves. They send "Остановить печать …?" with "Да, остановить" and "Отмена" (10 minutes, once). The press sends `stop` once, and only while the same print (task id and name) is still `PREPARE` (heating, levelling), `RUNNING` or `PAUSE`. Otherwise it says why and sends nothing.
- A restart in the middle of a start (during the upload or the 30 s watch) is reported at the next start of the service: the owner is asked to look at the printer, and the command is never sent again.

### Never exposed

- Printer commands not reachable from any tool or button: resume, raw G-code, axis moves, heating, AMS load or unload, HMS clearing, file deletion, LAN and Developer Mode settings. The link sends only `get_version`, `pushall`, `ams_filament_setting`, `project_file`, `pause` and `stop`, each checked field by field before it leaves.
- Tool results and Telegram texts carry file and job names only: no server paths, printer address, serial, access code or tokens. Confirmation ids stay out of the log, and the outbox dedupe keys carry a hash of them. `prepare_print` and `request_stop` return the id to the client, but it confirms nothing: presses come only from the service-notify add-in.

### Not yet checked on the printer

The live check with the owner present confirms:

- that the P2S reports the task id the service sent (otherwise every start ends as `unconfirmed`),
- that it answers `project_file` with a `SUCCESS` ack carrying `sequence_id "0"`, and accepts the plate md5 in lower case,
- the `pause` and `stop` payload shapes, and `stop` during `PREPARE`,
- TLS session reuse on the FTPS data socket, and the FTPS upload path form.

## Checks after deploy

```
systemctl status bambu-service
journalctl -u bambu-service -n 50 --no-pager
cat /sys/fs/cgroup/system.slice/bambu-service.service/memory.current
```

- Status is `active (running)`, the journal shows the MCP server listening on both addresses and no tracebacks.
- `memory.current` is under 120 MB (125829120 bytes). The hard limit is 150M.
- MCP call with the token. Keep it out of argv and history: the owner creates a mode-600 header file once, typing the token (`install -m 600 /dev/null /srv/bambu/.curl-auth`, then `nano` it to one line `Authorization: Bearer <CLIENT_TOKEN_ASSISTANT>`), and deletes it afterwards:

```
curl -s http://127.0.0.1:8765/mcp \
  -H @/srv/bambu/.curl-auth \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

It must list the bambu tools. Without the header the answer is 401.
