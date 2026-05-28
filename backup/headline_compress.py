import re
import unicodedata

# Abbreviations for compression (can be expanded)
_ABBREVS = {
    r"\bbasis points?\b": "bps",
    r"\binterest rates?\b": "rates",
    r"\bexchange[- ]traded funds?\b": "ETF",
    r"\bcryptocurrencies?\b": "crypto",
    r"\bcrypto(?:currency)?\b": "crypto",
    r"\bsecurities and exchange commission\b": "SEC",
    r"\bfederal reserve\b": "FED",
    r"\bpercent\b": "%",
    r"\bper cent\b": "%",
    r"\bpoints?\b": "pts",

    # extra finance / macro
    r"\bmillion\b": "M",
    r"\bbillion\b": "B",
    r"\btrillion\b": "T",
    r"\bdollars?\b": "USD",
    r"\busd\b": "USD",
    r"\beuros?\b": "EUR",
    r"\beur\b": "EUR",
    r"\byen\b": "JPY",
    r"\bjpy\b": "JPY",
    r"\byuan\b": "CNY",
    r"\bcny\b": "CNY",
    r"\btreasur(?:y|ies)\b": "UST",
    r"\byields?\b": "YLD",
    r"\bshares?\b": "SHRS",
    r"\bstocks?\b": "STKS",
    r"\bfutures?\b": "FUTS",
}

# Phrases that usually don't add much for a compressed memory headline
_FLUFF = [
    r"according to .*",
    r"people familiar with the matter.*",
    r"sources (say|said).*",
    r"in a statement.*",
    r"reports (say|suggest).*",
    r"amid concerns.*",
    r"as the market reacts.*",
    r"in latest development.*",

    # extra fluff / promo
    r"watch live.*",
    r"watch now.*",
    r"tune in.*",
    r"join us.*",
    r"full (story|coverage).*",
    r"more details.*",
    r"click here.*",
    r"read more.*",
    r"live (blog|updates?).*",
]

# High-frequency glue words we can drop from the compressed form
_STOPWORDS = {
    "THE", "A", "AN",
    "OF", "ON", "IN", "AT", "TO", "FOR", "FROM", "WITH", "WITHOUT",
    "AND", "OR", "BUT",
    "IF", "AS", "BY", "ABOUT", "OVER", "UNDER",
    "NEW", "LATEST", "JUST", "BREAKING", "LIVE",
    "UPDATE", "UPDATES",
    "SAY", "SAYS", "SAID", "REPORTS", "REPORTED",
    "ANNOUNCE", "ANNOUNCES", "ANNOUNCED",
}


def _strip_urls(text: str) -> str:
    return re.sub(r"https?://\S+|www\.\S+", "", text)


def _strip_emojis(text: str) -> str:
    """
    Remove most emoji/symbol characters, but keep some finance-relevant ASCII.
    """
    kept = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat in ("So", "Cs"):
            if ch in "$€£¥%+-.,:;/":
                kept.append(ch)
            else:
                continue
        else:
            kept.append(ch)
    return "".join(kept)


def _semantic_shorten(text: str) -> str:
    """
    Apply small, domain-specific semantic shortening:
      - abbreviations
      - removal of fluff clauses
      - collapse whitespace
      - uppercase for wire style.
    """
    if not text:
        return ""

    t = text.lower()

    # abbreviations
    for pattern, repl in _ABBREVS.items():
        t = re.sub(pattern, repl, t)

    # remove fluff
    for junk in _FLUFF:
        t = re.sub(junk, "", t)

    # collapse whitespace
    t = re.sub(r"\s+", " ", t).strip()

    # uppercase for news-wire style
    t = t.upper()
    return t


def _remove_stopwords_preserve_numbers(text: str) -> str:
    """
    Remove common glue words but keep:
      - tokens containing digits
      - tokens containing cashtags or % signs
    """
    if not text:
        return ""

    tokens = text.split()
    cleaned = []
    for tok in tokens:
        t_up = tok.upper()

        # numbers / cashtags / percents are always kept
        if any(ch.isdigit() for ch in tok) or "$" in tok or "%" in tok:
            cleaned.append(tok)
            continue

        if t_up in _STOPWORDS:
            continue

        cleaned.append(tok)

    return " ".join(cleaned)


def compress_headline_local(headline: str, max_len: int = 100, max_words: int = 15) -> str:
    """
    Full compression pipeline combining mechanical + semantic steps for GPT headlines.

    We assume input looks like:
      '🚨#BREAKING: FEDERAL RESERVE CUTS INTEREST RATES BY 25 BASIS POINTS...'

    Steps:
      - Remove URLs & emojis
      - Strip '🚨#BREAKING:' and similar alert prefixes
      - Semantic shortening (abbreviations, fluff removal)
      - Drop stopwords / glue words, keep numbers & cashtags
      - Limit to max_words and max_len
    """
    if not headline:
        return ""

    # 1) Remove URLs and emojis
    t = _strip_urls(headline)
    t = _strip_emojis(t)

    # 2) Strip common alert prefixes
    t = re.sub(
        r"^(🚨|\s)*\s*(#?BREAKING|JUST IN)[:\-\s]*",
        "",
        t,
        flags=re.IGNORECASE,
    ).strip()

    # 3) Normalize whitespace first
    t = re.sub(r"\s+", " ", t).strip()

    # 4) Semantic shortening (abbr, fluff removal, uppercase)
    t = _semantic_shorten(t)

    # 5) Remove stopwords while preserving numbers / cashtags
    t = _remove_stopwords_preserve_numbers(t)

    # 6) Truncate by words
    words = t.split()
    if len(words) > max_words:
        t = " ".join(words[:max_words])

    # 7) Truncate by characters
    if len(t) > max_len:
        t = t[: max_len - 1].rstrip() + "…"

    return t
