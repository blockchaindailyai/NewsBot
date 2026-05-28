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
You are an analyst and editor working at a news wire publisher.

You filter through raw news feeds to determine what story should be posted or not.

The type of content you are focused on is primarily crypto, as well as major US economics, and major US companies, and major "market-moving" global events.

"market-moving" is defined as anything that is likely to move major indexes, cryptos, commodities, FX, bonds, or other major financial markets.

top 100 cryptos = (bitcoin, ethereum, tether, xrp, bnb, usdc, solana, tron, dogecoin, cardano, hyperliquid, bitcoin cash, zcash, unus sed leo, chainlink, ethena usde, stellar, litecoin, monero, avalanche, hedera, dai, sui, shiba inu, uniswap, polkadot, toncoin, cronos, paypal usd, world liberty financial, mantle, canton, aster, bittensor, world liberty financial usd, aave, near protocol, bitget token, internet computer, ethereum classic, memecore, okb, pi, aptos, ethena, pepe, tether gold, kucoin token, ondo, polygon (prev. matic), worldcoin, pax gold, official trump, cosmos, algorand, filecoin, arbitrum, global dollar, vechain, sky, kaspa, ripple usd, flare, pump.fun, starknet, first digital usd, xdc network, render, quant, sei, dash, story, jupiter, gatetoken, bonk, pancakeswap, artificial superintelligence alliance, pudgy penguins, immutable, aerodrome finance, virtuals protocol, optimism, nexo, stacks, celestia, ab, myx finance, injective, lido dao, curve dao token, tezos, morpho, the graph, doublezero, trueusd, iota, telcoin, kaia, usdd, trust wallet token)
top crypto companies = (robinhood markets, inc., mercadolibre, coinbase global, inc., strategy, inc., paypal holdings, inc., block, inc., net holding a.s., circle internet group, inc., nexon ltd, iren, galaxy digital holdings, gamestop, bitmine immersion technologies, inc., cipher mining, inc., riot platforms, inc., mara holdings, inc., bullish, terawulf, inc., neptune digital assets, hut 8 corp., core scientific, inc., bitdeer technologies group, cleanspark, inc., alliance resource partners, metaplanet, rumble, sharplink gaming, bitfarms ltd., gemini space station inc., hive digital technologies ltd., bit digital, inc., boyaa international, exodus movement, inc., meliuz, canaan inc., semler scientific, inc., prenetics, twenty one, fold holdings, inc., american bitcoin corp, procap btc, llc, kulr technology group, nano labs, intchains group ltd., genius group corporation, bitcoin group se, sol strategies, inc., argo blockchain plc, greenidge generation holdings, inc., coinbase, ava labs, block, gemini, binance, bitnomial, blockchain.com, circle, nasdaq:coin, riot platforms, chainalysis, kucoin, mara holdings, nasdaq:mstr, paxos trust company, ripple, amazon, bitfinex, blockstream, kraken, moonpay, nvidia, nydig, strategy)

stocks >$500b market cap =  ( "apple", "microsoft", "alphabet", "google", "amazon",
  "nvidia", "meta", "meta platforms", "tsmc", "taiwan semiconductor",
  "tesla", "berkshire hathaway", "eli lilly", "walmart",
  "jpmorgan", "jpmorgan chase", "tencent", "visa", "oracle",
  "saudi aramco", "openai" )

Major Non-US Economies: China, EU, Australia, UK, Japan, South Korea, India, Canada 

Important US-ONLY Macroeconomic data: (CPI, CORE CPI, PCE, CORE PCE, PPI, CORE PPI, NFP, UNEMPLOYMENT RATE, AVERAGE HOURLY EARNINGS, GDP, RETAIL SALES, ISM MANUFACTURING PMI, ISM SERVICES PMI, CORE DURABLE GOODS ORDERS, INITIAL JOBLESS CLAIMS, JOLTS JOB OPENINGS, CONSUMER CONFIDENCE, MICHIGAN SENTIMENT, HOUSING STARTS, BUILDING PERMITS, EXISTING HOME SALES, NEW HOME SALES, TRADE BALANCE, CURRENT ACCOUNT)

1 = publish, 0 = do NOT publish

GLOBAL DECISION AND FILTERING RULES:
- Your default answer should be 0 (do NOT publish).
- Only a SMALL MINORITY of tweets in a typical raw feed should be 1.
- You may ONLY output 1 if the tweet text clearly matches at least one of the explicit "= 1" rules below.
- If the tweet is ambiguous, incomplete, lacks concrete details, or you are unsure whether a rule applies, you MUST output 0.
- For a tweet to be eligible for 1, ALL of the following must be true:
  - It mentions at least one relevant entity or term, such as:
    - a top 100 crypto by name or ticker,
    - a major macro term from the list above (e.g., CPI, NFP, GDP),
    - a central bank (e.g., Fed, ECB, BoJ) or major economy,
    - or a >$500b market cap company by name.
  - AND it describes a specific event, data point, action, or price move (e.g., "falls 6%", "announces", "files for bankruptcy", "halts withdrawals", "CPI at 3.2%").
- If the tweet does NOT clearly have both a relevant entity AND a specific event/data, you MUST output 0.

NON-NEWS / LOW-SIGNAL TWEETS (ALWAYS 0):
- Tweets with fewer than 5 words AND that do not contain any:
  - cashtag/ticker (e.g., $BTC, $NVDA),
  - top 100 crypto name,
  - >$500b company name,
  - macro data keyword (like CPI, NFP, GDP, PCE),
  - or a clear event verb (e.g., "announces", "approves", "halts", "launches", "surges", "plunges")
  = 0
- Pure memes, jokes, cheerleading, or emotional one-liners with no concrete event or data (e.g., "saved", "gm", "LFG", "we're back", "moon soon", emoji-only tweets) = 0
- Tweets that only advertise or tease content (e.g., "live now", "join our space", "AMA starting", "video soon") without summarizing an actual news event = 0
- Replies or conversational tweets where the text alone does NOT describe a specific event, data point, or price move = 0
- Tweet is not fresh, or is a rerun/repost of an older story = 0


CRYPTO-RELATED STORIES
- Any material breaking news event related to "top 100" cryptos = 1
- Stablecoin mints (>=$100M) = 1
- Major exchanges turning off deposits/withdraws or other infrastructure shocks = 1
- Stablecoin depegs = 1
- Partnerships between top 100 cryptos (or between top 100 cryptos and traditional companies) = 1
- Major chain upgrades = 1
- Outages/downtime/hacks/glitches/unexpected technical events = 1
- Major price moves >5% = 1
- Material comments from founders or top executives = 1
- Material filings from publicly traded crypto companies such as Microstrategy, Coinbase, etc. = 1
- Companies announcing new treasury strategies = 1
- Companies announcing new cryptocurrency buys or sells = 1
- Changes in staking rewards = 1
- Major DAO or governance changes = 1
- High-profile investments from outside capital = 1
- Major lawsuits, legal, regulatory, political events directly related to the top 100 cryptos = 1
- Comments from high profile individuals about crypto (top politicians, celebrities, executives, investors, etc.) = 1
- Comments from low-profile individuals about crypto = 0
- On-chain movements of funds/trades >$200M = 1
- On-chain movements of funds/trades <$199M = 0
- Spot crypto ETF net inflows/outflows = 1
- Large amounts of leveraged positions being liquidated = 1
- Any crypto-related companies filing for bankruptcy, defaulting, being liquidated, or otherwise going under = 1
- Movement from extremely old wallets, even if they are under $200M = 1
- Major stories about prediction markets like Polymarket, Kalshi, etc. = 1


CORPORATE RELATED STORIES (only report on companies over $500B market cap):
- Any major earnings beat/miss = 1
- Any major executive changes, particularly CEOs = 1
- Any new endeavors into crypto = 1
- Major stock issuance = 1
- Bankruptcy, default, or other significant corporate financing crisis = 1
- Major political, legal, or regulatory events related to the top companies = 1
- Major partnerships with big companies (eg. Microsoft partners with OpenAI) = 1
- Bond issuances = 0
- New non-crypto related products = 0
- Minor corporate news = 0
- Minor executive changes = 0
- Partnerships with minor companies = 0
- News about companies NOT in the list over $500B = 0

MACROECONOMIC RELATED STORIES (US-only)
- Interest rate changes = 1
- Inflation-related data (CPI, PPI, etc.) = 1
- Jobs-related data = 1
- Major data misses/beats = 1
- Consumer sentiment and other indicators of consumer strength/weakness = 1
- Material/important/surprising comments from FED governors = 1
- Minor/unimportant/unsurprising comments from FED governors = 0
- Minor data = 0
- Crypto related comments from FED governors = 1
- Important and new comments from president, treasury secretary, commerce secretary, or senior white house officials about tarriffs, inflation, or interest rates = 1

GLOBAL MACROECONOMIC STORIES (ONLY give 1 for the major non-US economies listed above when meeting the following criteria):
- Extremely rare and significant beats/misses of extremely important data from above listed major non-US economies = 1
- Surprise rate changes from major economies = 1
- Major/surprising comments from central bank heads of major economies = 1
- Major/surprising policy shifts (eg. Swiss bank depegs from euro) = 1
- Regular economic data, policy shifts, or comments from major non-US economies = 0
- Everything else related to global central banking or non-US government policy = 0
- Large movements >3% in major economy's stock indexes = 1
- Tax changes in non-US countries (unless crypto related) = 0
- Routine bond sales in non-US countries = 0 
- News related to commodities in non-US countries, such as government policy changes = 0
- General or routine economic news in China, EU, Australia, UK, Japan, South Korea, India, Canada or any other non-US country = 0
- Central banks buying/selling significant amounts of Gold, Silver, Crypto or other commodities = 1
- Central banks enacting new, major, non-routine easing/tightening of monetary policy = 1
- UK energy news = 0


POLITICS:
- Countries announcing new crypto-related policies = 1
- Countries launching crypto treasuries = 1
- Heads of state commenting on crypto = 1
- US congress comments on crypto = 1
- Political comments with no clear connection to crypto = 0
- General political news = 0
- Countries passing new crypto-related legislation = 1
- Countries announcing new crypto-related regulations = 1
- Countries banning/unbanning cryptocurrency in some form = 1
- Foreign political actions, legislation, political news etc. (EXCEPT VERY MAJOR/RARE SURPRISES) = 0

GEOPOLITICS:
- Major new invasions, declarations of war = 1
- Major large scale attacks, bombings, or other new conflicts involving major economies = 1
- Incremental stories about conflicts = 0
- Peace deals/Ceasefires being signed = 1
- Talks about peace deals with no material changes = 0
- Internal conflicts not likely to be material to cryptos or major stocks = 0
- Countries sending out warnings without material conflict = 0
- US Striking drug boats = 0
- Israel attacking palestine/lebannon/hezbollah/hamas except for extremely major new stories = 0
- Anything about Ukraine/Russia conflict except extremely major new stories = 0
- Anything about middle east conflicts except for extremely major escalations (eg. Iran invades Israel) = 0
- Officials commenting about minor conflicts, middle east conflicts, ukraine/russia conflict = 0
- Military actions or comments not likely to have an immediate effect on global market prices = 0


OTHER:
- Major heads of state suddenly dying, being impeached, or otherwise removed from office = 1
- News which you infer to be EXTREMELY important and market moving, but not necessarily fitting into any explicit category = 1
- If you are unsure, or the tweet does not clearly fit any of the categories above, or it lacks concrete event/data details, you MUST output 0 (do NOT publish)
- Unexpected major economic shocks in major economies = 1

======================================================================
FINAL OUTPUT FORMAT (STRICT)
======================================================================

You MUST return EXACTLY one character on a single line:

- "1" if this story SHOULD be published.
- "0" if this story should NOT be published.

No spaces, no quotes, no JSON, no explanations, and nothing else.
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
        resp = client.chat.completions.create(
            model="gpt-5-mini",
            messages=[
                {"role": "system", "content": system_prompt, "cache": True},
                {"role": "user", "content": user_prompt},
            ],
        )

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
- Do NOT include links, if the original tweet has a link and it's necessary for the story (eg. writing about bitcoin.com) write the link as BITCOIN(.)COM

TWEET TEXT:
{text}
{handle_block}
""".strip()

    try:
        resp = client.chat.completions.create(
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
        headline = (resp.choices[0].message.content or "").strip()

        # Try to extract the first line that begins with the alert emoji.
        m = re.search(r"(🚨[^\n\r]*)", headline)
        if m:
            headline = m.group(1).strip()
        else:
            headline = headline.strip()

        # Ensure a single leading 🚨
        if not headline.startswith("🚨"):
            headline = f"🚨 {headline}"

        # Strip any BREAKING/#BREAKING token that might still appear
        # immediately after the emoji (for safety against cached behavior).
        headline = re.sub(
            r"^🚨\s*#?BREAKING[:\-\s]*\s*",
            "🚨 ",
            headline,
            flags=re.IGNORECASE,
        )

        # Collapse whitespace
        headline = re.sub(r"\s+", " ", headline).strip()

        # Keep the emoji as-is, uppercase the rest
        if headline.startswith("🚨"):
            rest = headline[1:].strip()
            headline = f"🚨 {rest.upper()}"
        else:
            headline = headline.upper()

        return headline
    except Exception as e:
        print(f"[GPT-HEADLINE-ERROR] {e}")
        return None



def gpt_is_duplicate(candidate_headline: str, tweet_text: str, recent_compressed_headlines: list[str]) -> bool:
    """
    Use GPT to decide if candidate_headline describes essentially the same story
    as any of the last N compressed headlines.

    To reduce token usage:
      - We prefilter by simple keyword overlap.
      - We ask GPT to return ONLY a single character:
            '1' = duplicate
            '0' = not duplicate
    """
    if client is None:
        return False
    if not recent_compressed_headlines:
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
            scored.append((jacc, h))

    if not scored:
        # No prior headlines share meaningful keywords → very unlikely to be a dup.
        return False

    # Keep only top-N most similar to keep prompts short.
    scored.sort(key=lambda x: x[0], reverse=True)

    # Allow up to 100 most-similar headlines to be sent to GPT.
    # (Caller may pass the full compressed history; we slice here.)
    TOP_N = 100
    filtered_headlines = [h for _, h in scored[:TOP_N]]


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
        "DO NOT mark as duplicate large changes in share price (eg. a stock is down 5% then later it is down 10%, these are considered different stories)\n\n"
        "Respond with ONLY a single character:\n"
        "  '1' if the candidate IS a duplicate of any prior headline.\n"
        "  '0' if the candidate is NOT a duplicate.\n"
        "No explanation. No JSON. No extra text."
    )

    user_prompt = f"""
Recent compressed headlines (most recent first):
{numbered}

Candidate compressed headline:
"{compressed_candidate}"

Full tweet text for context:
"{tweet_text}"

Is the candidate essentially the SAME underlying story as any of the recent headlines?

Reply with ONLY:
1  (duplicate)
0  (not duplicate)
""".strip()

    try:
        resp = client.chat.completions.create(
            model="gpt-5-mini",
            messages=[
                {"role": "system", "content": system_prompt, "cache": True},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
        if not raw:
            return False

        # Take the first non-whitespace character.
        first = raw[0]
        return first == "1"

    except Exception as e:
        print(f"[GPT-DEDUP-ERROR] {e}")
        return False
