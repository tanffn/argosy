<# Install quiet event-driven catch-up. Argosy owns the daily cron; this task
   additionally requests the same job on sign-in/unlock (and at 18:00). #>
$ErrorActionPreference = 'Stop'
$ArgosyRoot = Split-Path -Parent $PSScriptRoot
$ArgosyPython = Join-Path $ArgosyRoot '.venv\Scripts\pythonw.exe'
$ArgosyScript = Join-Path $ArgosyRoot 'scripts\run_alpha_capture.py'
$ArgosyIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$ArgosyTaskName = 'Argosy Alpha Daily Capture'
$ArgosyService = New-Object -ComObject 'Schedule.Service'
$ArgosyService.Connect()
$ArgosyFolder = $ArgosyService.GetFolder('\')
try { $ArgosyOld = $ArgosyFolder.GetTask($ArgosyTaskName) } catch { $ArgosyOld = $null }
if ($ArgosyOld -and $ArgosyOld.Definition.Actions.Item(1).Arguments -notlike "*$ArgosyScript*") {
    throw 'Existing task belongs to another checkout; refusing replacement'
}
$ArgosyTask = $ArgosyService.NewTask(0)
$ArgosyTask.RegistrationInfo.Description = 'Capture Meet Kevin Alpha once daily through Argosy; catch up after sign-in/unlock, no recurring browser polling.'
$ArgosyTask.Principal.UserId = $ArgosyIdentity.Name
$ArgosyTask.Principal.LogonType = 3 # InteractiveToken: normal Chrome session, no stored password.
$ArgosyTask.Principal.RunLevel = 0
$ArgosyTask.Settings.Hidden = $true
$ArgosyTask.Settings.StartWhenAvailable = $true
$ArgosyTask.Settings.DisallowStartIfOnBatteries = $false
$ArgosyTask.Settings.StopIfGoingOnBatteries = $false
$ArgosyTask.Settings.MultipleInstances = 2 # IgnoreNew
# The task now runs the registered job locally, including the research fleet.
$ArgosyTask.Settings.ExecutionTimeLimit = 'PT1H'
$ArgosyDaily = $ArgosyTask.Triggers.Create(2)
$ArgosyDaily.StartBoundary = (Get-Date).Date.AddHours(18).ToString('yyyy-MM-ddTHH:mm:ss')
$ArgosyDaily.DaysInterval = 1
$ArgosyLogon = $ArgosyTask.Triggers.Create(9)
$ArgosyLogon.UserId = $ArgosyIdentity.Name
$ArgosyLogon.Delay = 'PT45S'
$ArgosyUnlock = $ArgosyTask.Triggers.Create(11)
$ArgosyUnlock.StateChange = 8 # TASK_SESSION_UNLOCK
$ArgosyUnlock.UserId = $ArgosyIdentity.Name
$ArgosyUnlock.Delay = 'PT30S'
$ArgosyAction = $ArgosyTask.Actions.Create(0)
$ArgosyAction.Path = $ArgosyPython
$ArgosyAction.Arguments = '"' + $ArgosyScript + '" --trigger'
$ArgosyAction.WorkingDirectory = $ArgosyRoot
$ArgosyFolder.RegisterTaskDefinition($ArgosyTaskName, $ArgosyTask, 6, $ArgosyIdentity.Name, $null, 3) | Out-Null
@{task=$ArgosyTaskName; schedule='18:00 daily + sign-in/unlock catch-up'; console_windows=$false; repeated_site_polling=$false} | ConvertTo-Json
