# Resolves the service port from the PAPER_MANAGER_PORT environment variable.
# Keep the 8765 default in sync with backend/server_config.py, which is the same
# source of truth for the Python side.
#
# NOTE: keep this file pure ASCII. Windows PowerShell 5.1 reads BOM-less .ps1
# files using the system ANSI code page (936 here), and non-ASCII comments can
# swallow the following newline, merging the next line of code into the comment.

function Get-PaperManagerPort {
  $Raw = $env:PAPER_MANAGER_PORT
  if (!$Raw) { return 8765 }

  $Parsed = 0
  if (![int]::TryParse($Raw, [ref]$Parsed) -or $Parsed -lt 1 -or $Parsed -gt 65535) {
    throw ("PAPER_MANAGER_PORT is not a valid port number: " + $Raw)
  }
  return $Parsed
}
