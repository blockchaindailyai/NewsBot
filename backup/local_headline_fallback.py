# local_headline_fallback.py
import re

_KEYWORDS = {
    "launch", "file", "files", "approve", "approves", "approval", "reject", "rejects",
    "list", "lists", "delist", "delists", "halt", "halts", "outage", "exploit", "hack",
    "breach", "merger", "acquire", "acquires", "deal", "partnership", "collab", "collaboration",
    "raises", "raise", "funding", "etf", "inflows", "outflows", "policy", "regulation",
    "sec", "cftc", "treasury", "bank", "banks", "listing", "tokenize", "settle", "settlement",
    "trade", "trading", "integrate", "integration", "support", "adds", "enable", "enables",
    "announces", "announced", "says", "reports", "downtime", "incident", "vulnerability",
    "upgrade", "mainnet", "testnet", "beta",
}
_STOP_HEADERS = {
    "news update", "daily update", "weekly update",
    "roundup", "highlights", "today", "november",
    "october", "september"
}


def _headline_sentence_score(s: str) -> float:
    s_up = s.upper()
    tokens = re.findall(r"[A-Z0-9]+", s_up)
    words = set(t.lower() for t in tokens)
    header_pen = any(h in s.lower() for h in _STOP_HEADERS)

    kw_hits = sum(1 for k in _KEYWORDS if k in words or k.upper() in s_up)
    has_digits = bool(re.search(r"\d", s))
    has_and = " AND " in s_up
    length = len(s)

    score = 0.0
    score += 0.25 * kw_hits
    if has_digits:
        score += 0.15
    if has_and:
        score += 0.05

    if 45 <= length <= 140:
        score += 0.25
    elif 30 <= length < 45 or 140 < length <= 220:
        score += 0.12

    if header_pen:
        score -= 0.35

    if re.search(
        r"\b(launch|file|approve|list|delist|halt|outage|exploit|hack|acquire|deal|partner|support|enable|tokenize|settle|trade|announce|reports|add|integrate)\w*\b",
        s,
        flags=re.I,
    ):
        score += 0.15

    return max(0.0, min(1.0, score))


def _is_redundant(a: str, b: str) -> bool:
    A = set(re.findall(r"[A-Z0-9]+", (a or "").upper()))
    B = set(re.findall(r"[A-Z0-9]+", (b or "").upper()))
    if not A or not B:
        return False
    inter = len(A & B) / max(1, len(B))
    return inter >= 0.70


def _final_headline_cleanup(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    s = s.rstrip(" .!;:–—")
    s = re.sub(r"( AND ){2,}", " AND ", s)
    if len(s) < 12:
        s = "MAJOR DEVELOPING STORY"
    return s


def generate_blockchain_daily_headline(text: str) -> str:
    """
    Rule-based fallback headline builder (local, no API).
    Creates: 🚨#BREAKING: [ALL CAPS HEADLINE] from the FULL tweet.
    """
    if not text or not text.strip():
        return "🚨#BREAKING: MAJOR DEVELOPING STORY"

    def strip_alert_prefix(s: str) -> str:
        return re.sub(
            r"^(🚨|\s)*\s*(#BREAKING|BREAKING|JUST IN|#속보|속보)[:\-\s]*",
            "",
            s,
            flags=re.IGNORECASE,
        )

    def replace_symbols(s: str) -> str:
        s = re.sub(r"@([A-Za-z0-9_]+)", lambda m: m.group(1).upper(), s)
        s = re.sub(r"#([A-Za-z0-9_]+)", lambda m: m.group(1).upper(), s)
        s = re.sub(r"\$([A-Za-z][A-Za-z0-9]{1,9})", lambda m: m.group(1).upper(), s)
        s = re.sub(r"https?://\S+", "", s)
        return s

    def normalize_connectors(s: str) -> str:
        return re.sub(r"\s*&\s*|\s*\+\s*|\/", " and ", s)

    lines = [ln.strip() for ln in text.splitlines() if ln and ln.strip()]
    lines = [normalize_connectors(replace_symbols(strip_alert_prefix(ln))) for ln in lines]
    full = re.sub(r"\s+", " ", " ".join(lines)).strip()

    parts = re.split(r"(?<=[\.\!\?])\s+|[\u2014–—]\s+|\s{2,}", full)
    chunks: list[str] = []
    for p in parts:
        p = p.strip(" .!;:—–")
        if not p:
            continue
        if len(p) > 180:
            chunks.extend([c.strip() for c in re.split(r",\s+(?=[A-Z])", p) if c.strip()])
        else:
            chunks.append(p)
    if not chunks:
        chunks = [full]

    best = max(chunks, key=_headline_sentence_score)

    headline_core = best
    if len(best) < 55:
        for s in chunks:
            if s == best:
                continue
            if _headline_sentence_score(s) < 0.35:
                continue
            if _is_redundant(headline_core, s):
                continue
            headline_core = f"{headline_core} — {s}"
            break

    headline_core = _final_headline_cleanup(headline_core)
    headline_core = headline_core.upper() if headline_core else "MAJOR DEVELOPING STORY"
    return f"🚨#BREAKING: {headline_core}"
