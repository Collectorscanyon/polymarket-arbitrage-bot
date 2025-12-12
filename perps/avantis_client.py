"""
Avantis API Client - Execute perp trades on Avantis (Base chain)

This is a skeleton client that will need to be filled in with actual
Avantis SDK/API calls once you have API access.

Avantis docs: https://docs.avantis.finance/
"""

import os
import time
import requests
from dataclasses import dataclass
from typing import Optional, Literal
from enum import Enum

# Avantis API base (replace with actual endpoint)
AVANTIS_API_BASE = os.getenv("AVANTIS_API_BASE", "https://api.avantis.io/v1")
AVANTIS_API_KEY = os.getenv("AVANTIS_API_KEY", "")


class OrderSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


@dataclass
class AvantisPosition:
    """Represents an open position on Avantis"""
    position_id: str
    asset: str
    side: str  # LONG or SHORT
    size_usd: float
    entry_price: float
    current_price: float
    leverage: float
    unrealized_pnl: float
    liquidation_price: float
    tp_price: Optional[float] = None
    sl_price: Optional[float] = None
    opened_at: str = ""


@dataclass
class AvantisOrder:
    """Represents an order to be placed"""
    asset: str
    side: str
    size_usd: float
    leverage: float
    order_type: str = "MARKET"
    limit_price: Optional[float] = None
    tp_price: Optional[float] = None
    sl_price: Optional[float] = None


@dataclass 
class OrderResult:
    """Result of an order placement"""
    success: bool
    order_id: Optional[str] = None
    position_id: Optional[str] = None
    fill_price: Optional[float] = None
    error: Optional[str] = None


class AvantisClient:
    """
    Client for interacting with Avantis perps protocol.
    
    NOTE: This is a skeleton implementation. You'll need to:
    1. Get Avantis API access/SDK
    2. Implement actual API calls
    3. Handle wallet signing for on-chain transactions
    """
    
    def __init__(self, api_key: str = None, dry_run: bool = True):
        self.api_key = api_key or AVANTIS_API_KEY
        self.dry_run = dry_run
        self.base_url = AVANTIS_API_BASE
        
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
    
    # ─────────────────────────────────────────────────────────────────
    # Market Data (these would hit Avantis API or on-chain data)
    # ─────────────────────────────────────────────────────────────────
    
    def get_markets(self) -> list[dict]:
        """
        Get list of available perp markets on Avantis.
        
        Returns list of dicts with:
        - asset: str (e.g., "DEGEN", "BNKR", "ETH")
        - price: float
        - funding_rate: float
        - open_interest: float
        - max_leverage: float
        """
        # TODO: Replace with actual API call
        # Example: return requests.get(f"{self.base_url}/markets", headers=self._headers()).json()
        
        # Mock data for development
        return [
            {
                "asset": "DEGEN",
                "price": 0.0342,
                "funding_rate_8h": 0.012,
                "open_interest_usd": 1450000,
                "volume_24h_usd": 2200000,
                "max_leverage": 10,
            },
            {
                "asset": "BNKR",
                "price": 0.0089,
                "funding_rate_8h": 0.008,
                "open_interest_usd": 890000,
                "volume_24h_usd": 1100000,
                "max_leverage": 5,
            },
            {
                "asset": "ETH",
                "price": 3450.50,
                "funding_rate_8h": 0.001,
                "open_interest_usd": 45000000,
                "volume_24h_usd": 120000000,
                "max_leverage": 50,
            },
        ]
    
    def get_market(self, asset: str) -> Optional[dict]:
        """Get data for a specific market"""
        markets = self.get_markets()
        for m in markets:
            if m["asset"].upper() == asset.upper():
                return m
        return None
    
    def get_price(self, asset: str) -> float:
        """Get current price for an asset"""
        market = self.get_market(asset)
        return market["price"] if market else 0.0
    
    def get_funding_rate(self, asset: str) -> float:
        """Get 8h funding rate for an asset"""
        market = self.get_market(asset)
        return market.get("funding_rate_8h", 0.0) if market else 0.0
    
    # ─────────────────────────────────────────────────────────────────
    # Account & Positions
    # ─────────────────────────────────────────────────────────────────
    
    def get_account_equity(self) -> float:
        """Get total account equity in USD"""
        # TODO: Replace with actual API call
        return float(os.getenv("PERPS_ACCOUNT_EQUITY", "10000"))
    
    def get_positions(self) -> list[AvantisPosition]:
        """Get all open positions"""
        # TODO: Replace with actual API call
        # Example:
        # resp = requests.get(f"{self.base_url}/positions", headers=self._headers())
        # return [AvantisPosition(**p) for p in resp.json()]
        
        # Mock: return empty for now
        return []
    
    def get_position(self, asset: str) -> Optional[AvantisPosition]:
        """Get position for a specific asset"""
        positions = self.get_positions()
        for p in positions:
            if p.asset.upper() == asset.upper():
                return p
        return None
    
    def get_net_exposure(self) -> dict:
        """Calculate net USD exposure across all positions"""
        positions = self.get_positions()
        long_usd = sum(p.size_usd for p in positions if p.side == "LONG")
        short_usd = sum(p.size_usd for p in positions if p.side == "SHORT")
        return {
            "long_usd": long_usd,
            "short_usd": short_usd,
            "net_usd": long_usd - short_usd,
            "direction": "LONG" if long_usd > short_usd else ("SHORT" if short_usd > long_usd else "FLAT"),
        }
    
    # ─────────────────────────────────────────────────────────────────
    # Order Execution
    # ─────────────────────────────────────────────────────────────────
    
    def place_order(self, order: AvantisOrder) -> OrderResult:
        """
        Place a new order on Avantis.
        
        In production, this would:
        1. Build the order transaction
        2. Sign with wallet
        3. Submit to Avantis
        4. Wait for confirmation
        """
        print(f"[AvantisClient] {'DRY RUN: ' if self.dry_run else ''}Placing order:")
        print(f"  Asset: {order.asset}")
        print(f"  Side: {order.side}")
        print(f"  Size: ${order.size_usd:.2f}")
        print(f"  Leverage: {order.leverage}x")
        print(f"  Type: {order.order_type}")
        if order.tp_price:
            print(f"  TP: {order.tp_price}")
        if order.sl_price:
            print(f"  SL: {order.sl_price}")
        
        if self.dry_run:
            # Simulate successful order
            return OrderResult(
                success=True,
                order_id=f"dry_run_{int(time.time())}",
                position_id=f"pos_dry_{int(time.time())}",
                fill_price=self.get_price(order.asset),
            )
        
        # TODO: Replace with actual API call
        # Example:
        # payload = {
        #     "asset": order.asset,
        #     "side": order.side,
        #     "size_usd": order.size_usd,
        #     "leverage": order.leverage,
        #     "order_type": order.order_type,
        #     "limit_price": order.limit_price,
        #     "tp_price": order.tp_price,
        #     "sl_price": order.sl_price,
        # }
        # resp = requests.post(f"{self.base_url}/orders", json=payload, headers=self._headers())
        # data = resp.json()
        # return OrderResult(success=data.get("success"), ...)
        
        return OrderResult(
            success=False,
            error="Not implemented - need Avantis API integration",
        )
    
    def close_position(self, asset: str) -> OrderResult:
        """Close an existing position"""
        position = self.get_position(asset)
        if not position:
            return OrderResult(success=False, error=f"No position found for {asset}")
        
        # Close by opening opposite position
        close_side = "SHORT" if position.side == "LONG" else "LONG"
        
        print(f"[AvantisClient] {'DRY RUN: ' if self.dry_run else ''}Closing {position.side} position in {asset}")
        
        if self.dry_run:
            return OrderResult(
                success=True,
                order_id=f"close_dry_{int(time.time())}",
                fill_price=self.get_price(asset),
            )
        
        # TODO: Implement actual close logic
        return OrderResult(
            success=False,
            error="Not implemented - need Avantis API integration",
        )
    
    def update_tp_sl(self, asset: str, tp_price: float = None, sl_price: float = None) -> OrderResult:
        """Update TP/SL for an existing position"""
        position = self.get_position(asset)
        if not position:
            return OrderResult(success=False, error=f"No position found for {asset}")
        
        print(f"[AvantisClient] {'DRY RUN: ' if self.dry_run else ''}Updating TP/SL for {asset}")
        if tp_price:
            print(f"  New TP: {tp_price}")
        if sl_price:
            print(f"  New SL: {sl_price}")
        
        if self.dry_run:
            return OrderResult(success=True)
        
        # TODO: Implement actual update logic
        return OrderResult(
            success=False,
            error="Not implemented - need Avantis API integration",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Convenience functions
# ─────────────────────────────────────────────────────────────────────────────

def get_client(dry_run: bool = None) -> AvantisClient:
    """Factory function to get configured client"""
    if dry_run is None:
        dry_run = os.getenv("AVANTIS_DRY_RUN", "true").lower() in ("true", "1", "yes")
    return AvantisClient(dry_run=dry_run)


if __name__ == "__main__":
    # Test the client
    client = get_client(dry_run=True)
    
    print("=== Available Markets ===")
    for m in client.get_markets():
        print(f"  {m['asset']}: ${m['price']} | Funding: {m['funding_rate_8h']*100:.3f}%")
    
    print(f"\n=== Account ===")
    print(f"  Equity: ${client.get_account_equity():,.2f}")
    print(f"  Positions: {len(client.get_positions())}")
    
    print(f"\n=== Test Order ===")
    order = AvantisOrder(
        asset="DEGEN",
        side="LONG",
        size_usd=500,
        leverage=3,
        tp_price=0.04,
        sl_price=0.032,
    )
    result = client.place_order(order)
    print(f"  Result: {'✓' if result.success else '✗'} {result.order_id or result.error}")
