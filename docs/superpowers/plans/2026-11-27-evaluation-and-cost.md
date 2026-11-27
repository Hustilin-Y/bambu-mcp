# Evaluation mode and the full cost: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** a goal ("прочно", "быстро"…) drives a part's print settings, with a base point set instantly and a "Подобрать" button that slices variants on the PC and sets the best one. Every proposal, summary and print confirmation shows plastic, electricity and amortization in the configured currency.

**Architecture:** the service owns everything:
- the goal table, the settings layers and the scoring;
- the variant loop, which goes through the existing `/slice`.

The slice-agent adds per-feature extrusion volumes to its `/slice` answer and accepts a few more whitelisted overrides. Money gets one formatter, a per-row currency, and an amortization history next to the tariff history. CAD Workshop's `PRINT_HINTS` gains `goal` and `goal_reason`.

**Tech Stack:** Python 3.12, uv, pytest `-W error`, SQLite (schema v9 to v11), httpx, mcp 2.3.0. CAD Workshop: cadlib, pytest.

**Spec:** `docs/superpowers/specs/2026-11-27-evaluation-and-cost-design.md`. Context: the main design `docs/superpowers/specs/2026-11-22-bambu-mcp-design.md` (sections 7, 11) and rulings R5 and R6 of phase 2 (`.superpowers/sdd/2026-11-23-phase2-slicing/progress.md`).

**Branch:** `evaluation` from `phase3-print` (5dfe1c2 or later). CAD Workshop: branch `print-goal` from `main`.

## Global Constraints

- Goal keys and labels, exactly: `balanced` «Обычно», `speed` «Быстро», `strength` «Прочно», `quality` «Красиво», `cheap` «Дёшево», `light` «Легко», `fit` «Точно», `watertight` «Герметично». A goal is a primary key plus an optional secondary key.
- Settings precedence (one function, `resolve_settings`): explicit > variant > goal base point > last job > hints > named set. Only explicit (the owner's words) fixes a setting against an evaluation.
- Goal sources: `owner`, `assistant` (the CAD agent, from `PRINT_HINTS.goal`), `fallback` (the manifest has no goal, which gives `balanced`). The proposal names the source: «выбрал ты», «от ассистента: <goal_reason>», «по умолчанию».
- A goal press changes the goal and its base point only. It never slices and never wakes the PC. Only «🔍 Подобрать» and `evaluate_job` slice variants.
- Evaluation runs for `kind = part` only. It uses at most 6 slices per run, the balanced baseline first, sequentially through `SliceClient.slice`, under the existing busy guard. A failed variant is skipped. A failed baseline fails the run and restores the previous proposal text.
- Strength proxy, relative to the run's baseline: `S = V_outer + V_inner + V_top + V_bottom + V_internal_solid + k × V_sparse`. Starting k values: 1.0 for gyroid, cubic and adaptivecubic. 0.9 for grid and rectilinear. 0.8 for honeycomb. 0.6 for lightning. 0.7 for anything else. Marked uncalibrated in the code.
- Rules for the hard limits: `fit` needs walls ≥ 3, `wall_generator = classic` and `outer_wall_speed` ≤ 60. `watertight` needs walls ≥ 4 and top and bottom ≥ 5 layers. Every variant still passes the manifest's checks: supports when `supports_suggested`.
- Amortization rate per hour = printer price ÷ life hours + upkeep per hour. It is kept with history by `valid_from`, like the tariff. Predicted cost uses the sliced minutes. The journal uses the actual minutes.
- Currency: an ISO code and a symbol (default `UAH` and `грн`). Country: ISO (default `UA`). Each spool price, tariff entry, amortization entry and journal row stores its currency. Nothing is converted. A cost whose inputs are in different currencies is refused, and the reason names the input.
- Cost lines, Russian, one formatter everywhere:
  - «пластик 5.60 грн (8 г × 700 грн/кг)»
  - «свет 1.92 грн (0.44 кВт·ч × 4.32)»
  - «амортизация 6.93 грн (52 мин × 8.00 грн/ч)»
  - «итого 14.45 грн»
  - With no amortization rate set, that line reads «амортизация: не задана» and the total excludes it.
- Telegram: at most 24 buttons per message. Russian texts have no em dash and no semicolon and use the existing escaping. Tests never touch the PC, the printer or Telegram.
- Phases 1-3 behaviour stays intact unless this plan changes it. The service is live on workshop.

## Review Focus

- The owner says «4 стенки» and then presses «Подобрать»: walls stay 4 in every variant (explicit wins). Tested in Task 6.
- A goal press while «Подобрать» is running is refused like a second «Подобрать» («уже подбираю»), and the stored goal does not change. Tested in Task 6.
- A spool priced in UAH after the currency is changed to EUR: the cost is refused with a line naming the spool, never mixed. Tested in Task 1.
- The baseline slices but every goal variant fails: the run ends with the baseline as the only variant and says so, with no crash and no empty table. Tested in Task 6.
- An old agent answers `/slice` without `features`: strength shows «?», the score uses time, grams and cost only, and the run still finishes. Tested in Task 6.

---

### Task 1: Money: currency, country, amortization, cost model

**Files:** Modify `src/bambu_mcp/store/db.py` (schema v9), `models.py`, `config.py`, `cost.py`, `mcp_server.py`. Test `tests/test_cost.py`, `tests/store/test_db.py`, `tests/test_mcp_server.py`.

**Interfaces:**
- Schema v9:
  - `meta` keys `currency`, `currency_symbol`, `country`;
  - `spools.currency`, `tariffs.currency`, `prints.currency`, `prints.cost_amort` (default `'UAH'` for existing rows);
  - a new table `amortization(valid_from TEXT PRIMARY KEY, printer_price REAL, life_hours REAL, upkeep_per_hour REAL, currency TEXT)`.
- `Money = (amount: float, currency: str)`. `cost.amortization_cost(minutes, rate_entry) -> Money`. `cost.full_cost(grams, spool, minutes, kwh, tariff, amort) -> CostBreakdown(plastic, power, amort | None, total, currency)`, or `CurrencyMismatch(input_name)`.
- MCP:
  - `set_amortization(valid_from, printer_price, life_hours, upkeep_per_hour=0)`
  - `set_currency(code, symbol)`
  - `set_country(code)`, which also sets the timezone default and the UA tariff reminder flag

  New prices and rates are stored in the current currency.
- [ ] Step 1 tests:
  - `test_migrate_v8_to_v9_keeps_uah`: real v8 DB with rows.
  - `test_amortization_rate_and_history`: two entries, a print before and after.
  - `test_full_cost_breakdown`.
  - `test_mixed_currency_refused_names_input`.
  - `test_no_amortization_line_when_unset`.
  - `test_set_currency_then_new_spool_in_eur`.
  - `test_country_ua_keeps_reminder_other_disables`.
- [ ] Steps 2-4: RED, implement, GREEN.
- [ ] Step 5: commit `Money: currency, country and amortization`.

### Task 2: Cost lines everywhere

**Files:** Modify `src/bambu_mcp/messages.py` (one `render_cost(breakdown) -> list[str]`, replacing every hard-coded «грн»), `service.py` (the proposal, sliced summary, print confirmation and journal rows use `full_cost`), `printgate.py`. Test `tests/test_messages.py`, `tests/test_service.py`.

- Proposal: the cost from the manifest estimate (grams × the slot spool's price, minutes from the last slice if any, else no electricity or amortization line and «время неизвестно до нарезки»).
- [ ] Step 1 tests:
  - `test_render_cost_exact_lines`: the sample strings from the constraints.
  - `test_proposal_summary_and_confirmation_show_breakdown`.
  - `test_journal_row_stores_amort_and_currency`.
  - `test_no_hardcoded_grn_left`: a grep test over `src/` outside the formatter.
- [ ] Steps 2-4: RED, implement, GREEN.
- [ ] Step 5: commit `Cost: plastic, power and amortization in every message`.

### Task 3: Whitelist additions

**Files:** Modify `src/slice_agent/runner.py` (constants and `Overrides.parse`), `src/bambu_mcp/mcp_server.py` (allowed values come from the runner constants). Test `tests/slice_agent/test_runner.py`, `tests/test_mcp_server.py`.

**Interfaces:**
- `top_shell_layers` and `bottom_shell_layers`: int 0..20.
- `outer_wall_speed`: number 20..300.
- `seam_position`: one of `aligned`, `back`, `nearest`, `random`.
- `ironing_type`: one of `no ironing`, `top`, `topmost`.
- `wall_generator`: one of `classic`, `arachne`.
- `layer_height` stays per nozzle.

Before coding, check each key name and value spelling against a flattened P2S process preset. In a test, read the keys from the phase 2 output fixtures or a redacted process fixture. A spelling that differs in Studio 02.08 wins, and the brief's name is changed.
- [ ] Step 1 tests:
  - `test_new_overrides_parse_and_reject_out_of_range`.
  - `test_argv_for_new_keys`: the CLI form `--key-with-hyphens=value`.
  - `test_mcp_allowed_values_from_runner`.
- [ ] Steps 2-4: RED, implement, GREEN.
- [ ] Step 5: commit `Whitelist: shells, outer wall speed, seam, ironing, wall generator`.

### Task 4: Feature volumes from the slice-agent

**Files:** Modify `src/slice_agent/output.py` (parse `Metadata/plate_<n>.gcode` inside the output: `;TYPE:` markers and E deltas to mm³ by filament diameter, bounded by the existing member caps), `src/slice_agent/app.py` (the `/slice` 200 body gains `features` per plate), `src/bambu_mcp/slicer.py` (reads `features`, optional), `src/bambu_mcp/evaluation.py` (new: `strength_proxy(features, pattern) -> float | None`). Test `tests/slice_agent/test_output.py`, `tests/test_slicer.py`, `tests/test_evaluation.py`.

**Interfaces:**
- `features`: `{"outer_wall", "inner_wall", "top_surface", "bottom_surface", "sparse_infill", "internal_solid_infill", "support", "other"}`, in mm³ as floats.
- The parser handles relative and absolute E (`M83`/`M82`), and `G92 E0` resets.
- [ ] Step 1 tests:
  - `test_feature_volumes_from_small_gcode`: a hand-written G-code with known volumes.
  - `test_relative_and_absolute_e`.
  - `test_parse_stops_at_cap`.
  - `test_old_agent_without_features`.
  - `test_strength_proxy_k_values`.
- [ ] Steps 2-4: RED, implement, GREEN.
- [ ] Step 5: commit `Slice-agent: feature volumes per plate`.

### Task 5: Goals, base points and settings layers

**Files:** Modify `src/bambu_mcp/evaluation.py` (the goal table: per goal the label, the grid, the base point, the score and the limits, all in one table), `proposal.py` (`resolve_settings` with `goal_base` and `variant` layers), `manifest.py` (accept the `goal` and `goal_reason` hints), `store/db.py` (schema v11: `jobs.goal_json`, `jobs.variant_json`, `jobs.variants_json`), `service.py` (the proposal goal line and goal buttons `job:<id>:goal:<key>`), `messages.py`, `mcp_server.py` (`goal` on `propose_job` and `slice_job`). Test `tests/test_evaluation.py`, `tests/test_proposal.py`, `tests/test_service.py`, `tests/test_manifest.py`, `tests/test_mcp_server.py`.

**Interfaces:**
- `GOALS: dict[str, Goal]`.
- `Goal(key, label, base: dict, grid: list[dict], score, limits)`.
- `goal_base(primary, secondary) -> dict` and `limits_ok(goal, settings, checks) -> list[str]`.
- `resolve_settings(explicit, last_job, hints, named_set, *, goal_base=None, variant=None, checks, nozzle)`.
- Source-of-goal logic in one function `job_goal(job, manifest) -> (primary, secondary, source, reason)`.
- [ ] Step 1 tests:
  - `test_precedence_with_goal_and_variant`.
  - `test_goal_press_sets_base_point_no_slice`: the fake waker and slicer are untouched.
  - `test_goal_from_print_hints_with_reason`.
  - `test_fallback_is_balanced`.
  - `test_fit_and_watertight_limits`.
  - `test_kit_has_no_podobrat_button`.
  - `test_button_count_within_24`.
  - `test_manifest_accepts_goal_hints_and_rejects_unknown_goal`.
- [ ] Steps 2-4: RED, implement, GREEN.
- [ ] Step 5: commit `Goals: base points, settings layers, goal buttons`.

### Task 6: «Подобрать»: the evaluation run

**Files:** Modify `src/bambu_mcp/evaluation.py` (`candidates(goal, explicit, base, checks, nozzle) -> list[dict]`, at most 6 with the baseline first, explicit keys fixed; `score(variants, goal) -> ranking`), `service.py` (`evaluate(job_id, goal=None)`, the `job:<id>:eval` and `job:<id>:variant:<n>` buttons, busy guard, edit the proposal in place), `messages.py` (the table), `mcp_server.py` (`evaluate_job`, `choose_variant`). Test `tests/test_evaluation.py`, `tests/test_service.py`, `tests/test_mcp_server.py`.

**Interfaces:**
- Table lines: `«<n> [✓] <short settings> · <min> мин · <g> г · <total> <sym> · прочность <x.x>×»`, with at most 6 lines.
- «Вариант N» stores that variant as the `variant` layer.
- «Резать» reuses the variant's sliced file when the settings, the slot and the agent's file sha are unchanged, and slices again otherwise.
- [ ] Step 1 tests:
  - `test_evaluation_slices_baseline_then_variants_sequentially`.
  - `test_explicit_keys_fixed_in_every_variant`.
  - `test_winner_by_goal_score_and_limits`.
  - `test_secondary_goal_breaks_ties`.
  - `test_failed_variant_skipped_failed_baseline_restores`.
  - `test_all_variants_fail_baseline_only`.
  - `test_no_features_strength_unknown`.
  - `test_second_press_and_goal_press_during_run_refused`.
  - `test_choose_variant_then_slice_reuses_file`.
  - `test_evaluate_job_tool_returns_table`.
- [ ] Steps 2-4: RED, implement, GREEN.
- [ ] Step 5: commit `Evaluation: slice variants and pick the best for the goal`.

### Task 7: CAD Workshop: `goal` in `PRINT_HINTS` (repo cad-workshop, branch `print-goal`)

**Files:** Modify `cadlib/manifest.py` (`HINT_KEYS` gains `goal`, which is one key or a two-key list from the constraint, and `goal_reason`, a string of at most 200 characters), `.claude/skills/cad/SKILL.md`, `CLAUDE.md`, `multiclaude/addon/rules.md`, `README.md` (the hint table), `templates/part_model.py` (a commented example). Test `tests/test_manifest.py`, plus the fixture copy from bambu-mcp if it changes.

- Rules text: set `goal` from what the part does and say why in `goal_reason`. Cover load, heat, outdoor use, fits (threads, snaps, press fits give `fit`), liquids or seals (give `watertight`), visible decor (gives `quality`), and test or calibration pieces (give `speed` and `cheap`). The owner's words override.
- [ ] Step 1 tests:
  - `test_goal_hint_accepts_one_or_two_keys`.
  - `test_goal_hint_rejects_unknown_and_three_keys`.
  - `test_goal_reason_length`.
- [ ] Steps 2-4: RED, implement, GREEN.
- [ ] Step 5: commit `PRINT_HINTS: goal and goal_reason for bambu-mcp`.

### Task 8: Deploy and live check (controller, the owner present)

1. Back up `bambu.db`. Deploy the service and restart through `workshop-admin` (migrations v9 and v10). Restart the PC slice-agent task.
2. Set the owner's amortization (they give the printer price and life hours) and check the currency and country defaults.
3. `example-bracket`: press «Прочно», check the base point and the cost lines, then press «Подобрать». Expect 4-6 variants, a table and a winner, with the walls kept when said in words. Pick «Вариант 2», then «Резать». The file is reused.
4. Record the results in `docs/evaluation-notes.md`.
