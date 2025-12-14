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
from datetime import datetime, timezone
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


def _env_float(name: str, default: Optional[float] = None) -> Optional[float]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw if raw else default


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

        # Optional: event-driven scanning (WS updates drive what we evaluate).
        # Keeps default behavior unchanged unless explicitly enabled.
        self._event_driven = _env_bool("BTC15_WSS_EVENT_DRIVEN", False)
        self._event_wait_sec = float(os.getenv("BTC15_WSS_EVENT_WAIT_SEC", "0"))
        self._event_max_markets = int(os.getenv("BTC15_WSS_EVENT_MAX_MARKETS_PER_TICK", "8"))

        # token_id -> market slug (rebuilt on cache refresh)
        self._token_to_slug: Dict[str, str] = {}

        # Tradeable window overrides.
        # By default we preserve the cache's built-in tradeable set (documented as 2–14 min).
        self._tradeable_source = _env_str("BTC15_TRADEABLE_SOURCE", "cache").lower()  # cache|active
        self._tradeable_min_min = _env_float("BTC15_TRADEABLE_MIN_MINUTES", None)
        self._tradeable_max_min = _env_float("BTC15_TRADEABLE_MAX_MINUTES", None)

        # If user provides either min/max override, prefer filtering from active markets.
        if (self._tradeable_min_min is not None) or (self._tradeable_max_min is not None):
            if self._tradeable_source == "cache":
                self._tradeable_source = "active"

        self._logged_config = False

    def _select_tradeable_markets(self) -> Dict[str, BTC15MarketInfo]:
        """Return the tradeable market set for this tick.

        Default behavior: uses cache.tradeable_markets (typically 2–14 minutes).
        If BTC15_TRADEABLE_MIN_MINUTES / BTC15_TRADEABLE_MAX_MINUTES are set, derives
        tradeables from active_markets using market.minutes_to_expiry.
        """
        if self._tradeable_source == "cache":
            return self._cache.tradeable_markets

        min_min = float(self._tradeable_min_min) if self._tradeable_min_min is not None else float("-inf")
        max_min = float(self._tradeable_max_min) if self._tradeable_max_min is not None else float("inf")

        tradeable: Dict[str, BTC15MarketInfo] = {}
        for slug, market in self._cache.active_markets.items():
            mte = getattr(market, "minutes_to_expiry", None)
            if mte is None:
                continue
            try:
                mte_val = float(mte)
            except Exception:
                continue

            # Skip already expired (or effectively expired) markets.
            if mte_val <= 0:
                continue

            if mte_val < min_min or mte_val > max_min:
                continue

            tradeable[slug] = market
        return tradeable
    
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
        
        dirty_tokens_count = 0
        evaluated_markets = 0
        last_error: Optional[str] = None
        sim_enabled = _env_bool("SIMULATION_ENABLED", False) and bool(BANKR_DRY_RUN)

        try:
            # Step 1: Refresh cache if needed
            if time.time() - self._last_cache_refresh > self.cache_refresh_interval:
                # Deterministic slugs around now (more reliable than "latest")
                self._cache.refresh_deterministic(offsets=(0, -1, 1, 2))
                self._last_cache_refresh = time.time()
                self._metrics.record_request("gamma")

            if not self._logged_config:
                # Log once per process start so you can confirm overrides.
                log.info(
                    "[BTC15Scan] config: min_edge_cents=%.3f auto_exec_edge_cents=%.3f tradeable_source=%s tradeable_window_min=%s tradeable_window_max=%s",
                    float(self.min_edge_cents),
                    float(self.auto_execute_threshold),
                    self._tradeable_source,
                    str(self._tradeable_min_min),
                    str(self._tradeable_max_min),
                )
                self._logged_config = True

            tradeable_markets = self._select_tradeable_markets()
            
            if not tradeable_markets:
                log.debug("[BTC15Scan] No tradeable markets (total cached: %d)", 
                         len(self._cache.active_markets))
                self._emit_decision(
                    code="NO_TRADEABLE",
                    message=(
                        f"No tradeable markets (cached={len(self._cache.active_markets)} "
                        f"source={self._tradeable_source} window_min={self._tradeable_min_min} window_max={self._tradeable_max_min})"
                    ),
                )
                return ScanResult(len(self._cache.active_markets), 0, 0, 0, [])

            # Optional: keep a live snapshot of books for all tradeable token IDs.
            if self._wss_enabled and self._wss is not None:
                asset_ids: List[str] = []
                token_to_slug: Dict[str, str] = {}
                for m in tradeable_markets.values():
                    if len(m.token_ids) >= 2:
                        t0 = str(m.token_ids[0])
                        t1 = str(m.token_ids[1])
                        asset_ids.append(t0)
                        asset_ids.append(t1)
                        # Most token_ids are unique per market; keep the latest mapping.
                        token_to_slug[t0] = m.slug
                        token_to_slug[t1] = m.slug
                if asset_ids:
                    # Start once, then update assets list on refresh.
                    self._wss.start(asset_ids)
                    self._wss.update_assets(asset_ids)

                # Update mapping used by event-driven mode.
                self._token_to_slug = token_to_slug

            # If enabled, only scan markets affected by WS updates.
            markets_to_scan: List[Tuple[str, BTC15MarketInfo]]
            if self._event_driven and self._wss_enabled and self._wss is not None:
                # Optionally block briefly waiting for an update.
                if self._event_wait_sec > 0:
                    self._wss.cache.wait_for_update(timeout=float(self._event_wait_sec))

                dirty_tokens = self._wss.cache.drain_dirty_token_ids()
                dirty_tokens_count = len(dirty_tokens)
                dirty_slugs: List[str] = []
                seen: set[str] = set()
                for token_id in dirty_tokens:
                    slug = self._token_to_slug.get(str(token_id))
                    if slug and slug in tradeable_markets and slug not in seen:
                        seen.add(slug)
                        dirty_slugs.append(slug)
                        if len(dirty_slugs) >= max(1, self._event_max_markets):
                            break

                markets_to_scan = [(s, tradeable_markets[s]) for s in dirty_slugs]
            else:
                markets_to_scan = list(tradeable_markets.items())

            evaluated_markets = len(markets_to_scan)
            
            # Step 2: Scan each selected market
            for slug, market in markets_to_scan:
                self._metrics.record_market_scanned()
                
                # Skip if we already have an open position
                if slug in self._active_positions:
                    self._emit_decision(
                        slug=slug,
                        market_label=(market.question or slug),
                        code="SKIP_OPEN_POSITION",
                        message="Skipping (already active position)",
                    )
                    continue
                
                # Step 3: Fetch CLOB orderbooks
                if len(market.token_ids) < 2:
                    log.debug("[BTC15Scan] %s: missing token IDs", slug)
                    self._emit_decision(
                        slug=slug,
                        market_label=(market.question or slug),
                        code="INVALID_TOKEN_IDS",
                        message=f"Missing token IDs (len={len(market.token_ids)})",
                    )
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
                    self._emit_decision(
                        slug=slug,
                        market_label=(market.question or slug),
                        code="BOOK_EMPTY",
                        message="Orderbook unavailable or empty",
                    )
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
                    self._emit_decision(
                        slug=slug,
                        market_label=(market.question or slug),
                        code="NOT_FILLABLE",
                        message=reason,
                        edge_cents=float(edge),
                        extra={
                            "sum_asks": float(orderbooks.sum_asks),
                            "up_ask": float(orderbooks.up_ask),
                            "down_ask": float(orderbooks.down_ask),
                        },
                    )
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
                    self._emit_decision(
                        slug=slug,
                        market_label=(market.question or slug),
                        code="SKIP_SIZE_ZERO",
                        message="Optimal size <= 0",
                        edge_cents=float(expected_edge),
                    )
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
                self._emit_decision(
                    slug=slug,
                    market_label=(market.question or slug),
                    code="ACTIONABLE",
                    message=f"Fillable opportunity: shares={target_shares:.2f} est_edge={expected_edge:.2f}c",
                    edge_cents=float(expected_edge),
                    extra={
                        "sum_asks": float(orderbooks.sum_asks),
                        "up_ask": float(orderbooks.up_ask),
                        "down_ask": float(orderbooks.down_ask),
                        "total_cost": float(opp.total_cost),
                    },
                )
                
                # Step 5: Decide whether to auto-execute or call Bankr
                if expected_edge >= self.auto_execute_threshold:
                    # High-confidence: auto-execute
                    success = self._execute_bracket(opp)
                    if success:
                        opps_actioned += 1
                        self._metrics.record_opportunity(edge, actioned=True)
                        actions.append(f"AUTO_EXECUTE: {opp.description}")
                    else:
                        self._emit_decision(
                            slug=slug,
                            market_label=(market.question or slug),
                            code="EXECUTE_FAILED",
                            message="Auto-execute returned false",
                            edge_cents=float(expected_edge),
                        )
                elif sim_enabled:
                    # Visualization mode: run the same execution path in dry-run so the
                    # dashboard can show paper trades when any fillable edge exists.
                    self._emit_decision(
                        slug=slug,
                        market_label=(market.question or slug),
                        code="SIM_EXECUTE",
                        message=f"Simulation enabled: executing dry-run (edge={expected_edge:.2f}c)",
                        edge_cents=float(expected_edge),
                    )
                    success = self._execute_bracket(opp)
                    if success:
                        opps_actioned += 1
                        self._metrics.record_opportunity(edge, actioned=True)
                        actions.append(f"SIM_EXECUTE: {opp.description}")
                else:
                    # Lower confidence: ask Bankr
                    self._emit_decision(
                        slug=slug,
                        market_label=(market.question or slug),
                        code="EDGE_TOO_SMALL",
                        message=f"Edge below auto threshold ({expected_edge:.2f}c < {self.auto_execute_threshold:.2f}c): prompting Bankr",
                        edge_cents=float(expected_edge),
                    )
                    success = self._prompt_bankr(opp)
                    if success:
                        opps_actioned += 1
                        self._metrics.record_opportunity(edge, actioned=True)
                        actions.append(f"BANKR_PROMPT: {opp.description}")
        
        except Exception as e:
            log.error("[BTC15Scan] Scan error: %s", e, exc_info=True)
            last_error = str(e)
            self._emit_decision(code="ERROR", message=f"Scan exception: {last_error}")
        
        finally:
            tick = self._metrics.end_tick()
            # Emit telemetry (best-effort, should never break scanning).
            try:
                self._emit_telemetry(
                    tick=tick,
                    tradeable_markets=len(self._cache.tradeable_markets),
                    evaluated_markets=evaluated_markets,
                    dirty_tokens=dirty_tokens_count,
                    opportunities_found=opps_found,
                    opportunities_actioned=opps_actioned,
                    actions_taken=actions,
                    last_error=last_error,
                )
            except Exception:
                pass
        
        return ScanResult(
            markets_scanned=len(self._cache.active_markets),
            opportunities_found=opps_found,
            opportunities_actioned=opps_actioned,
            best_edge_cents=best_edge,
            actions_taken=actions,
        )

    def _emit_decision(
        self,
        *,
        code: str,
        message: str,
        slug: Optional[str] = None,
        market_label: Optional[str] = None,
        edge_cents: Optional[float] = None,
        extra: Optional[dict] = None,
    ) -> None:
        """Best-effort: send a human-readable decision event to sidecar."""
        if _env_bool("BTC15_DECISION_FEED_ENABLED", True) is False:
            return

        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "ts_unix": int(datetime.now(timezone.utc).timestamp() * 1000),
            "slug": slug,
            "market_label": market_label,
            "code": code,
            "message": message,
            "edge_cents": edge_cents,
            "extra": extra or None,
        }

        try:
            import requests  # type: ignore

            requests.post(
                f"{SIDECAR_URL}/btc15/decision",
                json=payload,
                timeout=0.25,
            )
            self._metrics.record_request("sidecar")
        except Exception:
            return

    def _emit_telemetry(
        self,
        *,
        tick: Any,
        tradeable_markets: int,
        evaluated_markets: int,
        dirty_tokens: int,
        opportunities_found: int,
        opportunities_actioned: int,
        actions_taken: List[str],
        last_error: Optional[str],
    ) -> None:
        # Allow disabling telemetry posting entirely.
        if _env_bool("BTC15_TELEMETRY_ENABLED", True) is False:
            return

        ws_status = None
        if self._wss_enabled and self._wss is not None:
            try:
                ws_status = self._wss.get_status()
            except Exception:
                ws_status = None

        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            # Milliseconds since epoch (int-like), used for DB retention/indexing.
            "ts_unix": int(datetime.now(timezone.utc).timestamp() * 1000),
            "ws_connected": bool(ws_status.get("connected")) if isinstance(ws_status, dict) else False,
            "ws_last_msg_ts": ws_status.get("last_message_ts") if isinstance(ws_status, dict) else None,
            "event_driven": bool(self._event_driven),
            "tick_ms": float(getattr(tick, "duration_ms", 0.0)),
            "tradeable_markets": int(tradeable_markets),
            "evaluated_markets": int(evaluated_markets),
            "dirty_tokens": int(dirty_tokens),
            "gamma_calls": int(getattr(tick, "requests_gamma", 0)),
            "clob_calls": int(getattr(tick, "requests_clob", 0)),
            # Include this telemetry post itself (+1).
            "sidecar_posts": int(getattr(tick, "requests_sidecar", 0)) + 1,
            "edges_seen": int(opportunities_found),
            "edges_actionable": int(opportunities_actioned),
            "actions_taken": actions_taken,
            "last_error": (last_error or None),
        }

        # Best-effort: sidecar may not be running.
        # Avoid the shared http_client retry behavior here; telemetry must never
        # stall the scan loop.
        try:
            import requests  # type: ignore

            requests.post(
                f"{SIDECAR_URL}/btc15/telemetry",
                json=payload,
                timeout=0.4,
            )
        except Exception:
            return
    
    def _execute_bracket(self, opp: FillableOpportunity) -> bool:
        """
        Auto-execute a high-confidence bracket.
        
        Two-phase commit (LEG A then LEG B) via Bankr sidecar prompts.
        Persists state in SQLite for idempotent restarts.
        """
        dry_run = BANKR_DRY_RUN
        sim_enabled = _env_bool("SIMULATION_ENABLED", False)

        # If caller is attempting live execution but trading is disabled, make it explicit.
        if (not dry_run) and (_env_bool("TRADING_ENABLED", False) is False):
            self._emit_decision(
                slug=opp.market.slug,
                market_label=(opp.market.question or opp.market.slug),
                code="KILL_SWITCH",
                message="Live execution blocked (TRADING_ENABLED=false)",
                edge_cents=float(opp.expected_edge_cents),
            )
            return False
        
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
                if dry_run and sim_enabled:
                    try:
                        self._record_paper_trade(opp=opp, execution_id=exec_id)
                    except Exception:
                        pass
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

    def _record_paper_trade(self, *, opp: FillableOpportunity, execution_id: str) -> None:
        """Record a simulated (paper) BTC15 bracket trade in the sidecar DB."""
        # Compute average prices for the chosen size from the snapshot.
        up_cost, up_avg = opp.orderbooks.up_book.asks.cost_to_fill(float(opp.target_shares))
        down_cost, down_avg = opp.orderbooks.down_book.asks.cost_to_fill(float(opp.target_shares))
        if not (up_cost < float('inf') and down_cost < float('inf')):
            return

        opened_at = datetime.now(timezone.utc).isoformat()

        # Leg A: UP
        trade_open = {
            "execution_id": execution_id,
            "slug": opp.market.slug,
            "market_label": (opp.market.question or opp.market.slug),
            "entry_side": "UP",
            "entry_price": float(up_avg),
            "size_shares": float(opp.target_shares),
            "opened_at": opened_at,
            "mode": "SIM",
        }

        try:
            import requests  # type: ignore

            r = requests.post(f"{SIDECAR_URL}/btc15/trade-open", json=trade_open, timeout=1.2)
            if not r.ok:
                return
            data = r.json() if r.content else {}
        except Exception:
            return

        trade_id = data.get("id")
        if not trade_id:
            return

        # Leg B: DOWN
        try:
            import requests  # type: ignore

            requests.post(
                f"{SIDECAR_URL}/btc15/trade-hedge",
                json={
                    "id": int(trade_id),
                    "hedge_side": "DOWN",
                    "hedge_price": float(down_avg),
                    "hedge_cost": float(down_cost),
                    "hedged_at": datetime.now(timezone.utc).isoformat(),
                },
                timeout=1.2,
            )
            # Arb brackets have a deterministic payout of 1.0 per share.
            requests.post(
                f"{SIDECAR_URL}/btc15/trade-resolve",
                json={
                    "id": int(trade_id),
                    "payout": float(opp.target_shares),
                    "resolved_at": datetime.now(timezone.utc).isoformat(),
                },
                timeout=1.2,
            )
        except Exception:
            return
    
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
        # Optional overrides (useful for safe pipeline testing in SIM mode).
        min_edge_override = _env_float("BTC15_MIN_EDGE_CENTS", None)
        auto_exec_override = _env_float("BTC15_AUTO_EXECUTE_THRESHOLD_CENTS", None)
        _scanner = BTC15OptimizedScanner(
            min_edge_cents=float(min_edge_override) if min_edge_override is not None else BTC15_CONFIG.min_total_edge_cents,
            max_spread=0.03,
            min_depth_usdc=BTC15_CONFIG.min_orderbook_liq_usdc,
            max_position_usdc=BTC15_CONFIG.max_bracket_usdc,
            auto_execute_threshold_cents=float(auto_exec_override) if auto_exec_override is not None else 2.0,
        )
    return _scanner


def run_btc15_scan() -> ScanResult:
    """Convenience function to run a single scan."""
    return get_btc15_scanner().scan()
