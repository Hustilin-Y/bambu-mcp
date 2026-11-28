# bambu-mcp

A Python service that monitors and drives a Bambu Lab 3D printer over its local (LAN-only) MQTT, FTPS and camera interfaces, and exposes it to MCP clients and Telegram notifications.

## Phases

1. Monitor: read-only status, print tracking, energy and cost estimates, notifications.
2. Jobs: print jobs from CAD Workshop, sliced on the owner's PC, spool inventory, `load_spool`.
3. Printing: start a sliced job, pause and stop. A print starts or stops only on the owner's press in Telegram.

## MCP tools

| Kind | Tools |
| --- | --- |
| Read | `printer_status`, `ams_status`, `camera_snapshot`, `print_history`, `spools`, `plates`, `jobs`, `job_status` |
| Write (service data) | `add_spool`, `update_spool`, `set_spool_weight`, `map_slot`, `add_plate`, `set_default_plate`, `set_tariff`, `set_currency`, `set_country`, `set_amortization`, `update_print`, `create_job`, `propose_job`, `slice_job`, `evaluate_job`, `choose_variant`, `reset_settings` |
| Printer writes | `load_spool` (AMS slot or external spool setting), `prepare_print` (sends the start confirmation, starts nothing), `pause_print` (only while `RUNNING`), `request_stop` (sends the stop confirmation, stops nothing) |

There is no `start_print` tool and no resume. See [deploy/README.md](deploy/README.md), step 8, for what phase 3 needs and what is never exposed.

## Printer models

A printer model is a profile in `src/bambu_profiles/<model>.toml`, picked by `printer = "<model>"` in config.toml (and in the slice-agent's `slice-agent.toml`). The default is `p2s`.

| Profile | Status |
| --- | --- |
| `p2s` | Verified live: MQTT, FTPS to a USB stick, RTSPS camera, AMS, slicing with Studio 02.08.02.61. The external spool holder (slot 0) stays off (`external_spool = false`) until it is checked live. |
| `a1` | Best guesses from public sources (no AMS, external spool, microSD, JPEG camera on port 6000). Every field but the model name and bed size is listed as `unverified`. |

For a new model, copy a profile, run `scripts/printer_check.py` (read-only, never prints) against the printer and move what it confirms out of `unverified`. Pull requests welcome.

## Install

- With [multiclaude](https://github.com/korsicheg/multiclaude): the add-in in [multiclaude/](multiclaude/addon/README.md) (`multiclaude/install.sh`, then `assistant addon enable bambu`). It needs the service-notify add-in.
- By hand, as a system service: [deploy/README.md](deploy/README.md).
- Slicing (Bambu Studio CLI) on a Windows PC or a Linux server: [deploy/slicing-options.md](deploy/slicing-options.md).

## Design

See the spec in [docs/superpowers/specs/2026-11-22-bambu-mcp-design.md](docs/superpowers/specs/2026-11-22-bambu-mcp-design.md), and for profiles and the add-in [docs/superpowers/specs/2026-11-27-portable-addon-design.md](docs/superpowers/specs/2026-11-27-portable-addon-design.md).

## Development

```
uv run pytest -q
```

The add-in tests (`tests/test_addon.py`) need bash and jq and are skipped on Windows.

## Licence

MIT
