$ErrorActionPreference = "Stop"

# NOTE: keep this file pure ASCII. Windows PowerShell 5.1 reads BOM-less .ps1 files
# using the system ANSI code page (936 here), and non-ASCII comments can swallow the
# following newline, merging the next line of code into the comment.

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

$Url = "http://127.0.0.1:8765"
$PortInUse = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($PortInUse) {
  Write-Host ("Paper manager is already running. Opening " + $Url)
  Write-Host "To stop it, run:  .\stop.ps1"
  Start-Process $Url
  return
}

python .\prepare_environment.py
if ($LASTEXITCODE -ne 0) {
  throw "Environment preparation failed."
}

.\.venv\Scripts\python.exe -m backend.app
