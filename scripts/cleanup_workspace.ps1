<# Preview by default. -Apply moves only untracked test roots and obsolete loose
   DB safety copies to a timestamped sibling quarantine. Nothing is deleted.
   Managed backups/, live db/, runtime evidence and tracked scratch stay intact. #>
[CmdletBinding()]
param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$Workspace = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd('\')
function Assert-PlainAncestors([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            if ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Reparse point in candidate ancestry: $current"
            }
        }
        $current = Split-Path -Parent $current
    }
}
Assert-PlainAncestors $Workspace
Assert-PlainAncestors (Join-Path $Workspace 'db')
$Quarantine = Join-Path (Split-Path -Parent $Workspace) ('financial-advisor-cleanup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
if (Test-Path -LiteralPath $Quarantine) { throw 'Quarantine already exists' }
$gitInfo = New-Object System.Diagnostics.ProcessStartInfo
$gitInfo.FileName = 'git'
$gitInfo.Arguments = 'ls-files -z'
$gitInfo.WorkingDirectory = $Workspace
$gitInfo.UseShellExecute = $false
$gitInfo.CreateNoWindow = $true
$gitInfo.RedirectStandardOutput = $true
$gitInfo.StandardOutputEncoding = [Text.Encoding]::UTF8
$gitProcess = [Diagnostics.Process]::Start($gitInfo)
$tracked = $gitProcess.StandardOutput.ReadToEnd().Split([char]0)
$gitProcess.WaitForExit()
if ($gitProcess.ExitCode -ne 0) { throw 'Cannot verify tracked files' }
$candidates = @(Get-ChildItem -LiteralPath $Workspace -Directory -Force | Where-Object { $_.Name -like '.test-*' })
$candidates += @(Get-ChildItem -LiteralPath (Join-Path $Workspace 'db') -File | Where-Object {
    $_.Name -like 'argosy.db.bak_*' -or $_.Name -like 'argosy.db.SAFETY_*' -or $_.Name -like 'argosy_before_*.db'
})
$moves = @()
foreach ($item in $candidates) {
    $absolute = [IO.Path]::GetFullPath($item.FullName)
    if (-not $absolute.StartsWith($Workspace + '\', [StringComparison]::OrdinalIgnoreCase) -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Unsafe candidate: $absolute" }
    $relative = $absolute.Substring($Workspace.Length + 1).Replace('\', '/')
    if (@($tracked | Where-Object { $_ -eq $relative -or $_.StartsWith($relative + '/', [StringComparison]::OrdinalIgnoreCase) }).Count) {
        throw "Candidate contains tracked files: $relative"
    }
    $moves += [PSCustomObject]@{ Source=$absolute; Destination=(Join-Path $Quarantine $relative) }
}
Write-Output "Candidates: $($moves.Count); quarantine: $Quarantine; apply: $Apply"
$moves | Format-Table Source,Destination -AutoSize
if (-not $Apply -or $moves.Count -eq 0) { return }
if (Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python|pytest' -and $_.CommandLine -match '\bpytest\b' }) {
    throw 'Tests are running; retry cleanup after they finish'
}
New-Item -ItemType Directory -Path $Quarantine | Out-Null
$moves | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Quarantine 'manifest.json') -Encoding UTF8
$failures = @()
foreach ($move in $moves) {
    $parent = Split-Path -Parent $move.Destination
    if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent | Out-Null }
    try {
        Assert-PlainAncestors $move.Source
        Assert-PlainAncestors $move.Destination
        # Same-volume rename: do not recurse into test-created junctions/links.
        if ([IO.Directory]::Exists($move.Source)) { [IO.Directory]::Move($move.Source, $move.Destination) }
        else { [IO.File]::Move($move.Source, $move.Destination) }
    }
    catch { $failures += $move.Source; Write-Warning "Not moved (preserved): $($move.Source): $($_.Exception.Message)" }
}
Write-Output "Moved $($moves.Count - $failures.Count) items; $($failures.Count) could not be moved. Recover using $Quarantine\manifest.json. No files deleted."
if ($failures.Count) { exit 1 }
