#!/usr/bin/env python3
"""One bounded, source-preserving runtime shelf for Xiaoxun's memory MCP.

The canonical conversation record remains the ledger. This module only reads
the few reviewed files named by ``小寻/MCP记忆库/runtime_memory.json`` and
combines them with append-first Active Layer fragments. It never scans the
whole vault, follows arbitrary paths, or writes a second memory copy.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fragment_store import FragmentStore
from memory_reply_protocol import SECRET_PATTERNS, redact_text


SCHEMA = "xiaoxun-runtime-memory-library/1.0"
CONFIG_RELATIVE_PATH = Path("小寻") / "MCP记忆库" / "runtime_memory.json"
MAX_FILES_PER_LAYER = 12
MAX_WINDOW_IDS = 8


class MemoryLibraryError(ValueError):
    """The curated shelf is unavailable or contains an unsafe pointer."""


def _safe_text(value: Any, limit: int) -> str:
    raw = str(value or "")
    if any(pattern.search(raw) for pattern in SECRET_PATTERNS):
        return "[redacted sensitive memory]"
    text = redact_text(raw).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 18)].rstrip() + "\n...[truncated]"


class MemoryLibrary:
    """Read only the reviewed runtime shelf, never a free-form vault path."""

    def __init__(self, memory_root: Path | str,
                 fragment_store: FragmentStore,
                 config_relative_path: Path | str = CONFIG_RELATIVE_PATH):
        self.memory_root = Path(memory_root).expanduser().resolve()
        relative = Path(config_relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise MemoryLibraryError("library config must be below memory root")
        self.config_path = (self.memory_root / relative).resolve()
        if self.memory_root not in self.config_path.parents:
            raise MemoryLibraryError("library config escapes memory root")
        self.fragment_store = fragment_store

    def _read_config(self) -> dict[str, Any]:
        if not self.config_path.is_file():
            raise MemoryLibraryError("runtime memory library config is unavailable")
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MemoryLibraryError("runtime memory library config is invalid") from exc
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise MemoryLibraryError("runtime memory library schema is invalid")
        return data

    def _resolve_entry(self, entry: dict[str, Any]) -> tuple[str, str, Path, int]:
        if not isinstance(entry, dict):
            raise MemoryLibraryError("runtime memory entry must be an object")
        entry_id = str(entry.get("id") or "").strip()
        title = str(entry.get("title") or entry_id).strip()
        raw_path = str(entry.get("path") or "").strip()
        try:
            max_chars = int(entry.get("max_chars", 2400))
        except (TypeError, ValueError) as exc:
            raise MemoryLibraryError("runtime memory entry max_chars is invalid") from exc
        path = Path(raw_path)
        if (not entry_id or not raw_path or path.is_absolute() or ".." in path.parts
                or not 1 <= max_chars <= 12000):
            raise MemoryLibraryError("runtime memory entry is unsafe or incomplete")
        full = (self.memory_root / path).resolve()
        if self.memory_root not in full.parents or not full.is_file():
            raise MemoryLibraryError("runtime memory entry is unavailable: " + entry_id)
        return entry_id, title, full, max_chars

    def _layer_entries(self, config: dict[str, Any], name: str) -> list[dict[str, Any]]:
        entries = config.get(name, [])
        if not isinstance(entries, list) or len(entries) > MAX_FILES_PER_LAYER:
            raise MemoryLibraryError("runtime memory layer is invalid: " + name)
        return entries

    def reference(self, reference_id: str, max_chars: int = 5000) -> dict[str, Any]:
        """Read one reviewed deep-reference only when the conversation needs it."""
        if not isinstance(reference_id, str) or not reference_id.strip() or len(reference_id) > 120:
            raise MemoryLibraryError("reference_id must be a safe non-empty string")
        if not isinstance(max_chars, int) or not 1 <= max_chars <= 12000:
            raise MemoryLibraryError("max_chars must be an integer 1..12000")
        config = self._read_config()
        for entry in self._layer_entries(config, "references"):
            entry_id, title, path, entry_limit = self._resolve_entry(entry)
            if entry_id != reference_id:
                continue
            return {
                "schema": SCHEMA,
                "source": "reviewed-runtime-shelf",
                "id": entry_id,
                "title": title,
                "source_path": str(path.relative_to(self.memory_root)).replace("\\", "/"),
                "content": _safe_text(path.read_text(encoding="utf-8", errors="replace"),
                                      min(entry_limit, max_chars)),
            }
        raise MemoryLibraryError("unknown reviewed memory reference")

    @staticmethod
    def _fit(result: dict[str, Any], max_chars: int) -> dict[str, Any]:
        """Keep the MCP response bounded without changing its source files."""
        while len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) > max_chars:
            sections = result.get("sections") or []
            fragments = result.get("active_fragments") or []
            if fragments:
                fragments.pop()
                result["clipped"] = True
                continue
            if sections:
                last = sections[-1]
                content = str(last.get("content") or "")
                if len(content) > 160:
                    last["content"] = content[: max(120, len(content) // 2)] + "\n...[truncated]"
                else:
                    sections.pop()
                result["clipped"] = True
                continue
            result["clipped"] = True
            break
        result["section_count"] = len(result.get("sections") or [])
        result["active_fragment_count"] = len(result.get("active_fragments") or [])
        return result

    def briefing(self, current_window_ids: list[str] | None = None,
                 max_chars: int = 9000) -> dict[str, Any]:
        if not isinstance(max_chars, int) or not 600 <= max_chars <= 12000:
            raise MemoryLibraryError("max_chars must be an integer 600..12000")
        windows = [str(value).strip() for value in (current_window_ids or [])]
        if (len(windows) > MAX_WINDOW_IDS or any(not value or len(value) > 160 for value in windows)):
            raise MemoryLibraryError("current_window_ids must contain up to 8 safe ids")
        config = self._read_config()
        result: dict[str, Any] = {
            "schema": SCHEMA,
            "library": str(config.get("library") or "小寻运行时记忆库"),
            "sections": [],
            "active_fragments": [],
            "excluded_window_ids": windows,
            "source": "reviewed-runtime-shelf",
        }
        for layer, label in (("identity", "L0"), ("stable", "L2")):
            for entry in self._layer_entries(config, layer):
                entry_id, title, path, entry_limit = self._resolve_entry(entry)
                content = _safe_text(path.read_text(encoding="utf-8", errors="replace"), entry_limit)
                result["sections"].append({
                    "layer": label,
                    "id": entry_id,
                    "title": title,
                    "source_path": str(path.relative_to(self.memory_root)).replace("\\", "/"),
                    "content": content,
                })
        active = config.get("active_layer", {})
        if not isinstance(active, dict):
            raise MemoryLibraryError("active_layer config is invalid")
        try:
            top_n = int(active.get("top_n", 8))
            fragment_limit = int(active.get("max_fragment_chars", 1400))
        except (TypeError, ValueError) as exc:
            raise MemoryLibraryError("active_layer limits are invalid") from exc
        if not 0 <= top_n <= 20 or not 160 <= fragment_limit <= 4000:
            raise MemoryLibraryError("active_layer limits are outside the allowed range")
        for row in self.fragment_store.list("active", top_n=top_n, exclude_window_ids=windows):
            messages = []
            for message in row.get("messages") or []:
                if isinstance(message, dict):
                    messages.append({
                        "role": str(message.get("role") or ""),
                        "text": _safe_text(message.get("text"), fragment_limit),
                    })
            result["active_fragments"].append({
                "fragment_id": str(row.get("fragment_id") or ""),
                "date": str(row.get("date") or ""),
                "priority": str(row.get("priority") or "medium"),
                "episode_id": str(row.get("episode_id") or ""),
                "source_event_ids": list(row.get("source_event_ids") or [])[:40],
                "messages": messages,
                "event_context": _safe_text(row.get("event_context"), fragment_limit),
            })
        return self._fit(result, max_chars)
