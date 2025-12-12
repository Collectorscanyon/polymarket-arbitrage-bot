#!/usr/bin/env python3
"""
BTC15 Optimized Scanner - Debug/Test CLI

Run this to test the new optimized scanning infrastructure:
- Active set cache
- CLOB orderbook fetching
- Fillability checks
- Metrics logging

Usage:
    python -m bot.strategies.run_btc15_scan
    python -m bot.strategies.run_btc15_scan --ticks 5
"""

import argparse
import logging
import time
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from bot.strategies.btc15_cache import get_btc15_cache
from bot.strategies.btc15_clob import get_clob_fetcher
from bot.strategies.btc15_metrics import get_loop_metrics
from bot.strategies.btc15_scanner import get_btc15_scanner, run_btc15_scan


def main():
    parser = argparse.ArgumentParser(description="Run BTC15 optimized scanner")
    parser.add_argument("--ticks", type=int, default=3, help="Number of scan ticks to run")
    parser.add_argument("--interval", type=float, default=10.0, help="Seconds between ticks")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    
    print("=" * 60)
    print("BTC15 OPTIMIZED SCANNER - TEST RUN")
    print("=" * 60)
    print()
    
    # Initialize components
    cache = get_btc15_cache()
    clob = get_clob_fetcher()
    metrics = get_loop_metrics()
    scanner = get_btc15_scanner()
    
    print("[1] Refreshing active set cache...")
    new_markets = cache.refresh(limit=100)
    print(f"    Found {new_markets} new markets")
    print(f"    Total cached: {len(cache.active_markets)}")
    print(f"    Tradeable (2-14 min): {len(cache.tradeable_markets)}")
    print(f"    Upcoming (14-30 min): {len(cache.upcoming_markets)}")
    print()
    
    # Show active markets
    tradeable = cache.tradeable_markets
    upcoming = cache.upcoming_markets
    
    if tradeable:
        print("[2] TRADEABLE BTC15 Markets (2-14 min to expiry):")
        for slug, info in list(tradeable.items())[:5]:
            print(f"    - {slug}")
            print(f"      Question: {info.question}")
            print(f"      Expires in: {info.minutes_to_expiry:.1f} min")
            print(f"      Token IDs: {info.token_ids}")
            print()
    elif upcoming:
        print("[2] No tradeable markets right now")
        print("    UPCOMING markets (14-30 min):")
        for slug, info in list(upcoming.items())[:3]:
            print(f"    - {slug}: {info.minutes_to_expiry:.1f} min to expiry")
        print()
    else:
        print("[2] No active BTC15 markets found")
        print("    (btc-updown-15m markets may not be live right now)")
        if cache.active_markets:
            print(f"    Next market in: {min(m.minutes_to_expiry for m in cache.active_markets.values()):.0f} min")
        print()
    
    # Test CLOB fetch if we have tradeable or upcoming markets
    test_market = None
    if tradeable:
        test_market = list(tradeable.values())[0]
    elif upcoming:
        test_market = list(upcoming.values())[0]
    elif cache.active_markets:
        test_market = list(cache.active_markets.values())[0]
    
    if test_market and len(test_market.token_ids) >= 2:
        print("[3] Testing CLOB orderbook fetch...")
        bracket = clob.fetch_bracket(
            test_market.token_ids[0],
            test_market.token_ids[1],
        )
        if bracket:
            print(f"    Market: {test_market.slug}")
            print(f"    UP  ask: ${bracket.up_ask:.3f} (spread: {bracket.up_book.spread:.3f})")
            print(f"    DOWN ask: ${bracket.down_ask:.3f} (spread: {bracket.down_book.spread:.3f})")
            print(f"    Sum of asks: ${bracket.sum_asks:.3f}")
            print(f"    Edge: {bracket.edge_cents:.1f} cents")
            print(f"    Fetch time: {bracket.fetch_time_ms:.0f}ms")
            
            # Check fillability
            is_fillable, reason = bracket.is_fillable_arb(
                target_shares=10,
                min_edge_cents=1.0,
                min_depth_usdc=10,
            )
            print(f"    Fillable: {is_fillable} - {reason}")
        else:
            print("    Failed to fetch orderbook (may not have liquidity yet)")
        print()
    else:
        print("[3] No markets with token IDs to test CLOB fetch")
        print()
    
    # Run scan ticks
    print(f"[4] Running {args.ticks} scan ticks (interval: {args.interval}s)...")
    print()
    
    for i in range(args.ticks):
        print(f"--- Tick {i+1}/{args.ticks} ---")
        result = run_btc15_scan()
        print(f"    Markets scanned: {result.markets_scanned}")
        print(f"    Opportunities: {result.opportunities_found} found, {result.opportunities_actioned} actioned")
        print(f"    Best edge: {result.best_edge_cents:.1f} cents")
        if result.actions_taken:
            for action in result.actions_taken:
                print(f"    ACTION: {action}")
        print()
        
        if i < args.ticks - 1:
            time.sleep(args.interval)
    
    # Print summary
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print()
    
    cache_stats = cache.get_stats()
    print("Cache Stats:")
    print(f"  Active markets: {cache_stats['active_count']}")
    print(f"  Total refreshes: {cache_stats['refresh_count']}")
    print(f"  New slugs found: {cache_stats['new_slugs_found']}")
    print()
    
    print(f"CLOB Requests: {clob.request_count}")
    print()
    
    summary = metrics.get_summary(window_minutes=60)
    if "error" not in summary:
        print("Metrics (last 60min):")
        print(f"  Ticks: {summary['ticks']}")
        print(f"  Avg tick duration: {summary['avg_tick_ms']:.0f}ms")
        print(f"  Opportunities: {summary['opportunities_seen']} seen, {summary['opportunities_actioned']} actioned")
        if summary['opportunities_seen'] > 0:
            print(f"  Action rate: {summary['action_rate']*100:.1f}%")
        print(f"  Requests/min: Gamma={summary['requests_per_min']['gamma']:.1f}, CLOB={summary['requests_per_min']['clob']:.1f}")


if __name__ == "__main__":
    main()
