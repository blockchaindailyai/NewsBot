# storage.py
import os

SEEN_IDS_FILE = "seen_tweet_ids.txt"
TWEETS_LOG_FILE = "tweets.log"


def load_seen_ids():
    seen = set()
    if not os.path.exists(SEEN_IDS_FILE):
        return seen
    try:
        with open(SEEN_IDS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                tid = line.strip()
                if tid:
                    seen.add(tid)
    except Exception as e:
        print(f"[STORAGE] Failed to load seen IDs: {e!r}")
    return seen


def append_seen_id(tweet_id):
    if not tweet_id:
        return
    try:
        with open(SEEN_IDS_FILE, "a", encoding="utf-8") as f:
            f.write(f"{tweet_id}\n")
    except Exception as e:
        print(f"[STORAGE] Failed to append seen ID {tweet_id}: {e!r}")


def append_tweet_log(tweet_id, username, text, analysis=None, headline=None, post_result=None):
    """
    Append a structured log entry for a tweet.
    """
    try:
        with open(TWEETS_LOG_FILE, "a", encoding="utf-8") as f:
            f.write("=" * 80 + "\n")
            f.write(f"{tweet_id} {username}\n")
            f.write((text or "").replace("\r", " ").strip() + "\n")

            if analysis:
                score = analysis.get("importance_score", 0)
                label = analysis.get("label", "low")
                reason = analysis.get("reason", "")
                reason = reason.replace("\n", " ")
                f.write(f"[IMPORTANCE] {score} {label} - {reason}\n")

            if headline:
                f.write(f"[HEADLINE] {headline}\n")

            if post_result is not None:
                f.write(f"[POST] {'Success' if post_result else 'Failed'}\n")

            f.write("\n")
    except Exception as e:
        print(f"[STORAGE] Failed to append tweet log for {tweet_id}: {e!r}")
