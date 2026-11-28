# slice-agent on the PC

slice-agent slices print jobs for bambu-service with the Bambu Studio CLI on the owner's Windows PC. It runs as the owner's standard user (not elevated), started at logon by a scheduled task, and listens on `0.0.0.0:11436`. A firewall rule lets only the server (`-ServerAddress`, `192.0.2.10` in these examples) reach the port, and every endpoint but `/health` needs the bearer token.

On a Linux server instead of a Windows PC, see [../slicing-options.md](../slicing-options.md).

| What | Where |
| --- | --- |
| Code and venv | this repo checkout, `uv run` from its root |
| Settings (optional) | `%LOCALAPPDATA%\bambu-mcp\slice-agent.toml`, from `slice-agent.example.toml` |
| Pinned Bambu Studio | `%LOCALAPPDATA%\bambu-mcp\studio-<version>\` |
| Flattened presets (AGPL data, never in the repo) | `%LOCALAPPDATA%\bambu-mcp\profiles\` |
| Sliced files | `%USERPROFILE%\bambu-mcp\sliced\` |
| Tokens | user environment variables `SLICE_AGENT_TOKEN`, `PC_AGENT_TOKEN` |
| Task / firewall rule | `bambu-mcp slice-agent` (both) |

Prerequisites: uv, Bambu Studio installed and run once (so `%APPDATA%\BambuStudio\system\BBL` holds the downloaded system presets), and pc-agent from llm-orchestra (slice-agent holds a pc-agent lease during a slice so the PC does not sleep under the slicer).

Run the commands below from the repo root in Windows PowerShell, as the owner, not elevated, unless a step says otherwise.

## 1. Pinned Bambu Studio

The installed Studio updates itself, and a new version can change times and grams. slice-agent therefore runs its own copy, upgraded only on purpose.

1. Close Bambu Studio (a running Studio can hold files open mid-copy).
2. Read the version (or take it from Help > About):

   ```
   (Get-Item "C:\Program Files\Bambu Studio\bambu-studio.exe").VersionInfo.ProductVersion
   ```

3. Copy the install (robocopy exit codes 0 to 7 mean success):

   ```
   $v = "<version>"
   robocopy "C:\Program Files\Bambu Studio" "$env:LOCALAPPDATA\bambu-mcp\studio-$v" /E
   ```

4. Point `studio_exe` and `studio_resources` at the copy in `slice-agent.toml` (section 2) and write the version down next to them.

The copy pins the CLI only. The presets are pinned separately, at the moment you run `slice-agent flatten` (section 3): the slicer reads nothing but the flattened files, while the system presets under `%APPDATA%\BambuStudio` keep changing whenever the installed Studio downloads new ones.

### Upgrading Studio: the golden test

Keep the old `studio-<old version>` folder until the new one passes.

1. Before the upgrade, note the minutes and grams of a slice of `example-bracket` (cad-workshop, `build parts/example-bracket --print-ready`, then `bambu-mcp upload`, then `slice_job`, the numbers are in `job_status`).
2. Copy the new version as in steps 1 to 3 above, switch both paths in `slice-agent.toml`, restart the task (section 6).
3. Re-slice the same job with the same settings (`slice_job` on that job id).
4. Minutes and grams must each be within 3 % of the old result. If not, switch the paths back, restart, and investigate before upgrading.

Re-running `slice-agent flatten` after a preset update is the same kind of change: do it on purpose and repeat the golden test.

## 2. Settings: `%LOCALAPPDATA%\bambu-mcp\slice-agent.toml`

Start from `slice-agent.example.toml`. Without the file every key takes its default. The agent reads it once at start, so restart the task after a change.

| Key | Default | Notes |
| --- | --- | --- |
| `studio_exe` | `C:\Program Files\Bambu Studio\bambu-studio.exe` | set to the pinned copy |
| `studio_resources` | `C:\Program Files\Bambu Studio\resources\profiles\BBL` | set to the pinned copy's `resources\profiles\BBL` |
| `studio_data` | `%APPDATA%\BambuStudio` | where the downloaded system presets live |
| `profiles_dir` | `%LOCALAPPDATA%\bambu-mcp\profiles` | flatten output, slicer input |
| `sliced_dir` | `%USERPROFILE%\bambu-mcp\sliced` | output, plus `.incoming` and `.work-*` scratch folders |
| `listen` | `["0.0.0.0", 11436]` | `[host, port]` |
| `filaments` | `["Generic PETG @BBL P2S"]` | filament presets to flatten |
| `[machine]` | `"0.4" = "Bambu Lab P2S 0.4 nozzle"` | nozzle -> machine preset |
| `[process_sets]` | `standard = "0.20mm Standard @BBL P2S"` | name -> process preset |

TOML does not expand `%VARIABLES%`: write absolute paths, in single quotes so backslashes stay literal. Unknown keys are ignored without a warning, so check the spelling.

Every preset name must be a Bambu **system** preset name. User presets (`%APPDATA%\BambuStudio\user\...`) are not supported: flattening marks the result as inheriting a system preset of the same name.

The service always asks for the `standard` process set: a named set from the conversation travels as overrides of the whitelisted settings, so the PC needs no other entry in `[process_sets]`. The service sends the filament preset recorded on the spool in the chosen AMS slot (`filament_preset` of the spool), and the agent refuses any name that is not in `filaments`. Keep the two lists in step.

## 3. Flatten the presets

```
uv run slice-agent flatten
```

It resolves each preset from `studio_data` and `studio_resources` and writes `machine-<nozzle>.json`, `process-<set>.json` and `filament-<slug>.json` into `profiles_dir`. It checks the machine (bed 256 x 256, height 256, nozzle as configured) and prints `assertions OK`. Then one line per filament:

```
filament Generic PETG @BBL P2S: tray_info_idx=<id> nozzle_temp_min=<C> nozzle_temp_max=<C> material=PETG
```

Copy these values into the spool records on the server (`add_spool` or `update_spool`): `load_spool` needs them. A `?` means the preset lacks that key. Run flatten again after changing `machine`, `process_sets` or `filaments`, then restart the task. The output is AGPL data: it stays outside the repo.

## 4. Tokens

slice-agent reads two tokens from its environment, never from the TOML file or the command line:

- `SLICE_AGENT_TOKEN`: the bearer token bambu-service sends. Without it `serve` refuses to start (exit 2). The same value goes into `/srv/bambu/.env` on workshop as `SLICE_AGENT_TOKEN` (deploy/README.md).
- `PC_AGENT_TOKEN`: pc-agent's token, the `token` value in `%LOCALAPPDATA%\pc-agent\config.toml`. Without it the agent still slices but warns that the PC may fall asleep mid-slice.

The scheduled task inherits them from the owner's user environment variables. Set them so that no value lands in argv, the console or the PSReadLine history:

```
$b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); $t = -join ($b | ForEach-Object { $_.ToString("x2") }); [Environment]::SetEnvironmentVariable("SLICE_AGENT_TOKEN", $t, "User"); Set-Clipboard -Value $t; Remove-Variable b, t
```

That generates the token, stores it and puts it on the clipboard: paste it into `nano /srv/bambu/.env` on workshop, then clear the clipboard (`Set-Clipboard -Value " "`) and, if Windows clipboard history is on, delete the entry there (Win+V).

```
$t = (Select-String -Path "$env:LOCALAPPDATA\pc-agent\config.toml" -Pattern '^token\s*=\s*"(.+)"').Matches[0].Groups[1].Value; [Environment]::SetEnvironmentVariable("PC_AGENT_TOKEN", $t, "User"); Remove-Variable t
```

Then sign out and back in, so the task starts with the new environment. Never use `setx NAME value` or a literal token in a command: both put the value in argv and in the history (to clean up after a slip, run `Clear-History` and remove the line from `(Get-PSReadLineOption).HistorySavePath`). User environment variables are readable by every process of the owner's account: that is the accepted trade-off, as for pc-agent's own config file.

Rotating `SLICE_AGENT_TOKEN`: set the new value on both sides, restart the task and the bambu-service unit.

## 5. Install

```
powershell -ExecutionPolicy Bypass -File .\deploy\slice-agent\install.ps1 -ServerAddress <server IP>
```

Run it as the owner, **not** elevated: the task is registered for the user who runs the script. It prints every change and is safe to run again.

1. Checks: the repo root, `uv` on PATH, whether both user variables are set (never their values), whether `slice-agent.toml` exists.
2. Scheduled task `bambu-mcp slice-agent`: at logon of this user, run level Limited, no time limit, restart every minute on failure, one instance. The action is `uvw.exe run slice-agent serve` (uvw is uv without a console window) in the repo root. It holds no token. The task is kept when it already matches, replaced otherwise.
3. Firewall rule `bambu-mcp slice-agent`: inbound TCP 11436 from `-ServerAddress` only, any profile. This needs an administrator: from a normal shell the script leaves the rule alone and prints the exact one-line `Start-Process ... -Verb RunAs ... -FirewallOnly` command to run. It also warns about inbound BLOCK rules for Python (a cancelled "allow access" prompt leaves them, and they win over the allow rule).
4. Starts the task unless it is already running or `SLICE_AGENT_TOKEN` is missing, then polls `http://127.0.0.1:11436/health` for up to about 30 s.

The script re-resolves the uv path each run: run it again after uv moves.

Uninstall: `Unregister-ScheduledTask -TaskName "bambu-mcp slice-agent"`, and elevated `Remove-NetFirewallRule -DisplayName "bambu-mcp slice-agent"`.

## 6. Running it

Waking: bambu-service sends `POST /wake` to orchestra-gateway, then polls slice-agent's `/health` for up to 120 s. The task starts at **logon**: after sleep or hibernation the owner's session is still there and the agent answers. After a reboot or power-on to the sign-in screen nothing runs until the owner signs in, and the slice fails with "the PC did not wake within 2 minutes".

One slice at a time: a second request gets 409 `busy`, and the service retries once. The Studio CLI gets 300 s, then the agent kills its process tree.

Output: `<name>-<job id>-p1.gcode.3mf` for a part, `<name>-<job id>-kit.gcode.3mf` for a kit (one file with all plates). Open it in Bambu Studio and print from there. Do **not** rename it: the service matches the print to the job by this name (Studio sends it as the print's subtask name, without `.gcode.3mf`). A re-slice overwrites the file of the same name.

Restart (picks up a new token, config or code):

```
Stop-ScheduledTask -TaskName "bambu-mcp slice-agent"
Get-NetTCPConnection -LocalPort 11436 -State Listen -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName "bambu-mcp slice-agent"
```

If the second line still shows a listener after the stop, a child process survived: stop that `OwningProcess` before starting the task again.

Check from workshop. Keep the token out of argv: create a mode-600 header file once, typing the token (`install -m 600 /dev/null /srv/bambu/.curl-slice-auth`, then `nano` it to one line `Authorization: Bearer <SLICE_AGENT_TOKEN>`), and delete it afterwards:

```
curl -s http://<PC address>:11436/health
curl -s -o /dev/null -w '%{http_code}\n' -H @/srv/bambu/.curl-slice-auth http://<PC address>:11436/sliced/x
```

The first answers `{"ok":true}`. The second answers 404 with the right token and 401 without one. A timeout means the firewall rule (or a Python block rule) is in the way.

## Troubleshooting

The task runs without a window and slice-agent logs only to its console, so the task leaves no log file. To watch a slice, stop the task (section 6) and run the agent in a terminal:

```
uv run slice-agent serve
```

Smart App Control can block the unsigned `python.exe` in a uv venv (error 4551), at random. Always start the agent through `uv run` (the task does). If it is blocked anyway, see the PC notes for the workaround.

The agent never returns PC paths to the service: CLI errors are passed through with the Studio, profile, sliced and home folders replaced by placeholders such as `<sliced>`.
