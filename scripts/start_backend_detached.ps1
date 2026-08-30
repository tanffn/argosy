<#
Start the Argosy backend under the auto-restart supervisor (detached).

Preserves Start-Process semantics from the handovers: the supervisor survives
session cleanup; uvicorn is a supervised child. Logs land in tmp/.

Usage (PowerShell):
  .\scripts\start_backend_detached.ps1
  .\scripts\start_backend_detached.ps1 -Port 8000 -LogStem uvicorn_detached

(param() must be the first statement — a leading string literal is an
expression and breaks the param block; keep this header a comment.)
#>
param(
    [string]$HostAddr = "127.0.0.1",
    [int]$Port = 8000,
    [string]$LogStem = "uvicorn_service",
    [double]$RestartDelay = 5.0
)

$Root = Split-Path -Parent $PSScriptRoot
if (-not $env:ARGOSY_HOME) { $env:ARGOSY_HOME = $Root }

# Idempotency guard: a second supervisor must never stack on a running one
# (observed 2026-07-13: two full supervisor+uvicorn stacks fighting over the
# port). Also makes the logon-startup registration safe to fire when the
# backend was already started by hand. A live supervisor owns recovery even
# while its child is briefly between restarts; starting another during that
# gap creates two schedulers that race the same jobs and database.
#
# Match is anchored to THIS repo's run_backend_service.py — a foreign
# checkout's supervisor must not satisfy the guard. The port probe only
# distinguishes "serving" from "recovering" for diagnostics; neither state
# permits a second supervisor.
$SupervisorScript = Join-Path $Root "scripts\run_backend_service.py"
$SupervisorScriptNorm = [System.IO.Path]::GetFullPath($SupervisorScript)
$existing = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object {
        $_.CommandLine -and
        $_.CommandLine -match 'run_backend_service\.py' -and
        $_.CommandLine.IndexOf($SupervisorScriptNorm, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    })
$portBusy = $false
try {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($listener) { $portBusy = $true }
} catch {
    $probe = [System.Net.Sockets.TcpClient]::new()
    try {
        $portBusy = $probe.ConnectAsync($HostAddr, $Port).Wait(1000) -and $probe.Connected
    } finally {
        $probe.Dispose()
    }
}
if ($existing.Count -gt 0) {
    if ($portBusy) {
        Write-Host "Backend supervisor already running (PID $($existing[0].ProcessId)) on port $Port - nothing to do."
    } else {
        Write-Host "Backend supervisor already running (PID $($existing[0].ProcessId)); child is recovering on port $Port - nothing to do."
    }
    exit 0
} elseif ($portBusy) {
    # A manual/foreign listener must not make the supervisor crash-loop and
    # permanently give up. The recurring logon task retries after that listener
    # exits, at which point this script starts the supervised stack.
    Write-Error "Port $Port is already occupied by a non-supervised process; refusing to stack another backend."
    exit 2
}

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "Missing venv python at $Python"
    exit 1
}

$Tmp = Join-Path $Root "tmp"
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null

$SupervisorOut = Join-Path $Tmp "$LogStem.supervisor.log"
$SupervisorErr = Join-Path $Tmp "$LogStem.supervisor.err.log"

$Args = @(
    (Join-Path $Root "scripts\run_backend_service.py"),
    "--host", $HostAddr,
    "--port", "$Port",
    "--log-stem", $LogStem,
    "--restart-delay", "$RestartDelay"
)

Write-Host "Starting detached backend supervisor → $SupervisorOut"
# Some managed launchers inject both ``Path`` and ``PATH``. Windows treats
# names case-insensitively, but PowerShell 5's Start-Process builds a
# case-insensitive dictionary and throws on the duplicate before spawning.
# Preserve the effective value and normalize the process environment to one
# canonical key.
$PathKeys = @([Environment]::GetEnvironmentVariables('Process').Keys |
    Where-Object { $_.ToString().Equals('path', [StringComparison]::OrdinalIgnoreCase) })
if ($PathKeys.Count -gt 1) {
    $EffectivePath = $env:Path
    foreach ($PathKey in $PathKeys) {
        [Environment]::SetEnvironmentVariable($PathKey.ToString(), $null, 'Process')
    }
    [Environment]::SetEnvironmentVariable('Path', $EffectivePath, 'Process')
}
$proc = Start-Process -FilePath $Python `
    -ArgumentList $Args `
    -WorkingDirectory $Root `
    -RedirectStandardOutput $SupervisorOut `
    -RedirectStandardError $SupervisorErr `
    -WindowStyle Hidden `
    -PassThru

Write-Host "Supervisor PID=$($proc.Id) (child uvicorn auto-restarts on crash)"
