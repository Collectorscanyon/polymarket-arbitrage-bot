"""
Perps Signaler - Pulls market context and asks Bankr for trade decisions

This module:
1. Fetches market data from Avantis
2. Builds structured context (PerpMarketContext)
3. Sends to Bankr via sidecar with mode="perp_quant"
4. Parses the structured response
"""

import os
import json
import time
import requests
from typing import Optional

from .schemas import (
    PerpMarketContext,
    ExistingExposure,
    BankrPerpDecision,
)
from .avantis_client import get_client, AvantisClient


# Sidecar URL
SIDECAR_URL = os.getenv("SIDECAR_URL", "http://localhost:4000")

# Assets we're interested in trading
TRACKED_ASSETS = os.getenv("PERPS_TRACKED_ASSETS", "DEGEN,BNKR,ETH").split(",")

# Risk settings
MAX_LEVERAGE = float(os.getenv("PERPS_MAX_LEVERAGE", "3"))
MAX_RISK_PER_TRADE_PCT = float(os.getenv("PERPS_MAX_RISK_PCT", "1.0"))
MAX_CONCURRENT_POSITIONS = int(os.getenv("PERPS_MAX_POSITIONS", "5"))
MIN_CONFIDENCE = float(os.getenv("PERPS_MIN_CONFIDENCE", "0.6"))


def build_market_context(
    asset: str,
    client: AvantisClient,
    timeframe: str = "scalp_1h",
    technical_hints: dict = None,
) -> Optional[PerpMarketContext]:
    """
    Build a complete market context for Bankr to analyze.
    
    Args:
        asset: The asset symbol (e.g., "DEGEN")
        client: AvantisClient instance
        timeframe: Trading timeframe (scalp_1h, swing_4h, position_1d)
        technical_hints: Optional dict with support/resistance levels, liquidation hints
    
    Returns:
        PerpMarketContext or None if asset not found
    """
    market = client.get_market(asset)
    if not market:
        print(f"[PerpSignaler] Asset {asset} not found on Avantis")
        return None
    
    # Get account info
    equity = client.get_account_equity()
    exposure = client.get_net_exposure()
    positions = client.get_positions()
    
    # Build context
    ctx = PerpMarketContext(
        asset=asset,
        chain="Base",
        venue="Avantis",
        timeframe=timeframe,
        price=market.get("price", 0),
        change_24h_pct=market.get("change_24h_pct", 0),
        funding_8h=market.get("funding_rate_8h", 0),
        open_interest_usd=market.get("open_interest_usd", 0),
        volume_24h_usd=market.get("volume_24h_usd", 0),
        account_equity_usd=equity,
        max_leverage_allowed=min(MAX_LEVERAGE, market.get("max_leverage", 10)),
        max_risk_per_trade_pct=MAX_RISK_PER_TRADE_PCT,
        max_concurrent_positions=MAX_CONCURRENT_POSITIONS,
        existing_exposure=ExistingExposure(
            net_usd=exposure["net_usd"],
            direction=exposure["direction"],
        ),
    )
    
    # Add technical hints if provided
    if technical_hints:
        ctx.liquidation_heatmap_hint = technical_hints.get("liquidation_hint", "")
        ctx.support_levels = technical_hints.get("support_levels", [])
        ctx.resistance_levels = technical_hints.get("resistance_levels", [])
    
    return ctx


# The exact output schema we expect from Bankr
BANKR_OUTPUT_SCHEMA_STRICT = """{
  "decision": "LONG" | "SHORT" | "NO_TRADE",
  "confidence": <number 0.0-1.0>,
  "rationale": "<brief explanation, 1-2 sentences>",
  "entry": {
    "order_type": "limit" | "market",
    "entry_min": <number>,
    "entry_max": <number>
  },
  "stop_loss": {
    "price": <number>,
    "risk_pct_equity": <number>
  },
  "take_profit": {
    "price": <number>,
    "expected_rr": <number>
  },
  "size": {
    "notional_usd": <number>,
    "leverage": <number>
  },
  "time_horizon_hours": <number>
}"""


def build_bankr_prompt(context: PerpMarketContext, positions: list = None) -> str:
    """
    Build the complete prompt to send to Bankr in perp_quant mode.
    
    This is the "magic" - a structured prompt that makes Bankr an effective oracle.
    """
    # Format existing positions if any
    positions_str = "None"
    if positions:
        positions_str = json.dumps(positions, indent=2)
    
    prompt = f"""You are Bankr, my risk-aware leveraged quant for Base chain perpetual futures.

ROLE: You are the FINAL SAY oracle. Your job is to analyze market context and my risk constraints, then output a precise trading decision. If conviction is low, you MUST say NO_TRADE.

=== MARKET CONTEXT ===
Asset: {context.asset}
Venue: {context.venue} on {context.chain}
Timeframe: {context.timeframe}

Price Data:
- Current Price: ${context.price}
- 24h Change: {context.change_24h_pct:+.2f}%
- 8h Funding Rate: {context.funding_8h:.4f} ({context.funding_8h*100:.3f}%)
- Open Interest: ${context.open_interest_usd:,.0f}
- 24h Volume: ${context.volume_24h_usd:,.0f}

Technical Levels:
- Support: {context.support_levels if context.support_levels else 'Not provided'}
- Resistance: {context.resistance_levels if context.resistance_levels else 'Not provided'}
- Liquidation Hint: {context.liquidation_heatmap_hint or 'Not provided'}

=== MY RISK STATE ===
Account Equity: ${context.account_equity_usd:,.2f}
Current Net Exposure: ${context.existing_exposure.net_usd:,.2f} {context.existing_exposure.direction}
Open Positions: {positions_str}

=== HARD CONSTRAINTS (NEVER VIOLATE) ===
1. Max Leverage: {context.max_leverage_allowed}x
2. Max Risk Per Trade: {context.max_risk_per_trade_pct}% of equity (=${context.account_equity_usd * context.max_risk_per_trade_pct / 100:.2f})
3. Max Concurrent Positions: {context.max_concurrent_positions}
4. If confidence < 60% → decision MUST be NO_TRADE
5. Every trade MUST have defined SL and TP

=== YOUR DECISION FRAMEWORK ===
1. Assess trend bias (funding, OI, recent move)
2. Identify key levels (support/resistance, liquidation clusters)
3. Calculate position size where max loss = risk_pct of equity
4. Set TP with minimum 2:1 R:R ratio when possible
5. If setup is unclear or low conviction → NO_TRADE

=== OUTPUT FORMAT ===
Respond with ONLY valid JSON matching this exact schema:
{BANKR_OUTPUT_SCHEMA_STRICT}

NO text before or after the JSON. The JSON must be parseable."""

    return prompt


def ask_bankr(context: PerpMarketContext, dry_run: bool = False) -> BankrPerpDecision:
    """
    Send context to Bankr and get a trading decision.
    
    Args:
        context: The market context to analyze
        dry_run: If True, Bankr won't execute anything (but still analyzes)
    
    Returns:
        BankrPerpDecision with parsed response
    """
    prompt = build_bankr_prompt(context)
    
    # Calculate rough estimated_usdc (this is just for tracking, no actual spend on analysis)
    estimated_usdc = 0  # Analysis-only prompts don't spend
    
    payload = {
        "message": prompt,
        "mode": "perp_quant",  # Special mode flag
        "dry_run": dry_run,
        "estimated_usdc": estimated_usdc,
    }
    
    try:
        print(f"[PerpSignaler] Asking Bankr about {context.asset}...")
        resp = requests.post(
            f"{SIDECAR_URL}/prompt",
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        
        if data.get("status") != "ok":
            return BankrPerpDecision(
                parse_success=False,
                parse_error=f"Bankr error: {data.get('error', 'unknown')}",
            )
        
        # Parse the response
        response_text = data.get("summary") or data.get("raw", {}).get("response", "")
        decision = BankrPerpDecision.from_json(response_text)
        
        print(f"[PerpSignaler] Bankr says: {decision.decision} (confidence: {decision.confidence:.0%})")
        if decision.reason:
            print(f"[PerpSignaler] Reason: {decision.reason[:100]}...")
        
        return decision
        
    except requests.exceptions.RequestException as e:
        return BankrPerpDecision(
            parse_success=False,
            parse_error=f"Request failed: {e}",
        )
    except Exception as e:
        return BankrPerpDecision(
            parse_success=False,
            parse_error=f"Unexpected error: {e}",
        )


def scan_opportunities(
    assets: list[str] = None,
    timeframe: str = "scalp_1h",
    dry_run: bool = True,
) -> list[tuple[str, PerpMarketContext, BankrPerpDecision]]:
    """
    Scan multiple assets for trading opportunities.
    
    Returns list of (asset, context, decision) tuples for actionable signals.
    """
    assets = assets or TRACKED_ASSETS
    client = get_client(dry_run=dry_run)
    
    actionable = []
    
    for asset in assets:
        asset = asset.strip().upper()
        
        # Build context
        context = build_market_context(asset, client, timeframe)
        if not context:
            continue
        
        # Ask Bankr
        decision = ask_bankr(context, dry_run=dry_run)
        
        # Check if actionable
        if decision.is_actionable() and decision.confidence >= MIN_CONFIDENCE:
            actionable.append((asset, context, decision))
            print(f"[PerpSignaler] ✓ {asset}: {decision.decision} @ {decision.confidence:.0%} confidence")
        else:
            reason = "low confidence" if decision.confidence < MIN_CONFIDENCE else decision.decision
            print(f"[PerpSignaler] ✗ {asset}: {reason}")
    
    return actionable


def log_signal_to_sidecar(asset: str, decision: BankrPerpDecision):
    """Log the signal to sidecar for dashboard display"""
    try:
        requests.post(
            f"{SIDECAR_URL}/telemetry",
            json={
                "type": "perp_signal",
                "asset": asset,
                "decision": decision.decision,
                "confidence": decision.confidence,
                "reason": decision.reason[:200] if decision.reason else "",
                "size_usd": decision.size.notional_usd,
                "leverage": decision.max_leverage,
            },
            timeout=5,
        )
    except Exception as e:
        print(f"[PerpSignaler] Failed to log signal: {e}")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Perps Signaler - Get Bankr trading signals")
    parser.add_argument("--assets", "-a", default=",".join(TRACKED_ASSETS), help="Comma-separated assets to scan")
    parser.add_argument("--timeframe", "-t", default="scalp_1h", choices=["scalp_1h", "swing_4h", "position_1d"])
    parser.add_argument("--live", action="store_true", help="Run in live mode (default: dry run)")
    args = parser.parse_args()
    
    assets = [a.strip() for a in args.assets.split(",")]
    dry_run = not args.live
    
    print(f"[PerpSignaler] Scanning {len(assets)} assets in {'DRY RUN' if dry_run else 'LIVE'} mode")
    print(f"[PerpSignaler] Timeframe: {args.timeframe}")
    print()
    
    opportunities = scan_opportunities(
        assets=assets,
        timeframe=args.timeframe,
        dry_run=dry_run,
    )
    
    print()
    print(f"=== Found {len(opportunities)} actionable signals ===")
    for asset, ctx, decision in opportunities:
        print(f"\n{asset}:")
        print(f"  Direction: {decision.decision}")
        print(f"  Confidence: {decision.confidence:.0%}")
        print(f"  Entry: {decision.entry_zone.min_price} - {decision.entry_zone.max_price}")
        print(f"  TP: {decision.take_profit.target_price} (R:R {decision.take_profit.expected_rr})")
        print(f"  SL: {decision.stop_loss.price}")
        print(f"  Size: ${decision.size.notional_usd:.2f} @ {decision.max_leverage}x")
