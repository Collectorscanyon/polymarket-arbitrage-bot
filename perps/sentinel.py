"""
Price Sentinel - "Local Scout → Bankr Sniper" Architecture

Runs locally, watching prices 24/7 (cheap).
Only pings Bankr when we're in a "top/bottom danger zone".

Key principles:
1. Local sentinel watches for extremes (15s intervals, no API costs)
2. Only when at daily high/low zone → send ONE structured command to Bankr
3. Bankr decides whether to execute on Avantis
4. Cooldowns prevent spam (one signal per direction per hour)
"""

import os
import sys
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Literal

from .price_feeds import get_price_snapshot, PriceSnapshot
from .sentinel_config import (
    get_config,
    get_enabled_symbols,
    AssetSentinelConfig,
    SENTINEL_LOOP_INTERVAL,
    SENTINEL_DRY_RUN,
    CONTEXT_WALLET,
    GLOBAL_DAILY_LOSS_CAP,
)
from bot.sidecar_client import SidecarClient


Direction = Literal["LONG", "SHORT"]


class Sentinel:
    """
    Price sentinel for one or more assets.
    
    Watches prices locally and only fires to Bankr when:
    - Price is in top/bottom zone of daily range
    - Cooldown has passed
    - Trend filter passes (price vs MA alignment)
    """
    
    def __init__(self, symbols: list = None, dry_run: bool = None):
        self.symbols = symbols or get_enabled_symbols()
        self.dry_run = dry_run if dry_run is not None else SENTINEL_DRY_RUN
        self.client = SidecarClient()
        
        # Track last signal time per symbol + direction
        self.last_signal: Dict[str, Dict[str, Optional[datetime]]] = {}
        for symbol in self.symbols:
            self.last_signal[symbol] = {"LONG": None, "SHORT": None}
        
        # Track daily realized loss for global cap
        self.daily_realized_loss = 0.0
        self.last_loss_reset_date = datetime.utcnow().date()
        
        print(f"[Sentinel] Initialized for {self.symbols}")
        print(f"[Sentinel] Dry Run: {self.dry_run}")
        print(f"[Sentinel] Wallet: {CONTEXT_WALLET[:10]}..." if CONTEXT_WALLET else "[Sentinel] WARNING: No wallet configured!")
    
    def _reset_daily_loss_if_needed(self):
        """Reset daily loss tracker at midnight UTC"""
        today = datetime.utcnow().date()
        if today != self.last_loss_reset_date:
            self.daily_realized_loss = 0.0
            self.last_loss_reset_date = today
            print(f"[Sentinel] Daily loss reset for {today}")
    
    def _cooldown_ok(self, symbol: str, direction: Direction) -> bool:
        """Check if cooldown has passed for this symbol + direction"""
        config = get_config(symbol)
        last = self.last_signal[symbol][direction]
        
        if last is None:
            return True
        
        elapsed = datetime.utcnow() - last
        return elapsed >= timedelta(minutes=config.cooldown_minutes)
    
    def _mark_signal(self, symbol: str, direction: Direction):
        """Record that we just fired a signal"""
        self.last_signal[symbol][direction] = datetime.utcnow()
    
    def _check_short_setup(self, snap: PriceSnapshot, config: AssetSentinelConfig) -> Optional[str]:
        """
        Check if we have a SHORT setup (fade the top).
        
        Returns reason string if signal should fire, None otherwise.
        """
        # Zone check: price in top X% of daily range
        if snap.pos_in_range < config.top_zone:
            return None
        
        # Blowoff protection: not way above the high
        max_price = snap.high_24h * (1 + config.max_above_high_pct)
        if snap.price > max_price:
            return None  # Too extended, don't fade
        
        # Trend filter: we're in an uptrend (fading extension, not catching a falling knife)
        if config.require_trend_filter and snap.price <= snap.ma_4h:
            return None  # Price below 4h MA, not a fade setup
        
        # Cooldown check
        if not self._cooldown_ok(snap.symbol, "SHORT"):
            return None
        
        return f"Top zone fade: pos={snap.pos_in_range:.3f}, price ${snap.price:,.2f} near daily high ${snap.high_24h:,.2f}"
    
    def _check_long_setup(self, snap: PriceSnapshot, config: AssetSentinelConfig) -> Optional[str]:
        """
        Check if we have a LONG setup (fade the bottom).
        
        Returns reason string if signal should fire, None otherwise.
        """
        # Zone check: price in bottom X% of daily range
        if snap.pos_in_range > config.bottom_zone:
            return None
        
        # Blowoff protection: not way below the low
        min_price = snap.low_24h * (1 - config.max_below_low_pct)
        if snap.price < min_price:
            return None  # Nuking through, don't catch
        
        # Trend filter: we're in a downtrend (buying washout, not chasing a pump)
        if config.require_trend_filter and snap.price >= snap.ma_4h:
            return None  # Price above 4h MA, not a washout buy
        
        # Cooldown check
        if not self._cooldown_ok(snap.symbol, "LONG"):
            return None
        
        return f"Bottom zone buy: pos={snap.pos_in_range:.3f}, price ${snap.price:,.2f} near daily low ${snap.low_24h:,.2f}"
    
    def _build_bankr_command(
        self,
        direction: Direction,
        snap: PriceSnapshot,
        config: AssetSentinelConfig,
        reason: str,
    ) -> dict:
        """Build the structured command to send to Bankr"""
        return {
            "mode": "perp_sentinel",
            "venue": "avantis",
            "wallet": CONTEXT_WALLET,
            "symbol": snap.symbol,
            "suggested_direction": direction,
            "max_leverage": config.max_leverage,
            "max_usdc_per_trade": config.max_usdc_per_trade,
            "max_daily_loss": config.max_daily_loss,
            "context": {
                "price": snap.price,
                "high_24h": snap.high_24h,
                "low_24h": snap.low_24h,
                "pos_in_range": snap.pos_in_range,
                "range_pct": snap.range_pct,
                "ma_1h": snap.ma_1h,
                "ma_4h": snap.ma_4h,
                "change_24h_pct": snap.change_24h_pct,
                "timestamp": snap.timestamp,
                "reason_from_sentinel": reason,
            },
        }
    
    def _send_to_bankr(self, command: dict) -> dict:
        """Send the command to Bankr via sidecar"""
        symbol = command["symbol"]
        direction = command["suggested_direction"]
        ctx = command["context"]
        
        mode_str = "[DRY RUN] " if self.dry_run else "🔥 "
        print(f"\n{mode_str}[Sentinel] Firing {direction} signal for {symbol}")
        print(f"  Reason: {ctx['reason_from_sentinel']}")
        print(f"  Price: ${ctx['price']:,.2f}")
        print(f"  Position in Range: {ctx['pos_in_range']:.3f}")
        print(f"  Max Size: ${command['max_usdc_per_trade']}, Max Lev: {command['max_leverage']}x")
        
        # Build the prompt for Bankr
        prompt = self._build_sentinel_prompt(command)
        
        decision = {"action": "UNKNOWN", "reason": "No response"}
        result_status = "error"
        
        try:
            resp = self.client.post(
                "/prompt",
                json={
                    "message": prompt,
                    "mode": "perp_sentinel",
                    "dry_run": self.dry_run,
                    "estimated_usdc": command["max_usdc_per_trade"],
                },
                timeout=120,
            )
            resp.raise_for_status()
            result = resp.json()
            
            if result.get("status") == "ok":
                # Parse Bankr's decision from the response
                decision = self._parse_bankr_decision(result)
                print(f"  ✓ Bankr decision: {decision.get('action', 'UNKNOWN')}")
                if decision.get("reason"):
                    print(f"    Reason: {decision['reason']}")
                
                result["decision"] = decision
                result_status = "ok"
            else:
                print(f"  ✗ Bankr error: {result.get('error', 'Unknown')}")
                decision = {"action": "ERROR", "reason": result.get("error", "Unknown")}
                result_status = "error"
                
        except Exception as e:
            print(f"  ✗ Request error: {e}")
            decision = {"action": "ERROR", "reason": str(e)}
            result = {"status": "error", "error": str(e)}
            result_status = "error"
        
        # Log the signal to sidecar DB
        self._log_signal_to_db(command, decision, result_status)
        
        return result
    
    def _log_signal_to_db(self, command: dict, decision: dict, result_status: str):
        """Log the sentinel signal to the sidecar DB"""
        ctx = command["context"]
        try:
            self.client.post(
                "/sentinel/signal",
                json={
                    "symbol": command["symbol"],
                    "direction": command["suggested_direction"],
                    "pos_in_range": ctx["pos_in_range"],
                    "price": ctx["price"],
                    "high_24h": ctx["high_24h"],
                    "low_24h": ctx["low_24h"],
                    "range_pct": ctx["range_pct"],
                    "bankr_action": decision.get("action", "UNKNOWN"),
                    "bankr_reason": decision.get("reason", "")[:200],  # Truncate
                    "size_usdc": decision.get("size_usdc", 0),
                    "leverage": decision.get("leverage", 0),
                    "dry_run": self.dry_run,
                    "result_status": result_status,
                },
                timeout=10,
            )
        except Exception as e:
            print(f"  [Warning] Failed to log signal to DB: {e}")
    
    def _parse_bankr_decision(self, result: dict) -> dict:
        """Parse Bankr's JSON decision from the response"""
        import json
        import re
        
        # Look for JSON in the response (summary or full_response)
        text = result.get("summary", "") or result.get("full_response", "")
        
        # Try to find JSON in the text
        try:
            # Try direct parse first
            return json.loads(text.strip())
        except:
            pass
        
        # Look for JSON pattern in text
        json_pattern = r'\{[^{}]*"action"[^{}]*\}'
        match = re.search(json_pattern, text)
        if match:
            try:
                return json.loads(match.group())
            except:
                pass
        
        # Fallback: look for keywords
        text_lower = text.lower()
        if "skip" in text_lower or "no_trade" in text_lower:
            return {"action": "SKIP", "reason": "Parsed from text response"}
        
        if "execute" in text_lower or "long" in text_lower or "short" in text_lower:
            return {"action": "EXECUTE", "reason": "Parsed from text response (uncertain)"}
        
        return {"action": "UNKNOWN", "reason": "Could not parse response"}
    
    def _build_sentinel_prompt(self, command: dict) -> str:
        """Build the prompt text for Bankr with strict JSON output schema"""
        ctx = command["context"]
        dir_arrow = "📈" if command["suggested_direction"] == "LONG" else "📉"
        
        return f"""[SENTINEL SIGNAL] {dir_arrow} {command["symbol"]} {command["suggested_direction"]}

═══════════════════════════════════════════════════════════════
SIGNAL CONTEXT
═══════════════════════════════════════════════════════════════
Reason: {ctx["reason_from_sentinel"]}

═══════════════════════════════════════════════════════════════
MARKET DATA (as of signal)
═══════════════════════════════════════════════════════════════
Price:          ${ctx["price"]:,.2f}
24h High:       ${ctx["high_24h"]:,.2f}
24h Low:        ${ctx["low_24h"]:,.2f}
24h Range:      {ctx["range_pct"]:.2f}%
Pos in Range:   {ctx["pos_in_range"]:.3f}  (0.00 = daily low, 1.00 = daily high)
24h Change:     {ctx["change_24h_pct"]:+.2f}%
MA (1h):        ${ctx["ma_1h"]:,.2f}
MA (4h):        ${ctx["ma_4h"]:,.2f}

═══════════════════════════════════════════════════════════════
HARD LIMITS (enforced by code - you cannot exceed)
═══════════════════════════════════════════════════════════════
Max Leverage:   {command["max_leverage"]}x
Max Size:       ${command["max_usdc_per_trade"]} USDC
Daily Loss Cap: ${command["max_daily_loss"]}

═══════════════════════════════════════════════════════════════
YOUR DECISION
═══════════════════════════════════════════════════════════════

Respond with EXACTLY ONE of these JSON objects (no prose, no markdown):

EXECUTE: {{"action":"EXECUTE","side":"{command["suggested_direction"]}","size_usdc":<num up to {command["max_usdc_per_trade"]}>,"leverage":<num up to {command["max_leverage"]}>,"reason":"<20 words max>"}}

SKIP:    {{"action":"SKIP","reason":"<why you're passing>"}}

═══════════════════════════════════════════════════════════════
DECISION RULES (default: SKIP)
═══════════════════════════════════════════════════════════════
SKIP if:
• Momentum is strong in the signal direction (ripping, not stalling)
• Price just broke 24h high/low (breakout, not fade)
• Range < 1.5% (low vol = low conviction)
• 24h change > 5% in signal direction (overextended trend)
• Any doubt → SKIP. We catch the next one.

EXECUTE only if:
• Price at genuine intraday extreme (pos near 0 or 1)
• Price is STALLING (momentum exhausted)
• Range is healthy (BTC > 1.5%, ETH > 2%)
• You have real conviction this is a fade, not a breakout

Output ONLY valid JSON. Nothing else.
"""
    
    def check_symbol(self, symbol: str) -> Optional[dict]:
        """
        Check one symbol for signals.
        
        Returns the Bankr response if a signal was fired, None otherwise.
        """
        config = get_config(symbol)
        if not config.enabled:
            return None
        
        # Get price snapshot
        snap = get_price_snapshot(symbol)
        if snap is None:
            return None
        
        # Check minimum range requirement
        if snap.range_pct < config.min_range_pct:
            return None  # Low vol day, skip
        
        # Check for SHORT setup
        short_reason = self._check_short_setup(snap, config)
        if short_reason:
            command = self._build_bankr_command("SHORT", snap, config, short_reason)
            result = self._send_to_bankr(command)
            self._mark_signal(symbol, "SHORT")
            return result
        
        # Check for LONG setup
        long_reason = self._check_long_setup(snap, config)
        if long_reason:
            command = self._build_bankr_command("LONG", snap, config, long_reason)
            result = self._send_to_bankr(command)
            self._mark_signal(symbol, "LONG")
            return result
        
        return None
    
    def scan_all(self) -> Dict[str, dict]:
        """Scan all enabled symbols for signals"""
        self._reset_daily_loss_if_needed()
        
        # Check global daily loss cap
        if self.daily_realized_loss >= GLOBAL_DAILY_LOSS_CAP:
            print(f"[Sentinel] Global daily loss cap reached (${self.daily_realized_loss:.2f}), pausing")
            return {}
        
        results = {}
        for symbol in self.symbols:
            result = self.check_symbol(symbol)
            if result:
                results[symbol] = result
        
        return results
    
    def loop(self, interval: int = None):
        """Run the sentinel loop continuously"""
        interval = interval or SENTINEL_LOOP_INTERVAL
        print(f"\n[Sentinel] Starting loop (interval: {interval}s)")
        print(f"[Sentinel] Watching: {self.symbols}")
        print("-" * 50)
        
        loop_count = 0
        while True:
            try:
                loop_count += 1
                
                # Periodic status (every 20 loops)
                if loop_count % 20 == 0:
                    now = datetime.utcnow().strftime("%H:%M:%S UTC")
                    print(f"[Sentinel] {now} - Loop #{loop_count}, watching {len(self.symbols)} symbols")
                
                self.scan_all()
                
            except KeyboardInterrupt:
                print("\n[Sentinel] Stopped by user")
                break
            except Exception as e:
                print(f"[Sentinel] Error: {e}")
            
            time.sleep(interval)


# ═══════════════════════════════════════════════════════════════════════════════
# CLI INTERFACE
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Price Sentinel - Local Scout → Bankr Sniper")
    parser.add_argument("--symbols", "-s", type=str, help="Comma-separated symbols (default: all enabled)")
    parser.add_argument("--interval", "-i", type=int, default=15, help="Check interval in seconds")
    parser.add_argument("--live", action="store_true", help="Execute live trades (not dry run)")
    parser.add_argument("--once", action="store_true", help="Run once and exit (for testing)")
    
    args = parser.parse_args()
    
    symbols = args.symbols.split(",") if args.symbols else None
    sentinel = Sentinel(symbols=symbols, dry_run=not args.live)
    
    if args.once:
        print("=== Single Scan ===")
        results = sentinel.scan_all()
        if results:
            print(f"\nSignals fired: {list(results.keys())}")
        else:
            print("\nNo signals")
    else:
        sentinel.loop(interval=args.interval)
