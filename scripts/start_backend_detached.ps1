"""Start the Argosy backend under the auto-restart supervisor (detached).

Preserves Start-Process semantics from the handovers: the supervisor survives
session cleanup; uvicorn is a supervised child. Logs land in tmp/.

Usage (PowerShell):
  .\scripts\start_backend_detached.ps1
  .\scripts\start_backend_detached.ps1 -Port 8000 -LogStem uvicorn_detached
"""
param(
    [string]$HostAddr = "127.0.0.1",
    [int]$Port = 8000,
    [string]$LogStem = "uvicorn_service",
    [double]$RestartDelay = 5.0
)

$Root = Split-Path -Parent $PSScriptRoot
if (-not $env:ARGOSY_HOME) { $env:ARGOSY_HOME = $Root }
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
