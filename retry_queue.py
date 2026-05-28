# retry_queue.py
import json
import os

from post_headline import post_headline_with_driver

RETRY_QUEUE_FILE = "post_retry_queue.json"
MAX_RETRY_ATTEMPTS = 5


def load_retry_queue():
    if not os.path.exists(RETRY_QUEUE_FILE):
        return []
    try:
        with open(RETRY_QUEUE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        norm = []
        for item in data:
            try:
                norm.append({
                    "tweet_id": item.get("tweet_id", ""),
                    "username": item.get("username", ""),
                    "text": item.get("text", ""),
                    "headline": item.get("headline", ""),
                    "attempts": int(item.get("attempts", 0)),
                })
            except Exception:
                continue
        return norm
    except Exception as e:
        print(f"[RETRY] Failed to load retry queue: {e!r}")
        return []


def save_retry_queue(queue):
    try:
        with open(RETRY_QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[RETRY] Failed to save retry queue: {e!r}")


def queue_retry(tweet_id, username, text, headline, queue, reason=None):
    """
    Add a failed post attempt to the retry queue if not already present.
    """
    if not headline:
        return

    # Avoid duplicates by tweet_id OR headline
    for item in queue:
        if item.get("tweet_id") == tweet_id or item.get("headline") == headline:
            return

    entry = {
        "tweet_id": tweet_id,
        "username": username,
        "text": text,
        "headline": headline,
        "attempts": 0,
    }
    queue.append(entry)
    print(f"[RETRY] Queued post for retry: {tweet_id} | {headline} (reason={reason})")


def process_retry_queue(poster_driver, queue):
    """
    Process queued posts and try to publish them again.

    Returns:
        gave_up_any (bool): True if we gave up on at least one tweet
        after MAX_RETRY_ATTEMPTS.
    """
    if not queue:
        return False

    print(f"[RETRY] Processing {len(queue)} queued posts...")
    gave_up_any = False

    for item in list(queue):  # copy so we can modify original
        tid = item.get("tweet_id", "")
        headline = item.get("headline", "")
        attempts = int(item.get("attempts", 0))

        if attempts >= MAX_RETRY_ATTEMPTS:
            print(f"[RETRY] Giving up on {tid} after {attempts} attempts.")
            queue.remove(item)
            gave_up_any = True
            continue

        print(f"[RETRY] Attempting retry ({attempts + 1}/{MAX_RETRY_ATTEMPTS}) for {tid} -> {headline}")

        success = False
        try:
            success = post_headline_with_driver(poster_driver, headline)
        except Exception as e:
            success = False
            print(f"[RETRY-ERROR] {tid}: {e!r}")

        if success:
            print(f"[RETRY] Success on retry for {tid} -> {headline}")
            queue.remove(item)
        else:
            item["attempts"] = attempts + 1

    print(f"[RETRY] Queue after processing: {len(queue)} remaining.")
    return gave_up_any
