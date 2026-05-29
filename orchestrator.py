# orchestrator.py
import sys
import time
import threading
import subprocess
import logging
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
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
from scraper import is_likely_ad_tweet, scrape_home_tweets

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
from story_dedupe import (
    build_story_fingerprint,
    likely_same_batch_story,
    representative_score,
)
from story_registry import append_dedupe_audit, get_canonical_key

from storage import (
    load_seen_ids,
    append_seen_id,
    append_tweet_log,
)
from config import (
    DEBUG_QUEUE_LOGS,
    RETRY_CHECK_INTERVAL,
    SCRAPE_BACKOFF_SHORT_SECONDS,
    SCRAPE_BACKOFF_LONG_SECONDS,
    SCRAPE_FAILURE_THRESHOLD,
    LOOP_SLEEP_SECONDS,
    ACTIVE_LOOP_SLEEP_SECONDS,
    MAX_ANALYSIS_WORKERS,
    ANALYSIS_QUEUE_MAXSIZE,
)

logger = logging.getLogger("orchestrator")


@dataclass
class BotRuntime:
    should_exit: bool = False
    stop_event: threading.Event = field(default_factory=threading.Event)


runtime = BotRuntime()


def safe_print(*args, **kwargs):
    logger.info(" ".join(str(a) for a in args))


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
    safe_print("[COMMAND] Type 'q', 'quit', 'exit', or 'stop' then Enter to shut down gracefully.\n")
    for line in sys.stdin:
        cmd = line.strip().lower()
        if cmd in {"q", "quit", "exit", "stop"}:
            safe_print("[COMMAND] Shutdown command received. Exiting after current cycle.")
            runtime.should_exit = True
            runtime.stop_event.set()
            break


def analyze_tweet_worker(tweet_id, username, text, post_queue: Queue, story_fp=None, supporting_tweets=None):
    """
    Per-tweet analysis worker.

    - Runs GPT importance analysis + local dedupe pipeline.
    - Prints a *single atomic block* (no interlacing between threads).
    - Logs to file.
    - If a new headline is found, enqueue it for the post_sender thread.
    """
    try:
        analysis = analyze_tweet_importance(
            tweet_id,
            username,
            text,
            story_fp=story_fp,
            supporting_tweets=supporting_tweets,
        )
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
        if runtime.stop_event.is_set() and post_queue.empty() and not retry_queue:
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
            time.sleep(SCRAPE_BACKOFF_SHORT_SECONDS)
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




def _log_analysis_future_error(fut: Future):
    try:
        fut.result()
    except Exception as e:
        safe_print(f"[ERROR] Analysis worker crashed: {e!r}")


def _trim_done_futures(futures: set[Future]):
    done = {f for f in futures if f.done()}
    if done:
        futures.difference_update(done)




def _prefilter_unique_stories_batch(tweets):
    """
    Group one scraped batch into likely-unique stories before GPT analysis.

    CPU note: batches are small (~10-15 tweets), but this still uses direct key
    indexes first and only falls back to pairwise similarity for ambiguous cases.
    It returns the representative fingerprint so analysis does not rebuild it.
    """
    groups = []
    key_to_group = {}

    for tid, username, text in tweets:
        try:
            fingerprint = build_story_fingerprint(text)
        except Exception as e:
            safe_print(f"[DEDUP-WARN] Failed building story fingerprint for {tid}: {e!r}")
            groups.append({
                "items": [(tid, username, text, None)],
                "representative": (tid, username, text, None),
                "keys": set(),
            })
            continue

        keys = set(fingerprint.batch_keys)
        matched_group = None

        for key in keys:
            matched_group = key_to_group.get(key)
            if matched_group is not None:
                break

        if matched_group is None:
            for group in groups:
                rep_fp = group["representative"][3]
                if rep_fp is not None and likely_same_batch_story(fingerprint, rep_fp):
                    matched_group = group
                    break

        item = (tid, username, text, fingerprint)
        if matched_group is None:
            group = {"items": [item], "representative": item, "keys": keys}
            groups.append(group)
            for key in keys:
                key_to_group[key] = group
            continue

        matched_group["items"].append(item)
        matched_group["keys"].update(keys)
        for key in keys:
            key_to_group[key] = matched_group

        current = matched_group["representative"]
        current_score = representative_score(current[1], current[2], current[3])
        incoming_score = representative_score(username, text, fingerprint)
        if incoming_score > current_score:
            matched_group["representative"] = item

    unique = []
    for group in groups:
        rep_tid, rep_username, rep_text, rep_fp = group["representative"]
        supporting = [(tid, username) for tid, username, _, _ in group["items"]]
        unique.append((rep_tid, rep_username, rep_text, rep_fp, supporting))

        if len(group["items"]) > 1:
            skipped = [tid for tid, _, _, _ in group["items"] if tid != rep_tid]
            append_dedupe_audit(
                "batch_duplicate_grouped",
                kept_tweet_id=rep_tid,
                kept_username=rep_username,
                skipped_tweet_ids=skipped,
                source_tweet_ids=[tid for tid, _ in supporting],
                source_accounts=[username for _, username in supporting],
                canonical_key=get_canonical_key(rep_fp) if rep_fp is not None else "",
                is_recurring=bool(rep_fp and rep_fp.is_recurring),
                period_key=(rep_fp.period_key if rep_fp is not None else ""),
            )
            if DEBUG_QUEUE_LOGS:
                safe_print(
                    f"[DEDUP-BATCH] Kept representative tweet {rep_tid}; "
                    f"skipped same-batch duplicates: {', '.join(skipped)}"
                )

    return unique


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
    runtime.should_exit = False
    runtime.stop_event.clear()

    scraper_driver, poster_driver = ensure_both_profiles_ready()

    seen_ids = load_seen_ids()
    load_signal_stats()
    retry_queue = load_retry_queue()

    safe_print(f"[INIT] Loaded {len(seen_ids)} previously scraped tweet IDs from disk.")
    safe_print(f"[INIT] Loaded {len(retry_queue)} queued posts from previous runs.")
    safe_print("[INIT] Starting continuous scraping loop.\n")

    post_queue: Queue = Queue()
    analysis_pool = ThreadPoolExecutor(max_workers=max(1, MAX_ANALYSIS_WORKERS))
    analysis_futures: set[Future] = set()

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
        while not runtime.should_exit:
            safe_print("[LOOP] Scraping home timeline...")

            try:
                tweets = scrape_home_tweets(scraper_driver)
            except (InvalidSessionIdException, WebDriverException, ReadTimeoutError, TimeoutException) as e:
                safe_print(f"[WARN] Scraper driver error ({e.__class__.__name__}); will back off briefly: {e!r}")
                consecutive_scrape_failures += 1
                if consecutive_scrape_failures >= SCRAPE_FAILURE_THRESHOLD:
                    safe_print("[BACKOFF] Too many scrape failures; sleeping 60 seconds.")
                    time.sleep(SCRAPE_BACKOFF_LONG_SECONDS)
                    consecutive_scrape_failures = 0
                else:
                    time.sleep(SCRAPE_BACKOFF_SHORT_SECONDS)
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

                batch_candidates = []
                for tid, username, text in tweets:
                    try:
                        if username and not username.startswith("@"):
                            username = f"@{username}"

                        if tid in seen_ids:
                            continue

                        if is_likely_ad_tweet(username, text):
                            safe_print(f"[AD-FILTER] Skipping likely ad tweet {tid} from {username} before analysis.")
                            seen_ids.add(tid)
                            append_seen_id(tid)
                            continue

                        update_signal(username, tweet_id=tid, seen=True)
                        seen_ids.add(tid)
                        append_seen_id(tid)

                        batch_candidates.append((tid, username, text))
                    except Exception as e:
                        safe_print(f"[ERROR] Failed preparing tweet {tid}: {e!r}")

                unique_candidates = _prefilter_unique_stories_batch(batch_candidates)

                for tid, username, text, story_fp, supporting_tweets in unique_candidates:
                    try:
                        _trim_done_futures(analysis_futures)
                        if len(analysis_futures) >= max(1, ANALYSIS_QUEUE_MAXSIZE):
                            safe_print(f"[ANALYSIS] Backpressure active; skipping tweet {tid} this cycle.")
                            continue

                        fut = analysis_pool.submit(
                            analyze_tweet_worker,
                            tid,
                            username,
                            text,
                            post_queue,
                            story_fp,
                            supporting_tweets,
                        )
                        fut.add_done_callback(_log_analysis_future_error)
                        analysis_futures.add(fut)

                    except Exception as e:
                        safe_print(f"[ERROR] Failed handling tweet {tid}: {e!r}")
                        continue

            try:
                save_signal_stats()
            except Exception as e:
                safe_print(f"[WARN] Failed to save signal stats: {e!r}")

            cycle_sleep = ACTIVE_LOOP_SLEEP_SECONDS if tweets else LOOP_SLEEP_SECONDS
            for _ in range(max(1, cycle_sleep)):
                if runtime.should_exit:
                    break
                time.sleep(1)

        safe_print("[EXIT] Shutdown flag set. Stopping scraper loop.")

    finally:
        runtime.stop_event.set()

        try:
            safe_print("[EXIT] Waiting for in-flight analysis tasks...")
            analysis_pool.shutdown(wait=True, cancel_futures=False)
        except Exception as e:
            safe_print(f"[WARN] Failed waiting for analysis tasks: {e!r}")

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
