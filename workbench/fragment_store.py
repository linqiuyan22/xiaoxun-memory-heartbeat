#!/usr/bin/env python3
"""Append-first Markdown storage for Active Layer fragments.

Fragments are a bounded, human-readable projection of canonical ledger events;
they are not a second event database.  Every fragment keeps source event IDs
and original message text so a later review can trace it back to the ledger.
The store creates new files atomically, refuses an idempotency collision with
different content, and archives by changing only the status frontmatter.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable


BEIJING = dt.timezone(dt.timedelta(hours=8))
FRAGMENT_ID_RE = re.compile(r"^frag-[A-Za-z0-9][A-Za-z0-9._-]{0,100}$")
STATUSES = {"active", "archived"}
PRIORITIES = {"high", "medium", "low"}


def _now() -> str:
    return dt.datetime.now(BEIJING).isoformat(timespec="seconds")


def _scalar(value: Any) -> str:
    """Serialize one frontmatter scalar without depending on PyYAML."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return '"' + text + '"'


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return ""
    if value in {"true", "false"}:
        return value == "true"
    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?(?:\d+\.\d*|\d*\.\d+)", value):
        return float(value)
    return value


def _quote_list(values: Iterable[Any]) -> str:
    return "[" + ", ".join(_scalar(item) for item in values) + "]"


def _parse_list(value: str) -> list[Any]:
    value = value.strip()
    if not (value.startswith("[") and value.endswith("]")):
        return []
    body = value[1:-1].strip()
    if not body:
        return []
    # The writer emits JSON-compatible quoted scalars, so a small JSON parse
    # is sufficient and avoids accepting arbitrary YAML constructs.
    try:
        parsed = json.loads("[" + body.replace("'", '"') + "]")
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return [_parse_scalar(item) for item in body.split(",")]


def _frontmatter(data: dict[str, Any]) -> str:
    lines = ["---"]
    ordered = (
        "fragment_id", "candidate_id", "created_at", "date", "source", "source_window",
        "visibility", "status", "priority", "episode_id", "trigger",
        "first_message_id", "last_message_id", "event_context", "reason", "messages",
        "source_event_ids", "window_ids", "emotion",
    )
    written: set[str] = set()
    for key in ordered:
        if key not in data:
            continue
        written.add(key)
        value = data[key]
        if key in {"source_event_ids", "window_ids"}:
            value = _quote_list(value or [])
        elif key == "messages":
            value = _scalar(json.dumps(value or [], ensure_ascii=False, separators=(",", ":")))
        elif isinstance(value, (dict, list)):
            value = _scalar(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        else:
            value = _scalar(value)
        lines.append(f"{key}: {value}")
    # Archiving must not erase fields introduced by a later schema version.
    # Keep unknown scalar fields in a deterministic order for safe rewrites.
    for key in sorted(set(data) - written):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key) or key in {"path", "body", "file_sha256"}:
            continue
        value = data[key]
        if isinstance(value, (dict, list)):
            value = _scalar(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        else:
            value = _scalar(value)
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


def _read_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise ValueError("fragment is missing frontmatter")
    end = text.find("\n---", 4)
    if end < 0:
        raise ValueError("fragment frontmatter is not closed")
    raw = text[4:end]
    data: dict[str, Any] = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ValueError("invalid frontmatter line")
        key, value = line.split(":", 1)
        key = key.strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            raise ValueError("invalid frontmatter key")
        data[key] = _parse_list(value) if key in {"source_event_ids", "window_ids"} else _parse_scalar(value)
        if key == "messages" and isinstance(data[key], str):
            try:
                decoded = json.loads(data[key])
            except json.JSONDecodeError:
                decoded = []
            data[key] = decoded if isinstance(decoded, list) else []
        if key == "emotion" and isinstance(data[key], str):
            try:
                decoded = json.loads(data[key])
            except json.JSONDecodeError:
                decoded = {}
            data[key] = decoded if isinstance(decoded, dict) else {}
    body = text[end + len("\n---"):].lstrip("\n")
    return data, body


def _canonical_payload(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identity(record: dict[str, Any]) -> str:
    """Return the retry identity, excluding timestamps and mutable status."""
    keys = ("source", "source_window", "visibility", "priority", "episode_id",
            "trigger", "first_message_id", "last_message_id", "event_context",
            "reason", "source_event_ids", "window_ids", "emotion", "messages")
    return _canonical_payload({key: record.get(key) for key in keys})


class FragmentStore:
    """Human-readable fragment files under one explicit root."""

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()

    def _path(self, fragment_id: str) -> Path:
        if not isinstance(fragment_id, str) or not FRAGMENT_ID_RE.fullmatch(fragment_id):
            raise ValueError("fragment_id must match frag-<safe id>")
        return self.root / (fragment_id + ".md")

    @staticmethod
    def derive_id(payload: dict[str, Any]) -> str:
        source_ids = payload.get("source_event_ids") or payload.get("trigger_ids") or []
        basis = {
            "source_event_ids": [str(item) for item in source_ids],
            "window_ids": [str(item) for item in payload.get("window_ids") or []],
            "messages": payload.get("messages") or [],
            "event_context": str(payload.get("event_context") or ""),
            "version": 1,
        }
        digest = hashlib.sha256(_canonical_payload(basis).encode("utf-8")).hexdigest()[:20]
        date = str(payload.get("date") or _now())[:10].replace("-", "")
        return "frag-%s-%s" % (date, digest)

    @staticmethod
    def normalize(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("fragment payload must be an object")
        messages = payload.get("messages") or []
        if not isinstance(messages, list) or not messages or len(messages) > 20:
            raise ValueError("messages must be a non-empty list of at most 20 items")
        clean_messages = []
        for item in messages:
            if not isinstance(item, dict):
                raise ValueError("each message must be an object")
            role = str(item.get("role") or "")
            text = str(item.get("text") or "")
            if role not in {"user", "assistant", "action"} or not text.strip():
                raise ValueError("message role/text is invalid")
            if len(text) > 4000:
                raise ValueError("message text exceeds 4000 chars")
            clean_messages.append({"role": role, "text": text})
        source_ids = payload.get("source_event_ids") or payload.get("trigger_ids") or []
        windows = payload.get("window_ids") or []
        if not isinstance(source_ids, list) or not source_ids or len(source_ids) > 40 or not all(str(x).strip() for x in source_ids):
            raise ValueError("source_event_ids must be a list of at most 40 strings")
        if not isinstance(windows, list) or len(windows) > 10 or not all(str(x).strip() for x in windows):
            raise ValueError("window_ids must be a list of at most 10 strings")
        priority = str(payload.get("priority") or "medium")
        if priority not in PRIORITIES:
            raise ValueError("priority must be high, medium, or low")
        status = str(payload.get("status") or "active")
        if status not in STATUSES:
            raise ValueError("status must be active or archived")
        visibility = str(payload.get("visibility") or "private")
        if visibility not in {"private", "shared"}:
            raise ValueError("visibility must be private or shared")
        def number(name: str, default: float, minimum: float = -1, maximum: float = 1) -> float:
            value = payload.get(name, default)
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ValueError(name + " must be numeric")
            if not minimum <= value <= maximum:
                raise ValueError(name + " is outside its allowed range")
            return value
        fragment_id = str(payload.get("fragment_id") or "")
        candidate_id = str(payload.get("candidate_id") or "")
        if candidate_id and (not candidate_id.startswith("candidate:") or len(candidate_id) > 100):
            raise ValueError("candidate_id must be an existing candidate identifier")
        valence = number("emotion_valence", 0.0)
        arousal = number("emotion_arousal", 0.0, 0, 1)
        result = {
            "fragment_id": fragment_id,
            "candidate_id": candidate_id,
            "created_at": str(payload.get("created_at") or _now()),
            "date": str(payload.get("date") or payload.get("created_at") or _now())[:19],
            "source": str(payload.get("source") or "cc"),
            "source_window": str(payload.get("source_window") or ""),
            "visibility": visibility,
            "status": status,
            "priority": priority,
            "episode_id": str(payload.get("episode_id") or ""),
            "trigger": str(payload.get("trigger") or "")[:500],
            "first_message_id": str(payload.get("first_message_id") or (source_ids[0] if source_ids else "")),
            "last_message_id": str(payload.get("last_message_id") or (source_ids[-1] if source_ids else "")),
            "event_context": str(payload.get("event_context") or "")[:1200],
            "reason": str(payload.get("reason") or "")[:500],
            "source_event_ids": [str(x) for x in source_ids],
            "window_ids": [str(x) for x in windows],
            "emotion": {"valence": valence, "arousal": arousal},
            "messages": clean_messages,
        }
        result["trigger_ids"] = list(result["source_event_ids"])
        if not result["fragment_id"]:
            result["fragment_id"] = FragmentStore.derive_id(result)
        if not FRAGMENT_ID_RE.fullmatch(result["fragment_id"]):
            raise ValueError("fragment_id must match frag-<safe id>")
        return result

    def write(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = self.normalize(payload)
        path = self._path(record["fragment_id"])
        body_lines = ["## Original Messages"]
        body_lines.extend("- **%s**: %s" % (item["role"], item["text"])
                          for item in record["messages"])
        body_lines.extend(["", "## Event Context", record["event_context"] or "(none)"])
        body = "\n".join(body_lines) + "\n"
        document = _frontmatter(record) + "\n\n" + body
        digest = hashlib.sha256(document.encode("utf-8")).hexdigest()
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            existing_data, _ = _read_frontmatter(existing)
            if _identity(existing_data) != _identity(record):
                raise ValueError("fragment_id already exists with different content")
            existing_digest = hashlib.sha256(existing.encode("utf-8")).hexdigest()
            return {"ok": True, "status": "duplicate", "fragment_id": record["fragment_id"],
                    "path": str(path), "sha256": existing_digest,
                    "stored_status": existing_data.get("status", "active")}
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".fragment-", suffix=".tmp", dir=str(self.root))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(document)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return {"ok": True, "status": "created", "fragment_id": record["fragment_id"],
                "path": str(path), "sha256": digest}

    def _load(self, path: Path) -> dict[str, Any]:
        front, body = _read_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        front["path"] = str(path)
        front["body"] = body
        front["file_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return front

    def list(self, status: str = "active", top_n: int = 5,
             exclude_window_ids: list[str] | None = None) -> list[dict[str, Any]]:
        if status not in STATUSES:
            raise ValueError("status must be active or archived")
        if not isinstance(top_n, int) or not 1 <= top_n <= 50:
            raise ValueError("top_n must be an integer 1..50")
        if not self.root.exists():
            return []
        excluded = {str(item) for item in (exclude_window_ids or [])}
        rows = []
        for path in sorted(self.root.glob("frag-*.md")):
            try:
                row = self._load(path)
            except (OSError, ValueError):
                continue
            if row.get("status") != status:
                continue
            windows = set(str(x) for x in row.get("window_ids") or [])
            if excluded and windows.intersection(excluded):
                continue
            rank = {"high": 3, "medium": 2, "low": 1}.get(row.get("priority"), 0)
            emotion = row.get("emotion") if isinstance(row.get("emotion"), dict) else {}
            arousal = float(emotion.get("arousal", 0) or 0)
            row["selection_score"] = round(rank * 10 + arousal, 4)
            rows.append(row)
        rows.sort(key=lambda row: (row["selection_score"], str(row.get("created_at") or "")), reverse=True)
        selected = []
        episode_counts: dict[str, int] = {}
        for row in rows:
            episode_id = str(row.get("episode_id") or "")
            if episode_id and episode_counts.get(episode_id, 0) >= 2:
                continue
            selected.append(row)
            if episode_id:
                episode_counts[episode_id] = episode_counts.get(episode_id, 0) + 1
            if len(selected) >= top_n:
                break
        return selected

    def archive(self, fragment_id: str) -> dict[str, Any]:
        path = self._path(fragment_id)
        if not path.exists():
            raise FileNotFoundError("fragment not found")
        data, body = _read_frontmatter(path.read_text(encoding="utf-8"))
        if data.get("status") == "archived":
            return {"ok": True, "status": "already_archived", "fragment_id": fragment_id,
                    "path": str(path)}
        data["status"] = "archived"
        document = _frontmatter(data) + "\n\n" + body
        fd, temp_name = tempfile.mkstemp(prefix=".fragment-archive-", suffix=".tmp", dir=str(self.root))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(document)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return {"ok": True, "status": "archived", "fragment_id": fragment_id, "path": str(path)}

