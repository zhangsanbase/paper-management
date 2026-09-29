$ErrorActionPreference = "Stop"

# NOTE: keep this file pure ASCII. Windows PowerShell 5.1 reads BOM-less .ps1 files
# using the system ANSI code page (936 here), and non-ASCII comments can swallow the
# following newline, merging the next line of code into the comment.

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Launcher = Join-Path $ProjectRoot "launcher.pyw"
$LauncherShim = Join-Path $ProjectRoot "launch_tray.vbs"
$IconPath = Join-Path $ProjectRoot "assets\paper-manager.ico"
$Pythonw = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$WScript = Join-Path $env:WINDIR "System32\wscript.exe"

if (!(Test-Path $Launcher)) {
  throw "launcher.pyw was not found."
}

if (!(Test-Path $LauncherShim)) {
  throw "launch_tray.vbs was not found."
}

if (!(Test-Path $Pythonw)) {
  if (!(Test-Path $Python)) {
    Write-Host "Creating virtual environment for the desktop shortcut..."
    python -m venv (Join-Path $ProjectRoot ".venv")
  }
}

if (!(Test-Path $Pythonw)) {
  throw "pythonw.exe was not found in .venv\Scripts. Run .\start.ps1 once or check Python installation."
}

if (!(Test-Path $WScript)) {
  throw "wscript.exe was not found."
}

$ShortcutName = (-join ([char[]](0x79D1, 0x7814, 0x6587, 0x732E, 0x7BA1, 0x7406, 0x5668))) + ".lnk"
$Shell = New-Object -ComObject WScript.Shell

function Update-PaperManagerShortcut {
  param([string]$ShortcutPath)

  $Shortcut = $Shell.CreateShortcut($ShortcutPath)
  $Shortcut.TargetPath = $WScript
  $Shortcut.Arguments = '"' + $LauncherShim + '"'
  $Shortcut.WorkingDirectory = $ProjectRoot
  if (Test-Path $IconPath) {
    $Shortcut.IconLocation = $IconPath + ",0"
  } else {
    $Shortcut.IconLocation = $Pythonw + ",0"
  }
  $Shortcut.Description = "Start local paper manager tray launcher"
  $Shortcut.Save()
  Write-Host ("Shortcut updated: " + $ShortcutPath)
}

$Desktop = [Environment]::GetFolderPath("Desktop")
Update-PaperManagerShortcut -ShortcutPath (Join-Path $Desktop $ShortcutName)
Update-PaperManagerShortcut -ShortcutPath (Join-Path $ProjectRoot $ShortcutName)
