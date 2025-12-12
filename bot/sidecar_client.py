# bot/sidecar_client.py
"""
Simple HTTP client for communicating with the Node.js sidecar.
"""

import os
import requests
from typing import Any, Optional


class SidecarClient:
    """HTTP client for the sidecar API."""

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = base_url or os.getenv("SIDECAR_BASE_URL", "http://localhost:4000")

    def get(self, path: str, **kwargs) -> requests.Response:
        """GET request to sidecar."""
        return requests.get(f"{self.base_url}{path}", timeout=10, **kwargs)

    def post(self, path: str, json: Any = None, **kwargs) -> requests.Response:
        """POST request to sidecar."""
        return requests.post(f"{self.base_url}{path}", json=json, timeout=10, **kwargs)

    def get_status(self) -> dict:
        """Get bot status and guardrails."""
        try:
            resp = self.get("/status")
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    def get_open_positions(self) -> list:
        """Get all open positions from the ledger."""
        try:
            resp = self.get("/positions/open")
            resp.raise_for_status()
            data = resp.json()
            return data.get("trades", [])
        except Exception:
            return []

    def get_pnl_summary(self) -> dict:
        """Get PnL summary."""
        try:
            resp = self.get("/positions/summary")
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    def send_telemetry(self, event_type: str, data: dict) -> bool:
        """Send telemetry event to sidecar."""
        try:
            resp = self.post("/telemetry", json={"type": event_type, **data})
            return resp.ok
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────────────
    # Perp Trade Execution (Bankr = brain + hands)
    # ─────────────────────────────────────────────────────────────────

    def execute_perp_trade(
        self,
        symbol: str,
        direction: str,
        size_usdc: float,
        reason: str,
        wallet: str,
        max_leverage: float = 5.0,
        max_usdc_per_trade: float = 350.0,
        daily_loss_cap: float = 200.0,
        venue: str = "avantis",
        dry_run: bool = False,
    ) -> dict:
        """
        Ask Bankr to execute a perp trade directly on Avantis.
        
        Bankr = brain AND hands. We send intent + constraints, Bankr executes.
        
        Args:
            symbol: Asset symbol (e.g., "ETH-PERP", "DEGEN-PERP")
            direction: "LONG" or "SHORT"
            size_usdc: Position size in USDC
            reason: Why this trade (from signal engine or user)
            wallet: Context wallet address for Avantis
            max_leverage: Maximum leverage allowed (default 5x)
            max_usdc_per_trade: Per-trade cap (default 350)
            daily_loss_cap: Daily loss limit (default 200)
            venue: Trading venue (default "avantis")
            dry_run: If True, Bankr describes but doesn't execute
        
        Returns:
            dict with status, transactions, and Bankr response
        """
        command = {
            "mode": "perp_trade",
            "venue": venue,
            "wallet": wallet,
            "constraints": {
                "max_leverage": max_leverage,
                "max_usdc_per_trade": max_usdc_per_trade,
                "daily_loss_cap": daily_loss_cap,
            },
            "intent": {
                "symbol": symbol,
                "direction": direction,
                "size_usdc": size_usdc,
                "reason": reason,
            },
        }
        
        try:
            resp = self.post(
                "/prompt",
                json={
                    "message": self._build_perp_trade_prompt(command),
                    "mode": "perp_trade",
                    "dry_run": dry_run,
                    "estimated_usdc": size_usdc,
                },
                timeout=120,  # Perp trades may take longer
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _build_perp_trade_prompt(self, command: dict) -> str:
        """Build the prompt for Bankr to execute a perp trade."""
        intent = command["intent"]
        constraints = command["constraints"]
        
        return f"""Execute a perpetual futures trade on {command["venue"].upper()}.

TRADE INTENT:
- Symbol: {intent["symbol"]}
- Direction: {intent["direction"]}
- Size: ${intent["size_usdc"]:.2f} USDC
- Reason: {intent["reason"]}

CONSTRAINTS (MUST NOT EXCEED):
- Max Leverage: {constraints["max_leverage"]}x
- Max USDC per trade: ${constraints["max_usdc_per_trade"]:.2f}
- Daily Loss Cap: ${constraints["daily_loss_cap"]:.2f}

WALLET: {command["wallet"]}

Execute this trade on {command["venue"].upper()}. Use the appropriate leverage and set reasonable TP/SL based on the market conditions.
If the trade cannot be executed safely within these constraints, explain why and do NOT execute.
"""

    def close_perp_position(
        self,
        symbol: str,
        wallet: str,
        reason: str = "Manual close request",
        venue: str = "avantis",
        dry_run: bool = False,
    ) -> dict:
        """
        Ask Bankr to close a perp position on Avantis.
        
        Args:
            symbol: Asset symbol (e.g., "ETH-PERP")
            wallet: Context wallet address
            reason: Why closing this position
            venue: Trading venue (default "avantis")
            dry_run: If True, describe but don't execute
        
        Returns:
            dict with status and Bankr response
        """
        prompt = f"""Close my perpetual futures position on {venue.upper()}.

CLOSE REQUEST:
- Symbol: {symbol}
- Reason: {reason}

WALLET: {wallet}

Close this position at market. If there is no open position for this symbol, respond with "NO_POSITION_FOUND".
"""
        
        try:
            resp = self.post(
                "/prompt",
                json={
                    "message": prompt,
                    "mode": "perp_trade",
                    "dry_run": dry_run,
                    "estimated_usdc": 0,
                },
                timeout=120,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def get_perp_positions(self) -> list:
        """Get open perp positions from the ledger."""
        try:
            resp = self.get("/perps/positions")
            resp.raise_for_status()
            data = resp.json()
            return data.get("positions", [])
        except Exception:
            return []

    def get_perp_status(self) -> dict:
        """Get perps trading status and settings."""
        try:
            resp = self.get("/perps/status")
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}
