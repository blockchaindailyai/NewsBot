import os
import re
import json
import time
import random
import logging
from typing import Optional

from openai import OpenAI
from headline_compress import compress_headline_local
from story_dedupe import StoryFingerprint, build_story_fingerprint
from config import (
    OPENAI_TIMEOUT_SECONDS,
    OPENAI_RETRY_ATTEMPTS,
    OPENAI_RETRY_BASE_DELAY,
    OPENAI_FAILURE_COOLDOWN_SECONDS,
)

API_KEY = os.getenv("OPENAI_API_KEY")
if not API_KEY:
    print("[GPT-WARN] OPENAI_API_KEY is not set. Importance analysis will always fail.")
client = OpenAI(api_key=API_KEY) if API_KEY else None
logger = logging.getLogger("gpt_client")
_failure_until = 0.0

_DEDUPE_TOP_N = 25
_DEDUPE_GPT_MIN_SCORE = 0.20
_DEDUPE_CONTEXT_MAX_CHARS = 260

_DEDUPE_ACTION_TERMS = {
    "APPROVE", "APPROVES", "APPROVED", "FILE", "FILES", "FILED",
    "LAUNCH", "LAUNCHES", "LAUNCHED", "LIST", "LISTS", "LISTED",
    "HALT", "HALTS", "HALTED", "HACK", "HACKED", "EXPLOIT", "EXPLOITED",
    "SUE", "SUES", "SUED", "CHARGE", "CHARGES", "CHARGED",
    "SETTLE", "SETTLES", "SETTLED", "BUY", "BUYS", "BOUGHT",
    "SELL", "SELLS", "SOLD", "ACQUIRE", "ACQUIRES", "ACQUIRED",
    "PARTNER", "PARTNERS", "PARTNERED", "RAISE", "RAISES", "RAISED",
    "CUT", "CUTS", "HIKE", "HIKES", "BEAT", "BEATS", "MISS", "MISSES",
    "FALL", "FALLS", "RISE", "RISES", "JUMP", "JUMPS", "DROP", "DROPS",
    "MINT", "MINTS", "BURN", "BURNS", "TRANSFER", "TRANSFERS",
    "OUTAGE", "DEPEG", "DEFAULT", "BANKRUPTCY", "INVEST", "INVESTS",
}


def _chat_completion_with_retry(**kwargs):
    global _failure_until
    now = time.time()
    if now < _failure_until:
        return None

    for attempt in range(1, OPENAI_RETRY_ATTEMPTS + 1):
        try:
            return client.chat.completions.create(timeout=OPENAI_TIMEOUT_SECONDS, **kwargs)
        except Exception as e:
            if attempt >= OPENAI_RETRY_ATTEMPTS:
                logger.error("OpenAI request failed after retries: %r", e)
                _failure_until = time.time() + OPENAI_FAILURE_COOLDOWN_SECONDS
                return None
            delay = OPENAI_RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
            logger.warning("OpenAI request failed (attempt %s/%s): %r; retrying in %.2fs", attempt, OPENAI_RETRY_ATTEMPTS, e, delay)
            time.sleep(delay)


def _token_set(text: str) -> set[str]:
    """
    Simple tokenization used for keyword-overlap prefiltering in dedupe.
    Uppercases and keeps only [A-Z0-9]+ chunks.
    """
    return set(re.findall(r"[A-Z0-9]+", (text or "").upper()))


def _dedupe_score(
    cand_tokens: set[str],
    headline_tokens: set[str],
    candidate_fp: StoryFingerprint,
) -> float:
    """Rank prior headlines by same-story risk before spending GPT tokens."""
    if not cand_tokens or not headline_tokens:
        return 0.0

    shared = cand_tokens & headline_tokens
    union = cand_tokens | headline_tokens
    jacc = len(shared) / len(union) if union else 0.0
    score = jacc

    if shared & _DEDUPE_ACTION_TERMS:
        score += 0.12

    if any(re.search(r"\d", tok) for tok in shared):
        score += 0.10

    fp_tokens = _token_set(candidate_fp.entity_action_key)
    if fp_tokens and shared & fp_tokens:
        score += min(0.12, 0.03 * len(shared & fp_tokens))

    return score


def _dedupe_context_excerpt(tweet_text: str) -> str:
    """Return a compact source excerpt only for ambiguous dedupe prompts."""
    cleaned = re.sub(r"https?://\S+|www\.\S+", " ", tweet_text or "", flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= _DEDUPE_CONTEXT_MAX_CHARS:
        return cleaned
    return cleaned[: _DEDUPE_CONTEXT_MAX_CHARS - 1].rstrip() + "…"


def _should_include_dedupe_context(compressed_candidate: str, cand_tokens: set[str]) -> bool:
    """Avoid sending full tweet text unless the headline is too sparse to compare."""
    if len(cand_tokens) < 6:
        return True
    has_action = bool(cand_tokens & _DEDUPE_ACTION_TERMS)
    has_number = any(re.search(r"\d", tok) for tok in cand_tokens)
    if not has_action and not has_number:
        return True
    return len(compressed_candidate or "") < 45


def _normalize_generated_headline(headline: str) -> Optional[str]:
    if not headline:
        return None

    headline = str(headline).strip()

    # Try to extract the first line that begins with the alert emoji.
    m = re.search(r"(🚨[^\n\r]*)", headline)
    if m:
        headline = m.group(1).strip()

    if not headline:
        return None

    # Ensure a single leading 🚨.
    if not headline.startswith("🚨"):
        headline = f"🚨 {headline}"

    # Strip BREAKING/#BREAKING immediately after the emoji; the account name
    # already signals urgency and this avoids repetitive spam-like wording.
    headline = re.sub(
        r"^🚨\s*#?BREAKING[:\-\s]*\s*",
        "🚨 ",
        headline,
        flags=re.IGNORECASE,
    )

    headline = re.sub(r"\s+", " ", headline).strip()

    if headline.startswith("🚨"):
        rest = headline[1:].strip()
        headline = f"🚨 {rest.upper()}"
    else:
        headline = headline.upper()

    return headline[:270].strip() or None


def _json_from_response(raw: str) -> dict:
    raw = (raw or "").strip()
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass

    # Be forgiving if the model wraps the JSON in prose or a code fence.
    m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not m:
        return {}
    try:
        parsed = json.loads(m.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def analyze_tweet_for_publish(tweet_id: str, username: str, text: str) -> Optional[dict]:
    """
    Single-call GPT path: decide whether to publish and, if so, draft the
    publishable headline in the same response.
    """
    if client is None:
        return None

    system_prompt = r"""
You are an experienced financial news editor for a real-time crypto + US-macro newswire.

Return ONLY valid JSON with this exact shape:
{
  "publish": 0 or 1,
  "headline": null or "🚨 ALL-CAPS HEADLINE",
  "reason": "very short internal reason"
}

DEFAULT = publish 0. Only a small minority of tweets should ever be publish 1.

Publish only when the tweet clearly describes a concrete, market-relevant event involving at least one of:
- major crypto assets/tickers, exchanges, issuers, regulators, or well-known crypto companies
- US macro data/policy (CPI, PPI, PCE, NFP, GDP, jobless claims, Fed/FOMC/Treasury, rates, tariffs)
- major US megacap equities or clearly market-moving finance events
- major geopolitical escalations that are truly market moving

Always publish 0 for routine commentary, memes, vague opinions, promos, Spaces/AMAs/watch-live posts with no substance, stale/ICYMI resharing, non-US routine data, and price chatter without a concrete percent/move/event.

If publish is 1, headline rules:
- Prefix with exactly one 🚨.
- Uppercase English.
- Keep under 140 characters when possible.
- Do NOT include BREAKING or #BREAKING.
- No hashtags except cashtags like $BTC or $AAPL.
- Do not include @ handles unless the handle is itself the story.
- Never invent facts; use only what the tweet explicitly says.
- Capture the entity, action, and impact.
- Do not include links; if a domain is necessary, write it like BITCOIN(.)COM.

If publish is 0, headline must be null.
""".strip()

    user_prompt = f"""
Tweet text:
{text}

Handle: {username}
""".strip()

    try:
        resp = _chat_completion_with_retry(
            model="gpt-5-mini",
            messages=[
                {"role": "system", "content": system_prompt, "cache": True},
                {"role": "user", "content": user_prompt},
            ],
        )
        if resp is None:
            return None

        raw = (resp.choices[0].message.content or "").strip()
        parsed = _json_from_response(raw)
        publish_raw = parsed.get("publish", 0)
        publish = 1 if str(publish_raw).strip().lower() in {"1", "true", "yes"} else 0
        headline = _normalize_generated_headline(parsed.get("headline") or "") if publish else None

        # If the model marked publish but omitted a headline, let the caller use
        # its deterministic fallback rather than spending another model round-trip.
        return {
            "tweet_id": str(tweet_id),
            "importance_score": publish,
            "label": "high" if publish else "low",
            "headline": headline,
            "reason": str(parsed.get("reason") or "").strip(),
        }
    except Exception as e:
        print(f"[GPT-ERROR] analyze_tweet_for_publish failed for {tweet_id}: {e}")
        return None


def score_tweet_importance(tweet_id: str, username: str, text: str) -> Optional[dict]:
    """
    Call GPT to decide if a tweet should be published or not.

    NEW MINIMAL OUTPUT FORMAT FROM GPT:
      1 -> publish
      0 -> do NOT publish

    Returned to caller as:
      {
        "tweet_id": str,
        "importance_score": 0|1,
        "label": "low"|"high"  # derived locally, only for logging/stats
      }
    """
    if client is None:
        return None

    system_prompt = (r"""
You decide if a tweet should be published by a real-time crypto + US-macro newswire.

FINAL OUTPUT:
Return ONLY:
1  → publish
0  → do NOT publish

DEFAULT = 0.
Only a small minority of tweets should ever be 1.

A tweet must pass BOTH TESTS:

====================================================
TEST 1 — RELEVANT ENTITY REQUIRED
Tweet MUST explicitly mention at least ONE of:

- A major cryptocurrency or ticker (BTC, ETH, SOL, TRX, XRP, etc.)
- A major crypto exchange, issuer, regulator, or well-known crypto company
- A major US macro term (CPI, PPI, PCE, NFP, GDP, JOBLESS CLAIMS, RETAIL SALES, FED, FOMC, TREASURY, etc.)
- A major US megacap equity (AAPL, MSFT, AMZN, NVDA, META, TSLA, GOOGL, etc.)
- A major US government figure (President, Treasury Sec, Fed Chair/Governors)

If NONE of these appear → return 0.

Non-US companies, non-US politicians, and non-US economic data DO NOT COUNT unless the event is massively market-moving (see TEST 2).

====================================================
TEST 2 — SPECIFIC MARKET-MOVING EVENT REQUIRED
Tweet MUST clearly describe a real event such as:

CRYPTO:
- exchange outages, halts, hacks, exploits
- chain outages, upgrades, halts
- stablecoin depegs or ≥$100M mints/burns
- major lawsuits/regulation/enforcement involving crypto
- >4% major crypto price moves
- ETF flows (spot only)
- >$500M on-chain transfers for BTC or USDT
- >$250M on-chain transfers for anything else
- On-chain trades (longs/shorts) >$100M
- Long/short liqudations >$250M
- Famous/popular/well-known/influential investor's trades
- big corporate filings about crypto, treasury buys, or sales
- major governance/DAO changes

US MACRO:
- CPI/PPI/PCE/NFP/UMICH/ISM/GDP coming in as data
- big beats/misses
- Fed rate changes, major Fed comments, policy signals
- Treasury/White House comments on inflation, tariffs, rates
- major consumer or housing reports

MEGACAP / FINANCE:
- earnings beat/miss
- major CEO/CFO changes
- major partnership, major crypto involvement
- bankruptcy, default, major financing crisis
- major stock move >5%

GEOPOLITICAL (ONLY IF TRULY MARKET-MOVING):
- major new war declarations
- major attacks or escalations involving US, EU, China, Japan
- major peace deals
Routine Ukraine/Israel/Middle East updates = ALWAYS 0.

====================================================
ALWAYS 0 (DO NOT PUBLISH):
- Anything routine from non-US economies (China, EU, Korea, Japan, India, UK, etc.)
- Non-US inflation, GDP, PMIs, trade data (unless extremely shocking AND explicitly stated)
- Routine political statements without market impact
- General commentary, opinions, memes, hype, promos, Spaces, AMAs
- Vague statements without data/action
- Old stories, ICYMI, re-shares
- Any tweet under 5 words unless it contains a ticker + a clear event
- Price changes without % (e.g., "BTC up")
- Non-crypto tech news unless it involves megacaps AND is clearly material
- Routine filings, earnings, reports, product announcements, or statements from any non-crypto corporations

====================================================
WHEN IN DOUBT:
If the tweet is not concrete, not US-macro, not major-crypto, or not definitely market-moving → return 0.

Return ONLY:
1 or 0
""")


    user_prompt = f"""
Tweet text:
{text}

Handle: {username}

Return ONLY a single character:
- 1  -> publish
- 0  -> do NOT publish
""".strip()

    try:
        resp = _chat_completion_with_retry(
            model="gpt-5-mini",
            messages=[
                {"role": "system", "content": system_prompt, "cache": True},
                {"role": "user", "content": user_prompt},
            ],
        )

        if resp is None:
            return None

        raw = (resp.choices[0].message.content or "").strip()

        # Extract first 0/1 digit to be safe if the model ever adds junk
        m = re.search(r"[01]", raw)
        if not m:
            flag = 0
        else:
            flag = int(m.group(0))

        score = 1 if flag == 1 else 0
        label = "high" if score == 1 else "low"

        return {
            "tweet_id": str(tweet_id),
            "importance_score": score,  # now strictly 0 or 1
            "label": label,
        }

    except Exception as e:
        print(f"[GPT-ERROR] score_tweet_importance failed for {tweet_id}: {e}")
        return None



def generate_headline_with_gpt(text: str, username: Optional[str] = None) -> Optional[str]:
    """
    GPT-powered headline builder (uses full tweet + optionally the handle).
    Returns: 🚨 [ALL CAPS WIRE HEADLINE] (no '#BREAKING' tag) or None on failure.

    The handle is provided ONLY as context for credibility/relevance.
    The model is explicitly told NOT to insert the handle into the headline
    unless it already appears in the tweet or is clearly part of the story.
    """
    if client is None:
        return None

    # Normalize handle format a bit
    handle_str = None
    if username:
        u = username.strip()
        if u:
            if not u.startswith("@"):
                u = "@" + u
            handle_str = u

    handle_block = ""
    if handle_str:
        handle_block = f"\nHandle (source account): {handle_str}\n"

    prompt = f"""
Write a concise ALL-CAPS Bloomberg/Reuters-style headline summarizing this tweet.
Prefix with: 🚨
Do NOT include '#BREAKING' or 'BREAKING' anywhere in the headline.
No emojis except the leading one. Keep <140 chars.

CONTEXT ABOUT SOURCE:
- You are given the X/Twitter handle that posted the tweet.
- You may use the handle ONLY to understand credibility, role, or context.
- DO NOT include the handle or `@` mention in the headline
  unless it is already clearly part of the story in the tweet text
  (for example, if the tweet itself is about that account).


RULES:
- NEVER invent facts. Use ONLY what is explicitly written.
- If no clear action/outcome/statement/numbers/decision, DO NOT guess.
- If tweet is only a promo (WATCH LIVE, INTERVIEW, SPACES, AMA, LISTEN NOW)
  AND gives no summary:
    → Write a simple 'LIVE INTERVIEW WITH X' style headline using ONLY given names/roles.
    → Do NOT add motives, reasons, speculation, or implied context.
- If tweet DOES summarize content:
    → Focus on the substantive event (action/decision/claim/denial/data).
- If the story has ICYMI or seems to be an older story based on a provided date/time, indicate that in your headline.
- Write headlines in your own words, avoid making exact copies but the facts, persons, times, events, etc. MUST BE THE SAME.
- NEVER write "tweet says" or anything similar to that, attribute the tweet to the actual author if you are quoting the author of the tweet and it's not just a normal headline story.
- NEVER write things like "XXX RESEARCH ALERT:" or anything similar.
- ONLY cite major outlets (Bloomberg, Reuters, CNBC, WSJ, etc.) when it is specifically a press report from one of those outlets.

FORMAT:
- Uppercase English.
- No hashtags except cashtags ($BTC, $AAPL).
- Capture the most newsworthy entity + action + impact.
- Strip fluff, quotes, emojis, and ad language.
- Do NOT include links, if the original tweet has a link and it's necessary for the story (eg. writing about bitcoin.com) write the link as BITCOIN(.)COM, or PUMP(.)FUN, or ALT(.)TOWN

TWEET TEXT:
{text}
{handle_block}
""".strip()

    try:
        resp = _chat_completion_with_retry(
            model="gpt-5-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You are an experienced financial news editor.",
                    "cache": True,
                },
                {"role": "user", "content": prompt},
            ],
        )
        if resp is None:
            return None

        return _normalize_generated_headline(resp.choices[0].message.content or "")
    except Exception as e:
        print(f"[GPT-HEADLINE-ERROR] {e}")
        return None



def gpt_is_duplicate(
    candidate_headline: str,
    tweet_text: str,
    recent_compressed_headlines: list[str],
    story_fp: StoryFingerprint | None = None,
) -> bool:
    """
    Use GPT to decide if candidate_headline describes essentially the same story
    as any of the last N compressed headlines.

    To reduce token usage:
      - We prefilter and rank by local same-story signals.
      - We send only the strongest compressed-headline shortlist.
      - We include source tweet context only for sparse/ambiguous candidates.
      - We ask GPT to return ONLY a single character:
            '1' = duplicate
            '0' = not duplicate
    """
    if client is None:
        return False
    if not recent_compressed_headlines:
        return False

    candidate_fp = story_fp or build_story_fingerprint(f"{candidate_headline}\n{tweet_text}")
    if candidate_fp.is_recurring:
        # Scheduled macro/earnings/data releases often repeat the same entities,
        # verbs, and numbers across periods. Without period-aware stored history,
        # the safest high-recall behavior is to fail open and avoid suppressing
        # a new important release (e.g. April CPI vs May CPI).
        return False

    compressed_candidate = compress_headline_local(candidate_headline)
    cand_tokens = _token_set(compressed_candidate)
    if not cand_tokens:
        return False

    # ---------- Keyword-overlap prefilter (local, cheap) ----------
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
            scored.append((_dedupe_score(cand_tokens, h_tokens, candidate_fp), h))

    if not scored:
        # No prior headlines share meaningful keywords → very unlikely to be a dup.
        return False

    # Keep only the strongest same-story risks to keep prompts short. The GPT call
    # is a final adjudicator, not the broad search layer.
    scored.sort(key=lambda x: x[0], reverse=True)
    if scored[0][0] < _DEDUPE_GPT_MIN_SCORE:
        return False

    filtered_headlines = [h for _, h in scored[:_DEDUPE_TOP_N]]

    numbered = "\n".join(
        f"{idx + 1}. {h}"
        for idx, h in enumerate(filtered_headlines)
    )

    system_prompt = (
        "You are an assistant editor for a real-time crypto news account.\n"
        "Decide if a NEW headline is essentially the SAME core story as any of the RECENT headlines.\n\n"
        "Two headlines are the SAME STORY if they describe the same core event "
        "(same entity + same action + same event), even if wording differs.\n"
        "If they are about different events, even with similar entities, treat them as NOT duplicates.\n\n"
        "DO NOT mark as duplicate comments from an event, such as an FOMC, Networking event, or earnings call unless you've seen essentially the same comment already.\n\n"
        "DO NOT mark recurring scheduled data as duplicate unless the same release period/date is explicit in both items. "
        "Examples: April CPI and May CPI are NOT duplicates, even if numbers are similar.\n\n"
        "DO NOT mark as duplicate large changes in share price (eg. a stock is down 5% then later it is down 10%, these are considered different stories)\n\n"
        "Respond with ONLY a single character:\n"
        "  '1' if the candidate IS a duplicate of any prior headline.\n"
        "  '0' if the candidate is NOT a duplicate.\n"
        "No explanation. No JSON. No extra text."
    )

    source_context = ""
    if _should_include_dedupe_context(compressed_candidate, cand_tokens):
        excerpt = _dedupe_context_excerpt(tweet_text)
        if excerpt:
            source_context = f'\nSource context excerpt, for disambiguation only:\n"{excerpt}"\n'

    user_prompt = f"""
Recent compressed headlines (most recent first):
{numbered}

Candidate compressed headline:
"{compressed_candidate}"
{source_context}
Is the candidate essentially the SAME underlying story as any of the recent headlines?

Reply with ONLY:
1  (duplicate)
0  (not duplicate)
""".strip()

    try:
        resp = _chat_completion_with_retry(
            model="gpt-5-mini",
            messages=[
                {"role": "system", "content": system_prompt, "cache": True},
                {"role": "user", "content": user_prompt},
            ],
        )
        if resp is None:
            return None

        raw = (resp.choices[0].message.content or "").strip()
        if not raw:
            return False

        # Take the first non-whitespace character.
        first = raw[0]
        return first == "1"

    except Exception as e:
        print(f"[GPT-DEDUP-ERROR] {e}")
        return False
