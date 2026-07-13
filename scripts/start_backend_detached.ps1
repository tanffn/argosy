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
# backend was already started by hand.
$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'run_backend_service\.py' }
if ($existing) {
    Write-Host "Backend supervisor already running (PID $($existing[0].ProcessId)) - nothing to do."
    exit 0
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
$proc = Start-Process -FilePath $Python `
    -ArgumentList $Args `
    -WorkingDirectory $Root `
    -RedirectStandardOutput $SupervisorOut `
    -RedirectStandardError $SupervisorErr `
    -WindowStyle Hidden `
    -PassThru

Write-Host "Supervisor PID=$($proc.Id) (child uvicorn auto-restarts on crash)"
