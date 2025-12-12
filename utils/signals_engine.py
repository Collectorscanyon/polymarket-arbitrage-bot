"""Signals Engine - Process trading signals from external sources.

This module provides infrastructure for:
1. Loading signals from JSON files or HTTP endpoints
2. Filtering signals by confidence, size, and other criteria
3. Routing signals to the appropriate execution handlers

Signal schema:
{
    "market_slug": "will-trump-win-2024",
    "side": "LONG" | "SHORT",
    "confidence": 0.75,  // 0.0 to 1.0
    "size_bucket": "SMALL" | "MEDIUM" | "LARGE",
    "reason": "AI analysis suggests high probability",
    "source": "aixbt" | "custom" | "manual",
    "timestamp": "2024-12-08T10:00:00Z",
    "expires_at": "2024-12-08T12:00:00Z"  // optional
}
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)


@dataclass
class TradingSignal:
    """Represents a trading signal from an external source."""
    market_slug: str
    side: str  # "LONG" or "SHORT"
    confidence: float  # 0.0 to 1.0
    size_bucket: str  # "SMALL", "MEDIUM", "LARGE"
    reason: str = ""
    source: str = "unknown"
    timestamp: Optional[str] = None
    expires_at: Optional[str] = None
    
    # Computed fields
    is_expired: bool = field(init=False, default=False)
    
    def __post_init__(self):
        # Check expiration
        if self.expires_at:
            try:
                expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
                self.is_expired = datetime.now(timezone.utc) > expires
            except Exception:
                self.is_expired = False

    @classmethod
    def from_dict(cls, data: dict) -> "TradingSignal":
        """Create a TradingSignal from a dictionary."""
        return cls(
            market_slug=data.get("market_slug", ""),
            side=data.get("side", "LONG"),
            confidence=float(data.get("confidence", 0.0)),
            size_bucket=data.get("size_bucket", "SMALL"),
            reason=data.get("reason", ""),
            source=data.get("source", "unknown"),
            timestamp=data.get("timestamp"),
            expires_at=data.get("expires_at"),
        )

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "market_slug": self.market_slug,
            "side": self.side,
            "confidence": self.confidence,
            "size_bucket": self.size_bucket,
            "reason": self.reason,
            "source": self.source,
            "timestamp": self.timestamp,
            "expires_at": self.expires_at,
        }


class SignalsEngine:
    """Engine for loading, filtering, and processing trading signals."""

    # Size bucket to USDC mapping
    SIZE_MAP = {
        "SMALL": 2.0,
        "MEDIUM": 5.0,
        "LARGE": 10.0,
    }

    def __init__(
        self,
        min_confidence: float = 0.6,
        allowed_sources: Optional[list[str]] = None,
        signals_file: Optional[str] = None,
        signals_url: Optional[str] = None,
    ):
        """Initialize the signals engine.
        
        Args:
            min_confidence: Minimum confidence threshold (0.0 to 1.0)
            allowed_sources: List of allowed signal sources, or None for all
            signals_file: Path to local signals JSON file
            signals_url: URL to fetch signals from
        """
        self.min_confidence = min_confidence
        self.allowed_sources = allowed_sources
        self.signals_file = signals_file
        self.signals_url = signals_url
        
        self._cached_signals: list[TradingSignal] = []
        self._last_fetch: Optional[datetime] = None

    def load_signals(self, force_refresh: bool = False) -> list[TradingSignal]:
        """Load signals from configured sources.
        
        Args:
            force_refresh: Force reload even if recently fetched
            
        Returns:
            List of TradingSignal objects
        """
        signals = []

        # Load from file
        if self.signals_file:
            file_signals = self._load_from_file(self.signals_file)
            signals.extend(file_signals)

        # Load from URL
        if self.signals_url:
            url_signals = self._load_from_url(self.signals_url)
            signals.extend(url_signals)

        self._cached_signals = signals
        self._last_fetch = datetime.now(timezone.utc)

        log.info(f"Loaded {len(signals)} signals from sources")
        return signals

    def _load_from_file(self, filepath: str) -> list[TradingSignal]:
        """Load signals from a JSON file."""
        try:
            path = Path(filepath)
            if not path.exists():
                log.warning(f"Signals file not found: {filepath}")
                return []

            with open(path) as f:
                data = json.load(f)

            # Handle both single signal and array
            if isinstance(data, list):
                return [TradingSignal.from_dict(s) for s in data]
            elif isinstance(data, dict):
                if "signals" in data:
                    return [TradingSignal.from_dict(s) for s in data["signals"]]
                return [TradingSignal.from_dict(data)]

            return []

        except Exception as e:
            log.error(f"Error loading signals from file {filepath}: {e}")
            return []

    def _load_from_url(self, url: str) -> list[TradingSignal]:
        """Load signals from an HTTP endpoint."""
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            # Handle both single signal and array
            if isinstance(data, list):
                return [TradingSignal.from_dict(s) for s in data]
            elif isinstance(data, dict):
                if "signals" in data:
                    return [TradingSignal.from_dict(s) for s in data["signals"]]
                return [TradingSignal.from_dict(data)]

            return []

        except Exception as e:
            log.error(f"Error loading signals from URL {url}: {e}")
            return []

    def filter_signals(
        self,
        signals: Optional[list[TradingSignal]] = None,
    ) -> list[TradingSignal]:
        """Filter signals based on configured criteria.
        
        Args:
            signals: Signals to filter, or use cached if None
            
        Returns:
            Filtered list of signals
        """
        if signals is None:
            signals = self._cached_signals

        filtered = []
        for signal in signals:
            # Skip expired signals
            if signal.is_expired:
                log.debug(f"Skipping expired signal: {signal.market_slug}")
                continue

            # Check confidence threshold
            if signal.confidence < self.min_confidence:
                log.debug(f"Skipping low-confidence signal: {signal.market_slug} ({signal.confidence})")
                continue

            # Check allowed sources
            if self.allowed_sources and signal.source not in self.allowed_sources:
                log.debug(f"Skipping signal from disallowed source: {signal.source}")
                continue

            filtered.append(signal)

        log.info(f"Filtered to {len(filtered)} actionable signals")
        return filtered

    def get_stake_for_signal(self, signal: TradingSignal) -> float:
        """Get the USDC stake amount for a signal based on size bucket."""
        return self.SIZE_MAP.get(signal.size_bucket, self.SIZE_MAP["SMALL"])

    def process_signals(self) -> list[dict]:
        """Load, filter, and prepare signals for execution.
        
        Returns:
            List of execution-ready signal dicts with stake amounts
        """
        signals = self.load_signals()
        filtered = self.filter_signals(signals)

        execution_ready = []
        for signal in filtered:
            execution_ready.append({
                "market_slug": signal.market_slug,
                "side": signal.side,
                "confidence": signal.confidence,
                "stake_usdc": self.get_stake_for_signal(signal),
                "reason": signal.reason,
                "source": signal.source,
            })

        return execution_ready


# Default engine instance
_default_engine: Optional[SignalsEngine] = None


def get_engine(
    min_confidence: float = 0.6,
    signals_file: Optional[str] = None,
    signals_url: Optional[str] = None,
) -> SignalsEngine:
    """Get or create the default signals engine."""
    global _default_engine
    if _default_engine is None:
        _default_engine = SignalsEngine(
            min_confidence=min_confidence,
            signals_file=signals_file,
            signals_url=signals_url,
        )
    return _default_engine


def process_signals(
    signals_file: Optional[str] = None,
    signals_url: Optional[str] = None,
) -> list[dict]:
    """Convenience function to process signals."""
    engine = get_engine(signals_file=signals_file, signals_url=signals_url)
    return engine.process_signals()
