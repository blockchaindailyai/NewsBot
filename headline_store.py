import os
import re
import threading

from headline_compress import compress_headline_local

HEADLINES_FILE = "breaking_headlines.txt"           # full GPT headlines
COMPRESSED_HEADLINES_FILE = "compressed_headlines.txt"  # compressed for dedupe

_SEEN_HEADLINE_KEYS = set()       # normalized full headlines
_SEEN_COMPRESSED_KEYS = set()     # normalized compressed headlines
_COMPRESSED_HEADLINES: list[str] = []  # cached compressed headlines (avoids repeated disk scans)

# NEW: locks to make dedupe thread-safe
_FULL_LOCK = threading.Lock()
_COMPRESSED_LOCK = threading.Lock()


def normalize_headline_for_key(headline: str) -> str:
    """
    Normalize for dedupe keys:
      - strip leading alert prefix ('🚨', optional '#BREAKING', optional colon/hyphen)
      - uppercase
      - remove non-alphanumerics (keep spaces)
      - collapse spaces
    """
    h = str(headline or "").upper()

    # Strip leading emoji + optional BREAKING, supporting both old and new formats:
    #   "🚨#BREAKING: FED CUTS RATES"
    #   "🚨 BREAKING FED CUTS RATES"
    #   "🚨 FED CUTS RATES"
    h = re.sub(r"^🚨\s*", "", h)
    h = re.sub(r"^#?BREAKING[:\-\s]*", "", h)

    h = re.sub(r"[^A-Z0-9]+", " ", h)
    h = re.sub(r"\s+", " ", h).strip()
    return h



def load_headline_state() -> None:
    """
    Populate:
      - _SEEN_HEADLINE_KEYS from HEADLINES_FILE
      - _SEEN_COMPRESSED_KEYS from COMPRESSED_HEADLINES_FILE
    """
    # Full headlines
    if os.path.exists(HEADLINES_FILE):
        try:
            with open(HEADLINES_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    key = normalize_headline_for_key(line)
                    if key:
                        _SEEN_HEADLINE_KEYS.add(key)
        except Exception as e:
            print(f"[HEADLINE-WARN] Failed to load existing headlines: {e}")

    # Compressed headlines
    if os.path.exists(COMPRESSED_HEADLINES_FILE):
        try:
            with open(COMPRESSED_HEADLINES_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    key = normalize_headline_for_key(line)
                    if key and key not in _SEEN_COMPRESSED_KEYS:
                        _SEEN_COMPRESSED_KEYS.add(key)
                        _COMPRESSED_HEADLINES.append(line)
        except Exception as e:
            print(f"[HEADLINE-WARN] Failed to load compressed headlines: {e}")


def seen_full_preapi(headline: str) -> bool:
    """
    Check if a normalized version of this (pre-GPT) headline is already in full-headline keys.
    Used for fast exact dedupe before calling GPT.
    """
    key = normalize_headline_for_key(headline)
    # Read-only check is fine without lock because set operations are atomic,
    # and we only care about "best effort" here.
    return bool(key and key in _SEEN_HEADLINE_KEYS)


def _save_compressed_headline_if_new(headline: str) -> None:
    """
    Compress headline and save to COMPRESSED_HEADLINES_FILE if its compressed
    key is new.
    """
    compressed = compress_headline_local(headline)
    if not compressed:
        return
    key = normalize_headline_for_key(compressed)
    if not key:
        return

    with _COMPRESSED_LOCK:
        if key in _SEEN_COMPRESSED_KEYS:
            return
        _SEEN_COMPRESSED_KEYS.add(key)
        _COMPRESSED_HEADLINES.append(compressed.strip())
        try:
            with open(COMPRESSED_HEADLINES_FILE, "a", encoding="utf-8") as f:
                f.write(compressed.strip() + "\n")
        except Exception as e:
            print(f"[HEADLINE-WARN] Failed to write compressed headline: {e}")


def save_full_headline(headline: str, *, allow_duplicate_key: bool = False) -> bool:
    """
    Save full headline to file if it's not in _SEEN_HEADLINE_KEYS (exact/normalized),
    and also persist a compressed version for deduping.

    allow_duplicate_key is used for recurring scheduled data where the final
    headline can be text-identical across different periods (e.g. April CPI vs
    May CPI). In that case, the caller has already applied higher-level
    same-run safeguards and chooses recall over suppressing a potentially new
    important release.

    Returns True if written, False if duplicate.
    """
    key = normalize_headline_for_key(headline)
    if not key:
        return False

    with _FULL_LOCK:
        if key in _SEEN_HEADLINE_KEYS and not allow_duplicate_key:
            # Already seen in this process (or loaded from file).
            return False

        _SEEN_HEADLINE_KEYS.add(key)
        try:
            with open(HEADLINES_FILE, "a", encoding="utf-8") as f:
                f.write(headline.strip() + "\n")
        except Exception as e:
            print(f"[HEADLINE-WARN] Failed to write headline: {e}")

    # Compressed store uses its own lock
    _save_compressed_headline_if_new(headline)

    return True


def get_last_full_headlines(n: int = 100) -> list[str]:
    lines: list[str] = []
    try:
        if os.path.exists(HEADLINES_FILE):
            with open(HEADLINES_FILE, "r", encoding="utf-8") as f:
                all_lines = [ln.strip() for ln in f if ln.strip()]
            lines = all_lines[-n:]
    except Exception as e:
        print(f"[HEADLINE-WARN] Failed to read last headlines: {e}")
    return lines


def get_last_compressed_headlines(n: int = 100) -> list[str]:
    lines: list[str] = []
    try:
        if os.path.exists(COMPRESSED_HEADLINES_FILE):
            with open(COMPRESSED_HEADLINES_FILE, "r", encoding="utf-8") as f:
                all_lines = [ln.strip() for ln in f if ln.strip()]
            lines = all_lines[-n:]
    except Exception as e:
        print(f"[HEADLINE-WARN] Failed to read last compressed headlines: {e}")
    return lines

def get_all_compressed_headlines() -> list[str]:
    """
    Return cached compressed headlines loaded at startup and updated on writes.

    This avoids repeatedly scanning compressed_headlines.txt for every candidate,
    which is important on low-CPU/low-I/O VPS deployments.
    """
    with _COMPRESSED_LOCK:
        return list(_COMPRESSED_HEADLINES)
