# DeepSeek review subagent

This is a deliberately narrow child agent for independent code review. It has no tools: it receives a Git diff as text, calls DeepSeek's chat API once, returns a small JSON verdict, and records the verdict without storing the patch. Its transient systemd process runs as `xcore`, with a private `/tmp`, a read-only filesystem, and the memory/data directories inaccessible.

The Windows launcher accepts only diffs whose every path is under `workbench/`, and caps input at 24,000 characters. The daily heartbeat invokes it after local tests and rolls back workbench changes unless the child returns `APPROVE`. This subagent reviews; it does not edit code or access the official ledger.

To review a synthetic workbench diff manually from PowerShell:

```powershell
git diff -- workbench -- '*.py' | pwsh.exe -NoProfile -File .\reviewer\review-patch.ps1
```

Results are recorded under local `run-logs/` and remote `/srv/xiaoxun/deepseek-reviewer/reviews/`. The DeepSeek API key is read from a root-only, DeepSeek-only systemd environment file; it is not stored in this repository or passed as a command-line argument.
