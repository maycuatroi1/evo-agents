"""Identifiers and time helpers."""

from __future__ import annotations

import datetime as dt
import os
import threading
import time
import uuid

_lock = threading.Lock()
_last = 0


def uuid7() -> str:
    """Time-ordered UUIDv7, strictly increasing within this process.

    Run IDs take part in the corpus merge order, so two runs started in the same millisecond must still
    compare in the order they started.
    """
    global _last
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (
        (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | ((rand >> 62) & 0xFFF) << 64 | 0b10 << 62 | (rand & ((1 << 62) - 1))
    )
    with _lock:
        if value <= _last:
            value = _last + 1
        _last = value
    return str(uuid.UUID(int=value))


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_utc(value: str) -> dt.datetime:
    """ISO 8601, with or without a trailing Z, as an aware UTC datetime. A naive value is taken as UTC."""
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def normalize_time(value: str | None) -> str:
    """ISO 8601 to a fixed-width UTC string that sorts correctly as text. Empty stays empty."""
    if not value:
        return ""
    return parse_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def rev_key(rev: str | None) -> str:
    """Revisions that are plain integers are ordered and compare numerically. Anything else (a git blob
    SHA, an etag) carries no order: it compares equal here, so the run that observed it decides."""
    if rev and rev.isdigit():
        return "n" + rev.zfill(30)
    return ""


def epoch_to_iso(seconds: float) -> str:
    return dt.datetime.fromtimestamp(seconds, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
