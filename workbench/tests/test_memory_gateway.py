import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ledger_cli
from memory_gateway import MemoryGateway


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


if __name__ == "__main__":
    unittest.main()
