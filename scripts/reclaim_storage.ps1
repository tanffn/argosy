# Preview by default. -Apply refreshes a verified recovery copy before deleting
# only the audited, untracked obsolete files and byte-matched compressed originals.
[CmdletBinding()]
param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd('\')

function Assert-LocalFile([string]$Relative) {
    $full = [IO.Path]::GetFullPath((Join-Path $repo $Relative))
    if (-not $full.StartsWith($repo + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escapes workspace: $Relative"
    }
    $ancestor = $full
    while ($ancestor) {
        if (Test-Path -LiteralPath $ancestor) {
            if ((Get-Item -LiteralPath $ancestor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Refusing linked path: $ancestor"
            }
        }
        $ancestor = Split-Path -Parent $ancestor
    }
    $tracked = & git -C $repo ls-files -- $Relative
    if ($LASTEXITCODE -ne 0) { throw 'Cannot check tracked files' }
    if ($tracked) { throw "Refusing tracked file: $Relative" }
    if (Test-Path -LiteralPath $full -PathType Container) { throw "Expected a file: $Relative" }
    return $full
}

function Assert-GzipMatches([string]$Raw, [string]$Compressed) {
    $inputStream = [IO.File]::OpenRead($Compressed)
    $gzip = New-Object IO.Compression.GZipStream($inputStream, [IO.Compression.CompressionMode]::Decompress)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $actual = [BitConverter]::ToString($sha.ComputeHash($gzip)).Replace('-', '') }
    finally { $sha.Dispose(); $gzip.Dispose(); $inputStream.Dispose() }
    if ($actual -ne (Get-FileHash -LiteralPath $Raw -Algorithm SHA256).Hash) {
        throw "Compressed copy does not match; original preserved: $Raw"
    }
    # Ensure verified compressed bytes are durable before removing a raw copy.
    if ($Apply) {
        $stream = [IO.File]::Open($Compressed, 'Open', 'ReadWrite', 'Read')
        try { $stream.Flush($true) } finally { $stream.Dispose() }
    }
}

# Exact audited paths AND sizes; changed/reused files require a fresh review.
$obsolete = @{
    'scratchpad/discord_advisor/news-review-probe.db' = 746610688L
    'scratchpad/discord_advisor/fleet-probe.db' = 742526976L
    'scratchpad/discord_advisor/pre-discord-20260919T075112Z.db' = 742014976L
    'tmp/research-live-run-20260913-1742/argosy.db' = 703901696L
    'tmp/pre-shared-research-20260913.db' = 698572800L
    'tmp/argosy.db.bak-pre-fills-2026-07-06' = 232591360L
    'db/backups/pre_0120_20260922_0730.db' = 764391424L
    'db/backups/pre_0121_20260922_0756.db' = 764719104L
    'db/backups/pre_0122_20260922_122620.db' = 766930944L
    'backups/argosy-pre-0115-20260830.db' = 632561664L
    'backups/argosy_pre_0107_20260825.db' = 560726016L
    'backups/argosy_pre_0109_20260827.db' = 597716992L
    'tmp/storage-restore-check.db' = 780902400L
}
$targets = @()
foreach ($relative in $obsolete.Keys) {
    $full = Assert-LocalFile $relative
    if (Test-Path -LiteralPath $full) {
        if ((Get-Item -LiteralPath $full).Length -ne $obsolete[$relative]) {
            throw "Audited file changed; refusing cleanup: $relative"
        }
        $targets += [pscustomobject]@{ Path=$full; Relative=$relative; Gzip=$null }
    }
}
$backupRoot = Join-Path $repo 'backups'
# Validate the directory ancestry before enumeration; no recursive traversal.
[void](Assert-LocalFile 'backups/.path-validation')
if (Test-Path -LiteralPath $backupRoot) {
    foreach ($file in Get-ChildItem -LiteralPath $backupRoot -File) {
        if ($file.Name -notmatch '^argosy-\d{8}\.db$') { continue }
        $relative = 'backups/' + $file.Name
        $full = Assert-LocalFile $relative
        $compressed = Assert-LocalFile ($relative + '.gz')
        if (-not (Test-Path -LiteralPath $compressed)) { continue }
        Assert-GzipMatches $full $compressed
        $targets += [pscustomobject]@{ Path=$full; Relative=$relative; Gzip=$compressed }
    }
}
# Preflight all sidecars and open handles before deleting anything.
foreach ($target in $targets) {
    foreach ($suffix in @('-wal', '-shm', '-journal')) {
        $sidecar = Assert-LocalFile ($target.Relative + $suffix)
        if (-not (Test-Path -LiteralPath $sidecar)) { continue }
        if ($suffix -ne '-shm' -and (Get-Item -LiteralPath $sidecar).Length -gt 0) {
            throw "Snapshot has uncheckpointed data; not disposable: $sidecar"
        }
        $handle = [IO.File]::Open($sidecar, 'Open', 'Read', 'None')
        $handle.Dispose()
    }
    $handle = [IO.File]::Open($target.Path, 'Open', 'Read', 'None')
    $handle.Dispose()
}
$bytes = ($targets | ForEach-Object { (Get-Item -LiteralPath $_.Path).Length } | Measure-Object -Sum).Sum
$targets | Select-Object Relative, Gzip | Format-Table -AutoSize
Write-Host ('Eligible raw files: {0}; {1:N2} GiB' -f $targets.Count, ($bytes / 1GB))
if (-not $Apply) { Write-Host 'Preview only. Run with -Apply to back up, recheck, and remove these files.'; exit 0 }

$processes = Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine }
foreach ($target in $targets) {
    foreach ($process in $processes) {
        $command = $process.CommandLine.Replace('/', '\')
        if ($command.IndexOf($target.Relative.Replace('/', '\'), [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            throw "A process references a cleanup target (PID $($process.ProcessId)); stop it first"
        }
    }
}
$destination = Join-Path (Split-Path -Parent $repo) 'financial-advisor-backup-fresh-20260923'
& powershell.exe -NoProfile -File (Join-Path $PSScriptRoot 'backup_to_sibling.ps1') -Destination $destination -Quiet
if ($LASTEXITCODE -ne 0) { throw 'Recovery copy failed; nothing deleted' }
foreach ($target in $targets) {
    [void](Assert-LocalFile $target.Relative)
    if ($target.Gzip) { Assert-GzipMatches $target.Path $target.Gzip }
    elseif ((Get-Item -LiteralPath $target.Path).Length -ne $obsolete[$target.Relative]) {
        throw "File changed during backup; retained: $($target.Relative)"
    }
    foreach ($suffix in @('-wal', '-shm', '-journal')) {
        $sidecar = Assert-LocalFile ($target.Relative + $suffix)
        if (Test-Path -LiteralPath $sidecar) {
            if ($suffix -ne '-shm' -and (Get-Item -LiteralPath $sidecar).Length -gt 0) { throw "Sidecar changed: $sidecar" }
            $handle = [IO.File]::Open($sidecar, 'Open', 'Read', 'None')
            $handle.Dispose()
        }
    }
    $handle = [IO.File]::Open($target.Path, 'Open', 'Read', 'None')
    $handle.Dispose()
    Remove-Item -LiteralPath $target.Path -Force
    foreach ($suffix in @('-wal', '-shm', '-journal')) {
        $sidecar = Assert-LocalFile ($target.Relative + $suffix)
        if (Test-Path -LiteralPath $sidecar) { Remove-Item -LiteralPath $sidecar -Force }
    }
    Write-Host "Removed obsolete raw file: $($target.Relative)"
}
Write-Host 'Done. Retained dated snapshots can be restored from their .db.gz files.'
Write-Host 'Obsolete probe/migration databases were deleted, not quarantined; the fresh current DB backup is retained.'
Write-Host 'Live data, transcripts, dependencies, UI cache, old sibling backup and Drive sync cache were not deleted.'
