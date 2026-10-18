#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline tool broker (P1 交付件) — 模型请求 -> 工具执行 -> 结果回填 -> 同轮回答。

版本：v3 (2026-10-01)
- 新增 memory_append：把一条认识写进连续记忆（"小寻的本子" journal.jsonl，
  与 chatbot.add_journal 同文件同格式同滚动）；写后回显最近条目，失败对模型可见。
- 权限封底：run_test 用 /usr/bin/systemd-run 绝对路径、沙箱用户 uid0 硬拒、
  测试输出读取上限（64KB/流，防输出型 DoS）。
范围：
- 记忆工具：memory_search（接 MemoryGateway.search_events）、memory_read_file（白名单）、memory_append。
- 工作间工具：workspace_list / workspace_read / workspace_write / run_test（systemd 沙箱）。
- 不接受 shell、不接受任意路径、无删除操作。
- 每次调用都做 schema 校验；失败对模型可见（作为 tool 结果回填），不假装成功。
- 连续调用有硬上限；每次调用写审计（tool/args/result_size/latency/ok）。

用法：
    from memory_gateway import MemoryGateway
    from tool_broker import ToolBroker
    broker = ToolBroker(MemoryGateway())
    result = broker.run(question, model_fn, max_calls=3)
    # result: {"final": str, "calls": [...], "messages": [...], "run_id": str}
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import tempfile
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from memory_gateway import MemoryGateway
from memory_reply_protocol import SECRET_PATTERNS, redact_text
from canonical_memory import append_event

AUDIT_FILE = Path(__file__).resolve().parent / "tool_call_audit.jsonl"

# Shared result budget (chars) for ALL tools — keeps any single tool result
# from inflating the model context (see review 2026-09-30).
RESULT_BUDGET_CHARS = 9000

# Run-level total budget: the sum of ALL tool-result chars inside one run().
# When exhausted, further tool calls are refused and the model is asked to close.
RUN_BUDGET_CHARS = 12000

# The writable surface is a separate workspace, never the canonical memory
# tree, chatbot source, or the service root.  A deployment may override this
# with XIAOXUN_AGENT_WORKSPACE; the default is /root/xiaoxun/agent_workspace
# when this module lives in /root/xiaoxun/ledger_v2_shadow.
WORKSPACE_ROOT = Path(os.environ.get(
    "XIAOXUN_AGENT_WORKSPACE",
    "/var/lib/xiaoxun-agent/workspace",
))
WORKSPACE_WRITE_MAX_CHARS = 60000
WORKSPACE_READ_MAX_CHARS = 12000
WORKSPACE_LIST_MAX_FILES = 200
WORKSPACE_TEST_TIMEOUT_SEC = 120
WORKSPACE_TEST_USER = os.environ.get("XIAOXUN_AGENT_TEST_USER", "xiaoxun-agent")
WORKSPACE_TEST_GROUP = os.environ.get("XIAOXUN_AGENT_TEST_GROUP", WORKSPACE_TEST_USER)
WORKSPACE_FORBIDDEN_PARTS = {
    ".env", ".ssh", "id_rsa", "id_ed25519", "token", "secret",
    ".pem", ".key", "chatbot.py", "history.json", "ledger.db",
}

FILE_ALIASES = ("cloud_letter", "cloud_status", "xiaoxun_core",
                "xiaoxun_notebook", "xiaoxun_journal", "d_letter")

# ---- continuous memory ("小寻的本子") -----------------------------------
# 与 chatbot.add_journal 同文件、同格式 {"time", "text"}、同 500 行滚动上限。
JOURNAL_FILE = Path(os.environ.get(
    "XIAOXUN_JOURNAL_FILE",
    "/root/xiaoxun/data/journal.jsonl",
))
JOURNAL_MAX_LINES = 500
JOURNAL_TEXT_MAX_CHARS = 300
TZ_BEIJING = timezone(timedelta(hours=8))

# 测试输出读取上限（每流），防止输出型 DoS 把 root 进程内存吃满。
TEST_OUTPUT_READ_LIMIT = 64 * 1024
# 沙箱用户名称黑名单（平台无关的硬检查；Linux 上另有 uid0 解析检查）。
SANDBOX_USER_FORBIDDEN = {"", "root", "toor", "0", "daemon"}

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": ("Search raw memory events (chat history between Yanyan and me) "
                            "by keywords. Optional source filter: cc or telegram."),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "search keywords"},
                    "source": {"type": "string", "description": "cc | telegram (optional)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_read_file",
            "description": ("Read one whitelisted current file by alias: " + " / ".join(FILE_ALIASES)),
            "parameters": {
                "type": "object",
                "properties": {
                    "alias": {"type": "string"},
                    "max_chars": {"type": "integer"},
                },
                "required": ["alias"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_append",
            "description": ("Append one new observation/insight to your continuous memory "
                            "(\"your own notebook\" — the same kind of lines you see in your "
                            "context). Use it to remember decisions, discoveries, or things "
                            "said tonight. Max 300 chars; future conversations can recall it, "
                            "and it is mirrored to Yanyan."),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string",
                                "description": "the note to remember (<=300 chars)"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_list",
            "description": ("List files in the private work workspace. "
                            "Use this before editing so you know the current state."),
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "optional relative directory"},
                    "limit": {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_read",
            "description": ("Read a text file in the private work workspace. "
                            "Use offset/next_offset for long files."),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "relative workspace path"},
                    "offset": {"type": "integer"},
                    "max_chars": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_write",
            "description": ("Write a file in the private work workspace atomically. "
                            "This cannot modify canonical memory, production code, or delete files."),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "relative workspace path"},
                    "content": {"type": "string", "description": "complete replacement content"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_test",
            "description": ("Run the fixed unittest discovery command in the private workspace. "
                            "It accepts no command or shell text."),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


def _clean(text: str) -> str:
    """去除回复里可能残留的标记（[[...]]）与首尾空白。"""
    text = re.sub(r"\[\[[^\]]{0,120}\]\]", "", text or "")
    return text.strip()


class ToolBroker:
    def __init__(self, gateway: MemoryGateway, audit_file: Path | str = AUDIT_FILE,
                 workspace_root: Path | str | None = None,
                 test_timeout: int = WORKSPACE_TEST_TIMEOUT_SEC,
                 journal_file: Path | str = JOURNAL_FILE):
        self.gateway = gateway
        self.audit_file = Path(audit_file)
        self.journal_file = Path(journal_file)
        self.workspace_root = Path(workspace_root or WORKSPACE_ROOT).resolve()
        self.test_timeout = max(1, min(int(test_timeout), 300))

    # ---- validation -------------------------------------------------------
    @staticmethod
    def validate(name: str, args: dict) -> dict:
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
        if name == "memory_search":
            q = args.get("query")
            if not isinstance(q, str) or not q.strip():
                raise ValueError("query must be a non-empty string")
            if len(q) > 200:
                raise ValueError("query too long (max 200 chars)")
            src = args.get("source")
            if src is not None and src not in ("cc", "telegram", "self"):
                raise ValueError("source must be cc, telegram, or self")
            return {"query": q.strip(), "source": src}
        if name == "memory_read_file":
            alias = args.get("alias")
            if alias not in FILE_ALIASES:
                raise ValueError("unknown file alias")
            mc = args.get("max_chars", 5000)
            if not isinstance(mc, int) or not (1 <= mc <= 12000):
                raise ValueError("max_chars must be an integer 1..12000")
            return {"alias": alias, "max_chars": mc}
        if name == "memory_append":
            content = args.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("content must be a non-empty string")
            if len(content.strip()) > JOURNAL_TEXT_MAX_CHARS:
                raise ValueError("content too long (max %d chars)" % JOURNAL_TEXT_MAX_CHARS)
            if any(p.search(content) for p in SECRET_PATTERNS):
                raise ValueError("content looks like it contains a credential; refusing to store it")
            return {"content": content.strip()}
        if name == "workspace_list":
            prefix = args.get("prefix", "")
            if not isinstance(prefix, str) or len(prefix) > 200:
                raise ValueError("prefix must be a string up to 200 chars")
            limit = args.get("limit", 100)
            if not isinstance(limit, int) or not (1 <= limit <= WORKSPACE_LIST_MAX_FILES):
                raise ValueError("limit must be an integer 1..%d" % WORKSPACE_LIST_MAX_FILES)
            return {"prefix": prefix.strip(), "limit": limit}
        if name == "workspace_read":
            path = args.get("path")
            if not isinstance(path, str) or not path.strip():
                raise ValueError("path must be a non-empty string")
            if len(path) > 400:
                raise ValueError("path too long (max 400 chars)")
            offset = args.get("offset", 0)
            max_chars = args.get("max_chars", 4000)
            if not isinstance(offset, int) or offset < 0:
                raise ValueError("offset must be a non-negative integer")
            if not isinstance(max_chars, int) or not (1 <= max_chars <= WORKSPACE_READ_MAX_CHARS):
                raise ValueError("max_chars must be an integer 1..%d" % WORKSPACE_READ_MAX_CHARS)
            return {"path": path.strip(), "offset": offset, "max_chars": max_chars}
        if name == "workspace_write":
            path = args.get("path")
            content = args.get("content")
            if not isinstance(path, str) or not path.strip():
                raise ValueError("path must be a non-empty string")
            if len(path) > 400:
                raise ValueError("path too long (max 400 chars)")
            if not isinstance(content, str):
                raise ValueError("content must be a string")
            if len(content) > WORKSPACE_WRITE_MAX_CHARS:
                raise ValueError("content too long (max %d chars)" % WORKSPACE_WRITE_MAX_CHARS)
            return {"path": path.strip(), "content": content}
        if name == "run_test":
            extra = set(args) - set()
            if extra:
                raise ValueError("run_test accepts no arguments")
            return {}
        raise ValueError("unknown tool: %s" % name)

    # ---- execution --------------------------------------------------------
    @staticmethod
    def _is_secret(raw: str) -> bool:
        return any(pattern.search(raw or "") for pattern in SECRET_PATTERNS)

    def execute(self, name: str, args: dict, cap: int | None = None) -> str:
        budget = min(RESULT_BUDGET_CHARS, int(cap)) if cap else RESULT_BUDGET_CHARS
        if name == "memory_search":
            rows = self.gateway.search_events(args["query"], source=args.get("source"), limit=12)
            kept = []
            for r in rows:
                d = asdict(r)
                c = d.get("content") or ""
                # Redaction happens BEFORE anything enters the model context.
                if self._is_secret(c):
                    continue  # never forward credential-bearing events (same rule as protocol)
                c = redact_text(c)
                if len(c) > 400:
                    c = c[:400] + "...[truncated]"
                d["content"] = c
                kept.append(d)
            return self._fit_events({"events": kept, "count": len(kept)}, budget)
        if name == "memory_read_file":
            room = max(0, budget - 512)  # never inflate beyond the incoming budget
            if room < 40:
                return json.dumps({"alias": args["alias"], "content": "",
                                   "note": "budget too small"}, ensure_ascii=False)
            want = min(int(args.get("max_chars", 5000)), room)
            data = self.gateway.read_file(args["alias"], want)
            data["content"] = redact_text(data.get("content") or "")
            out = json.dumps(data, ensure_ascii=False)
            if len(out) > budget:
                over = len(out) - budget
                c = data["content"]
                keep = max(0, len(c) - over - 48)
                data["content"] = (c[:keep] + "...[truncated]") if keep < len(c) else c[:keep]
                out = json.dumps(data, ensure_ascii=False)
                if len(out) > budget:
                    data["content"] = ""
                    data["note"] = "clipped to budget"
                    out = json.dumps(data, ensure_ascii=False)
                if len(out) > budget:
                    out = json.dumps({"alias": data["alias"], "content": "",
                                      "note": "budget too small"}, ensure_ascii=False)
            return out
        if name == "memory_append":
            return self._memory_append(args["content"], budget)
        if name == "workspace_list":
            return self._workspace_list(args["prefix"], args["limit"], budget)
        if name == "workspace_read":
            return self._workspace_read(args["path"], args["offset"], args["max_chars"], budget)
        if name == "workspace_write":
            return self._workspace_write(args["path"], args["content"], budget)
        if name == "run_test":
            return self._run_test(budget)
        raise ValueError("unknown tool: %s" % name)

    # ---- continuous memory ("小寻的本子") --------------------------------
    def _memory_append(self, content: str, budget: int) -> str:
        """追加一条到连续记忆，返回含最近条目的结果（写回即可读回）。

        与 chatbot.add_journal 保持同文件/同格式（{"time","text"}）/同滚动（500 行）；
        失败不吞：异常向上抛出，由 run() 回填为对模型可见的 error。
        """
        text = content.strip()[:JOURNAL_TEXT_MAX_CHARS]
        journal = Path(self.journal_file)
        append_result = append_event(journal, text)
        event = append_result["event"]
        total = append_result.get("total_lines")
        recent = []
        post_warning = None
        try:
            lines = [l for l in journal.read_text(encoding="utf-8", errors="ignore").splitlines()
                     if l.strip()]
            if len(lines) > JOURNAL_MAX_LINES:
                journal.write_text("\n".join(lines[-JOURNAL_MAX_LINES:]) + "\n",
                                   encoding="utf-8")
                lines = lines[-JOURNAL_MAX_LINES:]
            total = len(lines)
            for line in lines[-3:]:
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                recent.append({"time": item.get("time"),
                               "text": redact_text(str(item.get("text") or ""))})
        except Exception as exc:
            # The line itself may already be appended; never let post-write
            # housekeeping failure masquerade as full success (review 1001).
            post_warning = "%s: %s" % (type(exc).__name__, str(exc)[:120])
        result = {"ok": post_warning is None, "written": len(text),
                  "file": "journal.jsonl (小寻的本子)",
                  "total_lines": total, "recent": recent,
                  "event_id": event["event_id"],
                  "canonical_source": event["source"]}
        if append_result.get("ledger") is not None:
            result["ledger"] = append_result["ledger"]
        if post_warning:
            result["post_write_warning"] = post_warning
            result["note"] = ("line was appended, but post-write housekeeping failed; "
                              "recall/rollover may be degraded")
        return self._bounded_json(result, budget)

    # ---- private work workspace ------------------------------------------
    def _workspace_path(self, relative: str) -> Path:
        raw = str(relative or "").strip().replace("\\", "/")
        while raw.startswith("./"):
            raw = raw[2:]
        candidate = Path(raw)
        if not raw or candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("path must stay inside the private workspace")
        lowered = raw.lower()
        for bad in WORKSPACE_FORBIDDEN_PARTS:
            if bad in lowered:
                raise ValueError("path contains a protected name: %s" % bad)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        full = (self.workspace_root / candidate).resolve(strict=False)
        root = self.workspace_root.resolve()
        if full != root and not str(full).startswith(str(root) + os.sep):
            raise ValueError("path escapes the private workspace")
        # Reject symlinks at every existing component.  This keeps a model from
        # redirecting an otherwise-valid relative path outside the workspace.
        cur = full
        while cur != root:
            if cur.exists() and cur.is_symlink():
                raise ValueError("symlink paths are not allowed")
            cur = cur.parent
        return full

    def _workspace_list(self, prefix: str, limit: int, budget: int) -> str:
        base = self._workspace_path(prefix) if prefix else self.workspace_root
        if not base.exists() or not base.is_dir():
            raise ValueError("workspace directory does not exist")
        rows = []
        root = self.workspace_root.resolve()
        for item in sorted(base.rglob("*"), key=lambda p: str(p).lower()):
            if len(rows) >= limit:
                break
            if not item.is_file() or item.is_symlink():
                continue
            try:
                rel = str(item.resolve().relative_to(root)).replace(os.sep, "/")
                rows.append({"path": rel, "size": item.stat().st_size})
            except (OSError, ValueError):
                continue
        return self._bounded_json({"root": ".", "files": rows, "count": len(rows)}, budget)

    def _workspace_read(self, relative: str, offset: int, max_chars: int, budget: int) -> str:
        full = self._workspace_path(relative)
        if not full.exists() or not full.is_file():
            raise ValueError("workspace file does not exist: %s" % relative)
        text = full.read_text(encoding="utf-8", errors="replace")
        segment = redact_text(text[offset:offset + max_chars])
        data = {"path": relative, "size": len(text), "offset": offset,
                "returned": len(segment), "content": segment}
        if offset + len(segment) < len(text):
            data["next_offset"] = offset + len(segment)
        return self._bounded_json(data, budget)

    def _workspace_write(self, relative: str, content: str, budget: int) -> str:
        full = self._workspace_path(relative)
        if full.exists() and full.is_symlink():
            raise ValueError("refusing to replace a symlink")
        full.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replacement prevents a concurrent reader from seeing a half file.
        fd, tmp_name = tempfile.mkstemp(prefix=".write-", dir=str(full.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                os.fchmod(handle.fileno(), 0o640)
            os.replace(tmp_name, full)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return self._bounded_json({
            "ok": True, "path": relative, "written": len(content),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }, budget)

    def _run_test(self, budget: int) -> str:
        # Hard guard (platform-independent): the sandbox user name must not be
        # something that can run as uid 0.
        if WORKSPACE_TEST_USER.strip().lower() in SANDBOX_USER_FORBIDDEN:
            return self._bounded_json({"ok": False, "rc": None,
                                       "note": "refusing: sandbox user %r is not allowed"
                                               % WORKSPACE_TEST_USER}, budget)
        try:  # Linux-only extra guard: resolve name and refuse uid 0 / missing users.
            import pwd
            if pwd.getpwnam(WORKSPACE_TEST_USER).pw_uid == 0:
                return self._bounded_json({"ok": False, "rc": None,
                                           "note": "refusing: sandbox user resolves to uid 0"},
                                          budget)
        except ImportError:
            pass  # non-Linux dev environment (offline tests)
        except KeyError:
            return self._bounded_json({"ok": False, "rc": None,
                                       "note": "sandbox user %r not found"
                                               % WORKSPACE_TEST_USER}, budget)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        tests = self.workspace_root / "tests"
        if not tests.is_dir():
            return self._bounded_json({"ok": False, "rc": None,
                                       "note": "no tests directory in workspace"}, budget)
        # Test files are model-writable code.  Never execute them in the
        # root chat process: use a fixed systemd sandbox with no network,
        # read-only filesystems, a non-root uid, and a hard wall-clock limit.
        # The command is constant; the model cannot supply shell text.
        command = [
            "/usr/bin/systemd-run", "--quiet", "--wait", "--pipe", "--collect",
            "--service-type=oneshot",
            "--uid=" + WORKSPACE_TEST_USER,
            "--gid=" + WORKSPACE_TEST_GROUP,
            "--working-directory=" + str(self.workspace_root),
            "--property=NoNewPrivileges=yes",
            "--property=PrivateNetwork=yes",
            "--property=PrivateTmp=yes",
            "--property=PrivateDevices=yes",
            "--property=ProtectSystem=strict",
            "--property=ProtectHome=yes",
            "--property=RestrictSUIDSGID=yes",
            "--property=LockPersonality=yes",
            "--property=MemoryMax=256M",
            "--property=CPUQuota=50%",
            "--property=TasksMax=32",
            "--property=TimeoutStartSec=%ss" % self.test_timeout,
            "--setenv=HOME=/nonexistent",
            "--setenv=PATH=/usr/bin:/bin",
            "--setenv=PYTHONDONTWRITEBYTECODE=1",
            "/usr/bin/env", "-i",
            "HOME=/nonexistent", "PATH=/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE=1",
            "/usr/bin/python3", "-m", "unittest", "discover", "-v", "-s", "tests",
        ]
        try:
            # Read at most TEST_OUTPUT_READ_LIMIT per stream via temp files:
            # never let a runaway test print fill the root process memory.
            with tempfile.TemporaryFile() as fout, tempfile.TemporaryFile() as ferr:
                proc = subprocess.run(command, cwd=str(self.workspace_root),
                                      stdout=fout, stderr=ferr,
                                      timeout=self.test_timeout + 15,
                                      check=False)
                fout.seek(0)
                ferr.seek(0)
                out_bytes = fout.read(TEST_OUTPUT_READ_LIMIT)
                err_bytes = ferr.read(TEST_OUTPUT_READ_LIMIT)
            output = (out_bytes.decode("utf-8", "replace")
                      + err_bytes.decode("utf-8", "replace")).strip()
            return self._bounded_json({"ok": proc.returncode == 0,
                                       "rc": proc.returncode,
                                       "sandbox": "systemd:nobody-like,read-only,no-network",
                                       "output": output}, budget)
        except subprocess.TimeoutExpired:
            return self._bounded_json({"ok": False, "rc": -1,
                                       "error": "tests timed out after %ds" % self.test_timeout,
                                       "sandbox": "systemd"}, budget)
        except (FileNotFoundError, PermissionError) as exc:
            return self._bounded_json({"ok": False, "rc": -1,
                                       "error": "sandbox unavailable: %s" % exc}, budget)

    @staticmethod
    def _bounded_json(data: dict, budget: int) -> str:
        out = json.dumps(data, ensure_ascii=False)
        if len(out) <= budget:
            return out
        if "output" in data:
            data = dict(data)
            data["output"] = str(data.get("output") or "")[:max(0, budget - 240)] + "...[truncated]"
            out = json.dumps(data, ensure_ascii=False)
        if len(out) > budget:
            return json.dumps({"ok": bool(data.get("ok", False)), "note": "result clipped to budget"},
                              ensure_ascii=False)[:budget]
        return out

    # ---- budget fitting ---------------------------------------------------
    @staticmethod
    def _fit_events(payload: dict, budget: int) -> str:
        """Serialize events and guarantee the final string NEVER exceeds budget.

        This is the hard post-serialization guard: it does not rely on any
        earlier per-record truncation.  Records are shrunk from the tail and
        then dropped until the result fits.
        """
        events = payload["events"]
        out = json.dumps(payload, ensure_ascii=False)
        clipped = False
        while len(out) > budget and events:
            last = events[-1]
            c = str(last.get("content") or "")
            over = len(out) - budget
            # Only shrink when it actually shortens the field (keeps the loop
            # convergent); otherwise the record must be dropped entirely.
            if len(c) > 54:
                keep = max(40, len(c) - over - 40)
                if keep + 14 < len(c):
                    last["content"] = c[:keep] + "...[truncated]"
                    clipped = True
                    out = json.dumps(payload, ensure_ascii=False)
                    continue
            events.pop()
            payload["count"] = len(events)
            clipped = True
            out = json.dumps(payload, ensure_ascii=False)
        if clipped:
            payload["clipped"] = True
            out = json.dumps(payload, ensure_ascii=False)
        if len(out) > budget:
            out = json.dumps({"events": [], "count": 0, "note": "budget too small"},
                             ensure_ascii=False)
        return out

    # ---- audit ------------------------------------------------------------
    def _audit(self, run_id, name, args, size, latency_ms, ok, err=None):
        if isinstance(args, dict):
            if name == "workspace_write":
                content = str(args.get("content") or "")
                args = {"path": str(args.get("path") or ""),
                        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                        "content_size": len(content)}
            elif name == "memory_append":
                content = str(args.get("content") or "")
                args = {"content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                        "content_size": len(content)}
            else:
                args = {k: (redact_text(v) if isinstance(v, str) else v) for k, v in args.items()}
        elif isinstance(args, str):
            args = redact_text(args)
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "tool": name,
            "args": args,
            "result_size": size,
            "latency_ms": latency_ms,
            "ok": ok,
        }
        if err:
            rec["error"] = str(err)[:200]
        with open(self.audit_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ---- main loop --------------------------------------------------------
    def run(self, question: str, model_fn: Callable, max_calls: int = 3, system_prompt: str = ""):
        """model_fn(messages, tools) -> assistant message dict（OpenAI 风格）。
        返回 {"final", "calls", "messages", "run_id"}。
        """
        run_id = "tb-%d" % int(time.time() * 1000)
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": question})
        calls: list = []
        attempts = 0
        spent = 0
        rounds = 0
        while True:
            rounds += 1
            if rounds > max_calls + 2:
                # Graceful close: no more tools; ask for a final answer once.
                messages.append({"role": "user",
                                 "content": "[tool budget reached] Answer the question now with what you have; no more tool calls are available."})
                msg = model_fn(messages, [])
                final = _clean(msg.get("content") or "")
                if not final:
                    final = "（工具调用额度已用完，我按现有信息作答。）"
                return {"final": final, "calls": calls, "messages": messages,
                        "run_id": run_id, "closed": "budget", "tool_chars_total": spent}
            msg = model_fn(messages, TOOL_SPECS)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                final = _clean(msg.get("content") or "")
                return {"final": final, "calls": calls, "messages": messages,
                        "run_id": run_id, "tool_chars_total": spent}
            messages.append({"role": "assistant",
                             "content": msg.get("content") or None,
                             "tool_calls": tool_calls})
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                raw_args = fn.get("arguments")
                t0 = time.time()
                args = None
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (dict(raw_args) if raw_args else {})
                except Exception:
                    args = None
                # Every model-issued tool-call attempt counts toward the cap,
                # including broken JSON and malformed arguments.
                attempts += 1
                if attempts > max_calls:
                    result = json.dumps({"error": "tool call limit reached (%d). Answer now with what you have." % max_calls})
                    ok = False
                elif args is None:
                    result = json.dumps({"error": "arguments are not valid JSON"})
                    ok = False
                else:
                    try:
                        vargs = self.validate(name, args)
                        remain = RUN_BUDGET_CHARS - spent
                        if remain <= 200:
                            raise ValueError("run tool budget exhausted; answer now with what you have")
                        result = self.execute(name, vargs, cap=min(RESULT_BUDGET_CHARS, remain))
                        ok = True
                        spent += len(result)
                    except Exception as e:
                        result = json.dumps({"error": str(e)})
                        ok = False
                lat = round((time.time() - t0) * 1000, 1)
                self._audit(run_id, name, args if args is not None else raw_args, len(result), lat, ok)
                calls.append({"tool": name, "args": args, "ok": ok, "result_size": len(result)})
                messages.append({"role": "tool", "tool_call_id": tc.get("id", "call"), "content": result})
