import unittest

from headline_dedupe import is_local_duplicate
from story_dedupe import build_story_fingerprint, likely_same_batch_story


class StoryDedupeTests(unittest.TestCase):
    def test_same_batch_crypto_outage_variants_match(self):
        first = build_story_fingerprint(
            "BREAKING: Coinbase says trading is halted for BTC and ETH due to outage"
        )
        second = build_story_fingerprint(
            "Coinbase halts BTC and ETH trading amid outage, status page says"
        )

        self.assertTrue(likely_same_batch_story(first, second))

    def test_recurring_macro_different_months_do_not_match(self):
        april = build_story_fingerprint("US APRIL CPI 3.4% VS 3.4% EST")
        may = build_story_fingerprint("US MAY CPI 3.4% VS 3.4% EST")

        self.assertTrue(april.is_recurring)
        self.assertTrue(may.is_recurring)
        self.assertNotEqual(april.period_key, may.period_key)
        self.assertFalse(likely_same_batch_story(april, may))

    def test_recurring_macro_history_dedupe_fails_open(self):
        self.assertFalse(
            is_local_duplicate(
                "🚨 US MAY CPI 3.4% VS 3.4% EST",
                threshold=0.1,
                tweet_text="US MAY CPI 3.4% VS 3.4% EST",
            )
        )


if __name__ == "__main__":
    unittest.main()
