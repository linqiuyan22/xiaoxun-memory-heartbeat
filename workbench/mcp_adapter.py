#!/usr/bin/env python3
"""Transport-neutral MCP facade over the existing ledger_v2 core.

This file intentionally contains no model loop and no filesystem shell.  Both
stdio and the HTTP draft call ``MemoryService.call``; retrieval, append,
redaction, budgets, and audit remain in ``MemoryGateway``/``ToolBroker``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from canonical_memory import append_candidate
from memory_lifecycle import activate_candidate as activate_reviewed_candidate
from memory_lifecycle import list_candidates, review_candidate as append_review_decision
from fragment_store import FragmentStore
from memory_gateway import MEMORY_ROOT, MemoryGateway
from memory_library import MemoryLibrary
from memory_reply_protocol import SECRET_PATTERNS, redact_text
from tool_broker import FILE_ALIASES, ToolBroker


PROTOCOL_VERSION = "2025-06-18"


def _enabled_tool_names() -> set[str] | None:
    """Return the deployment whitelist, or ``None`` for local development."""
    raw = os.environ.get("XIAOXUN_MCP_ENABLED_TOOLS", "").strip()
    if not raw:
        return None
    return {name.strip() for name in raw.split(",") if name.strip()}


ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT.parent / "data"
DEFAULT_AUDIT = DATA_ROOT / "mcp_tool_audit.jsonl"
DEFAULT_JOURNAL = Path(os.environ.get("XIAOXUN_JOURNAL_FILE", str(DATA_ROOT / "journal.jsonl")))
DEFAULT_LEDGER = Path(os.environ.get("XIAOXUN_CANONICAL_LEDGER_DB", str(ROOT / "ledger.db")))
DEFAULT_FRAGMENT_ROOT = Path(os.environ.get(
    "XIAOXUN_FRAGMENT_ROOT",
    str(DATA_ROOT / "fragments"),
))

# A desktop mirror is deliberately a read-only view of the canonical VPS
# ledger.  The switch is opt-in so the production service keeps its existing
# write surface; the local Claude configuration enables it explicitly.
READ_ONLY_MUTATION_TOOLS = frozenset({
    "memory_append", "hold", "grow", "propose", "I", "review_candidate", "activate_candidate",
    "fragment_write", "fragment_archive",
})


def _read_only_enabled() -> bool:
    return os.environ.get("XIAOXUN_MCP_READ_ONLY", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _remote_append_enabled() -> bool:
    return os.environ.get("XIAOXUN_REMOTE_MEMORY_APPEND", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


REMOTE_LIFECYCLE_TOOLS = frozenset({"grow", "review_candidate", "activate_candidate"})


def _remote_lifecycle_enabled() -> bool:
    return os.environ.get("XIAOXUN_REMOTE_MEMORY_LIFECYCLE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _remote_mcp_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Forward one explicitly allowed desktop mutation to the VPS canonical MCP."""
    if name not in {"memory_append", *REMOTE_LIFECYCLE_TOOLS}:
        raise ValueError("unsupported remote memory mutation")
    ssh_config = os.environ.get("XIAOXUN_REMOTE_SSH_CONFIG", str(Path.home() / ".ssh" / "config"))
    command = [
        "ssh", "-F", ssh_config, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "newvps",
        "sudo -u xcore env "
        "XIAOXUN_JOURNAL_FILE=/srv/xiaoxun/data/journal.jsonl "
        "XIAOXUN_CANONICAL_LEDGER_DB=/srv/xiaoxun/core/ledger.db "
        "XIAOXUN_MCP_AUDIT_FILE=/srv/xiaoxun/data/mcp_tool_audit.jsonl "
        "XIAOXUN_MEMORY_ROOT=/srv/xiaoxun/memory "
        "XIAOXUN_MCP_ENABLED_TOOLS=%s " % name +
        "/usr/bin/python3 -u /srv/xiaoxun/core/mcp_adapter.py",
    ]
    request = {"jsonrpc": "2.0", "id": "local-" + name, "method": "tools/call",
               "params": {"name": name, "arguments": arguments}}
    try:
        completed = subprocess.run(
            command, input=json.dumps(request, ensure_ascii=False) + "\n",
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=35,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("canonical memory service is unavailable; try again when the VPS connection returns") from exc
    if completed.returncode:
        raise RuntimeError("canonical memory service is unavailable; try again when the VPS connection returns")
    try:
        responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        response = responses[-1]
        if response.get("error"):
            raise ValueError(str(response["error"].get("message") or "remote MCP rejected the request"))
        return json.loads(response["result"]["content"][0]["text"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("canonical memory service returned an invalid response") from exc


def _remote_memory_append(content: str) -> dict[str, Any]:
    return _remote_mcp_call("memory_append", {"content": content})


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


MCP_RESULT_BUDGET_CHARS = 9000
MCP_EVENT_CONTENT_CHARS = 500


def _safe_content(value: Any) -> str:
    """Apply the same credential redaction to direct MCP reads as the broker."""
    raw = str(value or "")
    if any(pattern.search(raw) for pattern in SECRET_PATTERNS):
        return "[redacted sensitive memory]"
    content = redact_text(raw)
    return (content[:MCP_EVENT_CONTENT_CHARS] + "...[truncated]"
            if len(content) > MCP_EVENT_CONTENT_CHARS else content)


def _safe_event(row: Any) -> dict[str, Any]:
    data = asdict(row)
    data["content"] = _safe_content(data.get("content"))
    if "metadata_json" in data:
        data["metadata_json"] = _safe_content(data.get("metadata_json"))
    return data


def _bounded_records(payload: dict[str, Any], key: str,
                     budget: int = MCP_RESULT_BUDGET_CHARS) -> dict[str, Any]:
    """Bound direct MCP result paths that do not pass through ToolBroker."""
    result = dict(payload)
    records = list(result.get(key) or [])
    result[key] = records
    clipped = False
    while len(_json(result)) > budget and records:
        last = records[-1]
        content = str(last.get("content") or "")
        if len(content) > 80:
            over = len(_json(result)) - budget
            keep = max(40, len(content) - over - 32)
            if keep + 16 < len(content):
                last["content"] = content[:keep] + "...[truncated]"
                clipped = True
                continue
        records.pop()
        clipped = True
    if clipped:
        result["clipped"] = True
    if len(_json(result)) > budget:
        result[key] = []
        result["clipped"] = True
        result["note"] = "result budget too small"
    return result


class MemoryService:
    """The one operation surface shared by local and remote transports."""

    def __init__(self, gateway: MemoryGateway | None = None,
                 broker: ToolBroker | None = None,
                 ledger_db: Path | str | None = None,
                 candidate_db: Path | str | None = None,
                 fragment_root: Path | str | None = None,
                 memory_root: Path | str | None = None,
                 read_only: bool | None = None,
                 remote_append: Callable[[str], dict[str, Any]] | None = None,
                 remote_mutation: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None):
        self.gateway = gateway or MemoryGateway()
        self.ledger_db = Path(ledger_db or DEFAULT_LEDGER)
        self.read_only = _read_only_enabled() if read_only is None else bool(read_only)
        self.remote_append = remote_append or (
            _remote_memory_append if self.read_only and _remote_append_enabled() else None
        )
        self.remote_mutation = remote_mutation or (
            _remote_mcp_call if self.read_only and _remote_lifecycle_enabled() else None
        )
        self.broker = broker or ToolBroker(
            self.gateway,
            audit_file=os.environ.get("XIAOXUN_MCP_AUDIT_FILE", str(DEFAULT_AUDIT)),
            journal_file=DEFAULT_JOURNAL,
        )
        # Candidate records use the same ledger by default.  A separate path
        # is accepted only for offline fixtures, never created implicitly.
        self.candidate_db = Path(candidate_db or self.ledger_db)
        self.fragment_store = FragmentStore(fragment_root or DEFAULT_FRAGMENT_ROOT)
        self.memory_root = Path(memory_root or MEMORY_ROOT)
        # The existing ledger tools must remain available during staged rollout
        # when the reviewed shelf has not yet been copied to a host.
        self.memory_library: MemoryLibrary | None = None

    def _require_write_access(self) -> None:
        if self.read_only:
            raise PermissionError(
                "this local MCP uses a read-only ledger mirror; write through the VPS service instead"
            )

    def available_tools(self) -> list[dict[str, Any]]:
        if not self.read_only:
            tools = list(TOOLS)
        else:
            hidden = set(READ_ONLY_MUTATION_TOOLS)
            if self.remote_append is not None:
                hidden.discard("memory_append")
            if self.remote_mutation is not None:
                hidden.difference_update(REMOTE_LIFECYCLE_TOOLS)
            tools = [tool for tool in TOOLS if tool["name"] not in hidden]
        enabled = _enabled_tool_names()
        return [tool for tool in tools if enabled is None or tool["name"] in enabled]

    def breath(self, limit: int = 12) -> dict[str, Any]:
        if not isinstance(limit, int) or not 1 <= limit <= 80:
            raise ValueError("limit must be an integer 1..80")
        # ``read_events`` is chronological by default.  Breath is the
        # current-state tool, so select the ledger tail explicitly.
        rows = self.gateway.read_events(limit=limit, newest_first=True)
        return _bounded_records({"source": "ledger",
                                 "events": [_safe_event(row) for row in rows],
                                 "count": len(rows)}, "events")

    def memory_search(self, query: str, source: str | None = None,
                      recall_session_id: str | None = None) -> dict[str, Any]:
        args = {"query": query}
        if source is not None:
            args["source"] = source
        if recall_session_id is not None:
            args["recall_session_id"] = recall_session_id
        return json.loads(self.broker.execute("memory_search", self.broker.validate("memory_search", args)))

    def memory_get(self, alias: str | None = None, max_chars: int = 5000,
                   event_id: str | None = None) -> dict[str, Any]:
        if event_id:
            row = self.gateway.get_event(event_id)
            return {"event": _safe_event(row) if row else None}
        args = self.broker.validate("memory_read_file", {"alias": alias, "max_chars": max_chars})
        return json.loads(self.broker.execute("memory_read_file", args))

    def memory_briefing(self, current_window_ids: list[str] | None = None,
                        max_chars: int = MCP_RESULT_BUDGET_CHARS) -> dict[str, Any]:
        """Read the one reviewed shelf used to begin a new conversation."""
        if self.memory_library is None:
            self.memory_library = MemoryLibrary(self.memory_root, self.fragment_store)
        return self.memory_library.briefing(current_window_ids=current_window_ids,
                                            max_chars=max_chars)

    def memory_reference(self, reference_id: str,
                         max_chars: int = 5000) -> dict[str, Any]:
        """Read one named, reviewed deep reference only when it is needed."""
        if self.memory_library is None:
            self.memory_library = MemoryLibrary(self.memory_root, self.fragment_store)
        return self.memory_library.reference(reference_id, max_chars=max_chars)

    def append_self(self, content: str) -> dict[str, Any]:
        args = self.broker.validate("memory_append", {"content": content})
        if self.read_only:
            if self.remote_append is None:
                self._require_write_access()
            return self.remote_append(args["content"])
        return json.loads(self.broker.execute("memory_append", args))

    def _candidate(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        content = args.get("content")
        if not isinstance(content, str) or not content.strip() or len(content.strip()) > 1000:
            raise ValueError("content must be a non-empty string up to 1000 chars")
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            raise ValueError("content looks like it contains a credential; refusing to store it")
        ids = args.get("source_event_ids", [])
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 20
                or not all(isinstance(item, str) and item.strip() for item in ids)):
            raise ValueError("source_event_ids must contain 1..20 source event IDs")
        confidence = args.get("confidence")
        if confidence is not None and (not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1):
            raise ValueError("confidence must be between 0 and 1")
        risk_level = str(args.get("risk_level") or "personal")
        if risk_level not in {"operational", "personal", "relationship", "identity"}:
            raise ValueError("risk_level is invalid")
        normalized = {
            "content": content.strip(), "source_event_ids": ids, "confidence": confidence,
            "scene_gate": str(args.get("scene_gate") or "")[:300],
            "counterevidence": str(args.get("counterevidence") or "")[:500],
            "visibility": str(args.get("visibility") or "private"),
            "risk_level": risk_level,
        }
        if self.read_only:
            if operation == "grow" and self.remote_mutation is not None:
                return self.remote_mutation("grow", normalized)
            self._require_write_access()
        kind = {"hold": "fact_candidate", "grow": "reflection_candidate",
                "propose": "core_candidate"}[operation]
        return append_candidate(normalized["content"], kind=kind, operation=operation,
                                source_event_ids=ids, confidence=confidence,
                                scene_gate=normalized["scene_gate"],
                                counterevidence=normalized["counterevidence"],
                                visibility=normalized["visibility"], risk_level=risk_level,
                                ledger_db=self.candidate_db)

    def review_candidate(self, candidate_id: str, decision: str, reason: str = "") -> dict[str, Any]:
        args = {"candidate_id": candidate_id, "decision": decision, "reason": reason}
        if self.read_only:
            if self.remote_mutation is None:
                self._require_write_access()
            return self.remote_mutation("review_candidate", args)
        return append_review_decision(self.candidate_db, candidate_id, decision,
                                      reason=reason, reviewer="xiaoxun")

    def activate_candidate(self, candidate_id: str, priority: str = "medium") -> dict[str, Any]:
        args = {"candidate_id": candidate_id, "priority": priority}
        if self.read_only:
            if self.remote_mutation is None:
                self._require_write_access()
            return self.remote_mutation("activate_candidate", args)
        return activate_reviewed_candidate(self.candidate_db, self.fragment_store.root,
                                           candidate_id, priority=priority)


    def review_status(self, limit: int = 20) -> dict[str, Any]:
        report = list_candidates(self.candidate_db, limit=limit)
        for row in report["recent"]:
            row["content"] = _safe_content(row.get("content"))
            row["review_reason"] = _safe_content(row.get("review_reason"))
        return _bounded_records(report, "recent")


    @staticmethod
    def _fragment_has_secret(payload: dict[str, Any]) -> bool:
        values: list[str] = []
        for key in ("trigger", "event_context", "reason"):
            values.append(str(payload.get(key) or ""))
        for item in payload.get("messages") or []:
            if isinstance(item, dict):
                values.append(str(item.get("text") or ""))
        return any(pattern.search(value) for value in values for pattern in SECRET_PATTERNS)

    @staticmethod
    def _safe_fragment(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result.pop("body", None)
        for key in ("trigger", "event_context", "reason"):
            if key in result:
                result[key] = _safe_content(result[key])
        if isinstance(result.get("messages"), list):
            result["messages"] = [
                {"role": str(item.get("role") or ""),
                 "text": _safe_content(item.get("text"))}
                for item in result["messages"] if isinstance(item, dict)
            ]
        return result

    def fragment_write(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_write_access()
        if not isinstance(arguments, dict):
            raise ValueError("fragment payload must be an object")
        if self._fragment_has_secret(arguments):
            raise ValueError("fragment looks like it contains a credential; refusing to store it")
        # When a canonical ledger is available, do not let a fragment become
        # an untraceable second memory source.  Offline fixtures may omit the
        # ledger, which keeps this operation testable without production data.
        normalized = FragmentStore.normalize(arguments)
        if self.ledger_db.exists():
            missing = [event_id for event_id in normalized["source_event_ids"]
                       if self.gateway.get_event(event_id) is None]
            if missing:
                raise ValueError("source_event_ids not found in canonical ledger")
        return self.fragment_store.write(normalized)

    def fragment_list(self, status: str = "active", top_n: int = 5,
                      exclude_window_ids: list[str] | None = None) -> dict[str, Any]:
        rows = [self._safe_fragment(row) for row in self.fragment_store.list(
            status=status, top_n=top_n, exclude_window_ids=exclude_window_ids)]
        return _bounded_records({"status": status, "fragments": rows,
                                 "count": len(rows)}, "fragments")

    def fragment_archive(self, fragment_id: str) -> dict[str, Any]:
        self._require_write_access()
        return self.fragment_store.archive(fragment_id)

    def _audit_call(self, run_id: str, name: str, arguments: dict[str, Any],
                    result: dict[str, Any] | None, started: float,
                    ok: bool = True, error: str | None = None) -> None:
        """Record direct MCP calls, which do not pass through ``run()``.

        The HTTP and stdio adapters call ``MemoryService.call`` directly.  A
        previous implementation therefore executed tools successfully while
        bypassing ``ToolBroker.run`` and leaving no trustworthy MCP audit
        entry.  Correlation IDs are recorded, but never the note contents.
        """
        result_ref: dict[str, Any] = {}
        if isinstance(result, dict):
            for key in ("event_id", "source_id", "candidate_id", "canonical_source", "fragment_id"):
                if result.get(key) is not None:
                    result_ref[key] = result[key]
            event = result.get("event")
            if isinstance(event, dict):
                for key in ("event_id", "source_id"):
                    if event.get(key) is not None:
                        result_ref[key] = event[key]
        audit_arguments: dict[str, Any] = arguments
        if name == "fragment_write":
            raw = _json(arguments)
            import hashlib
            audit_arguments = {
                "fragment_id": str(arguments.get("fragment_id") or ""),
                "source_event_ids": list(arguments.get("source_event_ids") or arguments.get("trigger_ids") or [])[:40],
                "window_ids": list(arguments.get("window_ids") or [])[:10],
                "message_count": len(arguments.get("messages") or []) if isinstance(arguments.get("messages"), list) else 0,
                "payload_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            }
        try:
            self.broker._audit(
                run_id,
                name,
                audit_arguments,
                len(_json(result)) if result is not None else 0,
                round((time.monotonic() - started) * 1000, 1),
                ok,
                error,
                result_ref=result_ref,
            )
        except Exception:
            # Auditing must not turn a successful memory operation into a
            # client-visible failure.  The service watchdog still owns the
            # audit file's availability check.
            return

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        started = time.monotonic()
        run_id = "mcp-%d" % time.time_ns()
        raw_args = arguments if isinstance(arguments, dict) else {}
        try:
            enabled = _enabled_tool_names()
            if enabled is not None and name not in enabled:
                raise ValueError("tool disabled: %s" % name)
            result = self._call_unlogged(name, arguments)
        except Exception as exc:
            self._audit_call(run_id, name, raw_args, None, started,
                             ok=False, error=str(exc))
            raise
        result_ok = not (isinstance(result, dict) and result.get("ok") is False)
        self._audit_call(run_id, name, raw_args, result, started, ok=result_ok)
        return result

    def _call_unlogged(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = arguments or {}
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        if name == "breath":
            return self.breath(args.get("limit", 12))
        if name == "memory_search":
            return self.memory_search(args.get("query"), args.get("source"), args.get("recall_session_id"))
        if name in {"memory_get", "memory_read_file"}:
            return self.memory_get(args.get("alias"), args.get("max_chars", 5000), args.get("event_id"))
        if name == "memory_briefing":
            return self.memory_briefing(args.get("current_window_ids"),
                                        args.get("max_chars", MCP_RESULT_BUDGET_CHARS))
        if name == "memory_reference":
            return self.memory_reference(args.get("reference_id"), args.get("max_chars", 5000))
        if name == "memory_append":
            return self.append_self(args.get("content"))
        if name in {"hold", "grow", "propose"}:
            return self._candidate(name, args)
        if name == "I":
            content = str(args.get("content") or "").strip()
            if content:
                return self._candidate("grow", args)
            rows = [row for row in self.gateway.read_events(
                        actor="xiaoxun", limit=80, newest_first=True)
                    if row.kind in {"reflection_candidate", "fact_candidate", "memory"}]
            return _bounded_records({"observations": [_safe_event(row) for row in rows[:12]],
                                     "count": min(len(rows), 12)}, "observations")
        if name == "review_status":
            return self.review_status(args.get("limit", 20))
        if name == "review_candidate":
            return self.review_candidate(args.get("candidate_id"), args.get("decision"), args.get("reason", ""))
        if name == "activate_candidate":
            return self.activate_candidate(args.get("candidate_id"), args.get("priority", "medium"))
        if name == "fragment_write":
            return self.fragment_write(args)
        if name == "fragment_list":
            return self.fragment_list(args.get("status", "active"), args.get("top_n", 5),
                                       args.get("exclude_window_ids"))
        if name == "fragment_archive":
            return self.fragment_archive(args.get("fragment_id"))
        raise ValueError("unknown tool: %s" % name)


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": required or [], "additionalProperties": False}}


TOOLS = [
    _schema("breath", "Read a bounded recent ledger window.",
            {"limit": {"type": "integer", "minimum": 1, "maximum": 80}}),
    _schema("memory_search", "Search bounded original memory events.",
            {"query": {"type": "string", "maxLength": 200},
             "source": {"type": "string", "enum": ["cc", "telegram", "self"]},
             "recall_session_id": {"type": "string", "maxLength": 120}}, ["query"]),
    _schema("memory_get", "Read one stable event or named whitelisted memory projection.",
            {"event_id": {"type": "string", "maxLength": 160},
             "alias": {"type": "string", "enum": list(FILE_ALIASES)},
             "max_chars": {"type": "integer", "minimum": 1, "maximum": 12000}}),
    _schema("memory_briefing", "Read only the reviewed runtime shelf plus bounded delayed Active Layer fragments.",
            {"current_window_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
             "max_chars": {"type": "integer", "minimum": 600, "maximum": 12000}}),
    _schema("memory_reference", "Read one named reviewed deep reference from the runtime shelf.",
            {"reference_id": {"type": "string", "maxLength": 120},
             "max_chars": {"type": "integer", "minimum": 1, "maximum": 12000}}, ["reference_id"]),
    _schema("memory_append", "Append one bounded self observation to canonical memory.",
            {"content": {"type": "string", "maxLength": 300}}, ["content"]),
    _schema("hold", "Hold a fact candidate for later review; never auto-approves.",
            {"content": {"type": "string", "maxLength": 1000},
             "source_event_ids": {"type": "array", "items": {"type": "string"}},
             "confidence": {"type": "number", "minimum": 0, "maximum": 1},
             "scene_gate": {"type": "string", "maxLength": 300},
             "counterevidence": {"type": "string", "maxLength": 500},
             "visibility": {"type": "string", "enum": ["private", "shared"]},
             "risk_level": {"type": "string", "enum": ["operational", "personal", "relationship", "identity"]}}, ["content", "source_event_ids"]),
    _schema("grow", "Write a reflection candidate for review.",
            {"content": {"type": "string", "maxLength": 1000},
             "source_event_ids": {"type": "array", "items": {"type": "string"}},
             "confidence": {"type": "number", "minimum": 0, "maximum": 1},
             "scene_gate": {"type": "string", "maxLength": 300},
             "counterevidence": {"type": "string", "maxLength": 500},
             "visibility": {"type": "string", "enum": ["private", "shared"]},
             "risk_level": {"type": "string", "enum": ["operational", "personal", "relationship", "identity"]}}, ["content", "source_event_ids"]),
    _schema("propose", "Propose a core candidate for review; never mutates core self.",
            {"content": {"type": "string", "maxLength": 1000},
             "source_event_ids": {"type": "array", "items": {"type": "string"}},
             "confidence": {"type": "number", "minimum": 0, "maximum": 1},
             "scene_gate": {"type": "string", "maxLength": 300},
             "counterevidence": {"type": "string", "maxLength": 500},
             "visibility": {"type": "string", "enum": ["private", "shared"]},
             "risk_level": {"type": "string", "enum": ["operational", "personal", "relationship", "identity"]}}, ["content", "source_event_ids"]),
    _schema("I", "Read recent self-observations or propose one for review.",
            {"content": {"type": "string", "maxLength": 1000},
             "source_event_ids": {"type": "array", "items": {"type": "string"}},
             "confidence": {"type": "number", "minimum": 0, "maximum": 1},
             "scene_gate": {"type": "string", "maxLength": 300},
             "counterevidence": {"type": "string", "maxLength": 500},
             "visibility": {"type": "string", "enum": ["private", "shared"]}}),
    _schema("review_status", "List candidate records, decisions, and pending counts.",
            {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
    _schema("review_candidate", "Append an explicit review decision; it never edits history.",
            {"candidate_id": {"type": "string", "maxLength": 100},
             "decision": {"type": "string", "enum": ["approve", "reject", "archive"]},
             "reason": {"type": "string", "maxLength": 500}}, ["candidate_id", "decision"]),
    _schema("activate_candidate", "Project one approved non-core candidate into Active Layer.",
            {"candidate_id": {"type": "string", "maxLength": 100},
             "priority": {"type": "string", "enum": ["high", "medium", "low"]}}, ["candidate_id"]),
    _schema("fragment_write", "Append one Active Layer fragment Markdown projection with original quotes.",
            {"fragment_id": {"type": "string", "maxLength": 110},
             "date": {"type": "string", "maxLength": 40},
             "source": {"type": "string", "maxLength": 40},
             "source_window": {"type": "string", "maxLength": 160},
             "visibility": {"type": "string", "enum": ["private", "shared"]},
             "status": {"type": "string", "enum": ["active", "archived"]},
             "priority": {"type": "string", "enum": ["high", "medium", "low"]},
             "episode_id": {"type": "string", "maxLength": 160},
             "trigger": {"type": "string", "maxLength": 500},
             "source_event_ids": {"type": "array", "items": {"type": "string"}},
             "window_ids": {"type": "array", "items": {"type": "string"}},
             "messages": {"type": "array", "items": {"type": "object"}},
             "event_context": {"type": "string", "maxLength": 1200},
             "reason": {"type": "string", "maxLength": 500},
             "emotion_valence": {"type": "number", "minimum": -1, "maximum": 1},
             "emotion_arousal": {"type": "number", "minimum": 0, "maximum": 1}},
            ["source_event_ids", "messages"]),
    _schema("fragment_list", "Read a bounded Active Layer view; archived files remain on disk.",
            {"status": {"type": "string", "enum": ["active", "archived"]},
             "top_n": {"type": "integer", "minimum": 1, "maximum": 50},
             "exclude_window_ids": {"type": "array", "items": {"type": "string"}}}),
    _schema("fragment_archive", "Mark one fragment archived without deleting its Markdown file.",
            {"fragment_id": {"type": "string", "maxLength": 110}}, ["fragment_id"]),
]


def rpc(service: MemoryService, request: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC request.  Notifications intentionally return None."""
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                "error": {"code": -32600, "message": "invalid request"}}
    method = request.get("method")
    ident = request.get("id")
    if ident is None and method == "notifications/initialized":
        return None
    try:
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": ident,
                    "result": {"protocolVersion": PROTOCOL_VERSION,
                               "capabilities": {"tools": {"listChanged": False}},
                               "serverInfo": {"name": "xiaoxun-ledger", "version": "0.1.0"}}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": ident, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": ident, "result": {"tools": service.available_tools()}}
        if method == "tools/call":
            params = request.get("params") or {}
            result = service.call(params.get("name"), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": ident,
                    "result": {"content": [{"type": "text", "text": _json(result)}],
                               "isError": False}}
        raise ValueError("method not found: %s" % method)
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": ident,
                "error": {"code": -32602 if method == "tools/call" else -32601,
                          "message": str(exc)[:300]}}


def serve_stdio(service: MemoryService | None = None, stdin=None, stdout=None) -> int:
    """Run newline-delimited JSON-RPC over stdio, with no diagnostic stdout."""
    service = service or MemoryService()
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            response = rpc(service, request)
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": str(exc)[:200]}}
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve_stdio())
