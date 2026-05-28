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


TWITTER_USERNAME = os.getenv("TWITTER_USERNAME", "BCDNewsBot")
COOKIE_PATH = os.getenv("COOKIE_PATH", "twitter_cookies.pkl")

DEBUG_QUEUE_LOGS = _get_bool("DEBUG_QUEUE_LOGS", False)
RETRY_CHECK_INTERVAL = _get_float("RETRY_CHECK_INTERVAL", 60.0)
SCRAPE_BACKOFF_SHORT_SECONDS = _get_int("SCRAPE_BACKOFF_SHORT_SECONDS", 5)
SCRAPE_BACKOFF_LONG_SECONDS = _get_int("SCRAPE_BACKOFF_LONG_SECONDS", 60)
SCRAPE_FAILURE_THRESHOLD = _get_int("SCRAPE_FAILURE_THRESHOLD", 5)
LOOP_SLEEP_SECONDS = _get_int("LOOP_SLEEP_SECONDS", 20)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
OPENAI_TIMEOUT_SECONDS = _get_float("OPENAI_TIMEOUT_SECONDS", 25.0)
OPENAI_RETRY_ATTEMPTS = _get_int("OPENAI_RETRY_ATTEMPTS", 3)
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
