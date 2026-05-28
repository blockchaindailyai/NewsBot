# auth.py
# Scraper = headless(old); Poster = visible off-screen (not minimized), so React sees real input.

import os
import time
import undetected_chromedriver as uc

from config import (
    SCRAPER_HEADLESS,
    POSTER_HEADLESS,
    SCRAPER_WINDOW_SIZE,
    SCRAPER_WINDOW_POS,
    POSTER_WINDOW_SIZE,
    POSTER_WINDOW_POS,
    FORCE_CHROME_MAJOR,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Runtime settings are loaded from config.py / environment variables.

CHROME_BETA_BIN   = r"C:\Program Files\Google\Chrome Beta\Application\chrome.exe"
CHROME_STABLE_BIN = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROFILE_DIR_SCRAPER = os.path.join(os.getcwd(), "x_profile")
PROFILE_DIR_POSTER  = os.path.join(os.getcwd(), "x_poster_profile")


def _pick_chrome_binary() -> str:
    """
    Prefer Chrome Beta if installed; otherwise fall back to Stable.
    """
    if os.path.exists(CHROME_BETA_BIN):
        return CHROME_BETA_BIN
    return CHROME_STABLE_BIN


def _make_options(user_data_dir: str, headless: bool, size: str, pos: str) -> uc.ChromeOptions:
    opts = uc.ChromeOptions()

    # ✅ Force which Chrome EXE gets launched (Beta preferred)
    opts.binary_location = _pick_chrome_binary()

    opts.add_argument(f"--user-data-dir={user_data_dir}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-popup-blocking")
    opts.add_argument("--disable-features=IsolateOrigins,site-per-process")
    opts.add_argument("--force-device-scale-factor=1")

    # Prevent throttling/occlusion issues (important for off-screen windows)
    opts.add_argument("--disable-background-timer-throttling")
    opts.add_argument("--disable-backgrounding-occluded-windows")
    opts.add_argument("--disable-renderer-backgrounding")

    opts.add_argument(f"--window-size={size}")
    opts.add_argument(f"--window-position={pos}")

    if headless:
        # legacy headless is more compatible with X selectors
        opts.add_argument("--headless=old")
        opts.add_argument("--hide-scrollbars")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")

    return opts


def _make_driver(user_data_dir: str, headless: bool, size: str, pos: str) -> uc.Chrome:
    opts = _make_options(user_data_dir, headless, size, pos)

    # ✅ Force UC to fetch a matching major-version chromedriver (prevents 145 driver vs 144 chrome)
    if FORCE_CHROME_MAJOR is None:
        driver = uc.Chrome(options=opts)
    else:
        driver = uc.Chrome(options=opts, version_main=int(FORCE_CHROME_MAJOR))

    # Ensure device metrics and bring-to-front semantics even in headless
    try:
        w, h = (int(x) for x in size.split(","))
        driver.set_window_size(w, h)
        driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
            "width": w, "height": h, "deviceScaleFactor": 1, "mobile": False
        })
        driver.execute_cdp_cmd("Page.bringToFront", {})
        # For visible poster: explicitly place window (prevents OS from moving/minimizing)
        x, y = (int(x) for x in pos.split(","))
        driver.set_window_rect(x=x, y=y, width=w, height=h)
    except Exception:
        pass

    return driver


def _profile_has_cookies(user_data_dir: str) -> bool:
    suspects = [
        os.path.join(user_data_dir, "Network", "Cookies"),
        os.path.join(user_data_dir, "Local State"),
    ]
    for p in suspects:
        try:
            if os.path.exists(p) and os.path.getsize(p) > 0:
                return True
        except Exception:
            pass
    return False


def _looks_logged_in(driver) -> bool:
    """
    Kept for possible debugging / future use,
    but we no longer *gate* normal runs on this.
    """
    try:
        driver.get("https://x.com/home")
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(0.5)
        possible = driver.find_elements(
            By.CSS_SELECTOR,
            "a[href='/compose/post'], [data-testid='SideNav_AccountSwitcher_Button'], a[role='link'][href='/home']"
        )
        return len(possible) > 0
    except Exception:
        return False


def _bootstrap_profile(user_data_dir: str, role: str):
    os.makedirs(user_data_dir, exist_ok=True)

    # --- Decide once whether cookies exist ---
    has_cookies = _profile_has_cookies(user_data_dir)

    if role == "scraper":
        # If we already have cookies, stick to configured headless/visible.
        # If no cookies yet, launch visible so you can log in manually.
        headless = SCRAPER_HEADLESS if has_cookies else False
        size, pos = SCRAPER_WINDOW_SIZE, SCRAPER_WINDOW_POS
    else:  # poster
        # Poster should be visible anyway; if no cookies yet, also visible.
        headless = POSTER_HEADLESS if has_cookies else False
        size, pos = POSTER_WINDOW_SIZE, POSTER_WINDOW_POS

    driver = _make_driver(user_data_dir=user_data_dir, headless=headless, size=size, pos=pos)

    # --- Manual login ONLY if there are NO cookies yet ---
    if not has_cookies:
        # First-time bootstrap for this profile.
        if headless:
            # Shouldn't normally happen now, but just in case:
            try:
                driver.quit()
            except Exception:
                pass
            driver = _make_driver(user_data_dir=user_data_dir, headless=False, size=size, pos=pos)

        driver.get("https://x.com/home")
        print("[LOGIN] No cookies found; log in on this window, go to Home (Following is fine), then press Enter here...")
        try:
            input()
        except EOFError:
            pass

        # After manual login, check once and then trust cookies in future runs.
        if _looks_logged_in(driver):
            print("[LOGIN] Cookies saved. Using this session from now on.")
        else:
            print("[LOGIN] Not logged in; please try again next run.")

    mode = ("headless(old)" if headless else f"visible@{pos}")
    return driver, mode


def ensure_both_profiles_ready():
    print("[CHECK] Verifying scraper and poster profiles...")
    scraper_driver, scraper_mode = _bootstrap_profile(PROFILE_DIR_SCRAPER, "scraper")
    poster_driver,  poster_mode  = _bootstrap_profile(PROFILE_DIR_POSTER,  "poster")
    print(f"[STATUS] scraper={scraper_mode}  poster={poster_mode}")
    return scraper_driver, poster_driver
