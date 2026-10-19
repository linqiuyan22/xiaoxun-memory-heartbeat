import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ledger_cli
from mcp_adapter import MemoryService
from memory_gateway import MemoryGateway
from tool_broker import ToolBroker


class MemoryGatewaySearchTest(unittest.TestCase):
    def test_older_human_match_survives_one_thousand_newer_system_matches(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-gateway-") as directory:
            db_path = Path(directory) / "synthetic.db"
            target = dict(source="cc", source_id="synthetic-human",
                          conversation_id="synthetic-thread", ts="2026-01-01T00:00:00Z",
                          actor="yanyan", kind="message", visibility="normal", content="alpha")
            newer = [dict(source="cc", source_id=f"synthetic-ocr-{i}",
                          conversation_id="synthetic-thread",
                          ts=f"2026-01-02T00:{i // 60:02d}:{i % 60:02d}Z",
                          actor="ocr", kind="message", visibility="normal", content="alpha")
                     for i in range(1000)]
            db = ledger_cli.connect(db_path)
            try:
                ledger_cli.ingest_rows(db, [target, *newer])
            finally:
                db.close()
            gateway = MemoryGateway(db_path=db_path)
            with patch("memory_gateway._filter_context", side_effect=lambda rows: (list(rows), 0)), \
                    patch.object(MemoryGateway, "_journal_events", return_value=[]):
                result = gateway.search_events("alpha", limit=1)
            self.assertEqual([row.source_id for row in result], ["synthetic-human"])

    def test_older_full_match_survives_one_thousand_newer_partial_matches(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-gateway-") as directory:
            db_path = Path(directory) / "synthetic.db"
            target = dict(source="cc", source_id="synthetic-target",
                          conversation_id="synthetic-thread", ts="2026-01-01T00:00:00Z",
                          actor="yanyan", kind="message", visibility="normal",
                          content="alpha omega")
            newer = [dict(source="cc", source_id=f"synthetic-noise-{i}",
                          conversation_id="synthetic-thread",
                          ts=f"2026-01-02T00:{i // 60:02d}:{i % 60:02d}Z",
                          actor="ocr", kind="message", visibility="normal", content="alpha")
                     for i in range(1000)]
            db = ledger_cli.connect(db_path)
            try:
                ledger_cli.ingest_rows(db, [target, *newer])
            finally:
                db.close()
            gateway = MemoryGateway(db_path=db_path)
            with patch("memory_gateway._filter_context", side_effect=lambda rows: (list(rows), 0)), \
                    patch.object(MemoryGateway, "_journal_events", return_value=[]):
                result = gateway.search_events("alpha omega", limit=1)
            self.assertEqual([row.source_id for row in result], ["synthetic-target"])
            self.assertEqual(result[0].content, "alpha omega")

    def test_ascii_case_variants_rank_full_match_consistently(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-gateway-") as directory:
            db_path = Path(directory) / "synthetic.db"
            rows = [dict(source="cc", source_id=source_id,
                         conversation_id="synthetic-thread", ts=ts,
                         actor="yanyan", kind="message", visibility="normal",
                         content=content)
                    for source_id, content, ts in (
                        ("full", "ALPHA OMEGA", "2026-01-01T00:00:00Z"),
                        ("partial", "alpha", "2026-01-02T00:00:00Z"))]
            db = ledger_cli.connect(db_path)
            try:
                ledger_cli.ingest_rows(db, rows)
            finally:
                db.close()
            gateway = MemoryGateway(db_path=db_path)
            with patch("memory_gateway._filter_context", side_effect=lambda items: (list(items), 0)), \
                    patch.object(MemoryGateway, "_journal_events", return_value=[]):
                for query in ("alpha omega", "ALPHA OMEGA", "Alpha Omega"):
                    with self.subTest(query=query):
                        result = gateway.search_events(query, limit=2)
                        self.assertEqual([row.source_id for row in result], ["full", "partial"])

    def test_explicit_correction_ranks_before_old_fact_without_erasing_it(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-gateway-") as directory:
            db_path = Path(directory) / "synthetic.db"
            original_id = ledger_cli.event_id("cc", "original")
            rows = [
                dict(source="cc", source_id="original", conversation_id="synthetic-thread",
                     ts="2026-01-01T00:00:00Z", actor="yanyan", kind="message",
                     visibility="normal", content="alpha room is blue"),
                dict(source="cc", source_id="reply", conversation_id="synthetic-thread",
                     ts="2026-01-02T00:00:00Z", actor="yanyan", kind="message",
                     visibility="normal", content="alpha room needs repainting", reply_to="original"),
                dict(source="cc", source_id="correction", conversation_id="synthetic-thread",
                     ts="2026-01-03T00:00:00Z", actor="yanyan", kind="message",
                     visibility="normal", content="alpha correction: room is green",
                     metadata={"supersedes_event_ids": [original_id]}),
            ]
            db = ledger_cli.connect(db_path)
            try:
                self.assertEqual(ledger_cli.ingest_rows(db, rows)["added"], 3)
                self.assertEqual(db.execute(
                    "SELECT content FROM events WHERE event_id=?", (original_id,)
                ).fetchone()["content"], "alpha room is blue")
            finally:
                db.close()
            gateway = MemoryGateway(db_path=db_path)
            with patch("memory_gateway._filter_context", side_effect=lambda items: (list(items), 0)), \
                    patch.object(MemoryGateway, "_journal_events", return_value=[]):
                result = gateway.search_events("alpha room", limit=6)
            self.assertEqual({row.source_id for row in result}, {"original", "reply", "correction"})
            self.assertLess(
                [row.source_id for row in result].index("correction"),
                [row.source_id for row in result].index("original"),
            )

    def test_session_recall_prefers_unseen_evidence_then_falls_back(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-session-recall-") as directory:
            db_path = Path(directory) / "synthetic.db"
            rows = [
                dict(source="cc", source_id="newer", conversation_id="synthetic-thread",
                     ts="2026-01-02T00:00:00Z", actor="yanyan", kind="message",
                     visibility="normal", content="alpha evidence newer"),
                dict(source="cc", source_id="older", conversation_id="synthetic-thread",
                     ts="2026-01-01T00:00:00Z", actor="yanyan", kind="message",
                     visibility="normal", content="alpha evidence older"),
            ]
            db = ledger_cli.connect(db_path)
            try:
                self.assertEqual(ledger_cli.ingest_rows(db, rows)["added"], 2)
            finally:
                db.close()
            gateway = MemoryGateway(db_path=db_path)
            with patch("memory_gateway._filter_context", side_effect=lambda items: (list(items), 0)), \
                    patch.object(MemoryGateway, "_journal_events", return_value=[]):
                first = gateway.search_events("alpha", limit=1, recall_session_id="chat-a")
                second = gateway.search_events("alpha", limit=2, recall_session_id="chat-a")
                other_chat = gateway.search_events("alpha", limit=1, recall_session_id="chat-b")
                fallback = gateway.search_events("alpha", limit=2, recall_session_id="chat-a")
            self.assertEqual([row.source_id for row in first], ["newer"])
            self.assertEqual([row.source_id for row in second], ["older"])
            self.assertEqual([row.source_id for row in other_chat], ["newer"])
            self.assertEqual([row.source_id for row in fallback], ["newer", "older"])

    def test_mcp_search_forwards_the_optional_session_id(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-mcp-session-") as directory:
            root = Path(directory)
            db_path = root / "synthetic.db"
            db = ledger_cli.connect(db_path)
            try:
                ledger_cli.ingest_rows(db, [
                    dict(source="cc", source_id="first", conversation_id="synthetic-thread",
                         ts="2026-01-02T00:00:00Z", actor="yanyan", kind="message",
                         visibility="normal", content="alpha first"),
                    dict(source="cc", source_id="second", conversation_id="synthetic-thread",
                         ts="2026-01-01T00:00:00Z", actor="yanyan", kind="message",
                         visibility="normal", content="alpha second"),
                ])
            finally:
                db.close()
            gateway = MemoryGateway(db_path=db_path)
            broker = ToolBroker(gateway, audit_file=root / "audit.jsonl", journal_file=root / "journal.jsonl")
            service = MemoryService(gateway=gateway, broker=broker, ledger_db=db_path,
                                    fragment_root=root / "fragments", memory_root=root / "memory")
            with patch("memory_gateway._filter_context", side_effect=lambda items: (list(items), 0)), \
                    patch.object(MemoryGateway, "_journal_events", return_value=[]), \
                    patch.object(gateway, "search_events", wraps=gateway.search_events) as search:
                first = service.memory_search("alpha", recall_session_id="mcp-chat")
            self.assertEqual(first["events"][0]["source_id"], "first")
            self.assertEqual(search.call_args.kwargs["recall_session_id"], "mcp-chat")


if __name__ == "__main__":
    unittest.main()
