#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only candidate retrieval from the unified event ledger.

This is deliberately not a memory writer and does not call an LLM.  The
chatbot can use it only behind LEDGER_RECALL=on for an explicit [[回忆: ...]]
request.  Its result keeps source and actor labels so a model can distinguish
desktop and Telegram records instead of blending them into a false memory.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from selector_filters import classify_row as _selector_classify
from supersession import index as _supersession_index


DB_PATH = Path(__file__).resolve().with_name("ledger.db")
STOP = {"我们", "你们", "这个", "那个", "什么", "怎么", "一下", "时候", "现在", "之前", "可以"}


def terms(query: str) -> list[str]:
    found: list[str] = []
    for part in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", query or ""):
        if len(part) <= 5 and part not in STOP:
            found.append(part)
        for width in (2, 3):
            for pos in range(max(0, len(part) - width + 1)):
                token = part[pos:pos + width]
                if token not in STOP:
                    found.append(token)
    return list(dict.fromkeys(found))[:12]


def search(query: str, limit: int = 6) -> list[dict]:
    """Return scored raw-event candidates in the old recall_raw shape."""
    needles = terms(query)
    if not needles or not DB_PATH.exists():
        return []
    where = " OR ".join("content LIKE ?" for _ in needles)
    like_args = ["%" + token + "%" for token in needles]
    # Rank candidates before applying the bounded SQL window. A recency-only
    # window can discard an older event matching every term when newer events
    # match just one. Keep recency as a tie-breaker, not the primary sort.
    candidate_score = " + ".join(
        "CASE WHEN content LIKE ? THEN 1 ELSE 0 END" for _ in needles
    )
    args = ["derived", *like_args, *like_args]
    db = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True)
    try:
        rows = db.execute(
            "SELECT ts,source,source_id,actor,kind,visibility,content,event_id,metadata_json "
            "FROM events WHERE visibility<>? AND (" + where + ")" +
            " ORDER BY (" + candidate_score + ") DESC, "
            "CASE WHEN actor IN ('yanyan','xiaoxun') THEN 1 ELSE 0 END DESC, "
            "ts DESC LIMIT 80", args
        ).fetchall()
    finally:
        db.close()
    filtered = []
    for ts, source, source_id, actor, kind, visibility, content, eid, meta in rows:
        if not source_id:
            raise RuntimeError(
                "lineage missing on row %s; refusing to return as normal event" % eid)
        verdict = _selector_classify({"event_id": eid, "source_id": source_id,
                                      "actor": actor, "kind": kind, "content": content,
                                      "visibility": visibility, "metadata_json": meta})
        if not verdict["context_ok"]:
            continue
        filtered.append((ts, source, actor, visibility, content, eid,
                         source_id, kind, meta))
    superseded_by = _supersession_index({
        "event_id": eid, "metadata_json": meta
    } for _, _, _, _, _, eid, _, _, meta in filtered)
    scored = []
    ascii_lower = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                                "abcdefghijklmnopqrstuvwxyz")
    score_needles = [token.translate(ascii_lower) for token in needles]
    for ts, source, actor, visibility, content, eid, source_id, kind, meta in filtered:
        text = str(content or "")
        # SQLite LIKE matches ASCII case-insensitively by default. Score those
        # same matches consistently, without changing the returned evidence.
        score_text = text.translate(ascii_lower)
        hit = sum(1 for token in score_needles if token in score_text)
        actor_score = 1 if actor in ("yanyan", "xiaoxun") else 0
        # Keep system/OCR events queryable but do not let an OCR transcription
        # outrank the actual people who said the matching words.
        # An old event remains evidence, but an explicit replacement takes
        # precedence for a current-fact question.  Nothing else can demote it.
        current_score = 0 if eid in superseded_by else 1
        scored.append((current_score, hit, actor_score, str(ts), source, actor,
                       visibility, text, eid, source_id, kind, meta))
    scored.sort(key=lambda row: (row[0], row[1], row[2], row[3]), reverse=True)
    result = []
    for _, _, _, ts, source, actor, visibility, text, eid, source_id, kind, meta in scored[:limit]:
        result.append({
            "ts": ts,
            "part": "ledger",
            "src": "ledger:" + source,
            "text": "[%s | %s | %s] %s" % (source, actor, visibility, text),
            "event_id": eid,
            "source_id": source_id,
            "kind": kind,
            "metadata_json": meta,
            "fact_status": "superseded" if eid in superseded_by else "current_or_unresolved",
            "superseded_by": list(superseded_by.get(eid, ())),
        })
    return result


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--limit", type=int, default=6)
    parsed = parser.parse_args()
    print(json.dumps(search(parsed.query, parsed.limit), ensure_ascii=False, indent=2))
