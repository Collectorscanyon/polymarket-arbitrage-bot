"""
Perps Signal Loop - Continuous Bankr Oracle Scanner

This runs as a background process, periodically:
1. Scans tracked assets
2. Asks Bankr for trade decisions
3. Logs all decisions to the dashboard
4. Optionally executes trades (when not in DRY_RUN)

Usage:
    python -m perps.signal_loop                    # DRY_RUN mode
    python -m perps.signal_loop --live             # Live execution
    python -m perps.signal_loop --interval 300     # 5 minute intervals
"""

import os
import sys
import time
import signal
import argparse
from datetime import datetime
from typing import Optional

from .perps_signaler import (
    build_market_context,
    ask_bankr,
    log_signal_to_sidecar,
    TRACKED_ASSETS,
    MIN_CONFIDENCE,
)
from .perps_executor import execute_decision, ExecutionResult
from .avantis_client import get_client


# Configuration
SIGNAL_LOOP_INTERVAL = int(os.getenv("PERPS_SIGNAL_INTERVAL", "300"))  # 5 min default
SIGNAL_LOOP_DRY_RUN = os.getenv("PERPS_DRY_RUN", "true").lower() in ("true", "1", "yes")

# Graceful shutdown
shutdown_requested = False


def handle_shutdown(signum, frame):
    global shutdown_requested
    print("\n[SignalLoop] Shutdown requested, finishing current cycle...")
    shutdown_requested = True


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


def log_decision(asset: str, decision, executed: bool = False, result: Optional[ExecutionResult] = None):
    """Pretty-print a decision to console"""
    ts = datetime.now().strftime("%H:%M:%S")
    
    if not decision.parse_success:
        print(f"  [{ts}] {asset}: ❌ Parse error: {decision.parse_error[:50]}")
        return
    
    emoji = {"LONG": "🟢", "SHORT": "🔴", "NO_TRADE": "⚪"}.get(decision.decision, "❓")
    conf = f"{decision.confidence*100:.0f}%" if decision.confidence else "?"
    
    print(f"  [{ts}] {asset}: {emoji} {decision.decision} @ {conf} confidence")
    
    if decision.decision in ("LONG", "SHORT"):
        print(f"         Entry: {decision.entry_zone.min_price} - {decision.entry_zone.max_price}")
        print(f"         TP: {decision.take_profit.target_price} | SL: {decision.stop_loss.price}")
        print(f"         Size: ${decision.size.notional_usd:.2f} @ {decision.max_leverage}x")
        if decision.reason:
            print(f"         Reason: {decision.reason[:80]}...")
        
        if executed and result:
            if result.success:
                print(f"         → ✅ EXECUTED (order: {result.trade_id})")
            elif result.guardrail_blocked:
                print(f"         → ⛔ BLOCKED: {result.guardrail_reason}")
            else:
                print(f"         → ❌ FAILED: {result.error}")


def run_signal_cycle(
    assets: list[str],
    timeframe: str,
    dry_run: bool,
    execute: bool = True,
) -> dict:
    """
    Run one cycle of signal scanning.
    
    Returns stats dict with counts.
    """
    stats = {
        "scanned": 0,
        "signals": 0,
        "executed": 0,
        "blocked": 0,
        "no_trade": 0,
        "errors": 0,
    }
    
    client = get_client(dry_run=dry_run)
    
    for asset in assets:
        asset = asset.strip().upper()
        stats["scanned"] += 1
        
        # Build context
        context = build_market_context(asset, client, timeframe)
        if not context:
            stats["errors"] += 1
            continue
        
        # Ask Bankr
        decision = ask_bankr(context, dry_run=dry_run)
        
        # Log to sidecar
        log_signal_to_sidecar(asset, decision)
        
        # Handle result
        if not decision.parse_success:
            stats["errors"] += 1
            log_decision(asset, decision)
            continue
        
        if decision.decision == "NO_TRADE":
            stats["no_trade"] += 1
            log_decision(asset, decision)
            continue
        
        # Actionable signal
        if decision.confidence >= MIN_CONFIDENCE:
            stats["signals"] += 1
            
            if execute:
                result = execute_decision(asset, decision, context, dry_run=dry_run)
                if result.success:
                    stats["executed"] += 1
                elif result.guardrail_blocked:
                    stats["blocked"] += 1
                log_decision(asset, decision, executed=True, result=result)
            else:
                log_decision(asset, decision)
        else:
            stats["no_trade"] += 1
            log_decision(asset, decision)
    
    return stats


def run_loop(
    assets: list[str],
    timeframe: str = "scalp_1h",
    interval: int = 300,
    dry_run: bool = True,
    execute: bool = True,
):
    """
    Main signal loop - runs continuously until shutdown.
    """
    print("=" * 60)
    print("  PERPS SIGNAL LOOP - Bankr Oracle Mode")
    print("=" * 60)
    print(f"  Assets:    {', '.join(assets)}")
    print(f"  Timeframe: {timeframe}")
    print(f"  Interval:  {interval}s")
    print(f"  Mode:      {'🔴 LIVE' if not dry_run else '🟡 DRY RUN'}")
    print(f"  Execute:   {'Yes' if execute else 'Signal Only'}")
    print("=" * 60)
    print()
    
    cycle = 0
    total_stats = {
        "scanned": 0,
        "signals": 0,
        "executed": 0,
        "blocked": 0,
        "no_trade": 0,
        "errors": 0,
    }
    
    while not shutdown_requested:
        cycle += 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[Cycle {cycle}] {now}")
        print("-" * 40)
        
        try:
            stats = run_signal_cycle(
                assets=assets,
                timeframe=timeframe,
                dry_run=dry_run,
                execute=execute,
            )
            
            # Accumulate stats
            for k in total_stats:
                total_stats[k] += stats.get(k, 0)
            
            print("-" * 40)
            print(f"  Scanned: {stats['scanned']} | Signals: {stats['signals']} | "
                  f"Executed: {stats['executed']} | Blocked: {stats['blocked']}")
            
        except Exception as e:
            print(f"  ❌ Cycle error: {e}")
            total_stats["errors"] += 1
        
        if not shutdown_requested:
            print(f"\n  Next scan in {interval}s...")
            # Sleep in small increments to allow graceful shutdown
            for _ in range(interval):
                if shutdown_requested:
                    break
                time.sleep(1)
    
    # Final summary
    print("\n" + "=" * 60)
    print("  SIGNAL LOOP STOPPED")
    print("=" * 60)
    print(f"  Total Cycles: {cycle}")
    print(f"  Total Scanned: {total_stats['scanned']}")
    print(f"  Total Signals: {total_stats['signals']}")
    print(f"  Total Executed: {total_stats['executed']}")
    print(f"  Total Blocked: {total_stats['blocked']}")
    print(f"  Total Errors: {total_stats['errors']}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Perps Signal Loop - Continuous Bankr Oracle Scanner"
    )
    parser.add_argument(
        "--assets", "-a",
        default=",".join(TRACKED_ASSETS),
        help="Comma-separated assets to scan"
    )
    parser.add_argument(
        "--timeframe", "-t",
        default="scalp_1h",
        choices=["scalp_1h", "swing_4h", "position_1d"],
        help="Trading timeframe"
    )
    parser.add_argument(
        "--interval", "-i",
        type=int,
        default=SIGNAL_LOOP_INTERVAL,
        help="Seconds between scans"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run in live mode (default: dry run)"
    )
    parser.add_argument(
        "--signal-only",
        action="store_true",
        help="Only log signals, don't execute"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit (no loop)"
    )
    
    args = parser.parse_args()
    
    assets = [a.strip() for a in args.assets.split(",")]
    dry_run = not args.live
    execute = not args.signal_only
    
    if args.once:
        # Single run mode
        print(f"[SignalLoop] Single scan mode - {'LIVE' if not dry_run else 'DRY RUN'}")
        stats = run_signal_cycle(
            assets=assets,
            timeframe=args.timeframe,
            dry_run=dry_run,
            execute=execute,
        )
        print(f"\nStats: {stats}")
    else:
        # Continuous loop
        run_loop(
            assets=assets,
            timeframe=args.timeframe,
            interval=args.interval,
            dry_run=dry_run,
            execute=execute,
        )


if __name__ == "__main__":
    main()
