# signal_stats.py
"""
Signal tracker for Blockchain Daily bot.

Features:
- Counts per-account "seen" and "posted" events.
- Prevents double counting per (username, tweet_id, event_type).
  (A tweet can be counted once as seen AND once as posted.)
- Logs each counted event with UTC timestamp to account_signal_events.csv.
- On every save_signal_stats(), recomputes rolling:
    * daily  (last 24h)
    * weekly (last 7d)
    * monthly (last 30d)
    * average-per-day over full history
- Writes ONE unified CSV: account_signal.csv with columns:
  username,
  total seen, total posted, total ratio,
  daily seen, daily posted, daily ratio,
  weekly seen, weekly posted, weekly ratio,
  monthly seen, monthly posted, monthly ratio,
  avg seen/day, avg posted/day, avg ratio, avg days
"""

import csv
import os
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Optional, Dict

ACCOUNT_SIGNAL_CSV = "account_signal.csv"
ACCOUNT_SIGNAL_EVENTS_CSV = "account_signal_events.csv"

# username -> { "seen": int, "posted": int }
account_stats: Dict[str, Dict[str, int]] = {}

# NEW: username -> dict(tweet_id -> {"seen": bool, "posted": bool})
_tweet_flags_per_account: Dict[str, Dict[str, Dict[str, bool]]] = {}


# ---------------------------
# Helpers / normalization
# ---------------------------

def _norm_user(username: str) -> str:
    return (username or "").strip().lower()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _ensure_events_header():
    if os.path.exists(ACCOUNT_SIGNAL_EVENTS_CSV):
        return
    try:
        with open(ACCOUNT_SIGNAL_EVENTS_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ts_utc", "username", "tweet_id", "seen_inc", "posted_inc"])
    except Exception as e:
        print(f"[SIGNAL] Failed to create events CSV: {e!r}")


def _append_event(username: str, tweet_id: str, seen_inc: int, posted_inc: int):
    if not (seen_inc or posted_inc):
        return
    _ensure_events_header()
    try:
        with open(ACCOUNT_SIGNAL_EVENTS_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                _utc_now().isoformat(),
                username,
                tweet_id or "",
                int(seen_inc),
                int(posted_inc),
            ])
    except Exception as e:
        print(f"[SIGNAL] Failed to append event: {e!r}")


def _iter_events(since_utc: Optional[datetime] = None):
    """
    Yield events from events CSV. Optionally filter to those >= since_utc.
    Each yield: (ts, user, seen_inc, posted_inc)
    """
    if not os.path.exists(ACCOUNT_SIGNAL_EVENTS_CSV):
        return
    try:
        with open(ACCOUNT_SIGNAL_EVENTS_CSV, "r", newline="", encoding="utf-8") as f:
            r = csv.DictReader(f)
            for row in r:
                ts = _parse_ts(row.get("ts_utc", "") or "")
                if not ts:
                    continue
                if since_utc and ts < since_utc:
                    continue
                user = _norm_user(row.get("username", ""))
                if not user:
                    continue
                try:
                    seen_inc = int(row.get("seen_inc", 0) or 0)
                    posted_inc = int(row.get("posted_inc", 0) or 0)
                except ValueError:
                    continue
                yield ts, user, seen_inc, posted_inc
    except Exception as e:
        print(f"[SIGNAL] Failed to read events: {e!r}")


# ---------------------------
# Load / save
# ---------------------------

def load_signal_stats():
    """
    Loads totals snapshot from account_signal.csv if present.
    Supports both old and new unified formats.
    Initializes per-user tweet flag maps.
    """
    if not os.path.exists(ACCOUNT_SIGNAL_CSV):
        return

    try:
        with open(ACCOUNT_SIGNAL_CSV, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = [fn.strip().lower() for fn in (reader.fieldnames or [])]
            has_total = ("total seen" in fieldnames) or ("total_seen" in fieldnames)

            for row in reader:
                user_raw = row.get("username")
                if not user_raw:
                    continue
                user = _norm_user(user_raw)

                try:
                    if has_total:
                        seen = int(row.get("total seen", row.get("total_seen", 0)) or 0)
                        posted = int(row.get("total posted", row.get("total_posted", 0)) or 0)
                    else:
                        seen = int(row.get("seen", 0) or 0)
                        posted = int(row.get("posted", 0) or 0)
                except ValueError:
                    continue

                account_stats[user] = {"seen": seen, "posted": posted}
                _tweet_flags_per_account.setdefault(user, {})

    except Exception as e:
        print(f"[SIGNAL] Failed to load signal stats: {e!r}")


def save_signal_stats():
    """
    Writes ONE unified CSV (account_signal.csv) with total + rolling + averages.
    Rolling windows are computed from events history; totals from snapshot.
    """
    daily_stats = get_window_stats("daily")
    weekly_stats = get_window_stats("weekly")
    monthly_stats = get_window_stats("monthly")
    avg_stats = get_average_stats()

    try:
        with open(ACCOUNT_SIGNAL_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)

            writer.writerow([
                "username",
                "total seen", "total posted", "total ratio",
                "daily seen", "daily posted", "daily ratio",
                "weekly seen", "weekly posted", "weekly ratio",
                "monthly seen", "monthly posted", "monthly ratio",
                "avg seen/day", "avg posted/day", "avg ratio", "avg days"
            ])

            users = sorted(set(account_stats.keys())
                           | set(daily_stats.keys())
                           | set(weekly_stats.keys())
                           | set(monthly_stats.keys())
                           | set(avg_stats.keys()))

            for user in users:
                total_seen = int(account_stats.get(user, {}).get("seen", 0))
                total_posted = int(account_stats.get(user, {}).get("posted", 0))
                total_ratio = (total_posted / total_seen) if total_seen else 0.0

                d = daily_stats.get(user, {"seen": 0, "posted": 0, "ratio": 0.0})
                w = weekly_stats.get(user, {"seen": 0, "posted": 0, "ratio": 0.0})
                m = monthly_stats.get(user, {"seen": 0, "posted": 0, "ratio": 0.0})
                a = avg_stats.get(user, {"days": 0, "avg_seen_per_day": 0.0, "avg_posted_per_day": 0.0, "avg_ratio": 0.0})

                writer.writerow([
                    user,
                    total_seen, total_posted, f"{total_ratio:.4f}",
                    d["seen"], d["posted"], f"{d['ratio']:.4f}",
                    w["seen"], w["posted"], f"{w['ratio']:.4f}",
                    m["seen"], m["posted"], f"{m['ratio']:.4f}",
                    f"{a['avg_seen_per_day']:.4f}", f"{a['avg_posted_per_day']:.4f}", f"{a['avg_ratio']:.4f}", a["days"]
                ])

    except Exception as e:
        print(f"[SIGNAL] Failed to save unified signal stats: {e!r}")


# ---------------------------
# Main update entry point
# ---------------------------

def update_signal(username, tweet_id=None, seen=False, posted=False):
    """
    Increment per-account stats ONLY ONCE per (username, tweet_id, event_type).

    A tweet can count once as 'seen' and once as 'posted'.
    """
    if not username:
        return

    username = _norm_user(username)

    if username not in account_stats:
        account_stats[username] = {"seen": 0, "posted": 0}
    flags_map = _tweet_flags_per_account.setdefault(username, {})

    tid = ""
    if tweet_id is not None:
        tid = str(tweet_id)
        tflags = flags_map.setdefault(tid, {"seen": False, "posted": False})
    else:
        tflags = {"seen": False, "posted": False}

    seen_inc = 0
    posted_inc = 0

    if seen and not tflags["seen"]:
        tflags["seen"] = True
        seen_inc = 1
        account_stats[username]["seen"] += 1

    if posted and not tflags["posted"]:
        tflags["posted"] = True
        posted_inc = 1
        account_stats[username]["posted"] += 1

    if tweet_id is not None:
        flags_map[tid] = tflags

    _append_event(username, tid, seen_inc, posted_inc)


# ---------------------------
# Rolling-window statistics
# ---------------------------

def get_window_stats(window: str = "daily") -> Dict[str, Dict[str, float]]:
    window = (window or "daily").lower()
    now = _utc_now()

    since = None
    if window == "daily":
        since = now - timedelta(days=1)
    elif window == "weekly":
        since = now - timedelta(days=7)
    elif window == "monthly":
        since = now - timedelta(days=30)
    elif window == "all":
        since = None
    else:
        since = now - timedelta(days=1)

    agg = defaultdict(lambda: {"seen": 0, "posted": 0})
    for _, user, s_inc, p_inc in _iter_events(since):
        agg[user]["seen"] += s_inc
        agg[user]["posted"] += p_inc

    out: Dict[str, Dict[str, float]] = {}
    for user, d in agg.items():
        seen = int(d["seen"])
        posted = int(d["posted"])
        ratio = (posted / seen) if seen else 0.0
        out[user] = {"seen": seen, "posted": posted, "ratio": ratio}
    return out


def get_average_stats() -> Dict[str, Dict[str, float]]:
    if not os.path.exists(ACCOUNT_SIGNAL_EVENTS_CSV):
        return {}

    first_ts: Optional[datetime] = None
    totals = defaultdict(lambda: {"seen": 0, "posted": 0})

    for ts, user, s_inc, p_inc in _iter_events(None):
        if first_ts is None or ts < first_ts:
            first_ts = ts
        totals[user]["seen"] += s_inc
        totals[user]["posted"] += p_inc

    if first_ts is None:
        return {}

    now = _utc_now()
    days_span = max(1, (now - first_ts).days + 1)

    out: Dict[str, Dict[str, float]] = {}
    for user, d in totals.items():
        seen = int(d["seen"])
        posted = int(d["posted"])
        out[user] = {
            "days": days_span,
            "avg_seen_per_day": seen / days_span,
            "avg_posted_per_day": posted / days_span,
            "avg_ratio": (posted / seen) if seen else 0.0
        }
    return out
