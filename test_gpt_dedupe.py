import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

openai_stub = ModuleType("openai")
openai_stub.OpenAI = lambda api_key=None: object()
sys.modules.setdefault("openai", openai_stub)

import gpt_client


def _response(content="0"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


class GptDedupePromptTests(unittest.TestCase):
    def test_dedupe_prompt_caps_shortlist_and_omits_rich_full_tweet_context(self):
        captured = {}

        def fake_completion(**kwargs):
            captured.update(kwargs)
            return _response("0")

        recent = [
            f"COINBASE HALT BTC ETH TRADING OUTAGE {idx}%"
            for idx in range(40)
        ]
        long_tweet = "Coinbase halts BTC and ETH trading after outage. " + (
            "extra detail " * 80
        )

        with patch.object(gpt_client, "client", object()), patch.object(
            gpt_client, "_chat_completion_with_retry", side_effect=fake_completion
        ):
            self.assertFalse(
                gpt_client.gpt_is_duplicate(
                    "🚨 COINBASE HALTS BTC AND ETH TRADING AFTER OUTAGE HITS 10% VOLUME",
                    long_tweet,
                    recent,
                )
            )

        user_prompt = captured["messages"][1]["content"]
        numbered_lines = [
            line
            for line in user_prompt.splitlines()
            if line[:1].isdigit() and "." in line[:4]
        ]
        self.assertEqual(len(numbered_lines), gpt_client._DEDUPE_TOP_N)
        self.assertNotIn("Full tweet text for context", user_prompt)
        self.assertNotIn("extra detail extra detail extra detail", user_prompt)

    def test_gpt_dedupe_fails_open_for_asset_price_moves(self):
        with patch.object(gpt_client, "client", object()), patch.object(
            gpt_client, "_chat_completion_with_retry"
        ) as completion:
            self.assertFalse(
                gpt_client.gpt_is_duplicate(
                    "🚨 $BTC DROPS 8% AS LIQUIDATIONS ACCELERATE",
                    "$BTC drops 8% as liquidations accelerate",
                    ["BTC DROPS 5% AS LIQUIDATIONS ACCELERATE"],
                )
            )

        completion.assert_not_called()

    def test_dedupe_prompt_includes_capped_excerpt_for_sparse_candidate(self):
        captured = {}

        def fake_completion(**kwargs):
            captured.update(kwargs)
            return _response("0")

        long_tweet = "SEC delays decision on ETF application. " + (
            "context detail " * 80
        )

        with patch.object(gpt_client, "client", object()), patch.object(
            gpt_client, "_chat_completion_with_retry", side_effect=fake_completion
        ):
            self.assertFalse(
                gpt_client.gpt_is_duplicate(
                    "🚨 SEC ETF DELAY",
                    long_tweet,
                    ["SEC ETF DELAY", "SEC DELAYS ETF APPLICATION"],
                )
            )

        user_prompt = captured["messages"][1]["content"]
        self.assertIn("Source context excerpt", user_prompt)
        self.assertLessEqual(len(user_prompt), 1200)


if __name__ == "__main__":
    unittest.main()
