# bot/utils/polymarket.py
"""
Polymarket price fetching utilities.
"""

import logging
from typing import Optional

from utils.http_client import request as http_request


GAMMA_API_BASE = "https://gamma-api.polymarket.com"
logger = logging.getLogger(__name__)


def get_market_price(market_slug: str, side: str = "YES") -> float:
    """
    Fetch the current price for a market outcome from Polymarket.
    
    Args:
        market_slug: The market identifier/slug
        side: "YES" or "NO"
        
    Returns:
        Current price as a float (0.0 to 1.0)
        
    Raises:
        ValueError: If market not found or price unavailable
    """
    try:
        # Try to get market data from Gamma API
        resp = http_request(
            "GET",
            f"{GAMMA_API_BASE}/markets",
            params={"slug": market_slug},
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json()
        
        if not markets:
            # Try searching by condition_id or question
            resp = http_request(
                "GET",
                f"{GAMMA_API_BASE}/markets",
                params={"_limit": 100, "active": "true"},
                timeout=10,
            )
            resp.raise_for_status()
            markets = resp.json()
            
            # Search for matching market
            market = None
            for m in markets:
                if market_slug.lower() in (m.get("slug", "") or "").lower():
                    market = m
                    break
                if market_slug.lower() in (m.get("question", "") or "").lower():
                    market = m
                    break
            
            if not market:
                raise ValueError(f"Market not found: {market_slug}")
        else:
            market = markets[0] if isinstance(markets, list) else markets
        
        # Extract price based on side
        if side.upper() == "YES":
            price = float(market.get("outcomePrices", [0.5, 0.5])[0])
        else:
            price = float(market.get("outcomePrices", [0.5, 0.5])[1])
        
        return price
        
    except Exception as e:
        raise ValueError(f"Failed to fetch price for {market_slug}: {e}")


def get_market_prices(market_slug: str) -> tuple[float, float]:
    """
    Fetch both YES and NO prices for a market.
    
    Returns:
        Tuple of (yes_price, no_price)
    """
    try:
        resp = http_request(
            "GET",
            f"{GAMMA_API_BASE}/markets",
            params={"slug": market_slug},
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json()
        
        if not markets:
            return (0.5, 0.5)  # Default mid-market
            
        market = markets[0] if isinstance(markets, list) else markets
        prices = market.get("outcomePrices", [0.5, 0.5])
        
        return (float(prices[0]), float(prices[1]))
        
    except Exception as e:
        logger.debug("Failed to fetch prices for %s: %s", market_slug, e)
        return (0.5, 0.5)


def get_market_info(market_slug: str) -> Optional[dict]:
    """
    Get full market information.
    
    Returns:
        Market data dict or None if not found
    """
    try:
        resp = http_request(
            "GET",
            f"{GAMMA_API_BASE}/markets",
            params={"slug": market_slug},
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json()
        
        if markets:
            return markets[0] if isinstance(markets, list) else markets
        return None
        
    except Exception as e:
        logger.debug("Failed to fetch market info for %s: %s", market_slug, e)
        return None
