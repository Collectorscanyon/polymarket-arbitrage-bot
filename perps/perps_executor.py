"""
Perps Executor - Execute Bankr's trading decisions on Avantis

This module:
1. Takes BankrPerpDecision from the signaler
2. Validates against risk guardrails
3. Executes on Avantis
4. Logs to SQLite + dashboard
5. Hands off to exit manager
"""

import os
import time
import requests
from typing import Optional
from dataclasses import dataclass

from .schemas import BankrPerpDecision, PerpMarketContext
from .avantis_client import get_client, AvantisClient, AvantisOrder, OrderResult


# Sidecar URL for telemetry
SIDECAR_URL = os.getenv("SIDECAR_URL", "http://localhost:4000")

# Risk guardrails (defense in depth - even if Bankr suggests more)
MAX_LEVERAGE_HARD_CAP = float(os.getenv("PERPS_MAX_LEVERAGE_HARD", "5"))
MAX_POSITION_USD_HARD_CAP = float(os.getenv("PERPS_MAX_POSITION_USD", "5000"))
MAX_RISK_PCT_HARD_CAP = float(os.getenv("PERPS_MAX_RISK_PCT_HARD", "2.0"))
MIN_CONFIDENCE_TO_EXECUTE = float(os.getenv("PERPS_MIN_CONFIDENCE_EXECUTE", "0.65"))

# Execution settings
PERPS_DRY_RUN = os.getenv("PERPS_DRY_RUN", "true").lower() in ("true", "1", "yes")


@dataclass
class ExecutionResult:
    """Result of executing a trade"""
    success: bool
    trade_id: Optional[str] = None
    position_id: Optional[str] = None
    fill_price: Optional[float] = None
    size_usd: Optional[float] = None
    leverage: Optional[float] = None
    side: Optional[str] = None
    error: Optional[str] = None
    guardrail_blocked: bool = False
    guardrail_reason: Optional[str] = None


def validate_decision(
    decision: BankrPerpDecision,
    context: PerpMarketContext,
) -> tuple[bool, str]:
    """
    Validate a trading decision against hard guardrails.
    
    Returns (is_valid, reason)
    """
    # Check parse success
    if not decision.parse_success:
        return False, f"Parse failed: {decision.parse_error}"
    
    # Check decision type
    if decision.decision not in ("LONG", "SHORT"):
        return False, f"Non-actionable decision: {decision.decision}"
    
    # Check confidence
    if decision.confidence < MIN_CONFIDENCE_TO_EXECUTE:
        return False, f"Confidence {decision.confidence:.0%} < min {MIN_CONFIDENCE_TO_EXECUTE:.0%}"
    
    # Check leverage cap
    if decision.max_leverage > MAX_LEVERAGE_HARD_CAP:
        return False, f"Leverage {decision.max_leverage}x > hard cap {MAX_LEVERAGE_HARD_CAP}x"
    
    # Check position size cap
    if decision.size.notional_usd > MAX_POSITION_USD_HARD_CAP:
        return False, f"Size ${decision.size.notional_usd:.2f} > hard cap ${MAX_POSITION_USD_HARD_CAP:.2f}"
    
    # Check risk percentage
    if decision.stop_loss.risk_pct_equity > MAX_RISK_PCT_HARD_CAP:
        return False, f"Risk {decision.stop_loss.risk_pct_equity:.1f}% > hard cap {MAX_RISK_PCT_HARD_CAP:.1f}%"
    
    # Check we have valid SL (required)
    if decision.stop_loss.price <= 0:
        return False, "No valid stop loss price"
    
    # Check entry makes sense vs SL
    if decision.decision == "LONG":
        if decision.stop_loss.price >= decision.entry_zone.max_price:
            return False, "SL >= entry for LONG"
    else:  # SHORT
        if decision.stop_loss.price <= decision.entry_zone.min_price:
            return False, "SL <= entry for SHORT"
    
    return True, "Valid"


def execute_decision(
    asset: str,
    decision: BankrPerpDecision,
    context: PerpMarketContext,
    dry_run: bool = None,
) -> ExecutionResult:
    """
    Execute a validated trading decision on Avantis.
    
    Args:
        asset: The asset to trade
        decision: Bankr's trading decision
        context: The market context that was analyzed
        dry_run: Override dry run setting (default: use env)
    
    Returns:
        ExecutionResult with trade details or error
    """
    if dry_run is None:
        dry_run = PERPS_DRY_RUN
    
    # Validate first
    is_valid, reason = validate_decision(decision, context)
    if not is_valid:
        print(f"[PerpsExecutor] Guardrail blocked: {reason}")
        return ExecutionResult(
            success=False,
            guardrail_blocked=True,
            guardrail_reason=reason,
        )
    
    # Get client
    client = get_client(dry_run=dry_run)
    
    # Check for existing position (might want to add to it or skip)
    existing = client.get_position(asset)
    if existing:
        print(f"[PerpsExecutor] Already have {existing.side} position in {asset}")
        # For now, skip if already have position (could implement scaling logic)
        return ExecutionResult(
            success=False,
            error=f"Already have {existing.side} position",
        )
    
    # Build the order
    order = AvantisOrder(
        asset=asset,
        side=decision.decision,
        size_usd=decision.size.notional_usd,
        leverage=decision.max_leverage,
        order_type=decision.entry_zone.type.upper(),
        limit_price=decision.entry_zone.max_price if decision.entry_zone.type == "limit" else None,
        tp_price=decision.take_profit.target_price,
        sl_price=decision.stop_loss.price,
    )
    
    # Execute
    print(f"[PerpsExecutor] {'DRY RUN: ' if dry_run else ''}Executing {decision.decision} {asset}")
    print(f"  Size: ${order.size_usd:.2f} @ {order.leverage}x")
    print(f"  TP: {order.tp_price} | SL: {order.sl_price}")
    
    result = client.place_order(order)
    
    if result.success:
        # Log to sidecar
        log_trade_to_sidecar(
            asset=asset,
            decision=decision,
            result=result,
            dry_run=dry_run,
        )
        
        return ExecutionResult(
            success=True,
            trade_id=result.order_id,
            position_id=result.position_id,
            fill_price=result.fill_price,
            size_usd=order.size_usd,
            leverage=order.leverage,
            side=decision.decision,
        )
    else:
        return ExecutionResult(
            success=False,
            error=result.error,
        )


def log_trade_to_sidecar(
    asset: str,
    decision: BankrPerpDecision,
    result: OrderResult,
    dry_run: bool,
):
    """Log executed trade to sidecar for dashboard + tracking"""
    try:
        # Log to activity
        requests.post(
            f"{SIDECAR_URL}/telemetry",
            json={
                "type": "perp_trade_executed",
                "asset": asset,
                "side": decision.decision,
                "size_usd": decision.size.notional_usd,
                "leverage": decision.max_leverage,
                "entry_price": result.fill_price,
                "tp_price": decision.take_profit.target_price,
                "sl_price": decision.stop_loss.price,
                "confidence": decision.confidence,
                "reason": decision.reason[:200] if decision.reason else "",
                "order_id": result.order_id,
                "position_id": result.position_id,
                "dry_run": dry_run,
            },
            timeout=5,
        )
        
        # Also log to perp trades table (if endpoint exists)
        requests.post(
            f"{SIDECAR_URL}/telemetry/perp-trade-open",
            json={
                "order_id": result.order_id,
                "position_id": result.position_id,
                "asset": asset,
                "side": decision.decision,
                "size_usd": decision.size.notional_usd,
                "leverage": decision.max_leverage,
                "entry_price": result.fill_price,
                "tp_price": decision.take_profit.target_price,
                "sl_price": decision.stop_loss.price,
                "time_horizon_hours": decision.time_horizon_hours,
                "bankr_confidence": decision.confidence,
                "bankr_reason": decision.reason,
            },
            timeout=5,
        )
        
    except Exception as e:
        print(f"[PerpsExecutor] Failed to log trade: {e}")


def execute_all_signals(
    signals: list[tuple[str, PerpMarketContext, BankrPerpDecision]],
    dry_run: bool = None,
) -> list[tuple[str, ExecutionResult]]:
    """
    Execute all actionable signals from the signaler.
    
    Args:
        signals: List of (asset, context, decision) tuples from scan_opportunities
        dry_run: Override dry run setting
    
    Returns:
        List of (asset, ExecutionResult) tuples
    """
    results = []
    
    for asset, context, decision in signals:
        result = execute_decision(asset, decision, context, dry_run=dry_run)
        results.append((asset, result))
        
        if result.success:
            print(f"[PerpsExecutor] ✓ {asset}: Executed {result.side} ${result.size_usd:.2f}")
        elif result.guardrail_blocked:
            print(f"[PerpsExecutor] ⊘ {asset}: Blocked - {result.guardrail_reason}")
        else:
            print(f"[PerpsExecutor] ✗ {asset}: Failed - {result.error}")
        
        # Small delay between orders
        time.sleep(0.5)
    
    return results


if __name__ == "__main__":
    import argparse
    from .perps_signaler import scan_opportunities, TRACKED_ASSETS
    
    parser = argparse.ArgumentParser(description="Perps Executor - Execute Bankr signals")
    parser.add_argument("--assets", "-a", default=",".join(TRACKED_ASSETS), help="Comma-separated assets")
    parser.add_argument("--timeframe", "-t", default="scalp_1h")
    parser.add_argument("--live", action="store_true", help="Run in live mode (default: dry run)")
    args = parser.parse_args()
    
    assets = [a.strip() for a in args.assets.split(",")]
    dry_run = not args.live
    
    print(f"[PerpsExecutor] Mode: {'DRY RUN' if dry_run else '🔴 LIVE 🔴'}")
    print()
    
    # Get signals
    print("=== Scanning for opportunities ===")
    signals = scan_opportunities(assets=assets, timeframe=args.timeframe, dry_run=dry_run)
    
    if not signals:
        print("\nNo actionable signals found.")
        exit(0)
    
    print(f"\n=== Executing {len(signals)} signals ===")
    results = execute_all_signals(signals, dry_run=dry_run)
    
    # Summary
    successful = sum(1 for _, r in results if r.success)
    blocked = sum(1 for _, r in results if r.guardrail_blocked)
    failed = sum(1 for _, r in results if not r.success and not r.guardrail_blocked)
    
    print(f"\n=== Summary ===")
    print(f"  ✓ Executed: {successful}")
    print(f"  ⊘ Blocked: {blocked}")
    print(f"  ✗ Failed: {failed}")
