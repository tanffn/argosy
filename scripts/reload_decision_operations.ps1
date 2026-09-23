<# Reload this checkout's supervised backend and start its local UI if absent.
   Refuses to interrupt active research. Never opens a console window. #>
param([switch]$AllowIdleDiscord)
$ErrorActionPreference = 'Stop'
$ArgosyRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$ArgosyPython = Join-Path $ArgosyRoot '.venv\Scripts\python.exe'
$ArgosySupervisor = Join-Path $ArgosyRoot 'scripts\run_backend_service.py'
$BackendListener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($BackendListener) {
    $BackendProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($BackendListener[0].OwningProcess)"
    if ($BackendProcess.CommandLine -notmatch 'argosy\.api\.main:create_app') { throw 'Port 8000 is not Argosy' }
    $Ancestor = $BackendProcess
    $VerifiedSupervisor = $false
    for ($Step = 0; $Step -lt 4; $Step++) {
        $Ancestor = Get-CimInstance Win32_Process -Filter "ProcessId=$($Ancestor.ParentProcessId)"
        if (-not $Ancestor) { break }
        if ($Ancestor.CommandLine -and $Ancestor.CommandLine.Contains($ArgosySupervisor)) {
            $VerifiedSupervisor = $true
            break
        }
    }
    if (-not $VerifiedSupervisor) { throw 'Backend is not owned by this checkout supervisor' }
    Push-Location $ArgosyRoot
    try {
        & $ArgosyPython -c "import sqlite3,sys; c=sqlite3.connect('file:db/argosy.db?mode=ro',uri=True); allow=sys.argv[1]=='True'; rows=c.execute('select job_name from job_runs where status=?',('running',)).fetchall(); print('Active jobs:',rows); blockers=[r for r in rows if not (allow and r[0]=='discord_advisor')]; tables={r[0] for r in c.execute('select name from sqlite_master where type=?',('table',))}; busy=allow and ('chat_turns' not in tables or 'chat_analysis_requests' not in tables or c.execute('select 1 from chat_turns where status in (?,?) limit 1',('queued','delivery_pending')).fetchone() or c.execute('select 1 from chat_analysis_requests where status in (?,?,?) limit 1',('queued','running','cancelling')).fetchone()); print('Idle Discord reload allowed:',allow,'Busy chat:',bool(busy)); sys.exit(bool(blockers or busy))" $AllowIdleDiscord.IsPresent
        if ($LASTEXITCODE -ne 0) { throw 'Jobs are active; retry after they finish' }
    } finally { Pop-Location }
    Stop-Process -Id $BackendProcess.ProcessId
    Write-Output 'Backend child stopped; existing hidden supervisor owns restart.'
} else {
    & (Join-Path $PSScriptRoot 'start_backend_detached.ps1')
}
# Wait for startup (including orphan cleanup) before the caller can open an
# operator job. Returning immediately raced cleanup, which marked that new
# job cancelled even though its separate operator process was still working.
$ArgosyReadyDeadline = [DateTime]::UtcNow.AddSeconds(40)
$ArgosyBackendReady = $false
do {
    try {
        $ArgosyHealth = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/health' -UseBasicParsing -TimeoutSec 2
        $ArgosyBackendReady = $ArgosyHealth.StatusCode -eq 200
    } catch {
        $ArgosyBackendReady = $false
    }
    if (-not $ArgosyBackendReady) { Start-Sleep -Milliseconds 500 }
} while (-not $ArgosyBackendReady -and [DateTime]::UtcNow -lt $ArgosyReadyDeadline)
if (-not $ArgosyBackendReady) { throw 'Backend did not become HTTP-ready within 40 seconds; do not start operator jobs yet' }
Write-Output 'Backend HTTP-ready; startup cleanup completed.'
$UiListener = Get-NetTCPConnection -LocalPort 1337 -State Listen -ErrorAction SilentlyContinue
if (-not $UiListener) {
    $ArgosyNode = (Get-Command node.exe).Source
    Start-Process -FilePath $ArgosyNode -ArgumentList @('node_modules/next/dist/bin/next', 'dev', '--hostname', '127.0.0.1', '-p', '1337') -WorkingDirectory (Join-Path $ArgosyRoot 'ui') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $ArgosyRoot 'tmp/ui_operations.log') -RedirectStandardError (Join-Path $ArgosyRoot 'tmp/ui_operations.err.log') | Out-Null
    Write-Output 'Local UI started hidden on 127.0.0.1:1337.'
}
