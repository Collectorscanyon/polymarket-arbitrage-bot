"""
Price Feeds - Fetches price data for the sentinel

Supports multiple sources:
- CryptoCompare (primary - 100k calls/month, good rate limits)
- CoinGecko (backup only - heavy rate limits)
- Binance (geo-blocked in some regions)

Provides:
- Current price
- 24h high/low (rolling)
- Simple moving averages (1h, 4h approximations)
"""

import os
import time
import requests
from dataclasses import dataclass
from typing import Optional, Dict, Literal
from datetime import datetime, timedelta
from collections import deque


@dataclass
class PriceSnapshot:
    """Complete price snapshot for sentinel analysis"""
    symbol: str
    price: float
    high_24h: float
    low_24h: float
    change_24h_pct: float
    
    # Moving averages (approximations from recent price history)
    ma_1h: float
    ma_4h: float
    
    # Computed fields
    pos_in_range: float = 0.0  # 0 = at low, 1 = at high
    range_pct: float = 0.0     # Range as % of price
    
    # Metadata
    timestamp: str = ""
    source: str = ""
    wallet: str = ""
    
    def __post_init__(self):
        # Compute derived fields
        range_size = self.high_24h - self.low_24h
        if range_size > 0:
            self.pos_in_range = (self.price - self.low_24h) / range_size
            self.range_pct = (range_size / self.price) * 100
        
        if not self.timestamp:
            self.timestamp = datetime.utcnow().isoformat() + "Z"


# ═══════════════════════════════════════════════════════════════════════════════
# RATE LIMIT & CACHE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

# Cache with longer TTL to reduce API calls
_PRICE_CACHE: Dict[str, tuple] = {}  # symbol -> (timestamp, PriceSnapshot)
CACHE_TTL_SECONDS = 60  # 60 second cache - we don't need faster updates

# 429 backoff tracking - per API
_API_COOLDOWN: Dict[str, float] = {}  # api_name -> cooldown_until timestamp
COOLDOWN_DURATION = 300  # 5 minute cooldown after 429


def _is_on_cooldown(api_name: str) -> bool:
    """Check if an API is on cooldown from 429 errors"""
    cooldown_until = _API_COOLDOWN.get(api_name, 0)
    return time.time() < cooldown_until


def _set_cooldown(api_name: str):
    """Set cooldown for an API after 429 error"""
    _API_COOLDOWN[api_name] = time.time() + COOLDOWN_DURATION
    print(f"[PriceFeeds] {api_name} rate limited - cooling off for {COOLDOWN_DURATION}s")


def _get_cached(symbol: str) -> Optional[PriceSnapshot]:
    """Get cached snapshot if still valid"""
    if symbol in _PRICE_CACHE:
        cached_ts, cached_snap = _PRICE_CACHE[symbol]
        if time.time() - cached_ts < CACHE_TTL_SECONDS:
            return cached_snap
    return None


def _get_stale_cache(symbol: str) -> Optional[PriceSnapshot]:
    """Get stale cache as fallback (any age)"""
    if symbol in _PRICE_CACHE:
        return _PRICE_CACHE[symbol][1]
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# PRICE HISTORY FOR MA CALCULATION
# ═══════════════════════════════════════════════════════════════════════════════

PRICE_HISTORY: Dict[str, deque] = {}
MAX_HISTORY_SIZE = 1000


def _add_to_history(symbol: str, price: float, timestamp: float):
    """Add a price point to history"""
    if symbol not in PRICE_HISTORY:
        PRICE_HISTORY[symbol] = deque(maxlen=MAX_HISTORY_SIZE)
    PRICE_HISTORY[symbol].append((timestamp, price))


def _calculate_ma(symbol: str, lookback_seconds: int) -> float:
    """Calculate simple moving average from history"""
    if symbol not in PRICE_HISTORY or len(PRICE_HISTORY[symbol]) == 0:
        return 0.0
    
    history = PRICE_HISTORY[symbol]
    now = time.time()
    cutoff = now - lookback_seconds
    
    prices = [p for ts, p in history if ts >= cutoff]
    if not prices:
        prices = [p for _, p in history]
    
    return sum(prices) / len(prices) if prices else 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# COINGECKO FEED (BACKUP ONLY - strict rate limits)
# ═══════════════════════════════════════════════════════════════════════════════

COINGECKO_IDS = {
    "BTC-PERP": "bitcoin",
    "ETH-PERP": "ethereum",
    "SOL-PERP": "solana",
    "DEGEN-PERP": "degen-base",
    "BNKR-PERP": "bankr",
}

COINGECKO_BASE = "https://api.coingecko.com/api/v3"


def _fetch_coingecko(symbol: str) -> Optional[PriceSnapshot]:
    """
    Fetch from CoinGecko API - BACKUP ONLY.
    CoinGecko has strict rate limits (10-30 calls/min on free tier).
    Use CryptoCompare as primary instead.
    """
    cg_id = COINGECKO_IDS.get(symbol)
    if not cg_id:
        print(f"[PriceFeeds] No CoinGecko ID for {symbol}")
        return None
    
    # Check cache first
    cached = _get_cached(symbol)
    if cached:
        return cached
    
    # Check if we're on cooldown from 429
    if _is_on_cooldown("coingecko"):
        stale = _get_stale_cache(symbol)
        if stale:
            return stale
        return None
    
    try:
        url = f"{COINGECKO_BASE}/coins/{cg_id}?localization=false&tickers=false&community_data=false&developer_data=false"
        resp = requests.get(url, timeout=10)
        
        # Handle rate limit
        if resp.status_code == 429:
            _set_cooldown("coingecko")
            stale = _get_stale_cache(symbol)
            if stale:
                return stale
            return None
        
        resp.raise_for_status()
        data = resp.json()
        
        market = data.get("market_data", {})
        price = market.get("current_price", {}).get("usd", 0)
        high_24h = market.get("high_24h", {}).get("usd", price)
        low_24h = market.get("low_24h", {}).get("usd", price)
        change_24h = market.get("price_change_percentage_24h", 0)
        
        # Add to history for MA calculation
        now = time.time()
        _add_to_history(symbol, price, now)
        
        # Calculate MAs from history
        ma_1h = _calculate_ma(symbol, 3600)
        ma_4h = _calculate_ma(symbol, 14400)
        
        if ma_1h == 0:
            ma_1h = price
        if ma_4h == 0:
            ma_4h = price
        
        snap = PriceSnapshot(
            symbol=symbol,
            price=price,
            high_24h=high_24h,
            low_24h=low_24h,
            change_24h_pct=change_24h,
            ma_1h=ma_1h,
            ma_4h=ma_4h,
            source="coingecko",
            wallet=os.getenv("BANKR_CONTEXT_WALLET", ""),
        )
        
        # Cache the result
        _PRICE_CACHE[symbol] = (time.time(), snap)
        return snap
        
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 429:
            _set_cooldown("coingecko")
        print(f"[PriceFeeds] CoinGecko error for {symbol}: {e}")
        stale = _get_stale_cache(symbol)
        if stale:
            return stale
        return None
    except Exception as e:
        print(f"[PriceFeeds] CoinGecko error for {symbol}: {e}")
        stale = _get_stale_cache(symbol)
        if stale:
            return stale
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# CRYPTOCOMPARE FEED (PRIMARY - high rate limits, rolling 24h data)
# ═══════════════════════════════════════════════════════════════════════════════

CRYPTOCOMPARE_SYMBOLS = {
    "BTC-PERP": "BTC",
    "ETH-PERP": "ETH",
    "SOL-PERP": "SOL",
}

CRYPTOCOMPARE_BASE = "https://min-api.cryptocompare.com/data"


def _fetch_cryptocompare(symbol: str) -> Optional[PriceSnapshot]:
    """
    Fetch from CryptoCompare API using pricemultifull endpoint.
    This gives us real rolling 24h high/low, not just daily candle data.
    """
    cc_symbol = CRYPTOCOMPARE_SYMBOLS.get(symbol)
    if not cc_symbol:
        print(f"[PriceFeeds] No CryptoCompare symbol for {symbol}")
        return None
    
    # Check cache first
    cached = _get_cached(symbol)
    if cached:
        return cached
    
    # Check if we're on cooldown from 429
    if _is_on_cooldown("cryptocompare"):
        stale = _get_stale_cache(symbol)
        if stale:
            return stale
        return None
    
    try:
        # Use pricemultifull for rolling 24h data (HIGH24HOUR, LOW24HOUR)
        url = f"{CRYPTOCOMPARE_BASE}/pricemultifull?fsyms={cc_symbol}&tsyms=USD"
        resp = requests.get(url, timeout=10)
        
        # Handle rate limit
        if resp.status_code == 429:
            _set_cooldown("cryptocompare")
            stale = _get_stale_cache(symbol)
            if stale:
                return stale
            return None
        
        resp.raise_for_status()
        data = resp.json()
        
        # CryptoCompare returns 200 but with Response="Error" when rate limited
        if data.get("Response") == "Error":
            error_msg = data.get("Message", "Unknown error")
            if "rate limit" in error_msg.lower():
                _set_cooldown("cryptocompare")
            print(f"[PriceFeeds] CryptoCompare API error: {error_msg}")
            stale = _get_stale_cache(symbol)
            if stale:
                return stale
            return None
        
        raw = data.get("RAW", {}).get(cc_symbol, {}).get("USD", {})
        if not raw:
            return None
        
        price = float(raw.get("PRICE", 0))
        high_24h = float(raw.get("HIGH24HOUR", price))
        low_24h = float(raw.get("LOW24HOUR", price))
        change_24h = float(raw.get("CHANGEPCT24HOUR", 0))
        
        # Add to history for MA calculation
        now = time.time()
        _add_to_history(symbol, price, now)
        
        # Calculate MAs from history
        ma_1h = _calculate_ma(symbol, 3600)
        ma_4h = _calculate_ma(symbol, 14400)
        
        if ma_1h == 0:
            ma_1h = price
        if ma_4h == 0:
            ma_4h = price
        
        snap = PriceSnapshot(
            symbol=symbol,
            price=price,
            high_24h=high_24h,
            low_24h=low_24h,
            change_24h_pct=change_24h,
            ma_1h=ma_1h,
            ma_4h=ma_4h,
            source="cryptocompare",
            wallet=os.getenv("BANKR_CONTEXT_WALLET", ""),
        )
        
        # Cache the result
        _PRICE_CACHE[symbol] = (time.time(), snap)
        return snap
        
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 429:
            _set_cooldown("cryptocompare")
        print(f"[PriceFeeds] CryptoCompare error for {symbol}: {e}")
        stale = _get_stale_cache(symbol)
        if stale:
            return stale
        return None
    except Exception as e:
        print(f"[PriceFeeds] CryptoCompare error for {symbol}: {e}")
        stale = _get_stale_cache(symbol)
        if stale:
            return stale
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# COINBASE FEED (FALLBACK - no rate limits, but only spot price)
# ═══════════════════════════════════════════════════════════════════════════════

COINBASE_SYMBOLS = {
    "BTC-PERP": "BTC",
    "ETH-PERP": "ETH",
    "SOL-PERP": "SOL",
}

COINBASE_BASE = "https://api.coinbase.com/v2"

# Track our own 24h high/low since Coinbase only gives spot
_24H_TRACKING: Dict[str, Dict] = {}  # symbol -> {high: float, low: float, reset_ts: float}


def _fetch_coinbase(symbol: str) -> Optional[PriceSnapshot]:
    """
    Fetch from Coinbase API - FALLBACK option.
    No rate limits, but only gives spot price.
    We track our own 24h high/low from price history.
    """
    cb_symbol = COINBASE_SYMBOLS.get(symbol)
    if not cb_symbol:
        print(f"[PriceFeeds] No Coinbase symbol for {symbol}")
        return None
    
    # Check cache first
    cached = _get_cached(symbol)
    if cached:
        return cached
    
    try:
        url = f"{COINBASE_BASE}/prices/{cb_symbol}-USD/spot"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        price = float(data.get("data", {}).get("amount", 0))
        if price == 0:
            return None
        
        now = time.time()
        
        # Initialize or update 24h tracking
        if symbol not in _24H_TRACKING:
            _24H_TRACKING[symbol] = {"high": price, "low": price, "reset_ts": now}
        
        tracking = _24H_TRACKING[symbol]
        
        # Reset every 24 hours
        if now - tracking["reset_ts"] > 86400:
            tracking["high"] = price
            tracking["low"] = price
            tracking["reset_ts"] = now
        
        # Update high/low
        tracking["high"] = max(tracking["high"], price)
        tracking["low"] = min(tracking["low"], price)
        
        high_24h = tracking["high"]
        low_24h = tracking["low"]
        
        # Calculate change from 24h ago (approximate from tracking)
        change_24h = 0.0
        
        # Add to history for MA calculation
        _add_to_history(symbol, price, now)
        
        # Calculate MAs from history
        ma_1h = _calculate_ma(symbol, 3600)
        ma_4h = _calculate_ma(symbol, 14400)
        
        if ma_1h == 0:
            ma_1h = price
        if ma_4h == 0:
            ma_4h = price
        
        snap = PriceSnapshot(
            symbol=symbol,
            price=price,
            high_24h=high_24h,
            low_24h=low_24h,
            change_24h_pct=change_24h,
            ma_1h=ma_1h,
            ma_4h=ma_4h,
            source="coinbase",
            wallet=os.getenv("BANKR_CONTEXT_WALLET", ""),
        )
        
        # Cache the result
        _PRICE_CACHE[symbol] = (time.time(), snap)
        return snap
        
    except Exception as e:
        print(f"[PriceFeeds] Coinbase error for {symbol}: {e}")
        stale = _get_stale_cache(symbol)
        if stale:
            return stale
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# BINANCE FEED
# ═══════════════════════════════════════════════════════════════════════════════

BINANCE_SYMBOLS = {
    "BTC-PERP": "BTCUSDT",
    "ETH-PERP": "ETHUSDT",
    "SOL-PERP": "SOLUSDT",
}

BINANCE_BASE = "https://api.binance.com/api/v3"


def _fetch_binance(symbol: str) -> Optional[PriceSnapshot]:
    """Fetch from Binance API (faster, more reliable)"""
    bn_symbol = BINANCE_SYMBOLS.get(symbol)
    if not bn_symbol:
        print(f"[PriceFeeds] No Binance symbol for {symbol}, falling back to CoinGecko")
        return _fetch_coingecko(symbol)
    
    try:
        # Get 24h ticker
        url = f"{BINANCE_BASE}/ticker/24hr?symbol={bn_symbol}"
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        
        price = float(data.get("lastPrice", 0))
        high_24h = float(data.get("highPrice", price))
        low_24h = float(data.get("lowPrice", price))
        change_24h = float(data.get("priceChangePercent", 0))
        
        # Add to history for MA calculation
        now = time.time()
        _add_to_history(symbol, price, now)
        
        # Calculate MAs from history
        ma_1h = _calculate_ma(symbol, 3600)
        ma_4h = _calculate_ma(symbol, 14400)
        
        if ma_1h == 0:
            ma_1h = price
        if ma_4h == 0:
            ma_4h = price
        
        return PriceSnapshot(
            symbol=symbol,
            price=price,
            high_24h=high_24h,
            low_24h=low_24h,
            change_24h_pct=change_24h,
            ma_1h=ma_1h,
            ma_4h=ma_4h,
            source="binance",
            wallet=os.getenv("BANKR_CONTEXT_WALLET", ""),
        )
        
    except Exception as e:
        print(f"[PriceFeeds] Binance error for {symbol}: {e}")
        # Fall back to CoinGecko
        return _fetch_coingecko(symbol)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN INTERFACE
# ═══════════════════════════════════════════════════════════════════════════════

# Default to cryptocompare (higher rate limits than coingecko, no geo-block like binance)
FEED_SOURCE = os.getenv("PRICE_FEED_SOURCE", "cryptocompare")


def get_price_snapshot(symbol: str) -> Optional[PriceSnapshot]:
    """
    Get complete price snapshot for a symbol.
    
    Priority chain (each step only tried if previous fails):
    1. CryptoCompare (default, rolling 24h data)
    2. CoinGecko (good 24h data but strict rate limits)
    3. Coinbase (unlimited but only spot price, we track our own 24h)
    
    With 429 backoff, we avoid hammering rate-limited APIs.
    """
    result = None
    
    if FEED_SOURCE == "cryptocompare":
        result = _fetch_cryptocompare(symbol)
        if result is None:
            result = _fetch_coingecko(symbol)
        if result is None:
            result = _fetch_coinbase(symbol)  # Ultimate fallback
    elif FEED_SOURCE == "binance":
        result = _fetch_binance(symbol)
        if result is None:
            result = _fetch_cryptocompare(symbol)
        if result is None:
            result = _fetch_coinbase(symbol)
    elif FEED_SOURCE == "coingecko":
        result = _fetch_coingecko(symbol)
        if result is None:
            result = _fetch_cryptocompare(symbol)
        if result is None:
            result = _fetch_coinbase(symbol)
    elif FEED_SOURCE == "coinbase":
        result = _fetch_coinbase(symbol)
    else:
        # Default: try all in order
        result = _fetch_cryptocompare(symbol)
        if result is None:
            result = _fetch_coingecko(symbol)
        if result is None:
            result = _fetch_coinbase(symbol)
    
    return result


def get_btc_snapshot() -> Optional[PriceSnapshot]:
    """Convenience function for BTC"""
    return get_price_snapshot("BTC-PERP")


def get_eth_snapshot() -> Optional[PriceSnapshot]:
    """Convenience function for ETH"""
    return get_price_snapshot("ETH-PERP")


def get_all_snapshots(symbols: list = None) -> Dict[str, PriceSnapshot]:
    """Get snapshots for multiple symbols with rate limit protection"""
    if symbols is None:
        symbols = ["BTC-PERP", "ETH-PERP"]
    
    results = {}
    for i, symbol in enumerate(symbols):
        snap = get_price_snapshot(symbol)
        if snap:
            results[symbol] = snap
        
        # Small delay between symbols to respect rate limits
        # Only if we're using coingecko and there are more symbols to fetch
        if FEED_SOURCE == "coingecko" and i < len(symbols) - 1:
            time.sleep(2)  # 2 second delay between requests
    
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# CLI TEST
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=== Price Feeds Test ===\n")
    print(f"Feed Source: {FEED_SOURCE}\n")
    
    for symbol in ["BTC-PERP", "ETH-PERP"]:
        print(f"--- {symbol} ---")
        snap = get_price_snapshot(symbol)
        if snap:
            print(f"  Price: ${snap.price:,.2f}")
            print(f"  24h Range: ${snap.low_24h:,.2f} - ${snap.high_24h:,.2f}")
            print(f"  Range %: {snap.range_pct:.2f}%")
            print(f"  Position in Range: {snap.pos_in_range:.3f} (0=low, 1=high)")
            print(f"  MA 1h: ${snap.ma_1h:,.2f}")
            print(f"  MA 4h: ${snap.ma_4h:,.2f}")
            print(f"  24h Change: {snap.change_24h_pct:+.2f}%")
            print(f"  Source: {snap.source}")
        else:
            print("  Failed to fetch")
        print()
