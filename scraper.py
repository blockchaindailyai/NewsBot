# scraper.py
# Scrape tweets from Home, forcing the "Following" timeline.
# UPDATED: Never skip a cycle. If confirmation fails, do a same-page recovery.

import time

from config import SCRAPER_AD_LABELS

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException


def _normalized_text(value: str) -> str:
    return " ".join((value or "").split()).casefold()


def _matches_ad_label(value: str) -> bool:
    normalized = _normalized_text(value)
    if not normalized:
        return False
    return normalized in {_normalized_text(label) for label in SCRAPER_AD_LABELS}


def is_promoted_article(article) -> bool:
    """Return True when an X timeline article is marked as an ad/promoted post."""
    marker_xpaths = (
        ".//*[@data-testid='socialContext']",
        ".//*[@aria-label]",
        ".//*[not(ancestor::*[@data-testid='tweetText']) and (self::span or self::div)][normalize-space()]",
    )

    for xpath in marker_xpaths:
        try:
            candidates = article.find_elements(By.XPATH, xpath)
        except Exception:
            continue

        for candidate in candidates:
            try:
                if _matches_ad_label(candidate.get_attribute("aria-label") or ""):
                    return True
            except Exception:
                pass

            try:
                if _matches_ad_label(candidate.text):
                    return True
            except Exception:
                pass

    return False


AD_BLOCKING_SCRIPT = r"""
const labels = new Set((arguments[0] || []).map((value) =>
  String(value || '').replace(/\s+/g, ' ').trim().toLocaleLowerCase()
));
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim().toLocaleLowerCase();
const isAdLabel = (value) => labels.has(normalize(value));
const articleIsPromoted = (article) => {
  if (!article) return false;
  const markerSelector = [
    '[data-testid="socialContext"]',
    '[aria-label]',
    'span',
    'div'
  ].join(',');
  for (const node of article.querySelectorAll(markerSelector)) {
    if (node.closest('[data-testid="tweetText"]')) continue;
    if (isAdLabel(node.getAttribute('aria-label')) || isAdLabel(node.textContent)) {
      return true;
    }
  }
  return false;
};
const removeMedia = (article) => {
  for (const media of article.querySelectorAll('video, audio, source, img[src*="/amplify_video/"], [data-testid="videoPlayer"]')) {
    try {
      if (typeof media.pause === 'function') media.pause();
      media.removeAttribute('src');
      media.load?.();
      media.remove();
    } catch (_) {}
  }
};
const prune = (root = document) => {
  let removed = 0;
  for (const article of root.querySelectorAll?.('article') || []) {
    if (articleIsPromoted(article)) {
      removeMedia(article);
      article.remove();
      removed += 1;
    }
  }
  return removed;
};
window.__newsbotPrunePromotedArticles = prune;
if (!window.__newsbotPromotedArticleObserver) {
  window.__newsbotPromotedArticleObserver = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes || []) {
        if (node.nodeType !== Node.ELEMENT_NODE) continue;
        if (node.matches?.('article')) {
          if (articleIsPromoted(node)) {
            removeMedia(node);
            node.remove();
          }
        } else {
          prune(node);
        }
      }
    }
  });
  window.__newsbotPromotedArticleObserver.observe(document.body || document.documentElement, {
    childList: true,
    subtree: true,
  });
}
return prune();
"""


def extract_text_with_emojis(el):
    try:
        return el.text
    except Exception:
        return ""


def install_ad_blocking_script(driver) -> int:
    """
    Install an in-page MutationObserver that removes promoted X articles as soon
    as they appear, preventing ad videos/media from continuing to burn CPU.
    """
    try:
        removed = driver.execute_script(AD_BLOCKING_SCRIPT, sorted(SCRAPER_AD_LABELS))
        return int(removed or 0)
    except Exception:
        return 0


def prune_promoted_articles(driver) -> int:
    """Remove already-rendered promoted articles from the feed."""
    try:
        removed = driver.execute_script("return window.__newsbotPrunePromotedArticles?.() || 0;")
        return int(removed or 0)
    except Exception:
        return 0


def remove_article(driver, article) -> bool:
    """Best-effort DOM removal for a promoted article found by Selenium."""
    try:
        driver.execute_script(
            """
            const article = arguments[0];
            for (const media of article.querySelectorAll?.('video, audio, source, [data-testid="videoPlayer"]') || []) {
              try {
                if (typeof media.pause === 'function') media.pause();
                media.removeAttribute('src');
                media.load?.();
                media.remove();
              } catch (_) {}
            }
            article.remove();
            """,
            article,
        )
        return True
    except Exception:
        return False


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

    install_ad_blocking_script(driver)

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

    prune_promoted_articles(driver)

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

    prune_promoted_articles(driver)

    tweets = []
    articles = driver.find_elements(By.XPATH, "//section//article")

    # GUARD 2: if we still appear to be on For You, do one last quick correction.
    if articles and is_tab_selected(driver, foryou_labels) and not is_tab_selected(driver, following_labels):
        ensure_following_selected(driver, timeout=1.8)
        prune_promoted_articles(driver)
        articles = driver.find_elements(By.XPATH, "//section//article")

    for article in articles:
        try:
            if is_promoted_article(article):
                remove_article(driver, article)
                continue

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
