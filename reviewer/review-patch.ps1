$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root 'run-logs'
$patch = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($patch)) { throw 'No diff received.' }
if ($patch.Length -gt 24000) { throw 'Diff exceeds the 24000-character child-review limit.' }
$diffPaths = [regex]::Matches($patch, '(?m)^diff --git a/(.+?) b/(.+?)$')
if ($diffPaths.Count -eq 0) { throw 'Input is not a Git unified diff.' }
foreach ($match in $diffPaths) {
    if (-not $match.Groups[1].Value.StartsWith('workbench/') -or
        -not $match.Groups[2].Value.StartsWith('workbench/')) {
        throw 'The child reviewer accepts workbench-only diffs.'
    }
}
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$remoteOutput = $patch | & ssh -o BatchMode=yes newvps /srv/xiaoxun/deepseek-reviewer/run-review.sh
$sshExit = $LASTEXITCODE
$resultText = ($remoteOutput -join "`n").Trim()
try { $result = $resultText | ConvertFrom-Json -ErrorAction Stop }
catch { throw 'DeepSeek reviewer returned no valid JSON result.' }
$localLog = Join-Path $logs "deepseek-review-$stamp.json"
[IO.File]::WriteAllText($localLog, ($result | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
Write-Output ($result | ConvertTo-Json -Compress -Depth 8)
if ($sshExit -ne 0 -or $result.verdict -ne 'APPROVE') { exit 1 }
exit 0
