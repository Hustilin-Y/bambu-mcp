# Slicing on your own server: options

bambu-service never slices itself. It sends jobs to **slice-agent**, a small HTTP service that runs
the Bambu Studio CLI, and fetches the sliced file back. The service side is the same for every option:

```toml
# bambu-service config.toml
slice_agent_url = "http://127.0.0.1:11436"   # or the PC's address for option c
# gateway_url stays unset unless a sleeping PC has to be woken first (option c)
```

```sh
# bambu-service .env (mode 600)
SLICE_AGENT_TOKEN=<a long random string, the same one the agent gets>
```

The agent's own settings live in `slice-agent.toml`: `$XDG_CONFIG_HOME/bambu-mcp/slice-agent.toml`
(usually `~/.config/bambu-mcp/slice-agent.toml`) on Linux, `%LOCALAPPDATA%\bambu-mcp\slice-agent.toml`
on Windows. On Linux, `studio_exe` and `studio_resources` are required:

```toml
printer = "a1"                     # the same profile name as bambu-service's config.toml
listen = ["127.0.0.1", 11436]      # loopback when the service is on the same host
studio_exe = "/opt/studio/squashfs-root/AppRun"
studio_resources = "/opt/studio/squashfs-root/resources/profiles/BBL"
# machine, process_sets and filaments default to the profile's; list extra filaments here:
# filaments = ["Generic PLA @BBL A1", "Generic PETG @BBL A1"]
```

After the first start, run `slice-agent flatten` once (and again after changing filaments or
Studio). It writes the flattened presets to `profiles_dir`, outside the repo: they are Bambu's
data and never go into git.

All options share these risks:

- **Pin Studio.** The owner's setup is checked with Bambu Studio **02.08.02.61**. Another
  version may name settings differently. Pin one version and update on purpose.
- **Same profile on both sides.** `printer` in `slice-agent.toml` must name the same profile as
  bambu-service's config.toml. The agent checks a sliced file's `printer_model` and bed against
  its own profile, and the service checks them again against the printer's before it prints.
- **Golden slice.** After setup, slice one known part through the service (`slice_job`) and
  check `job_status`: a wrong preset shows up as a clear error, not a bad print.
- **Studio's Linux CLI is less travelled.** It needs a display even when slicing (hence
  `xvfb-run`) and a set of GTK/WebKit libraries. Headless quirks show up as a non-zero exit
  code and a log tail in `job_status`.
- **pc-agent is a Windows-PC helper.** Without `PC_AGENT_TOKEN` the agent prints a warning
  at start and slices anyway.

## (a) AppImage and xvfb-run, as a systemd user unit (simplest on one server)

1. Packages (Ubuntu 24.04): `sudo apt install xvfb xauth libgl1 libegl1 libgtk-3-0
   libwebkit2gtk-4.1-0 libgstreamer1.0-0 libgstreamer-plugins-base1.0-0`.
2. Download the Studio AppImage of the pinned version from Bambu Lab's GitHub releases and
   extract it, so it runs without FUSE:
   `mkdir -p /opt/studio && cd /opt/studio && ./Bambu_Studio_*.AppImage --appimage-extract`
   (it creates `squashfs-root/`).
3. In your bambu-mcp checkout: `uv sync --frozen --no-dev`.
4. Write `slice-agent.toml` as above and `~/.config/bambu-mcp/slice-agent.env` (mode 600)
   with `SLICE_AGENT_TOKEN=...`.
5. `xvfb-run -a .venv/bin/slice-agent flatten`.
6. Copy `deploy/slice-agent/slice-agent.service` to `~/.config/systemd/user/`, then
   `systemctl --user daemon-reload && systemctl --user enable --now slice-agent`. For it to run
   without a login session, root runs `loginctl enable-linger <user>` once.
7. Set `slice_agent_url` and `SLICE_AGENT_TOKEN` for bambu-service and restart it.

## (b) Docker

`deploy/slice-agent/Dockerfile.example` and `compose.example.yaml` show one way: Ubuntu, the
extracted AppImage, the agent, `xvfb-run`. They are examples: bambu-mcp does not build or
publish an image. Build it with `--build-arg STUDIO_APPIMAGE_URL=<pinned release URL>`, mount
`slice-agent.toml` read-only, keep `slice-agent.env` (mode 600) out of git, publish the port on
`127.0.0.1` only, and run `docker compose run --rm slice-agent xvfb-run -a .venv/bin/slice-agent
flatten` once. The service side is as above.

Good when the server already runs containers, or to keep Studio's libraries off the host.

## (c) The slice-agent on a Windows PC

This is how the owner runs it: `deploy/slice-agent/README.md` and `install.ps1` set up a
scheduled task and a firewall rule letting only the server in
(`install.ps1 -ServerAddress <server IP>`). Set `printer = "<your profile>"` in the PC's `slice-agent.toml`
too: without it the agent slices for the P2S. bambu-service refuses to print a file sliced for
another model, so a mismatch shows up as a refused print, never as a wrong one. On the service side, set
`slice_agent_url = "http://<PC IP>:11436"`. If the PC sleeps, orchestra-gateway's `gateway_url`
and `GATEWAY_KEY` wake it first; without them the PC has to be awake when you slice.

Good when the server is small (Studio needs 1-2 GB of RAM per slice) or you already slice on
that PC.
