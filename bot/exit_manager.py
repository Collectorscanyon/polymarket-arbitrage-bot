"""Exit Manager - Automatic TP/SL/max-hold/nightly-flatten automation.

This module monitors open positions and automatically closes them when:
- Take-profit threshold is reached (e.g. +2.2% P&L)
- Stop-loss threshold is reached (e.g. -0.95% P&L)
- Position has been held longer than MAX_HOLD_HOURS
- It's the nightly auto-flatten hour (e.g. 23:00 UTC)

Can run standalone: python -m bot.exit_manager
Or controlled via sidecar /exit-manager/start and /exit-manager/stop endpoints.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional

# Add project root to path for imports
sys.path.insert(0, str(__file__).rsplit("bot", 1)[0])

from config import (
    TAKE_PROFIT_PCT,
    STOP_LOSS_PCT,
    MAX_HOLD_HOURS,
    AUTO_FLATTEN_HOUR_UTC,
    EXIT_LOOP_SLEEP_SECONDS,
    EXIT_MANAGER_DRY_RUN,
)
from bot.sidecar_client import SidecarClient
from bot.utils.polymarket import get_market_price

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Exit reason tags for clear logging
EXIT_TAG_TP = "[TP]"
EXIT_TAG_SL = "[SL]"
EXIT_TAG_MAX_HOLD = "[MAX_HOLD]"
EXIT_TAG_NIGHTLY = "[NIGHTLY_FLATTEN]"


class ExitManager:
    """Monitors positions and triggers exits based on TP/SL/time rules."""

    def __init__(self):
        self.client = SidecarClient()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Track which positions we've already triggered exits for
        # (to avoid duplicate close attempts)
        self._triggered_exits: set[int] = set()

        # Track last flatten hour to avoid multiple flattens per hour
        self._last_flatten_hour: Optional[int] = None

    def _calculate_pnl_pct(self, avg_price: float, current_price: float, side: str) -> float:
        """Calculate P&L percentage for a position.

        For YES positions: profit when price goes up
        For NO positions: profit when price goes down (we bought NO = bet against YES)
        """
        if avg_price <= 0:
            return 0.0

        if side.upper() == "YES":
            # Bought YES at avg_price, current value is current_price
            pnl_pct = ((current_price - avg_price) / avg_price) * 100
        else:
            # Bought NO at avg_price, NO price = 1 - YES price
            # If we bought NO at 0.40, current NO price = 1 - current_YES_price
            # For simplicity, we store NO trades with their actual NO price
            pnl_pct = ((current_price - avg_price) / avg_price) * 100

        return pnl_pct

    def _get_position_age_hours(self, timestamp: str) -> float:
        """Calculate how many hours ago the position was opened."""
        try:
            # Parse ISO format timestamp
            opened_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = now - opened_at
            return delta.total_seconds() / 3600
        except Exception as e:
            log.warning(f"Could not parse timestamp '{timestamp}': {e}")
            return 0.0

    def _should_exit(
        self,
        position: dict,
        current_price: float,
    ) -> tuple[bool, str, str]:
        """Determine if a position should be exited and why.

        Returns:
            (should_exit, reason, tag)
        """
        pos_id = position.get("id")
        avg_price = position.get("avg_price", 0)
        side = position.get("side", "YES")
        timestamp = position.get("timestamp", "")

        # Check for per-position overrides
        tp_pct = position.get("tp_pct_override") or TAKE_PROFIT_PCT
        sl_pct = position.get("sl_pct_override") or STOP_LOSS_PCT
        max_hold = position.get("max_hold_override") or MAX_HOLD_HOURS

        # Skip if we already triggered an exit for this position
        if pos_id in self._triggered_exits:
            return False, "", ""

        # Calculate P&L
        pnl_pct = self._calculate_pnl_pct(avg_price, current_price, side)

        # Check take-profit
        if pnl_pct >= tp_pct:
            return True, f"TP hit ({pnl_pct:.2f}% >= {tp_pct}%)", EXIT_TAG_TP

        # Check stop-loss
        if pnl_pct <= sl_pct:
            return True, f"SL hit ({pnl_pct:.2f}% <= {sl_pct}%)", EXIT_TAG_SL

        # Check max hold time
        age_hours = self._get_position_age_hours(timestamp)
        if age_hours >= max_hold:
            return True, f"Max hold exceeded ({age_hours:.1f}h >= {max_hold}h)", EXIT_TAG_MAX_HOLD

        return False, "", ""

    def _is_flatten_hour(self) -> bool:
        """Check if it's the nightly auto-flatten hour."""
        if AUTO_FLATTEN_HOUR_UTC < 0:
            return False

        now_utc = datetime.now(timezone.utc)
        current_hour = now_utc.hour

        # Only trigger once per hour
        if current_hour == self._last_flatten_hour:
            return False

        if current_hour == AUTO_FLATTEN_HOUR_UTC:
            self._last_flatten_hour = current_hour
            return True

        return False

    def _close_position(self, position: dict, reason: str, tag: str = "") -> bool:
        """Request the sidecar to close a position."""
        pos_id = position.get("id")
        market_slug = position.get("market_slug", "")
        market_label = position.get("market_label", market_slug)
        side = position.get("side", "YES")
        size_usdc = position.get("size_usdc", 0)

        log.info(f"{tag} Closing position #{pos_id}: {market_label} {side} ${size_usdc} - {reason}")

        # Check if we're in dry-run mode
        if EXIT_MANAGER_DRY_RUN:
            log.info(f"{tag} [DRY-RUN] Would close position #{pos_id} but DRY_RUN is enabled")
            self._triggered_exits.add(pos_id)  # Still mark as triggered to avoid spam
            return True

        try:
            # Build close prompt
            opposite_side = "NO" if side.upper() == "YES" else "YES"
            prompt = f"sell {size_usdc} USDC of {opposite_side} on {market_slug} to close position"

            resp = self.client.post("/prompt", {
                "prompt": prompt,
                "estimated_usdc": size_usdc,
            })

            if resp.status_code == 200:
                log.info(f"{tag} ✓ Close request sent for position #{pos_id}")
                self._triggered_exits.add(pos_id)
                return True
            else:
                log.warning(f"{tag} Close request failed for position #{pos_id}: {resp.status_code} {resp.text}")
                return False

        except Exception as e:
            log.error(f"{tag} Error closing position #{pos_id}: {e}")
            return False

    def run_once(self) -> int:
        """Run one iteration of the exit manager.

        Returns:
            Number of positions exited this iteration.
        """
        exits_triggered = 0

        # Check for nightly flatten
        if self._is_flatten_hour():
            log.info(f"{EXIT_TAG_NIGHTLY} 🌙 Nightly flatten triggered at hour {AUTO_FLATTEN_HOUR_UTC} UTC")
            if EXIT_MANAGER_DRY_RUN:
                log.info(f"{EXIT_TAG_NIGHTLY} [DRY-RUN] Would flatten all but DRY_RUN is enabled")
            else:
                try:
                    resp = self.client.post("/flatten-all", {})
                    if resp.status_code == 200:
                        log.info(f"{EXIT_TAG_NIGHTLY} Flatten-all request sent successfully")
                        return -1  # Special code for flatten-all
                except Exception as e:
                    log.error(f"{EXIT_TAG_NIGHTLY} Flatten-all failed: {e}")

        # Get open positions from sidecar
        try:
            resp = self.client.get("/positions/open")
            if resp.status_code != 200:
                log.warning(f"Failed to get open positions: {resp.status_code}")
                return 0

            data = resp.json()
            positions = data.get("trades", [])  # Note: API returns 'trades' not 'positions'

        except Exception as e:
            log.error(f"Error fetching positions: {e}")
            return 0

        if not positions:
            log.debug("No open positions to monitor")
            return 0

        log.info(f"Monitoring {len(positions)} open position(s)")

        # Check each position
        for pos in positions:
            pos_id = pos.get("id")
            market_slug = pos.get("market_slug", "")
            side = pos.get("side", "YES")

            if not market_slug:
                log.warning(f"Position #{pos_id} has no market_slug, skipping")
                continue

            # Fetch current price
            try:
                current_price = get_market_price(market_slug, side)
                if current_price is None:
                    log.warning(f"Could not get price for {market_slug}, skipping")
                    continue

            except Exception as e:
                log.warning(f"Error getting price for {market_slug}: {e}")
                continue

            # Check if we should exit
            should_exit, reason, tag = self._should_exit(pos, current_price)

            if should_exit:
                if self._close_position(pos, reason, tag):
                    exits_triggered += 1

        return exits_triggered

    def loop(self):
        """Main loop - runs until stop() is called."""
        log.info("=" * 50)
        log.info("Exit Manager started")
        log.info(f"  DRY-RUN mode: {EXIT_MANAGER_DRY_RUN}")
        log.info(f"  TP threshold: {TAKE_PROFIT_PCT}%")
        log.info(f"  SL threshold: {STOP_LOSS_PCT}%")
        log.info(f"  Max hold: {MAX_HOLD_HOURS}h")
        log.info(f"  Auto-flatten hour (UTC): {AUTO_FLATTEN_HOUR_UTC}")
        log.info(f"  Check interval: {EXIT_LOOP_SLEEP_SECONDS}s")
        log.info("=" * 50)

        while not self._stop_event.is_set():
            try:
                exits = self.run_once()
                if exits > 0:
                    log.info(f"Triggered {exits} exit(s) this iteration")

            except Exception as e:
                log.error(f"Exit manager error: {e}")

            # Wait for next iteration (interruptible)
            self._stop_event.wait(EXIT_LOOP_SLEEP_SECONDS)

        log.info("Exit Manager stopped")

    def start(self):
        """Start the exit manager in a background thread."""
        if self._thread and self._thread.is_alive():
            log.warning("Exit manager already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self.loop, daemon=True)
        self._thread.start()
        log.info("Exit manager thread started")

    def stop(self):
        """Stop the exit manager."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("Exit manager thread stopped")


# ─────────────────────────────────────────────────────────────
# Standalone entry point
# ─────────────────────────────────────────────────────────────

def main():
    """Run exit manager as a standalone process."""
    manager = ExitManager()

    try:
        manager.loop()
    except KeyboardInterrupt:
        log.info("Keyboard interrupt received")
        manager.stop()


if __name__ == "__main__":
    main()
