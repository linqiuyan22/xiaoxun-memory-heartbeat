# Codex Heartbeat Workbench — memory search

This is a disposable working copy of Python source files copied from `/srv/xiaoxun/core` on the VPS. It contains no ledger database, personal memory records, API keys, or production configuration. The live canonical ledger and `/srv/xiaoxun/memory` are outside this workbench and must never be read or changed by scheduled jobs.

## Rules for every run

- Work only inside this directory. Do not use SSH, SCP, remote commands, MCP memory tools, or deploy anything.
- Do not add or inspect personal memory data. Use synthetic SQLite fixtures only.
- Read `TASKS.md`, choose at most one small actionable task related to search reliability, stale-fact handling, or event linking, then update the task list and `RUNLOG.md`.
- Start by inspecting the current code and tests. Prefer small changes with meaningful tests. Run the relevant tests and record the exact command and result.
- Preserve original event evidence. Do not infer that newer automatically supersedes older; only explicit correction/supersession evidence can establish that.
- If a safe task cannot be completed or tests cannot run, report `BLOCKED` and explain the concrete reason. Do not claim completion without test output.
- Do not run Git commands or write under `.git`; the outer supervisor validates and records successful changes.
- Before finishing, write `HEARTBEAT-RESULT.json` with `status` (`PASS`, `BLOCKED`, or `NOOP`), a short summary, and the exact tests run. Keep the response concise.
