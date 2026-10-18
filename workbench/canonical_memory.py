#!/usr/bin/env python3
"""Canonical self-memory append shared by the broker and MCP adapter.

The existing journal format is intentionally kept as a projection: ``time``
and ``text`` remain present so older readers continue to work.  New records
also carry the event-ledger fields and, when the journal is the configured
production journal, are ingested into the same ``ledger.db``.  A temporary
test journal never writes the repository database.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_JOURNAL = Path(os.environ.get(
    "XIAOXUN_JOURNAL_FILE",
    str(ROOT.parent / "data" / "journal.jsonl"),
))
DEFAULT_LEDGER = Path(os.environ.get(
    "XIAOXUN_CANONICAL_LEDGER_DB",
    str(ROOT / "ledger.db"),
))
JOURNAL_MAX_LINES = 500
JOURNAL_TEXT_MAX_CHARS = 300
BEIJING = dt.timezone(dt.timedelta(hours=8))
_LEDGER_LOCK = threading.Lock()


def _event_id(source_id: str) -> str:
    # Keep the projection ID identical to ledger_cli.event_id("self", ...).
    # Otherwise a journal read and the canonical row would describe the same
    # event with two different IDs.
    digest = hashlib.sha256(("self\0" + source_id).encode("utf-8")).hexdigest()
    return "evt-" + digest[:32]


def _timestamp(now: dt.datetime | None = None) -> str:
    value = now or dt.datetime.now(BEIJING)
    if value.tzinfo is None:
        value = value.replace(tzinfo=BEIJING)
    return value.isoformat(timespec="seconds")


def build_event(content: str, *, now: dt.datetime | None = None,
                source_id: str | None = None, kind: str = "memory",
                visibility: str = "private", metadata: dict[str, Any] | None = None,
                max_chars: int = JOURNAL_TEXT_MAX_CHARS) -> dict[str, Any]:
    """Build one normalized self-observation with a stable persisted ID."""
    text = str(content or "").strip()[:int(max_chars)]
    if not text:
        raise ValueError("content must be non-empty")
    timestamp = _timestamp(now)
    sid = str(source_id or ("self:" + uuid.uuid4().hex))
    return {
        "event_id": _event_id(sid),
        # ``self`` is the ledger's existing source label for Xiaoxun's own
        # observations; journal.jsonl remains the human-readable projection.
        "source": "self",
        "source_id": sid,
        "conversation_id": "xiaoxun:self",
        "ts": timestamp,
        "actor": "xiaoxun",
        "kind": kind,
        "visibility": visibility,
        "content": text,
        "metadata": dict(metadata or {}),
        # Backward-compatible journal projection fields.
        "time": timestamp[:16].replace("T", " "),
        "text": text,
    }


def _ledger_enabled(journal: Path, ledger_db: Path | None) -> Path | None:
    if ledger_db is not None:
        return Path(ledger_db)
    # A caller-supplied temporary journal is a test fixture.  Only the
    # configured production journal may create the canonical ledger side.
    try:
        same = journal.resolve() == DEFAULT_JOURNAL.resolve()
    except OSError:
        same = journal == DEFAULT_JOURNAL
    return DEFAULT_LEDGER if same else None


def _ingest_ledger(event: dict[str, Any], ledger_db: Path) -> dict[str, int]:
    """Use the existing ledger normalizer/ingest path; never create a second DB."""
    import ledger_cli

    with _LEDGER_LOCK:
        db = ledger_cli.connect(ledger_db)
        try:
            row = dict(event)
            row.pop("time", None)
            row.pop("text", None)
            metadata = dict(row.get("metadata") or {})
            metadata.update({"projection": "journal.jsonl", "self_memory": True})
            row["metadata"] = metadata
            stats = ledger_cli.ingest_rows(db, [row])
        finally:
            db.close()
    return stats


def append_event(journal_file: Path | str, content: str, *,
                 ledger_db: Path | str | None = None,
                 max_lines: int = JOURNAL_MAX_LINES) -> dict[str, Any]:
    """Append a canonical event and return it with ledger/projection status."""
    journal = Path(journal_file)
    event = build_event(content)
    ledger_path = _ledger_enabled(journal, Path(ledger_db) if ledger_db else None)
    ledger_stats = None
    if ledger_path is not None:
        ledger_stats = _ingest_ledger(event, ledger_path)
        if ledger_stats.get("bad") or ledger_stats.get("conflict"):
            raise RuntimeError("canonical ledger rejected self event: %s" % ledger_stats)

    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    projection_warning = None
    try:
        lines = [line for line in journal.read_text(encoding="utf-8", errors="ignore").splitlines()
                 if line.strip()]
        if len(lines) > int(max_lines):
            lines = lines[-int(max_lines):]
            journal.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as exc:
        # The append is durable even when post-write housekeeping fails.  Keep
        # the warning visible to the broker/model instead of claiming success.
        lines = []
        projection_warning = "%s: %s" % (type(exc).__name__, str(exc)[:160])
    return {
        "event": event,
        "journal_file": journal.name,
        "ledger_db": str(ledger_path) if ledger_path else None,
        "ledger": ledger_stats,
        "total_lines": len(lines),
        "post_write_warning": projection_warning,
    }


def append_candidate(content: str, *, kind: str, operation: str,
                     source_event_ids: list[str] | None = None,
                     confidence: float | None = None,
                     scene_gate: str = "",
                     counterevidence: str = "",
                     visibility: str = "private",
                     risk_level: str = "personal",
                     ledger_db: Path | str | None = None) -> dict[str, Any]:
    """Append a candidate record to the canonical ledger, never auto-approve it."""
    if kind not in {"fact_candidate", "reflection_candidate", "core_candidate"}:
        raise ValueError("unsupported candidate kind")
    if operation not in {"hold", "grow", "propose"}:
        raise ValueError("unsupported candidate operation")
    if visibility not in {"private", "shared"}:
        raise ValueError("unsupported visibility")
    risk = str(risk_level or "personal").strip()
    if risk not in {"operational", "personal", "relationship", "identity"}:
        raise ValueError("unsupported risk_level")
    ids = [str(item) for item in (source_event_ids or []) if str(item).strip()][:20]
    if confidence is not None and not 0 <= float(confidence) <= 1:
        raise ValueError("confidence must be between 0 and 1")
    # The candidate identity is deterministic for retries: re-sending the
    # same statement/evidence updates nothing and is reported as duplicate.
    identity = json.dumps({"kind": kind, "content": str(content).strip(),
                           "source_event_ids": ids}, ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"))
    source_id = "candidate:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    metadata = {
        "candidate_id": source_id,
        "operation": operation,
        "status": "candidate",
        "source_event_ids": ids,
        "confidence": None if confidence is None else float(confidence),
        "scene_gate": str(scene_gate or "")[:300],
        "counterevidence": str(counterevidence or "")[:500],
        "revision_of": None,
        "risk_level": risk,
    }
    event = build_event(str(content).strip()[:1000], source_id=source_id,
                        kind=kind, visibility=visibility, metadata=metadata, max_chars=1000)
    path = Path(ledger_db) if ledger_db is not None else DEFAULT_LEDGER
    stats = _ingest_ledger(event, path)
    return {"ok": stats.get("bad", 0) == 0 and stats.get("conflict", 0) == 0,
            "candidate_id": source_id, "event_id": event["event_id"],
            "kind": kind, "operation": operation, "status": "candidate",
            "ledger": stats}

