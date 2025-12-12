"""
Perps Exit Manager - Automated TP/SL/Time management for leveraged positions

Same concept as the Polymarket exit manager, but for perp positions:
- Take Profit: Close when unrealized PnL exceeds threshold
- Stop Loss: Close when loss exceeds threshold  
- Max Hold: Close positions held too long
- Nightly Flatten: Optional close all at specific hour

Works with the Avantis client to execute closes.
"""

import os
import sys
import time
import signal
import logging
import requests
from datetime import datetime, timezone
from typing import Optional

from .avantis_client import get_client, AvantisPosition


# ─────────────────────────────────────────────────────────────────────────────
# Configuration (from environment)
# ─────────────────────────────────────────────────────────────────────────────

SIDECAR_URL = os.getenv("SIDECAR_URL", "http://localhost:4000")

# Exit thresholds
PERPS_TAKE_PROFIT_PCT = float(os.getenv("PERPS_TAKE_PROFIT_PCT", "5.0"))
PERPS_STOP_LOSS_PCT = float(os.getenv("PERPS_STOP_LOSS_PCT", "-3.0"))
PERPS_MAX_HOLD_HOURS = float(os.getenv("PERPS_MAX_HOLD_HOURS", "24"))
PERPS_AUTO_FLATTEN_HOUR = int(os.getenv("PERPS_AUTO_FLATTEN_HOUR", "-1"))  # -1 = disabled

# Loop settings
PERPS_EXIT_LOOP_INTERVAL = int(os.getenv("PERPS_EXIT_LOOP_INTERVAL", "60"))
PERPS_EXIT_DRY_RUN = os.getenv("PERPS_EXIT_DRY_RUN", "true").lower() in ("true", "1", "yes")

# Exit tags
EXIT_TAG_TP = "TAKE_PROFIT"
EXIT_TAG_SL = "STOP_LOSS"
EXIT_TAG_MAX_HOLD = "MAX_HOLD"
EXIT_TAG_NIGHTLY = "NIGHTLY_FLATTEN"


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("perps_exit_manager")


# ─────────────────────────────────────────────────────────────────────────────
# Graceful shutdown
# ─────────────────────────────────────────────────────────────────────────────

shutdown_requested = False


def handle_shutdown(signum, frame):
    global shutdown_requested
    logger.info("Shutdown requested...")
    shutdown_requested = True


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


# ─────────────────────────────────────────────────────────────────────────────
# Exit Manager Logic
# ─────────────────────────────────────────────────────────────────────────────

def get_open_perp_positions() -> list[dict]:
    """Fetch open perp positions from sidecar"""
    try:
        resp = requests.get(f"{SIDECAR_URL}/perps/positions", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("positions", [])
    except Exception as e:
        logger.error(f"Error fetching perp positions: {e}")
        return []


def calculate_pnl_pct(position: dict) -> float:
    """Calculate PnL percentage for a position"""
    entry = position.get("entry_price", 0)
    current = position.get("current_price", 0)
    side = position.get("side", "LONG")
    leverage = position.get("leverage", 1)
    
    if entry <= 0 or current <= 0:
        return 0.0
    
    if side == "LONG":
        pnl_pct = ((current - entry) / entry) * 100 * leverage
    else:
        pnl_pct = ((entry - current) / entry) * 100 * leverage
    
    return pnl_pct


def calculate_hold_hours(position: dict) -> float:
    """Calculate hours since position was opened"""
    opened_at = position.get("opened_at")
    if not opened_at:
        return 0.0
    
    try:
        opened = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - opened
        return delta.total_seconds() / 3600
    except Exception:
        return 0.0


def should_exit_position(position: dict) -> tuple[bool, str, str]:
    """
    Determine if a position should be exited.
    
    Returns: (should_exit, exit_tag, reason)
    """
    pnl_pct = calculate_pnl_pct(position)
    hold_hours = calculate_hold_hours(position)
    asset = position.get("asset", "?")
    
    # Check for position-specific overrides (from DB)
    tp_threshold = position.get("tp_pct_override") or PERPS_TAKE_PROFIT_PCT
    sl_threshold = position.get("sl_pct_override") or PERPS_STOP_LOSS_PCT
    max_hold = position.get("max_hold_override") or PERPS_MAX_HOLD_HOURS
    
    # Take Profit
    if pnl_pct >= tp_threshold:
        return True, EXIT_TAG_TP, f"{asset}: PnL {pnl_pct:+.2f}% >= TP {tp_threshold}%"
    
    # Stop Loss
    if pnl_pct <= sl_threshold:
        return True, EXIT_TAG_SL, f"{asset}: PnL {pnl_pct:+.2f}% <= SL {sl_threshold}%"
    
    # Max Hold Time
    if hold_hours >= max_hold:
        return True, EXIT_TAG_MAX_HOLD, f"{asset}: Held {hold_hours:.1f}h >= max {max_hold}h"
    
    # Nightly Flatten
    if PERPS_AUTO_FLATTEN_HOUR >= 0:
        now_utc = datetime.now(timezone.utc)
        if now_utc.hour == PERPS_AUTO_FLATTEN_HOUR:
            return True, EXIT_TAG_NIGHTLY, f"{asset}: Nightly flatten at {PERPS_AUTO_FLATTEN_HOUR}:00 UTC"
    
    return False, "", ""


def close_position(position: dict, exit_tag: str, reason: str, dry_run: bool = True) -> bool:
    """Close a position via Avantis client"""
    asset = position.get("asset", "?")
    order_id = position.get("order_id")
    size_usd = position.get("size_usd", 0)
    pnl_pct = calculate_pnl_pct(position)
    
    if dry_run:
        logger.info(f"[DRY RUN] Would close {asset} ({exit_tag}): {reason}")
        return True
    
    try:
        client = get_client(dry_run=False)
        result = client.close_position(asset)
        
        if result.success:
            logger.info(f"✅ Closed {asset} ({exit_tag}): {reason}")
            
            # Log to sidecar
            try:
                requests.post(
                    f"{SIDECAR_URL}/telemetry/perp-trade-close",
                    json={
                        "order_id": order_id,
                        "realized_pnl": size_usd * (pnl_pct / 100),
                    },
                    timeout=5,
                )
                requests.post(
                    f"{SIDECAR_URL}/telemetry",
                    json={
                        "type": "perp_exit",
                        "asset": asset,
                        "exit_tag": exit_tag,
                        "reason": reason,
                        "pnl_pct": pnl_pct,
                    },
                    timeout=5,
                )
            except Exception:
                pass
            
            return True
        else:
            logger.error(f"❌ Failed to close {asset}: {result.error}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Error closing {asset}: {e}")
        return False


def run_exit_check(dry_run: bool = True) -> dict:
    """
    Run one cycle of exit checks.
    
    Returns stats dict.
    """
    stats = {
        "checked": 0,
        "exited": 0,
        "failed": 0,
        "held": 0,
    }
    
    positions = get_open_perp_positions()
    stats["checked"] = len(positions)
    
    for pos in positions:
        should_exit, exit_tag, reason = should_exit_position(pos)
        
        if should_exit:
            success = close_position(pos, exit_tag, reason, dry_run=dry_run)
            if success:
                stats["exited"] += 1
            else:
                stats["failed"] += 1
        else:
            stats["held"] += 1
            asset = pos.get("asset", "?")
            pnl_pct = calculate_pnl_pct(pos)
            hold_hours = calculate_hold_hours(pos)
            logger.debug(f"  {asset}: PnL {pnl_pct:+.2f}%, held {hold_hours:.1f}h - HOLD")
    
    return stats


def run_exit_loop(dry_run: bool = True, interval: int = 60):
    """Main exit manager loop"""
    logger.info("=" * 50)
    logger.info("Perps Exit Manager started")
    logger.info(f"  DRY-RUN mode: {dry_run}")
    logger.info(f"  TP threshold: +{PERPS_TAKE_PROFIT_PCT}%")
    logger.info(f"  SL threshold: {PERPS_STOP_LOSS_PCT}%")
    logger.info(f"  Max hold: {PERPS_MAX_HOLD_HOURS}h")
    logger.info(f"  Auto-flatten hour: {PERPS_AUTO_FLATTEN_HOUR if PERPS_AUTO_FLATTEN_HOUR >= 0 else 'Disabled'}")
    logger.info(f"  Check interval: {interval}s")
    logger.info("=" * 50)
    
    while not shutdown_requested:
        try:
            stats = run_exit_check(dry_run=dry_run)
            
            if stats["checked"] > 0:
                logger.info(f"Checked {stats['checked']} | Exited {stats['exited']} | Held {stats['held']}")
            else:
                logger.debug("No open perp positions")
                
        except Exception as e:
            logger.error(f"Exit check error: {e}")
        
        # Sleep with shutdown check
        for _ in range(interval):
            if shutdown_requested:
                break
            time.sleep(1)
    
    logger.info("Perps Exit Manager stopped")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Perps Exit Manager")
    parser.add_argument("--live", action="store_true", help="Run in live mode")
    parser.add_argument("--interval", type=int, default=PERPS_EXIT_LOOP_INTERVAL)
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--verbose", "-v", action="store_true")
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    dry_run = not args.live
    
    if args.once:
        stats = run_exit_check(dry_run=dry_run)
        print(f"Stats: {stats}")
    else:
        run_exit_loop(dry_run=dry_run, interval=args.interval)


if __name__ == "__main__":
    main()
