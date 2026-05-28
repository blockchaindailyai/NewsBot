# orchestrator.py
import sys
import time
import threading
import subprocess
from queue import Queue, Empty

from selenium.common.exceptions import (
    TimeoutException,
    InvalidSessionIdException,
    WebDriverException,
)
from urllib3.exceptions import ReadTimeoutError

from auth import ensure_both_profiles_ready
from analyze import analyze_tweet_importance
from post_headline import post_headline_with_driver, ComposeUnavailableError
from scraper import scrape_home_tweets

from retry_queue import (
    load_retry_queue,
    save_retry_queue,
    process_retry_queue,
    queue_retry,
)
from signal_stats import (
    load_signal_stats,
    save_signal_stats,
    update_signal,   # <-- supports tweet_id kwarg
)
from storage import (
    load_seen_ids,
    append_seen_id,
    append_tweet_log,
)

# ---------------- CONFIG: VERBOSITY / BEHAVIOR ---------------- #

DEBUG_QUEUE_LOGS = False
RETRY_CHECK_INTERVAL = 60.0

# --------------------------------------------------------------- #

SHOULD_EXIT = False
STOP_EVENT = threading.Event()

_print_lock = threading.Lock()


def safe_print(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)


def _kill_chrome_tree():
    """
    Kill any leftover chrome/chromedriver that may be holding profile locks
    or leaving zombie sessions around after crashes.
    """
    for img in ("chrome.exe", "chromedriver.exe"):
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", img, "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass


def _poster_session_is_dead(err: Exception) -> bool:
    """
    Best-effort detection that the Selenium session is dead / disconnected.
    """
    s = (str(err) or "").lower()
    return (
        isinstance(err, InvalidSessionIdException)
        or "invalid session id" in s
        or "disconnected" in s
        or "chrome not reachable" in s
        or "session deleted" in s
        or "target window already closed" in s
        or "no such window" in s
    )


def _rebuild_poster_driver(current_driver):
    """
    Rebuild ONLY the poster driver using your existing auth bootstrap,
    and avoid leaking an extra scraper driver by immediately quitting it.
    """
    safe_print("[POST] Rebuilding poster driver...")
    try:
        try:
            current_driver.quit()
        except Exception:
            pass

        # Kill any zombie processes that might lock the profile
        _kill_chrome_tree()

        # ensure_both_profiles_ready returns (scraper_driver, poster_driver)
        scraper_tmp, poster_new = ensure_both_profiles_ready()

        # We only want the poster driver here; quit the temporary scraper
        try:
            scraper_tmp.quit()
        except Exception:
            pass

        safe_print("[POST] Poster driver rebuilt successfully.")
        return poster_new
    except Exception as e:
        safe_print(f"[POST] Failed to rebuild poster driver: {e!r}")
        return None


def input_listener():
    """
    Listens on stdin for 'q/quit/exit/stop' and sets SHOULD_EXIT/STOP_EVENT so
    the main loop and poster thread can exit cleanly.
    """
    global SHOULD_EXIT
    safe_print("[COMMAND] Type 'q', 'quit', 'exit', or 'stop' then Enter to shut down gracefully.\n")
    for line in sys.stdin:
        cmd = line.strip().lower()
        if cmd in {"q", "quit", "exit", "stop"}:
            safe_print("[COMMAND] Shutdown command received. Exiting after current cycle.")
            SHOULD_EXIT = True
            STOP_EVENT.set()
            break


def analyze_tweet_worker(tweet_id, username, text, post_queue: Queue):
    """
    Per-tweet analysis worker.

    - Runs GPT importance analysis + local dedupe pipeline.
    - Prints a *single atomic block* (no interlacing between threads).
    - Logs to file.
    - If a new headline is found, enqueue it for the post_sender thread.
    """
    try:
        analysis = analyze_tweet_importance(tweet_id, username, text)
    except Exception as e:
        safe_print(f"[ERROR] Failed analyzing tweet {tweet_id}: {e!r}")
        return

    score = analysis.get("importance_score", 0)
    label = analysis.get("label", "low")
    reason = analysis.get("reason", "")
    headline = analysis.get("headline")

    label_char = label[0].upper() if label else "L"
    imp = f"{score}{label_char}"

    display_text = (text or "").replace("\n", " ")
    if len(display_text) > 220:
        display_text = display_text[:217] + "..."

    lines = []
    lines.append("=" * 80)
    lines.append(f"{username} | {display_text} | {imp}")

    if reason:
        short_reason = reason.replace("\n", " ")
        if len(short_reason) > 200:
            short_reason = short_reason[:197] + "..."
        lines.append(f"    ↳ {short_reason}")

    if headline:
        lines.append(f"[HEADLINE] {headline}")

    block = "\n".join(lines)
    safe_print(block)
    safe_print()

    # Log analysis stage
    try:
        append_tweet_log(
            tweet_id,
            username,
            text,
            analysis=analysis,
            headline=headline,
            post_result=None,
        )
    except Exception as e:
        safe_print(f"[WARN] Failed to log tweet {tweet_id}: {e!r}")

    # Hand off to poster thread
    if headline:
        if DEBUG_QUEUE_LOGS:
            safe_print(f"[POST-QUEUE] Enqueue headline for tweet {tweet_id}.")
        post_queue.put({
            "tweet_id": tweet_id,
            "username": username,
            "text": text,
            "headline": headline,
        })


def post_sender_loop(poster_driver, post_queue: Queue, retry_queue):
    """
    Independent thread responsible for actually posting headlines.

    Loop:
      1) Periodically process retry_queue (failed posts from earlier runs).
      2) Dequeue new headlines from post_queue and post them.
      3) Repeat until STOP_EVENT is set and there is no more work.
    """
    last_retry_check = 0.0
    compose_backoff_until = 0.0  # when > now, temporarily stop trying to post

    while True:
        # Global shutdown: stop once requested AND there is no more work.
        if STOP_EVENT.is_set() and post_queue.empty() and not retry_queue:
            break

        now = time.time()

        # 1) Periodic retry queue processing
        if retry_queue and (now - last_retry_check >= RETRY_CHECK_INTERVAL):
            try:
                if DEBUG_QUEUE_LOGS:
                    safe_print(f"[RETRY] Processing {len(retry_queue)} queued posts...")

                gave_up_any = process_retry_queue(poster_driver, retry_queue)

            except (InvalidSessionIdException, WebDriverException, ReadTimeoutError, TimeoutException) as e:
                safe_print(f"[RETRY] Poster driver error while processing retries: {e!r}")
                if _poster_session_is_dead(e):
                    new_driver = _rebuild_poster_driver(poster_driver)
                    if new_driver:
                        poster_driver = new_driver
                        try:
                            # Try once more immediately after rebuild
                            gave_up_any = process_retry_queue(poster_driver, retry_queue)
                        except Exception as e2:
                            safe_print(f"[RETRY] Still failing after poster rebuild: {e2!r}")
                            gave_up_any = False
                    else:
                        gave_up_any = False
                else:
                    gave_up_any = False

            except Exception as e:
                safe_print(f"[RETRY] Error while processing retry queue: {e!r}")
                gave_up_any = False

            # Persist retry queue state from here (single-threaded owner)
            try:
                save_retry_queue(retry_queue)
            except Exception as e:
                safe_print(f"[RETRY] Failed to save retry queue: {e!r}")

            if gave_up_any:
                safe_print("[RETRY] Some posts hit max attempts (gave up).")

            last_retry_check = now

        # 2) Handle new posts from analysis workers
        try:
            item = post_queue.get(timeout=2.0)
        except Empty:
            continue

        tweet_id = item.get("tweet_id")
        username = item.get("username")
        text = item.get("text")
        headline = item.get("headline")

        if DEBUG_QUEUE_LOGS:
            safe_print(f"[POST-QUEUE] Dequeue headline for tweet {tweet_id}.")

        # Backoff window for compose outages / site error pages
        if compose_backoff_until and time.time() < compose_backoff_until:
            if DEBUG_QUEUE_LOGS:
                safe_print("[POST] Compose backoff active; requeueing item without posting.")
            post_queue.put(item)
            post_queue.task_done()
            time.sleep(5)
            continue

        post_result = None
        try:
            post_result = post_headline_with_driver(poster_driver, headline)
            safe_print(f"[POST] {'Success' if post_result else 'Failed'} -> {headline}")

        except ComposeUnavailableError as e:
            post_result = False
            safe_print(f"[POST-ERROR] Compose unavailable while posting {tweet_id}: {e!r}")

            # ✅ IMPORTANT: if the underlying webdriver session is dead, rebuild it
            if _poster_session_is_dead(e):
                new_driver = _rebuild_poster_driver(poster_driver)
                if new_driver:
                    poster_driver = new_driver
                else:
                    # If we can't rebuild, back off harder to avoid thrashing
                    compose_backoff_until = time.time() + 30

            # Even if it's not a dead session, back off a bit to avoid hammering
            compose_backoff_until = time.time() + 15

        except (InvalidSessionIdException, WebDriverException, ReadTimeoutError, TimeoutException) as e:
            post_result = False
            safe_print(f"[POST-ERROR] WebDriver error while posting {tweet_id}: {e!r}")

            # ✅ Rebuild poster on dead session indicators
            if _poster_session_is_dead(e):
                new_driver = _rebuild_poster_driver(poster_driver)
                if new_driver:
                    poster_driver = new_driver
                else:
                    compose_backoff_until = time.time() + 30

        except Exception as e:
            post_result = False
            safe_print(f"[POST-ERROR] Unexpected error while posting {tweet_id}: {e!r}")

        # Signal stats (count once per tweet_id)
        if headline:
            update_signal(username, tweet_id=tweet_id, posted=True)

        if not post_result and headline:
            queue_retry(
                tweet_id,
                username,
                text,
                headline,
                retry_queue,
                reason="initial_post_failed",
            )

        # Optional: second log including post_result
        try:
            append_tweet_log(
                tweet_id,
                username,
                text,
                analysis=None,  # already logged once with analysis
                headline=headline,
                post_result=post_result,
            )
        except Exception as e:
            safe_print(f"[WARN] Failed to log post result for {tweet_id}: {e!r}")

        post_queue.task_done()

    safe_print("[EXIT] post_sender thread exiting.")


def run_bot():
    """
    Main bot session with threading:

      - Main thread:
          * Owns both Selenium drivers.
          * Runs the scraping loop (home timeline).
          * Marks tweets as seen.
          * Spawns per-tweet analysis threads (no Selenium inside those).

      - input_listener thread:
          * Watches stdin for 'q/quit/exit/stop' and flips SHOULD_EXIT/STOP_EVENT.

      - post_sender thread:
          * Owns poster_driver.
          * Handles retry_queue + new headlines from analysis workers.
          * Saves retry_queue itself (no race with main thread).

      - Per-tweet analysis threads:
          * Run analyze_tweet_importance().
          * Print full info in a single atomic block.
          * If headline, enqueue for post_sender.
    """
    global SHOULD_EXIT
    SHOULD_EXIT = False
    STOP_EVENT.clear()

    scraper_driver, poster_driver = ensure_both_profiles_ready()

    seen_ids = load_seen_ids()
    load_signal_stats()
    retry_queue = load_retry_queue()

    safe_print(f"[INIT] Loaded {len(seen_ids)} previously scraped tweet IDs from disk.")
    safe_print(f"[INIT] Loaded {len(retry_queue)} queued posts from previous runs.")
    safe_print("[INIT] Starting continuous scraping loop.\n")

    post_queue: Queue = Queue()

    listener_thread = threading.Thread(target=input_listener, daemon=True)
    listener_thread.start()

    poster_thread = threading.Thread(
        target=post_sender_loop,
        args=(poster_driver, post_queue, retry_queue),
        daemon=False,
    )
    poster_thread.start()

    consecutive_scrape_failures = 0

    try:
        while not SHOULD_EXIT:
            safe_print("[LOOP] Scraping home timeline...")

            try:
                tweets = scrape_home_tweets(scraper_driver)
            except (InvalidSessionIdException, WebDriverException, ReadTimeoutError, TimeoutException) as e:
                safe_print(f"[WARN] Scraper driver error ({e.__class__.__name__}); will back off briefly: {e!r}")
                consecutive_scrape_failures += 1
                if consecutive_scrape_failures >= 5:
                    safe_print("[BACKOFF] Too many scrape failures; sleeping 60 seconds.")
                    time.sleep(60)
                    consecutive_scrape_failures = 0
                else:
                    time.sleep(5)
                continue
            except Exception as e:
                safe_print(f"[ERROR] Unexpected scrape error: {e!r}")
                raise

            if not tweets:
                safe_print("[LOOP] No tweets found this cycle.\n")
                consecutive_scrape_failures += 1
            else:
                safe_print(f"[LOOP] Found {len(tweets)} tweets this cycle.")
                consecutive_scrape_failures = 0

                for tid, username, text in tweets:
                    try:
                        if username and not username.startswith("@"):
                            username = f"@{username}"

                        if tid in seen_ids:
                            continue

                        update_signal(username, tweet_id=tid, seen=True)

                        seen_ids.add(tid)
                        append_seen_id(tid)

                        worker = threading.Thread(
                            target=analyze_tweet_worker,
                            args=(tid, username, text, post_queue),
                            daemon=True,
                        )
                        worker.start()

                    except Exception as e:
                        safe_print(f"[ERROR] Failed handling tweet {tid}: {e!r}")
                        continue

            try:
                save_signal_stats()
            except Exception as e:
                safe_print(f"[WARN] Failed to save signal stats: {e!r}")

            for _ in range(20):
                if SHOULD_EXIT:
                    break
                time.sleep(1)

        safe_print("[EXIT] Shutdown flag set. Stopping scraper loop.")

    finally:
        STOP_EVENT.set()

        try:
            safe_print("[EXIT] Waiting for post_queue to drain...")
            post_queue.join()
        except Exception:
            pass

        try:
            safe_print("[EXIT] Waiting for post_sender thread to exit...")
            poster_thread.join(timeout=10)
        except Exception:
            pass

        try:
            save_retry_queue(retry_queue)
        except Exception as e:
            safe_print(f"[WARN] Failed final save of retry queue: {e!r}")

        try:
            scraper_driver.quit()
        except Exception:
            pass
        try:
            poster_driver.quit()
        except Exception:
            pass

        safe_print("[EXIT] Drivers closed in run_bot().")
