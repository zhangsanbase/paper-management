$ErrorActionPreference = "Stop"

# NOTE: keep this file pure ASCII. Windows PowerShell 5.1 reads BOM-less .ps1 files
# using the system ANSI code page (936 here), and non-ASCII comments can swallow the
# following newline, merging the next line of code into the comment.

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

. (Join-Path $ProjectRoot "scripts\resolve-port.ps1")
$Port = Get-PaperManagerPort

$Connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (!$Connections) {
  Write-Host "Paper manager is not running."
  return
}

# With an anaconda-based venv, .venv\Scripts\python.exe spawns a second base
# interpreter, and the child is what actually holds the port. So walk up the parent
# chain and stop the whole backend.app process tree.
function Get-BackendProcessIds {
  param([int]$StartId)

  $Ids = New-Object System.Collections.Generic.List[int]
  $CurrentId = $StartId
  while ($CurrentId -and $CurrentId -ne 0) {
    $Process = Get-CimInstance Win32_Process -Filter "ProcessId=$CurrentId" -ErrorAction SilentlyContinue
    if (!$Process) { break }
    # Only python interpreters count. Without the name check, any ancestor whose
    # command line merely mentions "backend.app" gets dragged in and killed -- a
    # shell running a compound command, editor task or CI runner, for example.
    $IsPython = $Process.Name -like "python*.exe"
    if ($IsPython -and $Process.CommandLine -and $Process.CommandLine -like "*backend.app*") {
      $Ids.Add([int]$Process.ProcessId)
      $CurrentId = [int]$Process.ParentProcessId
    } else {
      break
    }
  }
  return $Ids
}

# Collect first, then stop: children before parents. The other order orphans the child
# and leaves the port occupied.
$TargetIds = New-Object System.Collections.Generic.List[int]
foreach ($ProcessId in ($Connections.OwningProcess | Sort-Object -Unique)) {
  foreach ($Id in (Get-BackendProcessIds -StartId $ProcessId)) {
    if (!$TargetIds.Contains($Id)) {
      $TargetIds.Add($Id)
    }
  }
}

if ($TargetIds.Count -eq 0) {
  Write-Host ("Port " + $Port + " is held by process " + ($Connections.OwningProcess -join ", ") + ", but it does not look like the paper manager. Nothing was stopped.")
  return
}

Write-Host ("Stopping paper manager (PID " + ($TargetIds -join ", ") + ")...")
foreach ($Id in $TargetIds) {
  try {
    Stop-Process -Id $Id -Force -ErrorAction Stop
  } catch {
    # The process may have exited on its own already; that is not a failure.
  }
}

foreach ($Attempt in 1..20) {
  Start-Sleep -Milliseconds 250
  if (!(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
    Write-Host "Paper manager stopped."
    return
  }
}

throw ("Port " + $Port + " is still in use after stopping. Check Task Manager for leftover python processes.")
