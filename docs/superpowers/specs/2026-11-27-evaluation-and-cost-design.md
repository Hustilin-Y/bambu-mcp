# Evaluation mode and the full cost: design

Date 2026-11-27. Owner: the bambu-mcp owner. Builds on the main design (`2026-11-22-bambu-mcp-design.md`, its "Later, asked by the owner: evaluation mode" part) and on phases 1-3, which are live on workshop.

## 1. Intent

The owner wants the connector to pick good print settings for a part's purpose, without hand-tuning infill and walls. They also want to see what a print really costs before printing it. Two requests, one design, because both live in the proposal and the summaries:

- **Evaluation:** a goal ("прочно", "быстро", "дёшево"…) drives the settings. On one button press the service slices a few variants on the PC, compares them, and sets the best one.
- **Cost:** every proposal, sliced summary and print confirmation shows plastic, electricity and printer amortization as separate lines plus a total. Currency and country are settings, not hard-coded.

## 2. Goals, non-goals, anti-goals

Goals:
- A goal is always shown on the proposal, with where it came from (owner, CAD Workshop's agent, or the manifest fallback). It can be changed with one tap.
- "🔍 Подобрать" slices 4-6 variants for the goal and replaces the proposal with the winner plus a comparison table. The owner can pick another variant with one tap.
- The CAD Workshop agent sets the goal from the part's purpose, through `PRINT_HINTS`.
- Cost in three lines before printing: plastic, electricity (kWh and tariff), amortization (hours × rate).
- The currency, its symbol and the country can be changed. History is kept, so old journal rows keep their own money.

Non-goals:
- Evaluation runs only on a button press. It never runs automatically on every job (owner's choice).
- No physical strength testing in this work. The strength proxy is uncalibrated until the owner breaks a few parts (later).
- No exchange-rate conversion between currencies and no tariff fetching from the web.
- Multi-material plates stay out of scope, as in phase 3.

Anti-goals (a failure even if it works):
- A goal button or "Подобрать" that wakes the PC without the owner asking. Goal buttons only change the goal. Slicing starts only from "Подобрать".
- Evaluation silently overriding what the owner said in words. Ruling R6 stands: the owner's explicit settings win over the evaluation, and the evaluation varies only the settings the owner did not fix.
- A winner that breaks the part's own checks (supports the checks require, or tolerance features thinned).
- Money shown without its currency, or old journal costs changing when a rate changes.

## 3. Constraints

- The slice-agent on the PC slices one job at a time. A slice of a small part takes about 5 s and a large one up to about a minute. Each `/slice` holds a pc-agent lease that keeps the PC awake for 10 minutes afterwards.
- Workshop has about 3 GB of RAM. bambu-service stays under MemoryMax 150M. Variant files are not copied to workshop: metrics come back in the `/slice` JSON.
- Overrides go only through the slice-agent whitelist (`slice_agent.runner`, shared constants since phase 2 Task 8). New keys need a range and a test.
- Telegram button rows: at most 8 per row and 24 in all per message (service-notify).
- Russian Telegram texts with no em dash and no semicolon. English code, docs and errors to Claude.
- Two repos. bambu-mcp holds the service and the agent. CAD Workshop holds `PRINT_HINTS` and the agent rules. Each repo has its own branch and PR.

## 4. Approaches

The owner first chose **when** evaluation runs. The options were:
- every job automatically: wakes the PC for every part, even a 10-minute test;
- a button on the proposal;
- rules first, then slicing on request: two levels.

The owner picked **the button**.

For **how** it is built, the options were:
- **A, the service iterates.** bambu-service calls the existing `/slice` 4-6 times. The agent adds per-feature volumes, parsed from the G-code on the PC, to each `/slice` response. The service scores the variants against the preset table.
- **B, a new `/evaluate` on the PC.** The agent slices all variants under one lease and returns a table. The selection logic would live on the PC.

The owner picked **A**. The preset rules stay in one place next to the existing settings logic, the agent change is small, and the 10-minute lease after each slice keeps the PC awake between variants.

## 5. Design

### 5.1 Goals and presets

Keys and Russian labels: `balanced` «Обычно», `speed` «Быстро», `strength` «Прочно», `quality` «Красиво», `cheap` «Дёшево», `light` «Легко», `fit` «Точно», `watertight` «Герметично». A job has a primary goal and an optional secondary one. The primary sets the hard limits. The secondary breaks ties.

| Goal | Varies (the candidate grid) | Score | Hard limits |
|---|---|---|---|
| balanced (default) | infill 10-25 %, walls 2-3, grid/gyroid | equal weights: time, cost, strength | the manifest checks pass |
| speed | layer 0.24-0.28, infill 5-15 %, walls 2, lightning/grid | time | strength ≥ 0.6 × balanced |
| strength | walls 3-6, infill 25-60 %, gyroid/cubic/adaptivecubic, top/bottom +2 | strength, then time | time ≤ 2.5 × balanced |
| quality | layer 0.12-0.16, slower outer wall, seam aligned or back, ironing on top when the part has flat tops | the smallest layer, then time | time ≤ 3 × balanced |
| cheap | infill 0-12 %, walls 2, lightning, minimum shells, no brim unless the checks ask | total cost | strength ≥ 0.5 × balanced, the checks pass |
| light | infill 0-10 %, lightning/honeycomb, walls 2, minimum shells | grams | strength ≥ 0.4 × balanced |
| fit | classic wall generator, walls ≥ 3, slow outer wall, layer 0.12-0.2 | dimensional risk (outer wall speed, layer, wall count), then time | no tolerance feature is thinned |
| watertight | walls 4-6, top/bottom ≥ 5, 100 % infill when the part's volume is under a cap | shell thickness | no sparse infill reaches an outer surface |

"× balanced" means against the balanced variant of the same run, which is always sliced first. The exact grid points (which 4-6 combinations per goal) are the builder's call, within the ranges above.

### 5.2 Who picks the goal

The order, first wins:
1. **The owner**, with a goal button or in words through the MCP tools (`goal` parameter, ruling R6).
2. **CAD Workshop's agent**, through `PRINT_HINTS["goal"]` (primary, or `[primary, secondary]`) and `PRINT_HINTS["goal_reason"]` (≤ 200 characters, shown on the proposal).
3. **The service fallback** from the manifest:
   - tolerance features in the checks → `fit`;
   - a cavity open at the top → `watertight`;
   - otherwise `balanced`.

The proposal shows «цель: прочно (от ассистента: несущий кронштейн)», «(выбрал ты)» or «(по манифесту)».

### 5.3 The button flow

- **The proposal**, instant and with no slicing, has these rows:
  - Резать / Поменять;
  - the slot buttons (phase 2 Task 12);
  - the goal buttons, the current one marked ✓ (up to 8, in two rows of 4);
  - «🔍 Подобрать».
- **A goal press** stores the goal on the job with the source "owner". It sets that goal's **base point**, a fixed default from the preset table, and edits the proposal in place. It does not slice and does not wake the PC. Example base point for `strength`: walls 4, infill 40 % gyroid, top/bottom +2 (owner's decision 2026-11-27).
  - The base point becomes the `goal` settings layer: below `variant` and `explicit`, above the last job, the hints and the named set. Precedence: explicit > variant > goal base > last job > hints > named set.
  - So "Прочно", then "Резать", slices the base point. "Подобрать" refines it by slicing.
  - A goal picked by the CAD agent or the fallback also applies its base point. Each goal's base point is the middle of its grid in 5.1 (the builder writes them down in one table).
- **«🔍 Подобрать»**:
  1. Wake the PC (phase 2 waker).
  2. Slice the balanced baseline, then 3-5 goal variants, one after another through `/slice`.
  3. Score them and store all variants with the job.
  4. Edit the proposal to the winner, with a table of at most 6 lines: «1 ✓ 4 стенки 40% gyroid · 52 мин · 8 г · 14.45 грн · прочность 1.8×».
  5. Add a row of «Вариант N» buttons.

  While it runs, the proposal says «подбираю варианты…» and the button is disabled through the same busy guard as slicing. A second press is answered «уже подбираю».
- **«Вариант N»** sets that variant's settings as the job's explicit settings and marks it ✓. **«Резать»** then slices exactly that variant: the existing flow, reusing the variant's file when its settings and slot are unchanged.
- **The MCP tools**: `propose_job(job_id, slot?, goal?)`, `evaluate_job(job_id, goal?)` (runs the same evaluation and returns the table), `choose_variant(job_id, n)`. `slice_job(job_id, settings?, slot?, goal?)`: explicit settings win, and a `goal` applies its base point.
- **Failures**: a variant that fails to slice is listed as failed and skipped. If the baseline fails, the evaluation fails with the agent's reason, and the proposal goes back to its previous text.

### 5.4 Metrics from the slice-agent

The `/slice` 200 body gains `features` per plate:
- extruded volume in mm³ for: outer wall, inner wall, top surface, bottom surface, sparse infill, internal solid infill, support, other;
- the layer height used.

These are parsed on the PC from the G-code `;TYPE:` markers and the extrusion lengths inside the `.gcode.3mf`. The parse is bounded: the existing output caps apply, and parsing stops after the cap. Older agents omit `features`, and the service then shows strength as "?".

Strength proxy, relative to the balanced variant:

`S = V_outer + V_inner + V_top + V_bottom + V_internal_solid + k(pattern) × V_sparse`

with k = 1.0 for gyroid, cubic and adaptivecubic, 0.9 for grid and rectilinear, 0.8 for honeycomb, 0.6 for lightning, and 0.7 otherwise. The k values are the builder's starting point, marked as uncalibrated in the code.

### 5.5 Whitelist additions

Add to the slice-agent override whitelist. Each gets a range test and is exposed in the evaluation grid only for the goals that use it:
- `top_shell_layers` and `bottom_shell_layers`: 0-20
- `outer_wall_speed`: 20-300 mm/s
- `seam_position`: `aligned`, `back`, `nearest`, `random`
- `ironing_type`: `no ironing`, `top`, `topmost`
- `wall_generator`: `classic`, `arachne`
- `layer_height` (exists): 0.08-0.28 for a 0.4 nozzle

Check each key name against the flattened P2S presets before adding it. Names that Studio 02.08 spells differently use Studio's spelling.

### 5.6 Cost

- **Currency and country**: service settings, changed by the owner with one MCP command.
  - `currency` is an ISO code (default `UAH`).
  - `currency_symbol` (default `грн`).
  - `country` is ISO (default `UA`). It sets the default timezone and whether the quarterly tariff reminder runs (UA only).
  - Changing the currency asks nothing else: new prices and rates are entered in it.
- **Money values**: spool prices, tariff entries and amortization entries each store their currency. Journal rows keep the currency they were costed in. Mixed currencies inside one cost are refused, and the line says which price is in another currency. Nothing is converted.
- **Amortization**: a rate per hour of printing, with history like the tariff.
  - The owner sets the printer price, the expected service life in hours, and an optional upkeep per hour (nozzles, belts, plates) with `set_amortization(valid_from, printer_price, life_hours, upkeep_per_hour=0)`.
  - Rate = price ÷ life hours + upkeep.
  - Predicted cost uses the sliced time. The journal uses the actual time.
- **Where cost shows**: the proposal (from the manifest estimate, or the last slice when there is one), the evaluation table (total per variant), the sliced summary and the print confirmation. The lines read:
  - «пластик 5.60 грн (8 г × 700 грн/кг)»
  - «свет 1.92 грн (0.44 кВт·ч × 4.32)»
  - «амортизация 6.93 грн (52 мин × 8.00 грн/ч)»
  - «итого 14.45 грн»
- **The journal row** gains `cost_amort` and `currency`. Schema v9 holds the currency fields and the amortization history.

### 5.7 CAD Workshop side

- `PRINT_HINTS` gains:
  - `goal`: one key, or a list of two keys, from 5.1;
  - `goal_reason`: a string of at most 200 characters.

  Both are validated by the closed key list, like the existing hints. The manifest carries them in `hints`, and the bambu-mcp manifest reader accepts them.
- The cad skill and CLAUDE.md tell the agent to set `goal` from the task and to say why: what the part does, the load, heat, outdoor use, fits, liquids, whether it is a test or calibration piece. The guidance includes the examples from the main design.

### 5.8 Calls made after the builder check (owner may override)

- **Manifest fallback and the `fit` and `watertight` limits.** The manifest has no tolerance-feature or open-cavity data today, and adding it to CAD Workshop's checks is out of scope here. So the fallback is `balanced` only: `fit` and `watertight` come from the owner or from the CAD agent's `goal`. Their hard limits become rules on the settings:
  - `fit`: walls ≥ 3, the classic wall generator, outer wall speed ≤ 60 mm/s;
  - `watertight`: walls ≥ 4, top and bottom ≥ 5 layers.
- **What the evaluation may vary.** Only the owner's words fix a setting (`explicit`, R6). `PRINT_HINTS` values and the last job's settings are starting points the evaluation may change. A chosen variant is stored as its own layer `variant`, between explicit and the last job. It is not stored as explicit, so a later "Подобрать" (for example after a goal change) can still vary it. The next job of the same part inherits the chosen variant's settings as "last job", as today.
- **Kits.** Evaluation runs for `kind = part` only in this version. For a kit, "Подобрать" is not shown, and the goal buttons still set the goal for the record.
- **Button budget.** The owner has one AMS (4 slots): 2 + 4 + 8 + 1 + 6 = 21 buttons, under 24. With more slots, the variant row replaces the goal row after an evaluation.

## 6. What is left to the builder

- The exact candidate grid per goal, within the 5.1 ranges, at most 6 variants including the baseline.
- The scoring weights beyond the primary/secondary rule, and the tie-breaks.
- The G-code parser details and the k(pattern) starting values (5.4).
- The storage of variants (a table or job JSON) and the proposal text layout within the Telegram limits.
- The amortization defaults (a P2S price and life hours) shown in the README as an example only. The owner sets the real values.
- The order of work across the two repos.
