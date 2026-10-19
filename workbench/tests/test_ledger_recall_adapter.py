"""Synthetic characterization of the current candidate-window limitation."""
import functools
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ledger_cli
import ledger_recall_adapter as recall
import selector_filters


class CandidateWindowTest(unittest.TestCase):
    def test_older_human_evidence_survives_newer_same_score_system_events(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="synthetic-recall-", dir=root) as directory:
            db_path = Path(directory) / "synthetic.db"
            quarantine_path = Path(directory) / "quarantine.json"
            quarantine_path.write_text('{"source_ids": [], "event_ids": []}', encoding="utf-8")
            classify = functools.partial(
                selector_filters.classify_row,
                quarantine=selector_filters.load_quarantine(quarantine_path),
            )

            def event(source_id, actor, ts):
                return dict(source="cc", source_id=source_id,
                            conversation_id="synthetic-thread", ts=ts,
                            actor=actor, kind="message", visibility="normal",
                            content="alpha")

            target = event("synthetic-human", "yanyan", "2026-01-01T00:00:00Z")
            newer = [event(f"synthetic-ocr-{i}", "ocr",
                           f"2026-01-02T00:{i // 60:02d}:{i % 60:02d}Z")
                     for i in range(80)]
            db = ledger_cli.connect(db_path)
            try:
                ledger_cli.ingest_rows(db, [target, *newer])
                with patch.object(recall, "DB_PATH", db_path), patch.object(
                    recall, "_selector_classify", classify
                ):
                    result = recall.search("alpha", limit=1)
                self.assertEqual([row["source_id"] for row in result],
                                 ["synthetic-human"])
                self.assertTrue(result[0]["text"].endswith("alpha"))
            finally:
                db.close()

    def test_explicit_reply_link_preserves_original_and_newer_unlinked_evidence(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="synthetic-recall-", dir=root) as directory:
            db_path = Path(directory) / "synthetic.db"
            db = ledger_cli.connect(db_path)
            try:
                rows = [dict(source="cc", source_id=source_id,
                             conversation_id="synthetic-thread", ts=ts,
                             actor="yanyan", kind="message", visibility="normal",
                             content=content, reply_to=reply_to)
                        for source_id, content, ts, reply_to in (
                            ("original", "alpha room is blue", "2026-01-01T00:00:00Z", None),
                            ("reply", "alpha correction: room is green",
                             "2026-01-02T00:00:00Z", "original"),
                            ("unlinked", "alpha room is red", "2026-01-03T00:00:00Z", None))]
                self.assertEqual(ledger_cli.ingest_rows(db, rows)["added"], 3)
                evidence_sql = (
                    "SELECT event_id,source_id,content,content_hash,reply_to,metadata_json "
                    "FROM events ORDER BY source_id")
                before = [tuple(row) for row in db.execute(evidence_sql)]
                links = {row["source_id"]: row["reply_to"] for row in db.execute(
                    "SELECT source_id,reply_to FROM events")}
                self.assertEqual(links, {"original": None, "reply": "original",
                                         "unlinked": None})
                classify = functools.partial(selector_filters.classify_row,
                                             quarantine={"source_ids": set(),
                                                         "event_ids": set()})
                with patch.object(recall, "DB_PATH", db_path), patch.object(
                    recall, "_selector_classify", classify
                ):
                    result = recall.search("alpha", limit=6)
                # A reply link is evidence of a relationship, not by itself a
                # supersession rule. Recency must not silently discard evidence.
                self.assertEqual({row["source_id"] for row in result},
                                 {"original", "reply", "unlinked"})
                by_source = {row["source_id"]: row for row in result}
                for raw in rows:
                    returned = by_source[raw["source_id"]]
                    self.assertEqual(returned["event_id"],
                                     ledger_cli.event_id("cc", raw["source_id"]))
                    self.assertEqual(returned["text"],
                                     "[cc | yanyan | normal] " + raw["content"])
                self.assertEqual([tuple(row) for row in db.execute(evidence_sql)], before)
            finally:
                db.close()

    def test_only_explicit_correction_demotes_old_fact_without_erasing_it(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="synthetic-recall-", dir=root) as directory:
            db_path = Path(directory) / "synthetic.db"
            db = ledger_cli.connect(db_path)
            try:
                original_id = ledger_cli.event_id("cc", "original")
                rows = [
                    dict(source="cc", source_id="original", conversation_id="synthetic-thread",
                         ts="2026-01-01T00:00:00Z", actor="yanyan", kind="message",
                         visibility="normal", content="alpha room is blue"),
                    # A reply and a newer unlinked statement are both evidence,
                    # never automatic proof that the original is obsolete.
                    dict(source="cc", source_id="reply", conversation_id="synthetic-thread",
                         ts="2026-01-02T00:00:00Z", actor="yanyan", kind="message",
                         visibility="normal", content="alpha room needs repainting", reply_to="original"),
                    dict(source="cc", source_id="unlinked", conversation_id="synthetic-thread",
                         ts="2026-01-03T00:00:00Z", actor="yanyan", kind="message",
                         visibility="normal", content="alpha room has a red curtain"),
                    dict(source="cc", source_id="correction", conversation_id="synthetic-thread",
                         ts="2026-01-04T00:00:00Z", actor="yanyan", kind="message",
                         visibility="normal", content="alpha correction: room is green",
                         metadata={"supersedes_event_ids": [original_id]}),
                ]
                self.assertEqual(ledger_cli.ingest_rows(db, rows)["added"], 4)
                classify = functools.partial(selector_filters.classify_row,
                                             quarantine={"source_ids": set(), "event_ids": set()})
                with patch.object(recall, "DB_PATH", db_path), patch.object(
                    recall, "_selector_classify", classify
                ):
                    result = recall.search("alpha room", limit=6)
                by_source = {row["source_id"]: row for row in result}
                self.assertEqual(set(by_source), {"original", "reply", "unlinked", "correction"})
                self.assertEqual(by_source["original"]["fact_status"], "superseded")
                self.assertEqual(by_source["original"]["superseded_by"], [
                    ledger_cli.event_id("cc", "correction")])
                self.assertEqual(by_source["reply"]["fact_status"], "current_or_unresolved")
                self.assertEqual(by_source["unlinked"]["fact_status"], "current_or_unresolved")
                self.assertLess(
                    [row["source_id"] for row in result].index("correction"),
                    [row["source_id"] for row in result].index("original"),
                )
                self.assertEqual(db.execute(
                    "SELECT content FROM events WHERE event_id=?", (original_id,)
                ).fetchone()["content"], "alpha room is blue")
            finally:
                db.close()

    def test_ascii_case_variants_rank_full_match_above_newer_partial_match(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="synthetic-recall-", dir=root) as directory:
            db_path = Path(directory) / "synthetic.db"
            db = ledger_cli.connect(db_path)
            try:
                rows = [dict(source="cc", source_id=source_id,
                             conversation_id="synthetic-thread", ts=ts,
                             actor="yanyan", kind="message", visibility="normal",
                             content=content)
                        for source_id, content, ts in (
                            ("full", "ALPHA OMEGA", "2026-01-01T00:00:00Z"),
                            ("partial", "alpha", "2026-01-02T00:00:00Z"))]
                self.assertEqual(ledger_cli.ingest_rows(db, rows)["added"], 2)
                classify = functools.partial(selector_filters.classify_row,
                                             quarantine={"source_ids": set(),
                                                         "event_ids": set()})
                with patch.object(recall, "DB_PATH", db_path), patch.object(
                    recall, "_selector_classify", classify
                ):
                    for query in ("alpha omega", "ALPHA OMEGA", "Alpha Omega"):
                        with self.subTest(query=query):
                            result = recall.search(query, limit=2)
                            self.assertEqual([row["source_id"] for row in result],
                                             ["full", "partial"])
                            self.assertTrue(result[0]["text"].endswith("ALPHA OMEGA"))
                self.assertEqual(db.execute(
                    "SELECT content FROM events WHERE source_id='full'"
                ).fetchone()["content"], "ALPHA OMEGA")
            finally:
                db.close()

    def test_older_best_match_survives_eighty_newer_partial_matches(self):
        # All files stay in this workbench; never use the default ledger/config.
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="synthetic-recall-", dir=root) as directory:
            db_path = Path(directory) / "synthetic.db"
            quarantine_path = Path(directory) / "quarantine.json"
            quarantine_path.write_text('{"source_ids": [], "event_ids": []}', encoding="utf-8")
            classify = functools.partial(
                selector_filters.classify_row,
                quarantine=selector_filters.load_quarantine(quarantine_path),
            )

            def event(source_id, content, ts):
                return dict(source="cc", source_id=source_id,
                            conversation_id="synthetic-thread", ts=ts,
                            actor="yanyan", kind="message", visibility="normal",
                            content=content)

            target = event("synthetic-target", "alpha omega", "2026-01-01T00:00:00Z")
            newer = [event(f"synthetic-noise-{i}", "alpha",
                           f"2026-01-02T00:{i // 60:02d}:{i % 60:02d}Z")
                     for i in range(80)]
            db = ledger_cli.connect(db_path)
            try:
                stats = ledger_cli.ingest_rows(db, [target, *newer[:79]])
                self.assertEqual(stats, dict(received=80, added=80, duplicate=0,
                                             conflict=0, bad=0))
                with patch.object(recall, "DB_PATH", db_path), patch.object(
                    recall, "_selector_classify", classify
                ):
                    target_id = ledger_cli.event_id("cc", "synthetic-target")
                    self.assertEqual(ledger_cli.ingest_rows(db, [newer[79]])["added"], 1)
                    after = recall.search("alpha omega", limit=100)
                    self.assertEqual(len(after), 80)
                    self.assertEqual(after[0]["event_id"], target_id)
                    self.assertEqual(after[0]["source_id"], "synthetic-target")
                    self.assertEqual(sum(row["source_id"].startswith("synthetic-noise-")
                                         for row in after), 79)
                    # Retrieval still returns the original evidence verbatim.
                    self.assertEqual(db.execute(
                        "SELECT content FROM events WHERE event_id=?", (target_id,)
                    ).fetchone()["content"], "alpha omega")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
