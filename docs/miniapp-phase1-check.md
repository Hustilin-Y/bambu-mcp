# Print tab, phase 1: the owner's check on the phone

What the tests can't see: the tab in Telegram on a real phone with the real printer. Design: `docs/superpowers/specs/2026-11-28-miniapp-print-tab-design.md`, acceptance AC1.3, AC1.4, AC1.5 and AC1.7.

## Deploy (Claude does it on workshop, with the owner's OK)

1. In `~/assistant`: `~/bambu-mcp/multiclaude/install.sh --uninstall` if the tab was installed before, then the multiclaude change with `press_from_app` (service-notify) is merged and pulled.
2. In `~/bambu-mcp`: this branch is merged and pulled, then `multiclaude/install.sh` and `assistant addon enable bambu`. `setup` adds `APP_TOKEN_MINIAPP` to `~/.config/bambu/.env`, writes `~/.config/bambu/miniapp.json` and restarts `bambu-service`.
3. `assistant relay deploy`, so the relay serves `addons/bambu/print.js`.
4. Restart the router the way the workshop-server skill says, so it loads the `bambu` module.

## Checks

| # | Do | See |
| --- | --- | --- |
| 1 | Open the Mini App | A tab **Печать** after Chat, Panel and the local-gpu tabs. Nobody but the owner sees it |
| 2 | Open **Печать** with the printer idle | The printer's model and "Свободен", the slots the printer reported, the jobs and the history |
| 3 | **📷 Снимок** | "Снимок придёт в Telegram", and a photo arrives in the chat within about 15 s |
| 4 | Print a short test job from the tab: **🖨 Печать** on its plate | "Готовлю печать…", then within 2 minutes (the PC may wake) a sheet with the same summary as the Telegram message and **Стол чистый, печатать** / **Отмена** |
| 5 | Press **Стол чистый, печатать** in the tab | The print starts. The Telegram message says "⏳ Стол чистый, печатать… (Mini App)", then the outcome. A press on the Telegram message after that says it's already answered |
| 6 | While it prints | Progress and the layer follow the printer within 10 s (AC1.3) |
| 7 | **⏸ Пауза** in the tab | The printer pauses. The print's Telegram message now shows **▶️ Продолжить** and **Остановить…** |
| 8 | **▶️ Продолжить** in the tab | The print resumes. The Telegram message shows **⏸ Пауза** again (AC1.4) |
| 9 | Pause and resume with the Telegram buttons | The same, and the tab follows (AC1.4) |
| 10 | Start a print on the printer's screen, then **⏹ Стоп** in the tab | A stop sheet with **Да, остановить** / **Отмена**. **Да, остановить** stops it (AC1.7) |
| 11 | **Что умеет система ▸** | The MCP tools in three groups, the buttons, the printer's AMS, camera, storage and unverified items |
| 12 | Stop `bambu-service` for a minute with the tab open | A yellow line "Сервис принтера не отвечает, данные на HH:MM", the data stays, and it clears once the service is back |

Anything that differs: a screenshot and the step number to Claude.
