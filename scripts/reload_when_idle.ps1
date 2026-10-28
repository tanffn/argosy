<# Deploy completed local changes without interrupting ongoing research.
   Run hidden. The existing reload helper rechecks ownership and busy chat.
   This only waits/reloads; it never cancels jobs or clears failure receipts. #>
param([int]$MaxWaitMinutes = 120)
$ErrorActionPreference = 'Stop'
if ($MaxWaitMinutes -lt 1 -or $MaxWaitMinutes -gt 240) { throw 'Invalid wait budget' }
$ArgosyRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$ArgosyPython = Join-Path $ArgosyRoot '.venv\Scripts\python.exe'
$ArgosyDeadline = [DateTime]::UtcNow.AddMinutes($MaxWaitMinutes)
Push-Location $ArgosyRoot
try {
    do {
        & $ArgosyPython -c "import sqlite3,sys; c=sqlite3.connect('file:db/argosy.db?mode=ro',uri=True); busy=c.execute('select job_name from job_runs where status=? and job_name<>?',('running','discord_advisor')).fetchall(); chat=c.execute('select 1 from chat_turns where status in (?,?) limit 1',('queued','delivery_pending')).fetchone() or c.execute('select 1 from chat_analysis_requests where status in (?,?,?) limit 1',('queued','running','cancelling')).fetchone(); print('Waiting for active jobs:',busy,'Busy chat:',bool(chat)); sys.exit(bool(busy or chat))"
        if ($LASTEXITCODE -eq 0) {
            & (Join-Path $PSScriptRoot 'reload_decision_operations.ps1') -AllowIdleDiscord
            if ($LASTEXITCODE -ne 0) { throw 'Idle reload did not complete' }
            Write-Output ('DEPLOYED ' + [DateTime]::UtcNow.ToString('o'))
            exit 0
        }
        Start-Sleep -Seconds 30
    } while ([DateTime]::UtcNow -lt $ArgosyDeadline)
    throw 'Deployment wait expired; existing work was not interrupted. Check active jobs before retrying.'
} finally {
    Pop-Location
}
