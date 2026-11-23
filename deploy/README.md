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
```

`BAMBU_ACCESS_CODE` and `BAMBU_SERIAL` are required. `BAMBU_HOST` is also needed for the printer link: it defaults to empty, so without it the service starts but cannot connect. Each `CLIENT_TOKEN_<NAME>` defines one MCP client (the name is only used in logs). Without any, the MCP server does not start. Generate a token with `openssl rand -hex 32`.

## 3. Settings: `/srv/bambu/config.toml`

Start from `config.example.toml`. The deployed file needs at least:

```toml
data_dir = "/srv/bambu/data"
tls_mode = "ca"
ca_file = "/srv/bambu/data/bambu.cert"
notify_url = "http://127.0.0.1:8790"   # only once step 5 is done
```

`data_dir` must be absolute: the default is relative to the app dir. Every other key (`listen`, `camera_backend`, `heatup_kwh`, `timezone`, `[power_w]`) has a default, see `config.example.toml`. Unknown keys fail the start.

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
