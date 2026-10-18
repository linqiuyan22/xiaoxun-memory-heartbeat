#!/usr/bin/env python3
"""Append-only candidate review and Active Layer promotion.

The canonical ledger keeps source conversations, candidate records, and review
records.  Markdown fragments are a rebuildable Active Layer projection rather
than a second memory authority.  This module never changes a historical ledger
row: every approval or rejection is appended as a separate ``memory_review``
event with a source link to its candidate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import ledger_cli
from fragment_store import FragmentStore
from memory_reply_protocol import SECRET_PATTERNS

CANDIDATE_KINDS = ("fact_candidate", "reflection_candidate", "core_candidate")
DECISION_TO_STATUS = {
    "approve": "approved",
    "reject": "rejected",
    "archive": "archived",
    "auto": "approved_auto",
}
RISK_LEVELS = {"operational", "personal", "relationship", "identity"}
ACTIVE_LIMIT = 20


def _read_connection(ledger_db: Path | str) -> sqlite3.Connection:
    path = Path(ledger_db).resolve().as_posix()
    db = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    db.row_factory = sqlite3.Row
    return db


def _metadata(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = json.loads(row["metadata_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}


def _candidate_rows(ledger_db: Path | str) -> list[dict[str, Any]]:
    db = _read_connection(ledger_db)
    try:
        rows = db.execute(
            "SELECT event_id,source_id,conversation_id,ts,kind,content,visibility,metadata_json "
            "FROM events WHERE kind IN ('fact_candidate','reflection_candidate','core_candidate') "
            "ORDER BY ts DESC,event_id DESC"
        ).fetchall()
        reviews = db.execute(
            "SELECT event_id,ts,metadata_json FROM events WHERE kind='memory_review' "
            "ORDER BY ts ASC,event_id ASC"
        ).fetchall()
    finally:
        db.close()

    decisions: dict[str, dict[str, Any]] = {}
    for row in reviews:
        meta = _metadata(row)
        candidate_id = str(meta.get("reviewed_candidate_id") or "")
        status = str(meta.get("status") or "")
        if candidate_id and status in set(DECISION_TO_STATUS.values()):
            decisions[candidate_id] = {
                "status": status,
                "review_event_id": row["event_id"],
                "reviewed_at": row["ts"],
                "reviewer": str(meta.get("reviewer") or ""),
                "reason": str(meta.get("reason") or ""),
            }

    result: list[dict[str, Any]] = []
    for row in rows:
        meta = _metadata(row)
        candidate_id = str(meta.get("candidate_id") or row["source_id"])
        current = decisions.get(candidate_id, {})
        result.append({
            "event_id": row["event_id"],
            "candidate_id": candidate_id,
            "conversation_id": row["conversation_id"],
            "ts": row["ts"],
            "kind": row["kind"],
            "content": row["content"],
            "visibility": row["visibility"],
            "source_event_ids": list(meta.get("source_event_ids") or []),
            "confidence": meta.get("confidence"),
            "risk_level": str(meta.get("risk_level") or "personal"),
            "scene_gate": str(meta.get("scene_gate") or ""),
            "counterevidence": str(meta.get("counterevidence") or ""),
            "status": current.get("status", "candidate"),
            "review_event_id": current.get("review_event_id"),
            "reviewed_at": current.get("reviewed_at"),
            "reviewer": current.get("reviewer"),
            "review_reason": current.get("reason"),
        })
    return result


def list_candidates(ledger_db: Path | str, limit: int = 20) -> dict[str, Any]:
    if not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer 1..1000")
    rows = _candidate_rows(ledger_db)
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for row in rows:
        by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    return {
        "candidate_count": len(rows),
        "pending_count": by_status.get("candidate", 0),
        "by_kind": by_kind,
        "by_status": by_status,
        "recent": rows[:limit],
    }


def _get_candidate(ledger_db: Path | str, candidate_id: str) -> dict[str, Any]:
    if not isinstance(candidate_id, str) or not candidate_id.startswith("candidate:") or len(candidate_id) > 100:
        raise ValueError("candidate_id is invalid")
    for row in _candidate_rows(ledger_db):
        if row["candidate_id"] == candidate_id:
            return row
    raise FileNotFoundError("candidate not found")


def review_candidate(ledger_db: Path | str, candidate_id: str, decision: str,
                     *, reason: str = "", reviewer: str = "xiaoxun") -> dict[str, Any]:
    candidate = _get_candidate(ledger_db, candidate_id)
    if decision not in {"approve", "reject", "archive", "auto"}:
        raise ValueError("unsupported review decision")
    if decision == "auto" and candidate["kind"] != "reflection_candidate":
        raise ValueError("automatic review is limited to reflection candidates")
    if reviewer not in {"xiaoxun", "yanyan", "system_auto"}:
        raise ValueError("unsupported reviewer")
    reason = str(reason or "").strip()[:500]
    if any(pattern.search(reason) for pattern in SECRET_PATTERNS):
        raise ValueError("review reason looks like it contains a credential")
    status = DECISION_TO_STATUS[decision]
    identity = json.dumps({"candidate_id": candidate_id, "status": status,
                           "reason": reason, "reviewer": reviewer},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    source_id = "review:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    db = ledger_cli.connect(ledger_db)
    try:
        written = ledger_cli.append_self_event(
            db,
            content="Memory review %s for %s" % (status, candidate_id),
            kind="memory_review",
            visibility="private",
            source_id=source_id,
            metadata={
                "reviewed_candidate_id": candidate_id,
                "candidate_event_id": candidate["event_id"],
                "status": status,
                "reviewer": reviewer,
                "reason": reason,
            },
        )
    finally:
        db.close()
    return {
        "ok": True, "candidate_id": candidate_id, "decision": decision,
        "status": status, "review_event_id": written["event_id"],
        "duplicate": bool(written["duplicate"]),
    }


def _source_events(ledger_db: Path | str, ids: list[str]) -> list[dict[str, str]]:
    clean = [str(value) for value in ids if str(value).strip()]
    if not clean:
        return []
    placeholders = ",".join("?" for _ in clean)
    db = _read_connection(ledger_db)
    try:
        rows = db.execute(
            "SELECT event_id,conversation_id,actor,content FROM events WHERE event_id IN (" + placeholders + ")",
            clean,
        ).fetchall()
    finally:
        db.close()
    by_id = {row["event_id"]: row for row in rows}
    missing = [value for value in clean if value not in by_id]
    if missing:
        raise ValueError("candidate references source events that are unavailable")
    return [{"event_id": row["event_id"], "conversation_id": row["conversation_id"],
             "actor": row["actor"], "content": row["content"]} for row in (by_id[value] for value in clean)]


def activate_candidate(ledger_db: Path | str, fragment_root: Path | str, candidate_id: str,
                       *, priority: str = "medium") -> dict[str, Any]:
    candidate = _get_candidate(ledger_db, candidate_id)
    if candidate["status"] not in {"approved", "approved_auto"}:
        raise ValueError("candidate must be approved before it can enter Active Layer")
    if candidate["kind"] == "core_candidate":
        raise ValueError("core candidates do not enter Active Layer automatically")
    if priority not in {"high", "medium", "low"}:
        raise ValueError("priority must be high, medium, or low")
    sources = _source_events(ledger_db, candidate["source_event_ids"])
    if not sources:
        raise ValueError("candidate needs source_event_ids before activation")
    for row in sources:
        if any(pattern.search(row["content"]) for pattern in SECRET_PATTERNS):
            raise ValueError("candidate source includes a credential and cannot be projected")
    messages = []
    for row in sources[:4]:
        role = "assistant" if row["actor"] == "xiaoxun" else "user"
        messages.append({"role": role, "text": row["content"][:1200]})
    windows = list(dict.fromkeys(row["conversation_id"] for row in sources))[:10]
    store = FragmentStore(fragment_root)
    result = store.write({
        "source": "ledger-review",
        "source_window": candidate["conversation_id"],
        "visibility": candidate["visibility"],
        "status": "active",
        "priority": priority,
        "episode_id": candidate_id,
        "trigger": "approved candidate",
        "event_context": candidate["content"],
        "reason": "candidate %s was %s" % (candidate_id, candidate["status"]),
        "source_event_ids": candidate["source_event_ids"],
        "window_ids": windows,
        "messages": messages,
        "candidate_id": candidate_id,
    })
    active = store.list("active", top_n=50)
    archived = []
    for row in active[ACTIVE_LIMIT:]:
        archived.append(store.archive(row["fragment_id"])["fragment_id"])
    return {"ok": True, "candidate_id": candidate_id,
            "fragment_id": result["fragment_id"], "fragment_status": result["status"],
            "archived_fragment_ids": archived}


def _auto_eligible(candidate: dict[str, Any], ledger_db: Path | str) -> bool:
    if candidate["status"] != "candidate" or candidate["kind"] != "reflection_candidate":
        return False
    if candidate["risk_level"] != "operational" or candidate["counterevidence"]:
        return False
    try:
        confidence = float(candidate["confidence"])
    except (TypeError, ValueError):
        return False
    if confidence < 0.9 or len(set(candidate["source_event_ids"])) < 2:
        return False
    try:
        sources = _source_events(ledger_db, candidate["source_event_ids"])
    except ValueError:
        return False
    return len({row["conversation_id"] for row in sources}) >= 2


def reconcile(ledger_db: Path | str, fragment_root: Path | str, *, apply: bool = False) -> dict[str, Any]:
    rows = _candidate_rows(ledger_db)
    eligible = [row for row in rows if _auto_eligible(row, ledger_db)]
    result: dict[str, Any] = {"apply": bool(apply), "eligible_count": len(eligible),
                              "candidate_ids": [row["candidate_id"] for row in eligible],
                              "promoted": []}
    if not apply:
        return result
    for row in eligible:
        review = review_candidate(ledger_db, row["candidate_id"], "auto",
                                  reason="repeated operational evidence across independent conversations",
                                  reviewer="system_auto")
        activation = activate_candidate(ledger_db, fragment_root, row["candidate_id"])
        result["promoted"].append({"candidate_id": row["candidate_id"],
                                   "review_event_id": review["review_event_id"],
                                   "fragment_id": activation["fragment_id"]})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile approved Xiaoxun memory candidates into Active Layer")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--fragments", required=True)
    parser.add_argument("--apply", action="store_true", help="append automatic review records and create fragments")
    args = parser.parse_args()
    print(json.dumps(reconcile(args.ledger, args.fragments, apply=args.apply), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
