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
     - Claim high-precision local story keys produced by story_dedupe.py.
     - Recurring data only contributes broad keys when the period/date is
       explicit, preventing April-vs-May style false duplicates.
     - If key already claimed in this run: skip GPT entirely and return no headline.

  4) OPTIONAL LOCAL NEAR-DUPE CHECK (pre-GPT, no tokens)
     - Use Jaccard-based dedupe on compressed headlines for non-recurring
       stories. Recurring scheduled data fails open to avoid suppressing
       important new releases that resemble older periods.

  5) LIBERAL LOCAL REJECT FILTER (pre-GPT, no tokens)
     - Skip only obvious no-news chatter/promos/link-only posts with no market signal.

  6) COMBINED GPT PUBLISH DECISION + HEADLINE (gpt-5-mini)
     - Decide publish/no-publish and draft the 🚨 headline in one model call.
     - If the model publishes but omits a headline, fall back to the local headline.

  7) POST-GPT LOCAL NEAR-DUPE (compressed Jaccard)
     - Compare the candidate GPT headline vs last 100 COMPRESSED headlines.
     - If highly similar: treat as duplicate story, return no headline.

  8) GPT-BASED DEDUPE vs RECENT COMPRESSED HEADLINES
     - Ask GPT if this headline describes essentially the
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
import re

from gpt_client import (
    analyze_tweet_for_publish,
    gpt_is_duplicate,
)
from headline_store import (
    load_headline_state,
    seen_full_preapi,
    save_full_headline,
    get_all_compressed_headlines,
)

from headline_dedupe import is_local_duplicate
from local_headline_fallback import generate_blockchain_daily_headline
from story_dedupe import StoryFingerprint, build_story_fingerprint
from story_registry import (
    append_dedupe_audit,
    get_canonical_key,
    has_historical_duplicate,
    load_story_registry,
    save_story_record,
)


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
load_story_registry()





def _core_story_key_from_text(text: str) -> str:
    """
    Build a coarse story key directly from raw tweet text to catch
    same-story posts that use different wording or account-specific prefixes.
    """
    if not text:
        return ""

    cleaned = (text or "").upper()
    cleaned = re.sub(r"https?://\S+", " ", cleaned)
    cleaned = re.sub(r"[@#]([A-Z0-9_]+)", r"\1", cleaned)
    cleaned = re.sub(r"\$([A-Z][A-Z0-9]{1,9})", r"\1", cleaned)
    cleaned = re.sub(r"[^A-Z0-9\s]", " ", cleaned)

    tokens = re.findall(r"[A-Z0-9]+", cleaned)
    if not tokens:
        return ""

    stop = {
        "BREAKING", "JUST", "IN", "SOURCE", "SOURCES", "REPORT", "REPORTS",
        "SAYS", "SAY", "RUMOR", "RUMORS", "THREAD", "UPDATE", "NEWS",
        "THE", "A", "AN", "TO", "FOR", "OF", "AND", "ON", "AT", "BY",
        "WITH", "FROM", "IS", "ARE", "WAS", "WERE", "AS", "THAT", "THIS",
    }
    filtered = [t for t in tokens if t not in stop]
    if not filtered:
        filtered = tokens

    # Keep the early semantic spine (order matters) while avoiding very long keys.
    return " ".join(filtered[:18])

# In-memory story-level dedupe (per process/run).
_STORY_KEYS: set[str] = set()
_STORY_LOCK = threading.Lock()


def _claim_story_slot(story_keys: tuple[str, ...]) -> bool:
    """
    Claim one local story slot for this process.

    The keys are intentionally high precision. Recurring scheduled data only
    contributes a broad story key when the period/date is explicit, which avoids
    treating a new monthly release as a duplicate of an older similar release.
    """
    keys = tuple(k for k in story_keys if k)
    if not keys:
        return True

    with _STORY_LOCK:
        if any(k in _STORY_KEYS for k in keys):
            return False
        _STORY_KEYS.update(keys)
        return True


_MATERIAL_SIGNAL_RE = re.compile(
    r"(\$[A-Z]{1,6}\b|\b(BTC|ETH|SOL|XRP|USDT|USDC|DOGE|BNB|TRX|ADA|AVAX|"
    r"BITCOIN|ETHEREUM|CRYPTO|STABLECOIN|ETF|FED|FOMC|CPI|PPI|PCE|NFP|GDP|"
    r"JOBLESS|INFLATION|TREASURY|TARIFF|RATE|YIELD|SEC|CFTC|DOJ|NASDAQ|NYSE|"
    r"AAPL|MSFT|NVDA|AMZN|META|TSLA|GOOGL|APPLE|MICROSOFT|NVIDIA|TESLA|"
    r"BINANCE|COINBASE|KRAKEN|TETHER|CIRCLE)\b|\d+(?:\.\d+)?\s?%|\$\d)",
    re.IGNORECASE,
)

_PROMO_ONLY_RE = re.compile(
    r"\b(watch live|listen live|join us|set a reminder|spaces?|ama|webinar|podcast|"
    r"subscribe|like and retweet|retweet to win|giveaway|airdrop|mint is live|"
    r"whitelist|discord|telegram|link in bio|sign up|register now)\b",
    re.IGNORECASE,
)

_EMPTY_CHATTER_RE = re.compile(
    r"^(gm|gn|good morning|lol|lfg|wagmi|soon|big soon|👀|🔥|🚀|\.\.\.)[\s!.🚀🔥👀]*$",
    re.IGNORECASE,
)


def _local_reject_reason(text: str) -> str | None:
    """
    Very liberal pre-GPT reject filter.

    This only removes obvious no-news items. Anything with a market/entity signal
    still flows to GPT so the model remains the main editorial decision-maker.
    """
    normalized = " ".join((text or "").split())
    if not normalized:
        return "empty_text"

    if _MATERIAL_SIGNAL_RE.search(normalized):
        return None

    words = re.findall(r"[A-Za-z0-9$%]+", normalized)
    if _EMPTY_CHATTER_RE.match(normalized):
        return "empty_chatter"

    if len(words) <= 4:
        return "too_short_without_market_signal"

    # URL-only / link-dump style posts with no detectable market signal.
    without_urls = re.sub(r"https?://\S+|www\.\S+", "", normalized, flags=re.IGNORECASE).strip()
    if not without_urls or len(re.findall(r"[A-Za-z0-9$%]+", without_urls)) <= 2:
        return "link_only_without_market_signal"

    # Promo-only items are skipped only when no material signal was found above.
    if _PROMO_ONLY_RE.search(normalized):
        return "promo_without_market_signal"

    return None


# ---------------------------- Main Entry ---------------------------- #


def analyze_tweet_importance(
    tweet_id,
    username,
    text,
    story_fp: StoryFingerprint | None = None,
    supporting_tweets=None,
):
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

    # ---------- (1) Local fallback headline + high-precision fingerprint (no GPT) ---------- #
    story_fp = story_fp or build_story_fingerprint(text)
    candidate_pre = story_fp.fallback_headline or generate_blockchain_daily_headline(text)
    supporting_tweets = supporting_tweets or [(tweet_id_str, username)]
    source_tweet_ids = [str(tid) for tid, _ in supporting_tweets]
    source_accounts = [acct for _, acct in supporting_tweets if acct]
    canonical_key = get_canonical_key(story_fp)

    # ---------- (2) Structured + legacy exact historical dedupe (pre-GPT) ---------- #
    # Structured registry checks are period-aware. Legacy exact headline checks are
    # only used where they cannot suppress a recurring story without a period.
    try:
        can_use_exact_history = (not story_fp.is_recurring) or bool(story_fp.period_key)
        structured_dup = has_historical_duplicate(story_fp)
        legacy_dup = can_use_exact_history and seen_full_preapi(candidate_pre)
        if structured_dup or legacy_dup:
            append_dedupe_audit(
                "historical_duplicate_skipped",
                tweet_id=tweet_id_str,
                username=username,
                canonical_key=canonical_key,
                reason="structured_registry" if structured_dup else "legacy_headline",
                is_recurring=story_fp.is_recurring,
                period_key=story_fp.period_key,
            )
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
    # Only the first thread to claim this high-precision story key will
    # proceed to GPT. Others will bail out to save tokens.
    try:
        claimed = _claim_story_slot(story_fp.batch_keys)
    except Exception as e:
        print(f"[DEDUP-WARN] _claim_story_slot failed for {tweet_id_str}: {e!r}")
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
        if is_local_duplicate(candidate_pre, threshold=0.78, tweet_text=text, story_fp=story_fp):
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

    # ---------- (5) Liberal local obvious-junk reject (pre-GPT) ---------- #
    reject_reason = _local_reject_reason(text)
    if reject_reason:
        return {
            "tweet_id": tweet_id_str,
            "importance_score": 0,
            "label": "low",
            "reason": reject_reason,
            "headline": None,
        }

    # ---------- (6) Single-call GPT publish decision + headline generation ---------- #
    score_data = analyze_tweet_for_publish(tweet_id_str, username, text)
    if score_data is None:
        return _fallback(tweet_id_str, "analysis_failed_no_api_key_or_gpt_error")

    score = int(score_data.get("importance_score", 0))
    label = score_data.get("label", "low")
    # score == 1 -> publish; score == 0 -> do NOT publish
    if score == 0:
        return {
            "tweet_id": tweet_id_str,
            "importance_score": score,
            "label": label,
            "headline": None,
        }

    candidate = score_data.get("headline") or candidate_pre

    # ---------- (7) Post-GPT local near-dup (compressed Jaccard) ---------- #
    # Compare GPT headline against last 100 compressed headlines.
    try:
        if is_local_duplicate(candidate, threshold=0.82, tweet_text=text, story_fp=story_fp):
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
        if gpt_is_duplicate(candidate, text, recent_compressed, story_fp=story_fp):
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
        if save_full_headline(candidate, allow_duplicate_key=story_fp.is_recurring):
            save_story_record(
                headline=candidate,
                fingerprint=story_fp,
                tweet_id=tweet_id_str,
                username=username,
                source_tweet_ids=source_tweet_ids,
                source_accounts=source_accounts,
            )
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
