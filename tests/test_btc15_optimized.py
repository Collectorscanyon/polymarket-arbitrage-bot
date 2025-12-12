"""Tests for BTC15 optimization modules."""

import pytest
import time
from unittest.mock import patch, MagicMock

from bot.strategies.btc15_clob import (
    OrderbookLevel, SideBook, MarketOrderbook, BracketOrderbooks
)
from bot.strategies.btc15_metrics import LoopMetrics
from bot.strategies.btc15_buffer import SidecarWriteBuffer, EventPriority


class TestOrderbook:
    """Test CLOB orderbook logic."""
    
    def test_sidebook_best_price(self):
        """Best price is first level."""
        book = SideBook(levels=[
            OrderbookLevel(price=0.45, size=100),
            OrderbookLevel(price=0.46, size=200),
        ])
        assert book.best_price == 0.45
        assert book.best_size == 100
    
    def test_sidebook_empty(self):
        """Empty book returns 0."""
        book = SideBook(levels=[])
        assert book.best_price == 0.0
        assert book.best_size == 0.0
    
    def test_cost_to_fill_single_level(self):
        """Fill entirely from one level."""
        book = SideBook(levels=[
            OrderbookLevel(price=0.50, size=100),
        ])
        cost, avg = book.cost_to_fill(50)
        assert cost == 25.0  # 50 * 0.50
        assert avg == 0.50
    
    def test_cost_to_fill_multiple_levels(self):
        """Fill walks through multiple levels."""
        book = SideBook(levels=[
            OrderbookLevel(price=0.50, size=30),
            OrderbookLevel(price=0.52, size=50),
            OrderbookLevel(price=0.55, size=100),
        ])
        cost, avg = book.cost_to_fill(50)
        # 30 @ 0.50 = 15, 20 @ 0.52 = 10.4, total = 25.4
        assert cost == pytest.approx(25.4, rel=0.01)
        assert avg == pytest.approx(0.508, rel=0.01)
    
    def test_cost_to_fill_insufficient_depth(self):
        """Not enough depth returns infinity."""
        book = SideBook(levels=[
            OrderbookLevel(price=0.50, size=30),
        ])
        cost, avg = book.cost_to_fill(100)
        assert cost == float('inf')


class TestBracketOrderbooks:
    """Test bracket (both sides) logic."""
    
    def _make_bracket(self, up_ask: float, down_ask: float, size: float = 100) -> BracketOrderbooks:
        up = MarketOrderbook(
            token_id="up",
            bids=SideBook([OrderbookLevel(up_ask - 0.02, size)]),
            asks=SideBook([OrderbookLevel(up_ask, size)]),
            timestamp=time.time(),
        )
        down = MarketOrderbook(
            token_id="down",
            bids=SideBook([OrderbookLevel(down_ask - 0.02, size)]),
            asks=SideBook([OrderbookLevel(down_ask, size)]),
            timestamp=time.time(),
        )
        return BracketOrderbooks(up, down, fetch_time_ms=10)
    
    def test_sum_asks(self):
        """Sum of asks calculated correctly."""
        bracket = self._make_bracket(0.45, 0.53)
        assert bracket.sum_asks == pytest.approx(0.98, rel=0.01)
    
    def test_edge_cents(self):
        """Edge in cents = (1 - sum) * 100."""
        bracket = self._make_bracket(0.45, 0.53)  # sum = 0.98
        assert bracket.edge_cents == pytest.approx(2.0, rel=0.1)
    
    def test_no_arb_when_sum_high(self):
        """No arb when sum >= 1."""
        bracket = self._make_bracket(0.50, 0.51)  # sum = 1.01
        is_fillable, reason = bracket.is_fillable_arb(10, min_edge_cents=1.0)
        assert is_fillable is False
        assert "edge" in reason.lower() or "fillable" in reason.lower()
    
    def test_arb_when_sum_low(self):
        """Arb opportunity when sum < 1 with enough edge."""
        bracket = self._make_bracket(0.45, 0.52, size=200)  # sum = 0.97, edge = 3c
        is_fillable, reason = bracket.is_fillable_arb(
            target_shares=10,
            min_edge_cents=1.0,
            min_depth_usdc=10,
        )
        assert is_fillable is True
        assert "fillable" in reason.lower()


class TestLoopMetrics:
    """Test metrics tracking."""
    
    def test_tick_lifecycle(self):
        """Start and end tick records metrics."""
        metrics = LoopMetrics()
        metrics.start_tick()
        metrics.record_market_scanned()
        metrics.record_market_scanned()
        metrics.record_opportunity(2.5, actioned=True)
        metrics.record_request("gamma")
        metrics.record_request("clob")
        
        tick = metrics.end_tick()
        
        assert tick.markets_scanned == 2
        assert tick.opportunities_seen == 1
        assert tick.opportunities_actioned == 1
        assert tick.requests_gamma == 1
        assert tick.requests_clob == 1
        assert tick.edge_cents == pytest.approx(2.5, rel=0.1)
    
    def test_summary_empty(self):
        """Summary handles no ticks gracefully."""
        metrics = LoopMetrics()
        summary = metrics.get_summary()
        assert "error" in summary


class TestSidecarBuffer:
    """Test write buffer."""
    
    def test_enqueue_normal(self):
        """Normal events are buffered."""
        buffer = SidecarWriteBuffer("http://test", auto_start=False)
        buffer.enqueue("/test", {"key": "value"}, EventPriority.NORMAL)
        
        assert len(buffer._buffer) == 1
        assert buffer._events_buffered == 1
    
    @patch("bot.strategies.btc15_buffer.post_json")
    def test_critical_flushes_immediately(self, mock_post):
        """Critical events trigger immediate flush."""
        mock_post.return_value = {"ok": True}
        buffer = SidecarWriteBuffer("http://test", auto_start=False)
        buffer.enqueue("/critical", {"key": "value"}, EventPriority.CRITICAL)
        
        # Should have flushed immediately
        assert len(buffer._buffer) == 0
        assert buffer._immediate_flushes == 1
        mock_post.assert_called()
    
    def test_buffer_stats(self):
        """Stats are tracked correctly."""
        buffer = SidecarWriteBuffer("http://test", auto_start=False)
        buffer.enqueue("/a", {}, EventPriority.NORMAL)
        buffer.enqueue("/b", {}, EventPriority.LOW)
        
        stats = buffer.get_stats()
        assert stats["buffered_now"] == 2
        assert stats["events_buffered_total"] == 2
