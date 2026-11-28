# Print tab in the multiclaude Mini App: design

Status: approved by the owner, 2026-11-28 (draft 2, after a builder check). Builds on the bambu-mcp design (`2026-11-22-bambu-mcp-design.md`), the portable add-in design (`2026-11-27-portable-addon-design.md`), the chat Mini App (`multiclaude/docs/chat-miniapp/SPEC.md`) and its add-in extension points (`multiclaude-local-gpu/docs/ORCHESTRA-MINIAPP.md` sections 3, 12, 13.7). Input: the PM review `.superpowers/miniapp/pm-review-2026-11-28.md` and the owner's answers there.

## 1. Intent

The owner wants everything about the printer in one place on the phone: see the printer, its slots and jobs, pause, resume, stop and start a print, see what the system can do, and later look at any cad-workshop model in 3D and turn it with a finger. Today this is spread over Telegram messages and the assistant, and some of it (resume, stopping a print started elsewhere, a picture now) has no button at all.

## 2. Goals, non-goals, anti-goals

Goals:
- A "Print" tab in the chat Mini App, owner only, delivered in steps: printer first (phase 1), then a file channel (phase 2), then 3D (phase 3).
- Start and stop confirmed inside the tab with the same strength as the Telegram button.
- Pause and Resume buttons in the tab and in Telegram.
- A Capabilities screen built from the code, so it can't drift from what the system really does.
- 3D view of every cad-workshop model, built on the server or on the PC, and of every job's model.

Non-goals (now):
- Slicing, variant choice or job creation from the tab (the assistant keeps doing them).
- Toolpaths from the sliced file, timelapse, live camera.
- Other people than the owner, and the friend's A1 setup. The tab works for the friend only once the `chat` add-in and add-in module loading are upstream.

Anti-goals (failure even if it works):
- Any path where a client token, a model or a script can start a print without the owner's press on a button the service sent.
- A secret (access code, serial, printer IP, tokens, camera URL) on the phone, in the public relay files or in a feed event.
- Anything that assumes a P2S or an AMS.
- The router or bambu-service holding a whole file in memory.

## 3. Constraints

- workshop: about 3 GB RAM, cad builds already take up to 1.2 GB. No decimation or mesh work on the server outside `cad.py build`.
- Cloudflare free plan. R2 is already enabled on the account (done for the chat relay on 2026-11-14).
- Relay limits (`relay/src/chat.js`): `addon_state` at most 64 KB and kept one per add-in, an event at most 16 KB, 20 sends a minute per person, a PUT from the VM at most 8 MB. Tab modules at most 100 KB each, page and modules under 300 KB gzip (ON4).
- `chat_tab_action` and `chat_assist` share one queue per person, one request at a time (`chat/__init__.py`, `_assisting`): a tab action must answer fast, and progress goes through `addon_state`.
- Owner only, the local-gpu way (router module installed beside multiclaude, `bambu` added to `ALL` in `lib/multiclaude/addons/__init__.py`, see 12). Upstream add-in loading comes later.
- bambu-service today exposes only `/mcp` and `POST /jobs`, and all client tokens are equal (`BearerTokenMiddleware`).

## 4. Approaches considered

How a start pressed in the tab reaches the printer:

| | Approach | Trade-off |
| --- | --- | --- |
| **A (chosen)** | The tab presses the same service-notify button the service sent to Telegram. The press enters the same queue with `via: "miniapp"`. | bambu-service gets no new start path, design section 16's "no client token can start a print" holds. Needs a generic change in service-notify (list and press waiting buttons from the Mini App). |
| B | A "confirm" endpoint in bambu-service for the tab. | Less code, but a client token could start a print, and Telegram and the tab would have two confirmation logics. |
| C | The router module calls the MCP tools. | Start is deliberately not a tool, and an MCP client in the router adds weight. |

How the phone shows 3D:

| | Approach | Trade-off |
| --- | --- | --- |
| **Own viewer (chosen)** | `cad.py build` writes a light preview in our own simple binary mesh format. The tab draws it with a small WebGL viewer (about 15 KB). | No 3MF parsing on the phone, fits the 100 KB module limit, more code to own. |
| three.js + 3MFLoader | Lazy-loaded, about 200 KB gzip. | Breaks ON4 and the module limit, needs an exception, parses zip and XML on the phone. |

How PC models reach the server: the server pulls from slice-agent on the PC while the PC is awake and caches them (chosen), versus the PC pushing after each build (needs a new server token on the PC and ties cad-workshop to the printer side, which its rules forbid).

## 5. Architecture

```
phone (tab module print.js) ──feed/tab_action──> relay (Worker + DO) ──> router (multiclaude)
                                                                         ├─ add-in module `bambu` (core.patch)
                                                                         │    └─ HTTP, scoped token ──> bambu-service /v1/*
                                                                         └─ service-notify (press from the app)
phone <── GET blob (tma auth) ── relay R2 bucket <── PUT (relay token) ── router `bambu` (phase 2+)
bambu-service ──> slice-agent on the PC (models, phase 3)
```

- **bambu-service** owns all printer data and all actions, as today. New: a small REST API under `/v1/` for the router module, behind a separate token with scopes (section 6.1).
- **Router module `bambu`** (in this repo, `multiclaude/addon/lib/bambu/`, installed like local-gpu's): the chat hooks, polling bambu-service while the page is open, streaming files to the relay. It holds no printer secret, only its scoped token.
- **service-notify** (multiclaude, generic, upstreamable): lists a service's waiting buttons for the Mini App and accepts a press from it.
- **Relay** (multiclaude, generic): an R2 bucket for add-in files and two generic routes (phase 2).
- **cad-workshop**: writes the preview at build. **slice-agent**: lists PC models and serves their previews.

## 6. Phase 1: the printer tab

### 6.1 bambu-service: `/v1/` API and scoped tokens

Token config gains scopes. Existing MCP client tokens keep full MCP access and get no `/v1/` scope. A new token `miniapp` (in `.env`, never in `config.toml`) has only the `/v1/` scopes:

| Endpoint | Scope | What |
| --- | --- | --- |
| `GET /v1/snapshot` | read | One JSON: status (as `_status`), link state, printer profile (model name, bed, slot layout, camera kind, storage kind, unverified list), slots with mapped spools, spools summary, the 20 newest jobs (id, name, state, plates, best variant's grams, time and cost), the 12 newest journal rows, pending confirmations (6.3), currency code and symbol. No serial, IP, access code or file paths. At most 48 KB (lists cut, `truncated: true`). |
| `GET /v1/capabilities` | read | Built from code (6.5). |
| `POST /v1/pause` | control | `service.pause_print()`. |
| `POST /v1/snapshot-to-telegram` | control | One camera frame sent to the owner through service-notify. Refused while another snapshot runs. |
| `POST /v1/prepare` `{job_id, plate}` | control | Same as the `prepare_print` tool: sends the Telegram confirmation. Answers `{pending: true}` at once and runs in the background (the PC may need up to 120 s to wake, and the message goes through the outbox). The confirmation shows in the snapshot's `pending` once service-notify has given its `message_id`, which the outbox keeps from now on (today it drops it, `service.py` near line 665). An error shows in the snapshot too. |
| `POST /v1/request-stop` | control | Same as `request_stop`: sends the Telegram stop confirmation. Works for prints started anywhere. |

None of these can start, resume or stop a print. The `/mcp` app and `POST /jobs` stay as they are.

### 6.2 Pause and Resume in Telegram

- service-notify's `once` is per message, not per button (`service_notify/taps.py`). So the buttons are kept as a whole: the STARTED message (own prints) and the first status message of an external print carry **Pause** and **Stop** (`once: true`). After a Pause press the service edits that message back to **Resume** and **Stop** (`PATCH /v1/messages/<id>` puts buttons back and makes them pressable again), and after Resume back to Pause and Stop. No service-notify change for this. [call]
- **Resume is a press, never a token.** It works only as a press on the Resume button the service sent, in Telegram or in the tab through `press_from_app` (6.3). There is no `/v1/resume` and no MCP tool for it. `printer/link.py` gains `resume` in its allowed commands, called only from the press handler, as stop is. Reason: resuming moves the head and heats again, the same class as a start, and the `miniapp` token sits on a machine every assistant can read. [call]
- Design section 9 changes: resume is exposed through the owner's press only. A model can pause, only the owner resumes.

### 6.3 Start and stop confirmed in the tab (approach A)

- bambu-service records each confirmation it sends (start or stop) with its service-notify `message_id`, kind, summary text, warnings and expiry, and lists the unexpired, unanswered ones in the snapshot's `pending`.
- service-notify, generic: a Python function for other router add-ins, `press_from_app(user, service, message_id, button_index)`. It runs the same checks a Telegram tap runs (the message is that service's, in that person's chat, not answered when `once`, not expired), queues the press with `via: "miniapp"`, and edits the Telegram message to "⏳ <label>… (Mini App)" as for a tap. Button ids never leave the router: the phone sees labels and indexes only. A press in Telegram after one in the app (or the reverse) gets the existing "already answered" outcome.
- The tab shows a pending confirmation as a sheet: the summary, warnings, and the same buttons with the same labels ("Стол чистый, печатать", "Отмена", stop's own). Pressing one sends `tab_action` `confirm {message_id, index}`. Pause, Resume and Stop messages appear in `pending` the same way, so the tab's **Продолжить** and **Стоп** are presses on the service's own buttons. The router module checks the person is the owner and calls `press_from_app`.
- Freshness: the relay already refuses a tab action whose init data is older than 1 h. The confirmation's own 10-minute expiry and the service's re-check of printer and AMS state on the press stay as they are (section 9).
- Design section 16 changes to: "Every print start is confirmed by the owner's press on a button the service sent, in Telegram or the same button shown in the Mini App to the owner only. No client token can start a print." The threat model paragraph is unchanged.

### 6.4 Router module `bambu`

- `chat_tabs(user)`: `[{"id": "print", "title": "Печать", "module": "print.js"}]` for the owner only, nothing for anyone else.
- `on_tick`: while the owner's page is open (the chat add-in's page-open signal, as local-gpu's `_look`), read `GET /v1/snapshot` every 5 s while the printer is printing, preparing or paused, every 30 s otherwise, and `feed_add` an `addon_state` only when it changed. Reads run in a background thread, the tick never waits. bambu-service down: the state says so with the time of the last good snapshot.
- `chat_tab_action(user, action, values)`: `pause`, `snapshot`, `prepare {job_id, plate}`, `stop`, `confirm {message_id, index}`, `capabilities`. Each answers within 5 s (`/v1/prepare` returns `pending` at once). Owner only. Unknown actions: an error.
- `chat_backfill(user)`: the last state, so the tab draws at once.
- Memory: the state is at most 60 KB, nothing else kept.

### 6.5 Capabilities screen

`GET /v1/capabilities` returns:
- MCP tools from the server's own registry (`mcp.list_tools()`): name, first line of the docstring, class from the annotations (read, change, destructive). Grouped in the tab as "смотреть", "менять", "опасное (нужно подтверждение)".
- Buttons: the Telegram buttons and tab actions the service offers, from one table in code that the senders also use.
- Printer: model name, AMS (units and slots, or "no AMS, external spool"), camera kind, storage kind, bed size, and the profile's unverified items marked "not checked on a real printer".

Text in English from the code. The tab's own chrome is Russian. [call]

### 6.6 The tab (phase 1)

```
[ Чат | Панель | Печать ]
┌ P2S · Печатает 42% · слой 118/280 · ещё 1 ч 12 мин
│ bracket-v3 (j17) · сопло 220° · стол 65°
│ ⚠ HMS: …
│ [Снимок] [Пауза] [Стоп]        (Продолжить instead of Пауза while paused)
├ Слоты: ■ PLA белый 640 г  ■ PETG чёрный 210 г  ▢ пусто
├ Задания: j17 печатается · j16 готово к печати ▸ · …
├ История: 12 последних, граммы и стоимость
└ Что умеет система ▸
```

- A job opens with its plates and best variant. **Печать** on a plate calls `prepare` and then shows the confirmation sheet (6.3) once it appears in the state.
- **Снимок** sends a frame to Telegram in phase 1 and shows it in the tab from phase 2.
- Buttons show only when allowed by the state and the profile (Pause only while running, Stop only in the profile's `stoppable_states`).
- Slots come from the profile: an A1 without AMS shows one external spool.

## 7. Phase 2: files to the phone

- Relay (generic, chat add-in): an R2 bucket `addon-files` bound as `ADDON_FILES` (owner confirmed on 2026-11-28), a lifecycle rule deleting objects after 1 day (created by `relay-deploy` with `wrangler r2 bucket create` and `lifecycle add`, as SPEC 9 planned for uploads). Routes: `PUT /app/chat/files/<addon>/<key>` from the VM with the relay token (at most 8 MB, the existing `PUT_MAX`), `GET /app/chat/files/<addon>/<key>` from the page with `Authorization: tma <init data>`, signature at most 24 h old. The relay knows members, not roles, so the PUT names the one Telegram id allowed to read the object (stored as R2 custom metadata) and a GET from anyone else gets 403. Keys are random, never a path. [call]
- Router `bambu` streams files to the relay in 1 MB chunks, never holding a whole file. An `addon_state` carries only the key and the size.
- The tab gains: the snapshot picture (the router fetches it from a new `GET /v1/snapshot.jpg`, control scope), and plate PNGs of jobs (`GET /v1/jobs/<id>/plates/<n>.png`, read scope).
- Fallback if R2 is refused: Durable Object storage in chunks of at most 1 MB, capped at 8 MB per file (SPEC 6.8).

## 8. Phase 3: 3D

### 8.1 The preview file

- `cad.py build` (and `kit`) writes `out/<name>.preview.bin` next to the 3MF, from the mesh it already holds: decimated to at most 60,000 triangles, positions quantized to uint16 over the bounding box, uint32 or uint16 indices, one group per body with a color when the model gives one. The mesh lies as printed when `--print-ready` was used. Target at most 1 MB. [call: format]
- Format v1: magic `CWPV`, version, bbox min/max (float32 x 6), group count, then per group color (RGBA8), vertex count, index count, positions, indices. Little endian. Normals are computed on the phone.
- `cad.py preview` already exists (it renders PNGs and rebuilds the model), so the new command is `cad.py mesh-preview <folder | --all>`: writes `preview.bin` for models already built, from their 3MF, without rebuilding. [call]
- A kit (`cad.py kit`) gets one preview for the whole 3MF: one group per object, each group tagged with its plate number. The viewer shows plate 1 with a plate switcher when there are more. [call]
- `cad.py` stays printer-agnostic: it writes a file and connects to nothing.

### 8.2 The catalog

- bambu-service keeps the catalog: `GET /v1/models` (read scope), entries `{id, name, folder, source: "server" | "pc", built_at, has_preview, has_png, synced_at}`, `GET /v1/models/<id>/preview.bin` and `/png`.
- Server models: `CAD_HOME/**/out/*.preview.bin`, at most 3 levels deep, rescanned at most once a minute.
- PC models: slice-agent gains `GET /models` and `GET /models/<id>/preview.bin|png` over its cad-workshop folder. bambu-service syncs every 10 minutes while slice-agent's `/health` answers, into `data_dir/models/pc/`, and never wakes the PC for it. A PC model deleted there disappears from the cache at the next sync. When the PC sleeps, the tab shows "с ПК, синхронизировано <time>".
- Jobs: `create_job` copies the preview next to the job's 3MF when the manifest's folder has one. A job without one shows its PNG.

### 8.3 The viewer

- `relay-public/viewer.js`, a lazily imported ES module, WebGL 1, at most 20 KB gzip.
- One finger turns around the model, pinch zooms, two fingers pan, double tap resets. The bed outline from the profile's `bed_mm`, Z up, the model as it lies on the bed.
- The tab's list "Модели": search by name, newest first, with source badges. Opening one asks `tab_action` `model {id}`. The router streams the preview to the relay (7) and puts its key in the state.
- Over 8 MB or no preview: the PNG.

## 9. Changes by repository

| Repo | Phase | Change |
| --- | --- | --- |
| bambu-mcp | 1 | `/v1/` API, scoped tokens, resume on press, Pause and Resume buttons, outbox keeps message ids, pending confirmations, capabilities, router module `bambu`, `print.js`, installer insert of `bambu` in `ALL`, design sections 9 and 16 updated |
| multiclaude | 1 | service-notify `press_from_app` and `via` on presses (generic) |
| multiclaude | 2 | relay R2 binding and file routes, the chat add-in's file helper (generic) |
| bambu-mcp | 2 | snapshot and plate PNG endpoints, streaming to the relay |
| cad-workshop | 3 | preview at build, `cad.py mesh-preview` |
| bambu-mcp | 3 | catalog, slice-agent model endpoints, sync, `viewer.js` |

## 10. Non-functional

| ID | Requirement |
| --- | --- |
| PN1 | The router grows under 5 MB with the tab open. Files pass through in chunks of at most 1 MB |
| PN2 | bambu-service: the snapshot is built from state in memory, no extra MQTT traffic. At most one request every 5 s from the router |
| PN3 | No serial, IP, access code, token, camera URL or server path in any `/v1/` answer, `addon_state`, relay file or tab module |
| PN4 | `print.js` at most 60 KB and `viewer.js` at most 20 KB gzip. Page and modules under 300 KB |
| PN5 | Everything from the profile: no P2S or AMS assumption in the module, the tab or the viewer |
| PN6 | Tests: bambu-mcp `tests/` for the API, scopes, router module and preview reader. multiclaude `tests/` for `press_from_app` and the file routes. cad-workshop `tests/` for the preview writer |

## 11. Acceptance criteria

Phase 1:
- **AC1.1** [auto] A `miniapp` token reaches `/v1/*` and gets 403 on `/mcp` and `POST /jobs`. An MCP token gets 403 on `/v1/*`.
- **AC1.2** [auto] No `/v1/` answer and no `addon_state` contains the serial, IP, access code, a token or a path (checked against the test config's values).
- **AC1.3** During a print the tab's progress follows the printer within 10 s.
- **AC1.4** Pause in the tab pauses the print, Продолжить continues it. The same from the Telegram buttons, which switch between Pause and Resume.
- **AC1.4a** [auto] No token, `miniapp` or MCP, can resume: there is no endpoint, and `link.py` sends resume only from the press handler.
- **AC1.5** Print on a plate: the Telegram confirmation appears and the tab shows the same sheet. Pressing "Стол чистый, печатать" in the tab starts the print, and the Telegram message turns to the outcome. A second press in Telegram says it's already answered.
- **AC1.6** [auto] `press_from_app` refuses a message of another service, another chat, an expired or answered one, and a person who isn't the owner. No button id reaches the page.
- **AC1.7** Stop for a print started from the printer's screen works from the tab with the same confirmation.
- **AC1.8** [auto] The Capabilities answer lists every registered MCP tool with its class, and a tool added in code appears without other changes.
- **AC1.9** [auto] With an A1 profile (no AMS) the snapshot has one external slot and the tab draws it.
- **AC1.10** [auto] A person who isn't the owner gets no tab and no `bambu` state.

Phase 2:
- **AC2.1** Snapshot in the tab shows a picture within 15 s.
- **AC2.2** [auto] A file GET without valid init data, or from a non-owner, gets 401 or 403. Objects are gone after a day.

Phase 3:
- **AC3.1** [auto] A preview written by `cad.py build` reads back to the same bounding box within 0.1 mm and at most 60,000 triangles.
- **AC3.2** The caulk-gun model (12.6 MB 3MF) opens in the tab and turns smoothly on the owner's Android phone.
- **AC3.3** A model built on the PC appears in the list within 10 minutes while the PC is awake, and stays visible while it sleeps.

## 12. Calls I made (the owner may override)

- Resume is not an MCP tool and has no token endpoint, only the service's button.
- Pause and Snapshot act at once from the tab. Resume and Stop are presses on the service's buttons.
- The `/v1/` snapshot is one endpoint, not one per section, to keep the router simple and the requests few.
- The Capabilities text stays in English from the code.
- The preview format is our own, not glTF: smaller to write and read, no library on either end.
- Model sync every 10 minutes, never waking the PC.

- `bambu` joins `ALL` by an idempotent text insert in its installer (add the name if missing, remove it on teardown), not by a second `core.patch`: local-gpu's patch already edits the same line, and a patch would not apply on top of it. Order: local-gpu's patch first when both are installed. [call]
- **[owner]** The R2 bucket `addon-files` with a 1-day lifecycle on the existing Cloudflare account (phase 2), confirmed 2026-11-28.

## 13. Left to the builder

- Decimation method for the preview (trimesh's quadric decimation or another), as long as AC3.1 holds and the build's memory cap is kept.
- The exact JSON shapes of `/v1/` answers and the tab's layout details.
- How the chat add-in's page-open signal is read (reuse local-gpu's pattern).
- slice-agent's config for the cad-workshop folder on the PC.
