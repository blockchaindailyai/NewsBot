# story_registry.py
"""
Structured, in-memory story registry backed by JSONL.

The registry is intentionally lightweight for small VPS deployments:
- load once at process start
- keep dedupe indexes in memory
- append one JSON object per posted story

It complements the legacy headline text files while giving dedupe decisions
period-aware story metadata instead of relying only on compressed headline text.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from config import (
    DEDUPE_AUDIT_PATH,
    STORY_REGISTRY_PATH,
    NON_RECURRING_DUP_WINDOW_HOURS,
)
from story_dedupe import StoryFingerprint

_LOCK = threading.Lock()
_LOADED = False
_RECORDS: list[dict[str, Any]] = []
_RECURRING_KEYS: set[str] = set()
_EXACT_KEYS: set[str] = set()
_CANONICAL_KEYS: set[str] = set()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _iso_now() -> str:
    return _utc_now().isoformat().replace("+00:00", "Z")


def _recurring_key(fp: StoryFingerprint) -> str:
    if not fp.is_recurring or not fp.period_key or not fp.entity_action_key:
        return ""
    return f"recurring:{fp.period_key}:{fp.entity_action_key}"


def _story_key(fp: StoryFingerprint) -> str:
    if fp.is_recurring or fp.is_price_move or not fp.entity_action_key:
        return ""
    return f"story:{fp.entity_action_key}"


def _exact_key(fp: StoryFingerprint) -> str:
    if not fp.exact_key:
        return ""
    return f"exact:{fp.exact_key}"


def _canonical_key(fp: StoryFingerprint) -> str:
    return _recurring_key(fp) or _story_key(fp) or _exact_key(fp)


def _index_record(record: dict[str, Any]) -> None:
    exact_key = record.get("exact_key")
    recurring_key = record.get("recurring_key")
    canonical_key = record.get("canonical_key")
    if exact_key:
        _EXACT_KEYS.add(str(exact_key))
    if recurring_key:
        _RECURRING_KEYS.add(str(recurring_key))
    if canonical_key:
        _CANONICAL_KEYS.add(str(canonical_key))


def load_story_registry() -> None:
    global _LOADED
    with _LOCK:
        if _LOADED:
            return
        _LOADED = True
        if not os.path.exists(STORY_REGISTRY_PATH):
            return
        try:
            with open(STORY_REGISTRY_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        _RECORDS.append(record)
                        _index_record(record)
        except Exception as e:
            print(f"[STORY-REGISTRY-WARN] Failed to load story registry: {e}")


def get_canonical_key(fp: StoryFingerprint) -> str:
    """Return the best structured key for this story fingerprint."""
    return _canonical_key(fp)


def has_historical_duplicate(fp: StoryFingerprint) -> bool:
    """
    High-precision historical duplicate check.

    Recurring stories require an explicit period key, so April-vs-May style
    stories do not collide. Non-recurring broad story keys are checked only in a
    recent time window to avoid suppressing a genuinely new outage/exploit/etc.
    much later. Asset price moves deliberately do not use broad story keys,
    because a later larger move in the same asset/direction is a new market
    update, not a duplicate. Exact headline keys remain global for non-recurring
    stories.
    """
    load_story_registry()
    recurring_key = _recurring_key(fp)
    exact_key = _exact_key(fp)
    story_key = _story_key(fp)

    with _LOCK:
        canonical_key = _canonical_key(fp)
        if recurring_key:
            return recurring_key in _RECURRING_KEYS or canonical_key in _CANONICAL_KEYS

        if exact_key and exact_key in _EXACT_KEYS and not fp.is_recurring:
            return True

        if not story_key:
            return False

        cutoff = _utc_now() - timedelta(hours=max(1, NON_RECURRING_DUP_WINDOW_HOURS))
        for record in reversed(_RECORDS):
            if record.get("story_key") != story_key:
                continue
            posted_at = _parse_dt(str(record.get("posted_at", "")))
            if posted_at and posted_at >= cutoff:
                return True
        return False


def save_story_record(
    *,
    headline: str,
    fingerprint: StoryFingerprint,
    tweet_id: str,
    username: str,
    source_tweet_ids: list[str] | None = None,
    source_accounts: list[str] | None = None,
) -> None:
    """Append structured metadata for a posted story."""
    load_story_registry()
    record = {
        "posted_at": _iso_now(),
        "tweet_id": str(tweet_id),
        "username": username or "",
        "headline": headline or "",
        "fallback_headline": fingerprint.fallback_headline,
        "exact_key": _exact_key(fingerprint),
        "story_key": _story_key(fingerprint),
        "recurring_key": _recurring_key(fingerprint),
        "canonical_key": _canonical_key(fingerprint),
        "entity_action_key": fingerprint.entity_action_key,
        "is_recurring": fingerprint.is_recurring,
        "is_price_move": fingerprint.is_price_move,
        "period_key": fingerprint.period_key,
        "tokens": sorted(fingerprint.token_set),
        "source_tweet_ids": source_tweet_ids or [str(tweet_id)],
        "source_accounts": source_accounts or ([username] if username else []),
    }

    with _LOCK:
        _RECORDS.append(record)
        _index_record(record)
        try:
            with open(STORY_REGISTRY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        except Exception as e:
            print(f"[STORY-REGISTRY-WARN] Failed to write story registry: {e}")


def append_dedupe_audit(event: str, **details: Any) -> None:
    """Persist lightweight dedupe decisions for later tuning/debugging."""
    payload = {
        "created_at": _iso_now(),
        "event": event,
        **details,
    }
    try:
        with open(DEDUPE_AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as e:
        print(f"[DEDUPE-AUDIT-WARN] Failed to write audit event: {e}")
