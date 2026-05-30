# story_dedupe.py
"""
High-precision local story dedupe helpers.

Design goal: save GPT tokens by catching obvious duplicates early without
silencing legitimately new recurring stories (for example April CPI vs May CPI).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from local_headline_fallback import generate_blockchain_daily_headline
from headline_store import normalize_headline_for_key
from config import ACCOUNT_TRUST_SCORES


_STOPWORDS = {
    "BREAKING", "JUST", "IN", "SOURCE", "SOURCES", "REPORT", "REPORTS",
    "SAYS", "SAY", "RUMOR", "RUMORS", "THREAD", "UPDATE", "NEWS", "ICYMI",
    "THE", "A", "AN", "TO", "FOR", "OF", "AND", "ON", "AT", "BY", "WITH",
    "FROM", "IS", "ARE", "WAS", "WERE", "AS", "THAT", "THIS", "IT", "ITS",
    "NEW", "LATEST", "NOW", "TODAY", "LIVE", "DUE", "AMID", "STATUS", "PAGE",
}

_RECURRING_TERMS = {
    "CPI", "PPI", "PCE", "NFP", "PAYROLLS", "PAYROLL", "JOBS", "JOBLESS",
    "CLAIMS", "GDP", "ISM", "PMI", "RETAIL", "SALES", "UMICH", "INFLATION",
    "UNEMPLOYMENT", "FOMC", "FED", "RATE", "RATES", "EARNINGS", "EPS",
    "REVENUE", "CPIY", "CORE",
}

_MONTHS = {
    "JAN", "JANUARY", "FEB", "FEBRUARY", "MAR", "MARCH", "APR", "APRIL",
    "MAY", "JUN", "JUNE", "JUL", "JULY", "AUG", "AUGUST", "SEP", "SEPT",
    "SEPTEMBER", "OCT", "OCTOBER", "NOV", "NOVEMBER", "DEC", "DECEMBER",
}

_ACTION_NORMALIZATIONS = {
    "APPROVES": "APPROVE", "APPROVED": "APPROVE",
    "FILES": "FILE", "FILED": "FILE",
    "LAUNCHES": "LAUNCH", "LAUNCHED": "LAUNCH",
    "LISTS": "LIST", "LISTED": "LIST",
    "HALTS": "HALT", "HALTED": "HALT",
    "HACKED": "HACK", "EXPLOITED": "EXPLOIT",
    "SUES": "SUE", "SUED": "SUE",
    "CHARGES": "CHARGE", "CHARGED": "CHARGE",
    "SETTLES": "SETTLE", "SETTLED": "SETTLE",
    "BUYS": "BUY", "BOUGHT": "BUY",
    "SELLS": "SELL", "SOLD": "SELL",
    "ACQUIRES": "ACQUIRE", "ACQUIRED": "ACQUIRE",
    "PARTNERS": "PARTNER", "PARTNERED": "PARTNER",
    "RAISES": "RAISE", "RAISED": "RAISE",
    "CUTS": "CUT", "HIKES": "HIKE",
    "BEATS": "BEAT", "MISSES": "MISS",
    "FALLS": "FALL", "RISES": "RISE", "JUMPS": "JUMP", "DROPS": "DROP",
    "SURGES": "SURGE", "SINKS": "SINK", "SLIDES": "SLIDE", "RALLIES": "RALLY",
    "MINTS": "MINT", "BURNS": "BURN",
    "TRANSFERS": "TRANSFER",
}

_ACTION_WORDS = {
    "APPROVES", "APPROVE", "APPROVED", "FILES", "FILE", "FILED", "LAUNCHES",
    "LAUNCH", "LISTS", "LIST", "HALTS", "HALT", "HACKED", "HACK", "EXPLOIT",
    "EXPLOITED", "OUTAGE", "SUES", "SUE", "CHARGES", "CHARGE", "SETTLES",
    "SETTLE", "BUYS", "BUY", "SELLS", "SELL", "ACQUIRES", "ACQUIRE",
    "PARTNERS", "PARTNER", "RAISES", "RAISE", "CUTS", "CUT", "HIKES", "HIKE",
    "BEATS", "MISSES", "MISS", "FALLS", "FALL", "RISES", "RISE", "JUMPS",
    "JUMP", "DROPS", "DROP", "SURGES", "SURGE", "SINKS", "SINK", "SLIDES",
    "SLIDE", "RALLIES", "RALLY", "DOWN", "UP", "MINTS", "MINT", "BURNS",
    "BURN", "TRANSFER", "TRANSFERS",
}

_PRICE_MOVE_ACTIONS = {
    "UP", "DOWN", "RISE", "RISES", "FALL", "FALLS", "JUMP", "JUMPS",
    "DROP", "DROPS", "SURGE", "SURGES", "SINK", "SINKS", "SLIDE", "SLIDES",
    "RALLY", "RALLIES", "GAIN", "GAINS", "LOSE", "LOSES", "PLUNGE", "PLUNGES",
}

_PRICE_MOVE_MARKET_TERMS = {
    "SHARES", "STOCK", "STOCKS", "TOKEN", "TOKENS", "COIN", "COINS", "CRYPTO",
    "BTC", "BITCOIN", "ETH", "ETHEREUM", "SOL", "XRP", "DOGE", "BNB", "ADA",
    "AVAX", "TRX", "ZEC", "LTC", "LINK", "SUI", "HYPE",
}


@dataclass(frozen=True)
class StoryFingerprint:
    fallback_headline: str
    exact_key: str
    entity_action_key: str
    token_set: frozenset[str]
    is_recurring: bool
    is_price_move: bool
    period_key: str

    @property
    def batch_keys(self) -> tuple[str, ...]:
        """Keys safe enough for same-batch duplicate grouping."""
        keys: list[str] = []
        if self.exact_key:
            keys.append(f"exact:{self.exact_key}")
        if self.entity_action_key and not self.is_price_move:
            if self.is_recurring:
                # Recurring data can be deduped locally only when the release
                # period/date is explicit. Otherwise, fail open to avoid hiding
                # this month's data because last month's looked similar.
                if self.period_key:
                    keys.append(f"recurring:{self.period_key}:{self.entity_action_key}")
            else:
                keys.append(f"story:{self.entity_action_key}")
        return tuple(keys)


def _clean_text(text: str) -> str:
    t = (text or "").upper()
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[@#]([A-Z0-9_]+)", r"\1", t)
    t = re.sub(r"\$([A-Z][A-Z0-9]{1,9})", r"\1", t)
    t = re.sub(r"[^A-Z0-9.%\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def story_tokens(text: str) -> list[str]:
    cleaned = _clean_text(text)
    tokens = re.findall(r"[A-Z0-9]+(?:\.[0-9]+)?%?", cleaned)
    return [
        _ACTION_NORMALIZATIONS.get(t, t)
        for t in tokens
        if t and t not in _STOPWORDS
    ]


def _extract_period_key(tokens: list[str]) -> str:
    period: list[str] = []
    for idx, tok in enumerate(tokens):
        if tok in _MONTHS:
            period.append(tok[:3])
            # Include a neighboring year if present.
            for neighbor in (idx - 1, idx + 1):
                if 0 <= neighbor < len(tokens) and re.fullmatch(r"20\d{2}", tokens[neighbor]):
                    period.append(tokens[neighbor])
        elif re.fullmatch(r"20\d{2}", tok):
            period.append(tok)
        elif re.fullmatch(r"Q[1-4]", tok):
            period.append(tok)
    return " ".join(dict.fromkeys(period))


def _entity_action_key(tokens: list[str]) -> str:
    important: list[str] = []
    for tok in tokens:
        keep = (
            tok in _ACTION_WORDS
            or tok in _RECURRING_TERMS
            or tok in _MONTHS
            or bool(re.search(r"\d", tok))
            or (tok.isalpha() and len(tok) >= 3 and len(important) < 8)
        )
        if keep:
            important.append(tok)
        if len(important) >= 14:
            break
    return " ".join(important)


def _is_asset_price_move(raw_text: str, tokens: list[str], is_recurring: bool) -> bool:
    """
    Identify market tape-style asset price moves.

    These stories often share only an asset and a direction (for example BTC down
    5%, then BTC down 8%). Treating that broad shape as a duplicate hides fresh
    market moves, so only exact-headline safeguards should suppress them.
    """
    if is_recurring:
        return False

    token_set = set(tokens)
    has_move = bool(token_set & _PRICE_MOVE_ACTIONS)
    has_percent = any(re.fullmatch(r"\d+(?:\.\d+)?%", tok) for tok in tokens)
    has_market_term = bool(token_set & _PRICE_MOVE_MARKET_TERMS)
    has_cash_ticker = bool(re.search(r"\$[A-Z]{1,6}\b", raw_text or ""))

    return has_move and has_percent and (has_market_term or has_cash_ticker)


def build_story_fingerprint(text: str) -> StoryFingerprint:
    fallback = generate_blockchain_daily_headline(text)
    exact_key = normalize_headline_for_key(fallback)
    tokens = story_tokens(text or fallback)
    token_set = frozenset(tokens)
    is_recurring = bool(token_set & _RECURRING_TERMS)
    is_price_move = _is_asset_price_move(text or fallback, tokens, is_recurring)
    period_key = _extract_period_key(tokens)
    return StoryFingerprint(
        fallback_headline=fallback,
        exact_key=exact_key,
        entity_action_key=_entity_action_key(tokens),
        token_set=token_set,
        is_recurring=is_recurring,
        is_price_move=is_price_move,
        period_key=period_key,
    )


def token_jaccard(a: frozenset[str] | set[str], b: frozenset[str] | set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


def likely_same_batch_story(a: StoryFingerprint, b: StoryFingerprint) -> bool:
    """High-precision same-batch duplicate check."""
    if set(a.batch_keys) & set(b.batch_keys):
        return True

    if a.is_price_move or b.is_price_move:
        # Price moves need exact-key equality to dedupe; same asset/direction with
        # a changed magnitude is a fresh market update.
        return False

    if a.is_recurring or b.is_recurring:
        # For recurring data, only same explicit period can be auto-merged.
        if not a.period_key or a.period_key != b.period_key:
            return False

    jacc = token_jaccard(a.token_set, b.token_set)
    seq = SequenceMatcher(None, a.entity_action_key, b.entity_action_key).ratio()
    return jacc >= 0.62 and seq >= 0.72


def _normalized_handle(username: str) -> str:
    handle = (username or "").strip().lower()
    return handle if handle.startswith("@") else f"@{handle}" if handle else ""


def account_trust_score(username: str) -> float:
    """Small, configurable boost for choosing the best duplicate representative."""
    handle = _normalized_handle(username)
    if not handle:
        return -0.15

    configured = ACCOUNT_TRUST_SCORES.get(handle) or ACCOUNT_TRUST_SCORES.get(handle.lstrip("@"))
    if configured is not None:
        try:
            return float(configured)
        except (TypeError, ValueError):
            return 0.0

    # Conservative generic defaults; operators can override via
    # ACCOUNT_TRUST_SCORES_JSON for their own source list.
    official_markers = ("secgov", "federalreserve", "treasury", "cftc", "whitehouse")
    if any(marker in handle for marker in official_markers):
        return 1.0
    if handle == "@unknown":
        return -0.25
    return 0.0


def representative_score(username: str, text: str, fingerprint: StoryFingerprint) -> float:
    """Pick the richest/clearest tweet from a duplicate group."""
    score = 0.0
    length = len(text or "")
    score += min(length, 280) / 280.0
    score += min(len(fingerprint.token_set), 30) / 30.0
    if any(ch.isdigit() for ch in text or ""):
        score += 0.35
    if fingerprint.period_key:
        score += 0.25
    score += account_trust_score(username)
    if re.search(r"\b(rumor|unconfirmed|source|sources)\b", text or "", flags=re.I):
        score -= 0.30
    return score
