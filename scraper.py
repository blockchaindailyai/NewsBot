# scraper.py
# Scrape tweets from Home, forcing the "Following" timeline.
# UPDATED: Never skip a cycle. If confirmation fails, do a same-page recovery.

import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException


def extract_text_with_emojis(el):
    try:
        return el.text
    except Exception:
        return ""


def _js_click(driver, el) -> bool:
    try:
        driver.execute_script("arguments[0].click();", el)
        return True
    except Exception:
        try:
            el.click()
            return True
        except Exception:
            return False


def _tab_label_variants():
    # English + Korean UI
    return ("Following", "팔로잉"), ("For you", "추천", "For You")


def is_tab_selected(driver, labels) -> bool:
    for lbl in labels:
        try:
            if driver.find_elements(
                By.XPATH,
                f"//div[@role='tablist']//*[@aria-selected='true' and normalize-space()='{lbl}']"
            ):
                return True
        except Exception:
            pass
    return False


def click_tab(driver, labels) -> bool:
    xpaths = [
        "//div[@role='tablist']//*[normalize-space()=$LBL]/ancestor::*[@role='tab'][1]",
        "//div[@role='tablist']//*[normalize-space()=$LBL]/ancestor::a[1]",
        "//*[@role='tab' and normalize-space()=$LBL]",
        "//*[normalize-space()=$LBL]/ancestor::*[@role='tab'][1]",
        "//*[normalize-space()=$LBL]/ancestor::a[1]",
    ]
    for lbl in labels:
        for xp in xpaths:
            xp2 = xp.replace("$LBL", f"'{lbl}'")
            try:
                els = driver.find_elements(By.XPATH, xp2)
                if els and _js_click(driver, els[0]):
                    return True
            except Exception:
                continue
    return False


def wait_for_tablist(driver, timeout: float = 6.0) -> bool:
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.XPATH, "//div[@role='tablist']"))
        )
        return True
    except TimeoutException:
        return False


def ensure_following_selected(driver, timeout: float = 3.0) -> bool:
    following_labels, _ = _tab_label_variants()

    if is_tab_selected(driver, following_labels):
        return True

    deadline = time.time() + timeout
    while time.time() < deadline:
        click_tab(driver, following_labels)

        # short poll for selection flip
        end = time.time() + 0.8
        while time.time() < end:
            if is_tab_selected(driver, following_labels):
                time.sleep(0.20)  # tiny settle to reduce flip-back
                return True
            time.sleep(0.10)

    return False


def recover_following_same_page(driver) -> bool:
    """
    Single recovery attempt on the SAME page:
    - by now hydration is usually done
    - wait a bit, click Following, verify briefly
    """
    following_labels, _ = _tab_label_variants()

    time.sleep(1.0)  # "hydration is done" delay
    click_tab(driver, following_labels)

    # brief verify
    end = time.time() + 2.0
    while time.time() < end:
        if is_tab_selected(driver, following_labels):
            time.sleep(0.20)
            return True
        time.sleep(0.10)

    return False


def open_home(driver):
    driver.get("https://x.com/home")

    try:
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    except TimeoutException:
        pass

    # wait for tablist to exist then settle a bit
    if wait_for_tablist(driver, timeout=6.0):
        time.sleep(0.8)

    # fast attempt
    ok = ensure_following_selected(driver, timeout=3.0)

    # If it fails, do a SAME-PAGE recovery instead of skipping the cycle
    if not ok:
        ok = recover_following_same_page(driver)
        if not ok:
            # Don't skip; we proceed but we log it.
            print("[WARN] Could not confirm 'Following' after recovery; proceeding anyway (guarded scrape).")

    # Wait for tweets
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, "//section//article"))
        )
    except TimeoutException:
        pass

    return True


def scrape_home_tweets(driver):
    open_home(driver)

    following_labels, foryou_labels = _tab_label_variants()

    # GUARD 1: right before scraping, if For You is selected, click Following once more.
    if is_tab_selected(driver, foryou_labels) and not is_tab_selected(driver, following_labels):
        ensure_following_selected(driver, timeout=1.8)

    tweets = []
    articles = driver.find_elements(By.XPATH, "//section//article")

    # GUARD 2: if we still appear to be on For You, do one last quick correction.
    if articles and is_tab_selected(driver, foryou_labels) and not is_tab_selected(driver, following_labels):
        ensure_following_selected(driver, timeout=1.8)
        articles = driver.find_elements(By.XPATH, "//section//article")

    for article in articles:
        try:
            link = article.find_element(By.XPATH, ".//a[contains(@href, '/status/')]")
            href = link.get_attribute("href") or ""
            if "/status/" not in href:
                continue

            tweet_id = href.split("/status/")[-1].split("?")[0].strip()
            if not tweet_id:
                continue

            try:
                user_span = article.find_element(By.XPATH, ".//span[starts-with(normalize-space(text()), '@')]")
                username = user_span.text.strip()
                if username.lower() == "@bcdnewsbot":
                    continue
            except Exception:
                username = "@unknown"

            try:
                text_div = article.find_element(By.XPATH, ".//div[@data-testid='tweetText']")
            except Exception:
                continue

            text = extract_text_with_emojis(text_div).strip()
            if not text:
                continue

            tweets.append((tweet_id, username, text))

        except Exception:
            continue

    return tweets
