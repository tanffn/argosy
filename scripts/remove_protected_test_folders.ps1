<# One-off cleanup of the 33 verified test roots left on 2026-09-23.
   Default: preview only. -Delete requires elevated PowerShell and permanently
   deletes ONLY these named, untracked roots. Links are unlinked, never followed.
   Never run while tests are using these folders. #>
[CmdletBinding()]
param([switch]$Delete)
$ErrorActionPreference = 'Stop'
$Workspace = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd('\')
$Names = @(
    '.test-alpha-horizon-20260921'
    '.test-alpha-retry-20260921'
    '.test-contract-boundaries-20260921'
    '.test-contract-consumers-20260922'
    '.test-contract-final-20260921'
    '.test-contract-loop-20260921'
    '.test-contract-rollout-20260922'
    '.test-horizon-20260921'
    '.test-horizon-evaluator-20260921'
    '.test-price-recovery-20260922'
    '.test-research-inputs-20260913'
    '.test-research-shared-a'
    '.test-research-shared-b'
    '.test-research-shared-c'
    '.test-research-shared-d'
    '.test-research-shared-e'
    '.test-research-shared-f'
    '.test-research-shared-h'
    '.test-research-shared-i'
    '.test-research-shared-j'
    '.test-research-shared-k'
    '.test-scoring-contract-20260921'
    '.test-scoring-contract2-20260921'
    '.test-scoring-contract3-20260921'
    '.test-temp18'
    '.test-temp19'
    '.test-temp20'
    '.test-temp21'
    '.test-temp22'
    '.test-temp23'
    '.test-temp24'
    '.test-temp25'
    '.test-temp26'
)
# Refuse workspace aliases before any permission changes or deletion.
$ancestor = $Workspace
while ($ancestor) {
    if ((Get-Item -LiteralPath $ancestor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Workspace ancestry contains a link: $ancestor"
    }
    $ancestor = Split-Path -Parent $ancestor
}
$info = New-Object Diagnostics.ProcessStartInfo
$info.FileName = 'git'
$info.Arguments = 'ls-files -z'
$info.WorkingDirectory = $Workspace
$info.UseShellExecute = $false
$info.CreateNoWindow = $true
$info.RedirectStandardOutput = $true
$info.StandardOutputEncoding = [Text.Encoding]::UTF8
$gitProcess = [Diagnostics.Process]::Start($info)
$tracked = $gitProcess.StandardOutput.ReadToEnd().Split([char]0)
$gitProcess.WaitForExit()
if ($gitProcess.ExitCode -ne 0) { throw 'Cannot verify tracked files; nothing deleted' }
$entries = @(Get-ChildItem -LiteralPath $Workspace -Force -Directory)
$targets = @()
foreach ($name in $Names) {
    $item = $entries | Where-Object { $_.Name -eq $name }
    if ($null -eq $item) { continue }
    $absolute = [IO.Path]::GetFullPath($item.FullName)
    if ((Split-Path -Parent $absolute) -ne $Workspace -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Unsafe test root: $absolute"
    }
    if (@($tracked | Where-Object { $_ -eq $name -or $_.StartsWith($name + '/', [StringComparison]::OrdinalIgnoreCase) }).Count) {
        throw "Tracked files found under $name; nothing deleted"
    }
    $targets += $absolute
}
$targets | ForEach-Object { Write-Output $_ }
Write-Output "Verified test roots: $($targets.Count). Delete enabled: $Delete"
if (-not $Delete -or -not $targets.Count) { return }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Open PowerShell using Run as administrator, then rerun with -Delete'
}
if (Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python|pytest' -and $_.CommandLine -match '\bpytest\b' }) {
    throw 'Tests are running; stop them before deleting test folders'
}
$sid = $identity.User.Value
function Remove-TestEntry([string]$Path, [string]$TestRoot) {
    $absolute = [IO.Path]::GetFullPath($Path)
    if ($absolute -ne $TestRoot -and -not $absolute.StartsWith($TestRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Entry escaped its verified test root: $absolute"
    }
    $entry = Get-Item -LiteralPath $absolute -Force
    if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        if ($absolute -eq $TestRoot) { throw "Test root changed into a link: $absolute" }
        # Non-recursive deletion removes the link itself, not its target.
        if ($entry.PSIsContainer) { [IO.Directory]::Delete($absolute, $false) }
        else { [IO.File]::Delete($absolute) }
        return
    }
    # Repair access one entry at a time; no recursive ACL command that might
    # follow a pytest-created junction outside this test folder.
    & takeown.exe /F $absolute /A | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Cannot take ownership: $absolute" }
    & icacls.exe $absolute /grant:r "*${sid}:(F)" /L | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Cannot grant cleanup access: $absolute" }
    if ($entry.PSIsContainer) {
        foreach ($child in Get-ChildItem -LiteralPath $absolute -Force) {
            Remove-TestEntry $child.FullName $TestRoot
        }
        Remove-Item -LiteralPath $absolute -Force
    } else {
        Remove-Item -LiteralPath $absolute -Force
    }
}
foreach ($target in $targets) {
    Remove-TestEntry $target $target
    Write-Output "Permanently deleted: $target"
}
