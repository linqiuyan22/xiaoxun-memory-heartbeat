#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Explicit-only fact correction relationships for read-only retrieval.

The append-only ledger keeps every original event.  A later row can *state*
that it supersedes an earlier event by putting a list of canonical event IDs
in ``metadata.supersedes_event_ids``.  Dates, reply links, and similar wording
are deliberately not treated as proof of a correction.
"""
from __future__ import annotations

import json
import re

EVENT_ID = re.compile(r"^evt-[0-9a-f]{32}$")
FIELD = "supersedes_event_ids"


def _get(row, name, default=""):
    return row.get(name, default) if isinstance(row, dict) else getattr(row, name, default)


def targets(row) -> tuple[str, ...]:
    """Return only well-formed, explicitly declared ledger targets."""
    metadata = _get(row, "metadata", None)
    if metadata is None:
        metadata = _get(row, "metadata_json", "")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return ()
    if not isinstance(metadata, dict):
        return ()
    declared = metadata.get(FIELD)
    if not isinstance(declared, list):
        return ()
    return tuple(item for item in declared
                 if isinstance(item, str) and EVENT_ID.fullmatch(item))


def index(rows) -> dict[str, tuple[str, ...]]:
    """Map an old event ID to its explicitly declared replacement event IDs."""
    result: dict[str, list[str]] = {}
    for row in rows:
        replacement = str(_get(row, "event_id", ""))
        if not EVENT_ID.fullmatch(replacement):
            continue
        for target in targets(row):
            result.setdefault(target, []).append(replacement)
    return {target: tuple(replacements) for target, replacements in result.items()}
