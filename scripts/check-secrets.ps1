<#
    密钥兜底检查：阻止密钥、个人数据与本地运行文件进入 git。

    首次运行会自动把自己安装为本仓库的 pre-commit 钩子（仅对
    D:\Code\Paper_mangement 生效，不改动全局 git 配置）。
    之后每次 git commit 都会自动检查，命中则阻止提交。

    手动运行：pwsh scripts/check-secrets.ps1
    重装钩子：pwsh scripts/check-secrets.ps1 -Install
#>
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$NoInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# 受保护的路径：这些文件永远不应进入 git
$forbiddenPathPattern = '^(data/(?!\.gitkeep$)|backups/|\.env|.*\.env$|.*\.(log|tmp)$|.*\.sqlite3(\.bak)?$)'

# 密钥形态：sk- 开头的长串，或 api_key/secret/token 后跟长值的赋值
$secretPattern = 'sk-[A-Za-z0-9]{20,}|(api[_-]?key|secret|token)[^A-Za-z0-9]{1,4}["'']?[A-Za-z0-9_\-]{16,}'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$HookPath = Join-Path $RepoRoot '.git/hooks/pre-commit'

function Get-CheckFileList {
    # 已跟踪文件始终检查，保证手动运行与钩子运行结果一致
    $tracked = @(
        git -C $RepoRoot ls-files --cached 2>$null |
        Where-Object { $_ -and $_.Trim() }
    )

    # 额外检查已暂存但尚未跟踪的文件（git add 之后、首次提交之前）
    $staged = @(
        git -C $RepoRoot diff --cached --name-only --diff-filter=ACMR 2>$null |
        Where-Object { $_ -and $_.Trim() }
    )

    return @($tracked + $staged | Sort-Object -Unique)
}

$files = Get-CheckFileList
$violations = New-Object System.Collections.Generic.List[string]

# --- 检查 1：路径规则 -------------------------------------------------------
$badPaths = @($files | Where-Object { $_ -match $forbiddenPathPattern })

if ($badPaths.Count -gt 0) {
    $violations.Add("以下本地文件不应进入 git：")
    foreach ($p in $badPaths) { $violations.Add("  $p") }
}

# --- 检查 2：内容规则 -------------------------------------------------------
# 只对已入库的路径做内容扫描；git grep 退出码 1 表示无命中，属预期结果
$contentHits = @()
if ($files.Count -gt 0) {
    $contentHits = @(
        git -C $RepoRoot grep -I -n -E $secretPattern -- @files 2>$null
    )
}

if ($contentHits.Count -gt 0) {
    $violations.Add("以下位置疑似包含密钥：")
    foreach ($hit in $contentHits) { $violations.Add("  $hit") }
}

# --- 检查 3：确认受保护文件仍被 .gitignore 忽略 ------------------------------
foreach ($probe in @('data/config.json', 'backups/')) {
    git -C $RepoRoot check-ignore -q -- $probe 2>$null
    if ($LASTEXITCODE -ne 0) {
        $violations.Add("警告：$probe 已不再被 .gitignore 忽略，存在误提交风险。")
    }
}

# --- 输出结果 ---------------------------------------------------------------
if ($violations.Count -gt 0) {
    Write-Host ""
    Write-Host "检查未通过，已阻止提交。" -ForegroundColor Red
    Write-Host ""
    foreach ($line in $violations) { Write-Host $line }
    Write-Host ""
    Write-Host "确认是误报时，可用 git commit --no-verify 跳过本次检查。"
    Write-Host ""
    exit 1
}

# --- 安装钩子 ---------------------------------------------------------------
$installResult = $null

if (-not $NoInstall) {
    $existing = $null
    if (Test-Path -LiteralPath $HookPath) {
        $existing = Get-Content -LiteralPath $HookPath -Raw
    }

    # 内容一致则视为已安装，避免每次提交都改写钩子
    if ($existing -and ($existing -match 'check-secrets')) {
        $installResult = 'already'
    }
    elseif ($existing) {
        $installResult = 'conflict'
    }
    else {
        $hooksDir = Split-Path -Parent $HookPath
        if (-not (Test-Path -LiteralPath $hooksDir)) {
            New-Item -ItemType Directory -Path $hooksDir -Force | Out-Null
        }

        # 用 sh 包装，把 -NoInstall 传下去，避免钩子再次安装自己。
        # 路径优先按仓库实际位置解析，这样整个项目目录被移动后钩子依然有效。
        $hookBody = @"
#!/bin/sh
root=`$(git rev-parse --show-toplevel 2>/dev/null)
[ -n "`$root" ] || root="$($RepoRoot -replace '\\','/')"
script="`$root/scripts/check-secrets.ps1"
[ -f "`$script" ] || exit 0
command -v pwsh >/dev/null 2>&1 && exec pwsh -NoProfile -ExecutionPolicy Bypass -File "`$script" -NoInstall
exec powershell -NoProfile -ExecutionPolicy Bypass -File "`$script" -NoInstall
"@
        Set-Content -LiteralPath $HookPath -Value $hookBody -Encoding ascii -NoNewline
        $installResult = 'installed'
    }
}

Write-Host "OK"

switch ($installResult) {
    'installed' { Write-Host "pre-commit 钩子已安装，以后每次提交会自动检查。" }
    'already'   { Write-Host "pre-commit 钩子已存在。" }
    'conflict'  {
        Write-Host "提示：已存在其他 pre-commit 钩子，未覆盖。"
        Write-Host "如需启用，请手动把下面的调用加入 $HookPath ："
        Write-Host "  pwsh -NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\check-secrets.ps1`" -NoInstall"
    }
    default { }
}

exit 0
