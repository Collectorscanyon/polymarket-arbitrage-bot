"""
Perps Trading Schemas - Input/Output structures for Bankr Quant Mode

These dataclasses define the exact JSON schemas used for:
1. Sending market context TO Bankr
2. Receiving trading decisions FROM Bankr
"""

from dataclasses import dataclass, field, asdict
from typing import Optional, Literal
from enum import Enum
import json


class Decision(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NO_TRADE = "NO_TRADE"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


@dataclass
class ExistingExposure:
    """Current portfolio exposure"""
    net_usd: float = 0.0
    direction: Literal["LONG", "SHORT", "FLAT"] = "FLAT"


@dataclass
class PerpMarketContext:
    """
    The structured input schema we send to Bankr for perp analysis.
    This is the "solid input" that makes Bankr an effective oracle.
    """
    # Asset identification
    asset: str                          # e.g., "DEGEN", "BNKR", "ETH"
    chain: str = "Base"                 # Chain where venue operates
    venue: str = "Avantis"              # Trading venue
    
    # Timeframe context
    timeframe: str = "scalp_1h"         # scalp_1h, swing_4h, position_1d
    
    # Price data
    price: float = 0.0                  # Current spot price
    change_24h_pct: float = 0.0         # 24h change percentage
    
    # Perp-specific metrics
    funding_8h: float = 0.0             # 8-hour funding rate
    open_interest_usd: float = 0.0      # Total OI in USD
    volume_24h_usd: float = 0.0         # 24h trading volume
    
    # Technical levels (optional but valuable)
    liquidation_heatmap_hint: str = ""  # e.g., "cluster of long liqs 3-5% below spot"
    support_levels: list = field(default_factory=list)      # e.g., [0.0325, 0.0300]
    resistance_levels: list = field(default_factory=list)   # e.g., [0.0365, 0.0380]
    
    # Account & risk constraints
    account_equity_usd: float = 10000.0
    max_leverage_allowed: float = 3.0
    max_risk_per_trade_pct: float = 1.0
    max_concurrent_positions: int = 5
    
    # Current exposure
    existing_exposure: ExistingExposure = field(default_factory=ExistingExposure)
    
    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization"""
        d = asdict(self)
        return d
    
    def to_json(self) -> str:
        """Convert to formatted JSON string"""
        return json.dumps(self.to_dict(), indent=2)


@dataclass
class EntryZone:
    """Where to enter the trade"""
    type: str = "market"     # "limit" or "market"
    min_price: float = 0.0
    max_price: float = 0.0


@dataclass
class TakeProfit:
    """Take profit target"""
    target_price: float = 0.0
    expected_rr: float = 0.0  # Risk/reward ratio


@dataclass
class StopLoss:
    """Stop loss level"""
    price: float = 0.0
    risk_pct_equity: float = 0.0  # What % of equity is at risk


@dataclass
class PositionSize:
    """Position sizing"""
    notional_usd: float = 0.0
    contracts: float = 0.0


@dataclass
class BankrPerpDecision:
    """
    The structured output schema we expect FROM Bankr.
    This is what we parse and execute.
    """
    decision: str = "NO_TRADE"          # LONG, SHORT, or NO_TRADE
    confidence: float = 0.0              # 0.0 to 1.0
    
    entry_zone: EntryZone = field(default_factory=EntryZone)
    take_profit: TakeProfit = field(default_factory=TakeProfit)
    stop_loss: StopLoss = field(default_factory=StopLoss)
    
    max_leverage: float = 1.0
    size: PositionSize = field(default_factory=PositionSize)
    
    time_horizon_hours: int = 6
    reason: str = ""                     # Bankr's reasoning
    
    # Metadata (added by our parser)
    raw_response: str = ""
    parse_success: bool = False
    parse_error: str = ""
    
    @classmethod
    def from_dict(cls, data: dict) -> "BankrPerpDecision":
        """Parse from Bankr's JSON response"""
        try:
            entry = data.get("entry_zone", {})
            tp = data.get("take_profit", {})
            sl = data.get("stop_loss", {})
            sz = data.get("size", {})
            
            decision = cls(
                decision=data.get("decision", "NO_TRADE"),
                confidence=float(data.get("confidence", 0.0)),
                entry_zone=EntryZone(
                    type=entry.get("type", "market"),
                    min_price=float(entry.get("min_price", 0)),
                    max_price=float(entry.get("max_price", 0)),
                ),
                take_profit=TakeProfit(
                    target_price=float(tp.get("target_price", 0)),
                    expected_rr=float(tp.get("expected_rr", 0)),
                ),
                stop_loss=StopLoss(
                    price=float(sl.get("price", 0)),
                    risk_pct_equity=float(sl.get("risk_pct_equity", 0)),
                ),
                max_leverage=float(data.get("max_leverage", 1.0)),
                size=PositionSize(
                    notional_usd=float(sz.get("notional_usd", 0)),
                    contracts=float(sz.get("contracts", 0)),
                ),
                time_horizon_hours=int(data.get("time_horizon_hours", 6)),
                reason=data.get("reason", ""),
                parse_success=True,
            )
            return decision
        except Exception as e:
            return cls(
                parse_success=False,
                parse_error=str(e),
            )
    
    @classmethod
    def from_json(cls, json_str: str) -> "BankrPerpDecision":
        """Parse from JSON string, extracting JSON from mixed content if needed"""
        try:
            # Try to find JSON in the response (Bankr might include explanation text)
            import re
            json_match = re.search(r'\{[\s\S]*\}', json_str)
            if json_match:
                data = json.loads(json_match.group())
                result = cls.from_dict(data)
                result.raw_response = json_str
                return result
            else:
                return cls(
                    parse_success=False,
                    parse_error="No JSON object found in response",
                    raw_response=json_str,
                )
        except json.JSONDecodeError as e:
            return cls(
                parse_success=False,
                parse_error=f"JSON parse error: {e}",
                raw_response=json_str,
            )
    
    def is_actionable(self) -> bool:
        """Check if this decision should trigger a trade"""
        return (
            self.parse_success
            and self.decision in ("LONG", "SHORT")
            and self.confidence > 0.5
            and self.size.notional_usd > 0
        )
    
    def to_dict(self) -> dict:
        return asdict(self)


# Output schema as string for the system prompt
BANKR_OUTPUT_SCHEMA = """{
  "decision": "LONG" | "SHORT" | "NO_TRADE",
  "confidence": 0.0-1.0,
  "entry_zone": {
    "type": "limit" | "market",
    "min_price": <number>,
    "max_price": <number>
  },
  "take_profit": {
    "target_price": <number>,
    "expected_rr": <number>
  },
  "stop_loss": {
    "price": <number>,
    "risk_pct_equity": <number>
  },
  "max_leverage": <number>,
  "size": {
    "notional_usd": <number>,
    "contracts": <number>
  },
  "time_horizon_hours": <number>,
  "reason": "<string explaining the trade thesis>"
}"""


# ─────────────────────────────────────────────────────────────────
# Perp Trade Execution Schemas (Bankr = brain + hands mode)
# ─────────────────────────────────────────────────────────────────

@dataclass
class TradeConstraints:
    """Constraints Bankr must respect when executing trades"""
    max_leverage: float = 5.0
    max_usdc_per_trade: float = 350.0
    daily_loss_cap: float = 200.0


@dataclass
class TradeIntent:
    """What we want Bankr to do"""
    symbol: str = "ETH-PERP"
    direction: Literal["LONG", "SHORT"] = "LONG"
    size_usdc: float = 0.0
    reason: str = ""


@dataclass
class PerpTradeCommand:
    """
    Command schema for asking Bankr to execute a perp trade directly.
    
    This is the "Bankr = brain AND hands" mode where:
    - We provide intent (what we want) and constraints (limits)
    - Bankr executes the trade on Avantis
    - We don't maintain any Avantis integration code
    """
    mode: Literal["perp_trade"] = "perp_trade"
    venue: str = "avantis"
    wallet: str = ""
    constraints: TradeConstraints = field(default_factory=TradeConstraints)
    intent: TradeIntent = field(default_factory=TradeIntent)
    
    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "venue": self.venue,
            "wallet": self.wallet,
            "constraints": asdict(self.constraints),
            "intent": asdict(self.intent),
        }
    
    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)
    
    @classmethod
    def from_dict(cls, data: dict) -> "PerpTradeCommand":
        constraints = data.get("constraints", {})
        intent = data.get("intent", {})
        return cls(
            mode=data.get("mode", "perp_trade"),
            venue=data.get("venue", "avantis"),
            wallet=data.get("wallet", ""),
            constraints=TradeConstraints(
                max_leverage=float(constraints.get("max_leverage", 5.0)),
                max_usdc_per_trade=float(constraints.get("max_usdc_per_trade", 350.0)),
                daily_loss_cap=float(constraints.get("daily_loss_cap", 200.0)),
            ),
            intent=TradeIntent(
                symbol=intent.get("symbol", "ETH-PERP"),
                direction=intent.get("direction", "LONG"),
                size_usdc=float(intent.get("size_usdc", 0)),
                reason=intent.get("reason", ""),
            ),
        )


@dataclass
class BankrExecutionResult:
    """Result from Bankr executing a perp trade"""
    success: bool = False
    executed: bool = False
    
    # Trade details (if executed)
    symbol: str = ""
    direction: str = ""
    size_usdc: float = 0.0
    entry_price: float = 0.0
    leverage: float = 1.0
    
    # Transaction info
    tx_hash: str = ""
    job_id: str = ""
    
    # Bankr's response
    summary: str = ""
    error: str = ""
    raw_response: dict = field(default_factory=dict)
    
    @classmethod
    def from_response(cls, resp: dict) -> "BankrExecutionResult":
        """Parse from sidecar response"""
        if resp.get("status") == "error":
            return cls(
                success=False,
                executed=False,
                error=resp.get("error", "Unknown error"),
                raw_response=resp,
            )
        
        # Check if there are transactions (means Bankr executed something)
        transactions = resp.get("transactions", [])
        executed = len(transactions) > 0
        
        # Try to extract trade details from rich data or summary
        summary = resp.get("summary", "")
        
        result = cls(
            success=resp.get("success", False),
            executed=executed,
            job_id=resp.get("jobId", ""),
            summary=summary,
            raw_response=resp,
        )
        
        # If we have transactions, try to parse the first one
        if transactions:
            tx = transactions[0]
            result.tx_hash = tx.get("hash", "")
        
        return result


if __name__ == "__main__":
    # Test the schemas
    ctx = PerpMarketContext(
        asset="DEGEN",
        price=0.0342,
        change_24h_pct=8.7,
        funding_8h=0.012,
        open_interest_usd=1450000,
        volume_24h_usd=2200000,
        liquidation_heatmap_hint="cluster of long liqs 3-5% below spot",
        support_levels=[0.0325, 0.0300],
        resistance_levels=[0.0365, 0.0380],
    )
    print("=== Input Context ===")
    print(ctx.to_json())
    
    # Test parsing a mock Bankr response
    mock_response = '''
    Based on my analysis, here's my recommendation:
    
    {
      "decision": "LONG",
      "confidence": 0.72,
      "entry_zone": {
        "type": "limit",
        "min_price": 0.0338,
        "max_price": 0.0344
      },
      "take_profit": {
        "target_price": 0.0378,
        "expected_rr": 2.4
      },
      "stop_loss": {
        "price": 0.0330,
        "risk_pct_equity": 0.8
      },
      "max_leverage": 3,
      "size": {
        "notional_usd": 800,
        "contracts": 23450
      },
      "time_horizon_hours": 6,
      "reason": "DEGEN broke out of range with rising OI but healthy funding..."
    }
    '''
    
    decision = BankrPerpDecision.from_json(mock_response)
    print("\n=== Parsed Decision ===")
    print(f"Decision: {decision.decision}")
    print(f"Confidence: {decision.confidence}")
    print(f"Actionable: {decision.is_actionable()}")
    print(f"Reason: {decision.reason}")
