import sys
import unittest
from types import ModuleType
from unittest.mock import patch

openai_stub = ModuleType("openai")
openai_stub.OpenAI = lambda api_key=None: object()
sys.modules.setdefault("openai", openai_stub)

import analyze


class AnalyzeDuplicateReportingTests(unittest.TestCase):
    def setUp(self):
        analyze._STORY_KEYS.clear()

    def _base_patches(self):
        return (
            patch.object(analyze, "has_historical_duplicate", return_value=False),
            patch.object(analyze, "seen_full_preapi", return_value=False),
            patch.object(analyze, "_claim_story_slot", return_value=True),
        )

    def test_post_gpt_local_duplicate_reports_local_reason_and_candidate(self):
        with self._base_patches()[0], self._base_patches()[1], self._base_patches()[2], patch.object(
            analyze,
            "analyze_tweet_for_publish",
            return_value={
                "importance_score": 1,
                "label": "high",
                "headline": "🚨 COINBASE HALTS BTC AND ETH TRADING AFTER OUTAGE",
            },
        ), patch.object(analyze, "is_local_duplicate", side_effect=[False, True]):
            result = analyze.analyze_tweet_importance(
                "1",
                "@acct",
                "Coinbase halts BTC and ETH trading after outage",
            )

        self.assertEqual(result["importance_score"], 1)
        self.assertIsNone(result["headline"])
        self.assertEqual(result["dedupe_source"], "local dedupe")
        self.assertIn("post-GPT near-duplicate", result["dedupe_stage"])
        self.assertIn("local dedupe", result["reason"])
        self.assertEqual(
            result["suppressed_headline"],
            "🚨 COINBASE HALTS BTC AND ETH TRADING AFTER OUTAGE",
        )

    def test_gpt_duplicate_reports_only_gpt_dedupe_reason(self):
        with self._base_patches()[0], self._base_patches()[1], self._base_patches()[2], patch.object(
            analyze,
            "analyze_tweet_for_publish",
            return_value={
                "importance_score": 1,
                "label": "high",
                "headline": "🚨 ETH FALLS 4% AS LIQUIDATIONS TOP $200M",
            },
        ), patch.object(analyze, "is_local_duplicate", return_value=False), patch.object(
            analyze, "get_all_compressed_headlines", return_value=["ETH FALLS LIQUIDATIONS"]
        ), patch.object(analyze, "gpt_is_duplicate", return_value=True):
            result = analyze.analyze_tweet_importance(
                "2",
                "@acct",
                "ETH falls 4% as liquidations top $200m",
            )

        self.assertEqual(result["importance_score"], 1)
        self.assertIsNone(result["headline"])
        self.assertEqual(result["dedupe_source"], "gpt dedupe")
        self.assertEqual(result["reason"], "Duplicate skipped after publish analysis: gpt dedupe.")
        self.assertEqual(
            result["suppressed_headline"],
            "🚨 ETH FALLS 4% AS LIQUIDATIONS TOP $200M",
        )


if __name__ == "__main__":
    unittest.main()
