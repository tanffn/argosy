# Copy project source + runtime data to a sibling backup folder via robocopy.
# Usage from project root:
#   .\scripts\backup_to_sibling.ps1
#   .\scripts\backup_to_sibling.ps1 -Destination "D:\Projects\financial-advisor-backup"
#   .\scripts\backup_to_sibling.ps1 -DryRun
#   .\scripts\backup_to_sibling.ps1 -Mirror     # also deletes orphans in dest

[CmdletBinding()]
param(
    [string]$Destination = (Join-Path (Split-Path -Parent $PSScriptRoot) "..\financial-advisor-backup" | Resolve-Path -ErrorAction SilentlyContinue),
    [switch]$Mirror,
    [switch]$DryRun,
    [switch]$Quiet
)

$Source = Split-Path -Parent $PSScriptRoot

if (-not $Destination) {
    $Destination = Join-Path (Split-Path -Parent $Source) "financial-advisor-backup"
}

$ExcludeDirs = @(
    '.git', '.venv', 'node_modules', '.next', '.worktrees',
    'graphify-out', 'tmp', '.claude', '.pytest_cache', '.ruff_cache',
    '.progress', '.idea', '.vscode', '__pycache__', '.turbo',
    '.mypy_cache', 'htmlcov', 'out'
)
$ExcludeFiles = @('*.bak', '*.pyc', '*.pyo', '*.swp', '*.swo', 'result.md')

$args = @($Source, $Destination, '/E', '/COPY:DAT', '/R:1', '/W:5')
$args += '/XD'; $args += $ExcludeDirs
$args += '/XF'; $args += $ExcludeFiles
$args += '/NP'
if ($Quiet)  { $args += '/NFL'; $args += '/NDL'; $args += '/NJH' }
if ($Mirror) { $args += '/PURGE' }
if ($DryRun) { $args += '/L' }

Write-Host "Source:      $Source"
Write-Host "Destination: $Destination"
if ($Mirror) { Write-Host "Mode:        MIRROR (orphans in dest will be deleted)" -ForegroundColor Yellow }
if ($DryRun) { Write-Host "Mode:        DRY-RUN (no files will be copied)" -ForegroundColor Cyan }
Write-Host ""

& robocopy @args
$ec = $LASTEXITCODE

Write-Host ""
Write-Host "robocopy exit code: $ec"
# Robocopy: 0-7 = success variants, 8+ = failure
if ($ec -lt 8) {
    Write-Host "SUCCESS" -ForegroundColor Green
    exit 0
} else {
    Write-Host "FAILURE" -ForegroundColor Red
    exit $ec
}
