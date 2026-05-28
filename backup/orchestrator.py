# orchestrator.py
import sys
import time
import threading

from selenium.common.exceptions import (
    TimeoutException,
    InvalidSessionIdException,
    WebDriverException,
)
from urllib3.exceptions import ReadTimeoutError

from auth import ensure_both_profiles_ready
from analyze import analyze_tweet_importance
from post_headline import post_headline_with_driver
from scraper import scrape_home_tweets
from headline_compress import compress_headline_local

from retry_queue import (
    load_retry_queue,
    save_retry_queue,
    queue_retry,
    process_retry_queue,
)
from signal_stats import update_signal
from storage import (
    load_seen_ids,
    append_seen_id,
    append_tweet_log,
)


SHOULD_EXIT = False


def request_shutdown():
    global SHOULD_EXIT
    SHOULD_EXIT = True


def _restart_drivers(old_scraper, old_poster):
    """
    Helper to close both drivers and re-run ensure_both_profiles_ready().
    """
    print("[DRIVER] Restarting Chrome drivers...")
    try:
        if old_scraper:
            old_scraper.quit()
    except Exception:
        pass

    try:
        if old_poster:
            old_poster.quit()
    except Exception:
        pass

    new_scraper, new_poster = ensure_both_profiles_ready()
    print("[DRIVER] Drivers restarted.")
    return new_scraper, new_poster


def run_bot():
    """
    Main bot "session":
      - create drivers
      - load state
      - loop until SHOULD_EXIT is set
      - quit drivers in a finally block

    Any unexpected exception here is allowed to propagate up to main.py,
    which acts as a watchdog and restarts run_bot().
    """
    global SHOULD_EXIT
    SHOULD_EXIT = False  # reset for this run

    scraper_driver, poster_driver = ensure_both_profiles_ready()

    # State for dedupe and retry
    seen_ids = load_seen_ids()
    print(f"[INIT] Loaded {len(seen_ids)} previously scraped tweet IDs from disk.")

    retry_queue = load_retry_queue()
    print(f"[INIT] Loaded {len(retry_queue)} queued posts from previous runs.")

    consecutive_scrape_failures = 0

    try:
        while not SHOULD_EXIT:
            # ----------------- 1) Process retry queue ----------------- #
            if retry_queue:
                print(f"[RETRY] Queue has {len(retry_queue)} pending items.")
                gave_up_any = False
                # process_retry_queue returns (updated_queue, drivers, gave_up_any)
                retry_queue, scraper_driver, poster_driver, gave_up_any = process_retry_queue(
                    retry_queue,
                    scraper_driver,
                    poster_driver,
                )

                try:
                    save_retry_queue(retry_queue)
                    print(f"[RETRY] Queue after processing: {len(retry_queue)} remaining.")
                except Exception as e:
                    print(f"[RETRY] Failed to save retry queue: {e!r}")

                if gave_up_any:
                    print("[RETRY] Some posts hit max attempts; restarting Chrome drivers...")
                    scraper_driver, poster_driver = _restart_drivers(scraper_driver, poster_driver)
                    # Start next loop iteration with fresh drivers
                    time.sleep(5)
                    continue

            # ----------------- 2) Scrape home timeline ----------------- #
            print("[LOOP] Scraping home timeline...")

            try:
                tweets = scrape_home_tweets(scraper_driver)
            except (InvalidSessionIdException, WebDriverException, ReadTimeoutError, TimeoutException) as e:
                print(f"[WARN] Scraper driver error ({e.__class__.__name__}); restarting drivers...")
                consecutive_scrape_failures += 1
                scraper_driver, poster_driver = _restart_drivers(scraper_driver, poster_driver)
                # Backoff if things are really broken
                if consecutive_scrape_failures >= 5:
                    print("[BACKOFF] Too many scrape failures; sleeping 60 seconds.")
                    time.sleep(60)
                    consecutive_scrape_failures = 0
                continue
            except Exception as e:
                # Let truly unexpected errors bubble out
                print(f"[ERROR] Unexpected scrape error: {e!r}")
                raise

            consecutive_scrape_failures = 0  # reset on success

            if not tweets:
                print("[LOOP] No tweets found this cycle.")
                time.sleep(5)
                continue

            print(f"[LOOP] Found {len(tweets)} tweets this cycle.")

            # ----------------- 3) Analyze + post ----------------- #
            new_count = 0
            for tid, username, text in tweets:
                try:
                    # --- signal: every seen tweet ---
                    update_signal(username, seen=True)

                    if tid in seen_ids:
                        print(f"[SKIP] Ignoring {tid} (already scraped).")
                        continue

                    new_count += 1
                    seen_ids.add(tid)
                    append_seen_id(tid)

                    # --- importance analysis ---
                    analysis = analyze_tweet_importance(tid, username, text)

                    print("=" * 80)
                    score = analysis.get("importance_score", 0)
                    label = analysis.get("label", "low")
                    label_char = label[0].upper()
                    imp = f"{score}{label_char}"

                    display_text = text.replace("\n", " ")
                    if len(display_text) > 180:
                        display_text = display_text[:177] + "..."

                    print(f"{username} | {display_text} | {imp}")

                    reason = analysis.get("reason", "")
                    if reason:
                        short_reason = reason.replace("\n", " ")
                        if len(short_reason) > 200:
                            short_reason = short_reason[:197] + "..."
                        print(f"    ↳ {short_reason}")

                    headline = analysis.get("headline")
                    post_result = None

                    # --- posting ---
                    if headline:
                        print(f"[HEADLINE] {headline}")
                        try:
                            compressed = compress_headline_local(headline)
                            if compressed:
                                print(f"[COMPRESS] {compressed}")
                            post_result = post_headline_with_driver(poster_driver, headline)
                            print(f"[POST] {'Success' if post_result else 'Failed'} -> {headline}")
                        except Exception as e:
                            post_result = False
                            print(f"[POST-ERROR] {e!r}")

                        if not post_result:
                            queue_retry(
                                tid,
                                username,
                                text,
                                headline,
                                retry_queue,
                                reason="initial_post_failed",
                            )

                    # --- signal: count posts ---
                    if headline:
                        update_signal(username, posted=True)

                    # --- log to file ---
                    try:
                        append_tweet_log(
                            tid,
                            username,
                            text,
                            analysis=analysis,
                        )
                    except Exception as e:
                        print(f"[LOG-WARN] Failed to append tweet log: {e!r}")

                except KeyboardInterrupt:
                    print("[MAIN] KeyboardInterrupt received in inner loop. Exiting.")
                    SHOULD_EXIT = True
                    break
                except Exception as e:
                    print(f"[ERROR] Unexpected error per-tweet: {e!r}")
                    # Don't kill the whole bot on a single tweet failure.
                    continue

            print(f"[LOOP] Cycle complete. New tweets processed: {new_count}.")
            # Short pause before next scrape
            for _ in range(5):
                if SHOULD_EXIT:
                    break
                time.sleep(1)

        print("[EXIT] Shutdown flag set. Stopping scraper loop.")

    finally:
        # Ensure drivers are closed no matter what
        try:
            scraper_driver.quit()
        except Exception:
            pass
        try:
            poster_driver.quit()
        except Exception:
            pass
