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
    driver.get(COMPOSE_URL)
    _wait(driver, 12).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, '[role="dialog"]'))
    )
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

    - For each attempt, after opening compose, we poll for a *hydrated* editor
      (React-bound) for a few seconds.
    """
    PER_ATTEMPT_WAIT = 6.0   # seconds to keep polling per attempt
    POLL_INTERVAL = 0.25     # seconds between checks

    for attempt in range(1, max_attempts + 1):
        try:
            _open_compose(driver)
        except Exception as e:
            print(f"[POST] Error opening compose (attempt {attempt}/{max_attempts}): {e}")
            time.sleep(delay_between)
            continue

        editor = None
        deadline = time.time() + PER_ATTEMPT_WAIT

        while time.time() < deadline:
            # Primary: JS-assisted editor detection that ONLY returns a hydrated React editor
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

            # Secondary: CSS-based fallback (in case React-prop detection fails for some reason)
            if not editor:
                editor = _find_first(driver, EDITOR_SELECTORS)

            if editor:
                try:
                    driver.execute_script("arguments[0].focus();", editor)
                except Exception:
                    pass
                print(f"[POST] Found compose editor on attempt {attempt}/{max_attempts}.")
                return editor

            time.sleep(POLL_INTERVAL)

        print(
            f"[POST] Compose editor not found within {PER_ATTEMPT_WAIT}s "
            f"(attempt {attempt}/{max_attempts}); retrying..."
        )
        time.sleep(delay_between)

    return None


def _single_post_attempt(driver: WebDriver, txt: str) -> bool:
    """
    ONE full attempt:
      - open compose (with retries)
      - focus editor, clear, type
      - wait for enabled Post
      - Ctrl+Enter
      - fall back to button click
      - confirm dialog disappears
    """
    # Keep trying to get a live editor (with refreshes)
    editor = _get_compose_editor_with_retries(driver, max_attempts=6, delay_between=1.0)
    if not editor:
        print("[POST] Could not find compose editor after multiple attempts.")
        return False

    # Focus, clear, type (trusted events because window is visible)
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

    # Slow typing helps React reliably capture input on some setups
    for ch in txt:
        try:
            editor.send_keys(ch)
        except Exception:
            # If something goes wrong mid-typing, bail on this attempt
            print("[POST] Error while typing into editor.")
            return False
        time.sleep(0.01)

    time.sleep(0.2)

    # Wait for Post to enable (nudge if necessary)
    btn = _wait_post_button_enabled(driver, timeout=10.0)
    if btn and (btn.get_attribute("aria-disabled") in ("true", "True")):
        try:
            editor.send_keys(" ")
            editor.send_keys(Keys.BACK_SPACE)
        except Exception:
            pass
        btn = _wait_post_button_enabled(driver, timeout=3.0)

    # 1) Ctrl+Enter (most reliable)
    try:
        editor.send_keys(Keys.CONTROL, Keys.ENTER)
        _wait(driver, 10).until_not(
            EC.presence_of_element_located((By.CSS_SELECTOR, '[role="dialog"]'))
        )
        print("[POST] Posted successfully (Ctrl+Enter).")
        return True
    except Exception:
        # fall through to button click path
        pass

    # 2) Click button (then JS click)
    if btn:
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
        _wait(driver, 10).until_not(
            EC.presence_of_element_located((By.CSS_SELECTOR, '[role="dialog"]'))
        )
        print("[POST] Posted successfully (dialog closed).")
        return True
    except Exception:
        print("[POST] Clicked Post; dialog still present.")
        return False


def post_headline_with_driver(
    driver: WebDriver,
    text: str,
    max_post_attempts: int = 3,
) -> bool:
    """
    Public entry: sanitize text, then try up to max_post_attempts
    full post cycles before giving up.

    This ensures we don't bail after a single
    'Clicked Post; dialog still present.' glitch.
    """
    txt = _sanitize_ascii(text)
    if not txt:
        print("[POST] Empty/invalid headline after sanitization.")
        return False

    for attempt in range(1, max_post_attempts + 1):
        print(f"[POST] Overall post attempt {attempt}/{max_post_attempts}...")
        ok = _single_post_attempt(driver, txt)
        if ok:
            return True
        print(f"[POST] Overall post attempt {attempt}/{max_post_attempts} failed; will retry.")
        time.sleep(1.0)

    print("[POST] All overall post attempts failed for this headline.")
    return False
