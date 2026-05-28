# headline_dedupe.py
import re

from headline_store import get_last_compressed_headlines
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
    Compare candidate headline (COMPRESSED token set) to last 100 COMPRESSED headlines.
    Returns True if any similarity >= threshold.
    """
    last = get_last_compressed_headlines(100)
    cand_comp = compress_headline_local(candidate_headline)
    cand_tokens = _token_set(cand_comp)

    for h in last:
        h_tokens = _token_set(h)
        if _jaccard(cand_tokens, h_tokens) >= threshold:
            return True

    return False
