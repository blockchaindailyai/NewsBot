# analyze.py
"""
Tweet analysis pipeline for Blockchain Daily bot.

Flow summary:

  0) Initialize headline state from disk on import.

  1) LOCAL FALLBACK HEADLINE (no GPT)
     - Build a deterministic fallback headline from full tweet text.

  2) DISK-BASED EXACT DEDUPE (pre-GPT, no tokens)
     - Check if this fallback headline (normalized) matches any previously
       saved full headline (from breaking_headlines.txt).
     - If yes: treat as already-covered story, skip GPT, return no headline.

  3) IN-MEMORY STORY-LEVEL DEDUPE (pre-GPT, no tokens)
     - Normalize the fallback headline to a key (strip 🚨#BREAKING, etc.).
     - Maintain a per-process set of story keys.
     - If key already claimed in this run: skip GPT entirely and return no headline.
       This specifically catches the case where multiple accounts tweet
       the same story in the same scrape cycle.

  4) OPTIONAL LOCAL NEAR-DUPE CHECK (pre-GPT, no tokens)
     - Use Jaccard-based dedupe on the fallback headline vs the last 100
       COMPRESSED headlines.
     - If it's very similar to a recent story, treat it as already covered,
       skip GPT and return no headline.

  5) GPT IMPORTANCE SCORING (gpt-5-mini)
     - If scoring fails: fallback to importance_score=0, no headline.
     - If score <= 44 return score/label but no headline.

  6) GPT HEADLINE GENERATION (gpt-5.1)
     - Generate a full 🚨#BREAKING: ALL-CAPS headline from the tweet text.
     - If GPT fails, fall back to the local deterministic headline.

  7) POST-GPT LOCAL NEAR-DUPE (compressed Jaccard)
     - Compare the candidate GPT headline vs last 100 COMPRESSED headlines.
     - If highly similar: treat as duplicate story, return no headline.

  8) GPT-BASED DEDUPE vs RECENT COMPRESSED HEADLINES
     - Ask GPT (cheap prompt) if this headline describes essentially the
       same story as any of the recent compressed headlines.
     - If GPT returns "duplicate": return no headline.

  9) FINAL SAVE & RETURN
     - save_full_headline() persists the full headline and a compressed
       form for future dedupe.
     - If save succeeds: return headline for posting.
     - If save detects an exact duplicate: return no headline.
"""

from typing import Dict, Any
import threading

from gpt_client import (
    score_tweet_importance,
    generate_headline_with_gpt,
    gpt_is_duplicate,
)
from headline_store import (
    load_headline_state,
    seen_full_preapi,
    save_full_headline,
    get_all_compressed_headlines,
    normalize_headline_for_key,
)

from headline_dedupe import is_local_duplicate
from local_headline_fallback import generate_blockchain_daily_headline


# ---------------------------- Fallback ---------------------------- #


def _fallback(tweet_id: str, reason: str) -> Dict[str, Any]:
    # 'reason' kept only for internal debugging if needed
    return {
        "tweet_id": str(tweet_id),
        "importance_score": 0,
        "label": "low",
        "headline": None,
    }


# ---------------------------- Global State ---------------------------- #

# Load existing headline state from disk (full + compressed).
load_headline_state()

# In-memory story-level dedupe (per process/run).
# Uses normalized fallback headline keys to claim "this story slot".
_STORY_KEYS: set[str] = set()
_STORY_LOCK = threading.Lock()


def _claim_story_slot_from_fallback(headline: str) -> bool:
    """
    Try to claim a story slot based on the LOCAL fallback headline.

    Returns:
        True  -> this story is NEW in this process/run (we claim it).
        False -> we've already seen an essentially identical fallback headline
                 in this process/run; caller should treat it as duplicate and
                 skip GPT to save tokens.

    Behavior notes:
      - Uses normalize_headline_for_key() which strips alert prefixes,
        uppercases, and removes non-alphanumerics.
      - Only applies in-memory; disk-based dedupe is handled separately.
    """
    key = normalize_headline_for_key(headline)
    if not key:
        # If something goes wrong with normalization, fail open and
        # allow the story through rather than killing analysis.
        return True

    with _STORY_LOCK:
        if key in _STORY_KEYS:
            return False
        _STORY_KEYS.add(key)
        return True


# ---------------------------- Main Entry ---------------------------- #


def analyze_tweet_importance(tweet_id, username, text):
    """
    Analyze a tweet's importance for a crypto/news bot and optionally
    produce a headline.

    Returns dict:
      {
        "tweet_id": str,
        "importance_score": int,
        "label": "low"|"medium"|"high"|"critical",
        "headline": Optional[str],  # 🚨#BREAKING: ...
      }

    If 'headline' is None, nothing will be posted, but the score/label
    can still be used for internal stats.
    """
    tweet_id_str = str(tweet_id)

    # ---------- (1) Local fallback headline (no GPT) ---------- #
    candidate_pre = generate_blockchain_daily_headline(text)

    # ---------- (2) Disk-based exact dedupe (pre-GPT) ---------- #
    # Check if the normalized fallback headline matches any stored
    # full headline from previous runs. If yes, this is very likely
    # the same core story -> skip GPT entirely.
    try:
        if seen_full_preapi(candidate_pre):
            # Story already covered in prior runs.
            return {
                "tweet_id": tweet_id_str,
                "importance_score": 0,
                "label": "low",
                "headline": None,
            }
    except Exception as e:
        print(f"[DEDUP-WARN] seen_full_preapi failed for {tweet_id_str}: {e!r}")

    # ---------- (3) In-memory story-level dedupe (pre-GPT) ---------- #
    # This specifically handles the case where multiple accounts post the
    # same story at about the same time in the same scrape cycle.
    # Only the first thread to claim this fallback headline key will
    # proceed to GPT. Others will bail out to save tokens.
    try:
        claimed = _claim_story_slot_from_fallback(candidate_pre)
    except Exception as e:
        print(f"[DEDUP-WARN] _claim_story_slot_from_fallback failed for {tweet_id_str}: {e!r}")
        claimed = True  # fail open

    if not claimed:
        # Story already claimed this run; treat as duplicate and skip GPT.
        return {
            "tweet_id": tweet_id_str,
            "importance_score": 0,
            "label": "low",
            "headline": None,
        }

    # ---------- (4) Optional pre-GPT near-dup vs recent compressed ---------- #
    # Extra safeguard to skip GPT if this fallback headline is already
    # very similar to a recent compressed headline on disk.
    # This saves tokens across runs when the same story keeps circulating.
    try:
        if is_local_duplicate(candidate_pre, threshold=0.78):
            # Very similar to a recent story; we consider it already covered.
            return {
                "tweet_id": tweet_id_str,
                "importance_score": 0,
                "label": "low",
                "headline": None,
            }
    except Exception as e:
        print(f"[DEDUP-WARN] Pre-GPT near-dup check failed for {tweet_id_str}: {e!r}")
        # Fail open: if this check explodes, better to continue than crash.

    # ---------- (5) GPT importance scoring ---------- #
    score_data = score_tweet_importance(tweet_id_str, username, text)
    if score_data is None:
        return _fallback(tweet_id_str, "analysis_failed_no_api_key_or_gpt_error")

    score = int(score_data.get("importance_score", 0))
    label = score_data.get("label", "low")

    # New rule:
    # score == 1 -> publish
    # score == 0 -> do NOT publish
    if score == 0:
        return {
            "tweet_id": tweet_id_str,
            "importance_score": score,
            "label": label,
            "headline": None,
        }


    # ---------- (6) GPT headline generation ---------- #
    candidate = generate_headline_with_gpt(text)
    if not candidate:
        # If GPT fails for some reason, fall back to the local rule-based headline.
        candidate = candidate_pre

    # ---------- (7) Post-GPT local near-dup (compressed Jaccard) ---------- #
    # Compare GPT headline against last 100 compressed headlines.
    try:
        if is_local_duplicate(candidate, threshold=0.82):
            return {
                "tweet_id": tweet_id_str,
                "importance_score": score,
                "label": label,
                "headline": None,
            }
    except Exception as e:
        print(f"[DEDUP-WARN] Post-GPT local dedupe failed for {tweet_id_str}: {e!r}")

    # ---------- (8) GPT-based dedupe vs compressed headline HISTORY ---------- #
    # Use full compressed history, but gpt_is_duplicate will locally
    # prefilter and pass at most ~100 most-similar items into the prompt.
    recent_compressed = get_all_compressed_headlines()
    try:
        if gpt_is_duplicate(candidate, text, recent_compressed):
            return {
                "tweet_id": tweet_id_str,
                "importance_score": score,
                "label": label,
                "headline": None,
            }
    except Exception as e:
        print(f"[DEDUP-WARN] gpt_is_duplicate failed for {tweet_id_str}: {e!r}")

    # ---------- (9) Final save & return ---------- #
    # Save full headline (and compressed variant) if it's truly new.
    try:
        if save_full_headline(candidate):
            return {
                "tweet_id": tweet_id_str,
                "importance_score": score,
                "label": label,
                "headline": candidate,
            }
        else:
            # Exact normalized duplicate at save time (race or pre-existing).
            return {
                "tweet_id": tweet_id_str,
                "importance_score": score,
                "label": label,
                "headline": None,
            }
    except Exception as e:
        print(f"[HEADLINE-ERROR] Failed to save headline for {tweet_id_str}: {e!r}")
        # If saving fails, better to return no headline than risk repeating later.
        return {
            "tweet_id": tweet_id_str,
            "importance_score": score,
            "label": label,
            "headline": None,
        }
