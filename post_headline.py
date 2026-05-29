# post_headline.py
# Visible (off-screen) poster: UI composer with trusted events, then Ctrl+Enter/click.
from __future__ import annotations

import time
import unicodedata
from typing import Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.common.exceptions import TimeoutException


COMPOSE_URL = "https://x.com/compose/post"

EDITOR_SELECTORS = [
    '[role="dialog"] div[contenteditable="true"]',
    'div[data-testid="tweetTextarea_0"] [contenteditable="true"]',
    'div[data-testid="tweetTextarea_0"]',
    'div[contenteditable="true"]',
]

POST_BUTTON_SELECTORS = [
    'div[role="dialog"] div[data-testid="tweetButton"]',
    'div[data-testid="tweetButton"]',
    'div[role="button"][data-testid="tweetButtonInline"]',
]


class ComposeUnavailableError(Exception):
    """
    Raised when the X compose dialog cannot be loaded at all
    (site outage, interstitial, login issues, etc.).
    """
    pass


def _wait(driver: WebDriver, timeout: float = 10.0):
    return WebDriverWait(driver, timeout)


def _find_first(driver: WebDriver, selectors: list[str]) -> Optional[object]:
    """
    Do NOT require .is_displayed(), because the compose editor can be off-screen
    or occluded but still usable.
    """
    for css in selectors:
        try:
            el = driver.find_element(By.CSS_SELECTOR, css)
            if el:
                return el
        except Exception:
            pass
    return None


def _find_ready_compose_editor(driver: WebDriver) -> Optional[object]:
    """Return an already-open, hydrated compose editor if one is available."""
    try:
        editor = driver.execute_script("""
            const dialog = document.querySelector('[role="dialog"]');
            if (!dialog) return null;

            const ed =
                dialog.querySelector('div[data-testid="tweetTextarea_0"] [contenteditable="true"]') ||
                dialog.querySelector('div[data-testid="tweetTextarea_0"]') ||
                dialog.querySelector('[contenteditable="true"]');

            if (!ed) return null;

            const keys = Object.keys(ed);
            const hydrated = keys.some(
                k => k.startsWith('__reactProps') || k.startsWith('__reactFiber')
            );

            return hydrated ? ed : null;
        """)
    except Exception:
        editor = None

    if not editor:
        editor = _find_first(driver, EDITOR_SELECTORS)

    if editor:
        try:
            driver.execute_script("arguments[0].focus();", editor)
        except Exception:
            pass

    return editor


def _sanitize_ascii(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = "".join(ch if ord(ch) <= 0xFFFF else " " for ch in s)
    s = " ".join(s.split())
    return s[:270].strip()  # leave headroom


def _wait_post_button_enabled(driver: WebDriver, timeout: float = 8.0):
    end = time.time() + timeout
    last_btn = None
    while time.time() < end:
        for css in POST_BUTTON_SELECTORS:
            try:
                btn = driver.find_element(By.CSS_SELECTOR, css)
                last_btn = btn
                aria = btn.get_attribute("aria-disabled")
                if aria in (None, "", "false", "False"):
                    return btn
            except Exception:
                pass
        time.sleep(0.1)
    return last_btn


def _open_compose(driver: WebDriver) -> None:
    """
    Navigate to the compose URL and wait for the dialog.

    If the compose dialog never appears (timeout or driver error),
    raise ComposeUnavailableError so callers can back off instead of
    hammering X while it's down.
    """
    try:
        driver.get(COMPOSE_URL)
    except Exception as e:
        # Network / browser / site fatal error
        raise ComposeUnavailableError(f"driver.get({COMPOSE_URL}) failed: {e}") from e

    try:
        _wait(driver, 12).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, '[role="dialog"]'))
        )
    except TimeoutException as e:
        # Page loaded but no compose dialog -> likely interstitial / error page / timeout
        raise ComposeUnavailableError("Compose dialog did not appear (timeout).") from e

    # Try to bring window “to front” in case the browser minimized / backgrounded
    try:
        driver.execute_script("window.focus();")
    except Exception:
        pass

    # Small delay to allow React hydration to begin
    time.sleep(0.3)


def _get_compose_editor_with_retries(
    driver: WebDriver,
    max_attempts: int = 6,
    delay_between: float = 1.0,
):
    """
    Repeatedly try to open the compose dialog and locate the editor.

    - If X/compose is fundamentally unavailable, we raise ComposeUnavailableError
      immediately so the caller can back off at a higher level instead of burning
      through all attempts and spamming logs.
    """
    PER_ATTEMPT_WAIT = 6.0   # seconds to keep polling per attempt
    POLL_INTERVAL = 0.25     # seconds between checks

    for attempt in range(1, max_attempts + 1):
        try:
            _open_compose(driver)
        except ComposeUnavailableError as e:
            # Compose clearly isn't available right now; bubble up so caller
            # can trigger a global backoff instead of trying all 6 attempts.
            print(f"[POST] Compose unavailable on attempt {attempt}/{max_attempts}: {e}")
            raise
        except Exception as e:
            # Unexpected driver error; log and retry a few times within this call.
            print(f"[POST] Error opening compose (attempt {attempt}/{max_attempts}): {e}")
            time.sleep(delay_between)
            continue

        editor = None
        deadline = time.time() + PER_ATTEMPT_WAIT

        while time.time() < deadline:
            editor = _find_ready_compose_editor(driver)
            if editor:
                #print(f"[POST] Found compose editor on attempt {attempt}/{max_attempts}.")   #### OFF
                return editor

            time.sleep(POLL_INTERVAL)

        print(
            f"[POST] Compose editor not found within {PER_ATTEMPT_WAIT}s "
            f"(attempt {attempt}/{max_attempts}); retrying..."
        )
        time.sleep(delay_between)

    return None


def warm_compose_driver(driver: WebDriver) -> bool:
    """
    Best-effort preloader for the next post.

    Opens compose and verifies the hydrated editor while the poster is idle so
    the next headline can skip most navigation/hydration latency.
    """
    if _find_ready_compose_editor(driver):
        return True

    try:
        editor = _get_compose_editor_with_retries(driver, max_attempts=1, delay_between=0.0)
        return editor is not None
    except ComposeUnavailableError:
        raise
    except Exception as e:
        print(f"[POST] Warm compose failed: {e}")
        return False


def _single_post_attempt(driver: WebDriver, txt: str) -> bool:
    """
    ONE full attempt:
      - open compose (with retries)
      - focus editor, clear, type
      - immediately try Ctrl+Enter
      - if that fails, fall back to clicking Post button
      - confirm dialog disappears

    May raise ComposeUnavailableError if compose cannot be opened.
    """
    editor = _find_ready_compose_editor(driver) or _get_compose_editor_with_retries(driver, max_attempts=6, delay_between=1.0)
    if not editor:
        print("[POST] Could not find compose editor after multiple attempts.")
        return False

    # Focus, clear existing text
    try:
        editor.click()
    except Exception:
        pass
    time.sleep(0.05)
    try:
        editor.send_keys(Keys.CONTROL, "a")
        editor.send_keys(Keys.DELETE)
    except Exception:
        pass
    time.sleep(0.05)

    # FAST PATH: send full text in one go
    typed_ok = False
    try:
        editor.send_keys(txt)
        typed_ok = True
    except Exception:
        typed_ok = False

    # FALLBACK: per-character typing if bulk send failed
    if not typed_ok:
        for ch in txt:
            try:
                editor.send_keys(ch)
            except Exception:
                print("[POST] Error while typing into editor (fallback).")
                return False
            time.sleep(0.002)

    # Give React a very short moment to update internal state
    time.sleep(0.25)

    # ---------- 1) Try Ctrl+Enter *immediately* ----------
    try:
        editor.send_keys(Keys.CONTROL, Keys.ENTER)
        _wait(driver, 7).until_not(
            EC.presence_of_element_located((By.CSS_SELECTOR, '[role="dialog"]'))
        )
        return True
    except Exception:
        # If the dialog didn't close, we'll fall back to button click.
        pass

    # ---------- 2) Fallback: find Post button and click ----------
    btn = None
    end = time.time() + 3.0  # only wait up to 3s for an enabled Post button
    while time.time() < end:
        for css in POST_BUTTON_SELECTORS:
            try:
                candidate = driver.find_element(By.CSS_SELECTOR, css)
                aria = candidate.get_attribute("aria-disabled")
                if aria in (None, "", "false", "False"):
                    btn = candidate
                    break
            except Exception:
                continue
        if btn:
            break
        time.sleep(0.1)

    if not btn:
        print("[POST] Could not find enabled Post button.")
        return False

    try:
        btn.click()
    except Exception:
        try:
            driver.execute_script("arguments[0].click();", btn)
        except Exception:
            print("[POST] Could not click Post button.")
            return False

    # Success = dialog disappears
    try:
        _wait(driver, 7).until_not(
            EC.presence_of_element_located((By.CSS_SELECTOR, '[role="dialog"]'))
        )
        return True
    except Exception:
        print("[POST] Clicked Post; dialog still present.")
        return False


def post_headline_with_driver(
    driver: WebDriver,
    text: str,
    max_post_attempts: int = 2,  # was 3; faster overall, still robust
) -> bool:
    """
    Public entry: sanitize text, then try up to max_post_attempts
    full post cycles before giving up.

    May raise ComposeUnavailableError if compose cannot be opened at all.
    """
    txt = _sanitize_ascii(text)
    if not txt:
        print("[POST] Empty/invalid headline after sanitization.")
        return False

    for attempt in range(1, max_post_attempts + 1):
        #print(f"[POST] Overall post attempt {attempt}/{max_post_attempts}...")   #### OFF
        try:
            ok = _single_post_attempt(driver, txt)
        except ComposeUnavailableError:
            # Bubble this up to the caller (post_sender_loop / retry handler)
            # so they can back off globally rather than wasting attempts.
            raise

        if ok:
            return True

        print(f"[POST] Overall post attempt {attempt}/{max_post_attempts} failed; will retry.")
        time.sleep(1.0)

    print("[POST] All overall post attempts failed for this headline.")
    return False
