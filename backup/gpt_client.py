import os
import re
import json
from typing import Optional

from openai import OpenAI
from headline_compress import compress_headline_local

API_KEY = os.getenv("OPENAI_API_KEY")
if not API_KEY:
    print("[GPT-WARN] OPENAI_API_KEY is not set. Importance analysis will always fail.")
client = OpenAI(api_key=API_KEY) if API_KEY else None


def _token_set(text: str) -> set[str]:
    """
    Simple tokenization used for keyword-overlap prefiltering in dedupe.
    Uppercases and keeps only [A-Z0-9]+ chunks.
    """
    return set(re.findall(r"[A-Z0-9]+", (text or "").upper()))


def score_tweet_importance(tweet_id: str, username: str, text: str) -> Optional[dict]:
    """
    Call GPT to score tweet importance. Returns dict or None if API not available/fails.
    """
    if client is None:
        return None

    system_prompt = (
        "You score a tweet's importance for a real-time crypto/stock/macroeconomic news alert account. "
        "Higher score = more urgent, market-moving, interesting, or likely to go viral.\n\n"
        "Stories don't have to be market-moving, just interesting"
        "FIRST CHECK:\n"
        "- Is this a FRESH story? If yes continue analysis, if not score <10 (reason = not fresh)"
        "PRIORITIZE:\n"
        "- Gov/regulation, sanctions, lawsuits, central banks, major policy or economic actions.\n"
        "- Crypto-specific: hacks/exploits, chain halts, outages, listings/delistings, ETF/IPO filings, "
        "major exchange/project updates, acquisitions, funding, bankruptcies, wallet flows, big buys/sells, "
        "mainnet/testnet upgrades, state/federal/local/foreign policy, lawsuits\n"
        "- Corporate news from any public and major private crypto companies, incl. crypto buys/sells, financial data, filings, major events, etc.\n"
        "- Large liquidations or significant price moves (crypto, stocks, FX, commodities).\n"
        "- Major economic data (US/JP/EU/CN/SK/AU), esp. surprise beats/misses.\n"
        "- Tariff/trade actions.\n"
        "- Important corporate news from major public companies.\n"
        "- Higher credibility for official gov/institutions/exchanges and reputable media "
        "- Popular, controversial, well-known, or important individual's comments about crypto, especially executives, top-tier influencers, or officials"
        "(Bloomberg, CNBC, Reuters, CoinDesk, Cointelegraph, TheBlock, Decrypt).\n\n"
        "DOWNGRADE:\n"
        "- Tweets only saying WATCH LIVE / INTERVIEW / SPACES / AMA / LISTEN NOW with no concrete data "
        "or decisions → usually ≤25.\n"
        "- Memes, generic TA/sentiment, vague claims, unknown/low-cred sources.\n\n"
        "- Regional conflicts (eg. Israel, Ukraine, etc.) EXCEPT MAJOR NEW developments, AVOID incremental developments\n\n"
        "CRYPTO RELEVANCE RULE:\n"
        "- Meaningfully crypto-related → add +25 vs similar non-crypto story.\n"
        "- No clear crypto relevance → score ≤20.\n\n"
        "SCORES:\n"
        "0–20 low | 21–49 medium | 50–79 high | 80–100 critical.\n\n"
        "Respond ONLY with a strict JSON object."
    )

    user_prompt = f"""
Tweet ID: {tweet_id}
Tweet Text: {text}
Tweet Handle: {username}

Return JSON exactly in this format:
{{
  "tweet_id": "{tweet_id}",
  "importance_score": <0-100>,
  "label": "low" | "medium" | "high" | "critical",
  "reason": "<short one-line explanation of why you chose this score>"
}}
""".strip()

    try:
        resp = client.chat.completions.create(
            model="gpt-5-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

        raw = (resp.choices[0].message.content or "").strip()
        if not raw.startswith("{"):
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end != -1 and end > start:
                raw = raw[start: end + 1]

        data = json.loads(raw)

        score = int(data.get("importance_score", 0))
        label = str(data.get("label", "low")).lower().strip()
        reason = str(data.get("reason", "")).strip() or "no_reason_provided"

        score = max(0, min(100, score))
        if label not in ("low", "medium", "high", "critical"):
            label = (
                "critical" if score >= 80
                else "high" if score >= 50
                else "medium" if score >= 21
                else "low"
            )

        return {
            "tweet_id": str(data.get("tweet_id", tweet_id)),
            "importance_score": score,
            "label": label,
            "reason": reason,
        }

    except Exception as e:
        print(f"[GPT-ERROR] score_tweet_importance failed for {tweet_id}: {e}")
        return None


def generate_headline_with_gpt(text: str) -> Optional[str]:
    """
    GPT-powered headline builder (uses full tweet).
    Returns: 🚨#BREAKING: [ALL CAPS WIRE HEADLINE] or None on failure.
    """
    if client is None:
        return None

    prompt = f"""
Write a concise ALL-CAPS Bloomberg/Reuters-style headline summarizing this tweet.
Prefix with: 🚨#BREAKING:
No emojis except the leading one. Keep <140 chars.

RULES:
- NEVER invent facts. Use ONLY what is explicitly written.
- If no clear action/outcome/statement/numbers/decision, DO NOT guess.
- If tweet is only a promo (WATCH LIVE, INTERVIEW, SPACES, AMA, LISTEN NOW) AND gives no summary:
    → Write a simple 'LIVE INTERVIEW WITH X' style headline using ONLY given names/roles.
    → Do NOT add motives, reasons, speculation, or implied context.
- If tweet DOES summarize content:
    → Focus on the substantive event (action/decision/claim/denial/data).

FORMAT:
- Uppercase English.
- No hashtags except '#BREAKING' and cashtags ($BTC, $AAPL).
- Capture the most newsworthy entity + action + impact.
- Strip fluff, quotes, emojis, and ad language.

Tweet:
{text}
""".strip()

    try:
        resp = client.chat.completions.create(
            model="gpt-5.1",
            messages=[
                {"role": "system", "content": "You are an experienced financial news editor."},
                {"role": "user", "content": prompt},
            ],
        )
        headline = (resp.choices[0].message.content or "").strip()
        m = re.search(r"(🚨#BREAKING:[^\n\r]*)", headline, flags=re.IGNORECASE)
        if m:
            headline = m.group(1).strip()
        else:
            headline = f"🚨#BREAKING: {headline.strip().upper()}"
        headline = re.sub(r"\s+", " ", headline).strip()
        return headline
    except Exception as e:
        print(f"[GPT-HEADLINE-ERROR] {e}")
        return None


def gpt_is_duplicate(candidate_headline: str, tweet_text: str, recent_compressed_headlines: list[str]) -> bool:
    """
    Use GPT to decide if candidate_headline describes essentially the same story
    as any of the last N compressed headlines.

    To reduce token usage, we first prefilter recent_compressed_headlines by
    simple keyword overlap with the candidate, then only send the most similar
    subset to GPT.
    """
    if client is None:
        return False
    if not recent_compressed_headlines:
        return False

    compressed_candidate = compress_headline_local(candidate_headline)
    cand_tokens = _token_set(compressed_candidate)
    if not cand_tokens:
        return False

    # ---------- Keyword-overlap prefilter ----------
    scored: list[tuple[float, str]] = []
    for h in recent_compressed_headlines:
        h_tokens = _token_set(h)
        if not h_tokens:
            continue
        inter = len(cand_tokens & h_tokens)
        if inter == 0:
            continue
        union = len(cand_tokens | h_tokens)
        jacc = inter / union if union else 0.0

        # Keep if there is meaningful overlap:
        # - at least 2 shared tokens OR
        # - Jaccard similarity above a small threshold.
        if inter >= 2 or jacc >= 0.18:
            scored.append((jacc, h))

    if not scored:
        # No prior headlines share meaningful keywords → very unlikely to be a dup.
        return False

    # Keep only top-N most similar to keep prompts short
    scored.sort(key=lambda x: x[0], reverse=True)
    TOP_N = 20
    filtered_headlines = [h for _, h in scored[:TOP_N]]

    numbered = "\n".join(
        f"{idx+1}. {h}"
        for idx, h in enumerate(filtered_headlines)
    )

    system_prompt = (
        "You are an assistant editor for a real-time crypto news account.\n"
        "Your job is to detect if a NEW headline is essentially the SAME underlying story "
        "as any of the RECENT headlines.\n\n"
        "Two headlines are considered the SAME STORY if they describe the same core event "
        "(same entity + same action + same event), even if the wording is different.\n"
        "If they are about different events, even with similar entities, treat them as NOT duplicates.\n\n"
        "Respond ONLY with a compact JSON object."
    )

    user_prompt = f"""
Here are recent compressed headlines (most recent first):

{numbered}

Candidate compressed headline:
"{compressed_candidate}"

Full tweet text (for extra context):
"{tweet_text}"

Return JSON in exactly this format:

{{
  "is_duplicate": true or false,
  "match_index": <integer index of matching prior headline starting from 1, or -1 if none>,
  "reason": "<one-sentence explanation>"
}}
""".strip()

    try:
        resp = client.chat.completions.create(
            model="gpt-5-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
        if not raw.startswith("{"):
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end != -1 and end > start:
                raw = raw[start: end + 1]

        data = json.loads(raw)
        return bool(data.get("is_duplicate", False))
    except Exception as e:
        print(f"[GPT-DEDUP-ERROR] {e}")
        return False
