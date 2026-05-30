# headline_dedupe.py
import re

from headline_store import get_all_compressed_headlines
from headline_compress import compress_headline_local
from story_dedupe import StoryFingerprint, build_story_fingerprint


_RECURRING_HISTORY_TERMS = {
    "CPI", "PPI", "PCE", "NFP", "PAYROLL", "PAYROLLS", "JOBLESS", "CLAIMS",
    "GDP", "ISM", "PMI", "RETAIL", "SALES", "UMICH", "INFLATION",
    "UNEMPLOYMENT", "EARNINGS", "EPS", "REVENUE",
}


def _token_set(text: str) -> set[str]:
    return set(re.findall(r"[A-Z0-9]+", (text or "").upper()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _is_recurring_candidate(
    candidate_headline: str,
    tweet_text: str = "",
    story_fp: StoryFingerprint | None = None,
) -> bool:
    fp = story_fp or build_story_fingerprint(f"{candidate_headline}\n{tweet_text}")
    return fp.is_recurring or bool(_token_set(candidate_headline) & _RECURRING_HISTORY_TERMS)


def is_local_duplicate(
    candidate_headline: str,
    threshold: float = 0.82,
    *,
    tweet_text: str = "",
    allow_recurring_history: bool = False,
    story_fp: StoryFingerprint | None = None,
) -> bool:
    """
    Compare a candidate headline against compressed headline history.

    For recurring scheduled stories (CPI/PPI/PCE/NFP/GDP/jobless claims,
    earnings, etc.), the function deliberately fails open by default. A May CPI
    print can look nearly identical to an April CPI print, and those should both
    be eligible to post unless an explicit period-aware duplicate layer proves
    they are the same release. Asset price moves also fail open because BTC down
    5% and BTC down 8% are different market updates even though their compressed
    headlines can be highly similar.
    """
    fp = story_fp or build_story_fingerprint(f"{candidate_headline}\n{tweet_text}")
    if fp.is_price_move:
        return False
    if not allow_recurring_history and _is_recurring_candidate(candidate_headline, tweet_text, fp):
        return False

    last = get_all_compressed_headlines()
    if not last:
        return False

    cand_comp = compress_headline_local(candidate_headline)
    cand_tokens = _token_set(cand_comp)
    if not cand_tokens:
        return False

    for h in last:
        h_tokens = _token_set(h)
        if _jaccard(cand_tokens, h_tokens) >= threshold:
            return True

    return False
