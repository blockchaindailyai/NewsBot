import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

openai_stub = ModuleType("openai")
openai_stub.OpenAI = lambda api_key=None: object()
sys.modules.setdefault("openai", openai_stub)

import gpt_client


def _response(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


class GptAnalysisFormatTests(unittest.TestCase):
    def test_reject_uses_single_zero_and_no_reason_field(self):
        captured = {}

        def fake_completion(**kwargs):
            captured.update(kwargs)
            return _response("0")

        with patch.object(gpt_client, "client", object()), patch.object(
            gpt_client, "_chat_completion_with_retry", side_effect=fake_completion
        ):
            result = gpt_client.analyze_tweet_for_publish("1", "@acct", "gm")

        self.assertEqual(result["importance_score"], 0)
        self.assertIsNone(result["headline"])
        self.assertNotIn("reason", result)
        system_prompt = captured["messages"][0]["content"]
        self.assertIn("0\n1|🚨 ALL-CAPS HEADLINE", system_prompt)
        self.assertIn("return exactly 0", system_prompt)
        self.assertNotIn('"reason"', system_prompt)

    def test_publish_parses_pipe_delimited_headline(self):
        with patch.object(gpt_client, "client", object()), patch.object(
            gpt_client,
            "_chat_completion_with_retry",
            return_value=_response("1|🚨 btc etf inflows hit $500m"),
        ):
            result = gpt_client.analyze_tweet_for_publish(
                "2",
                "@acct",
                "BTC ETF inflows hit $500m",
            )

        self.assertEqual(result["importance_score"], 1)
        self.assertEqual(result["headline"], "🚨 BTC ETF INFLOWS HIT $500M")
        self.assertNotIn("reason", result)


if __name__ == "__main__":
    unittest.main()
