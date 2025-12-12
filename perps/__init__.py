# Perps Trading Module - Bankr as Brain + Hands
# 
# Three modes:
# 1. perp_quant: Bankr as oracle - outputs JSON decisions, we execute
# 2. perp_trade: Bankr as executor - Bankr directly trades on Avantis
# 3. perp_sentinel: Local Scout → Bankr Sniper - local watches, Bankr executes extremes
#
# The perps_execution module implements mode 2 (brain + hands)
# The sentinel module implements mode 3 (local scout → bankr sniper)

from .schemas import (
    PerpMarketContext,
    ExistingExposure,
    BankrPerpDecision,
    PerpTradeCommand,
    TradeConstraints,
    TradeIntent,
    BankrExecutionResult,
)

from .perps_execution import (
    BankrExecutor,
    execute_signal,
)

from .sentinel import Sentinel
from .sentinel_config import (
    AssetSentinelConfig,
    get_config,
    get_enabled_symbols,
    SENTINEL_LOOP_INTERVAL,
    SENTINEL_DRY_RUN,
)
from .price_feeds import (
    get_price_snapshot,
    get_btc_snapshot,
    get_eth_snapshot,
    PriceSnapshot,
)
