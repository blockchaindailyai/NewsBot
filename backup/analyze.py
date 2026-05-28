# analyze.py
from typing import Dict, Any

from gpt_client import (
    score_tweet_importance,
    generate_headline_with_gpt,
    gpt_is_duplicate,
)
from headline_store import (
    load_headline_state,
    seen_full_preapi,
    save_full_headline,
    get_last_compressed_headlines,
)
from headline_dedupe import is_local_duplicate
from local_headline_fallback import generate_blockchain_daily_headline


# ---------------------------- Fallback ---------------------------- #
def _fallback(tweet_id: str, reason: str) -> Dict[str, Any]:
    return {
        "tweet_id": tweet_id,
        "importance_score": 0,
        "label": "low",
        "reason": reason,
        "headline": None,
    }


# Initialize headline state at import
load_headline_state()


def analyze_tweet_importance(tweet_id, username, text):
    """
    Analyze a tweet's importance for a crypto/news bot.

    Flow:
      1) Build local candidate headline and check exact/normalized duplicate (pre-API).
      2) GPT importance scoring.
      3) If score <= 39: return explanation only.
      4) If score > 39: GPT headline generation (full tweet).
         4a) Local near-dup check vs last 100 COMPRESSED headlines (Jaccard).
         4b) GPT-based duplicate check vs last 100 COMPRESSED headlines.
         4c) Else: save headline (full + compressed) once; return it.
    """
    # ---------- (1) Exact-dup prefilter (NO API) ----------
    candidate_pre = generate_blockchain_daily_headline(text)
    if seen_full_preapi(candidate_pre):
        return {
            "tweet_id": tweet_id,
            "importance_score": 0,
            "label": "low",
            "reason": "duplicate_story_exact_preapi",
            "headline": None,
        }

    # ---------- (2) GPT scoring ----------
    score_data = score_tweet_importance(str(tweet_id), username, text)
    if score_data is None:
        return _fallback(tweet_id, "analysis_failed_no_api_key")

    score = score_data["importance_score"]
    label = score_data["label"]
    base_reason = score_data["reason"]

    # Not important enough -> no headline, just explanation
    if score <= 39:
        return {
            "tweet_id": str(tweet_id),
            "importance_score": score,
            "label": label,
            "reason": base_reason,
            "headline": None,
        }

    # ---------- (3) Important -> GPT headline + near-dup checks ----------
    candidate = generate_headline_with_gpt(text)
    if not candidate:
        # fall back to local rule-based headline
        candidate = generate_blockchain_daily_headline(text)

    # (3a) Local near-duplicate check vs last 100 COMPRESSED headlines
    if is_local_duplicate(candidate, threshold=0.82):
        return {
            "tweet_id": str(tweet_id),
            "importance_score": score,
            "label": label,
            "reason": "duplicate_story_similar_local_compressed",
            "headline": None,
        }

    # (3b) GPT-based duplicate check vs last 100 COMPRESSED headlines
    recent_compressed = get_last_compressed_headlines(100)
    if gpt_is_duplicate(candidate, text, recent_compressed):
        return {
            "tweet_id": str(tweet_id),
            "importance_score": score,
            "label": label,
            "reason": "duplicate_story_similar_gpt_compressed",
            "headline": None,
        }

    # (3c) Save if truly new (full + compressed) and return
    if save_full_headline(candidate):
        return {
            "tweet_id": str(tweet_id),
            "importance_score": score,
            "label": label,
            "reason": base_reason,   # GPT explanation
            "headline": candidate,   # Full headline for posting
        }
    else:
        return {
            "tweet_id": str(tweet_id),
            "importance_score": score,
            "label": label,
            "reason": "duplicate_story_exact_postapi",
            "headline": None,
        }
