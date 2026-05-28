# auth.py
# UPDATED: hardened driver init so Windows ChromeDriver won't crash on window positioning.
# - Avoid negative window coordinates (can throw InvalidArgumentException on some systems)
# - Minimize poster window instead of moving off-screen
# - Extension loading from profile (Control Panel for Twitter) if installed

import os
import time
from pathlib import Path

import undetected_chromedriver as uc
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

try:
    from packaging.version import Version
except Exception:
    Version = None


# =======================
# Profiles
# =======================
PROFILE_DIR_SCRAPER = os.path.join(os.getcwd(), "x_profile")
PROFILE_DIR_POSTER  = os.path.join(os.getcwd(), "x_poster_profile")


# =======================
# Headless / window
# =======================
# NOTE: Chrome extensions do NOT run in headless mode.
SCRAPER_HEADLESS_DEFAULT = True
POSTER_HEADLESS_DEFAULT  = False

SCRAPER_WINDOW_SIZE = (1400, 950)
POSTER_WINDOW_SIZE  = (1280, 900)

# Use safe non-negative positions to avoid InvalidArgumentException
SCRAPER_WINDOW_POS = (0, 0)
POSTER_WINDOW_POS  = (0, 0)  # we'll minimize instead of going negative


# =======================
# Extension: Control Panel for Twitter
# =======================
CONTROL_PANEL_EXTENSION_ID = "kpmjjdhbcfebfjgdnpjagcndoelnidfj"
ENABLE_CONTROL_PANEL_EXTENSION = True


def _profile_has_cookies(user_data_dir: str) -> bool:
    # Some installs store cookies in different places; these are common indicators.
    suspects = [
        os.path.join(user_data_dir, "Default", "Network", "Cookies"),
        os.path.join(user_data_dir, "Default", "Cookies"),
        os.path.join(user_data_dir, "Local State"),
    ]
    for p in suspects:
        try:
            if os.path.exists(p) and os.path.getsize(p) > 0:
                return True
        except Exception:
            pass
    return False


def _find_installed_extension_path(user_data_dir: str, extension_id: str):
    """
    Chrome stores installed extensions under:
      <user_data_dir>/Default/Extensions/<extension_id>/<version_folder>/
    """
    base = Path(user_data_dir) / "Default" / "Extensions" / extension_id
    if not base.exists() or not base.is_dir():
        return None

    versions = [p for p in base.iterdir() if p.is_dir()]
    if not versions:
        return None

    def _key(p: Path):
        if Version is not None:
            try:
                return Version(p.name)
            except Exception:
                pass
        return p.name

    best = sorted(versions, key=_key, reverse=True)[0]
    return str(best.resolve())


def _safe_set_window(driver, width: int, height: int, x: int, y: int):
    """
    Window ops frequently throw InvalidArgumentException on Windows Server/RDP.
    We make them best-effort and never fatal.
    """
    try:
        driver.set_window_size(width, height)
    except Exception as e:
        print(f"[WARN] set_window_size failed: {type(e).__name__}: {e}")

    # Only attempt non-negative positions (negative often crashes)
    if x is not None and y is not None and x >= 0 and y >= 0:
        try:
            driver.set_window_position(x, y)
        except Exception as e:
            print(f"[WARN] set_window_position failed: {type(e).__name__}: {e}")

    # set_window_rect is the most fragile; keep it optional
    if x is not None and y is not None and x >= 0 and y >= 0:
        try:
            driver.set_window_rect(x=x, y=y, width=width, height=height)
        except Exception as e:
            print(f"[WARN] set_window_rect failed: {type(e).__name__}: {e}")


def _safe_cdp(driver, cmd: str, params: dict):
    try:
        driver.execute_cdp_cmd(cmd, params)
    except Exception as e:
        print(f"[WARN] CDP {cmd} failed: {type(e).__name__}: {e}")


def _make_options(user_data_dir: str, headless: bool, role: str) -> uc.ChromeOptions:
    opts = uc.ChromeOptions()
    os.makedirs(user_data_dir, exist_ok=True)

    opts.add_argument(f"--user-data-dir={user_data_dir}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-popup-blocking")

    # Keep Chrome responsive even when minimized/occluded
    opts.add_argument("--disable-background-timer-throttling")
    opts.add_argument("--disable-backgrounding-occluded-windows")
    opts.add_argument("--disable-renderer-backgrounding")

    # Reduce automation flags a bit (helps stability)
    try:
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
    except Exception:
        pass

    # Extension handling (only when NOT headless)
    if ENABLE_CONTROL_PANEL_EXTENSION and (role == "scraper") and (not headless):
        ext_path = _find_installed_extension_path(user_data_dir, CONTROL_PANEL_EXTENSION_ID)
        if ext_path:
            print(f"[EXT] Found extension folder: {ext_path}")
            opts.add_argument(f"--load-extension={ext_path}")
        else:
            expected = Path(user_data_dir) / "Default" / "Extensions" / CONTROL_PANEL_EXTENSION_ID
            print(
                "[EXT] Control Panel for Twitter NOT found in this profile.\n"
                "      Install it once into the scraper profile (x_profile), then restart.\n"
                f"      Expected folder:\n        {expected}\n"
                "      Continuing without extension for this run."
            )
            # Don't hard-disable extensions; leaving default can avoid some Chrome quirks
            # opts.add_argument("--disable-extensions")  # keep commented
    else:
        # If you're running headless, don't try to load any extensions.
        # opts.add_argument("--disable-extensions")  # optional
        pass

    if headless:
        # Use old headless for compatibility; extensions won't work here.
        opts.add_argument("--headless=old")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")

    return opts


def _make_driver(user_data_dir: str, headless: bool, role: str):
    opts = _make_options(user_data_dir, headless=headless, role=role)

    try:
        driver = uc.Chrome(options=opts)
    except Exception as e:
        # If Chrome failed to start, surface the exact reason.
        print(f"[FATAL] Chrome failed to start for role={role}: {type(e).__name__}: {e}")
        raise

    # Apply safe window settings
    if role == "scraper":
        w, h = SCRAPER_WINDOW_SIZE
        x, y = SCRAPER_WINDOW_POS
    else:
        w, h = POSTER_WINDOW_SIZE
        x, y = POSTER_WINDOW_POS

    _safe_set_window(driver, w, h, x, y)

    # Safe CDP calls (best-effort)
    _safe_cdp(driver, "Emulation.setDeviceMetricsOverride", {
        "width": w, "height": h, "deviceScaleFactor": 1, "mobile": False
    })
    _safe_cdp(driver, "Page.bringToFront", {})

    # Minimize poster window so it doesn't annoy you (instead of negative coords)
    if role == "poster":
        try:
            driver.minimize_window()
        except Exception as e:
            print(f"[WARN] minimize_window failed: {type(e).__name__}: {e}")

    return driver


def _looks_logged_in(driver) -> bool:
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
    """
    Creates driver. If no cookies yet, opens visible Chrome for manual login.
    """
    has_cookies = _profile_has_cookies(user_data_dir)

    if role == "scraper":
        # If you want to install/configure the extension, scraper MUST be visible.
        if ENABLE_CONTROL_PANEL_EXTENSION:
            headless = False
        else:
            headless = SCRAPER_HEADLESS_DEFAULT if has_cookies else False
    else:
        headless = POSTER_HEADLESS_DEFAULT if has_cookies else False

    driver = _make_driver(user_data_dir=user_data_dir, headless=headless, role=role)

    # Manual login flow if cookies missing
    if not has_cookies:
        driver.get("https://x.com/home")
        print(f"[LOGIN] No cookies found for {role}. Log in on this window, then press Enter here...")
        try:
            input()
        except EOFError:
            pass

        if _looks_logged_in(driver):
            print(f"[LOGIN] {role} logged in; cookies should now be saved in profile.")
        else:
            print(f"[LOGIN] {role} not detected as logged in yet. You can retry next run.")

    mode = "headless(old)" if headless else "visible"
    return driver, mode


def ensure_both_profiles_ready():
    print("[CHECK] Verifying scraper and poster profiles...")

    scraper_driver, scraper_mode = _bootstrap_profile(PROFILE_DIR_SCRAPER, "scraper")
    poster_driver,  poster_mode  = _bootstrap_profile(PROFILE_DIR_POSTER,  "poster")

    print(f"[STATUS] scraper={scraper_mode}  poster={poster_mode}")
    return scraper_driver, poster_driver
