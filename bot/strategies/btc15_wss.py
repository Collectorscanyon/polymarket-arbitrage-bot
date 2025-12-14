"""CLOB WebSocket (market channel) subscriber for BTC15.

Purpose
- Maintain an in-memory best-bid/ask + depth snapshot per token_id.
- Let the BTC15 scanner evaluate opportunities without per-tick REST calls.

Design
- Uses the public MARKET channel: wss://ws-subscriptions-clob.polymarket.com/ws/market
- Subscribes by token ids ("assets_ids").
- Handles full "book" messages and incremental "price_change" messages.
- Threaded, with a safe fallback: callers can ignore this module entirely.

This module intentionally does not require auth for market channel subscription.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .btc15_clob import (
    BracketOrderbooks,
    MarketOrderbook,
    OrderbookLevel,
    SideBook,
)

log = logging.getLogger(__name__)

WSS_BASE_DEFAULT = "wss://ws-subscriptions-clob.polymarket.com"
MARKET_CHANNEL = "market"


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


@dataclass
class _TokenBookState:
    token_id: str
    bids_by_price: Dict[float, float]
    asks_by_price: Dict[float, float]
    last_ts: float

    def to_orderbook(self) -> MarketOrderbook:
        bids = [OrderbookLevel(price=p, size=s) for p, s in sorted(self.bids_by_price.items(), key=lambda x: -x[0]) if p > 0 and s > 0]
        asks = [OrderbookLevel(price=p, size=s) for p, s in sorted(self.asks_by_price.items(), key=lambda x: x[0]) if p > 0 and s > 0]
        return MarketOrderbook(
            token_id=self.token_id,
            bids=SideBook(levels=bids),
            asks=SideBook(levels=asks),
            timestamp=self.last_ts,
        )


class BTC15WSBookCache:
    """Thread-safe in-memory cache of token orderbooks."""

    def __init__(self):
        self._lock = threading.Lock()
        self._books: Dict[str, _TokenBookState] = {}
        self._dirty_token_ids: Set[str] = set()
        self._update_event = threading.Event()

    def drain_dirty_token_ids(self) -> Set[str]:
        """Return and clear the set of token_ids updated since last drain."""
        with self._lock:
            dirty = set(self._dirty_token_ids)
            self._dirty_token_ids.clear()
        return dirty

    def apply_market_event(self, msg: Dict[str, Any]) -> None:
        event_type = (msg.get("event_type") or msg.get("type") or "").lower()

        if event_type == "book":
            token_id = str(msg.get("asset_id") or "")
            if not token_id:
                return

            bids = msg.get("bids") or msg.get("buys") or []
            asks = msg.get("asks") or msg.get("sells") or []

            bids_by_price: Dict[float, float] = {}
            asks_by_price: Dict[float, float] = {}

            for lvl in bids:
                p = _safe_float(lvl.get("price"))
                s = _safe_float(lvl.get("size"))
                if p > 0 and s > 0:
                    bids_by_price[p] = s

            for lvl in asks:
                p = _safe_float(lvl.get("price"))
                s = _safe_float(lvl.get("size"))
                if p > 0 and s > 0:
                    asks_by_price[p] = s

            ts_ms = _safe_float(msg.get("timestamp"), default=time.time() * 1000)
            ts = float(ts_ms) / 1000.0

            with self._lock:
                self._books[token_id] = _TokenBookState(
                    token_id=token_id,
                    bids_by_price=bids_by_price,
                    asks_by_price=asks_by_price,
                    last_ts=ts,
                )
                self._dirty_token_ids.add(token_id)
            self._update_event.set()
            return

        if event_type == "price_change":
            price_changes = msg.get("price_changes") or []
            ts_ms = _safe_float(msg.get("timestamp"), default=time.time() * 1000)
            ts = float(ts_ms) / 1000.0

            with self._lock:
                for ch in price_changes:
                    token_id = str(ch.get("asset_id") or "")
                    if not token_id:
                        continue
                    price = _safe_float(ch.get("price"))
                    size = _safe_float(ch.get("size"))
                    side = str(ch.get("side") or "").upper()

                    st = self._books.get(token_id)
                    if st is None:
                        st = _TokenBookState(token_id=token_id, bids_by_price={}, asks_by_price={}, last_ts=ts)
                        self._books[token_id] = st

                    if side == "BUY":
                        if size <= 0:
                            st.bids_by_price.pop(price, None)
                        else:
                            st.bids_by_price[price] = size
                    elif side == "SELL":
                        if size <= 0:
                            st.asks_by_price.pop(price, None)
                        else:
                            st.asks_by_price[price] = size

                    st.last_ts = ts

                    self._dirty_token_ids.add(token_id)

            self._update_event.set()
            return

    def get_orderbook(self, token_id: str) -> Optional[MarketOrderbook]:
        with self._lock:
            st = self._books.get(str(token_id))
            if st is None:
                return None
            return st.to_orderbook()

    def get_bracket(self, up_token_id: str, down_token_id: str) -> Optional[BracketOrderbooks]:
        up = self.get_orderbook(up_token_id)
        down = self.get_orderbook(down_token_id)
        if not up or not down:
            return None
        return BracketOrderbooks(up_book=up, down_book=down, fetch_time_ms=0.0)

    def wait_for_update(self, timeout: float) -> bool:
        signaled = self._update_event.wait(timeout=timeout)
        if signaled:
            self._update_event.clear()
        return bool(signaled)


class CLOBMarketWSSubscriber:
    """Background thread that maintains BTC15WSBookCache via market channel."""

    def __init__(self, wss_base: str = WSS_BASE_DEFAULT):
        self.wss_base = wss_base.rstrip("/")
        self.cache = BTC15WSBookCache()

        self._assets: Set[str] = set()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ws = None

        self._connected: bool = False
        self._last_message_ts: float = 0.0

    def get_status(self) -> Dict[str, Any]:
        """Lightweight status snapshot for dashboards/telemetry."""
        with self._lock:
            connected = bool(self._connected)
            last_ts = float(self._last_message_ts)
        age = None
        if last_ts > 0:
            age = max(0.0, time.time() - last_ts)
        return {
            "connected": connected,
            "last_message_ts": last_ts if last_ts > 0 else None,
            "last_message_age_sec": age,
        }

    def start(self, asset_ids: Iterable[str]) -> None:
        with self._lock:
            self._assets = {str(a) for a in asset_ids if str(a)}

        if self._thread and self._thread.is_alive():
            return

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="btc15-wss", daemon=True)
        self._thread.start()

    def update_assets(self, asset_ids: Iterable[str]) -> None:
        with self._lock:
            self._assets = {str(a) for a in asset_ids if str(a)}
        # Best-effort re-subscribe (some servers accept re-subscribe messages).
        try:
            self._send_subscribe()
        except Exception:
            pass

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass

    def _send_subscribe(self) -> None:
        if self._ws is None:
            return
        with self._lock:
            assets = sorted(self._assets)
        msg = {"assets_ids": assets, "type": MARKET_CHANNEL}
        self._ws.send(json.dumps(msg))

    def _run(self) -> None:
        # websocket-client is the doc-recommended library.
        try:
            from websocket import WebSocketApp  # type: ignore
        except Exception as e:  # pragma: no cover
            log.error("websocket-client not installed: %s", e)
            return

        url = f"{self.wss_base}/ws/{MARKET_CHANNEL}"
        backoff = 1.0

        def on_open(ws):
            self._ws = ws
            with self._lock:
                self._connected = True
            backoff_local = 1.0
            try:
                self._send_subscribe()
            except Exception as e:
                log.debug("[BTC15WSS] subscribe failed: %s", e)

            def pinger():
                while not self._stop.is_set():
                    try:
                        ws.send("PING")
                    except Exception:
                        return
                    time.sleep(10)

            threading.Thread(target=pinger, name="btc15-wss-ping", daemon=True).start()
            return backoff_local

        def on_message(ws, message: str):
            if not message:
                return
            if message == "PONG" or message == "PING":
                return
            with self._lock:
                self._last_message_ts = time.time()
            try:
                msg = json.loads(message)
            except Exception:
                return
            if isinstance(msg, dict):
                self.cache.apply_market_event(msg)

        def on_error(ws, error):
            log.debug("[BTC15WSS] error: %s", error)

        def on_close(ws, close_status_code, close_msg):
            log.debug("[BTC15WSS] closed: %s %s", close_status_code, close_msg)
            with self._lock:
                self._connected = False

        while not self._stop.is_set():
            try:
                ws = WebSocketApp(url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
                self._ws = ws
                ws.run_forever()
            except Exception as e:
                log.debug("[BTC15WSS] run_forever failed: %s", e)

            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 20.0)


_ws_singleton: Optional[CLOBMarketWSSubscriber] = None


def get_btc15_market_ws() -> CLOBMarketWSSubscriber:
    global _ws_singleton
    if _ws_singleton is None:
        _ws_singleton = CLOBMarketWSSubscriber()
    return _ws_singleton
