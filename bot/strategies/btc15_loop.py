"""
BTC 15-Minute Up/Down Loop Strategy

A dedicated mean-reversion strategy for BTC 15-minute prediction markets.
Enters on cheap sides, hedges for arb profit or flattens before expiry.

Key concepts:
- "Bracket" = one market instance (e.g., "Will BTC go up in next 15min?")
- Enter on cheap side when ask <= trigger threshold
- Hedge by buying opposite side when total edge >= min_edge_cents
- Flatten near expiry if hedge wasn't possible

This module plugs into the main bot loop and uses the existing Bankr executor.
"""

import logging
import os
import time
import requests
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional, Dict, Any, NamedTuple

# Import config - handle both direct run and module import
try:
    from config import BTC15_CONFIG, BANKR_EXECUTOR_URL, BANKR_DRY_RUN
except ImportError:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from config import BTC15_CONFIG, BANKR_EXECUTOR_URL, BANKR_DRY_RUN


# ═══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════════════

def is_candidate_btc15_market(m: dict) -> bool:
    """
    Check if a market is a candidate for BTC 15-minute Up/Down strategy.
    
    This detects real BTC 15m markets based on question/slug patterns.
    Works independently of volume/expiry filters.
    
    Known patterns:
    - btc-updown-15m-{timestamp} (Polymarket's actual 15m BTC markets)
    - "Bitcoin Up or Down" in question
    - "btc" + "15m" or "15 min" patterns
    """
    q = (m.get("question") or "").lower()
    slug = (m.get("slug") or "").lower()
    text = slug + " " + q
    
    # STRONG MATCH: Polymarket's actual btc-updown-15m slug pattern
    # e.g., btc-updown-15m-1765405800
    if "btc-updown-15m" in slug:
        return True
    
    # STRONG MATCH: "Bitcoin Up or Down" in question
    if "bitcoin up or down" in q:
        return True
    
    # Must be BTC/Bitcoin related for pattern matching
    is_btc = "bitcoin" in text or "btc" in text
    if not is_btc:
        return False
    
    # Check for updown + time patterns
    has_updown = any(p in text for p in ["updown", "up or down", "up-or-down", "up / down"])
    has_15m = any(p in text for p in ["15m", "15 min", "15-min", "15 minute", "15-minute"])
    
    if has_updown and has_15m:
        return True
    
    # Fallback: check for other 15m patterns
    is_15m_pattern = any(pattern in text for pattern in [
        "next 15", "in 15",
        "higher than now", "lower than now",
    ])
    
    return is_btc and is_15m_pattern


@dataclass
class BracketState:
    """State for a single BTC 15m bracket (market instance)."""
    last_entry_ts: Optional[datetime] = None
    unhedged_side: Optional[str] = None  # "UP" or "DOWN"
    unhedged_cost: float = 0.0
    unhedged_size: float = 0.0  # number of shares
    losses_in_row: int = 0
    trade_id: Optional[int] = None  # Reference to btc15_trades row


@dataclass
class SidePrices:
    """Price info for one side of a market."""
    bid: float
    ask: float
    liq_usdc: float


class BTC15Config(NamedTuple):
    """Re-export for type hints (actual loaded from config.py)."""
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
    force_test_slug: str = ""  # Dev-only: force match on specific slug for testing


# ═══════════════════════════════════════════════════════════════════════════════
# SIDECAR HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

SIDECAR_URL = os.getenv("SIDECAR_URL", BANKR_EXECUTOR_URL)


def _load_states_from_sidecar() -> Dict[str, BracketState]:
    """Load persisted BTC15 states from sidecar SQLite."""
    try:
        resp = requests.get(f"{SIDECAR_URL}/btc15/states", timeout=5)
        if resp.ok:
            data = resp.json()
            states = {}
            for row in data.get("states", []):
                states[row["slug"]] = BracketState(
                    last_entry_ts=datetime.fromisoformat(row["last_entry_ts"]) if row.get("last_entry_ts") else None,
                    unhedged_side=row.get("unhedged_side"),
                    unhedged_cost=row.get("unhedged_cost", 0.0),
                    unhedged_size=row.get("unhedged_size", 0.0),
                    losses_in_row=row.get("losses_in_row", 0),
                    trade_id=row.get("trade_id"),
                )
            return states
    except Exception as e:
        logging.warning("[BTC15] Failed to load states from sidecar: %s", e)
    return {}


def _save_state_to_sidecar(slug: str, state: BracketState) -> None:
    """Persist a BTC15 state to sidecar SQLite."""
    try:
        payload = {
            "slug": slug,
            "last_entry_ts": state.last_entry_ts.isoformat() if state.last_entry_ts else None,
            "unhedged_side": state.unhedged_side,
            "unhedged_cost": state.unhedged_cost,
            "unhedged_size": state.unhedged_size,
            "losses_in_row": state.losses_in_row,
            "trade_id": state.trade_id,
        }
        requests.post(f"{SIDECAR_URL}/btc15/state", json=payload, timeout=5)
    except Exception as e:
        logging.warning("[BTC15] Failed to save state to sidecar: %s", e)


def _delete_state_from_sidecar(slug: str) -> None:
    """Delete a BTC15 state from sidecar SQLite."""
    try:
        requests.delete(f"{SIDECAR_URL}/btc15/state/{slug}", timeout=5)
    except Exception as e:
        logging.warning("[BTC15] Failed to delete state from sidecar: %s", e)


# ═══════════════════════════════════════════════════════════════════════════════
# TRADE LOGGING (for stats & PnL)
# ═══════════════════════════════════════════════════════════════════════════════

def _open_btc15_trade(
    slug: str,
    market_label: str,
    entry_side: str,
    entry_price: float,
    size_shares: float,
    dry_run: bool = True,
) -> Optional[int]:
    """Record a new BTC15 bracket entry in the trades table. Returns trade_id."""
    try:
        payload = {
            "slug": slug,
            "market_label": market_label,
            "entry_side": entry_side,
            "entry_price": entry_price,
            "size_shares": size_shares,
            "opened_at": datetime.utcnow().isoformat(),
            "mode": "DRY_RUN" if dry_run else "LIVE",
        }
        resp = requests.post(f"{SIDECAR_URL}/btc15/trade-open", json=payload, timeout=5)
        if resp.ok:
            data = resp.json()
            return data.get("id")
    except Exception as e:
        logging.warning("[BTC15] Failed to open trade: %s", e)
    return None


def _hedge_btc15_trade(
    trade_id: int,
    hedge_side: str,
    hedge_price: float,
    hedge_cost: float,
) -> bool:
    """Record a hedge for an existing bracket."""
    try:
        payload = {
            "id": trade_id,
            "hedge_side": hedge_side,
            "hedge_price": hedge_price,
            "hedged_at": datetime.utcnow().isoformat(),
            "hedge_cost": hedge_cost,
        }
        resp = requests.post(f"{SIDECAR_URL}/btc15/trade-hedge", json=payload, timeout=5)
        return resp.ok
    except Exception as e:
        logging.warning("[BTC15] Failed to hedge trade: %s", e)
    return False


def _flatten_btc15_trade(
    trade_id: int,
    sale_proceeds: float,
) -> bool:
    """Record a flatten (early exit) for a bracket."""
    try:
        payload = {
            "id": trade_id,
            "sale_proceeds": sale_proceeds,
            "resolved_at": datetime.utcnow().isoformat(),
        }
        resp = requests.post(f"{SIDECAR_URL}/btc15/trade-flatten", json=payload, timeout=5)
        return resp.ok
    except Exception as e:
        logging.warning("[BTC15] Failed to flatten trade: %s", e)
    return False


def _resolve_btc15_trade(
    trade_id: int,
    payout: float,
) -> bool:
    """
    Resolve a completed bracket (hedged or expired).
    
    For hedged brackets: payout = size_shares * 1.0 (one side always wins)
    For DRY_RUN simulation: we pick winner based on final price and compute PnL.
    """
    try:
        payload = {
            "id": trade_id,
            "payout": payout,
            "resolved_at": datetime.utcnow().isoformat(),
        }
        resp = requests.post(f"{SIDECAR_URL}/btc15/trade-resolve", json=payload, timeout=5)
        if resp.ok:
            data = resp.json()
            logging.info("[BTC15] Trade %d resolved: PnL=$%.2f", trade_id, data.get("realized_pnl", 0))
            return True
    except Exception as e:
        logging.warning("[BTC15] Failed to resolve trade: %s", e)
    return False


def _log_activity(
    slug: str,
    market_label: str,
    action: str,
    side: str,
    size_usdc: float,
    price: float = 0.0,
    edge_cents: float = 0.0,
    dry_run: bool = True,
    result: str = "",
) -> None:
    """Log a BTC15 activity to sidecar."""
    try:
        payload = {
            "slug": slug,
            "market_label": market_label,
            "action": action,
            "side": side,
            "size_usdc": size_usdc,
            "price": price,
            "edge_cents": edge_cents,
            "dry_run": 1 if dry_run else 0,
            "result": result,
        }
        requests.post(f"{SIDECAR_URL}/btc15/activity", json=payload, timeout=5)
    except Exception as e:
        logging.warning("[BTC15] Failed to log activity: %s", e)


def _send_bankr_command(command: str, estimated_usdc: float, dry_run: bool = True) -> Optional[dict]:
    """Send a command to Bankr via sidecar."""
    try:
        payload = {
            "message": command,
            "dry_run": dry_run,
            "estimated_usdc": estimated_usdc,
        }
        resp = requests.post(
            f"{SIDECAR_URL}/prompt",
            json=payload,
            timeout=60,
        )
        if resp.ok:
            return resp.json()
        else:
            logging.warning("[BTC15] Bankr command failed: %s", resp.text[:200])
            return None
    except Exception as e:
        logging.error("[BTC15] Bankr request error: %s", e)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# BTC15 LOOP CLASS
# ═══════════════════════════════════════════════════════════════════════════════

class BTC15Loop:
    """
    BTC 15-minute Up/Down loop strategy.
    
    Called by main bot loop for each market. Decides whether to:
    - Enter a new bracket (buy cheap side)
    - Hedge an existing leg (buy opposite side for arb)
    - Flatten before expiry (exit at minimal loss)
    """

    def __init__(self, cfg, logger: logging.Logger = None):
        self.cfg = cfg
        self.log = logger or logging.getLogger(__name__)
        self.state_by_market: Dict[str, BracketState] = _load_states_from_sidecar()
        self.daily_loss: float = 0.0
        self.daily_reset_date: date = datetime.utcnow().date()
        self._last_command_time: float = 0.0

    def _reset_daily_if_needed(self) -> None:
        """Reset daily loss counter on new day."""
        today = datetime.utcnow().date()
        if today != self.daily_reset_date:
            self.log.info("[BTC15] New day - resetting daily loss counter")
            self.daily_loss = 0.0
            self.daily_reset_date = today

    def _get_state(self, slug: str) -> BracketState:
        """Get or create state for a market."""
        if slug not in self.state_by_market:
            self.state_by_market[slug] = BracketState()
        return self.state_by_market[slug]

    def _is_btc15_market(self, market: Dict[str, Any], volume_usdc: float) -> bool:
        """
        Check if a market is a BTC 15m Up/Down market.
        
        Criteria:
        - force_test_slug match (dev-only), OR
        - is_candidate_btc15_market() returns True with:
          - Volume >= min_volume_usdc (relaxed for btc-updown-15m pattern)
          - Time to expiry between 5 and 30 minutes (relaxed for btc-updown-15m pattern)
        """
        slug = str(market.get("slug", "")).lower()
        
        # Dev-only: force match on specific slug for testing (EXCLUSIVE mode)
        force_slug = getattr(self.cfg, 'force_test_slug', '') or ''
        if force_slug:
            # When force_test_slug is set, ONLY that slug is allowed
            if slug == force_slug.lower():
                self.log.info("[BTC15] ✅ FORCE_TEST_SLUG matched: %s", slug)
                return True
            else:
                # Skip all other markets when testing
                return False
        
        # STRONG MATCH: btc-updown-15m slug pattern (Polymarket's actual 15m BTC markets)
        # These get relaxed volume/expiry checks since they ARE the real 15m markets
        if "btc-updown-15m" in slug:
            # Skip volume check for btc-updown-15m - these are new and may have $0 volume
            # We want to monitor them for arb opportunities as liquidity builds
            self.log.info("[BTC15] ✅ BTC-UPDOWN-15M matched: %s (vol=$%.0f)", slug, volume_usdc)
            return True
            return True
        
        # Use the dedicated candidate detector for other patterns
        if not is_candidate_btc15_market(market):
            # Also check legacy substring match
            substr = self.cfg.market_substr.lower()
            label = str(market.get("question", "")).lower()
            if substr not in slug and substr not in label:
                return False
        
        # Check volume
        if volume_usdc < self.cfg.min_volume_usdc:
            self.log.debug("[BTC15] %s: volume %.0f < min %.0f", slug, volume_usdc, self.cfg.min_volume_usdc)
            return False
        
        # Check time to expiry (need 5-30 minutes for non-btc-updown patterns)
        minutes_to_expiry = market.get("minutes_to_expiry") or market.get("time_to_expiry_minutes", 999)
        if not (5 <= minutes_to_expiry <= 30):
            self.log.debug("[BTC15] %s: expiry %d mins not in 5-30 range", slug, minutes_to_expiry)
            return False
        
        self.log.info("[BTC15] ✅ Market MATCHED: %s (vol=$%.0f, expiry=%d min)", slug, volume_usdc, minutes_to_expiry)
        return True

    def _daily_loss_exceeded(self) -> bool:
        """Check if daily loss limit has been exceeded by querying sidecar stats."""
        try:
            resp = requests.get(f"{SIDECAR_URL}/btc15/stats", timeout=5)
            if resp.ok:
                data = resp.json()
                today_pnl = data.get("today", {}).get("realized_pnl", 0)
                if today_pnl <= -self.cfg.daily_max_loss:
                    self.log.warning("[BTC15] Daily loss cap hit: $%.2f <= -$%.2f", today_pnl, self.cfg.daily_max_loss)
                    return True
        except Exception as e:
            self.log.debug("[BTC15] Failed to check daily loss: %s", e)
        return False

    def _parse_prices(self, prices: Dict[str, Any]) -> Optional[Dict[str, SidePrices]]:
        """
        Parse prices dict into structured format.
        
        Expected input format:
        {
            "UP": {"bid": 0.45, "ask": 0.47, "liq_usdc": 15000},
            "DOWN": {"bid": 0.52, "ask": 0.54, "liq_usdc": 12000},
        }
        
        Or from outcomePrices array: [yes_price, no_price] where YES=UP, NO=DOWN
        """
        try:
            # If it's the dict format
            if isinstance(prices, dict) and "UP" in prices:
                return {
                    "UP": SidePrices(
                        bid=float(prices["UP"].get("bid", 0)),
                        ask=float(prices["UP"].get("ask", 0)),
                        liq_usdc=float(prices["UP"].get("liq_usdc", 0)),
                    ),
                    "DOWN": SidePrices(
                        bid=float(prices["DOWN"].get("bid", 0)),
                        ask=float(prices["DOWN"].get("ask", 0)),
                        liq_usdc=float(prices["DOWN"].get("liq_usdc", 0)),
                    ),
                }
            
            # If it's outcomePrices array [yes, no] - treat as [UP, DOWN]
            if isinstance(prices, (list, tuple)) and len(prices) >= 2:
                # For simple prices, assume ask = price, bid = price - 0.01
                up_price = float(prices[0])
                down_price = float(prices[1])
                return {
                    "UP": SidePrices(
                        bid=up_price - 0.01,
                        ask=up_price,
                        liq_usdc=self.cfg.min_orderbook_liq_usdc,  # Assume sufficient
                    ),
                    "DOWN": SidePrices(
                        bid=down_price - 0.01,
                        ask=down_price,
                        liq_usdc=self.cfg.min_orderbook_liq_usdc,
                    ),
                }
                
        except (TypeError, ValueError, KeyError) as e:
            self.log.debug("[BTC15] Failed to parse prices: %s", e)
        
        return None

    def _look_for_new_entry(
        self,
        market: Dict[str, Any],
        prices: Dict[str, SidePrices],
        state: BracketState,
    ) -> int:
        """
        Look for a new entry on the cheap side.
        
        Returns: 1 if Bankr command sent, 0 otherwise.
        """
        slug = market.get("slug", "unknown")
        label = market.get("question", slug)
        
        # Check liquidity on both sides
        if prices["UP"].liq_usdc < self.cfg.min_orderbook_liq_usdc:
            self.log.debug("[BTC15] %s: UP liq %.0f < min", slug, prices["UP"].liq_usdc)
            return 0
        if prices["DOWN"].liq_usdc < self.cfg.min_orderbook_liq_usdc:
            self.log.debug("[BTC15] %s: DOWN liq %.0f < min", slug, prices["DOWN"].liq_usdc)
            return 0
        
        # Find cheap side (lower ask)
        if prices["UP"].ask <= prices["DOWN"].ask:
            cheap_side = "UP"
            cheap_prices = prices["UP"]
            expensive_prices = prices["DOWN"]
        else:
            cheap_side = "DOWN"
            cheap_prices = prices["DOWN"]
            expensive_prices = prices["UP"]
        
        # CORE ARB CHECK: Only enter if YES+NO sum < threshold (e.g. 0.99)
        # This is the "printable arb" - if sum < 1, buying both sides guarantees profit
        price_sum = cheap_prices.ask + expensive_prices.ask
        max_sum_for_entry = 1.0 - (self.cfg.min_total_edge_cents / 100.0)  # e.g. 1.0 - 0.01 = 0.99
        
        if price_sum >= max_sum_for_entry:
            self.log.debug(
                "[BTC15] %s: sum %.3f >= %.3f (no arb opportunity)",
                slug, price_sum, max_sum_for_entry
            )
            return 0
        
        self.log.info(
            "[BTC15] 🎯 ARB DETECTED: %s sum=%.3f < %.3f (edge=%.1f¢)",
            slug, price_sum, max_sum_for_entry, (1.0 - price_sum) * 100
        )
        
        # Check if cheap enough
        if cheap_prices.ask > self.cfg.cheap_side_trigger_max:
            self.log.debug(
                "[BTC15] %s: cheap side %s ask %.3f > trigger %.3f",
                slug, cheap_side, cheap_prices.ask, self.cfg.cheap_side_trigger_max
            )
            return 0
        
        # Check spread (skip if too wide)
        spread = cheap_prices.ask - cheap_prices.bid
        if spread > 0.02:
            self.log.debug("[BTC15] %s: spread %.3f > 0.02", slug, spread)
            return 0
        
        # Calculate stake
        stake = min(self.cfg.max_bracket_usdc, cheap_prices.liq_usdc)
        if stake < 5:
            self.log.debug("[BTC15] %s: stake %.0f < 5", slug, stake)
            return 0
        
        # Build Bankr prompt
        dry_run = BANKR_DRY_RUN
        dry_tag = "[DRY RUN] " if dry_run else ""
        
        prompt = f"""{dry_tag}BTC 15-minute bracket entry.

Market: {label}
Slug: {slug}

Strategy: Mean-reversion entry on cheap side.
The {cheap_side} side is trading at {cheap_prices.ask:.3f} ask, which is below our {self.cfg.cheap_side_trigger_max:.2f} trigger.

ACTION: Buy {cheap_side} shares up to ${stake:.0f} USDC.
- Use limit orders only
- Target average price <= {self.cfg.target_avg_max:.2f}
- This is ONE LEG of a bracket - we will hedge the other side later

Do NOT buy the opposite side yet. Just acquire the cheap {cheap_side} position."""

        self.log.info("[BTC15] Entry signal: %s %s @ %.3f, stake $%.0f", slug, cheap_side, cheap_prices.ask, stake)
        
        result = _send_bankr_command(prompt, stake, dry_run=dry_run)
        
        if result:
            # Calculate approximate shares
            size_shares = stake / cheap_prices.ask
            
            # Open a trade record for stats/PnL tracking
            trade_id = _open_btc15_trade(
                slug=slug,
                market_label=label,
                entry_side=cheap_side,
                entry_price=cheap_prices.ask,
                size_shares=size_shares,
                dry_run=dry_run,
            )
            
            # Update state
            state.last_entry_ts = datetime.utcnow()
            state.unhedged_side = cheap_side
            state.unhedged_cost = stake  # Approximate
            state.unhedged_size = size_shares
            state.trade_id = trade_id
            _save_state_to_sidecar(slug, state)
            
            # Log activity
            _log_activity(
                slug=slug,
                market_label=label,
                action="ENTER_CHEAP_SIDE",
                side=cheap_side,
                size_usdc=stake,
                price=cheap_prices.ask,
                dry_run=dry_run,
                result="SENT",
            )
            
            self._last_command_time = time.time()
            return 1
        
        return 0

    def _manage_existing_leg(
        self,
        market: Dict[str, Any],
        prices: Dict[str, SidePrices],
        state: BracketState,
    ) -> int:
        """
        Manage an existing unhedged leg - either hedge for profit or flatten.
        
        Returns: 1 if Bankr command sent, 0 otherwise.
        """
        slug = market.get("slug", "unknown")
        label = market.get("question", slug)
        
        # Determine other side
        if state.unhedged_side == "UP":
            other_side = "DOWN"
            other_prices = prices["DOWN"]
        else:
            other_side = "UP"
            other_prices = prices["UP"]
        
        # Calculate potential locked profit
        # If we bought side A at cost C, and buy side B at price P*size,
        # total_cost = C + size * P
        # total_payout = size * 1.0 (one side always pays $1)
        other_cost = state.unhedged_size * other_prices.ask
        total_cost = state.unhedged_cost + other_cost
        total_payout = state.unhedged_size * 1.0
        edge_cents = (total_payout - total_cost) * 100
        
        self.log.debug(
            "[BTC15] %s: edge check - unhedged %s, cost $%.2f, other_cost $%.2f, edge %.1f¢",
            slug, state.unhedged_side, state.unhedged_cost, other_cost, edge_cents
        )
        
        dry_run = BANKR_DRY_RUN
        dry_tag = "[DRY RUN] " if dry_run else ""
        
        # Check hedge condition
        if edge_cents >= self.cfg.min_total_edge_cents:
            stake = other_cost
            
            prompt = f"""{dry_tag}BTC 15-minute bracket HEDGE.

Market: {label}
Slug: {slug}

Strategy: Lock in arb profit by buying opposite side.
We hold {state.unhedged_side} shares (cost: ${state.unhedged_cost:.2f}, size: {state.unhedged_size:.2f}).
The {other_side} side is now at {other_prices.ask:.3f}.

Projected edge: {edge_cents:.1f} cents profit per share.

ACTION: Buy {other_side} shares to match our {state.unhedged_side} position.
- Size: {state.unhedged_size:.2f} shares (approx ${stake:.0f} USDC)
- Use limit orders
- This completes the bracket - ONE side will pay $1 at settlement"""

            self.log.info("[BTC15] Hedge signal: %s %s @ %.3f, edge %.1f¢", slug, other_side, other_prices.ask, edge_cents)
            
            result = _send_bankr_command(prompt, stake, dry_run=dry_run)
            
            if result:
                # Record hedge in trades table
                if state.trade_id:
                    _hedge_btc15_trade(
                        trade_id=state.trade_id,
                        hedge_side=other_side,
                        hedge_price=other_prices.ask,
                        hedge_cost=stake,
                    )
                    
                    # Immediately resolve the bracket - one side WILL pay $1 at settlement
                    # Payout = size_shares * $1.00 (guaranteed)
                    payout = state.unhedged_size * 1.0
                    _resolve_btc15_trade(trade_id=state.trade_id, payout=payout)
                    
                    # Record win (hedged brackets are always profitable by design)
                    state.wins_in_row = getattr(state, 'wins_in_row', 0) + 1
                    state.losses_in_row = 0
                
                # Clear state (bracket complete)
                old_trade_id = state.trade_id
                state.unhedged_side = None
                state.unhedged_cost = 0.0
                state.unhedged_size = 0.0
                state.trade_id = None
                _save_state_to_sidecar(slug, state)
                
                _log_activity(
                    slug=slug,
                    market_label=label,
                    action="HEDGE",
                    side=other_side,
                    size_usdc=stake,
                    price=other_prices.ask,
                    edge_cents=edge_cents,
                    dry_run=dry_run,
                    result="SENT",
                )
                
                self._last_command_time = time.time()
                return 1
            
            return 0
        
        # Check timeout / near-expiry condition
        now = datetime.utcnow()
        elapsed_sec = (now - state.last_entry_ts).total_seconds() if state.last_entry_ts else 9999
        minutes_to_expiry = market.get("minutes_to_expiry") or market.get("time_to_expiry_minutes", 999)
        
        should_flatten = (
            elapsed_sec > self.cfg.max_time_to_hedge_sec or
            minutes_to_expiry <= 5
        )
        
        if should_flatten:
            stake = self.cfg.max_bracket_usdc
            
            prompt = f"""{dry_tag}BTC 15-minute bracket FLATTEN.

Market: {label}
Slug: {slug}

Strategy: Exit unhedged position before expiry (timeout or near-expiry).
We hold {state.unhedged_side} shares (cost: ${state.unhedged_cost:.2f}, size: {state.unhedged_size:.2f}).
Elapsed: {elapsed_sec:.0f}s, Minutes to expiry: {minutes_to_expiry}

The hedge window has passed. Flatten to minimize loss.

ACTION: Sell our {state.unhedged_side} position.
- Try to get breakeven or minimal loss
- Use limit orders initially, market if needed
- Exit the position before settlement"""

            self.log.info("[BTC15] Flatten signal: %s %s, elapsed %.0fs, expiry %d min", 
                         slug, state.unhedged_side, elapsed_sec, minutes_to_expiry)
            
            result = _send_bankr_command(prompt, stake, dry_run=dry_run)
            
            if result:
                # Record flatten in trades table (assume minimal recovery)
                if state.trade_id:
                    # Estimate sale proceeds as ~50% of cost (conservative)
                    sale_proceeds = state.unhedged_cost * 0.5
                    _flatten_btc15_trade(
                        trade_id=state.trade_id,
                        sale_proceeds=sale_proceeds,
                    )
                
                # Clear state
                old_side = state.unhedged_side
                state.unhedged_side = None
                state.unhedged_cost = 0.0
                state.unhedged_size = 0.0
                state.losses_in_row += 1  # Assume loss on flatten
                state.trade_id = None
                _save_state_to_sidecar(slug, state)
                
                _log_activity(
                    slug=slug,
                    market_label=label,
                    action="FLATTEN",
                    side=old_side or "UNKNOWN",
                    size_usdc=stake,
                    dry_run=dry_run,
                    result="SENT",
                )
                
                self._last_command_time = time.time()
                return 1
        
        return 0

    def process_market(
        self,
        market: Dict[str, Any],
        prices: Any,
        volume_usdc: float,
    ) -> int:
        """
        Process a single market for BTC15 strategy.
        
        Called by main bot loop for each market.
        
        Args:
            market: Market dict with slug, question, minutes_to_expiry, etc.
            prices: Price data (dict with UP/DOWN or list [yes, no])
            volume_usdc: Market volume in USDC
            
        Returns:
            Number of Bankr commands sent (0 or 1)
        """
        # Reset daily counters
        self._reset_daily_if_needed()
        
        # Check if strategy is enabled
        if not self.cfg.enabled:
            return 0
        
        # Check daily loss limit (in-memory)
        if self.daily_loss <= -self.cfg.daily_max_loss:
            self.log.warning("[BTC15] ⏸️ Daily loss limit reached (in-memory: $%.2f)", self.daily_loss)
            return 0
        
        # Check daily loss limit (from sidecar DB - actual realized PnL)
        if self._daily_loss_exceeded():
            self.log.warning("[BTC15] ⏸️ Daily loss cap exceeded (from sidecar stats)")
            return 0
        
        # Check if this is a BTC15 market
        if not self._is_btc15_market(market, volume_usdc):
            return 0
        
        slug = market.get("slug", "unknown")
        state = self._get_state(slug)
        
        # Check losses in a row (streak pause)
        if state.losses_in_row >= self.cfg.max_losses_before_pause:
            self.log.warning("[BTC15] ⏸️ %s: PAUSED after %d consecutive losses", slug, state.losses_in_row)
            return 0
        
        # Check max open brackets
        open_brackets = sum(1 for s in self.state_by_market.values() if s.unhedged_side)
        if open_brackets >= self.cfg.max_open_brackets and not state.unhedged_side:
            self.log.debug("[BTC15] Max open brackets (%d) reached", self.cfg.max_open_brackets)
            return 0
        
        # Check cooldown (for new entries only)
        if not state.unhedged_side and state.last_entry_ts:
            elapsed = (datetime.utcnow() - state.last_entry_ts).total_seconds()
            if elapsed < self.cfg.cooldown_sec:
                self.log.debug("[BTC15] %s: cooldown (%.0fs < %ds)", slug, elapsed, self.cfg.cooldown_sec)
                return 0
        
        # Parse prices
        parsed_prices = self._parse_prices(prices)
        if not parsed_prices:
            self.log.debug("[BTC15] %s: failed to parse prices", slug)
            return 0
        
        # Branch: manage existing leg or look for new entry
        if state.unhedged_side:
            return self._manage_existing_leg(market, parsed_prices, state)
        else:
            return self._look_for_new_entry(market, parsed_prices, state)


# ═══════════════════════════════════════════════════════════════════════════════
# CLI TEST
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")
    
    print("=== BTC15 Loop Strategy Test ===")
    print(f"Config: {BTC15_CONFIG}")
    print()
    
    # Create loop instance
    loop = BTC15Loop(BTC15_CONFIG)
    
    # Test with mock market
    mock_market = {
        "slug": "btc-up-or-down-15m-2024-12-10-12-00",
        "question": "Will BTC go UP or DOWN in the next 15 minutes?",
        "minutes_to_expiry": 12,
    }
    
    mock_prices = {
        "UP": {"bid": 0.28, "ask": 0.30, "liq_usdc": 25000},
        "DOWN": {"bid": 0.68, "ask": 0.70, "liq_usdc": 20000},
    }
    
    print("Testing with mock market...")
    result = loop.process_market(mock_market, mock_prices, volume_usdc=100000)
    print(f"Commands sent: {result}")
