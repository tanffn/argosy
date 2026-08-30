Option Explicit

' Task Scheduler launches this Windows Script Host entry point without a
' console.  It delegates to the existing idempotent PowerShell starter with
' window style 0, so the five-minute watchdog cannot flash a terminal before
' PowerShell processes its own -WindowStyle Hidden argument.

If WScript.Arguments.Count <> 3 Then
    WScript.Quit 2
End If

Dim shell, fso, scriptDir, projectRoot, starter, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
projectRoot = fso.GetParentFolderName(scriptDir)
starter = fso.BuildPath(scriptDir, "start_backend_detached.ps1")

Function Quote(value)
    Quote = Chr(34) & Replace(CStr(value), Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function

command = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass" _
    & " -WindowStyle Hidden -File " & Quote(starter) _
    & " -HostAddr " & Quote(WScript.Arguments(0)) _
    & " -Port " & Quote(WScript.Arguments(1)) _
    & " -LogStem " & Quote(WScript.Arguments(2))

shell.CurrentDirectory = projectRoot
WScript.Quit shell.Run(command, 0, True)
