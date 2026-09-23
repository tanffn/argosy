<# Extend the existing backend logon/watchdog task to both servers and create a desktop shortcut. #>
[CmdletBinding()]
param([switch]$NoStart)
$ErrorActionPreference = 'Stop'
$ArgosyRoot = Split-Path -Parent $PSScriptRoot
$ArgosyLauncher = Join-Path $PSScriptRoot 'launch_argosy_silent.vbs'
$ArgosyTaskName = 'Argosy Backend Supervisor'
$ArgosyUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$ArgosyWscript = (Get-Command wscript.exe).Source
if (-not (Test-Path -LiteralPath $ArgosyLauncher)) { throw 'Missing silent launcher.' }
if (-not (Test-Path -LiteralPath (Join-Path $ArgosyRoot '.venv\Scripts\pythonw.exe'))) { throw 'Missing windowless Python interpreter.' }
New-Item -ItemType Directory -Force -Path (Join-Path $ArgosyRoot 'tmp') | Out-Null
$ArgosyExisting = Get-ScheduledTask -TaskName $ArgosyTaskName -ErrorAction SilentlyContinue
if ($ArgosyExisting) {
    if (-not ($ArgosyExisting.Actions.Arguments -like "*$ArgosyRoot*")) { throw 'Existing task belongs to another checkout; refusing replacement.' }
    $ArgosyBackup = Join-Path $ArgosyRoot ('tmp/startup-task-before-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.xml')
    Export-ScheduledTask -TaskName $ArgosyTaskName | Set-Content -LiteralPath $ArgosyBackup -Encoding Unicode
}
$ArgosyAction = New-ScheduledTaskAction -Execute $ArgosyWscript -Argument ('//B //Nologo "' + $ArgosyLauncher + '"') -WorkingDirectory $ArgosyRoot
$ArgosyLogon = New-ScheduledTaskTrigger -AtLogOn -User $ArgosyUser
$ArgosyWatchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$ArgosyPrincipal = New-ScheduledTaskPrincipal -UserId $ArgosyUser -LogonType Interactive -RunLevel Limited
$ArgosySettings = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4)
Register-ScheduledTask -TaskName $ArgosyTaskName -Action $ArgosyAction -Trigger @($ArgosyLogon,$ArgosyWatchdog) -Principal $ArgosyPrincipal -Settings $ArgosySettings -Description 'Silently health-check Argosy backend and UI at sign-in and every five minutes; restart owned unresponsive servers only.' -Force | Out-Null
$ArgosyShell = New-Object -ComObject WScript.Shell
$ArgosyDesktop = [Environment]::GetFolderPath('Desktop')
$ArgosyShortcutPath = Join-Path $ArgosyDesktop 'Argosy.lnk'
if (Test-Path -LiteralPath $ArgosyShortcutPath) {
    $ArgosyOldLink = $ArgosyShell.CreateShortcut($ArgosyShortcutPath)
    if ($ArgosyOldLink.Arguments -notlike "*$ArgosyRoot*") { throw 'Desktop Argosy shortcut is unrelated; refusing overwrite.' }
}
$ArgosyShortcut = $ArgosyShell.CreateShortcut($ArgosyShortcutPath)
$ArgosyShortcut.TargetPath = $ArgosyWscript
$ArgosyShortcut.Arguments = '//B //Nologo "' + $ArgosyLauncher + '" --open-browser'
$ArgosyShortcut.WorkingDirectory = $ArgosyRoot
$ArgosyShortcut.Description = 'Start/check Argosy silently and open its dashboard'
# Package the existing app logo as a Windows PNG-backed icon (no new artwork).
Add-Type -AssemblyName System.Drawing
$ArgosyIconPath = Join-Path $ArgosyRoot 'tmp/argosy-logo.ico'
$ArgosyLogo = [System.Drawing.Image]::FromFile((Join-Path $ArgosyRoot 'ui/public/logo.png'))
$ArgosyBitmap = New-Object System.Drawing.Bitmap 256,256
$ArgosyGraphics = [System.Drawing.Graphics]::FromImage($ArgosyBitmap)
$ArgosyPng = New-Object System.IO.MemoryStream
try {
    $ArgosyGraphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $ArgosyGraphics.DrawImage($ArgosyLogo, 0, 0, 256, 256)
    $ArgosyBitmap.Save($ArgosyPng, [System.Drawing.Imaging.ImageFormat]::Png)
    $ArgosyWriter = New-Object System.IO.BinaryWriter ([System.IO.File]::Create($ArgosyIconPath))
    try {
        $ArgosyWriter.Write([byte[]](0,0,1,0,1,0,0,0,0,0,1,0,32,0))
        $ArgosyWriter.Write([uint32]$ArgosyPng.Length)
        $ArgosyWriter.Write([uint32]22)
        $ArgosyWriter.Write($ArgosyPng.ToArray())
    } finally { $ArgosyWriter.Dispose() }
} finally {
    $ArgosyPng.Dispose(); $ArgosyGraphics.Dispose(); $ArgosyBitmap.Dispose(); $ArgosyLogo.Dispose()
}
$ArgosyShortcut.IconLocation = "$ArgosyIconPath,0"
$ArgosyShortcut.Save()
if (-not $NoStart) { Start-ScheduledTask -TaskName $ArgosyTaskName }
[pscustomobject]@{task=$ArgosyTaskName;user=$ArgosyUser;desktop=$ArgosyShortcutPath;frequency='Sign-in + every 5 minutes';silent=$true} | ConvertTo-Json
