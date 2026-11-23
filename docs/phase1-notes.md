# Phase 1 notes

Rulings made during the subagent-driven build of phase 1 and the review findings deferred to later. Kept here because the build ledger is deleted after the branch review.

## Rulings

- R1: Task 1 skips `git init` and keeps the existing .gitignore (extend it if needed) — already done by controller — none.
- R2: Commits need no per-task ask — owner approved local commits on phase1-monitor for the whole phase — none.
- R3: tls_mode "ca" = verify chain against the Bambu root bundle with check_hostname=False (paho passes the IP) plus an explicit peer CN == serial check right after connect, disconnect on mismatch — phase 0 verified this exact path — if wrong, TLS is weaker than intended, reviewer would see it.
- R4: Task 9 implements only the PyAV backend; `camera_backend` keeps only "pyav"; test_ffmpeg_argv_has_no_secret is dropped — phase 0 chose PyAV (+20 MB RSS, no argv exposure) — cost: an ffmpeg fallback added later if PyAV misbehaves on workshop.
- R5: Dev machine is Windows, CI and deploy are Linux: code and tests must be OS-neutral (pathlib, no shell). If Smart App Control blocks `.venv\Scripts\python.exe`, run tests with `uv run --no-project --with-requirements`-style fallback or WSL and say so in the report.
- Ruling R6: line endings LF everywhere; Task 3 implementer adds `.gitattributes` (`* text=auto eol=lf`) and renormalizes — CRLF rewrite of redact.py hides changes in review — cost: one extra commit
- Ruling R7: test_full_report_idle asserts FINISH (the real `pushall_idle` fixture is the idle printer after a finished print) plus an inline IDLE case — the plan assumed IDLE — none.
- Ruling R8: an empty AMS slot is a Tray with tray_type/color None (not skipped) — keeps slot numbering stable for slot mapping — consumers must treat None as empty.
- Ruling R9: implementers stage, controller commits (a subagent commit was blocked by the permission check; owner approved local commits to the controller) — none.
- Ruling R10: identity check inside the TLS handshake (before CONNECT carries the access code) accepted as stricter than R3; synthetic test CA/keys under tests/fixtures/synthetic/tls are test-only — none.
- Ruling R11: paused/resumed dedupe keys append |L<layer_num> so a second pause in the same print still notifies (plan key would suppress it forever) — deterministic across reconnects — cost: two pauses on one layer collapse into one notification.
- Ruling R12: finished/failed/cancelled messages show the print duration (render_event takes an optional duration_min, Task 11 passes it from the journal) in addition to the local end time — the owner wants how long it took, not only when — cost: one optional parameter.
- Ruling R13: Task 11 main() runs the service only, with a clearly marked place where Task 12 adds the MCP server to the same loop — Task 12 does not exist yet — none.
- Ruling R14: fit and spool questions are two messages (one message would exceed the add-in's 24-button limit); long dedupe keys hashed to SHA-256 (add-in id limit 128); current slot from raw tray_now, last valid value kept (finish report shows 255) — accepted as implemented.
- Ruling R15: Task 13 writes the deploy files and README only (steps 1-2); deploy (step 3) and the week-long acceptance (step 4) need the owner and stay open after the branch review — deploying changes workshop, outside this repo.
- Ruling R16: dedicated Linux user for the service parked — assistants have passwordless sudo on workshop, so a separate user would not protect the access code; spec section 16 states this threat model — cost: an assistant can read /srv/bambu/.env.
- Ruling R17: final fix wave accepted with: late start (one report) when FINISH/FAILED -> RUNNING keeps the same identity; 401 retried up to 24 h; 500 not_kept / 502 telegram_refused final; prints ending unseen get no cost/fit question — name and task_id arriving in two separate reports may still give two starts: check on real prints in acceptance — cost: a duplicate "started" message.
- R1: Task 1 skips `git init` and keeps the existing .gitignore (extend it if needed) — already done by controller — none.
- R2: Commits need no per-task ask — owner approved local commits on phase1-monitor for the whole phase — none.
- R3: tls_mode "ca" = verify chain against the Bambu root bundle with check_hostname=False (paho passes the IP) plus an explicit peer CN == serial check right after connect, disconnect on mismatch — phase 0 verified this exact path — if wrong, TLS is weaker than intended, reviewer would see it.
- R4: Task 9 implements only the PyAV backend; `camera_backend` keeps only "pyav"; test_ffmpeg_argv_has_no_secret is dropped — phase 0 chose PyAV (+20 MB RSS, no argv exposure) — cost: an ffmpeg fallback added later if PyAV misbehaves on workshop.
- R5: Dev machine is Windows, CI and deploy are Linux: code and tests must be OS-neutral (pathlib, no shell). If Smart App Control blocks `.venv\Scripts\python.exe`, run tests with `uv run --no-project --with-requirements`-style fallback or WSL and say so in the report.

## Deferred findings (minor, not fixed in phase 1)

- Task 2: minor (deferred): tls_mode "pin" accepted without a valid cert_sha256 (fail-open risk for Task 5)
- Task 2: minor (deferred): missing config.toml silently yields defaults
- Task 2: minor (deferred): port range / [::1] brackets / timezone not validated; non-table power_w raises AttributeError
- Task 2: minor (deferred): install_redaction not idempotent; extra= record fields not redacted; CLIENT_TOKEN names lowercased (case collisions)
- Task 2: minor (deferred): default listen port 8765 is a placeholder
- Task 2: minor (deferred): redact.py except branch not itself guarded (double fault if __repr__ raises)
- Task 2: minor (deferred): redact.py committed with CRLF line endings
- Task 3: minor (deferred): test_full_report_idle compares to fixture value instead of literal "FINISH"; test_idle_state_value not marked synthetic
- Task 3: minor (deferred): _str keeps "" (task_id/subtask_name "" vs None differ in identity) — carry to Task 6 dispatch
- Task 3: minor (deferred): non-numeric print_error -> 0; ams.ams [] wipes trays (untested)
- Task 4: minor (deferred): APPDATA preference untested; extractor trusts Studio JSON shape (null intro, short ecode)
- Task 4: minor (deferred): HmsTexts.load silent on missing file — Task 11 should log a warning once
- Task 5: minor (deferred): no real-paho-Client regression test through the handshake hook (pin mode relies on paho calling wrap_socket)
- Task 5: minor (deferred): stop()/_attempt race; _attempt catches only OSError; allowlist constants mutable; identity mismatch has no distinct status; test keys may trip secret scanners
- Task 6: minor (deferred): HMS dedupe-through-store not directly tested (reconnect case)
- Task 6: minor (deferred): identity may lag gcode_state flip (RUNNING before subtask_name updates) — check on real prints in acceptance
- Task 6: minor (deferred): seen_add before delivery: a failed send loses the event (Task 11 must account for it)
- Task 7: minor (deferred): history() LIKE escape test cannot fail; non-ASCII case folding; no concurrent-write stress test
- Task 7: minor (deferred): started_at/ended_at ISO text ordering — Task 11 must pass UTC-aware datetimes only
- Task 7: minor (deferred): migrate() no rollback; add_spool net_g<=0 / negative used_g unvalidated; schema.sql via Path(__file__) not importlib.resources
- Task 8: minor (deferred): missing PLA key KeyError message; zip without strict; global warned set; tzdata lower bound
- Task 9: minor (deferred): timeout test asserts nothing about secrets; first decoded frame may be partial mid-GOP
- Task 10: minor (deferred): spool_buttons no 24-button cap; POST retried on ReadTimeout may duplicate without dedupe (Task 11 should always pass dedupe for events); presses() retries 401 forever; duplicated backoff logic
- Task 12: minor (deferred): update_print cannot clear a spool / accepts inactive spools; bind failure not retried (Task 13 unit needs After=network-online.target); uvicorn crash not logged; clients must use IP (Host allowlist) — document in deploy README; serving-path tests thin; "Terminating session" INFO noise
- Task 12: minor (deferred): snapshot(wait=True) may start a grab with ~0 budget left; MCP grams without spool then _close sets spool_id without deduction
- Task 13: minor (deferred): Restart=on-failure does not cover a failed bind (README check covers it); claude mcp add scope unclear for the assistant; chown spelling
- Final: minor (deferred): INIT/OFFLINE/SLICING blip mid-print with same identity reads as "прервана" (treat only IDLE/different job as lost); empty-identity terminal opens a "без названия" row; SLICING/INIT not in _START_FROM; late-opened row after missed start gets duration 0; unseen-end journal failure not throttled; a retryable head blocks the FIFO up to 24 h; one throttle key for all link errors

## Open

- Deploy to workshop and the one-week acceptance (plan Task 13 steps 3-4), after the service-notify add-in is deployed.
