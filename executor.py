"""Logic responsible for delegating execution to the Bankr sidecar."""
from __future__ import annotations

import logging
import os
import time
from typing import Dict, Optional

import requests

from config import (
    ARB_STAKE_USDC,
    ARB_THRESHOLD,
    BANKR_EXECUTOR_URL,
    BANKR_DRY_RUN,
    BANKR_MIN_SECONDS_BETWEEN_PROMPTS,
    CHEAP_BUY_THRESHOLD,
    ENABLE_BANKR_EXECUTOR,
    HEDGE_STAKE_USDC,
    MARKET_COOLDOWN_SECONDS,
    MARKETS_TO_WATCH,
    MAX_BANKR_COMMANDS_PER_LOOP,
    SCAN_INTERVAL,
)

logger = logging.getLogger(__name__)

# Sidecar URL for telemetry (same as executor URL by default)
SIDECAR_URL = os.getenv("SIDECAR_URL", BANKR_EXECUTOR_URL)


class BankrWalletEmptyError(Exception):
    """Raised when the Bankr payment wallet has insufficient funds."""
    pass


class BankrCapExceededError(Exception):
    """Raised when a spend guardrail (per-prompt or daily cap) is hit."""
    pass


# ─────────────────────────────────────────────────────────────
# Telemetry: report trade opens to the sidecar for dashboard
# ─────────────────────────────────────────────────────────────
def send_trade_open_telemetry(
    command_id: str,
    market_label: str,
    market_slug: str,
    side: str,
    stake_usdc: float,
    price: float,
) -> None:
    """Report an opened trade to the sidecar for position tracking."""
    payload = {
        "command_id": command_id,
        "market_label": market_label,
        "market_slug": market_slug,
        "side": side,
        "size_usdc": float(stake_usdc),
        "avg_price": float(price),
    }
    try:
        requests.post(
            f"{SIDECAR_URL}/telemetry/trade-open",
            json=payload,
            timeout=2,
        )
        logger.debug("[TELEMETRY] Reported trade open: %s", command_id)
    except Exception as e:
        # Don't crash the bot if telemetry fails
        logger.warning("[TELEMETRY] Failed to report trade: %s", e)


# ─────────────────────────────────────────────────────────────
# Per-market cooldown tracking
# ─────────────────────────────────────────────────────────────
_last_executed: Dict[str, float] = {}


def _market_signature(market: str, op_type: str = "arb") -> str:
    """Create a stable key for an opportunity, so we can cooldown per-market."""
    return f"{op_type}:{market}"


def _is_on_cooldown(market: str, op_type: str = "arb") -> bool:
    """Check if we've recently sent a Bankr command for this market/op."""
    if MARKET_COOLDOWN_SECONDS <= 0:
        return False
    sig = _market_signature(market, op_type)
    last = _last_executed.get(sig, 0.0)
    elapsed = time.time() - last
    if elapsed < MARKET_COOLDOWN_SECONDS:
        logger.debug(
            "[COOLDOWN] Skipping %s, on cooldown (%.1fs < %ds)",
            sig,
            elapsed,
            MARKET_COOLDOWN_SECONDS,
        )
        return True
    return False


def _record_execution(market: str, op_type: str = "arb") -> None:
    """Mark that we just executed a Bankr command for this market/op."""
    sig = _market_signature(market, op_type)
    _last_executed[sig] = time.time()


# ─────────────────────────────────────────────────────────────
# Global prompt spacing (min seconds between any Bankr call)
# ─────────────────────────────────────────────────────────────
_last_prompt_time: float = 0.0


def _enforce_prompt_spacing() -> None:
    """Raise BankrCapExceededError if we're calling Bankr too fast."""
    global _last_prompt_time
    if BANKR_MIN_SECONDS_BETWEEN_PROMPTS <= 0:
        return
    now = time.time()
    dt = now - _last_prompt_time
    if dt < BANKR_MIN_SECONDS_BETWEEN_PROMPTS:
        wait = BANKR_MIN_SECONDS_BETWEEN_PROMPTS - dt
        raise BankrCapExceededError(
            f"MIN_SECONDS_BETWEEN_PROMPTS not met (wait {wait:.1f}s more)"
        )


def _update_prompt_time() -> None:
    """Record that we just sent a Bankr prompt."""
    global _last_prompt_time
    _last_prompt_time = time.time()


class BankrExecutor:
    """Sends natural-language commands to the local Bankr sidecar."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def send_command(
        self, command: str, dry_run: bool = False, estimated_usdc: float = 1.0
    ) -> Optional[dict]:
        """Send a natural-language command to the Bankr sidecar."""
        # Enforce global spacing between prompts
        _enforce_prompt_spacing()

        payload = {
            "message": command,
            "dry_run": dry_run,
            "estimated_usdc": estimated_usdc,
        }
        try:
            response = requests.post(
                f"{self.base_url}/prompt",
                json=payload,
                timeout=60,
            )

            # Handle guardrail responses before raise_for_status
            if response.status_code == 402:
                data = response.json()
                if data.get("error") == "BANKR_INSUFFICIENT_FUNDS":
                    logger.error(
                        "[BANKR] Wallet out of funds! Stop trading. Raw: %s",
                        data.get("raw", ""),
                    )
                    raise BankrWalletEmptyError("Bankr payment wallet is empty.")

            if response.status_code == 400:
                data = response.json()
                err_code = data.get("error", "")
                if err_code in {"MAX_USDC_PER_PROMPT_EXCEEDED", "DAILY_SPEND_CAP_REACHED"}:
                    logger.warning(
                        "[BANKR] Guardrail hit: %s. Details: %s",
                        err_code,
                        data.get("details", {}),
                    )
                    raise BankrCapExceededError(f"Guardrail: {err_code}")

            response.raise_for_status()
            data = response.json()
            
            # Record successful prompt time
            _update_prompt_time()
            
            logger.info("[BANKR] Command accepted: %s", data.get("jobId", "unknown"))
            return data

        except requests.RequestException as exc:
            logger.error("[BANKR] Request failed: %s", exc)
            return None
        except ValueError:
            logger.error("[BANKR] Sidecar returned non-JSON response")
            return None


def _default_market() -> str:
    if MARKETS_TO_WATCH:
        return MARKETS_TO_WATCH[0]
    return "DEMO-MARKET"


bankr = BankrExecutor(BANKR_EXECUTOR_URL)
_commands_sent_this_loop = 0


def reset_bankr_command_budget() -> None:
    """Reset per-loop command counter (call once each scan iteration)."""
    global _commands_sent_this_loop
    _commands_sent_this_loop = 0


def _consume_command_slot(market: str) -> bool:
    """Increment the command counter if under the cap."""
    global _commands_sent_this_loop
    if _commands_sent_this_loop >= MAX_BANKR_COMMANDS_PER_LOOP:
        logger.debug(
            "Command cap reached (%d); skipping additional instructions for %s",
            MAX_BANKR_COMMANDS_PER_LOOP,
            market,
        )
        return False
    _commands_sent_this_loop += 1
    return True


def execute_arb(yes_price: float, no_price: float, market: str, stake: float | None = None) -> str:
    """Execute a simple arbitrage command via the Bankr sidecar.
    
    Args:
        stake: USDC per side. Defaults to ARB_STAKE_USDC from config.
    
    Raises:
        BankrWalletEmptyError: If payment wallet is out of funds.
        BankrCapExceededError: If a spend guardrail is hit.
    """
    if stake is None:
        stake = ARB_STAKE_USDC
        
    total = yes_price + no_price
    if total >= ARB_THRESHOLD:
        return "No arb opportunity."

    # Check per-market cooldown
    if _is_on_cooldown(market, "arb"):
        return f"[COOLDOWN] Market '{market}' on cooldown, skipping arb."

    profit_pct = (1.0 - total) * 100.0
    command = (
        f"On Polymarket, for market '{market}', "
        f"buy ${stake:.2f} YES and ${stake:.2f} NO "
        f"if the combined price is {total:.3f} or lower. "
        f"Target locked profit of about {profit_pct:.1f}% after fees. "
        f"Use my existing Bankr wallet and keep risk small."
    )
    if not _consume_command_slot(market):
        return (
            f"[SKIP] Command cap ({MAX_BANKR_COMMANDS_PER_LOOP}) reached; "
            f"skipped arb instruction for {market}."
        )
    if not ENABLE_BANKR_EXECUTOR:
        return f"[DRY] Would send arb command: {command}"

    # estimated_usdc = stake * 2 (buying YES + NO)
    result = bankr.send_command(command, dry_run=BANKR_DRY_RUN, estimated_usdc=stake * 2)
    if result is None:
        return "Failed to send arb command."
    
    # Record successful execution for cooldown
    _record_execution(market, "arb")
    
    # Report to telemetry for position tracking (live mode only)
    job_id = result.get("jobId") or result.get("job_id")
    if job_id and not BANKR_DRY_RUN:
        # Report both legs as separate positions
        avg_price = (yes_price + no_price) / 2.0
        send_trade_open_telemetry(
            command_id=f"{job_id}_YES",
            market_label=market,
            market_slug=market,
            side="YES",
            stake_usdc=stake,
            price=yes_price,
        )
        send_trade_open_telemetry(
            command_id=f"{job_id}_NO",
            market_label=market,
            market_slug=market,
            side="NO",
            stake_usdc=stake,
            price=no_price,
        )
    
    return f"Sent arb command to Bankr: {command}"


def hedge_cheap_buy(market: str, outcome: str, price: float, stake: float | None = None) -> str:
    """Send a cheap-buy hedge instruction when a price looks mispriced.
    
    Args:
        stake: USDC position size. Defaults to HEDGE_STAKE_USDC from config.
    
    Raises:
        BankrWalletEmptyError: If payment wallet is out of funds.
        BankrCapExceededError: If a spend guardrail is hit.
    """
    if stake is None:
        stake = HEDGE_STAKE_USDC
        
    if price >= CHEAP_BUY_THRESHOLD:
        return "No cheap-buy opportunity."

    # Check per-market cooldown (use outcome in signature for hedge)
    op_type = f"hedge_{outcome.lower()}"
    if _is_on_cooldown(market, op_type):
        return f"[COOLDOWN] Market '{market}' {outcome} on cooldown, skipping hedge."

    command = (
        f"On Polymarket, for market '{market}', "
        f"buy ${stake:.2f} of {outcome} at around {price * 100:.1f} cents, "
        "then hedge the directional risk using a small opposite perp "
        "on Avantis or a similar perp venue if the liquidity is decent. "
        "Keep slippage low and avoid over-leverage."
    )
    if not _consume_command_slot(market):
        return (
            f"[SKIP] Command cap ({MAX_BANKR_COMMANDS_PER_LOOP}) reached; "
            f"skipped hedge instruction for {market}."
        )
    if not ENABLE_BANKR_EXECUTOR:
        return f"[DRY] Would send hedge command: {command}"

    # estimated_usdc = stake (the position size)
    result = bankr.send_command(command, dry_run=BANKR_DRY_RUN, estimated_usdc=stake)
    if result is None:
        return "Failed to send hedge command."
    
    # Record successful execution for cooldown
    _record_execution(market, op_type)
    
    # Report to telemetry for position tracking (live mode only)
    job_id = result.get("jobId") or result.get("job_id")
    if job_id and not BANKR_DRY_RUN:
        send_trade_open_telemetry(
            command_id=job_id,
            market_label=market,
            market_slug=market,
            side=outcome.upper(),
            stake_usdc=stake,
            price=price,
        )
    
    return f"Sent hedge command to Bankr: {command}"


def close_position(
    market_label: str,
    market_slug: str,
    side: str,
    size_usdc: float,
    max_slippage_bps: int = 50,
) -> Optional[dict]:
    """
    Close a position by selling shares at market price.
    
    Args:
        market_label: Human-readable market name
        market_slug: Market identifier/slug
        side: "YES" or "NO" - the side to sell
        size_usdc: Approximate USDC value of the position
        max_slippage_bps: Maximum slippage in basis points (default 50 = 0.5%)
    
    Returns:
        Result dict from Bankr or None on failure.
        
    Raises:
        BankrWalletEmptyError: If payment wallet is out of funds.
        BankrCapExceededError: If a spend guardrail is hit.
    """
    slippage_pct = max_slippage_bps / 100.0
    
    command = (
        f"On Polymarket, for market '{market_label}', "
        f"sell/close my {side} position worth approximately ${size_usdc:.2f}. "
        f"Use market order with max slippage of {slippage_pct:.1f}%. "
        f"Execute the trade to flatten this position."
    )
    
    logger.info("[CLOSE] Sending close command for %s %s ($%.2f)", market_label, side, size_usdc)
    
    if not ENABLE_BANKR_EXECUTOR:
        logger.info("[CLOSE] DRY mode - would send: %s", command)
        return {"status": "dry_run", "command": command}
    
    # Use a small estimated_usdc since we're closing, not opening
    # The actual trade value is already accounted for
    result = bankr.send_command(command, dry_run=BANKR_DRY_RUN, estimated_usdc=1.0)
    
    if result is None:
        logger.error("[CLOSE] Failed to send close command")
        return None
    
    logger.info("[CLOSE] Close command sent successfully: %s", result.get("jobId", "unknown"))
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    while True:
        YES_PRICE = 0.32
        NO_PRICE = 0.62
        MARKET = _default_market()
        print(execute_arb(YES_PRICE, NO_PRICE, MARKET))
        time.sleep(SCAN_INTERVAL)
