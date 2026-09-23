Option Explicit
Dim shell, fso, root, python, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
python = fso.BuildPath(root, ".venv\Scripts\pythonw.exe")
command = Chr(34) & python & Chr(34) & " " & Chr(34) & fso.BuildPath(root, "scripts\ensure_servers.py") & Chr(34)
If WScript.Arguments.Count > 0 Then
    If WScript.Arguments(0) = "--open-browser" Then command = command & " --open-browser"
End If
shell.CurrentDirectory = root
WScript.Quit shell.Run(command, 0, True)
