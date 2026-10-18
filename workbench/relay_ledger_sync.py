#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Copy raw PWA relay messages into the append-only memory ledger.

This is intentionally a narrow, lossless transport layer.  It never asks a
model to summarize, score, or promote a conversation into a lasting memory.
Each source message keeps its relay id and PWA session id, so repeats are
safe and a later memory layer can trace every fragment back to its origin.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import ledger_cli


SOURCE = "relay"
SOURCE_KEY_PREFIX = "relay-db:"
EXCLUDED_KINDS = frozenset({"thinking"})
VALID_DIRECTIONS = frozenset({"in", "out"})
DEFAULT_LIMIT = 250
MAX_LIMIT = 1000


def readonly_connect(path: Path) -> sqlite3.Connection:
    """Open the PWA store without ever taking write capability."""
    uri = "file:" + quote(str(path.resolve()).replace("\\", "/"), safe="/:" ) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def parse_meta(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def session_id(meta: dict[str, Any]) -> str:
    value = meta.get("api_session")
    return str(value).strip() if value is not None and str(value).strip() else "__legacy__"


def source_key(path: Path) -> str:
    return SOURCE_KEY_PREFIX + str(path.resolve())


def to_ledger_event(row: sqlite3.Row) -> dict[str, Any] | None:
    direction = str(row["direction"] or "")
    kind = str(row["kind"] or "")
    text = str(row["text"] or "")
    if direction not in VALID_DIRECTIONS or kind in EXCLUDED_KINDS or not text:
        return None
    meta = parse_meta(row["meta"])
    current_session = session_id(meta)
    return {
        "source": SOURCE,
        "source_id": f"relay:{int(row['id'])}",
        "conversation_id": f"relay:{current_session}",
        "ts": str(row["ts"]),
        "actor": "yanyan" if direction == "in" else "xiaoxun",
        "kind": "message",
        "content": text,
        "visibility": "private",
        "metadata": {
            "relay_message_id": int(row["id"]),
            "relay_direction": direction,
            "relay_kind": kind,
            "api_session": current_session,
        },
    }


def state_cursor(db: sqlite3.Connection, key: str) -> int:
    state = ledger_cli.state_get(db, SOURCE, key) or {}
    try:
        return max(0, int(state.get("last_id", 0)))
    except (TypeError, ValueError):
        return 0


def sync(*, relay_db: Path, ledger_db: Path, limit: int = DEFAULT_LIMIT, dry_run: bool = False) -> dict[str, Any]:
    """Ingest a bounded, idempotent page and advance its cursor on success."""
    if not relay_db.is_file():
        raise FileNotFoundError(f"relay database does not exist: {relay_db}")
    limit = min(max(int(limit), 1), MAX_LIMIT)
    key = source_key(relay_db)
    ledger = ledger_cli.connect(ledger_db)
    try:
        before = state_cursor(ledger, key)
        relay = readonly_connect(relay_db)
        try:
            rows = relay.execute(
                "SELECT id, ts, direction, kind, text, meta FROM messages "
                "WHERE id > ? ORDER BY id ASC LIMIT ?",
                (before, limit),
            ).fetchall()
        finally:
            relay.close()
        result: dict[str, Any] = {
            "source": SOURCE,
            "cursor_from": before,
            "read": len(rows),
            "eligible": 0,
            "skipped": {"thinking": 0, "empty": 0, "unsupported": 0},
            "dry_run": bool(dry_run),
        }
        events: list[dict[str, Any]] = []
        for row in rows:
            event = to_ledger_event(row)
            if event is None:
                if str(row["kind"] or "") in EXCLUDED_KINDS:
                    result["skipped"]["thinking"] += 1
                elif not str(row["text"] or ""):
                    result["skipped"]["empty"] += 1
                else:
                    result["skipped"]["unsupported"] += 1
                continue
            events.append(event)
        result["eligible"] = len(events)
        if dry_run:
            result["cursor_to"] = before if not rows else int(rows[-1]["id"])
            result["ingest"] = {"received": len(events), "added": 0, "duplicate": 0, "conflict": 0, "bad": 0}
            return result

        stats = ledger_cli.ingest_rows(ledger, events)
        result["ingest"] = stats
        if stats["bad"] or stats["conflict"]:
            # Never move past an entry that was not safely represented.
            result["cursor_to"] = before
            result["state_advanced"] = False
            return result
        after = before if not rows else int(rows[-1]["id"])
        ledger_cli.state_put(ledger, SOURCE, key, {"last_id": after})
        result["cursor_to"] = after
        result["state_advanced"] = True
        return result
    finally:
        ledger.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="sync PWA relay rows into the canonical raw ledger")
    parser.add_argument("--relay-db", default=os.environ.get("RELAY_DB"), help="path to the PWA relay SQLite database")
    parser.add_argument("--ledger-db", default=str(ledger_cli.DB_PATH), help="path to the canonical ledger SQLite database")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"rows per run (1–{MAX_LIMIT})")
    parser.add_argument("--dry-run", action="store_true", help="report counts without writing the ledger or cursor")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.relay_db:
        print(json.dumps({"error": "--relay-db or RELAY_DB is required"}, ensure_ascii=False), file=sys.stderr)
        return 2
    try:
        result = sync(
            relay_db=Path(args.relay_db),
            ledger_db=Path(args.ledger_db),
            limit=args.limit,
            dry_run=args.dry_run,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 2 if result["ingest"]["bad"] or result["ingest"]["conflict"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
