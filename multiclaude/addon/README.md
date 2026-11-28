# bambu

One person's assistant runs their Bambu Lab printer through [bambu-mcp](https://github.com/Hustilin-Y/bambu-mcp): status, camera, spools, print jobs from CAD Workshop, slicing, cost estimates. A print starts, resumes or stops only when that person presses the service's button, in Telegram or in the chat Mini App's Print tab.

With the chat add-in on, the owner gets a **Печать** tab in the Mini App: the printer, slots, jobs, history, what the system can do, and Pause, Resume, Stop and Print. `install.sh` puts its router module into multiclaude (`lib/multiclaude/addons/bambu/`, and one line adding `bambu` at the end of the add-in list), `setup` writes `~/.config/bambu/miniapp.json` (mode 600) with the tab's own token, which reaches only the service's `/v1/` API. Run `assistant relay deploy` after installing so the relay serves the tab's page.

## What it needs

- The **service-notify** add-in, for the print and stop buttons. `setup` stops without it.
- uv, jq, curl and a systemd user manager. The service runs as a **user unit** under the bot's user (about 100 MB). With lingering it keeps running without a login session (one root command, printed by `setup`).
- The printer in LAN-only mode with Developer Mode on, its IP, serial and access code. Profiles: `p2s` (checked) and `a1` (best guesses, see below).
- A slice-agent with Bambu Studio somewhere: see `deploy/slicing-options.md`.

## Setup

```sh
git clone https://github.com/Hustilin-Y/bambu-mcp ~/bambu-mcp
~/bambu-mcp/multiclaude/install.sh          # --user <name>; the owner by default
assistant addon enable bambu                 # asks for IP, serial, access code (hidden), model
```

`setup` never runs sudo. It writes `~/.config/bambu/.env` (mode 600) and `config.toml`, downloads the Bambu CA bundle into `~/.local/share/bambu`, issues the service-notify key, installs and starts the `bambu-service` user unit, and adds the `bambu` MCP server to the owner's workspace in `~/.claude.json`. Run it again any time: it keeps what is there. `assistant addon disable bambu` stops the service, revokes the key and removes the MCP entry. Settings and data stay.

Root steps it may print: `sudo loginctl enable-linger <bot user>`.

## Trust

On multiclaude every assistant runs as the bot's Linux user. Any assistant on this machine can read the printer's access code and the service tokens in `~/.config/bambu/.env`, and could start a print over MQTT itself. The Telegram button guards against a model misreading consent, not against an assistant set on forging it. One owner on the machine and `"frontdesk": "router"` in `config.json` keep the risk small.

## Another printer model

The `a1` profile is filled from public sources, and every field but the model name and bed size is marked unverified. `scripts/printer_check.py` reads the printer (never prints) and writes a report. Send it with your fixes to `src/bambu_profiles/a1.toml` as a pull request.
