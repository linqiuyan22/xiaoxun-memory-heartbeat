#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Systematic selector filters (1001 engineering dry-run, v0.3).

Every retrieval row passes through here before scoring/injection:

- hard excludes (both context and fact pools): subagent logs, compaction
  summaries, quarantined events (curated layer), legacy ``derived`` rows
  (judged on the RAW stored value via ``visibility_raw``, never on the
  mapped display value), and unapproved *_candidate records (review fix #1:
  a candidate is not an approved memory; only explicit approved status may
  pass).
- fact pool requires ``actor == yanyan``: assistant/self/system rows may be
  context but can never serve as user quotes.

Failure policy (loud, never silent): a missing or malformed quarantine file
raises ConfigError instead of defaulting to "no quarantine".
"""
from __future__ import annotations

import json
from pathlib import Path

QUARANTINE_FILE = Path(__file__).resolve().with_name("selector_quarantine_v0.1.json")

COMPACT_MARKERS = (
    "session is being continued",
    "ran out of context",
    "conversation summary",
    "compact summary",
)

HARD_EXCLUDE_REASONS = ("subagent_log", "compaction_summary", "quarantine",
                        "legacy_derived", "unapproved_candidate")

APPROVED_STATUSES = ("approved", "approved_policy")


class ConfigError(RuntimeError):
    """Raised when the filter cannot run with its required config."""


def load_quarantine(path=QUARANTINE_FILE) -> dict:
    path = Path(path)
    if not path.exists():
        raise ConfigError("quarantine file missing: %s" % path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError("quarantine file unreadable: %s" % exc)
    if not isinstance(data, dict) or "source_ids" not in data or "event_ids" not in data:
        raise ConfigError("quarantine file malformed: need source_ids/event_ids")
    return {"source_ids": set(map(str, data["source_ids"])),
            "event_ids": set(map(str, data["event_ids"]))}


def _get(row, name, default=""):
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _meta_dict(row) -> dict:
    meta = _get(row, "metadata", None)
    if meta is None:
        meta = _get(row, "metadata_json", "")
    if isinstance(meta, dict):
        return meta
    if isinstance(meta, str) and meta.strip():
        try:
            parsed = json.loads(meta)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def classify_row(row, quarantine=None) -> dict:
    q = quarantine if quarantine is not None else load_quarantine()
    source_id = str(_get(row, "source_id"))
    event_id = str(_get(row, "event_id"))
    meta = _get(row, "metadata", None)
    if meta is None:
        meta = _get(row, "metadata_json", "")
    if not isinstance(meta, str):
        meta = json.dumps(meta, ensure_ascii=False)
    content = str(_get(row, "content"))
    actor = str(_get(row, "actor"))
    visibility = str(_get(row, "visibility"))
    # Reviewer fix: judge the RAW stored value.  Callers may map the display
    # value (derived -> private) but must keep ``visibility_raw`` alongside.
    raw_visibility = str(_get(row, "visibility_raw") or visibility)

    lower = content.lstrip().lower()
    exclude = []
    if "/subagents/" in source_id or "/subagents/" in meta:
        exclude.append("subagent_log")
    if any(marker in lower for marker in COMPACT_MARKERS):
        exclude.append("compaction_summary")
    if source_id in q["source_ids"] or event_id in q["event_ids"]:
        exclude.append("quarantine")
    if visibility == "derived" or raw_visibility == "derived":
        exclude.append("legacy_derived")
    if str(_get(row, "kind")).endswith("_candidate"):
        status = str(_meta_dict(row).get("status") or "").strip().lower()
        if status not in APPROVED_STATUSES:
            exclude.append("unapproved_candidate")

    fact_gaps = []
    if actor != "yanyan":
        fact_gaps.append("not_user_quote")

    return {
        "context_ok": not exclude,
        "fact_ok": (not exclude) and actor == "yanyan",
        "hard_excludes": exclude,
        "fact_gaps": fact_gaps,
    }


def filter_rows(rows, pool="context", quarantine=None):
    q = quarantine if quarantine is not None else load_quarantine()
    kept, dropped = [], []
    for row in rows:
        verdict = classify_row(row, q)
        ok = verdict["context_ok"] if pool == "context" else verdict["fact_ok"]
        (kept if ok else dropped).append((row, verdict))
    return kept, dropped
