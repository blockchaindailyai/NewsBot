# headline_dedupe.py
import re

from headline_store import get_all_compressed_headlines

from headline_compress import compress_headline_local


def _token_set(text: str) -> set[str]:
    return set(re.findall(r"[A-Z0-9]+", (text or "").upper()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def is_local_duplicate(candidate_headline: str, threshold: float = 0.82) -> bool:
    """
    Compare candidate headline (COMPRESSED token set) to ALL COMPRESSED headlines
    on disk. Returns True if any similarity >= threshold.

    Full-history Jaccard means even day-old (or older) stories will still be
    seen as duplicates if they are essentially the same headline.
    """
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

