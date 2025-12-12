"""
Positions with live prices endpoint.
Returns open positions with current market prices and unrealized PnL.

Usage: python -m bot.positions_with_prices
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, str(__file__).rsplit("bot", 1)[0])

from bot.sidecar_client import SidecarClient
from bot.utils.polymarket import get_market_price


def calculate_unrealized_pnl(avg_price: float, current_price: float, side: str, size_usdc: float) -> tuple[float, float]:
    """Calculate unrealized PnL in USDC and percentage.
    
    Returns:
        (pnl_usdc, pnl_pct)
    """
    if avg_price <= 0 or size_usdc <= 0:
        return 0.0, 0.0

    if side.upper() == "YES":
        # Bought YES at avg_price, current value is current_price
        pnl_pct = ((current_price - avg_price) / avg_price) * 100
    else:
        # Bought NO at avg_price
        pnl_pct = ((current_price - avg_price) / avg_price) * 100

    # Approximate USDC P&L
    pnl_usdc = size_usdc * (pnl_pct / 100)

    return pnl_usdc, pnl_pct


def get_positions_with_prices() -> dict:
    """Fetch open positions and enrich with live prices."""
    client = SidecarClient()
    
    try:
        resp = client.get("/positions/open")
        if resp.status_code != 200:
            return {"ok": False, "error": f"Failed to get positions: {resp.status_code}"}
        
        data = resp.json()
        trades = data.get("trades", [])
    except Exception as e:
        return {"ok": False, "error": str(e)}

    enriched = []
    total_unrealized_pnl = 0.0
    total_exposure_yes = 0.0
    total_exposure_no = 0.0

    for trade in trades:
        market_slug = trade.get("market_slug", "")
        side = trade.get("side", "YES")
        avg_price = trade.get("avg_price", 0)
        size_usdc = trade.get("size_usdc", 0)

        current_price = None
        unrealized_pnl_usdc = 0.0
        unrealized_pnl_pct = 0.0

        if market_slug:
            try:
                current_price = get_market_price(market_slug, side)
                unrealized_pnl_usdc, unrealized_pnl_pct = calculate_unrealized_pnl(
                    avg_price, current_price, side, size_usdc
                )
                total_unrealized_pnl += unrealized_pnl_usdc
            except Exception:
                current_price = None

        # Track exposure
        if side.upper() == "YES":
            total_exposure_yes += size_usdc
        else:
            total_exposure_no += size_usdc

        enriched.append({
            **trade,
            "current_price": current_price,
            "unrealized_pnl_usdc": round(unrealized_pnl_usdc, 4),
            "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
        })

    return {
        "ok": True,
        "positions": enriched,
        "summary": {
            "total_unrealized_pnl": round(total_unrealized_pnl, 4),
            "total_positions": len(enriched),
            "net_exposure": round(total_exposure_yes - total_exposure_no, 2),
            "total_exposure_yes": round(total_exposure_yes, 2),
            "total_exposure_no": round(total_exposure_no, 2),
        }
    }


def main():
    """Print positions with prices as JSON."""
    result = get_positions_with_prices()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
