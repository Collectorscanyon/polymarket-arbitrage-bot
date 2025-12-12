"""Inspect a Polymarket market/event URL (or slug) and print whether it's live.

Usage:
  python -m bot.debug_inspect_market_url "https://polymarket.com/event/btc-updown-15m-1765405800"
  python -m bot.debug_inspect_market_url "btc-updown-15m-1765405800"

What it does:
- Extracts the slug from the URL
- Calls Gamma: https://gamma-api.polymarket.com/events?slug=<slug>
- Prints closed/active/endDate/minutes_to_expiry

This is designed to answer "why can’t the bot find it" — often the pasted link is already expired.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

from utils.http_client import get_json


GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"


_BTC15_SLUG_RE = re.compile(r"\bbtc-updown-15m-\d{9,}\b", re.IGNORECASE)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def extract_slug(text: str) -> Optional[str]:
    """Extract a btc-updown-15m slug from a URL or raw text."""
    if not text:
        return None

    direct = _BTC15_SLUG_RE.search(text)
    if direct:
        return direct.group(0)

    # Try URL parsing (handles query params / path segments)
    try:
        parsed = urlparse(text)
        qs = parse_qs(parsed.query or "")
        for key in ("slug", "event", "market"):
            if key in qs and qs[key]:
                candidate = qs[key][0]
                m = _BTC15_SLUG_RE.search(candidate)
                if m:
                    return m.group(0)

        # Path segments
        path = parsed.path or ""
        m = _BTC15_SLUG_RE.search(path)
        if m:
            return m.group(0)
    except Exception:
        pass

    return None


def _parse_end_date(obj: dict[str, Any]) -> Optional[datetime]:
    end_date_str = obj.get("endDate")
    if not end_date_str:
        return None
    if isinstance(end_date_str, str) and end_date_str.endswith("Z"):
        end_date_str = end_date_str[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(end_date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


@dataclass
class InspectResult:
    slug: str
    found: bool
    closed: Optional[bool]
    end_date: Optional[datetime]
    minutes_to_expiry: Optional[float]

    @property
    def active(self) -> Optional[bool]:
        if not self.found:
            return None
        if self.closed is None or self.minutes_to_expiry is None:
            return None
        return (not self.closed) and (self.minutes_to_expiry > 0)


def inspect_slug(slug: str) -> InspectResult:
    events = get_json(GAMMA_EVENTS_URL, params={"slug": slug}, timeout=8) or []
    if not events:
        return InspectResult(slug=slug, found=False, closed=None, end_date=None, minutes_to_expiry=None)

    event = events[0] if isinstance(events, list) else events
    # Prefer market-level fields if available (they can differ)
    markets = event.get("markets") or []
    m0 = markets[0] if markets else {}

    closed = m0.get("closed")
    if closed is None:
        closed = event.get("closed")

    end_date = _parse_end_date(m0) or _parse_end_date(event)
    minutes_to_expiry = None
    if end_date is not None:
        minutes_to_expiry = (end_date - _utcnow()).total_seconds() / 60.0

    return InspectResult(
        slug=slug,
        found=True,
        closed=bool(closed) if closed is not None else None,
        end_date=end_date,
        minutes_to_expiry=minutes_to_expiry,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect a Polymarket URL/slug and print whether it's live.")
    parser.add_argument("input", help="Polymarket URL or slug")
    args = parser.parse_args()

    slug = extract_slug(args.input) or (args.input.strip() if args.input.strip() else None)
    if not slug:
        print("Could not extract a slug from input")
        return 2

    result = inspect_slug(slug)

    print(f"slug: {result.slug}")
    print(f"found: {result.found}")
    if not result.found:
        return 1

    print(f"closed: {result.closed}")
    print(f"endDate: {result.end_date.isoformat() if result.end_date else None}")
    if result.minutes_to_expiry is None:
        print("minutes_to_expiry: None")
    else:
        print(f"minutes_to_expiry: {result.minutes_to_expiry:.2f}")
    print(f"active: {result.active}")

    if result.closed is True or (result.minutes_to_expiry is not None and result.minutes_to_expiry <= 0):
        print("VERDICT: NOT LIVE (expired/past bucket)")
    else:
        print("VERDICT: MAY BE LIVE (check liquidity/orderbook next)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
