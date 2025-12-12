"""
BTC15 Loop Metrics

Tracks efficiency KPIs for the BTC15 loop:
- tick_duration_ms
- requests_per_min (Gamma, CLOB, sidecar)
- book_updates_per_min
- opportunities_seen vs opportunities_actioned
- avg_edge_cents_on_entry and avg_realized_pnl
"""

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class TickMetrics:
    """Metrics for a single loop tick."""
    tick_id: int
    start_time: float
    duration_ms: float
    markets_scanned: int
    opportunities_seen: int
    opportunities_actioned: int
    requests_gamma: int
    requests_clob: int
    requests_sidecar: int
    edge_cents: float = 0.0  # Best edge seen this tick


@dataclass
class TradeMetrics:
    """Metrics for a single trade (entry + optional hedge)."""
    slug: str
    entry_time: float
    entry_edge_cents: float
    entry_price: float
    hedge_time: Optional[float] = None
    hedge_price: Optional[float] = None
    realized_pnl: Optional[float] = None


class LoopMetrics:
    """
    Tracks and reports efficiency metrics for the BTC15 loop.
    
    Logs these each tick for bottleneck analysis:
    - tick_duration_ms
    - requests_per_min 
    - opportunities_seen vs actioned
    - avg_edge_cents
    """
    
    def __init__(self, history_size: int = 100):
        self._history_size = history_size
        self._ticks: Deque[TickMetrics] = deque(maxlen=history_size)
        self._trades: Deque[TradeMetrics] = deque(maxlen=history_size)
        self._tick_counter = 0
        self._start_time = time.time()
        
        # Current tick state
        self._current_tick_start: Optional[float] = None
        self._current_markets = 0
        self._current_opps_seen = 0
        self._current_opps_actioned = 0
        self._current_requests_gamma = 0
        self._current_requests_clob = 0
        self._current_requests_sidecar = 0
        self._current_best_edge = 0.0
    
    def start_tick(self) -> None:
        """Mark the start of a new loop tick."""
        self._current_tick_start = time.time()
        self._tick_counter += 1
        self._current_markets = 0
        self._current_opps_seen = 0
        self._current_opps_actioned = 0
        self._current_requests_gamma = 0
        self._current_requests_clob = 0
        self._current_requests_sidecar = 0
        self._current_best_edge = 0.0
    
    def end_tick(self) -> TickMetrics:
        """Mark end of tick and record metrics."""
        if self._current_tick_start is None:
            self.start_tick()
        
        duration_ms = (time.time() - self._current_tick_start) * 1000
        
        tick = TickMetrics(
            tick_id=self._tick_counter,
            start_time=self._current_tick_start,
            duration_ms=duration_ms,
            markets_scanned=self._current_markets,
            opportunities_seen=self._current_opps_seen,
            opportunities_actioned=self._current_opps_actioned,
            requests_gamma=self._current_requests_gamma,
            requests_clob=self._current_requests_clob,
            requests_sidecar=self._current_requests_sidecar,
            edge_cents=self._current_best_edge,
        )
        
        self._ticks.append(tick)
        
        # Log the tick metrics
        log.info(
            "[METRICS] tick=%d dur=%.0fms mkts=%d opps=%d/%d reqs=G%d/C%d/S%d edge=%.1fc",
            tick.tick_id,
            tick.duration_ms,
            tick.markets_scanned,
            tick.opportunities_actioned,
            tick.opportunities_seen,
            tick.requests_gamma,
            tick.requests_clob,
            tick.requests_sidecar,
            tick.edge_cents,
        )
        
        return tick
    
    def record_market_scanned(self) -> None:
        """Record that a market was scanned."""
        self._current_markets += 1
    
    def record_opportunity(self, edge_cents: float, actioned: bool = False) -> None:
        """Record an opportunity seen (and whether it was acted on)."""
        self._current_opps_seen += 1
        if actioned:
            self._current_opps_actioned += 1
        self._current_best_edge = max(self._current_best_edge, edge_cents)
    
    def record_request(self, target: str) -> None:
        """Record an HTTP request. Target: 'gamma', 'clob', or 'sidecar'."""
        if target == "gamma":
            self._current_requests_gamma += 1
        elif target == "clob":
            self._current_requests_clob += 1
        elif target == "sidecar":
            self._current_requests_sidecar += 1
    
    def record_trade_entry(self, slug: str, edge_cents: float, price: float) -> None:
        """Record a new trade entry."""
        self._trades.append(TradeMetrics(
            slug=slug,
            entry_time=time.time(),
            entry_edge_cents=edge_cents,
            entry_price=price,
        ))
    
    def record_trade_exit(self, slug: str, realized_pnl: float) -> None:
        """Record trade exit with realized PnL."""
        for trade in reversed(self._trades):
            if trade.slug == slug and trade.realized_pnl is None:
                trade.realized_pnl = realized_pnl
                break
    
    def get_summary(self, window_minutes: float = 5.0) -> Dict:
        """Get summary metrics for the last N minutes."""
        cutoff = time.time() - (window_minutes * 60)
        recent_ticks = [t for t in self._ticks if t.start_time >= cutoff]
        recent_trades = [t for t in self._trades if t.entry_time >= cutoff]
        
        if not recent_ticks:
            return {"error": "no recent ticks"}
        
        # Aggregate tick metrics
        total_duration = sum(t.duration_ms for t in recent_ticks)
        total_markets = sum(t.markets_scanned for t in recent_ticks)
        total_opps_seen = sum(t.opportunities_seen for t in recent_ticks)
        total_opps_actioned = sum(t.opportunities_actioned for t in recent_ticks)
        total_gamma = sum(t.requests_gamma for t in recent_ticks)
        total_clob = sum(t.requests_clob for t in recent_ticks)
        total_sidecar = sum(t.requests_sidecar for t in recent_ticks)
        
        elapsed_min = max(0.01, (time.time() - recent_ticks[0].start_time) / 60)
        
        # Trade metrics
        completed_trades = [t for t in recent_trades if t.realized_pnl is not None]
        avg_entry_edge = (sum(t.entry_edge_cents for t in recent_trades) / len(recent_trades)
                         if recent_trades else 0)
        avg_realized_pnl = (sum(t.realized_pnl for t in completed_trades) / len(completed_trades)
                           if completed_trades else 0)
        
        return {
            "window_minutes": window_minutes,
            "ticks": len(recent_ticks),
            "avg_tick_ms": total_duration / len(recent_ticks),
            "markets_per_tick": total_markets / len(recent_ticks),
            "opportunities_seen": total_opps_seen,
            "opportunities_actioned": total_opps_actioned,
            "action_rate": total_opps_actioned / max(1, total_opps_seen),
            "requests_per_min": {
                "gamma": total_gamma / elapsed_min,
                "clob": total_clob / elapsed_min,
                "sidecar": total_sidecar / elapsed_min,
                "total": (total_gamma + total_clob + total_sidecar) / elapsed_min,
            },
            "trades": len(recent_trades),
            "trades_completed": len(completed_trades),
            "avg_entry_edge_cents": avg_entry_edge,
            "avg_realized_pnl": avg_realized_pnl,
        }
    
    def log_summary(self, window_minutes: float = 5.0) -> None:
        """Log a summary of recent metrics."""
        s = self.get_summary(window_minutes)
        if "error" in s:
            return
        
        log.info(
            "[METRICS SUMMARY] %.0fmin window: %d ticks, avg %.0fms | "
            "opps %d seen / %d acted (%.0f%%) | "
            "reqs/min G%.0f C%.0f S%.0f | "
            "trades %d, avg edge %.1fc, avg pnl $%.2f",
            s["window_minutes"],
            s["ticks"],
            s["avg_tick_ms"],
            s["opportunities_seen"],
            s["opportunities_actioned"],
            s["action_rate"] * 100,
            s["requests_per_min"]["gamma"],
            s["requests_per_min"]["clob"],
            s["requests_per_min"]["sidecar"],
            s["trades"],
            s["avg_entry_edge_cents"],
            s["avg_realized_pnl"],
        )


# Singleton
_metrics: Optional[LoopMetrics] = None


def get_loop_metrics() -> LoopMetrics:
    """Get or create singleton metrics instance."""
    global _metrics
    if _metrics is None:
        _metrics = LoopMetrics()
    return _metrics
