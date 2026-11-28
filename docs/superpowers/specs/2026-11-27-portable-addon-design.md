# bambu-mcp for another owner: multiclaude add-in, printer profiles, external spool: design

Date 2026-11-27. Owner: the bambu-mcp owner. Builds on phases 1-3 and the evaluation work, all merged to `main` and live on the owner's workshop.

## 1. Intent

The owner wants a friend to run bambu-mcp with as little effort as possible. The friend wrote multiclaude, runs it on a powerful server, and has a Bambu Lab A1 **without an AMS**.

"Comfortable" means the friend installs bambu-mcp the way multiclaude installs anything: `assistant addon enable bambu`, answers a few questions, and is done. Whatever we cannot verify without an A1, the friend fixes and sends back as a PR to the owner's repo.

## 2. Goals, non-goals, anti-goals

Goals:
- bambu-mcp ships a multiclaude add-in. Enabling it does all of the following:
  - installs the service;
  - issues its service-notify key;
  - registers the `bambu` MCP for the owner's assistant;
  - gives the assistant its rules;
  - asks only for the printer's IP, serial, access code and model.
- Printer models become profiles. P2S is filled from what we verified. A1 is filled from what we know and marked unverified.
- A printer without an AMS works: one external spool. That covers the spool inventory, the proposal, slicing, `load_spool` and print start.
- The service-notify add-in goes into upstream multiclaude as a PR. It needs a security and leak review first and the owner's approval before the PR is opened.
- The friend's slicing runs on his server. The docs describe the options, and the friend picks one.

Non-goals:
- No changes to the owner's own setup: workshop, PC slicing, wake through the gateway. The owner keeps P2S and the PC slice-agent.
- We do not pick the friend's slicing setup. We do not build Docker images or Linux installers for the slicer beyond what the docs need.
- Multi-printer support for one owner. Two printers in one service is a later step.
- Verifying A1 protocol details ourselves.

Anti-goals:
- Any secret of the owner's in the add-in, the repo or the upstream PR: tokens, the access code, the serial, IPs other than documentation examples, personal paths, the owner's Telegram id.
- An add-in that silently does something with root. Steps that need root are printed for the friend to run, as the cad-workshop add-in does now that bot users have no sudo.
- A profile value we guessed but present as verified.

## 3. Constraints

- multiclaude add-ins: a folder with `setup`, `rules.md`, an optional `teardown` and `next-steps`, installed by `install.sh` from the project's checkout. The cad-workshop add-in (`cad-workshop/multiclaude/addon/`) is the working example in this family. Since the owner's sudo split, a bot user has no sudo, so `setup` must not need root.
- The service needs a systemd unit, which is root. The add-in installs it as a **systemd user unit** under the bot's user, with lingering, the same way cad-workshop builds use user scopes. Otherwise it prints the one root command.
- service-notify's API (`/v1/messages`, presses, keys) is unchanged. The upstream PR is the add-in as it runs on workshop today (`multiclaude` fork, `850df24` and later).
- The A1 has LAN-only and Developer Mode on recent firmware. Its camera is a JPEG stream on TCP 6000 (ha-bambulab handles it), not RTSPS on 322. It stores files on a microSD card. The friend verifies all of this.
- Without an AMS, the printer's external spool is `ams_id 255`, `tray_id 254` (the `vt_tray` in reports). `project_file` uses `use_ams: false`. Per ha-bambulab and Bambu Studio, `ams_mapping` is `[-1]` or omitted. Unverified on any printer we have.

## 4. Approaches

The owner decided these in conversation:
- **How the friend installs:** as a multiclaude add-in shipped in the bambu-mcp repo. Not a separate installer, not Docker-first.
- **Slicing on the friend's server:** documented options, the friend picks. We wrote out these choices:
  - (a) the Bambu Studio AppImage plus `xvfb` under systemd;
  - (b) a Docker container with Studio CLI and `xvfb`;
  - (c) the existing slice-agent on a Windows or Linux PC, as the owner runs it.

  All of them talk to bambu-service through the existing slice-agent HTTP API, so the service does not change.
- **The A1 profile:** we fill in what we know. The friend refines it and sends a PR.
- **service-notify upstream:** first a security and leak review, then the owner's approval, then the PR.

Also considered and rejected:
- Moving the friend to the owner's two-machine layout. He has one strong server.
- Making the service itself a Telegram bot. multiclaude already is one.

## 5. Design

### 5.1 Printer profiles

`printers/<model>.toml` in the repo, picked by `printer = "p2s"` in `config.toml` (default `p2s`). One loader, one dataclass `PrinterProfile`. Each value notes whether it is verified, so the docs and `printer_status` can say what is unverified.

Fields:
- `model_name` (the printer_model string in sliced files)
- `bed_mm` (x, y, z)
- `hms_prefix`
- `camera` (`rtsps_322` or `jpeg_6000`)
- `storage` (`usb` or `sd`)
- `ams`: `none`, or units and slots, plus the external spool
- `machine_presets` per nozzle
- `process_sets`
- `filaments`
- `plate_types`
- `layer_heights` per nozzle
- `nozzle_temp_range`
- `power_w` per material
- `heatup_kwh`
- the `project_file` flags (`bed_type`, levelling, `layer_inspect`, `flow_cali`, `vibration_cali`)
- `gcode_states`: the idle and stoppable sets

Every P2S-specific constant listed in the main spec's "printer model as configuration" table moves into `printers/p2s.toml`, and the code reads the profile. `printers/a1.toml` is filled with:
- bed 256³;
- the A1 HMS prefix, if known, else empty, so the HMS table is optional;
- `jpeg_6000`;
- `sd`;
- `ams = "none"`;
- the `@BBL A1` preset names;
- power from Bambu's A1 FAQ if known, else the P2S values marked unverified.

The slice-agent's output check uses the profile's `model_name` and bed. The agent and the service read the same profile name: the agent from its own config, the service from `config.toml`.

### 5.2 External spool (no AMS)

- A "slot" can be the external spool. Its slot number is 0, shown as «внешняя катушка», internally `ams=255, slot=254`. `slot_number` and `slot_position` handle it, and the global 1-16 numbering stays for AMS slots.
- Reports: parse `vt_tray` into the same `Tray` shape. `ams_status` lists it, and `map_slot` and `load_spool` accept it.
- Proposal: with `ams = "none"`, the slot row shows the external spool only. `pick_slot` treats it as the only loaded slot.
- `load_spool` to the external spool sends `ams_filament_setting` with `ams_id 255, tray_id 254`. The allowlist accepts exactly that pair in addition to the AMS pairs.
- Print start: with no AMS, `use_ams false` and the profile's mapping form. The allowlist rebuilds the payload per profile.
- This also works for a P2S whose external holder is used, so the owner gets it too.

### 5.3 Camera for the A1

`camera.py` gains a `jpeg_6000` backend, following ha-bambulab's JPEG stream: TLS to port 6000, the auth packet with `bblp` and the access code, then length-prefixed JPEG frames. Take one frame for a snapshot. It shares the CA and CN check with MQTT. It is unverified: the friend tests it.

### 5.4 The multiclaude add-in

Folder `multiclaude/addon/` in bambu-mcp, installed by `multiclaude/install.sh` into multiclaude's `addons/bambu`, the same pattern as cad-workshop.

- `setup`, run by `assistant addon enable bambu` as the bot user, with no sudo:
  1. Check that `uv` exists, that service-notify is enabled, and that the checkout path exists. Otherwise say what is missing.
  2. Create `~/.local/share/bambu` (data) and the app venv (`uv sync --frozen --no-dev`).
  3. Ask for the IP, serial, access code and model, unless `.env` already has them. Use multiclaude's own prompt helper. If there is none, read a hidden terminal prompt. Write `.env` with mode 600. Never echo a secret, never put one in argv.
  4. Issue the notify key with `assistant service key bambu` and write it straight into `.env`.
  5. Generate the client token for the assistant into `.env`.
  6. Write `config.toml`:
     - the printer profile;
     - `listen` on loopback only by default;
     - `notify_url` from service-notify's port;
     - `jobs_root_allowed` = the cad-workshop checkout, if that add-in is on.
  7. Install and start a systemd **user** unit `bambu-service.service` (`systemctl --user`, lingering checked). If lingering is off, print the one root command `loginctl enable-linger <user>`.
  8. Fetch the Bambu CA bundle (ha-bambulab, MIT), the same as the deploy README.
  9. Register the MCP for the owner's assistant, at local scope in the owner's workspace, as `/mcp add` does, with the token from `.env`. The token is never in argv: use `claude mcp add-json` fed from a file read at mode 600, or multiclaude's helper if it has one.
- `rules.md`: the assistant's rules.
  - Jobs from CAD Workshop manifests.
  - Proposals, goals and «Подобрать».
  - Spools and `load_spool`.
  - Printing only through `prepare_print` and the owner's button.
  - Never `start_print`.
  - Costs and amortization commands.
- `teardown`: stop and disable the user unit, revoke the notify key and remove the MCP entry. Keep the data, and say where it is.
- `next-steps`: how to set up slicing (5.5), how to add spools, how to set the tariff and amortization, and what to verify on an A1 (5.7).

### 5.5 Slicing on the friend's server: options (docs only)

A doc `deploy/slicing-options.md` covers three options:
- (a) Studio AppImage plus `xvfb-run` with the slice-agent as a systemd user unit;
- (b) a Docker container (an example Dockerfile and compose, not built or published by us);
- (c) the existing PC slice-agent.

For each it gives:
- the steps;
- what to install;
- how `slice_agent_url` and `SLICE_AGENT_TOKEN` are set;
- known risks (Studio's Linux CLI dependencies, the pinned version and the golden test).

The slice-agent becomes OS-neutral where it is Windows-only today:
- paths from XDG on Linux, `%LOCALAPPDATA%` on Windows;
- process-group kill instead of taskkill on POSIX. That part already exists, so check it.

The service needs no change: `gateway_url` stays optional.

### 5.6 service-notify upstream

1. **Security and leak review of the add-in code**, as a fresh reviewer task:
   - token handling;
   - the press path;
   - only the owner's presses count;
   - the HTTP API bound to loopback;
   - size caps;
   - dedupe;
   - nothing of the owner's (ids, names, paths, tokens) in code, tests or docs;
   - git history checked for anything sensitive in the commits that would go upstream.
2. A findings list, and fixes in the fork.
3. **The owner approves.**
4. Only then the upstream PR to multiclaude, from a branch with only the add-in's commits rebased on upstream.

### 5.7 What the friend verifies (an A1 check script)

`scripts/printer_check.py`: read-only except for one optional step. It prints a report the friend can paste into a PR.
- MQTT TLS and the CN check;
- `get_version`;
- a pushall dump, redacted, saved as a fixture;
- the presence of `vt_tray`;
- `sdcard`;
- a camera frame over 6000;
- an FTPS login and a listing of the card root.
- Optional, with `--upload-test`: an FTPS upload of a tiny file, a size check, then the file stays (no delete: the service never deletes).

It never starts a print. A1 values the script confirms become "verified" in `printers/a1.toml` in the friend's PR.

### 5.8 Decisions after the builder check (2026-11-27)

- **service-notify reaches the friend through upstream PRs** (owner's decision, option b). Upstream multiclaude (korsicheg/multiclaude) has neither the add-in nor what it builds on. The fork's service-notify sits on 11 fork-only commits and edits the core (`router/asks.py`, the add-in loader, `tests/conftest.py`). So the upstream work is a series:
  - **PR 1**: the prerequisites. That is `"frontdesk": "router"` and the core hooks service-notify needs (`question_tapped` and its routing), rebased on upstream/main.
  - **PR 2**: the service-notify add-in itself, on top of PR 1.

  Each PR gets the security and leak review from 5.6 and the owner's approval before it is opened. The bambu add-in's `setup` requires service-notify. Without it, `setup` stops and points at those PRs. There is no degraded mode without buttons.
- **Trust on the friend's machine** (owner's decision, option a): accept and document. The bambu add-in's README and `next-steps` say this plainly, in one paragraph. On upstream multiclaude, every assistant runs as the bot's Linux user with sudo. Any assistant on that machine can read the printer's access code and the service tokens, and could start a print over MQTT itself. The Telegram-button gate guards against a model misreading consent, not against an assistant set on forging it. One owner on the machine and `"frontdesk": "router"` keep the risk small.
- **The owner's values in the repo** (builder's call): the current tree's deploy docs, the unit, the example config and the tests are moved to example values. These are documentation IPs, `<bot user>`, and `/srv/bambu` kept as an example path. The owner's real values stay only on his server. History is not rewritten. Hustilin-Y/bambu-mcp is public, so the friend clones it and sends PRs from a fork.
- **The owner's setup** (builder's call): every change stays backward compatible. A `config.toml` without `printer` is P2S, and the current slice-agent keeps working. The owner gets the external spool later, with its own deploy and a live check of the `ams_id 255 / tray_id 254` payload on the P2S, when the owner asks.
- **The cad-workshop add-in as the model**: its `setup` still has two optional `sudo` calls (libgl and linger). The bambu add-in prints root commands instead of calling sudo.

## 6. What is left to the builder

- The exact profile file format and the loader's validation.
- The A1 HMS prefix and power values (fill if known, else mark unverified).
- How `setup` prompts: multiclaude's helper if it has one, else a hidden-input terminal prompt. Read how `assistant` prompts today.
- Whether the MCP registration uses `claude mcp add-json` or a multiclaude helper.
- The order of work. The profile refactor comes first, because the external spool and the camera depend on it.
