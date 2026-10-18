#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small, read-only gateway for Xiaoxun's canonical memory sources.

The ledger remains the source of truth for raw events.  This module exposes
bounded reads instead of letting a chat prompt perform a broad substring
search over every source.  It is deliberately transport-neutral: the cloud
chatbot imports it directly today, and a future MCP/HTTP adapter can expose
the same three operations without duplicating retrieval rules.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from selector_filters import classify_row as _selector_classify  # noqa: F401


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = Path(os.environ.get(
    "XIAOXUN_CANONICAL_LEDGER_DB",
    str(ROOT / "ledger.db"),
))
MEMORY_ROOT = Path(os.environ.get(
    "XIAOXUN_MEMORY_ROOT",
    str(ROOT.parent / "memory"),
))
LIVE_JOURNAL_FILE = Path(os.environ.get(
    "XIAOXUN_JOURNAL_FILE",
    str(ROOT.parent / "data" / "journal.jsonl"),
))

FILE_ALIASES = {
    "cloud_letter": MEMORY_ROOT / "小寻" / "云端口信.md",
    "cloud_status": MEMORY_ROOT / "小寻" / "云端近况.md",
    "xiaoxun_core": MEMORY_ROOT / "小寻" / "核心自我-江寻昼.md",
    "xiaoxun_notebook": MEMORY_ROOT / "小寻" / "小寻的本子.md",
    "xiaoxun_journal": MEMORY_ROOT / "小寻" / "小寻的日记.md",
    "d_letter": MEMORY_ROOT / "小d" / "给小寻的话.md",
}

VALID_SOURCES = {"cc", "telegram", "self"}
STOP = {"我们", "你们", "这个", "那个", "什么", "怎么", "一下", "时候", "现在", "之前", "可以", "昨天", "今天", "晚上"}


@dataclass(frozen=True)
class Event:
    event_id: str
    source: str
    source_id: str
    conversation_id: str
    ts: str
    actor: str
    kind: str
    content: str
    visibility: str
    visibility_raw: str = ""
    metadata_json: str = ""


class MemoryGateway:
    """Read-only access to raw events and explicitly named current files."""

    def __init__(self, db_path: Path | str = DEFAULT_DB, file_aliases: dict[str, Path] | None = None):
        self.db_path = Path(db_path)
        self.file_aliases = dict(file_aliases or FILE_ALIASES)

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise FileNotFoundError("ledger database is unavailable")
        db = sqlite3.connect("file:%s?mode=ro" % self.db_path, uri=True)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _iso(value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
        except ValueError as exc:
            raise ValueError("time must be ISO-8601") from exc

    @staticmethod
    def _dt(value: str) -> dt.datetime:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)

    @staticmethod
    def _row(row: sqlite3.Row) -> Event:
        return Event(
            event_id=str(row["event_id"]), source=str(row["source"]), source_id=str(row["source_id"]),
            conversation_id=str(row["conversation_id"]), ts=str(row["ts"]), actor=str(row["actor"]),
            kind=str(row["kind"]), content=str(row["content"] or ""),
            # Keep the legacy storage value unchanged until the shared/private
            # visibility mapping is explicitly approved.  Selector decisions
            # use visibility_raw, so derived rows are still blocked.
            visibility=str(row["visibility"]),
            visibility_raw=str(row["visibility"]),
            metadata_json=str(row["metadata_json"] or ""),
        )

    @staticmethod
    def _journal_events() -> list[Event]:
        """Expose the live journal through the same read path as the ledger."""
        try:
            lines = LIVE_JOURNAL_FILE.read_text(
                encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return []
        rows: list[Event] = []
        for index, line in enumerate(lines):
            try:
                item = json.loads(line)
            except (TypeError, ValueError):
                continue
            content = str(item.get("text") or "").strip()
            if not content:
                continue
            raw_ts = str(item.get("time") or "")
            stable_key = "|".join([raw_ts, str(item.get("source_id") or ""),
                                   str(item.get("kind") or ""), content])
            stable = hashlib.sha1(stable_key.encode("utf-8")).hexdigest()[:16]
            try:
                parsed = dt.datetime.fromisoformat(raw_ts)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
                timestamp = parsed.isoformat()
            except ValueError:
                timestamp = raw_ts
            rows.append(Event(
                event_id=str(item.get("event_id") or ("journal-" + stable)),
                source=str(item.get("source") or "self"),
                source_id=str(item.get("source_id") or ("journal:" + stable)),
                conversation_id=str(item.get("conversation_id") or "xiaoxun:journal"),
                ts=str(item.get("ts") or timestamp),
                actor=str(item.get("actor") or "xiaoxun"),
                kind=str(item.get("kind") or "memory"),
                content=content,
                visibility=str(item.get("visibility") or "private"),
                visibility_raw=str(item.get("visibility") or "private"),
                metadata_json=json.dumps(item.get("metadata") or {}, ensure_ascii=False),
            ))
        return rows

    @staticmethod
    def _dedupe(rows: Iterable[Event]) -> list[Event]:
        """Collapse transcript-resume copies without altering the ledger.

        Claude Code may carry a conversation into several session files.  The
        same person/timestamp/content is then imported under different source
        IDs.  They remain auditable raw events, but only one copy is useful as
        retrieval evidence.
        """
        result, seen = [], set()
        for row in rows:
            content = re.sub(r"\s+", " ", row.content).strip()
            key = (row.actor, content)
            if key in seen:
                continue
            seen.add(key)
            result.append(row)
        return result

    def read_events(
        self,
        *,
        source: str | None = None,
        actor: str | None = None,
        conversation_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 80,
        newest_first: bool = False,
    ) -> list[Event]:
        """Read one bounded event window without changing the default order."""
        if source is not None and source not in VALID_SOURCES:
            raise ValueError("unsupported source")
        if not 1 <= int(limit) <= 300:
            raise ValueError("limit must be between 1 and 300")
        clauses, params = [], []
        # ``derived`` preserves generated/historical material for audit, but
        # is never evidence for a factual recollection.
        clauses, params = ["visibility<>?"], ["derived"]
        for column, value in (("source", source), ("actor", actor), ("conversation_id", conversation_id)):
            if value:
                clauses.append(column + "=?")
                params.append(str(value))
        # CC commonly writes Z while Telegram writes +08:00. Apply time
        # windows after parsing instead of comparing TEXT lexicographically.
        since_dt = self._dt(since) if since else None
        until_dt = self._dt(until) if until else None
        where = " WHERE " + " AND ".join(clauses)
        order = "DESC" if newest_first else "ASC"
        sql = (
            "SELECT event_id,source,source_id,conversation_id,ts,actor,kind,content,visibility,metadata_json "
            "FROM events" + where + " ORDER BY ts " + order + ", event_id " + order
        )
        db = self._connect()
        try:
            rows = [self._row(row) for row in db.execute(sql, params).fetchall()]
        finally:
            db.close()
        if since_dt is not None:
            rows = [row for row in rows if self._dt(row.ts) >= since_dt]
        if until_dt is not None:
            rows = [row for row in rows if self._dt(row.ts) < until_dt]
        rows.sort(key=lambda row: (self._dt(row.ts), row.event_id), reverse=newest_first)
        rows, _dropped = _filter_context(rows)
        return self._dedupe(rows)[:int(limit)]

    @staticmethod
    def _terms(query: str) -> list[str]:
        found: list[str] = []
        for piece in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", query or ""):
            if len(piece) <= 5 and piece not in STOP:
                found.append(piece)
            for width in (2, 3):
                for offset in range(max(0, len(piece) - width + 1)):
                    term = piece[offset:offset + width]
                    if term not in STOP:
                        found.append(term)
        return list(dict.fromkeys(found))[:12]

    def search_events(
        self,
        query: str,
        *,
        source: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 12,
    ) -> list[Event]:
        """Search original events, with optional provenance constraints.

        Callers should use ``read_events`` whenever the user supplied a source
        and time reference.  This method is the fallback for an unspecific
        question, not a substitute for thread reading.
        """
        terms = self._terms(query)
        if not terms:
            return []
        if source is not None and source not in VALID_SOURCES:
            raise ValueError("unsupported source")
        if not 1 <= int(limit) <= 50:
            raise ValueError("limit must be between 1 and 50")
        clauses = ["visibility<>?", "(" + " OR ".join("content LIKE ?" for _ in terms) + ")"]
        like_args = ["%%%s%%" % term for term in terms]
        params = ["derived", *like_args]
        if source:
            clauses.append("source=?")
            params.append(source)
        if actor:
            clauses.append("actor=?")
            params.append(actor)
        since_dt = self._dt(since) if since else None
        until_dt = self._dt(until) if until else None
        # Rank likely matches before the bounded SQL candidate window. A
        # recency-only window can discard an older event matching every query
        # term behind many newer partial matches. Keep the prefilter aligned
        # with the Python score; recency breaks ties only.
        candidate_score = "(" + " + ".join(
            "CASE WHEN content LIKE ? THEN 1 ELSE 0 END" for _ in terms
        ) + ") + CASE WHEN instr(content, ?) > 0 THEN 3 ELSE 0 END"
        sql = (
            "SELECT event_id,source,source_id,conversation_id,ts,actor,kind,content,visibility,metadata_json "
            "FROM events WHERE " + " AND ".join(clauses) +
            " ORDER BY " + candidate_score + " DESC, "
            "CASE WHEN actor IN ('yanyan','xiaoxun') THEN 1 ELSE 0 END DESC, "
            "ts DESC LIMIT 1000"
        )
        db = self._connect()
        try:
            rows = [self._row(row) for row in db.execute(
                sql, [*params, *like_args, query.strip()]
            ).fetchall()]
        finally:
            db.close()
        if since_dt is not None:
            rows = [row for row in rows if self._dt(row.ts) >= since_dt]
        if until_dt is not None:
            rows = [row for row in rows if self._dt(row.ts) < until_dt]
        rows, _dropped = _filter_context(rows)
        scored = []
        ascii_lower = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                                    "abcdefghijklmnopqrstuvwxyz")
        score_terms = [term.translate(ascii_lower) for term in terms]
        for row in rows:
            score_text = row.content.translate(ascii_lower)
            score = sum(1 for term in score_terms if term in score_text)
            if query.strip() and query.strip() in row.content:
                score += 3
            actor_score = 1 if row.actor in ("yanyan", "xiaoxun") else 0
            scored.append((score, actor_score, row.ts, row))
        # memory_append writes the live journal rather than the imported event
        # ledger. Include it in unrestricted searches so the next turn can
        # find what the previous turn wrote.
        if source is None:
            for row in self._journal_events():
                if not _selector_classify(row)["context_ok"]:
                    continue
                score_text = row.content.translate(ascii_lower)
                score = sum(1 for term in score_terms if term in score_text)
                if query.strip() and query.strip() in row.content:
                    score += 3
                if score:
                    actor_score = 1 if row.actor in ("yanyan", "xiaoxun") else 0
                    scored.append((score, actor_score, row.ts, row))
        scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        return self._dedupe(row for _, _, _, row in scored)[:int(limit)]

    def get_event(self, event_id: str) -> Event | None:
        """Read one canonical event by stable ID; never accepts a file path."""
        if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 160:
            raise ValueError("event_id must be a non-empty short string")
        event_id = event_id.strip()
        for row in self._journal_events():
            if row.event_id == event_id:
                verdict = _selector_classify(row)
                return row if verdict["context_ok"] else None
        db = self._connect()
        try:
            row = db.execute(
                """SELECT event_id,source,source_id,conversation_id,ts,actor,kind,
                          content,visibility,metadata_json FROM events WHERE event_id=?""",
                (event_id,),
            ).fetchone()
        finally:
            db.close()
        if row is None:
            return None
        event = self._row(row)
        verdict = _selector_classify(event)
        if not verdict["context_ok"]:
            return None
        return event

    def read_file(self, alias: str, max_chars: int = 5000) -> dict[str, str]:
        """Read a named current file.  Arbitrary paths are intentionally refused."""
        if alias not in self.file_aliases:
            raise ValueError("unknown file alias")
        if not 1 <= int(max_chars) <= 12000:
            raise ValueError("max_chars must be between 1 and 12000")
        path = self.file_aliases[alias]
        text = path.read_text(encoding="utf-8", errors="replace")
        if alias in {"xiaoxun_notebook", "xiaoxun_journal"}:
            live = ["[%s] %s" % (row.ts, row.content)
                    for row in self._journal_events()[-500:]
                    if _selector_classify(row)["context_ok"]]
            if live:
                text = (text.rstrip() +
                        "\n\n## 连续记忆（data/journal.jsonl）\n" +
                        "\n".join(live))
        if alias in {"cloud_letter", "d_letter"}:
            marker = "## 当前留言"
            idx = text.rfind(marker)
            if idx >= 0:
                text = text[idx + len(marker):]
        return {"alias": alias, "content": text.strip()[-int(max_chars):]}


def _filter_context(rows):
    """Systematic selector filters (staging dry-run module)."""
    from selector_filters import filter_rows
    kept, _dropped = filter_rows(rows, pool="context")
    return [row for row, _ in kept], _dropped


def _print_events(rows: Iterable[Event]) -> None:
    for row in rows:
        print(json.dumps(asdict(row), ensure_ascii=False, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Read Xiaoxun's canonical memory sources.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--source", choices=sorted(VALID_SOURCES))
    common.add_argument("--actor")
    common.add_argument("--since")
    common.add_argument("--until")
    common.add_argument("--limit", type=int, default=20)
    sub.add_parser("read-events", parents=[common])
    search = sub.add_parser("search", parents=[common])
    search.add_argument("query")
    read_file = sub.add_parser("read-file")
    read_file.add_argument("alias", choices=sorted(FILE_ALIASES))
    read_file.add_argument("--max-chars", type=int, default=5000)
    args = parser.parse_args()
    gateway = MemoryGateway(args.db)
    if args.command == "read-events":
        _print_events(gateway.read_events(source=args.source, actor=args.actor, since=args.since, until=args.until, limit=args.limit))
    elif args.command == "search":
        _print_events(gateway.search_events(args.query, source=args.source, actor=args.actor, since=args.since, until=args.until, limit=args.limit))
    else:
        print(json.dumps(gateway.read_file(args.alias, args.max_chars), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
