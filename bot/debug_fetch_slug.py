#!/usr/bin/env python3
"""
Debug helper to test fetching a specific event slug.

Usage:
    python -m bot.debug_fetch_slug btc-updown-15m-1765407600
    python -m bot.debug_fetch_slug --latest 20
    python -m bot.debug_fetch_slug --scan-15m
"""
import sys
import json
import requests
from datetime import datetime, timezone, timedelta


def fetch_by_slug(slug: str) -> dict | None:
    """Fetch a single event by slug."""
    url = f"https://gamma-api.polymarket.com/events?slug={slug}"
    print(f"[FETCH] {url}")
    resp = requests.get(url, timeout=5)
    if resp.ok:
        events = resp.json()
        if events:
            return events[0]
    return None


def fetch_latest_events(limit: int = 20) -> list:
    """Fetch newest events (by id descending)."""
    url = f"https://gamma-api.polymarket.com/events?order=id&ascending=false&closed=false&limit={limit}"
    print(f"[FETCH] {url}")
    resp = requests.get(url, timeout=5)
    if resp.ok:
        return resp.json()
    return []


def scan_btc_updown_15m() -> list:
    """Scan for btc-updown-15m events in the next 2 hours."""
    results = []
    now = datetime.now(timezone.utc)
    
    for minutes_offset in range(-30, 150, 15):
        target = now + timedelta(minutes=minutes_offset)
        ts = int(target.timestamp())
        ts = (ts // 900) * 900  # Round to 15m boundary
        
        slug = f"btc-updown-15m-{ts}"
        event = fetch_by_slug(slug)
        if event:
            results.append(event)
            print(f"  [OK] Found: {slug}")
        else:
            print(f"  [MISS] Not found: {slug}")
    
    return results


def print_event_summary(event: dict):
    """Print a formatted summary of an event and its markets."""
    print("\n" + "=" * 70)
    print(f"Event: {event.get('title', 'N/A')}")
    print(f"Slug:  {event.get('slug', 'N/A')}")
    print(f"ID:    {event.get('id', 'N/A')}")
    print(f"Closed: {event.get('closed', 'N/A')}")
    
    markets = event.get("markets", [])
    print(f"\nMarkets ({len(markets)}):")
    
    for m in markets:
        slug = m.get("slug", m.get("question", "?"))
        prices = m.get("outcomePrices", "N/A")
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except:
                pass
        
        if isinstance(prices, list) and len(prices) >= 2:
            p1 = float(prices[0]) if prices[0] else 0
            p2 = float(prices[1]) if prices[1] else 0
            price_sum = p1 + p2
            price_str = f"[{p1:.3f} / {p2:.3f}] sum={price_sum:.3f}"
        else:
            price_str = str(prices)
        
        vol = m.get("volume") or m.get("volumeNum") or 0
        closed = "CLOSED" if m.get("closed") else "OPEN"
        
        print(f"  [{closed}] | {slug[:50]:50} | {price_str} | vol=${vol}")
    
    print("=" * 70)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    
    arg = sys.argv[1]
    
    if arg == "--latest":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        print(f"\n[MODE] Fetching {limit} newest open events...\n")
        events = fetch_latest_events(limit)
        
        btc_updown_events = [e for e in events if "btc-updown-15m" in e.get("slug", "")]
        
        print(f"\nTotal events: {len(events)}")
        print(f"btc-updown-15m events: {len(btc_updown_events)}")
        
        if btc_updown_events:
            print("\nBTC-UPDOWN-15M Events Found:")
            for e in btc_updown_events:
                print_event_summary(e)
        else:
            print("\nNo btc-updown-15m events in latest. Showing first 5 events:")
            for e in events[:5]:
                print_event_summary(e)
    
    elif arg == "--scan-15m":
        print("\n[MODE] Scanning btc-updown-15m slugs (±2 hours)...\n")
        events = scan_btc_updown_15m()
        print(f"\nFound {len(events)} btc-updown-15m events")
        for e in events:
            print_event_summary(e)
    
    else:
        # Treat as a slug
        slug = arg
        print(f"\n[MODE] Fetching event by slug: {slug}\n")
        event = fetch_by_slug(slug)
        if event:
            print_event_summary(event)
        else:
            print(f"Event not found: {slug}")
            print("\nTry --latest or --scan-15m to find active events")


if __name__ == "__main__":
    main()
