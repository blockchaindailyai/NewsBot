# signal_stats.py
import csv
import os

ACCOUNT_SIGNAL_CSV = "account_signal.csv"

# username -> { "seen": int, "posted": int }
account_stats = {}


def load_signal_stats():
    if not os.path.exists(ACCOUNT_SIGNAL_CSV):
        return
    try:
        with open(ACCOUNT_SIGNAL_CSV, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                user = row.get("username")
                if not user:
                    continue
                try:
                    seen = int(row.get("seen", 0))
                    posted = int(row.get("posted", 0))
                except ValueError:
                    continue
                account_stats[user] = {"seen": seen, "posted": posted}
    except Exception as e:
        print(f"[SIGNAL] Failed to load signal stats: {e!r}")


def save_signal_stats():
    try:
        with open(ACCOUNT_SIGNAL_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["username", "seen", "posted", "ratio"])
            for user, data in account_stats.items():
                seen = data.get("seen", 0)
                posted = data.get("posted", 0)
                ratio = posted / seen if seen else 0.0
                writer.writerow([user, seen, posted, f"{ratio:.4f}"])
    except Exception as e:
        print(f"[SIGNAL] Failed to save signal stats: {e!r}")


def update_signal(username, seen=False, posted=False):
    if not username:
        return
    if username not in account_stats:
        account_stats[username] = {"seen": 0, "posted": 0}
    if seen:
        account_stats[username]["seen"] += 1
    if posted:
        account_stats[username]["posted"] += 1
