#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic routing for memory and named-file requests.

This is intentionally small and testable.  A model may write the final reply,
but it does not get to decide whether a request means "read the current file"
or "search every old memory".
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Protocol


class EventLike(Protocol):
    event_id: str
    source: str
    conversation_id: str
    ts: str
    actor: str
    content: str
    visibility: str


@dataclass(frozen=True)
class EvidenceEvent:
    """A bounded, provenance-preserving excerpt sent to the final model."""
    event_id: str
    source: str
    conversation_id: str
    ts: str
    actor: str
    content: str
    visibility: str


@dataclass(frozen=True)
class Route:
    kind: str
    query: str
    source: str | None = None
    actor: str | None = None
    since: str | None = None
    until: str | None = None
    file_alias: str | None = None


MEMORY_WORDS = ("回忆", "想起", "记得", "记不记得", "我说过", "你说过", "我们说过", "聊过", "翻记忆", "翻一下", "旧记录")
# "检索" alone still means the public web.  It only becomes an internal
# memory request when the same sentence names a private time/source/memory cue.
MEMORY_SEARCH_WORDS = ("检索", "搜索", "搜一下", "查一下", "查查", "翻翻")
MEMORY_SEARCH_CUES = ("记忆系统", "记忆库", "聊天记录", "口信", "昨晚", "昨天", "刚才",
                      "以前", "之前", "cc", "桌面", "云端聊天", "我说过", "你说过", "我们聊")
READ_WORDS = ("读", "看", "打开", "翻", "刚更新", "最新")
NOISE_PREFIXES = (
    "output token limit hit.", "[cross-session delivery notice]",
    "[request interrupted by user]", "no response requested.",
)
SECRET_PATTERNS = (
    re.compile(r'(?i)\bsk-[A-Za-z0-9_-]{12,}'),
    re.compile(r'(?i)(api[_ -]?key|token|password|secret)\s*[:=：]\s*[^\s,，;；。]{8,}'),
)


def _iso(value: dt.datetime) -> str:
    return value.isoformat(timespec="seconds")


def _night_window(now: dt.datetime) -> tuple[str, str]:
    local = now.astimezone(now.tzinfo)
    yesterday = (local - dt.timedelta(days=1)).date()
    start = dt.datetime.combine(yesterday, dt.time(18, 0), tzinfo=local.tzinfo)
    end = dt.datetime.combine(local.date(), dt.time(6, 0), tzinfo=local.tzinfo)
    return _iso(start), _iso(end)


def _yesterday_window(now: dt.datetime) -> tuple[str, str]:
    local = now.astimezone(now.tzinfo)
    day = (local - dt.timedelta(days=1)).date()
    start = dt.datetime.combine(day, dt.time.min, tzinfo=local.tzinfo)
    return _iso(start), _iso(start + dt.timedelta(days=1))


def _actor_for(text: str) -> str | None:
    if re.search(r"我.{0,8}(说|讲|聊|提|写|发)", text):
        return "yanyan"
    if re.search(r"你.{0,8}(说|讲|聊|提|写|发)", text):
        return "xiaoxun"
    return None


def _is_internal_memory_search(body: str, normalized: str) -> bool:
    """Keep a request such as "检索昨晚我们聊的记忆系统" off the web.

    A bare request to search is deliberately left alone.  The second gate is
    what prevents "检索 MCP 教程" or "搜索今天的新闻" from being hijacked.
    """
    return (any(word in body for word in MEMORY_SEARCH_WORDS)
            and any(cue in body or cue in normalized for cue in MEMORY_SEARCH_CUES))


def _identity_file_route(body: str) -> Route | None:
    """Route a small set of role questions to Xiaoxun's canonical self-file.

    These questions are ordinary chat rather than recollection requests, so
    they must be recognized before the general memory-word gate below.  The
    final response still receives only the bounded file excerpt.
    """
    if re.search(r"(?:小川|见川).{0,4}(?:是谁|是?什么人|做什么(?:的)?|负责什么)", body):
        return Route(kind="read_file", query=body, file_alias="xiaoxun_core")
    if re.search(r"(?:小定|定渊).{0,4}(?:是谁|是?什么人|做什么(?:的)?|负责什么)", body):
        return Route(kind="read_file", query=body, file_alias="xiaoxun_core")
    return None


def _relationship_style_query(body: str) -> bool:
    """Keep relationship/style questions in the normal agent turn.

    These prompts may mention memories ("以前怎么聊天", "CC 上怎么说话"),
    but a host-preloaded excerpt makes the model recite a report instead of
    choosing and digesting the relevant examples through its own tools.
    Factual event questions such as "昨晚发生了什么" stay on the bounded
    evidence route.
    """
    return bool(re.search(
        r"(?:怎么聊天|怎么说话|说话方式|语气|亲昵|亲密|相处|像不像|自然一点|以前.*(?:聊天|说话)|"
        r"(?:聊天|说话).*以前|cc.*(?:聊天|说话|亲昵)|(?:聊天|说话|亲昵).*cc)",
        body,
        re.I,
    ))


def route_request(text: str, now: dt.datetime) -> Route | None:
    """Map an explicit user request to a bounded source read."""
    body = str(text or "").strip()
    normalized = body.lower()
    if "云端口信" in body and any(word in body for word in READ_WORDS):
        return Route(kind="read_file", query=body, file_alias="cloud_letter")
    if "云端近况" in body and any(word in body for word in READ_WORDS):
        return Route(kind="read_file", query=body, file_alias="cloud_status")
    identity_route = _identity_file_route(body)
    if identity_route is not None:
        return identity_route
    if _relationship_style_query(body):
        return None
    source = "cc" if ("cc" in normalized or "桌面" in body) else ("telegram" if ("tele" in normalized or "云端聊天" in body) else None)
    is_memory = any(word in body for word in MEMORY_WORDS) or _is_internal_memory_search(body, normalized)
    if not is_memory:
        return None
    actor = _actor_for(body)
    since = until = None
    if "昨天晚上" in body or "昨晚" in body:
        since, until = _night_window(now)
    elif "昨天" in body:
        since, until = _yesterday_window(now)
    if source and (actor or since):
        return Route(kind="read_events", query=body, source=source, actor=actor, since=since, until=until)
    return Route(kind="search_events", query=body, source=source, actor=actor, since=since, until=until)


def _excerpt(event: EventLike, limit: int) -> EvidenceEvent:
    text = re.sub(r"\s+", " ", str(event.content or "")).strip()
    if len(text) > limit:
        head = max(1, limit * 3 // 4)
        tail = max(1, limit - head - 1)
        text = text[:head].rstrip() + "…" + text[-tail:].lstrip()
    return EvidenceEvent(
        event_id=event.event_id, source=event.source, conversation_id=event.conversation_id,
        ts=event.ts, actor=event.actor, content=text, visibility=event.visibility,
    )


def select_evidence(
    events: list[EventLike], char_budget: int = 4600, max_events: int = 24, *, coverage: bool = False,
) -> list[EventLike]:
    """Keep substantive raw events, ordered as they happened.

    The selection deliberately never substitutes a generated summary for an
    event.  For a long window, longer statements and both ends of the window
    are preferred so the final model sees a coherent, bounded source set.
    """
    usable, seen = [], set()
    for event in events:
        raw = str(event.content or "").strip()
        normalized = re.sub(r"\s+", " ", raw)
        if (not raw or event.visibility == "derived" or normalized.lower().startswith(NOISE_PREFIXES)
                or normalized.startswith("[Image: source:")
                or any(pattern.search(normalized) for pattern in SECRET_PATTERNS)
                or normalized in seen):
            continue
        seen.add(normalized)
        usable.append(event)
    if len(usable) <= max_events and sum(len(event.content) for event in usable) <= char_budget:
        return usable
    if coverage:
        # A whole-evening recollection is not a keyword search.  Give the
        # model a bounded set of chronological snapshots instead of only the
        # longest speeches, which previously made it answer one scene as if it
        # represented the whole night.
        count = min(max_events, len(usable))
        indices = sorted({round(index * (len(usable) - 1) / max(count - 1, 1)) for index in range(count)})
        per_event = max(80, (char_budget - count * 42) // max(count, 1))
        return [_excerpt(usable[index], per_event) for index in indices]
    scored = []
    last = len(usable) - 1
    for index, event in enumerate(usable):
        text = str(event.content).strip()
        score = min(len(text), 500) / 100.0
        if index in (0, last):
            score += 4
        if len(text) >= 60:
            score += 1
        scored.append((score, index, event))
    picked = sorted(scored, key=lambda row: (row[0], row[1]), reverse=True)[:max_events]
    picked = sorted((row[2] for row in picked), key=lambda event: (event.ts, event.event_id))
    result, used = [], 0
    for event in picked:
        if result and used + len(event.content) > char_budget:
            continue
        result.append(event)
        used += len(event.content)
    return result


def evidence_text(events: list[EventLike]) -> str:
    lines = []
    for event in events:
        text = re.sub(r"\s+", " ", str(event.content or "")).strip()
        for pattern in SECRET_PATTERNS:
            text = pattern.sub(lambda match: match.group(1) + "=[已隐藏]" if match.lastindex else "[已隐藏敏感串]", text)
        lines.append("[%s | %s | %s] %s" % (event.source, event.actor, event.ts, text))
    return "\n".join(lines)


def redact_text(text: str) -> str:
    """Remove credentials before any raw event/file enters a model prompt."""
    value = str(text or "")
    for pattern in SECRET_PATTERNS:
        value = pattern.sub(lambda match: match.group(1) + "=[已隐藏]" if match.lastindex else "[已隐藏敏感串]", value)
    return value
