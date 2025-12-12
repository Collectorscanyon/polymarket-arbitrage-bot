# Polymarket's Binary Arbitrage Bot    


![images](https://github.com/user-attachments/assets/d0db897d-0f4d-45e7-b25d-06eb83048944)


## How does binary arbitrage work?

Let’s consider this market about Kamala winning Vermont by 32 points. We would classify this as Binary because there is 1 yes and 1 no option to place a bet on. Now, the first instance of arbitrage could be within the **same** market. If we add the 72c yes and the 35c no, we get a total of **107**, indicating that there is no arbitrage opportunity here. If for example, it were 72 and 25, we would say there is a **3%** arbitrage opportunity because that total adds up to **97**.

### Explanation:

If you owned both positions, winning the 72c bet would earn you 28c. However, you would lose 25c from the no position and be left with 3 cents **(per contract)**. Conversely, winning the 25c no bet would net you 75 cents, but you subtract 72 because you also own the 72c yes position, netting you 3 cents again. We see here that regardless of the outcome of this binary market, you are guaranteed a 3 cent profit per contract

*Credits to explanation: u/areebkhan280*

## Technical Overview

The bot currently uses Polymarket’s Gamma Markets API, a RESTful service provided by Polymarket. This API serves as a hosted index of all on-chain market data and supplies:

    Resolved and unresolved market outcomes

    Market metadata (e.g., question text, categories, volumes)

    Token pairings and market structures

    Real-time price data for YES/NO or categorical outcomes

By querying this API regularly, the bot identifies pricing discrepancies that signal arbitrage opportunities — for example:

    Binary arbitrage: where YES + NO < $1

    Multi-market arbitrage: where the total of all mutually exclusive YES markets < $1 or > $1


Once an arbitrage signal is detected, the bot logs or alerts the user with actionable information (email,logging file...etc)

## How to install:

    pip install polymarket-arbitrage-bot
    python3 -m bot.main


## Future Updates:

In the future, we aim to expand its functionality to include working with cross-binary prediction markets, such as Kalshi, Robinhood...etc in order to catch potential arbitrage opportunities.

## Disclaimer

**I do not hold any responsibility for any direct or indirect losses, damages, or inconveniences that may arise from your use of this bot. Your use of this bot is at your own risk.**

## Contact 

Need help, have questions, or want to collaborate? Reach out!  

- **Telegram**: [@soladity](https://t.me/soladity)  

---

## Bankr Bot Integration

This project includes integration with [Bankr](https://bankr.bot) for automated trading execution.

### Trading Modes

**1. Polymarket Mode (default)**
- Bankr executes trades on Polymarket prediction markets
- Configured via `BANKR_*` environment variables
- Guardrails: `MAX_USDC_PER_PROMPT`, `DAILY_SPEND_CAP`

**2. Perps Mode - "Bankr = Brain + Hands"**
- Bankr directly executes leveraged perp trades on **Avantis** (Base chain)
- You don't maintain any Avantis integration code - Bankr handles everything
- Send intent + constraints, Bankr executes

### Perps Quick Start

```bash
# 1. Configure environment
PERPS_ENABLED=true
PERPS_MAX_LEVERAGE=5
PERPS_MAX_USDC_PER_TRADE=350
PERPS_DAILY_LOSS_CAP=200
PERPS_DRY_RUN=true

# 2. Start sidecar
cd sidecar && npm start

# 3. Execute a trade via Python
python -m perps.perps_execution long --symbol ETH-PERP --size 100 --reason "Bullish momentum"

# 4. Or use the dashboard
# Open http://localhost:4000/dashboard/perps.html
```

### Perps Command Schema

```python
command = {
    "mode": "perp_trade",
    "venue": "avantis",
    "wallet": "<your-wallet>",
    "constraints": {
        "max_leverage": 5,
        "max_usdc_per_trade": 350,
        "daily_loss_cap": 200
    },
    "intent": {
        "symbol": "ETH-PERP",
        "direction": "LONG",
        "size_usdc": 100,
        "reason": "Strong upward momentum after consolidation"
    }
}
```

### Dashboard

- **Polymarket Dashboard**: `http://localhost:4000/dashboard/`
- **Perps Dashboard**: `http://localhost:4000/dashboard/perps.html`

The perps dashboard includes:
- Quick trade execution (LONG/SHORT buttons)
- Open positions with live P&L
- Signal loop and exit manager controls
- Activity feed with trade history
  
