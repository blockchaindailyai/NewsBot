import json
import os


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_csv_set(name: str, default: set[str]) -> set[str]:
    raw = os.getenv(name)
    if raw is None:
        return set(default)
    values = {item.strip() for item in raw.split(",") if item.strip()}
    return values or set(default)


def _get_json_map(name: str) -> dict:
    raw = os.getenv(name)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


TWITTER_USERNAME = os.getenv("TWITTER_USERNAME", "BCDNewsBot")
COOKIE_PATH = os.getenv("COOKIE_PATH", "twitter_cookies.pkl")

DEBUG_QUEUE_LOGS = _get_bool("DEBUG_QUEUE_LOGS", False)
RETRY_CHECK_INTERVAL = _get_float("RETRY_CHECK_INTERVAL", 60.0)
SCRAPE_BACKOFF_SHORT_SECONDS = _get_int("SCRAPE_BACKOFF_SHORT_SECONDS", 5)
SCRAPE_BACKOFF_LONG_SECONDS = _get_int("SCRAPE_BACKOFF_LONG_SECONDS", 60)
SCRAPE_FAILURE_THRESHOLD = _get_int("SCRAPE_FAILURE_THRESHOLD", 5)
LOOP_SLEEP_SECONDS = _get_int("LOOP_SLEEP_SECONDS", 10)
ACTIVE_LOOP_SLEEP_SECONDS = _get_int("ACTIVE_LOOP_SLEEP_SECONDS", 2)
MAX_ANALYSIS_WORKERS = _get_int("MAX_ANALYSIS_WORKERS", 8)
ANALYSIS_QUEUE_MAXSIZE = _get_int("ANALYSIS_QUEUE_MAXSIZE", 200)

SCRAPER_AD_LABELS = _get_csv_set(
    "SCRAPER_AD_LABELS",
    {
        "Ad",
        "Promoted",
        "Sponsored",
        "광고",
        "프로모션",
    },
)


SCRAPER_ENABLE_AD_BLOCK_EXTENSION = _get_bool("SCRAPER_ENABLE_AD_BLOCK_EXTENSION", True)
SCRAPER_AD_BLOCK_EXTENSION_PATHS = _get_csv_set("SCRAPER_AD_BLOCK_EXTENSION_PATHS", set())
SCRAPER_AD_BLOCK_EXTENSION_IDS = _get_csv_set(
    "SCRAPER_AD_BLOCK_EXTENSION_IDS",
    {
        # uBlock Origin, uBlock Origin Lite, AdBlock, Adblock Plus
        "cjpalhdlnbpafiamejdnhcphjbkeiagm",
        "ddkjiahejlhfcafbddmgiahcphecmpfh",
        "gighmmpiobklfepjocnamgkkbiglidom",
        "cfhdojbkjhnklbpkdaibdccddilifddb",
    },
)

SCRAPER_BLOCK_AD_REQUESTS = _get_bool("SCRAPER_BLOCK_AD_REQUESTS", True)
SCRAPER_BLOCKED_URL_PATTERNS = _get_csv_set(
    "SCRAPER_BLOCKED_URL_PATTERNS",
    {
        "*://*.adnxs.com/*",
        "*://*.ads-twitter.com/*",
        "*://ads-twitter.com/*",
        "*://*.adsrvr.org/*",
        "*://*.amazon-adsystem.com/*",
        "*://*.doubleclick.net/*",
        "*://*.googleadservices.com/*",
        "*://*.googlesyndication.com/*",
        "*://*.moatads.com/*",
        "*://*.outbrain.com/*",
        "*://*.scorecardresearch.com/*",
        "*://*.taboola.com/*",
        "*://*.twitter.com/i/ads/*",
        "*://twitter.com/i/ads/*",
        "*://*.x.com/i/ads/*",
        "*://x.com/i/ads/*",
    },
)
SCRAPER_AD_TEXT_PATTERNS = _get_csv_set(
    "SCRAPER_AD_TEXT_PATTERNS",
    {
        "sponsored promotion",
        "sponsored post",
        "sign up for our free",
        "sign up for a free trial",
        "try for free",
        "book a demo",
        "claim free",
        "play free & win",
        "start trading",
        "open an account",
        "limited time only",
        "register for free",
        "free ai newsletter",
        "free credits",
        "free investing webcast",
        "free stake cash",
        "free version",
        "access a lite version",
        "register for the free",
        "limited time when you register",
    },
)
SCRAPER_AD_CTA_PATTERNS = _get_csv_set(
    "SCRAPER_AD_CTA_PATTERNS",
    {
        "book a demo",
        "download our",
        "download the guide",
        "download the report",
        "get more info",
        "grab your tickets",
        "join now",
        "learn more",
        "open an account",
        "register",
        "sign up",
        "start trading",
        "subscribe",
        "try for free",
    },
)
SCRAPER_AD_PROMO_TERMS = _get_csv_set(
    "SCRAPER_AD_PROMO_TERMS",
    {
        "casino",
        "credits",
        "demo",
        "free trial",
        "free webinar",
        "newsletter",
        "promotion",
        "sponsored",
        "stake cash",
        "tickets",
        "webcast",
        "webinar",
    },
)

STORY_REGISTRY_PATH = os.getenv("STORY_REGISTRY_PATH", "story_registry.jsonl")
DEDUPE_AUDIT_PATH = os.getenv("DEDUPE_AUDIT_PATH", "dedupe_audit.jsonl")
NON_RECURRING_DUP_WINDOW_HOURS = _get_int("NON_RECURRING_DUP_WINDOW_HOURS", 72)
ACCOUNT_TRUST_SCORES = _get_json_map("ACCOUNT_TRUST_SCORES_JSON")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
OPENAI_TIMEOUT_SECONDS = _get_float("OPENAI_TIMEOUT_SECONDS", 12.0)
OPENAI_RETRY_ATTEMPTS = _get_int("OPENAI_RETRY_ATTEMPTS", 2)
OPENAI_RETRY_BASE_DELAY = _get_float("OPENAI_RETRY_BASE_DELAY", 1.0)
OPENAI_FAILURE_COOLDOWN_SECONDS = _get_int("OPENAI_FAILURE_COOLDOWN_SECONDS", 30)

SCRAPER_HEADLESS = _get_bool("SCRAPER_HEADLESS", False)
POSTER_HEADLESS = _get_bool("POSTER_HEADLESS", False)
SCRAPER_WINDOW_SIZE = os.getenv("SCRAPER_WINDOW_SIZE", "1400,950")
SCRAPER_WINDOW_POS = os.getenv("SCRAPER_WINDOW_POS", "0,0")
POSTER_WINDOW_SIZE = os.getenv("POSTER_WINDOW_SIZE", "1280,900")
POSTER_WINDOW_POS = os.getenv("POSTER_WINDOW_POS", "0,0")
FORCE_CHROME_MAJOR = os.getenv("FORCE_CHROME_MAJOR", "none")
if FORCE_CHROME_MAJOR and FORCE_CHROME_MAJOR.lower() == "none":
    FORCE_CHROME_MAJOR = None
