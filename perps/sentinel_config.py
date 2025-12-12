"""
Sentinel Configuration - Per-asset settings for the Local Scout → Bankr Sniper architecture

Each asset has:
- Zone thresholds (what % of daily range triggers a signal)
- Cooldown (don't fire twice in this window)
- Max size, leverage, daily loss
- Optional filters (funding, OI, trend)
"""

import os
from dataclasses import dataclass, field
from typing import Dict, Literal


@dataclass
class AssetSentinelConfig:
    """Configuration for one asset's sentinel logic"""
    
    # Asset identification
    symbol: str                          # e.g., "BTC-PERP", "ETH-PERP"
    enabled: bool = True
    
    # Zone thresholds (0-1 scale, where 0=daily low, 1=daily high)
    top_zone: float = 0.96               # Price in top 4% → SHORT candidate
    bottom_zone: float = 0.04            # Price in bottom 4% → LONG candidate
    
    # Blowoff protection (don't fade if price is ripping way beyond range)
    max_above_high_pct: float = 0.001    # 0.1% above daily high = too extended
    max_below_low_pct: float = 0.001     # 0.1% below daily low = too extended
    
    # Minimum range size (don't trade if range is tiny - low vol day)
    min_range_pct: float = 0.5           # Range must be at least 0.5% of price
    
    # Cooldown between signals (same direction)
    cooldown_minutes: int = 60
    
    # Risk limits
    max_usdc_per_trade: float = 300.0
    max_leverage: float = 4.0
    max_daily_loss: float = 200.0
    
    # Trend filter (require price vs MA alignment)
    require_trend_filter: bool = True    # If True, check MA alignment
    
    def __post_init__(self):
        # Validate thresholds
        if not 0 < self.top_zone <= 1:
            raise ValueError(f"top_zone must be between 0 and 1, got {self.top_zone}")
        if not 0 <= self.bottom_zone < 1:
            raise ValueError(f"bottom_zone must be between 0 and 1, got {self.bottom_zone}")


# ═══════════════════════════════════════════════════════════════════════════════
# DEFAULT ASSET CONFIGURATIONS
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_CONFIGS: Dict[str, AssetSentinelConfig] = {
    "BTC-PERP": AssetSentinelConfig(
        symbol="BTC-PERP",
        enabled=True,
        # TRAINING WHEELS: Start strict, loosen after 24h DRY_RUN looks good
        top_zone=0.97,                   # Top 3% of range → very near daily high
        bottom_zone=0.03,                # Bottom 3% → very near daily low
        min_range_pct=1.5,               # BTC needs decent volatility (1.5%+ range)
        cooldown_minutes=120,            # 2h between same-direction (avoid overtrading)
        max_usdc_per_trade=75.0,         # TINY size for training wheels
        max_leverage=2.0,                # Very conservative leverage
        max_daily_loss=50.0,             # Tight daily cap
        require_trend_filter=True,       # Must align with trend
    ),
    "ETH-PERP": AssetSentinelConfig(
        symbol="ETH-PERP",
        enabled=True,
        # TRAINING WHEELS: Start strict, loosen after 24h DRY_RUN looks good
        top_zone=0.965,                  # Top 3.5% of range
        bottom_zone=0.035,               # Bottom 3.5%
        min_range_pct=2.0,               # ETH more volatile, need 2%+ range
        cooldown_minutes=90,             # 1.5h cooldown
        max_usdc_per_trade=50.0,         # TINY size for training wheels
        max_leverage=2.0,                # Very conservative leverage
        max_daily_loss=40.0,             # Tight daily cap
        require_trend_filter=True,       # Must align with trend
    ),
    "SOL-PERP": AssetSentinelConfig(
        symbol="SOL-PERP",
        enabled=False,                   # Disabled by default - higher risk
        top_zone=0.94,
        bottom_zone=0.06,
        min_range_pct=1.5,               # SOL needs more vol
        cooldown_minutes=45,
        max_usdc_per_trade=150.0,
        max_leverage=3.0,
        max_daily_loss=100.0,
        require_trend_filter=True,
    ),
}


def get_config(symbol: str) -> AssetSentinelConfig:
    """Get config for an asset, with env overrides"""
    if symbol not in DEFAULT_CONFIGS:
        raise ValueError(f"No sentinel config for {symbol}")
    
    config = DEFAULT_CONFIGS[symbol]
    
    # Allow env overrides (e.g., SENTINEL_BTC_TOP_ZONE=0.97)
    prefix = f"SENTINEL_{symbol.replace('-', '_').upper()}"
    
    if os.getenv(f"{prefix}_ENABLED"):
        config.enabled = os.getenv(f"{prefix}_ENABLED", "").lower() == "true"
    if os.getenv(f"{prefix}_TOP_ZONE"):
        config.top_zone = float(os.getenv(f"{prefix}_TOP_ZONE"))
    if os.getenv(f"{prefix}_BOTTOM_ZONE"):
        config.bottom_zone = float(os.getenv(f"{prefix}_BOTTOM_ZONE"))
    if os.getenv(f"{prefix}_COOLDOWN_MINUTES"):
        config.cooldown_minutes = int(os.getenv(f"{prefix}_COOLDOWN_MINUTES"))
    if os.getenv(f"{prefix}_MAX_USDC"):
        config.max_usdc_per_trade = float(os.getenv(f"{prefix}_MAX_USDC"))
    if os.getenv(f"{prefix}_MAX_LEVERAGE"):
        config.max_leverage = float(os.getenv(f"{prefix}_MAX_LEVERAGE"))
    
    return config


def get_enabled_symbols() -> list:
    """Get list of enabled symbols"""
    return [s for s, c in DEFAULT_CONFIGS.items() if c.enabled]


# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL SENTINEL SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════

# How often to check prices (seconds) - 30s default to avoid rate limits
SENTINEL_LOOP_INTERVAL = int(os.getenv("SENTINEL_LOOP_INTERVAL", "30"))

# Global dry run (overrides per-asset)
SENTINEL_DRY_RUN = os.getenv("SENTINEL_DRY_RUN", "true").lower() == "true"

# Context wallet for Avantis execution
CONTEXT_WALLET = os.getenv("BANKR_CONTEXT_WALLET", "")

# Global daily loss cap (stops all trading if exceeded)
GLOBAL_DAILY_LOSS_CAP = float(os.getenv("SENTINEL_GLOBAL_DAILY_LOSS_CAP", "500"))

# Price feed source
PRICE_FEED_SOURCE = os.getenv("PRICE_FEED_SOURCE", "coingecko")  # coingecko, binance, avantis


if __name__ == "__main__":
    print("=== Sentinel Configuration ===\n")
    print(f"Loop Interval: {SENTINEL_LOOP_INTERVAL}s")
    print(f"Dry Run: {SENTINEL_DRY_RUN}")
    print(f"Global Daily Loss Cap: ${GLOBAL_DAILY_LOSS_CAP}")
    print(f"Price Feed: {PRICE_FEED_SOURCE}")
    print(f"\nEnabled Symbols: {get_enabled_symbols()}")
    
    print("\n=== Per-Asset Configs ===")
    for symbol, config in DEFAULT_CONFIGS.items():
        status = "✅" if config.enabled else "❌"
        print(f"\n{status} {symbol}")
        print(f"   Zones: SHORT if pos >= {config.top_zone}, LONG if pos <= {config.bottom_zone}")
        print(f"   Cooldown: {config.cooldown_minutes}min")
        print(f"   Max: ${config.max_usdc_per_trade} @ {config.max_leverage}x")
