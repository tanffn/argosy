<#
Install Argosy's per-user backend watchdog task.

The action invokes start_backend_detached.ps1, which is idempotent. It fires at
logon and every five minutes while the user session is available, so a stopped
backend is recovered without waiting for another reboot. The task runs with the
current user's limited token; no broker action or trade approval is implied.

Usage:
  .\scripts\install_backend_logon_task.ps1
  .\scripts\install_backend_logon_task.ps1 -NoStart
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = "Argosy Backend Supervisor",
    [string]$HostAddr = "127.0.0.1",
    [int]$Port = 8000,
    [string]$LogStem = "uvicorn_service",
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Starter = Join-Path $PSScriptRoot "start_backend_detached.ps1"
$SilentStarter = Join-Path $PSScriptRoot "start_backend_silent.vbs"
if (-not (Test-Path -LiteralPath $Starter)) {
    throw "Missing backend starter: $Starter"
}
if (-not (Test-Path -LiteralPath $SilentStarter)) {
    throw "Missing windowless backend starter: $SilentStarter"
}

$WScript = (Get-Command wscript.exe -ErrorAction Stop).Source
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$ActionArguments = @(
    "//B",
    "//Nologo",
    ('"{0}"' -f $SilentStarter),
    ('"{0}"' -f $HostAddr),
    ('"{0}"' -f $Port),
    ('"{0}"' -f $LogStem)
) -join " "

$Action = New-ScheduledTaskAction `
    -Execute $WScript `
    -Argument $ActionArguments `
    -WorkingDirectory $ProjectRoot
$AtLogon = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
$Watchdog = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$Principal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUser `
    -LogonType Interactive `
    -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2)

if ($PSCmdlet.ShouldProcess($TaskName, "register per-user Argosy backend watchdog")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger @($AtLogon, $Watchdog) `
        -Principal $Principal `
        -Settings $Settings `
        -Description "Starts and health-checks the supervised Argosy API/scheduler." `
        -Force | Out-Null

    if (-not $NoStart) {
        Start-ScheduledTask -TaskName $TaskName
    }
    Write-Host "Registered '$TaskName' for $CurrentUser (logon + 5-minute watchdog)."
}
