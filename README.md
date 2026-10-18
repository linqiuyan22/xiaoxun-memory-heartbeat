# 小寻记忆心跳 · isolated workbench

This repository contains the isolated development workbench for improving 小寻 memory retrieval. It contains source code and synthetic tests only.

## Safety boundary
- The formal ledger, raw conversation archives, API keys, relay credentials and production configuration are not stored here.
- Scheduled work must only operate inside `workbench/` and use synthetic fixtures.
- Retrieval may surface evidence; it must not overwrite or infer facts from recency alone.
- Any deployment to the VPS requires an independently reviewed backup, test result and explicit runtime verification.

## Layout
- `workbench/` — retrieval, relation and lifecycle source modules plus synthetic unit tests
- `reviewer/` — bounded DeepSeek review helper; no tool or write authority
- `run-heartbeat.ps1` — local supervisor entry point

## Validation
Run the focused Python unit suite from `workbench/`:
```powershell
python -B -m unittest discover -v -s tests
```

This repository is safe for GitHub and code-assistant indexing precisely because its ignored files exclude production results and run logs.
