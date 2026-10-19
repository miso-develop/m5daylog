"""Regression for independent AI Review and formal GitHub recording policy.

This is a documentation contract test, not evidence of an independent review.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class IndependentReviewPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        cls.skill = (ROOT / ".agents/skills/code-review/SKILL.md").read_text(encoding="utf-8")

    def test_independence_is_session_based_and_fresh(self):
        for doc in (self.agents, self.skill):
            with self.subTest(document=doc[:24]):
                self.assertIn("別のAI会話・セッション", doc)
                self.assertIn("exact PR HEAD", doc)
                self.assertIn("live base", doc)
                self.assertIn("Engineering", doc)
                self.assertIn("Security", doc)

    def test_formal_review_is_not_approval_or_conversation_comment(self):
        for doc in (self.agents, self.skill):
            with self.subTest(document=doc[:24]):
                self.assertIn("COMMENT", doc)
                self.assertIn("COMMENTED", doc)
                self.assertIn("APPROVED", doc)
                self.assertIn("Review ID / URL", doc)
                self.assertIn("review commit SHA", doc)
                self.assertIn("Conversation comment", doc)
                self.assertIn("SUBMITTED", doc)

    def test_no_requirement_to_use_separate_github_identity(self):
        self.assertIn("同一GitHubアカウント", self.agents)
        self.assertIn("GitHub loginがPR authorと同一", self.skill)
        self.assertIn("server-side required approving review", self.agents)

    def test_existing_work_tracking_and_security_boundaries_stay(self):
        self.assertIn("open な `[Task]`", self.agents)
        self.assertIn("SECURITY.md", self.agents)
        self.assertIn("agent/PARALLEL-WORK.md", self.agents)
        self.assertIn("Security regression", self.skill)


if __name__ == "__main__":
    unittest.main()
