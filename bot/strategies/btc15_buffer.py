"""
Sidecar Write Buffer

Buffers telemetry/activity events and flushes in batches.
Reduces HTTP overhead while keeping trade-open/hedge/resolve atomic.

Pattern:
- Buffer non-critical events (activity, telemetry)
- Flush every N seconds or M events
- Immediate flush for critical events (trade lifecycle)
"""

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Dict, List, Optional, Any

try:
    from utils.http_client import post_json
except ImportError:
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from utils.http_client import post_json


log = logging.getLogger(__name__)


class EventPriority(Enum):
    """Priority levels for events."""
    CRITICAL = 1  # Trade lifecycle - flush immediately
    HIGH = 2      # Important state changes - flush soon
    NORMAL = 3    # Activity logs - can batch
    LOW = 4       # Telemetry - aggressive batching


@dataclass
class BufferedEvent:
    """A single event waiting to be flushed."""
    endpoint: str
    payload: Dict[str, Any]
    priority: EventPriority
    timestamp: float = field(default_factory=time.time)


class SidecarWriteBuffer:
    """
    Buffers writes to sidecar and flushes in batches.
    
    Critical events (trade lifecycle) are flushed immediately.
    Lower priority events are batched for efficiency.
    """
    
    def __init__(
        self,
        sidecar_url: str,
        flush_interval_sec: float = 5.0,
        max_buffer_size: int = 50,
        auto_start: bool = True,
    ):
        self.sidecar_url = sidecar_url.rstrip("/")
        self.flush_interval = flush_interval_sec
        self.max_buffer_size = max_buffer_size
        
        self._buffer: Deque[BufferedEvent] = deque(maxlen=max_buffer_size * 2)
        self._lock = threading.Lock()
        self._flush_thread: Optional[threading.Thread] = None
        self._running = False
        
        # Metrics
        self._events_buffered = 0
        self._events_flushed = 0
        self._flush_count = 0
        self._immediate_flushes = 0
        
        if auto_start:
            self.start()
    
    def start(self) -> None:
        """Start the background flush thread."""
        if self._running:
            return
        
        self._running = True
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()
        log.debug("[SidecarBuffer] Started flush thread")
    
    def stop(self) -> None:
        """Stop the flush thread and flush remaining events."""
        self._running = False
        if self._flush_thread:
            self._flush_thread.join(timeout=5.0)
        self._do_flush()  # Final flush
    
    def enqueue(
        self, 
        endpoint: str, 
        payload: Dict[str, Any],
        priority: EventPriority = EventPriority.NORMAL,
    ) -> None:
        """
        Add an event to the buffer.
        
        Critical events trigger immediate flush.
        """
        event = BufferedEvent(
            endpoint=endpoint,
            payload=payload,
            priority=priority,
        )
        
        with self._lock:
            self._buffer.append(event)
            self._events_buffered += 1
        
        # Immediate flush for critical events
        if priority == EventPriority.CRITICAL:
            self._do_flush()
            self._immediate_flushes += 1
        
        # Flush if buffer is getting full
        elif len(self._buffer) >= self.max_buffer_size:
            self._do_flush()
    
    def _flush_loop(self) -> None:
        """Background thread that flushes periodically."""
        while self._running:
            time.sleep(self.flush_interval)
            if self._buffer:
                self._do_flush()
    
    def _do_flush(self) -> None:
        """Flush all buffered events to sidecar."""
        with self._lock:
            if not self._buffer:
                return
            
            events = list(self._buffer)
            self._buffer.clear()
        
        if not events:
            return
        
        self._flush_count += 1
        
        # Group events by endpoint for batch sending
        by_endpoint: Dict[str, List[Dict]] = {}
        for event in events:
            if event.endpoint not in by_endpoint:
                by_endpoint[event.endpoint] = []
            by_endpoint[event.endpoint].append(event.payload)
        
        # Send batches
        for endpoint, payloads in by_endpoint.items():
            try:
                if len(payloads) == 1:
                    # Single event - send directly
                    url = f"{self.sidecar_url}{endpoint}"
                    post_json(url, payloads[0], timeout=5)
                else:
                    # Multiple events - try batch endpoint first
                    batch_endpoint = f"{endpoint}/batch"
                    url = f"{self.sidecar_url}{batch_endpoint}"
                    try:
                        post_json(url, {"events": payloads}, timeout=10)
                    except Exception:
                        # Fallback to individual sends
                        for payload in payloads:
                            try:
                                post_json(f"{self.sidecar_url}{endpoint}", payload, timeout=5)
                            except Exception as e:
                                log.warning("[SidecarBuffer] Failed to send event: %s", e)
                
                self._events_flushed += len(payloads)
                
            except Exception as e:
                log.warning("[SidecarBuffer] Flush failed for %s: %s", endpoint, e)
        
        log.debug("[SidecarBuffer] Flushed %d events in %d batches", 
                  len(events), len(by_endpoint))
    
    # ─────────────────────────────────────────────────────────────────
    # Convenience methods for common event types
    # ─────────────────────────────────────────────────────────────────
    
    def log_activity(
        self,
        slug: str,
        action: str,
        side: str,
        size_usdc: float,
        **extra,
    ) -> None:
        """Log a BTC15 activity event (batched)."""
        payload = {
            "slug": slug,
            "action": action,
            "side": side,
            "size_usdc": size_usdc,
            "timestamp": time.time(),
            **extra,
        }
        self.enqueue("/btc15/activity", payload, EventPriority.NORMAL)
    
    def open_trade(
        self,
        slug: str,
        entry_side: str,
        entry_price: float,
        size_shares: float,
        **extra,
    ) -> None:
        """Open a trade (CRITICAL - immediate flush)."""
        payload = {
            "slug": slug,
            "entry_side": entry_side,
            "entry_price": entry_price,
            "size_shares": size_shares,
            "opened_at": time.time(),
            **extra,
        }
        self.enqueue("/btc15/trade-open", payload, EventPriority.CRITICAL)
    
    def hedge_trade(
        self,
        trade_id: int,
        hedge_side: str,
        hedge_price: float,
        hedge_cost: float,
    ) -> None:
        """Record a hedge (CRITICAL - immediate flush)."""
        payload = {
            "id": trade_id,
            "hedge_side": hedge_side,
            "hedge_price": hedge_price,
            "hedge_cost": hedge_cost,
            "hedged_at": time.time(),
        }
        self.enqueue("/btc15/trade-hedge", payload, EventPriority.CRITICAL)
    
    def resolve_trade(self, trade_id: int, payout: float) -> None:
        """Resolve a trade (CRITICAL - immediate flush)."""
        payload = {
            "id": trade_id,
            "payout": payout,
            "resolved_at": time.time(),
        }
        self.enqueue("/btc15/trade-resolve", payload, EventPriority.CRITICAL)
    
    def send_telemetry(self, event_type: str, data: dict) -> None:
        """Send telemetry event (LOW priority, aggressive batching)."""
        payload = {
            "type": event_type,
            "timestamp": time.time(),
            **data,
        }
        self.enqueue("/telemetry", payload, EventPriority.LOW)
    
    def get_stats(self) -> dict:
        """Return buffer statistics."""
        return {
            "buffered_now": len(self._buffer),
            "events_buffered_total": self._events_buffered,
            "events_flushed_total": self._events_flushed,
            "flush_count": self._flush_count,
            "immediate_flushes": self._immediate_flushes,
            "running": self._running,
        }


# Singleton
_buffer: Optional[SidecarWriteBuffer] = None


def get_sidecar_buffer(sidecar_url: Optional[str] = None) -> SidecarWriteBuffer:
    """Get or create singleton buffer."""
    global _buffer
    if _buffer is None:
        import os
        try:
            from config import BANKR_EXECUTOR_URL
            url = sidecar_url or BANKR_EXECUTOR_URL
        except ImportError:
            url = sidecar_url or os.getenv("SIDECAR_URL", "http://localhost:4000")
        _buffer = SidecarWriteBuffer(url)
    return _buffer
