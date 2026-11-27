# Phase 3 notes: live acceptance

Run on 2026-11-27 with the owner at the printer. Setup: P2S on firmware 01.03.00.00, USB stick in, bambu-service at the `phase3-print` head on workshop (schema v8), slice-agent on the PC with the pinned Bambu Studio 02.08.02.61. Job 1 was `example-bracket`, sliced from AMS 0 slot 1 (slot 2 for the owner, SUNLU PETG blue).

| Check | Result |
| --- | --- |
| Start from Telegram ("Печатать…", then "Стол чистый, печатать") | OK. Upload 127 519 bytes. `project_file` sent once. Started 1.7 s after the send. |
| FTPS `STOR /<name>` lands in the USB root | OK |
| TLS session reuse on the FTPS data socket | OK |
| `project_file` with `sequence_id "0"` and a lower-case md5 | Accepted |
| The P2S echoes our `task_id` in its reports | Yes. Started detection works on it. |
| Journal match of the started print | Job 1 plate 1, spool 2, 5.31 g booked |
| `pause_print` while RUNNING | Sent once. The printer went to PAUSE. |
| `request_stop` during PAUSE, then "Да, остановить" | Sent once. The printer went to FAILED at 0 %. The journal booked the printed share (0 g) and the spool was back to 1000 g. |
| Start from Claude Code (`prepare_print`, then the press in Telegram) | OK |
| Stop during PREPARE (heating and levelling) | Stop sent 8 s after the start. The printer stopped. |
| Expired confirmation (pressed after 10 min) | Pending: the owner presses the button after expiry |

## Changes from the run

- No fit question after a failed or stopped print (the owner asked). The spool question still comes for a print that is not tied to a job, so that the printed share can be booked.

## Still open

- The expiry check above.
- `PLA-S` as an AMS tray type in `load_spool` (not tried on the printer).
- A kit printed through the button, plate 2 or later (covered by tests only).
