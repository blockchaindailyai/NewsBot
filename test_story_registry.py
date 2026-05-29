import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import story_registry
from story_dedupe import build_story_fingerprint


class StoryRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry_path = Path(self.tmp.name) / "registry.jsonl"
        self.audit_path = Path(self.tmp.name) / "audit.jsonl"
        self.path_patch = patch.object(story_registry, "STORY_REGISTRY_PATH", str(self.registry_path))
        self.audit_patch = patch.object(story_registry, "DEDUPE_AUDIT_PATH", str(self.audit_path))
        self.path_patch.start()
        self.audit_patch.start()
        story_registry._LOADED = False
        story_registry._RECORDS.clear()
        story_registry._RECURRING_KEYS.clear()
        story_registry._EXACT_KEYS.clear()
        story_registry._CANONICAL_KEYS.clear()

    def tearDown(self):
        self.path_patch.stop()
        self.audit_patch.stop()
        self.tmp.cleanup()

    def test_recurring_registry_blocks_same_period_not_different_period(self):
        april = build_story_fingerprint("US APRIL CPI 3.4% VS 3.4% EST")
        may = build_story_fingerprint("US MAY CPI 3.4% VS 3.4% EST")

        story_registry.save_story_record(
            headline="🚨 US APRIL CPI 3.4% VS 3.4% EST",
            fingerprint=april,
            tweet_id="1",
            username="@macro",
        )

        self.assertTrue(story_registry.has_historical_duplicate(april))
        self.assertFalse(story_registry.has_historical_duplicate(may))

    def test_audit_log_persists_event(self):
        story_registry.append_dedupe_audit("batch_duplicate_grouped", kept_tweet_id="1")
        self.assertIn("batch_duplicate_grouped", self.audit_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
