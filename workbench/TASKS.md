# Work queue

## Active objective
Improve memory search so it can retrieve evidence expressed with different wording, respect explicit fact corrections, and connect important related events. Keep the formal ledger and personal records unchanged.

## Tasks
- [x] Inspect the copied search path and available test setup; add synthetic tests that reproduce current search limitations. Verified the recall adapter's 80-candidate cutoff with a synthetic boundary regression (2026-10-16); retrieval implementation unchanged.
- [x] Implement one bounded retrieval improvement in the isolated copy and verify it with synthetic tests. ASCII case-insensitive scoring now agrees with SQLite LIKE candidate matching; synthetic lower/upper/mixed-case queries preserve full-match ranking and original evidence (2026-10-16). The 80-candidate cutoff remains unchanged.
- [x] Add tests for explicit event links or correction evidence if the source schema supports them; do not equate recency with supersession. Synthetic reply_to regression verifies stored links, retrieval of original/reply/newer unlinked events, and unchanged evidence after search (2026-10-16).
- [ ] Compare baseline and changed retrieval on a small synthetic query set; record exact results and limitations.

## Rules
A heartbeat may complete at most one task. Reorder the list only with a short evidence-based reason. New tasks must be small and directly support the active objective.
- [x] Compare baseline and changed retrieval on a small synthetic query set; record exact results and limitations. Baseline: older exact two-term event was absent after 80 newer one-term candidates. Changed: same corpus returned 80 rows; older exact event ranked first, with 79 partial candidates retained. Separate same-score corpus kept the older human event above 80 newer OCR events. Full report in RUNLOG.md (2026-10-16).
- [ ] Add a small isolated semantic-synonym retrieval experiment and compare against the keyword baseline; preserve source citations and avoid sending private records to external embedding services.
- [x] Define and test explicit supersession evidence handling; `reply_to` alone remains a relationship signal, not proof that an earlier fact is stale. A synthetic four-event ledger confirmed that only `metadata.supersedes_event_ids` demotes the cited old fact, labels it as superseded, preserves its raw evidence, and leaves a reply plus a newer unlinked event unresolved (2026-10-19).
- [x] Improve the live canonical-event search's bounded candidate ranking without writing the ledger. The actual `memory_gateway.py` now ranks by term/exact-phrase score, actor, and recency before the 1000-row cap; isolated 1001-row and case-variant regressions pass. Backed up and deployed to VPS; MCP service restarted and live paraphrase retrieval returned the expected cross-end memory first (2026-10-16).
- [ ] Evaluate actual hybrid archive retrieval on a representative paraphrase/time-change set; preserve evidence and report misses before tuning semantic ranking.
- [x] Add a reusable, isolated DeepSeek review subagent to the heartbeat flow. It accepts workbench-only diffs <=24k chars, has no tools or write access, logs verdicts, and causes rollback unless approved. Synthetic API test passed; path guard and systemd file-isolation probes passed (2026-10-16).
