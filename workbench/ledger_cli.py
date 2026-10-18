#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Append-only memory event ledger.

This program is deliberately independent from xiaoxun-chat.  It stores raw
events only; generating summaries or changing a reply is a separate layer.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "ledger.db"
VALID_SOURCES = {"cc", "telegram", "self", "relay"}
VALID_VISIBILITY = {"normal", "private", "derived"}
REQUIRED = ("source", "source_id", "conversation_id", "ts", "actor", "kind", "visibility")


def event_id(source: str, source_id: str) -> str:
    """The identity follows the source record, never a text hash."""
    digest = hashlib.sha256((source + "\0" + source_id).encode("utf-8")).hexdigest()
    return "evt-" + digest[:32]


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(str(db_path or DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            actor TEXT NOT NULL,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            reply_to TEXT,
            visibility TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            ingested_at TEXT NOT NULL,
            UNIQUE(source, source_id)
        );
        CREATE INDEX IF NOT EXISTS events_source_ts ON events(source, ts);
        CREATE INDEX IF NOT EXISTS events_conversation_ts ON events(conversation_id, ts);
        CREATE INDEX IF NOT EXISTS events_visibility_ts ON events(visibility, ts);
        CREATE TABLE IF NOT EXISTS source_state (
            source TEXT NOT NULL,
            source_key TEXT NOT NULL,
            cursor_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(source, source_key)
        );
        """
    )
    return db


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def normalize(event: dict[str, Any]) -> dict[str, Any]:
    missing = [name for name in REQUIRED if event.get(name) in (None, "")]
    if missing:
        raise ValueError("missing " + ",".join(missing))
    source = str(event["source"])
    if source not in VALID_SOURCES:
        raise ValueError("unsupported source")
    visibility = str(event["visibility"])
    if visibility not in VALID_VISIBILITY:
        raise ValueError("unsupported visibility")
    ts = str(event["ts"])
    try:
        dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid ISO timestamp") from exc
    # Media-only Telegram rows legitimately have an empty text body.  Keep the
    # empty original and retain media details in metadata instead of inventing
    # text or dropping the event.
    content = str(event.get("content") or "")
    source_id = str(event["source_id"])
    metadata = event.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    return {
        "event_id": event_id(source, source_id),
        "source": source,
        "source_id": source_id,
        "conversation_id": str(event["conversation_id"]),
        "ts": ts,
        "actor": str(event["actor"]),
        "kind": str(event["kind"]),
        "content": content,
        "content_hash": content_hash(content),
        "reply_to": event.get("reply_to"),
        "visibility": visibility,
        "metadata_json": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        "ingested_at": utc_now(),
    }


def ingest_rows(db: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    stats = {"received": 0, "added": 0, "duplicate": 0, "conflict": 0, "bad": 0}
    insert = """
        INSERT INTO events (
          event_id, source, source_id, conversation_id, ts, actor, kind, content,
          content_hash, reply_to, visibility, metadata_json, ingested_at
        ) VALUES (
          :event_id, :source, :source_id, :conversation_id, :ts, :actor, :kind, :content,
          :content_hash, :reply_to, :visibility, :metadata_json, :ingested_at
        )
    """
    with db:
        for raw in rows:
            stats["received"] += 1
            try:
                event = normalize(raw)
            except (TypeError, ValueError):
                stats["bad"] += 1
                continue
            try:
                db.execute(insert, event)
                stats["added"] += 1
            except sqlite3.IntegrityError:
                old = db.execute(
                    "SELECT event_id, content_hash FROM events WHERE source=? AND source_id=?",
                    (event["source"], event["source_id"]),
                ).fetchone()
                if old and old["event_id"] == event["event_id"] and old["content_hash"] == event["content_hash"]:
                    stats["duplicate"] += 1
                else:
                    stats["conflict"] += 1
    return stats


def append_self_event(
    db: sqlite3.Connection,
    *,
    content: str,
    conversation_id: str = "xiaoxun:self",
    kind: str = "memory",
    visibility: str = "private",
    metadata: dict[str, Any] | None = None,
    source_id: str | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """Append one model-authored event to the canonical ledger.

    The caller may retry with ``source_id`` without creating a duplicate.  The
    journal file is only a human-readable projection and must not be the write
    authority.
    """
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content must be a non-empty string")
    source_id = source_id or ("self:" + __import__("uuid").uuid4().hex)
    row = normalize({
        "source": "self",
        "source_id": source_id,
        "conversation_id": conversation_id,
        "ts": ts or utc_now(),
        "actor": "xiaoxun",
        "kind": kind,
        "content": content.strip(),
        "visibility": visibility,
        "metadata": metadata or {},
    })
    try:
        with db:
            db.execute(
                """INSERT INTO events (
                    event_id, source, source_id, conversation_id, ts, actor, kind,
                    content, content_hash, reply_to, visibility, metadata_json, ingested_at
                ) VALUES (:event_id, :source, :source_id, :conversation_id, :ts,
                    :actor, :kind, :content, :content_hash, :reply_to,
                    :visibility, :metadata_json, :ingested_at)""",
                row,
            )
        return {"event_id": row["event_id"], "source_id": source_id, "duplicate": False}
    except sqlite3.IntegrityError:
        old = db.execute(
            "SELECT event_id, content_hash FROM events WHERE source=? AND source_id=?",
            ("self", source_id),
        ).fetchone()
        if old and old["event_id"] == row["event_id"] and old["content_hash"] == row["content_hash"]:
            return {"event_id": row["event_id"], "source_id": source_id, "duplicate": True}
        raise ValueError("self event source_id conflicts with existing content")


def state_get(db: sqlite3.Connection, source: str, source_key: str) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT cursor_json FROM source_state WHERE source=? AND source_key=?", (source, source_key)
    ).fetchone()
    return json.loads(row["cursor_json"]) if row else None


def state_put(db: sqlite3.Connection, source: str, source_key: str, cursor: dict[str, Any]) -> None:
    with db:
        db.execute(
            """INSERT INTO source_state(source, source_key, cursor_json, updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(source,source_key) DO UPDATE SET cursor_json=excluded.cursor_json,
                 updated_at=excluded.updated_at""",
            (source, source_key, json.dumps(cursor, ensure_ascii=False, sort_keys=True), utc_now()),
        )


def cmd_ingest(_: argparse.Namespace) -> int:
    rows = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({})
    print(json.dumps(ingest_rows(connect(), rows), ensure_ascii=False, sort_keys=True))
    return 0


def cmd_state_put(_: argparse.Namespace) -> int:
    db = connect()
    stats = {"received": 0, "updated": 0, "bad": 0}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        stats["received"] += 1
        try:
            row = json.loads(line)
            source = str(row["source"])
            source_key = str(row["source_key"])
            cursor = row["cursor"]
            if source not in VALID_SOURCES or not source_key or not isinstance(cursor, dict):
                raise ValueError
            state_put(db, source, source_key, cursor)
            stats["updated"] += 1
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            stats["bad"] += 1
    print(json.dumps(stats, ensure_ascii=False, sort_keys=True))
    return 0 if not stats["bad"] else 2


def cmd_query(args: argparse.Namespace) -> int:
    clauses, params = [], []
    for name in ("source", "conversation_id", "actor", "visibility"):
        value = getattr(args, name)
        if value:
            clauses.append(name + "=?")
            params.append(value)
    if args.grep:
        clauses.append("content LIKE ?")
        params.append("%" + args.grep + "%")
    if args.since:
        clauses.append("ts>=?")
        params.append(args.since)
    if args.until:
        clauses.append("ts<=?")
        params.append(args.until)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    total = connect().execute("SELECT count(*) FROM events" + where, params).fetchone()[0]
    # A direct query is normally asking for the most recent matching record.
    # Callers that need an older record can constrain --until explicitly.
    sql = "SELECT * FROM events" + where + " ORDER BY ts DESC, event_id DESC LIMIT ?"
    rows = connect().execute(sql, [*params, args.limit]).fetchall()
    for row in rows:
        data = dict(row)
        data["metadata"] = json.loads(data.pop("metadata_json"))
        print(json.dumps(data, ensure_ascii=False, sort_keys=True))
    print(json.dumps({"matched": total, "returned": len(rows)}, ensure_ascii=False))
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    db = connect()
    by_source = dict(db.execute("SELECT source,count(*) FROM events GROUP BY source").fetchall())
    print(json.dumps({"db": str(DB_PATH), "events": sum(by_source.values()), "by_source": by_source,
                      "source_cursors": db.execute("SELECT count(*) FROM source_state").fetchone()[0]}, ensure_ascii=False))
    return 0


def cmd_verify(_: argparse.Namespace) -> int:
    db = connect()
    checks = {
        "event_count": db.execute("SELECT count(*) FROM events").fetchone()[0],
        "duplicate_source_ids": db.execute(
            "SELECT count(*) FROM (SELECT source,source_id,count(*) n FROM events GROUP BY source,source_id HAVING n>1)"
        ).fetchone()[0],
        "invalid_content_hash": db.execute("SELECT count(*) FROM events WHERE length(content_hash) != 64").fetchone()[0],
        "missing_required": db.execute(
            "SELECT count(*) FROM events WHERE source='' OR source_id='' OR conversation_id='' OR ts=''"
        ).fetchone()[0],
        "sqlite_integrity": db.execute("PRAGMA integrity_check").fetchone()[0],
    }
    print(json.dumps(checks, ensure_ascii=False, sort_keys=True))
    return 0 if checks["duplicate_source_ids"] == 0 and checks["missing_required"] == 0 and checks["sqlite_integrity"] == "ok" else 2


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest")
    sub.add_parser("state-put")
    q = sub.add_parser("query")
    q.add_argument("--source", choices=sorted(VALID_SOURCES))
    q.add_argument("--conversation-id")
    q.add_argument("--actor")
    q.add_argument("--visibility", choices=sorted(VALID_VISIBILITY))
    q.add_argument("--grep")
    q.add_argument("--since")
    q.add_argument("--until")
    q.add_argument("--limit", type=int, default=20)
    sub.add_parser("stats")
    sub.add_parser("verify")
    args = ap.parse_args()
    return {"ingest": cmd_ingest, "state-put": cmd_state_put, "query": cmd_query,
            "stats": cmd_stats, "verify": cmd_verify}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
