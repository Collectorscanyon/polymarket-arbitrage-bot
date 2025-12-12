"""
CLOB Orderbook Fetcher for BTC15 Markets

Fetches real orderbook depth from Polymarket's CLOB API.
This replaces stale outcomePrices with actual fillable prices.

Key checks before executing:
- cost_yes + cost_no <= 1.00 - edge
- both sides have enough depth at/near best ask  
- spread not insane (< max_spread)
"""

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any

try:
    from utils.http_client import get_json
except ImportError:
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from utils.http_client import get_json


log = logging.getLogger(__name__)

# Polymarket CLOB API
CLOB_API_BASE = "https://clob.polymarket.com"


@dataclass
class OrderbookLevel:
    """Single price level in the orderbook."""
    price: float
    size: float  # in shares
    
    @property
    def value_usdc(self) -> float:
        """Total value at this level in USDC."""
        return self.price * self.size


@dataclass  
class SideBook:
    """One side (bid or ask) of an orderbook."""
    levels: List[OrderbookLevel]
    
    @property
    def best_price(self) -> float:
        """Best (top) price - lowest ask or highest bid."""
        return self.levels[0].price if self.levels else 0.0
    
    @property
    def best_size(self) -> float:
        """Size available at best price."""
        return self.levels[0].size if self.levels else 0.0
    
    @property
    def total_depth_usdc(self) -> float:
        """Total depth in USDC across all levels."""
        return sum(lvl.value_usdc for lvl in self.levels)
    
    def cost_to_fill(self, target_shares: float) -> Tuple[float, float]:
        """
        Calculate cost to fill target_shares, walking the book.
        
        Returns (total_cost, avg_price) or (inf, inf) if not fillable.
        """
        if target_shares <= 0:
            return 0.0, 0.0
        
        remaining = target_shares
        total_cost = 0.0
        
        for lvl in self.levels:
            take = min(remaining, lvl.size)
            total_cost += take * lvl.price
            remaining -= take
            if remaining <= 0:
                break
        
        if remaining > 0:
            # Not enough depth
            return float('inf'), float('inf')
        
        avg_price = total_cost / target_shares
        return total_cost, avg_price


@dataclass
class MarketOrderbook:
    """Full orderbook for one outcome (YES or NO)."""
    token_id: str
    bids: SideBook
    asks: SideBook
    timestamp: float
    
    @property
    def spread(self) -> float:
        """Bid-ask spread."""
        if not self.bids.levels or not self.asks.levels:
            return float('inf')
        return self.asks.best_price - self.bids.best_price
    
    @property
    def mid_price(self) -> float:
        """Mid price between best bid and ask."""
        if not self.bids.levels or not self.asks.levels:
            return 0.5
        return (self.bids.best_price + self.asks.best_price) / 2


@dataclass
class BracketOrderbooks:
    """Orderbooks for both sides of a BTC15 bracket."""
    up_book: MarketOrderbook  # YES / Up side
    down_book: MarketOrderbook  # NO / Down side
    fetch_time_ms: float
    
    @property
    def up_ask(self) -> float:
        return self.up_book.asks.best_price
    
    @property
    def down_ask(self) -> float:
        return self.down_book.asks.best_price
    
    @property
    def sum_asks(self) -> float:
        """Sum of best asks - should be < 1.0 for arb opportunity."""
        return self.up_ask + self.down_ask
    
    @property
    def edge_cents(self) -> float:
        """Edge in cents if we buy both sides at best ask."""
        return (1.0 - self.sum_asks) * 100
    
    def is_fillable_arb(
        self, 
        target_shares: float,
        min_edge_cents: float = 1.0,
        max_spread: float = 0.03,
        min_depth_usdc: float = 50.0,
    ) -> Tuple[bool, str]:
        """
        Check if this is a fillable arbitrage opportunity.
        
        Returns (is_fillable, reason).
        """
        # Hot-path reject: if the best-ask edge is already too small, slippage
        # can only make it worse.
        if self.edge_cents < min_edge_cents:
            return False, f"Best-ask edge {self.edge_cents:.1f}c < {min_edge_cents:.1f}c"

        # Check spreads
        if self.up_book.spread > max_spread:
            return False, f"UP spread {self.up_book.spread:.3f} > {max_spread}"
        if self.down_book.spread > max_spread:
            return False, f"DOWN spread {self.down_book.spread:.3f} > {max_spread}"
        
        # Check depth at best ask
        up_depth = self.up_book.asks.best_size * self.up_ask
        down_depth = self.down_book.asks.best_size * self.down_ask
        
        if up_depth < min_depth_usdc:
            return False, f"UP depth ${up_depth:.0f} < ${min_depth_usdc:.0f}"
        if down_depth < min_depth_usdc:
            return False, f"DOWN depth ${down_depth:.0f} < ${min_depth_usdc:.0f}"
        
        # Calculate fillable cost for target shares
        up_cost, up_avg = self.up_book.asks.cost_to_fill(target_shares)
        down_cost, down_avg = self.down_book.asks.cost_to_fill(target_shares)
        
        if up_cost == float('inf'):
            return False, f"Cannot fill {target_shares:.1f} UP shares"
        if down_cost == float('inf'):
            return False, f"Cannot fill {target_shares:.1f} DOWN shares"
        
        # Check edge with slippage
        total_cost = up_cost + down_cost
        payout = target_shares  # One side pays $1 per share
        actual_edge_cents = (payout - total_cost) * 100
        
        if actual_edge_cents < min_edge_cents:
            return False, f"Fillable edge {actual_edge_cents:.1f}c < {min_edge_cents:.1f}c"
        
        return True, f"Fillable: {target_shares:.1f} shares, edge {actual_edge_cents:.1f}c"
    
    def get_optimal_size(
        self,
        max_usdc: float,
        min_edge_cents: float = 1.0,
    ) -> Tuple[float, float]:
        """
        Find optimal share size that maintains min_edge_cents.
        
        Returns (shares, expected_edge_cents).
        """
        # Binary search for max fillable size
        low, high = 0.0, max_usdc / 0.3  # Rough upper bound (cheap side ~30c)
        best_shares = 0.0
        best_edge = 0.0
        
        for _ in range(20):  # Binary search iterations
            mid = (low + high) / 2
            
            up_cost, _ = self.up_book.asks.cost_to_fill(mid)
            down_cost, _ = self.down_book.asks.cost_to_fill(mid)
            
            if up_cost == float('inf') or down_cost == float('inf'):
                high = mid
                continue
            
            total_cost = up_cost + down_cost
            if total_cost > max_usdc:
                high = mid
                continue
            
            edge = (mid - total_cost) * 100  # Payout - cost in cents
            
            if edge >= min_edge_cents:
                best_shares = mid
                best_edge = edge
                low = mid
            else:
                high = mid
        
        return best_shares, best_edge


class CLOBOrderbookFetcher:
    """Fetches orderbooks from Polymarket CLOB API."""
    
    def __init__(self):
        self._last_fetch_times: Dict[str, float] = {}
        self._request_count = 0
    
    def fetch_orderbook(self, token_id: str) -> Optional[MarketOrderbook]:
        """Fetch orderbook for a single token."""
        start = time.time()
        
        try:
            url = f"{CLOB_API_BASE}/book?token_id={token_id}"
            data = get_json(url, timeout=5)
            
            if not data:
                return None
            
            # Parse bids (buyers - sorted high to low)
            bids = []
            for item in data.get("bids", []):
                price = float(item.get("price", 0))
                size = float(item.get("size", 0))
                if price > 0 and size > 0:
                    bids.append(OrderbookLevel(price=price, size=size))
            
            # Parse asks (sellers - sorted low to high)
            asks = []
            for item in data.get("asks", []):
                price = float(item.get("price", 0))
                size = float(item.get("size", 0))
                if price > 0 and size > 0:
                    asks.append(OrderbookLevel(price=price, size=size))
            
            self._request_count += 1
            self._last_fetch_times[token_id] = time.time()
            
            return MarketOrderbook(
                token_id=token_id,
                bids=SideBook(levels=bids),
                asks=SideBook(levels=asks),
                timestamp=time.time(),
            )
            
        except Exception as e:
            log.debug("[CLOB] Failed to fetch book for %s: %s", token_id, e)
            return None
    
    def fetch_bracket(self, up_token_id: str, down_token_id: str) -> Optional[BracketOrderbooks]:
        """Fetch orderbooks for both sides of a bracket."""
        start = time.time()
        
        up_book = self.fetch_orderbook(up_token_id)
        down_book = self.fetch_orderbook(down_token_id)
        
        if not up_book or not down_book:
            return None
        
        elapsed_ms = (time.time() - start) * 1000
        
        return BracketOrderbooks(
            up_book=up_book,
            down_book=down_book,
            fetch_time_ms=elapsed_ms,
        )
    
    @property
    def request_count(self) -> int:
        return self._request_count


# Singleton
_fetcher: Optional[CLOBOrderbookFetcher] = None


def get_clob_fetcher() -> CLOBOrderbookFetcher:
    """Get or create singleton CLOB fetcher."""
    global _fetcher
    if _fetcher is None:
        _fetcher = CLOBOrderbookFetcher()
    return _fetcher
