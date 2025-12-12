"""
Perps Execution Module - Bankr = Brain + Hands

This module implements direct trade execution via Bankr on Avantis.
Unlike perps_signaler.py (which uses Bankr as an oracle), this module
asks Bankr to execute trades directly.

Key differences from oracle mode:
- mode="perp_trade" instead of mode="perp_quant"
- Bankr executes the trade, we don't maintain any Avantis integration code
- We send intent + constraints, Bankr handles the rest
"""

import os
import time
from typing import Optional

from bot.sidecar_client import SidecarClient
from .schemas import (
    PerpTradeCommand,
    TradeConstraints,
    TradeIntent,
    BankrExecutionResult,
)


# Configuration from environment
SIDECAR_URL = os.getenv("SIDECAR_URL", "http://localhost:4000")
CONTEXT_WALLET = os.getenv("BANKR_CONTEXT_WALLET", "")

# Default constraints (can be overridden per-trade)
DEFAULT_MAX_LEVERAGE = float(os.getenv("PERPS_MAX_LEVERAGE", "5"))
DEFAULT_MAX_USDC_PER_TRADE = float(os.getenv("PERPS_MAX_USDC_PER_TRADE", "350"))
DEFAULT_DAILY_LOSS_CAP = float(os.getenv("PERPS_DAILY_LOSS_CAP", "200"))

# Dry run mode
DRY_RUN = os.getenv("PERPS_DRY_RUN", "true").lower() == "true"


class BankrExecutor:
    """
    Executor that uses Bankr to trade perps directly.
    
    Bankr = brain AND hands. We provide intent and constraints,
    Bankr handles all the Avantis integration.
    """
    
    def __init__(
        self,
        wallet: str = None,
        max_leverage: float = None,
        max_usdc_per_trade: float = None,
        daily_loss_cap: float = None,
        dry_run: bool = None,
    ):
        self.client = SidecarClient(SIDECAR_URL)
        self.wallet = wallet or CONTEXT_WALLET
        self.max_leverage = max_leverage or DEFAULT_MAX_LEVERAGE
        self.max_usdc_per_trade = max_usdc_per_trade or DEFAULT_MAX_USDC_PER_TRADE
        self.daily_loss_cap = daily_loss_cap or DEFAULT_DAILY_LOSS_CAP
        self.dry_run = dry_run if dry_run is not None else DRY_RUN
        
        if not self.wallet:
            raise ValueError("BANKR_CONTEXT_WALLET must be set")
    
    def open_long(
        self,
        symbol: str,
        size_usdc: float,
        reason: str = "",
        leverage: float = None,
    ) -> BankrExecutionResult:
        """
        Open a LONG position via Bankr.
        
        Args:
            symbol: Asset symbol (e.g., "ETH-PERP", "DEGEN-PERP")
            size_usdc: Position size in USDC
            reason: Why this trade (from signal engine or manual)
            leverage: Override default max leverage for this trade
        
        Returns:
            BankrExecutionResult with execution details
        """
        return self._execute_trade(
            symbol=symbol,
            direction="LONG",
            size_usdc=size_usdc,
            reason=reason,
            max_leverage=leverage,
        )
    
    def open_short(
        self,
        symbol: str,
        size_usdc: float,
        reason: str = "",
        leverage: float = None,
    ) -> BankrExecutionResult:
        """
        Open a SHORT position via Bankr.
        
        Args:
            symbol: Asset symbol (e.g., "ETH-PERP", "DEGEN-PERP")
            size_usdc: Position size in USDC
            reason: Why this trade (from signal engine or manual)
            leverage: Override default max leverage for this trade
        
        Returns:
            BankrExecutionResult with execution details
        """
        return self._execute_trade(
            symbol=symbol,
            direction="SHORT",
            size_usdc=size_usdc,
            reason=reason,
            max_leverage=leverage,
        )
    
    def close_position(
        self,
        symbol: str,
        reason: str = "Manual close",
    ) -> BankrExecutionResult:
        """
        Close an existing position via Bankr.
        
        Args:
            symbol: Asset symbol to close
            reason: Why closing
        
        Returns:
            BankrExecutionResult with close details
        """
        mode_str = "[DRY RUN] " if self.dry_run else ""
        print(f"[PerpExecutor] {mode_str}Closing position: {symbol}")
        print(f"[PerpExecutor] Reason: {reason}")
        
        result = self.client.close_perp_position(
            symbol=symbol,
            wallet=self.wallet,
            reason=reason,
            venue="avantis",
            dry_run=self.dry_run,
        )
        
        execution_result = BankrExecutionResult.from_response(result)
        
        if execution_result.success:
            print(f"[PerpExecutor] ✓ Close request sent: {execution_result.summary[:100]}...")
        else:
            print(f"[PerpExecutor] ✗ Close failed: {execution_result.error}")
        
        return execution_result
    
    def _execute_trade(
        self,
        symbol: str,
        direction: str,
        size_usdc: float,
        reason: str,
        max_leverage: float = None,
    ) -> BankrExecutionResult:
        """Internal method to execute a trade."""
        mode_str = "[DRY RUN] " if self.dry_run else ""
        print(f"[PerpExecutor] {mode_str}Executing: {direction} {symbol}")
        print(f"[PerpExecutor] Size: ${size_usdc:.2f} USDC")
        print(f"[PerpExecutor] Max Leverage: {max_leverage or self.max_leverage}x")
        print(f"[PerpExecutor] Reason: {reason}")
        
        result = self.client.execute_perp_trade(
            symbol=symbol,
            direction=direction,
            size_usdc=size_usdc,
            reason=reason,
            wallet=self.wallet,
            max_leverage=max_leverage or self.max_leverage,
            max_usdc_per_trade=self.max_usdc_per_trade,
            daily_loss_cap=self.daily_loss_cap,
            venue="avantis",
            dry_run=self.dry_run,
        )
        
        execution_result = BankrExecutionResult.from_response(result)
        
        if execution_result.success and execution_result.executed:
            print(f"[PerpExecutor] ✓ Trade executed!")
            print(f"[PerpExecutor] Job ID: {execution_result.job_id}")
            if execution_result.tx_hash:
                print(f"[PerpExecutor] TX: {execution_result.tx_hash}")
        elif execution_result.success:
            print(f"[PerpExecutor] ✓ Request processed (no execution)")
            print(f"[PerpExecutor] Summary: {execution_result.summary[:200]}...")
        else:
            print(f"[PerpExecutor] ✗ Error: {execution_result.error}")
        
        return execution_result
    
    def get_positions(self) -> list:
        """Get open perp positions from ledger."""
        return self.client.get_perp_positions()
    
    def get_status(self) -> dict:
        """Get perps trading status."""
        return self.client.get_perp_status()


def execute_signal(
    symbol: str,
    direction: str,
    size_usdc: float,
    reason: str,
    dry_run: bool = None,
) -> BankrExecutionResult:
    """
    Convenience function to execute a single trade signal.
    
    This is what the signal engine can call directly.
    
    Args:
        symbol: Asset symbol (e.g., "ETH-PERP")
        direction: "LONG" or "SHORT"
        size_usdc: Position size in USDC
        reason: Signal reason/thesis
        dry_run: Override global dry_run setting
    
    Returns:
        BankrExecutionResult
    """
    executor = BankrExecutor(dry_run=dry_run)
    
    if direction.upper() == "LONG":
        return executor.open_long(symbol, size_usdc, reason)
    elif direction.upper() == "SHORT":
        return executor.open_short(symbol, size_usdc, reason)
    else:
        return BankrExecutionResult(
            success=False,
            error=f"Invalid direction: {direction}. Must be LONG or SHORT.",
        )


# ─────────────────────────────────────────────────────────────────
# CLI Interface
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Execute perp trades via Bankr")
    parser.add_argument("action", choices=["long", "short", "close", "status", "positions"])
    parser.add_argument("--symbol", "-s", type=str, help="Asset symbol (e.g., ETH-PERP)")
    parser.add_argument("--size", type=float, help="Size in USDC")
    parser.add_argument("--reason", "-r", type=str, default="CLI trade", help="Trade reason")
    parser.add_argument("--live", action="store_true", help="Execute live (not dry run)")
    
    args = parser.parse_args()
    
    executor = BankrExecutor(dry_run=not args.live)
    
    if args.action == "status":
        status = executor.get_status()
        print("\n=== Perps Status ===")
        print(f"Enabled: {status.get('enabled', False)}")
        print(f"Settings: {status.get('settings', {})}")
        print(f"PnL: {status.get('pnl', {})}")
    
    elif args.action == "positions":
        positions = executor.get_positions()
        print("\n=== Open Positions ===")
        if not positions:
            print("No open positions")
        for pos in positions:
            print(f"  {pos['asset']} {pos['side']} ${pos['size_usd']:.2f} @ {pos['entry_price']}")
    
    elif args.action in ("long", "short"):
        if not args.symbol:
            print("Error: --symbol is required for long/short")
            exit(1)
        if not args.size:
            print("Error: --size is required for long/short")
            exit(1)
        
        direction = "LONG" if args.action == "long" else "SHORT"
        result = execute_signal(
            symbol=args.symbol,
            direction=direction,
            size_usdc=args.size,
            reason=args.reason,
            dry_run=not args.live,
        )
        
        print(f"\n=== Execution Result ===")
        print(f"Success: {result.success}")
        print(f"Executed: {result.executed}")
        if result.summary:
            print(f"Summary: {result.summary[:300]}")
        if result.error:
            print(f"Error: {result.error}")
    
    elif args.action == "close":
        if not args.symbol:
            print("Error: --symbol is required for close")
            exit(1)
        
        result = executor.close_position(args.symbol, args.reason)
        
        print(f"\n=== Close Result ===")
        print(f"Success: {result.success}")
        if result.summary:
            print(f"Summary: {result.summary[:300]}")
        if result.error:
            print(f"Error: {result.error}")
