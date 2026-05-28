# scraper.py
# Responsible ONLY for scraping tweets from the home timeline.

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException


def extract_text_with_emojis(el):
    """
    Safe extractor using Selenium's .text (keeps emojis by default).
    """
    try:
        return el.text
    except Exception:
        return ""


def open_home(driver):
    """
    Open the X home timeline and wait (briefly) for <article> elements.
    """
    driver.get("https://x.com/home")
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.XPATH, "//section//article"))
        )
    except TimeoutException:
        print("[WARN] Timeout waiting for tweets on home; page may be login/interstitial or empty feed.")


def scrape_home_tweets(driver):
    """
    Scrape tweets (id, username, text) from the home timeline.

    Returns:
        list of (tweet_id, username, text)
    """
    open_home(driver)

    tweets = []
    articles = driver.find_elements(By.XPATH, "//section//article")
    print(f"[DEBUG] Found {len(articles)} <article> elements on home.")

    for article in articles:
        try:
            # 1) Get tweet ID from status link
            link = article.find_element(By.XPATH, ".//a[contains(@href, '/status/')]")
            href = link.get_attribute("href") or ""
            if "/status/" not in href:
                continue

            tweet_id = href.split("/status/")[-1].split("?")[0].strip()
            if not tweet_id:
                continue

            # 2) Get author handle and skip @BCDNewsBot
            try:
                user_span = article.find_element(
                    By.XPATH,
                    ".//span[starts-with(normalize-space(text()), '@')]"
                )
                username = user_span.text.strip()
                if username.lower() == "@bcdnewsbot":
                    print(f"[SKIP] Ignoring {tweet_id} from {username}")
                    continue
            except Exception:
                username = "@unknown"

            # 3) Tweet text
            try:
                text_div = article.find_element(
                    By.XPATH,
                    ".//div[@data-testid='tweetText']"
                )
            except Exception:
                # No visible tweet text (video-only, etc.)
                continue

            text = extract_text_with_emojis(text_div).strip()
            if not text:
                continue

            tweets.append((tweet_id, username, text))

        except Exception:
            # Ignore single-article failures
            continue

    return tweets
