"""
Debug script to verify which Polymarket markets match the BTC 15m criteria.

Usage:
    cd polymarket-arbitrage-bot
    python -m bot.debug_btc15_markets
"""

import sys
import os
from datetime import datetime, timezone

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import BTC15_CONFIG
from utils import MarketsDataParser, MultiMarketsDataParser
from bot.strategies.btc15_loop import BTC15Loop


def get_minutes_to_expiry(market: dict) -> float:
    """Calculate minutes until market expiration."""
    end_date_str = market.get("endDate") or market.get("endDateIso") or market.get("expirationTime")
    if not end_date_str:
        return 999.0
    
    try:
        # Handle various date formats
        if end_date_str.endswith("Z"):
            end_date_str = end_date_str[:-1] + "+00:00"
        if "." in end_date_str:
            # Truncate microseconds if present
            parts = end_date_str.split(".")
            end_date_str = parts[0] + "+00:00" if "+" not in parts[1] else parts[0] + "+" + parts[1].split("+")[1]
        
        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = (end_dt - now).total_seconds() / 60.0
        return max(0, delta)
    except Exception as e:
        return 999.0


def is_btc_candidate(market: dict) -> bool:
    """Check if market might be BTC-related based on slug or question."""
    slug = str(market.get("slug", "")).lower()
    question = str(market.get("question", "")).lower()
    
    btc_keywords = ["btc", "bitcoin"]
    pattern_keywords = ["up or down", "up-or-down", "updown", "15m", "15-min", "15 min"]
    
    # Check for BTC keywords
    has_btc = any(kw in slug or kw in question for kw in btc_keywords)
    # Check for pattern keywords  
    has_pattern = any(kw in slug or kw in question for kw in pattern_keywords)
    
    # Strong match: btc-updown-15m slug pattern
    if "btc-updown-15m" in slug:
        return True
    
    return has_btc or has_pattern


def fetch_btc_updown_events():
    """
    Specifically fetch btc-updown-15m events which may not appear in standard queries.
    These are short-lived intraday markets.
    """
    import requests
    from datetime import datetime, timezone, timedelta
    
    results = []
    
    # Try fetching recent btc-updown events by timestamp pattern
    # The slug format is btc-updown-15m-{unix_timestamp}
    now = datetime.now(timezone.utc)
    
    # Check for events in the next 2 hours (every 15 min = 8 potential events)
    for minutes_ahead in range(0, 120, 15):
        target_time = now + timedelta(minutes=minutes_ahead)
        timestamp = int(target_time.timestamp())
        # Round to nearest 15 min boundary
        timestamp = (timestamp // 900) * 900
        
        slug = f"btc-updown-15m-{timestamp}"
        try:
            resp = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=5)
            if resp.ok:
                data = resp.json()
                for event in data:
                    for m in event.get("markets", []):
                        m["_source"] = "btc-updown-direct"
                        results.append(m)
        except Exception:
            pass
    
    return results


def main():
    print("=" * 100)
    print("  BTC 15m Market Matcher Debug")
    print("=" * 100)
    print(f"  BTC15_MARKET_SUBSTR: '{BTC15_CONFIG.market_substr}'")
    print(f"  MIN_VOLUME_USDC:     ${BTC15_CONFIG.min_volume_usdc:.0f}")
    print(f"  BTC15_ENABLED:       {BTC15_CONFIG.enabled}")
    print("=" * 100)
    print()

    # Create a temporary BTC15Loop instance for the _is_btc15_market check
    loop = BTC15Loop(BTC15_CONFIG)

    # Fetch markets (same as main.py)
    print("[1/3] Fetching from gamma-api.polymarket.com/markets...")
    single_markets_parser = MarketsDataParser("https://gamma-api.polymarket.com/markets")
    single_markets = single_markets_parser.get_markets() or []
    print(f"       → Got {len(single_markets)} markets")

    print("[2/3] Fetching from gamma-api.polymarket.com/events...")
    events_parser = MultiMarketsDataParser("https://gamma-api.polymarket.com/events")
    events = events_parser.get_events() or []
    event_markets = []
    for event in events:
        for m in event.get("markets", []):
            event_markets.append(m)
    print(f"       → Got {len(event_markets)} markets from events")

    # Special: Try to fetch btc-updown-15m events directly (they may not appear in standard queries)
    print("[2.5] Fetching btc-updown-15m events directly...")
    btc_updown_markets = fetch_btc_updown_events()
    print(f"       → Got {len(btc_updown_markets)} btc-updown-15m markets")

    # Combine and dedupe by slug
    all_markets = {}
    for m in single_markets + event_markets + btc_updown_markets:
        slug = m.get("slug") or m.get("id") or str(id(m))
        all_markets[slug] = m
    
    print(f"[3/3] Combined unique markets: {len(all_markets)}")
    print()

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 1: BTC/Bitcoin/Up-or-Down candidates
    # ═══════════════════════════════════════════════════════════════════════════
    print("=" * 100)
    print("  BTC / BITCOIN / UP-OR-DOWN CANDIDATES (potential matches)")
    print("=" * 100)
    
    btc_candidates = []
    for slug, market in all_markets.items():
        if is_btc_candidate(market):
            volume = float(market.get("volume", 0) or market.get("volumeNum", 0) or 0)
            minutes_to_expiry = get_minutes_to_expiry(market)
            btc_candidates.append({
                "slug": slug,
                "question": str(market.get("question", ""))[:80],
                "volume": volume,
                "minutes_to_expiry": minutes_to_expiry,
                "endDate": market.get("endDate", ""),
            })
    
    if btc_candidates:
        # Sort by expiry (soonest first)
        btc_candidates.sort(key=lambda x: x["minutes_to_expiry"])
        
        print(f"\nFound {len(btc_candidates)} BTC-related candidates:\n")
        for c in btc_candidates:
            exp_str = f"{c['minutes_to_expiry']:.1f} min" if c['minutes_to_expiry'] < 999 else "N/A"
            print(f"  slug: {c['slug']}")
            print(f"  question: {c['question']}")
            print(f"  volume: ${c['volume']:,.0f}")
            print(f"  minutes_to_expiry: {exp_str}")
            print(f"  endDate: {c['endDate']}")
            print()
    else:
        print("\n  ❌ No BTC/Bitcoin/Up-or-Down candidates found.\n")

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 2: BTC15 Matching Results
    # ═══════════════════════════════════════════════════════════════════════════
    matching = []
    partial_matches = []

    for slug, market in all_markets.items():
        volume_usdc = float(market.get("volume", 0) or market.get("volumeNum", 0) or 0)
        minutes_to_expiry = get_minutes_to_expiry(market)
        
        # Add expiry info to market for _is_btc15_market
        market["minutes_to_expiry"] = minutes_to_expiry
        
        # Check if it matches via the actual _is_btc15_market method
        slug_lower = str(market.get("slug", "")).lower()
        label_lower = str(market.get("question", "")).lower()
        substr = BTC15_CONFIG.market_substr.lower()
        
        # Check for btc-updown-15m pattern OR substring match
        is_btc_updown = "btc-updown-15m" in slug_lower
        matches_substr = substr in slug_lower or substr in label_lower
        
        if is_btc_updown or matches_substr:
            full_match = loop._is_btc15_market(market, volume_usdc)
            
            entry = {
                "slug": slug[:60],
                "question": str(market.get("question", ""))[:60],
                "volume_usdc": volume_usdc,
                "minutes_to_expiry": minutes_to_expiry,
                "full_match": full_match,
            }
            
            if full_match:
                matching.append(entry)
            else:
                partial_matches.append(entry)

    print("=" * 100)
    print("  FULL MATCHES (all BTC15 criteria passed)")
    print("=" * 100)
    
    if matching:
        for m in sorted(matching, key=lambda x: x["minutes_to_expiry"]):
            exp_str = f"{m['minutes_to_expiry']:.1f} min" if m['minutes_to_expiry'] < 999 else "N/A"
            print(f"  ✅ {m['slug']}")
            print(f"     Q: {m['question']}")
            print(f"     Volume: ${m['volume_usdc']:,.0f} | Expiry: {exp_str}")
            print()
    else:
        print("  (No markets match all BTC15 criteria)")
    
    print()
    print("=" * 100)
    print("  PARTIAL MATCHES (substring match but failed volume/expiry)")
    print("=" * 100)
    
    if partial_matches:
        for m in sorted(partial_matches, key=lambda x: -x["volume_usdc"])[:15]:
            exp_str = f"{m['minutes_to_expiry']:.1f} min" if m['minutes_to_expiry'] < 999 else "N/A"
            vol_ok = "✓" if m['volume_usdc'] >= BTC15_CONFIG.min_volume_usdc else "✗"
            exp_ok = "✓" if 5 <= m['minutes_to_expiry'] <= 30 else "✗"
            print(f"  {m['slug']}")
            print(f"     Q: {m['question']}")
            print(f"     Volume: ${m['volume_usdc']:,.0f} {vol_ok} | Expiry: {exp_str} {exp_ok}")
            print()
    else:
        print("  (No partial matches)")

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 3: All markets dump (first 50)
    # ═══════════════════════════════════════════════════════════════════════════
    print()
    print("=" * 100)
    print("  ALL MARKETS (first 50 by volume)")
    print("=" * 100)
    
    all_sorted = []
    for slug, market in all_markets.items():
        volume = float(market.get("volume", 0) or market.get("volumeNum", 0) or 0)
        all_sorted.append({
            "slug": slug[:50],
            "question": str(market.get("question", ""))[:60],
            "volume": volume,
            "endDate": str(market.get("endDate", ""))[:25],
        })
    
    all_sorted.sort(key=lambda x: -x["volume"])
    
    for i, m in enumerate(all_sorted[:50], 1):
        print(f"{i:3}. {m['slug']}")
        print(f"     Q: {m['question']}")
        print(f"     Vol: ${m['volume']:,.0f} | End: {m['endDate']}")
        print()

    # ═══════════════════════════════════════════════════════════════════════════
    # SUMMARY
    # ═══════════════════════════════════════════════════════════════════════════
    print("=" * 100)
    print("  SUMMARY")
    print("=" * 100)
    print(f"  Total markets scanned:   {len(all_markets)}")
    print(f"  BTC-ish candidates:      {len(btc_candidates)}")
    print(f"  Full BTC15 matches:      {len(matching)}")
    print(f"  Partial matches:         {len(partial_matches)}")
    print()
    
    if btc_candidates and not matching:
        print("  💡 TIP: Found BTC candidates but no full matches.")
        print("     Check the candidate slugs above and update BTC15_MARKET_SUBSTR in .env")
        print(f"     Current: '{BTC15_CONFIG.market_substr}'")
        if btc_candidates:
            sample_slug = btc_candidates[0]["slug"]
            # Try to find common prefix
            if "-" in sample_slug:
                prefix = "-".join(sample_slug.split("-")[:4])  # First 4 parts
                print(f"     Suggested: '{prefix}'")
    
    if not btc_candidates:
        print("  💡 No BTC markets currently active. They may appear at specific times.")
        print("     BTC 15m markets often run on regular 15-minute intervals.")


if __name__ == "__main__":
    main()
