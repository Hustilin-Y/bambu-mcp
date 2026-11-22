# bambu-mcp

A Python service that monitors a Bambu Lab P2S 3D printer over its local MQTT and camera interfaces, and exposes the state to MCP clients and Telegram notifications.

## Phases

1. Monitor: read-only status, print tracking, energy and cost estimates, notifications.
2. Control: commands to the printer (not started).

Phase 1 sends only the `get_version` and `pushall` requests to the printer.

## Design

See the spec in [docs/superpowers/specs/2026-11-22-bambu-mcp-design.md](docs/superpowers/specs/2026-11-22-bambu-mcp-design.md).

## Development

```
uv run pytest -q
```

## Licence

MIT
