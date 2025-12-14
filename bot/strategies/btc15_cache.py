"""
BTC15 Active Set Cache

Maintains a tight in-memory cache of active btc-updown-15m markets.
Stops scanning the whole universe - only tracks live 15m markets.

Update logic:
- Every tick: call "latest active events" (limit 50-200)
- Filter only btc-updown-15m-*
- Add new slugs, drop expired slugs
- Only fetch full details for NEW slugs
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Any, Iterable

from .btc15_slug_source import fetch_candidate_events

try:
    from utils.http_client import get_json
except ImportError:
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from utils.http_client import get_json


log = logging.getLogger(__name__)

GAMMA_API_BASE = "https://gamma-api.polymarket.com"


def normalize_token_ids(value: Any) -> List[str]:
    """Normalize token IDs into a list of strings.

    Gamma sometimes returns clobTokenIds as a JSON-encoded string (e.g. '["123","456"]').
    If treated as an iterable string, code will iterate character-by-character, producing
    token_id values like '[' and '"' and breaking CLOB /book requests.
    """

    if value is None:
        return []

    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []

        decode_failed = object()
        try:
            decoded = json.loads(s)
        except Exception:
            decoded = decode_failed

        if decoded is decode_failed:
            token = s.strip().strip('"')
            return [token] if token else []

        # JSON null
        if decoded is None:
            return []

        # JSON string like '"123"'
        if isinstance(decoded, str):
            token = decoded.strip().strip('"')
            return [token] if token else []

        value = decoded

    if isinstance(value, (list, tuple)):
        out: List[str] = []
        for item in value:
            if item is None:
                continue
            token = str(item).strip().strip('"')
            if token:
                out.append(token)
        return out

    token = str(value).strip().strip('"')
    return [token] if token else []


@dataclass
class BTC15MarketInfo:
    """Cached metadata for a single BTC15 market."""
    slug: str
    condition_id: str
    question: str
    end_date: datetime
    outcomes: List[str]  # ["Up", "Down"] or ["Yes", "No"]
    token_ids: List[str]  # CLOB token IDs for YES/NO
    volume_usdc: float
    last_updated: float = field(default_factory=time.time)
    
    @property
    def minutes_to_expiry(self) -> float:
        """Minutes until market closes."""
        now = datetime.now(timezone.utc)
        delta = self.end_date - now
        return max(0, delta.total_seconds() / 60)

    @property
    def seconds_to_expiry(self) -> float:
        """Seconds until market closes."""
        now = datetime.now(timezone.utc)
        delta = self.end_date - now
        return max(0, delta.total_seconds())
    
    @property
    def is_expired(self) -> bool:
        """True if market has passed its end date."""
        return self.minutes_to_expiry <= 0


class BTC15ActiveSetCache:
    """
    Maintains the active set of btc-updown-15m markets.
    
    This replaces the expensive "scan 1900 markets" pattern with a tight
    cache that only tracks live BTC 15m markets.
    """
    
    def __init__(self, max_age_minutes: float = 30.0, no_trade_last_seconds: int = 90):
        self._markets: Dict[str, BTC15MarketInfo] = {}
        self._known_slugs: Set[str] = set()  # All slugs we've ever seen (for dedup)
        self._last_refresh: float = 0.0
        self._max_age_minutes = max_age_minutes
        self._no_trade_last_seconds = int(no_trade_last_seconds)
        
        # Metrics
        self._refresh_count = 0
        self._new_slugs_found = 0
        self._expired_removed = 0
    
    @property
    def active_markets(self) -> Dict[str, BTC15MarketInfo]:
        """Return all currently active (non-expired) markets."""
        return {k: v for k, v in self._markets.items() if not v.is_expired}
    
    @property
    def tradeable_markets(self) -> Dict[str, BTC15MarketInfo]:
        """Return markets in the tradeable window.

        Default guardrails:
        - 2 <= minutes_to_expiry <= 14
        - and NOT in last N seconds to expiry (default 90s)
        """
        return {k: v for k, v in self._markets.items() 
            if 2 <= v.minutes_to_expiry <= 14 and v.seconds_to_expiry > self._no_trade_last_seconds}
    
    @property
    def upcoming_markets(self) -> Dict[str, BTC15MarketInfo]:
        """Return markets expiring soon (14-30 minutes) - for monitoring."""
        return {k: v for k, v in self._markets.items() 
                if 14 < v.minutes_to_expiry <= 30}
    
    @property
    def active_slugs(self) -> Set[str]:
        """Return set of active market slugs."""
        return set(self.active_markets.keys())
    
    def get(self, slug: str) -> Optional[BTC15MarketInfo]:
        """Get cached info for a specific slug."""
        return self._markets.get(slug)
    
    def refresh(self, limit: int = 100) -> int:
        """
        Refresh the active set from Gamma API.
        
        Returns number of NEW markets discovered this refresh.
        """
        start_ts = time.time()
        new_count = 0
        
        try:
            # Fetch latest active events (cheap, limited query)
            url = f"{GAMMA_API_BASE}/events?closed=false&order=id&ascending=false&limit={limit}"
            events = get_json(url, timeout=10)
            
            if not events:
                log.debug("[BTC15Cache] No events returned from Gamma")
                return 0
            
            for event in events:
                # Filter to btc-updown-15m patterns
                slug = event.get("slug", "")
                if not slug.startswith("btc-updown-15m"):
                    continue
                
                # Check if we've already cached this
                if slug in self._markets:
                    # Update volume/last_updated but don't re-fetch full details
                    try:
                        self._markets[slug].token_ids = normalize_token_ids(self._markets[slug].token_ids)
                    except Exception:
                        # Best-effort; cache sanitization must never break refresh.
                        pass
                    self._markets[slug].last_updated = time.time()
                    continue
                
                # NEW slug - fetch full details and cache
                try:
                    market_info = self._fetch_market_details(event)
                    if market_info:
                        self._markets[slug] = market_info
                        self._known_slugs.add(slug)
                        new_count += 1
                        log.info("[BTC15Cache] NEW market: %s (expires in %.1f min)", 
                                 slug, market_info.minutes_to_expiry)
                except Exception as e:
                    log.warning("[BTC15Cache] Failed to fetch details for %s: %s", slug, e)
            
            # Prune only EXPIRED markets (keep future ones for monitoring)
            expired = [k for k, v in self._markets.items() if v.is_expired]
            for slug in expired:
                del self._markets[slug]
                self._expired_removed += 1
            
            self._last_refresh = time.time()
            self._refresh_count += 1
            self._new_slugs_found += new_count
            
            elapsed_ms = (time.time() - start_ts) * 1000
            log.debug("[BTC15Cache] Refresh done in %.0fms: %d active, %d new, %d pruned",
                      elapsed_ms, len(self._markets), new_count, len(expired))
            
        except Exception as e:
            log.error("[BTC15Cache] Refresh failed: %s", e)
        
        return new_count

    def refresh_deterministic(
        self,
        offsets: Iterable[int] = (0, -1, 1, 2),
    ) -> int:
        """Refresh using deterministic slug fetch around 'now'.

        This avoids the failure mode where "latest active events" mostly returns
        far-future pre-created buckets.
        """
        start_ts = time.time()
        new_count = 0

        try:
            lookups = fetch_candidate_events(offsets=offsets)
            for lookup in lookups:
                if not lookup.found:
                    continue

                for event in lookup.events:
                    slug = (event.get("slug") or "").strip()
                    if not slug.startswith("btc-updown-15m"):
                        continue

                    if slug in self._markets:
                        try:
                            self._markets[slug].token_ids = normalize_token_ids(self._markets[slug].token_ids)
                        except Exception:
                            pass
                        self._markets[slug].last_updated = time.time()
                        continue

                    try:
                        market_info = self._fetch_market_details(event)
                        if market_info:
                            self._markets[slug] = market_info
                            self._known_slugs.add(slug)
                            new_count += 1
                            log.info(
                                "[BTC15Cache] NEW market (deterministic): %s (expires in %.1f min)",
                                slug,
                                market_info.minutes_to_expiry,
                            )
                    except Exception as e:
                        log.warning("[BTC15Cache] Failed to fetch details for %s: %s", slug, e)

            # Prune expired
            expired = [k for k, v in self._markets.items() if v.is_expired]
            for slug in expired:
                del self._markets[slug]
                self._expired_removed += 1

            self._last_refresh = time.time()
            self._refresh_count += 1
            self._new_slugs_found += new_count

            elapsed_ms = (time.time() - start_ts) * 1000
            log.debug(
                "[BTC15Cache] Deterministic refresh done in %.0fms: %d cached, %d new, %d pruned",
                elapsed_ms,
                len(self._markets),
                new_count,
                len(expired),
            )
        except Exception as e:
            log.error("[BTC15Cache] Deterministic refresh failed: %s", e)

        return new_count
    
    def _fetch_market_details(self, event: dict) -> Optional[BTC15MarketInfo]:
        """Fetch full market details for a new slug."""
        slug = event.get("slug", "")
        
        # Get markets for this event
        markets = event.get("markets", [])
        if not markets:
            # Try fetching from markets endpoint
            url = f"{GAMMA_API_BASE}/markets?slug={slug}"
            try:
                markets = get_json(url, timeout=5) or []
            except Exception:
                return None
        
        if not markets:
            return None
        
        # Take the first market (BTC15 events typically have one market)
        m = markets[0] if isinstance(markets, list) else markets
        
        # Parse end date
        end_date_str = m.get("endDate") or event.get("endDate", "")
        try:
            # Handle ISO format with Z suffix
            if end_date_str.endswith("Z"):
                end_date_str = end_date_str[:-1] + "+00:00"
            end_date = datetime.fromisoformat(end_date_str)
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            log.warning("[BTC15Cache] Could not parse endDate for %s: %s", slug, end_date_str)
            return None
        
        # Extract token IDs for CLOB queries
        clob_token_ids = normalize_token_ids(m.get("clobTokenIds"))
        if len(clob_token_ids) < 2:
            # Try to get from outcomes
            outcomes = m.get("outcomes", [])
            clob_token_ids = [str(i) for i in range(len(outcomes))]
        
        return BTC15MarketInfo(
            slug=slug,
            condition_id=m.get("conditionId", ""),
            question=m.get("question", event.get("title", slug)),
            end_date=end_date,
            outcomes=m.get("outcomes", ["Up", "Down"]),
            token_ids=clob_token_ids,
            volume_usdc=float(m.get("volume", 0) or 0),
        )
    
    def get_stats(self) -> dict:
        """Return cache statistics for monitoring."""
        return {
            "active_count": len(self.active_markets),
            "total_cached": len(self._markets),
            "known_slugs": len(self._known_slugs),
            "refresh_count": self._refresh_count,
            "new_slugs_found": self._new_slugs_found,
            "expired_removed": self._expired_removed,
            "last_refresh_ago_sec": time.time() - self._last_refresh if self._last_refresh else None,
        }


# Singleton instance
_cache: Optional[BTC15ActiveSetCache] = None


def get_btc15_cache() -> BTC15ActiveSetCache:
    """Get or create the singleton BTC15 cache."""
    global _cache
    if _cache is None:
        _cache = BTC15ActiveSetCache()
    return _cache
