Option Explicit

Dim shell, fso, root, pythonw, launcher, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = fso.BuildPath(root, ".venv\Scripts\pythonw.exe")
launcher = fso.BuildPath(root, "launcher.pyw")

If Not fso.FileExists(pythonw) Then
  MsgBox "pythonw.exe was not found. Run create_shortcut.ps1 or start.ps1 once.", vbCritical, "Paper Manager"
  WScript.Quit 1
End If

If Not fso.FileExists(launcher) Then
  MsgBox "launcher.pyw was not found.", vbCritical, "Paper Manager"
  WScript.Quit 1
End If

command = """" & pythonw & """ """ & launcher & """"
shell.Run command, 0, False
