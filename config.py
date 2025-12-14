"""Application-wide configuration values.

These defaults keep runtime behavior predictable while allowing
individual modules to import a single source of truth. Adjust values via
environment variables before packaging for production if needed.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List

# Load .env file from project root if it exists
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    def _strip_inline_comment(raw_value: str) -> str:
        # Remove trailing inline comments like: VALUE  # comment
        # Preserve literal '#' when not preceded by whitespace.
        in_single = False
        in_double = False
        for i, ch in enumerate(raw_value):
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif ch == "#" and not in_single and not in_double:
                if i == 0 or raw_value[i - 1].isspace():
                    return raw_value[:i].rstrip()
        return raw_value.strip()

    with open(_env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            if line.startswith("export "):
                line = line[len("export ") :].lstrip()

            if "=" not in line:
                continue

            key, _, value = line.partition("=")
            key = key.strip()
            value = _strip_inline_comment(value.strip())

            if len(value) >= 2 and ((value[0] == value[-1] == '"') or (value[0] == value[-1] == "'")):
                value = value[1:-1]

            if key:
                os.environ.setdefault(key, value)


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"Environment variable {name} must be a float")


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"Environment variable {name} must be an int")


def _list(name: str, default: List[str]) -> List[str]:
    value = os.getenv(name)
    if value is None:
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


SCAN_INTERVAL = _int("SCAN_INTERVAL", 30)
ARB_THRESHOLD = _float("ARB_THRESHOLD", 0.99)
CHEAP_BUY_THRESHOLD = _float("CHEAP_BUY_THRESHOLD", 0.35)
MARKETS_TO_WATCH = _list("MARKETS_TO_WATCH", [])

BANKR_EXECUTOR_URL = os.getenv("BANKR_EXECUTOR_URL", "http://localhost:4000")
MY_BANKR_WALLET = os.getenv("MY_BANKR_WALLET", "0xYOUR_WALLET_OPTIONAL")
BANKR_DRY_RUN = os.getenv("BANKR_DRY_RUN", "true").lower() in {"1", "true", "yes"}
ENABLE_BANKR_EXECUTOR = os.getenv("ENABLE_BANKR_EXECUTOR", "true").lower() in {"1", "true", "yes"}
MAX_BANKR_COMMANDS_PER_LOOP = _int("MAX_BANKR_COMMANDS_PER_LOOP", 3)

# ─────────────────────────────────────────────────────────────
# Rate limiting & quality filters (prevent spam, focus on high EV)
# ─────────────────────────────────────────────────────────────

# How often the main loop should run (seconds)
LOOP_SLEEP_SECONDS = _int("LOOP_SLEEP_SECONDS", 5)

# Hard cap on how many Bankr commands per hour (0 = no limit)
BANKR_MAX_COMMANDS_PER_HOUR = _int("BANKR_MAX_COMMANDS_PER_HOUR", 30)

# Minimum time between Bankr prompts (seconds)
BANKR_MIN_SECONDS_BETWEEN_PROMPTS = _int("BANKR_MIN_SECONDS_BETWEEN_PROMPTS", 10)

# Per-market cooldown: don't re-ask Bankr about same market within this window (seconds)
MARKET_COOLDOWN_SECONDS = _int("MARKET_COOLDOWN_SECONDS", 90)

# Minimum edge in basis points to even consider sending to Bankr (e.g. 30 = 0.30%)
MIN_EDGE_BPS = _float("MIN_EDGE_BPS", 30.0)

# Max opportunities to execute per loop (even if more qualify)
MAX_OPS_PER_LOOP = _int("MAX_OPS_PER_LOOP", 1)

# ─────────────────────────────────────────────────────────────
# Stake sizes (must be <= sidecar's BANKR_MAX_USDC_PER_PROMPT)
# ─────────────────────────────────────────────────────────────

# USDC per side for arb trades (total = 2x this for YES+NO)
ARB_STAKE_USDC = _float("ARB_STAKE_USDC", 2.0)

# USDC for hedge/cheap-buy trades
HEDGE_STAKE_USDC = _float("HEDGE_STAKE_USDC", 2.0)

# ─────────────────────────────────────────────────────────────
# Exit Manager settings (automatic TP/SL/max-hold/nightly-flatten)
# ─────────────────────────────────────────────────────────────

# Take-profit threshold: exit when P&L % >= this (e.g. 2.2 = 2.2% profit)
TAKE_PROFIT_PCT = _float("TAKE_PROFIT_PCT", 2.2)

# Stop-loss threshold: exit when P&L % <= this (e.g. -0.95 = -0.95% loss)
STOP_LOSS_PCT = _float("STOP_LOSS_PCT", -0.95)

# Max hold time: exit if position is older than this many hours
MAX_HOLD_HOURS = _float("MAX_HOLD_HOURS", 16.0)

# Auto-flatten hour (UTC): flatten all positions at this hour (0-23), set to -1 to disable
AUTO_FLATTEN_HOUR_UTC = _int("AUTO_FLATTEN_HOUR_UTC", 23)

# How often the exit manager checks positions (seconds)
EXIT_LOOP_SLEEP_SECONDS = _int("EXIT_LOOP_SLEEP_SECONDS", 45)

# Exit manager dry-run mode: if true, log exits but don't execute them
EXIT_MANAGER_DRY_RUN = os.getenv("EXIT_MANAGER_DRY_RUN", "false").lower() in {"1", "true", "yes"}

# ─────────────────────────────────────────────────────────────
# Kalshi cross-arb settings
# ─────────────────────────────────────────────────────────────

ENABLE_KALSHI_ARB = os.getenv("ENABLE_KALSHI_ARB", "false").lower() in {"1", "true", "yes"}
KALSHI_API_KEY = os.getenv("KALSHI_API_KEY", "")
KALSHI_API_SECRET = os.getenv("KALSHI_API_SECRET", "")


# ─────────────────────────────────────────────────────────────
# BTC 15-minute Up/Down Loop Strategy Configuration
# ─────────────────────────────────────────────────────────────

from typing import NamedTuple


class BTC15Config(NamedTuple):
    """Configuration for the BTC 15-minute Up/Down loop strategy."""
    enabled: bool
    market_substr: str
    min_volume_usdc: float
    cheap_side_trigger_max: float
    target_avg_max: float
    max_bracket_usdc: float
    min_total_edge_cents: float
    max_time_to_hedge_sec: int
    min_orderbook_liq_usdc: float
    max_open_brackets: int
    cooldown_sec: int
    daily_max_loss: float
    max_losses_before_pause: int
    force_test_slug: str  # Dev-only: force match on specific slug for testing


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes"}


BTC15_CONFIG = BTC15Config(
    enabled=_bool("BTC15_ENABLED", True),
    market_substr=os.getenv("BTC15_MARKET_SUBSTR", "btc-up-or-down-15m"),
    min_volume_usdc=_float("BTC15_MIN_VOLUME_USDC", 100.0),  # Lower for BTC 15m markets
    cheap_side_trigger_max=_float("BTC15_CHEAP_SIDE_TRIGGER_MAX", 0.35),
    target_avg_max=_float("BTC15_TARGET_AVG_MAX", 0.30),
    max_bracket_usdc=_float("BTC15_MAX_BRACKET_USDC", 40.0),
    min_total_edge_cents=_float("BTC15_MIN_TOTAL_EDGE_CENTS", 1.0),
    max_time_to_hedge_sec=_int("BTC15_MAX_TIME_TO_HEDGE_SEC", 600),
    min_orderbook_liq_usdc=_float("BTC15_MIN_ORDERBOOK_LIQ_USDC", 50.0),  # Lower for BTC 15m
    max_open_brackets=_int("BTC15_MAX_OPEN_BRACKETS", 3),
    cooldown_sec=_int("BTC15_COOLDOWN_SEC", 300),
    daily_max_loss=_float("BTC15_DAILY_MAX_LOSS", 50.0),
    max_losses_before_pause=_int("BTC15_MAX_LOSSES_BEFORE_PAUSE", 3),
    force_test_slug=os.getenv("BTC15_FORCE_TEST_SLUG", ""),  # e.g. "some-liquid-market-slug"
)
