"""Deterministic BTC15 slug source.

The core reliability issue with BTC15 is that "latest events" often returns buckets
far in the future (Polymarket pre-creates them). For actually trading, we want the
bucket around now.

Slugs are multiples of 900 seconds:
  btc-updown-15m-<bucket>

This module generates a tiny list of candidate buckets around now (UTC) and
fetches only those events.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from utils.http_client import get_json


GAMMA_API_BASE = "https://gamma-api.polymarket.com"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def bucket_for_timestamp(ts: float, bucket_seconds: int = 900) -> int:
    return int(ts // bucket_seconds) * bucket_seconds


def bucket_for_datetime(dt: datetime, bucket_seconds: int = 900) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return bucket_for_timestamp(dt.timestamp(), bucket_seconds=bucket_seconds)


def slug_for_bucket(bucket: int) -> str:
    return f"btc-updown-15m-{int(bucket)}"


def candidate_buckets(now: Optional[datetime] = None, offsets: Iterable[int] = (0, -1, 1, 2)) -> List[int]:
    """Return candidate 15m buckets around now.

    offsets are in number of buckets (each bucket = 15 minutes).
    Example: (0, -1, 1, 2) -> [now, now-15m, now+15m, now+30m]
    """
    now = now or utcnow()
    base = bucket_for_datetime(now)
    out: List[int] = []
    seen = set()
    for off in offsets:
        b = base + int(off) * 900
        if b not in seen:
            out.append(b)
            seen.add(b)
    return out


@dataclass(frozen=True)
class SlugEventLookup:
    slug: str
    found: bool
    events: list


def fetch_events_for_slug(slug: str, timeout: float = 6.0) -> SlugEventLookup:
    events = get_json(f"{GAMMA_API_BASE}/events", params={"slug": slug}, timeout=timeout) or []
    return SlugEventLookup(slug=slug, found=bool(events), events=events if isinstance(events, list) else [events])


def fetch_candidate_events(
    now: Optional[datetime] = None,
    offsets: Iterable[int] = (0, -1, 1, 2),
    timeout: float = 6.0,
) -> List[SlugEventLookup]:
    lookups: List[SlugEventLookup] = []
    for bucket in candidate_buckets(now=now, offsets=offsets):
        slug = slug_for_bucket(bucket)
        lookups.append(fetch_events_for_slug(slug, timeout=timeout))
    return lookups
