import unittest

from selector_filters import classify_row


class SelectorFilterTest(unittest.TestCase):
    @staticmethod
    def _quarantine():
        return {"source_ids": set(), "event_ids": set()}

    def test_agent_handoff_wrapper_is_not_recall_evidence(self):
        row = {
            "event_id": "evt-synthetic-handoff",
            "source_id": "cc:synthetic-handoff",
            "actor": "yanyan",
            "kind": "message",
            "visibility": "private",
            "content": "Another Claude session sent a message:\n"
                       "<agent-message from=\"synthetic\">\n"
                       "[Subagent hand-back] The text below is the final report of a subagent.",
            "metadata_json": "{}",
        }
        verdict = classify_row(row, self._quarantine())
        self.assertFalse(verdict["context_ok"])
        self.assertFalse(verdict["fact_ok"])
        self.assertIn("agent_handoff", verdict["hard_excludes"])

    def test_ordinary_message_remains_eligible(self):
        row = {
            "event_id": "evt-synthetic-ordinary",
            "source_id": "cc:synthetic-ordinary",
            "actor": "yanyan",
            "kind": "message",
            "visibility": "private",
            "content": "今天把阅读计划调整成先做整理，再继续写作。",
            "metadata_json": "{}",
        }
        verdict = classify_row(row, self._quarantine())
        self.assertTrue(verdict["context_ok"])
        self.assertTrue(verdict["fact_ok"])
