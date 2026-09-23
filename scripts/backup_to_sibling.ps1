# Copy project source + runtime data to a sibling backup folder via robocopy.
# Usage from project root:
#   .\scripts\backup_to_sibling.ps1
#   .\scripts\backup_to_sibling.ps1 -Destination "D:\Projects\financial-advisor-backup"
#   .\scripts\backup_to_sibling.ps1 -DryRun
# Copy-only: destination-only recovery files are never purged.

[CmdletBinding()]
param(
    [string]$Destination = '',
    [switch]$Mirror,
    [switch]$DryRun,
    [switch]$Quiet
)

$Source = Split-Path -Parent $PSScriptRoot
$ErrorActionPreference = 'Stop'

if (-not $Destination) {
    $Destination = Join-Path (Split-Path -Parent $Source) "financial-advisor-backup"
}
$Source = [IO.Path]::GetFullPath($Source).TrimEnd('\')
$Destination = [IO.Path]::GetFullPath($Destination).TrimEnd('\')
if ($Destination -eq $Source -or $Destination.StartsWith($Source + '\', [StringComparison]::OrdinalIgnoreCase) -or
    $Source.StartsWith($Destination + '\', [StringComparison]::OrdinalIgnoreCase) -or
    $Destination -eq [IO.Path]::GetPathRoot($Destination).TrimEnd('\')) {
    throw 'Backup destination must be separate from the workspace, not its parent or a drive root'
}
if ($Mirror) { throw 'Mirror mode is disabled: this script preserves destination-only recovery files' }
function Assert-PlainAncestors([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            if ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Reparse point in backup path: $current"
            }
        }
        $current = Split-Path -Parent $current
    }
}
Assert-PlainAncestors $Source
Assert-PlainAncestors (Join-Path $Source 'db')
Assert-PlainAncestors $Destination
# Existing destination child junctions could redirect an otherwise safe copy.
# Walk explicitly, checking each entry before ever descending into it.
$pending = New-Object 'System.Collections.Generic.Queue[string]'
if (Test-Path -LiteralPath $Destination) { $pending.Enqueue($Destination) }
while ($pending.Count) {
    foreach ($entry in Get-ChildItem -LiteralPath $pending.Dequeue() -Force) {
        if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Destination contains a reparse point: $($entry.FullName)"
        }
        if ($entry.PSIsContainer) { $pending.Enqueue($entry.FullName) }
    }
}

$ExcludeDirs = @(
    '.git', '.venv', 'node_modules', '.next', '.worktrees',
    'graphify-out', 'tmp', '.claude', '.pytest_cache', '.ruff_cache',
    '.progress', '.idea', '.vscode', '__pycache__', '.turbo',
    '.mypy_cache', 'htmlcov', 'out',
    # `backups/` holds ~daily full-DB snapshots (~300 MB each, ~5.5 GB total).
    # Backing up backups into a sibling backup is redundant — the live
    # live data receives a separate verified online SQLite snapshot below.
    'backups', '.test-*', 'scratchpad', 'tmp_review', '.superpowers', 'codex-tandem',
    'logs', '.tmp.driveupload', '.tmp.drivedownload', '.stats', 'transcripts'
)
# Also skip loose DB snapshot copies in db/ (db/argosy.db.bak-*, .bak_* etc.);
# the live database is backed up through SQLite, never raw-copied.
$ExcludeFiles = @('*.bak', '*.bak-*', '*.bak_*', '*.pyc', '*.pyo', '*.swp', '*.swo', 'result.md',
    'argosy.db', 'argosy.db-wal', 'argosy.db-shm', 'argosy.db-journal',
    'argosy-consistent.db', 'argosy-consistent.db-wal', 'argosy-consistent.db-shm', 'argosy-consistent.db-journal',
    'argosy-consistent.db.gz',
    'argosy.db.SAFETY_*', 'argosy_before_*.db')

$args = @($Source, $Destination, '/E', '/COPY:DAT', '/R:1', '/W:5', '/XJ')
$args += '/XD'; $args += $ExcludeDirs
$args += '/XF'; $args += $ExcludeFiles
$args += '/NP'
if ($Quiet)  { $args += '/NFL'; $args += '/NDL'; $args += '/NJH' }
if ($DryRun) { $args += '/L' }

Write-Host "Source:      $Source"
Write-Host "Destination: $Destination"
if ($DryRun) { Write-Host "Mode:        DRY-RUN (no files will be copied)" -ForegroundColor Cyan }
Write-Host ""

if (-not $DryRun) {
    # Snapshot before copying evidence: subsequently committed records cannot
    # introduce new references into this database after evidence was copied.
    & (Join-Path $Source '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'backup_sqlite.py') `
        (Join-Path $Source 'db\argosy.db') (Join-Path $Destination 'db\argosy-consistent.db.gz')
    if ($LASTEXITCODE -ne 0) { throw 'Database snapshot failed; backup is incomplete' }
}
& robocopy @args
$ec = $LASTEXITCODE

Write-Host ""
Write-Host "robocopy exit code: $ec"
# Robocopy: 0-7 = success variants, 8+ = failure
if ($ec -lt 8) {
    if (-not $DryRun) {
        & (Join-Path $Source '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'copy_transcripts.py') `
            (Join-Path $Source 'transcripts') (Join-Path $Destination 'transcripts')
        if ($LASTEXITCODE -ne 0) { throw 'Transcript copy failed; backup is incomplete' }
    }
    Write-Host "SUCCESS" -ForegroundColor Green
    exit 0
} else {
    Write-Host "FAILURE" -ForegroundColor Red
    exit $ec
}
