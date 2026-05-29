import importlib.util
import sys
import types
import unittest
from unittest.mock import patch


if importlib.util.find_spec("selenium") is None:
    selenium = types.ModuleType("selenium")
    webdriver = types.ModuleType("selenium.webdriver")
    common = types.ModuleType("selenium.webdriver.common")
    by_module = types.ModuleType("selenium.webdriver.common.by")
    support = types.ModuleType("selenium.webdriver.support")
    ui_module = types.ModuleType("selenium.webdriver.support.ui")
    ec_module = types.ModuleType("selenium.webdriver.support.expected_conditions")
    exceptions_module = types.ModuleType("selenium.common.exceptions")
    selenium_common = types.ModuleType("selenium.common")

    class By:
        XPATH = "xpath"
        TAG_NAME = "tag name"
        CSS_SELECTOR = "css selector"

    class TimeoutException(Exception):
        pass

    class WebDriverWait:
        def __init__(self, driver, timeout):
            self.driver = driver
            self.timeout = timeout

        def until(self, condition):
            return condition(self.driver)

    def presence_of_element_located(locator):
        by, value = locator

        def _predicate(driver):
            return driver.find_element(by, value)

        return _predicate

    by_module.By = By
    exceptions_module.TimeoutException = TimeoutException
    ui_module.WebDriverWait = WebDriverWait
    ec_module.presence_of_element_located = presence_of_element_located

    sys.modules.setdefault("selenium", selenium)
    sys.modules.setdefault("selenium.webdriver", webdriver)
    sys.modules.setdefault("selenium.webdriver.common", common)
    sys.modules.setdefault("selenium.webdriver.common.by", by_module)
    sys.modules.setdefault("selenium.webdriver.support", support)
    sys.modules.setdefault("selenium.webdriver.support.ui", ui_module)
    sys.modules.setdefault("selenium.webdriver.support.expected_conditions", ec_module)
    sys.modules.setdefault("selenium.common", selenium_common)
    sys.modules.setdefault("selenium.common.exceptions", exceptions_module)

from selenium.webdriver.common.by import By

from scraper import (
    AD_BLOCKING_SCRIPT,
    enable_ad_request_blocking,
    install_ad_blocking_script,
    is_likely_ad_tweet,
    is_promoted_article,
    _matches_ad_label,
    prune_promoted_articles,
    remove_article,
    scrape_home_tweets,
)


class FakeElement:
    def __init__(self, text="", attributes=None, elements=None):
        self.text = text
        self.attributes = attributes or {}
        self.elements = elements or {}

    def get_attribute(self, name):
        return self.attributes.get(name)

    def find_elements(self, by, value):
        if by != By.XPATH:
            return []
        if value in self.elements:
            return self.elements[value]
        if "/i/ads" in value or "promoted" in value.lower():
            return self.elements.get("ad_links", [])
        if "@data-testid='socialContext'" in value:
            return self.elements.get("social_context", [])
        if "@aria-label" in value:
            return self.elements.get("aria_labelled", [])
        if "normalize-space()" in value:
            return self.elements.get("text_markers", [])
        if "//section//article" in value:
            return self.elements.get("articles", [])
        return []

    def find_element(self, by, value):
        matches = self.find_elements(by, value)
        if not matches:
            raise Exception(f"No fake element for {value}")
        return matches[0]


class FakeDriver(FakeElement):
    def __init__(self, text="", attributes=None, elements=None, script_results=None):
        super().__init__(text=text, attributes=attributes, elements=elements)
        self.script_results = list(script_results or [])
        self.cdp_commands = []
        self.scripts = []

    def get(self, url):
        self.last_url = url

    def execute_script(self, script, *args):
        self.scripts.append((script, args))
        if self.script_results:
            return self.script_results.pop(0)
        return 0

    def execute_cdp_cmd(self, command, params):
        self.cdp_commands.append((command, params))
        return {}


class ScraperAdBlockingTests(unittest.TestCase):
    def _article(self, tweet_id, username, text, social_context=None, ad_href=None):
        status_link = FakeElement(attributes={"href": f"https://x.com/{username[1:]}/status/{tweet_id}"})
        user = FakeElement(text=username)
        tweet_text = FakeElement(text=text)
        elements = {
            ".//a[contains(@href, '/status/')]": [status_link],
            ".//span[starts-with(normalize-space(text()), '@')]": [user],
            ".//div[@data-testid='tweetText']": [tweet_text],
        }
        if social_context is not None:
            elements["social_context"] = [FakeElement(text=social_context)]
        if ad_href is not None:
            elements["ad_links"] = [FakeElement(attributes={"href": ad_href})]
        return FakeElement(elements=elements)

    def test_browser_level_ad_request_blocking_uses_cdp(self):
        driver = FakeDriver()

        self.assertTrue(enable_ad_request_blocking(driver))
        self.assertEqual(driver.cdp_commands[0], ("Network.enable", {}))
        command, params = driver.cdp_commands[1]
        self.assertEqual(command, "Network.setBlockedURLs")
        self.assertIn("*://*.doubleclick.net/*", params["urls"])
        self.assertIn("*://*.x.com/i/ads/*", params["urls"])

    def test_promoted_social_context_is_identified_as_ad(self):
        article = self._article("123", "@macro", "Useful news", social_context="Promoted")

        self.assertTrue(is_promoted_article(article))

    def test_promoted_by_social_context_is_identified_as_ad(self):
        article = self._article("123", "@macro", "Useful news", social_context="Promoted by Acme")

        self.assertTrue(is_promoted_article(article))

    def test_i_ads_link_is_identified_as_ad(self):
        article = self._article("123", "@macro", "Useful news", ad_href="https://x.com/i/ads/123")

        self.assertTrue(is_promoted_article(article))

    def test_short_ad_label_allows_delimited_variants_only(self):
        self.assertTrue(_matches_ad_label("Ad · Acme"))
        self.assertTrue(_matches_ad_label("Ad by Acme"))
        self.assertFalse(_matches_ad_label("Markets advance after Fed decision"))

    def test_likely_ad_tweet_blocks_high_confidence_ad_copy(self):
        self.assertTrue(is_likely_ad_tweet("@ads", "Sponsored Promotion  DYOR"))
        self.assertTrue(
            is_likely_ad_tweet(
                "@ads",
                "The firm that's called every U.S. crash since 2008 is hosting a FREE investing webcast.",
            )
        )
        self.assertTrue(is_likely_ad_tweet("@ads", "Cut bugs in half. Sign up for a free trial!"))
        self.assertTrue(is_likely_ad_tweet("@ads", "Access a lite version FREE for a limited time when you register."))

    def test_likely_ad_tweet_blocks_cta_promo_combinations(self):
        self.assertTrue(is_likely_ad_tweet("@ads", "Join now and claim FREE STAKE CASH instantly."))
        self.assertTrue(is_likely_ad_tweet("@unknown", "Learn more about our platform today."))

    def test_likely_ad_tweet_allows_market_news_with_promo_words(self):
        self.assertFalse(is_likely_ad_tweet("@DeItaone", "CHINA TO STUDY EXPANDING VISA-FREE ACCESS"))
        self.assertFalse(is_likely_ad_tweet("@financialjuice", "India and EU conclude free trade pact talks."))
        self.assertFalse(is_likely_ad_tweet("@macro", "Download the report: CPI rises above estimates."))

    def test_scrape_home_tweets_skips_promoted_articles(self):
        organic = self._article("123", "@macro", "Central bank cuts rates")
        promoted = self._article("456", "@ads", "Trade now", social_context="Ad")
        driver = FakeDriver(elements={"articles": [promoted, organic]})

        with patch("scraper.open_home", return_value=True):
            tweets = scrape_home_tweets(driver)

        self.assertEqual(tweets, [("123", "@macro", "Central bank cuts rates")])

    def test_ad_blocking_script_is_installed_with_configured_labels(self):
        driver = FakeDriver(script_results=[2])

        removed = install_ad_blocking_script(driver)

        self.assertEqual(removed, 2)
        script, args = driver.scripts[0]
        self.assertIs(script, AD_BLOCKING_SCRIPT)
        self.assertIn("Promoted", args[0])
        self.assertIn("characterData: true", script)
        self.assertIn("setInterval", script)
        self.assertIn("parentElement", script)

    def test_prune_promoted_articles_calls_in_page_pruner(self):
        driver = FakeDriver(script_results=[3])

        removed = prune_promoted_articles(driver)

        self.assertEqual(removed, 3)
        self.assertIn("__newsbotPrunePromotedArticles", driver.scripts[0][0])

    def test_scrape_home_tweets_removes_ad_like_text_from_dom(self):
        ad_like = self._article("654", "@ads", "Sponsored Promotion  DYOR")
        driver = FakeDriver(elements={"articles": [ad_like]})

        with patch("scraper.open_home", return_value=True):
            tweets = scrape_home_tweets(driver)

        self.assertEqual(tweets, [])
        self.assertTrue(any(args == (ad_like,) for _, args in driver.scripts))

    def test_scrape_home_tweets_removes_promoted_articles_from_dom(self):
        promoted = self._article("456", "@ads", "Trade now", social_context="Ad")
        driver = FakeDriver(elements={"articles": [promoted]})

        with patch("scraper.open_home", return_value=True):
            tweets = scrape_home_tweets(driver)

        self.assertEqual(tweets, [])
        self.assertTrue(any(args == (promoted,) for _, args in driver.scripts))

    def test_remove_article_returns_true_after_dom_removal_script(self):
        article = self._article("456", "@ads", "Trade now", social_context="Ad")
        driver = FakeDriver()

        self.assertTrue(remove_article(driver, article))
        self.assertTrue(any(args == (article,) for _, args in driver.scripts))

    def test_text_containing_ad_substring_is_not_blocked(self):
        article = self._article("789", "@macro", "Markets advance after Fed decision")

        self.assertFalse(is_promoted_article(article))


if __name__ == "__main__":
    unittest.main()
