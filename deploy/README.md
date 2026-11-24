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
- The owner prints the sliced file from Bambu Studio on the PC, under the agent's name for it.

### Booking a sliced print

A print whose file name matches a sliced job (`<name>-<job id>-p1` or `-kit`, as Studio reports it) books that job's predicted grams to the spool chosen at slicing, **without asking** the spool question. A failed or cancelled one keeps the printed share of the grams when the printer reported a percentage, and otherwise refunds the booking and asks the usual phase 1 question. A print under any other name is handled as in phase 1.

### Spools and `load_spool`

Spool records have three new fields for `load_spool`: `tray_info_idx`, `nozzle_temp_min` and `nozzle_temp_max`. Copy them from the `slice-agent flatten` output on the PC (one line per filament preset) with `add_spool` or `update_spool`. `update_spool` can set or change them but **cannot clear** them.

`load_spool(spool_id, ams, slot)` sets the AMS slot's material, colour and nozzle range on the printer and maps the spool to the slot. It is the only printer write: the link now sends exactly one kind of write, `ams_filament_setting`, besides the phase 1 `get_version` and `pushall` requests. It refuses, sending nothing, unless all of these hold:

- the printer is connected and has sent a fresh report since it (re)connected,
- the printer is idle (`IDLE`, `FINISH` or `FAILED`),
- `ams` and `slot` are 0..3 and that AMS unit is in the last report,
- the spool is active and has `material` (an AMS type), `color_hex` (`#RRGGBB`), `tray_info_idx`, `nozzle_temp_min` and `nozzle_temp_max`.

It sends once and does not retry: check the slot on the printer screen. Nothing checks that the spool's material belongs to the `tray_info_idx` filament family, so a wrong id sets the wrong filament profile on the printer.

Still open (plan Task 10, Step 6): the owner has not yet confirmed on the idle printer the `ams_id`/`tray_id` numbering, that `PLA-S` is accepted, and that the next `pushall` shows the new type and colour. Treat `load_spool` as unverified until then.

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
