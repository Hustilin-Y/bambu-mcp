# bambu-mcp

A Python service that monitors a Bambu Lab P2S 3D printer over its local MQTT and camera interfaces, and exposes the state to MCP clients and Telegram notifications.

## Phases

1. Monitor: read-only status, print tracking, energy and cost estimates, notifications.
2. Jobs: print jobs from CAD Workshop, sliced on the owner's PC, spool inventory, `load_spool`.
3. Printing: start a sliced job, pause and stop. A print starts or stops only on the owner's press in Telegram.

## MCP tools

| Kind | Tools |
| --- | --- |
| Read | `printer_status`, `ams_status`, `camera_snapshot`, `print_history`, `spools`, `plates`, `jobs`, `job_status` |
| Write (service data) | `add_spool`, `update_spool`, `set_spool_weight`, `map_slot`, `add_plate`, `set_default_plate`, `set_tariff`, `update_print`, `create_job`, `propose_job`, `slice_job` |
| Printer writes | `load_spool` (AMS slot setting), `prepare_print` (sends the start confirmation, starts nothing), `pause_print` (only while `RUNNING`), `request_stop` (sends the stop confirmation, stops nothing) |

There is no `start_print` tool and no resume. See [deploy/README.md](deploy/README.md), step 8, for what phase 3 needs and what is never exposed.

## Design

See the spec in [docs/superpowers/specs/2026-11-22-bambu-mcp-design.md](docs/superpowers/specs/2026-11-22-bambu-mcp-design.md).

## Development

```
uv run pytest -q
```

## Licence

MIT
