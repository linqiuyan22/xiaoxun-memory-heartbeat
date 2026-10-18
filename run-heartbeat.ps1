$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$work = Join-Path $root 'workbench'
$logs = Join-Path $root 'run-logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$log = Join-Path $logs "$stamp.log"
$report = Join-Path $logs "$stamp-result.json"
$lastMessage = Join-Path $logs "$stamp-message.txt"
$codex = 'C:\Users\27488\bin\codex.cmd'
function Write-Log([string]$text) { Add-Content -LiteralPath $log -Value "[$(Get-Date -Format o)] $text" -Encoding utf8 }
function Rollback-Workbench([string]$base) {
    git -C $root restore --source $base --staged --worktree -- workbench 2>&1 | ForEach-Object { Add-Content -LiteralPath $log -Value "$_" -Encoding utf8 }
    git -C $root clean -fd -- workbench 2>&1 | ForEach-Object { Add-Content -LiteralPath $log -Value "$_" -Encoding utf8 }
}
try {
    $status = @(git -C $root status --porcelain --untracked-files=all)
    if ($status.Count -gt 0) { Write-Log 'SKIP: repository was not clean before run.'; exit 2 }
    $base = (git -C $root rev-parse HEAD).Trim()
    $prompt = @'
Read AGENTS.md and TASKS.md. Do at most one small task. Use only this workbench and synthetic fixtures. Do not call SSH, MCP, network services, or deploy. Run relevant tests and update TASKS.md, RUNLOG.md, and HEARTBEAT-RESULT.json. Do not use Git commands; the outer supervisor will independently run the tests and commit successful work. Report PASS only when you actually ran the relevant tests successfully; otherwise report BLOCKED or NOOP with the exact reason.
'@
    Write-Log "START base=$base"
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {

$prompt | & $codex exec --ephemeral --skip-git-repo-check --sandbox workspace-write --color never -C $work -o $lastMessage -c 'notify=[]' - 2>&1 | ForEach-Object { Add-Content -LiteralPath $log -Value "$_" -Encoding utf8 }
        $codexExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $oldPreference }
    Write-Log "CODEX_EXIT=$codexExit"
    $resultFile = Join-Path $work 'HEARTBEAT-RESULT.json'
    if (Test-Path $resultFile) { Copy-Item -LiteralPath $resultFile -Destination $report -Force }
    if ($codexExit -ne 0 -or -not (Test-Path $resultFile)) { Write-Log 'FAIL: Codex exit or result file missing; rolling back workbench.'; Rollback-Workbench $base; exit 3 }
    $result = Get-Content -LiteralPath $resultFile -Raw | ConvertFrom-Json
    if ($result.status -notin @('PASS','NOOP')) { Write-Log "FAIL: agent status=$($result.status); rolling back workbench."; Rollback-Workbench $base; exit 4 }
    $testPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    Push-Location $work
    try {
        python -B -m unittest discover -v -s tests 2>&1 | ForEach-Object { Add-Content -LiteralPath $log -Value "$_" -Encoding utf8 }
        $testExit = $LASTEXITCODE
    } finally { Pop-Location; $ErrorActionPreference = $testPreference }
    Write-Log "SUPERVISOR_TEST_EXIT=$testExit"
    $dbFiles = @(Get-ChildItem -LiteralPath $work -Recurse -File -ErrorAction SilentlyContinue | Where-Object {$_.Extension -in @('.db','.sqlite','.sqlite3')})
    if ($testExit -ne 0 -or $dbFiles.Count -gt 0) { Write-Log 'FAIL: independent tests failed or a database file appeared; rolling back workbench.'; Rollback-Workbench $base; exit 5 }
    git -C $root add -- workbench
    if ($LASTEXITCODE -ne 0) { throw 'git add before child review failed' }
    $pythonFiles = @(git -C $root diff --cached --name-only -- workbench | Where-Object {$_ -match '\.py$'})
    if ($pythonFiles.Count -gt 0) {
        $diffParts = foreach ($path in $pythonFiles) { git -C $root diff --cached -- $path }
        $pythonDiff = $diffParts -join "`n"
        Write-Log "DEEPSEEK_REVIEW_START python_files=$($pythonFiles.Count) diff_chars=$($pythonDiff.Length)"
        $reviewLines = $pythonDiff | & pwsh.exe -NoProfile -File (Join-Path $root 'reviewer\review-patch.ps1') 2>&1
        $reviewExit = $LASTEXITCODE
        $reviewText = $reviewLines -join "`n"
        Write-Log "DEEPSEEK_REVIEW_EXIT=$reviewExit result=$reviewText"
        if ($reviewExit -ne 0) { Write-Log 'FAIL: independent DeepSeek review did not approve; rolling back workbench.'; Rollback-Workbench $base; exit 6 }
    }
    $changed = @(git -C $root status --porcelain --untracked-files=all -- workbench)
    if ($changed.Count -gt 0) {
        git -C $root commit -m 'heartbeat: validated memory-search work' 2>&1 | ForEach-Object { Add-Content -LiteralPath $log -Value "$_" -Encoding utf8 }
        if ($LASTEXITCODE -ne 0) { throw 'git commit failed' }
    }
    Write-Log 'PASS: independent tests passed; only workbench changes were committed.'
    Get-ChildItem -LiteralPath $logs -File | Where-Object {$_.LastWriteTime -lt (Get-Date).AddDays(-30)} | Remove-Item -Force -ErrorAction SilentlyContinue
} catch {
    Write-Log "ERROR: $($_.Exception.Message)"
    if ($base) { Rollback-Workbench $base }
    exit 10
}

