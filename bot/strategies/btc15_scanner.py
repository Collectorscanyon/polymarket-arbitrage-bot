"""
Optimized BTC15 Scanner

A tight, efficient scanner that:
1. Maintains an active set cache (stops scanning 1900+ markets)
2. Uses CLOB orderbooks for real depth/fillability checks
3. Only calls Bankr when opportunities pass all pre-filters
4. Tracks efficiency metrics for bottleneck analysis

This replaces the "wide polling" pattern with "tight targeting."
"""

import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any

from .btc15_cache import BTC15ActiveSetCache, BTC15MarketInfo, get_btc15_cache
from .btc15_clob import CLOBOrderbookFetcher, BracketOrderbooks, get_clob_fetcher
from .btc15_metrics import LoopMetrics, get_loop_metrics
from .btc15_two_phase import get_btc15_two_phase_executor
from .btc15_wss import get_btc15_market_ws

try:
    from utils.http_client import post_json
except ImportError:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from utils.http_client import post_json

try:
    from config import BTC15_CONFIG, BANKR_EXECUTOR_URL, BANKR_DRY_RUN
except ImportError:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from config import BTC15_CONFIG, BANKR_EXECUTOR_URL, BANKR_DRY_RUN


log = logging.getLogger(__name__)

SIDECAR_URL = os.getenv("SIDECAR_URL", BANKR_EXECUTOR_URL)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


@dataclass
class ScanResult:
    """Result of scanning for BTC15 opportunities."""
    markets_scanned: int
    opportunities_found: int
    opportunities_actioned: int
    best_edge_cents: float
    actions_taken: List[str]


@dataclass
class FillableOpportunity:
    """A verified fillable arb opportunity."""
    market: BTC15MarketInfo
    orderbooks: BracketOrderbooks
    target_shares: float
    expected_edge_cents: float
    up_cost: float
    down_cost: float
    total_cost: float
    
    @property
    def description(self) -> str:
        return (f"{self.market.slug}: {self.target_shares:.1f} shares, "
                f"edge {self.expected_edge_cents:.1f}c, cost ${self.total_cost:.2f}")


class BTC15OptimizedScanner:
    """
    Optimized scanner for BTC15 markets.
    
    Flow:
    1. Refresh active set cache (cheap, limited query)
    2. For each active market, fetch CLOB orderbooks
    3. Check fillability (depth, spread, edge)
    4. Only act on verified opportunities
    """
    
    def __init__(
        self,
        min_edge_cents: float = 1.0,
        max_spread: float = 0.03,
        min_depth_usdc: float = 50.0,
        max_position_usdc: float = 40.0,
        cache_refresh_interval: float = 30.0,  # seconds
        auto_execute_threshold_cents: float = 2.0,  # Auto-execute if edge > this
    ):
        self.min_edge_cents = min_edge_cents
        self.max_spread = max_spread
        self.min_depth_usdc = min_depth_usdc
        self.max_position_usdc = max_position_usdc
        self.cache_refresh_interval = cache_refresh_interval
        self.auto_execute_threshold = auto_execute_threshold_cents
        
        self._cache = get_btc15_cache()
        self._clob = get_clob_fetcher()
        self._metrics = get_loop_metrics()
        
        self._last_cache_refresh = 0.0
        self._active_positions: Dict[str, dict] = {}  # slug -> position info

        self._wss_enabled = _env_bool("BTC15_WSS_ENABLED", False)
        self._wss = get_btc15_market_ws() if self._wss_enabled else None
    
    def scan(self) -> ScanResult:
        """
        Run one scan cycle.
        
        Returns metrics about what was found and acted on.
        """
        self._metrics.start_tick()
        actions = []
        best_edge = 0.0
        opps_found = 0
        opps_actioned = 0
        
        try:
            # Step 1: Refresh cache if needed
            if time.time() - self._last_cache_refresh > self.cache_refresh_interval:
                # Deterministic slugs around now (more reliable than "latest")
                self._cache.refresh_deterministic(offsets=(0, -1, 1, 2))
                self._last_cache_refresh = time.time()
                self._metrics.record_request("gamma")
            
            # Use tradeable_markets (2-14 min to expiry) instead of all active
            tradeable_markets = self._cache.tradeable_markets
            
            if not tradeable_markets:
                log.debug("[BTC15Scan] No tradeable markets (total cached: %d)", 
                         len(self._cache.active_markets))
                return ScanResult(len(self._cache.active_markets), 0, 0, 0, [])

            # Optional: keep a live snapshot of books for all tradeable token IDs.
            if self._wss_enabled and self._wss is not None:
                asset_ids: List[str] = []
                for m in tradeable_markets.values():
                    if len(m.token_ids) >= 2:
                        asset_ids.append(str(m.token_ids[0]))
                        asset_ids.append(str(m.token_ids[1]))
                if asset_ids:
                    # Start once, then update assets list on refresh.
                    self._wss.start(asset_ids)
                    self._wss.update_assets(asset_ids)
            
            # Step 2: Scan each tradeable market
            for slug, market in tradeable_markets.items():
                self._metrics.record_market_scanned()
                
                # Skip if we already have an open position
                if slug in self._active_positions:
                    continue
                
                # Step 3: Fetch CLOB orderbooks
                if len(market.token_ids) < 2:
                    log.debug("[BTC15Scan] %s: missing token IDs", slug)
                    continue

                orderbooks: Optional[BracketOrderbooks] = None

                if self._wss_enabled and self._wss is not None:
                    orderbooks = self._wss.cache.get_bracket(str(market.token_ids[0]), str(market.token_ids[1]))

                # Fallback to REST if we don't have snapshots yet.
                if not orderbooks:
                    orderbooks = self._clob.fetch_bracket(market.token_ids[0], market.token_ids[1])
                    self._metrics.record_request("clob")
                    self._metrics.record_request("clob")  # Two requests
                
                if not orderbooks:
                    log.debug("[BTC15Scan] %s: failed to fetch orderbooks", slug)
                    continue
                
                # Step 4: Check fillability
                is_fillable, reason = orderbooks.is_fillable_arb(
                    target_shares=self.max_position_usdc / 0.5,  # Rough estimate
                    min_edge_cents=self.min_edge_cents,
                    max_spread=self.max_spread,
                    min_depth_usdc=self.min_depth_usdc,
                )
                
                edge = orderbooks.edge_cents
                best_edge = max(best_edge, edge)
                
                if not is_fillable:
                    log.debug("[BTC15Scan] %s: not fillable - %s", slug, reason)
                    continue
                
                # Found a fillable opportunity!
                opps_found += 1
                self._metrics.record_opportunity(edge, actioned=False)
                
                # Calculate optimal size
                target_shares, expected_edge = orderbooks.get_optimal_size(
                    max_usdc=self.max_position_usdc,
                    min_edge_cents=self.min_edge_cents,
                )
                
                if target_shares <= 0:
                    continue
                
                up_cost, _ = orderbooks.up_book.asks.cost_to_fill(target_shares)
                down_cost, _ = orderbooks.down_book.asks.cost_to_fill(target_shares)
                
                opp = FillableOpportunity(
                    market=market,
                    orderbooks=orderbooks,
                    target_shares=target_shares,
                    expected_edge_cents=expected_edge,
                    up_cost=up_cost,
                    down_cost=down_cost,
                    total_cost=up_cost + down_cost,
                )
                
                log.info("[BTC15Scan] OPPORTUNITY: %s", opp.description)
                
                # Step 5: Decide whether to auto-execute or call Bankr
                if expected_edge >= self.auto_execute_threshold:
                    # High-confidence: auto-execute
                    success = self._execute_bracket(opp)
                    if success:
                        opps_actioned += 1
                        self._metrics.record_opportunity(edge, actioned=True)
                        actions.append(f"AUTO_EXECUTE: {opp.description}")
                else:
                    # Lower confidence: ask Bankr
                    success = self._prompt_bankr(opp)
                    if success:
                        opps_actioned += 1
                        self._metrics.record_opportunity(edge, actioned=True)
                        actions.append(f"BANKR_PROMPT: {opp.description}")
        
        except Exception as e:
            log.error("[BTC15Scan] Scan error: %s", e, exc_info=True)
        
        finally:
            self._metrics.end_tick()
        
        return ScanResult(
            markets_scanned=len(self._cache.active_markets),
            opportunities_found=opps_found,
            opportunities_actioned=opps_actioned,
            best_edge_cents=best_edge,
            actions_taken=actions,
        )
    
    def _execute_bracket(self, opp: FillableOpportunity) -> bool:
        """
        Auto-execute a high-confidence bracket.
        
        Two-phase commit (LEG A then LEG B) via Bankr sidecar prompts.
        Persists state in SQLite for idempotent restarts.
        """
        dry_run = BANKR_DRY_RUN
        
        try:
            if len(opp.market.token_ids) < 2:
                return False

            executor = get_btc15_two_phase_executor(sidecar_url=SIDECAR_URL, dry_run=dry_run)
            exec_id = (
                f"btc15:{opp.market.slug}:{opp.market.token_ids[0]}:{opp.market.token_ids[1]}:"
                f"{int(opp.target_shares * 1000)}"
            )

            # Conservative limit price padding (bounded)
            up_limit = float(opp.orderbooks.up_ask) * 1.003
            down_limit = float(opp.orderbooks.down_ask) * 1.003

            ok = executor.execute_bracket(
                execution_id=exec_id,
                slug=opp.market.slug,
                up_token_id=str(opp.market.token_ids[0]),
                down_token_id=str(opp.market.token_ids[1]),
                target_shares=float(opp.target_shares),
                up_price_limit=up_limit,
                down_price_limit=down_limit,
                estimated_total_usdc=float(opp.total_cost),
            )
            self._metrics.record_request("sidecar")

            if ok:
                log.info("[BTC15Scan] Two-phase executed: %s", opp.market.slug)
                self._active_positions[opp.market.slug] = {
                    "entry_time": time.time(),
                    "shares": opp.target_shares,
                    "edge": opp.expected_edge_cents,
                    "execution_id": exec_id,
                }
                self._metrics.record_trade_entry(
                    opp.market.slug,
                    opp.expected_edge_cents,
                    (opp.orderbooks.up_ask + opp.orderbooks.down_ask) / 2,
                )
            return bool(ok)
                
        except Exception as e:
            log.error("[BTC15Scan] Auto-execute error: %s", e)
            return False
    
    def _prompt_bankr(self, opp: FillableOpportunity) -> bool:
        """
        Send opportunity to Bankr for review/execution.
        
        Use this for borderline cases where human judgment helps.
        """
        dry_run = BANKR_DRY_RUN
        dry_tag = "[DRY RUN] " if dry_run else ""
        
        prompt = f"""{dry_tag}BTC15 Bracket Opportunity

Market: {opp.market.question}
Slug: {opp.market.slug}
Expires in: {opp.market.minutes_to_expiry:.1f} minutes

Orderbook Analysis:
- UP best ask: ${opp.orderbooks.up_ask:.3f} (spread: {opp.orderbooks.up_book.spread:.3f})
- DOWN best ask: ${opp.orderbooks.down_ask:.3f} (spread: {opp.orderbooks.down_book.spread:.3f})
- Sum of asks: ${opp.orderbooks.sum_asks:.3f}

Recommended Trade:
- Buy {opp.target_shares:.1f} UP shares @ ${opp.orderbooks.up_ask:.3f} = ${opp.up_cost:.2f}
- Buy {opp.target_shares:.1f} DOWN shares @ ${opp.orderbooks.down_ask:.3f} = ${opp.down_cost:.2f}
- Total cost: ${opp.total_cost:.2f}
- Guaranteed payout: ${opp.target_shares:.2f}
- Expected edge: {opp.expected_edge_cents:.1f} cents

Execute this bracket?"""

        try:
            payload = {
                "message": prompt,
                "dry_run": dry_run,
                "estimated_usdc": opp.total_cost,
            }
            
            result = post_json(f"{SIDECAR_URL}/prompt", payload, timeout=60)
            self._metrics.record_request("sidecar")
            
            if result:
                log.info("[BTC15Scan] Bankr prompted for: %s", opp.market.slug)
                return True
            return False
            
        except Exception as e:
            log.error("[BTC15Scan] Bankr prompt error: %s", e)
            return False
    
    def get_status(self) -> dict:
        """Get scanner status for monitoring."""
        return {
            "cache_stats": self._cache.get_stats(),
            "clob_requests": self._clob.request_count,
            "active_positions": len(self._active_positions),
            "metrics_summary": self._metrics.get_summary(window_minutes=5),
        }


# Singleton
_scanner: Optional[BTC15OptimizedScanner] = None


def get_btc15_scanner() -> BTC15OptimizedScanner:
    """Get or create singleton scanner."""
    global _scanner
    if _scanner is None:
        _scanner = BTC15OptimizedScanner(
            min_edge_cents=BTC15_CONFIG.min_total_edge_cents,
            max_spread=0.03,
            min_depth_usdc=BTC15_CONFIG.min_orderbook_liq_usdc,
            max_position_usdc=BTC15_CONFIG.max_bracket_usdc,
        )
    return _scanner


def run_btc15_scan() -> ScanResult:
    """Convenience function to run a single scan."""
    return get_btc15_scanner().scan()
