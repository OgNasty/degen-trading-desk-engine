# Phase 2: registers hidden Degen scheduled tasks for the current user. Set config.json run_side="pc" first; runner interval comes from schedule.check_interval_sec_pc (default 60 s).
param([switch]$Interactive)
$ErrorActionPreference = 'Stop'
$bot = Split-Path -Parent $PSScriptRoot
$pc  = Join-Path $bot 'pc'
$user = "$env:USERDOMAIN\$env:USERNAME"
$logon = if ($Interactive) { 'Interactive' } else { 'S4U' }
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType $logon -RunLevel Limited
$vbs = Join-Path $pc 'hidden.vbs'
# Runs the .cmd hidden (no console window) and waits, so the task shows Running while the job runs.
function Mk($cmdFile, $extra) { $a = '//B //Nologo "' + $vbs + '" "' + (Join-Path $pc $cmdFile) + '"'; if ($extra) { $a += ' ' + $extra }; New-ScheduledTaskAction -Execute 'wscript.exe' -Argument $a -WorkingDirectory $bot }
# Runner supervisor: at logon + every 5 min (revives it if the supervisor itself dies); one instance only.
$s1 = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$t1 = @( (New-ScheduledTaskTrigger -AtLogOn -User $user),
         (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)) )
if (-not $Interactive) { $t1 += New-ScheduledTaskTrigger -AtStartup }  # startup trigger needs S4U/admin
Register-ScheduledTask -TaskName 'Degen Runner' -Action (Mk 'run_runner.cmd' $null) -Trigger $t1 -Principal $principal -Settings $s1 -Force | Out-Null
# Morning balance daily 6:59 AM and 4:59 PM, one task with two daily triggers, same script, no flags, (buyside1012; daily_rebalance_v2 runs once per slot) and Friday rebalance Fri 9:01 PM (fri9pm 2026-10-11; was 8:23 AM) (PC local time = America/Phoenix).
# v1.0.0: per-install jitter (config.json "jitter", made by jitter_setup.py) shifts both times by a few minutes.
$py = Join-Path $bot '.venv\Scripts\python.exe'; if (-not (Test-Path $py)) { $py = 'python' }
$sch = (& $py (Join-Path $bot 'jitter_setup.py') | Out-String) | ConvertFrom-Json
$amAt = [datetime]::ParseExact($sch.morning_am, 'HH:mm', $null); $pmAt = [datetime]::ParseExact($sch.morning_pm, 'HH:mm', $null); $friAt = [datetime]::ParseExact($sch.friday_rsi, 'HH:mm', $null)
$s2 = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName 'Degen Morning Balance' -Action (Mk 'run_job.cmd' 'morning_balance_run.py') -Trigger @((New-ScheduledTaskTrigger -Daily -At $amAt), (New-ScheduledTaskTrigger -Daily -At $pmAt)) -Principal $principal -Settings $s2 -Force | Out-Null
Register-ScheduledTask -TaskName 'Degen Friday Rebalance' -Action (Mk 'run_job.cmd' 'friday_rsi.py') -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday -At $friAt) -Principal $principal -Settings $s2 -Force | Out-Null
Get-ScheduledTask -TaskName 'Degen*' | ForEach-Object { "{0} | {1} | {2}" -f $_.TaskName, $_.State, $_.Principal.LogonType }
