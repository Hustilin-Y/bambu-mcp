<#
.SYNOPSIS
  Installs slice-agent on this PC: a scheduled task that starts it at logon as the current user
  (not elevated, no console window, restarted on failure) and a firewall rule that lets only
  workshop reach TCP 11436.
.DESCRIPTION
  Run from a normal (not elevated) Windows PowerShell, as the user who signs in to this PC:
    powershell -ExecutionPolicy Bypass -File .\deploy\slice-agent\install.ps1
  The firewall rule needs an administrator. Without one the script registers the task and prints
  the one-line elevated command that adds only the rule (-FirewallOnly).
  Safe to run again: every step prints whether it created, replaced or kept something.
  No token is ever passed to the task: slice-agent reads SLICE_AGENT_TOKEN and PC_AGENT_TOKEN
  from the user environment (deploy/slice-agent/README.md, "Tokens").
#>
param(
    [Parameter(Mandatory = $true)][string]$ServerAddress,  # the bambu-service host, the only one let in
    [int]$Port = 11436,
    [string]$TaskName = "bambu-mcp slice-agent",
    [switch]$FirewallOnly
)
$ErrorActionPreference = "Stop"
$RuleName = "bambu-mcp slice-agent"
$Arguments = "run slice-agent serve"

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-ElevatedCommand {
    $inner = "-NoExit -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -FirewallOnly " +
        "-ServerAddress $ServerAddress -Port $Port"
    return "Start-Process powershell.exe -Verb RunAs -ArgumentList '$inner'"
}

function Set-AgentFirewallRule {
    $rule = @(Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue)
    if ($rule.Count -eq 1) {
        $ports = $rule[0] | Get-NetFirewallPortFilter
        $addresses = $rule[0] | Get-NetFirewallAddressFilter
        $same = ($rule[0].Enabled -eq "True") -and ($rule[0].Direction -eq "Inbound") -and
            ($rule[0].Action -eq "Allow") -and ($ports.Protocol -eq "TCP") -and
            ("$($ports.LocalPort)" -eq "$Port") -and ("$($addresses.RemoteAddress)" -eq $ServerAddress)
        if ($same) {
            Write-Host "    kept: firewall rule '$RuleName' (TCP $Port from $ServerAddress)"
            return
        }
    }
    if ($rule.Count -gt 0) {
        $rule | Remove-NetFirewallRule
        Write-Host "    removed: $($rule.Count) old firewall rule(s) '$RuleName' that differed"
    }
    New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP `
        -LocalPort $Port -RemoteAddress $ServerAddress -Profile Any | Out-Null
    Write-Host "    created: firewall rule '$RuleName' (inbound TCP $Port from $ServerAddress only)"
}

function Write-PythonBlockWarning {
    # A program BLOCK rule (left by a cancelled "allow access" prompt) wins over the port rule.
    try {
        $blocking = @(Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue |
            Where-Object { $_.Program -like "*python*" } | Get-NetFirewallRule |
            Where-Object { $_.Action -eq "Block" -and $_.Direction -eq "Inbound" -and $_.Enabled -eq "True" })
    } catch {
        return
    }
    if ($blocking.Count -gt 0) {
        Write-Warning ("Inbound BLOCK rules for Python exist and win over the allow rule: " +
            (($blocking | ForEach-Object { $_.DisplayName }) -join "; ") +
            ". Disable them (elevated) if workshop cannot connect.")
    }
}

if ($FirewallOnly) {
    if (-not (Test-Admin)) {
        Write-Host "The firewall rule needs an administrator. Run this one line:"
        Write-Host "    $(Get-ElevatedCommand)"
        exit 1
    }
    Write-Host "Firewall: TCP $Port only from $ServerAddress"
    Set-AgentFirewallRule
    Write-PythonBlockWarning
    exit 0
}

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$user = "$env:USERDOMAIN\$env:USERNAME"

Write-Host "1/4 Checks"
if (-not (Test-Path (Join-Path $repo "pyproject.toml"))) {
    throw "no pyproject.toml in ${repo}, run the script from the bambu-mcp checkout"
}
$uv = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $uv) {
    throw "uv is not on PATH. Install uv first, then run this again"
}
$exe = Join-Path (Split-Path $uv.Source) "uvw.exe"
if (Test-Path $exe) {
    Write-Host "    uv: $exe (uvw: no console window)"
} else {
    $exe = $uv.Source
    Write-Warning "uvw.exe is not next to $($uv.Source): the task will show a console window"
}
$tokenMissing = $false
foreach ($name in @("SLICE_AGENT_TOKEN", "PC_AGENT_TOKEN")) {
    if ([Environment]::GetEnvironmentVariable($name, "User")) {
        Write-Host "    user variable $name is set"
    } else {
        Write-Warning "user variable $name is not set (README.md, `"Tokens`")"
        if ($name -eq "SLICE_AGENT_TOKEN") { $tokenMissing = $true }
    }
}
$configPath = Join-Path $env:LOCALAPPDATA "bambu-mcp\slice-agent.toml"
if (Test-Path $configPath) {
    Write-Host "    settings: $configPath"
} else {
    Write-Host "    settings: no $configPath, the defaults apply (see slice-agent.example.toml)"
}

Write-Host "2/4 Scheduled task '$TaskName' for $user (at logon, not elevated, restarts on failure)"
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$changed = $true
if ($existing) {
    $actions = @($existing.Actions)
    $triggers = @($existing.Triggers)
    $same = ($actions.Count -eq 1) -and ($actions[0].Execute -eq $exe) -and
        ($actions[0].Arguments -eq $Arguments) -and ($actions[0].WorkingDirectory -eq $repo) -and
        ($triggers.Count -eq 1) -and ($triggers[0].CimClass.CimClassName -eq "MSFT_TaskLogonTrigger") -and
        ("$($existing.Principal.RunLevel)" -eq "Limited") -and
        ("$($existing.Principal.UserId)" -like "*$env:USERNAME") -and
        ($existing.Settings.RestartCount -eq 999)
    if ($same) {
        $changed = $false
        Write-Host "    kept: the task already runs '$exe $Arguments' in $repo"
    }
}
if ($changed) {
    $action = New-ScheduledTaskAction -Execute $exe -Argument $Arguments -WorkingDirectory $repo
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Description "bambu-mcp slice-agent: slices print jobs with Bambu Studio" `
        -Force | Out-Null
    if ($existing) {
        Write-Host "    replaced: the task now runs '$exe $Arguments' in $repo"
    } else {
        Write-Host "    created: the task runs '$exe $Arguments' in $repo"
    }
}

Write-Host "3/4 Firewall: TCP $Port only from $ServerAddress"
if (Test-Admin) {
    Set-AgentFirewallRule
} else {
    Write-Host "    not changed: the rule needs an administrator. Run this one line:"
    Write-Host "    $(Get-ElevatedCommand)"
}
Write-PythonBlockWarning

Write-Host "4/4 Start"
$state = "$((Get-ScheduledTask -TaskName $TaskName).State)"
if ($state -eq "Running") {
    if ($changed) {
        Write-Host "    running with the old definition: restart it (README.md, `"Restart`")"
    } else {
        Write-Host "    kept: already running"
    }
} elseif ($tokenMissing) {
    Write-Host "    not started: SLICE_AGENT_TOKEN is not set, slice-agent would refuse to start"
} else {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "    started"
}
$healthy = $false
for ($i = 0; $i -lt 15 -and -not $tokenMissing; $i++) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
        if ($health.ok) { $healthy = $true; break }
    } catch {
        Start-Sleep -Seconds 2
    }
}
if ($healthy) {
    Write-Host "slice-agent answers on port $Port."
} elseif (-not $tokenMissing) {
    Write-Warning ("slice-agent did not answer on port $Port. Stop the task and run " +
        "'uv run slice-agent serve' in a terminal to see its log (README.md, `"Troubleshooting`").")
}
