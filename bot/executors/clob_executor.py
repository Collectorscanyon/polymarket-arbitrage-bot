"""Direct Polymarket CLOB execution (no Bankr prompt latency).

This module is intentionally self-contained and uses a lazy import for
`py-clob-client` so the rest of the repo (and unit tests) can run without it.

Required env vars for live trading:
- POLYMARKET_PRIVATE_KEY

Optional env vars (proxy wallet / funder mode):
- POLYMARKET_SIGNATURE_TYPE (0, 1, or 2)
- POLYMARKET_FUNDER_ADDRESS

Safety:
- TRADING_ENABLED=true must be set (unless caller passes dry_run=True)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


HOST_DEFAULT = "https://clob.polymarket.com"
CHAIN_ID_POLYGON = 137


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def _maybe_float(val: Optional[str]) -> Optional[float]:
    if val is None or str(val).strip() == "":
        return None
    return float(val)


@dataclass(frozen=True)
class CLOBConfig:
    host: str = HOST_DEFAULT
    chain_id: int = CHAIN_ID_POLYGON
    signature_type: int = 0
    funder: Optional[str] = None
    max_estimated_usdc_per_order: Optional[float] = None

    @staticmethod
    def from_env() -> "CLOBConfig":
        signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
        funder = os.getenv("POLYMARKET_FUNDER_ADDRESS") or os.getenv("POLYMARKET_PROXY_ADDRESS")
        cap = _maybe_float(os.getenv("CLOB_MAX_ESTIMATED_USDC_PER_ORDER"))
        return CLOBConfig(
            host=os.getenv("POLYMARKET_CLOB_HOST", HOST_DEFAULT),
            chain_id=int(os.getenv("POLYMARKET_CHAIN_ID", str(CHAIN_ID_POLYGON))),
            signature_type=signature_type,
            funder=funder,
            max_estimated_usdc_per_order=cap,
        )


class DirectCLOBExecutor:
    """Thin wrapper around `py-clob-client` with conservative fill checking."""

    def __init__(self, cfg: Optional[CLOBConfig] = None):
        self.cfg = cfg or CLOBConfig.from_env()

        # Lazy import so tests can run without this dependency.
        try:
            from py_clob_client.client import ClobClient  # type: ignore
        except Exception as e:  # pragma: no cover
            raise RuntimeError(
                "py-clob-client is required for direct CLOB execution. "
                "Install with: pip install py-clob-client"
            ) from e

        private_key = _require_env("POLYMARKET_PRIVATE_KEY")

        self._client = ClobClient(
            self.cfg.host,
            key=private_key,
            chain_id=self.cfg.chain_id,
            signature_type=self.cfg.signature_type,
            funder=self.cfg.funder,
        )

        # Derive once; keep client warm.
        self._client.set_api_creds(self._client.create_or_derive_api_creds())

    def place_limit(
        self,
        *,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "GTC",
        estimated_usdc: Optional[float] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        if not dry_run and not _env_bool("TRADING_ENABLED", False):
            raise RuntimeError("TRADING_ENABLED is false; refusing to place live orders")

        if (
            estimated_usdc is not None
            and self.cfg.max_estimated_usdc_per_order is not None
            and float(estimated_usdc) > float(self.cfg.max_estimated_usdc_per_order)
        ):
            raise RuntimeError(
                f"Estimated order size {estimated_usdc:.2f} USDC exceeds cap "
                f"CLOB_MAX_ESTIMATED_USDC_PER_ORDER={self.cfg.max_estimated_usdc_per_order}"
            )

        if dry_run:
            return {
                "dry_run": True,
                "token_id": token_id,
                "side": side,
                "price": float(price),
                "size": float(size),
                "order_id": None,
                "raw": {},
            }

        # Lazy imports of types/constants.
        from py_clob_client.clob_types import OrderArgs, OrderType  # type: ignore

        # side should be the BUY/SELL constants, but client also accepts strings.
        order = OrderArgs(token_id=token_id, price=float(price), size=float(size), side=str(side))
        signed = self._client.create_order(order)
        ot = getattr(OrderType, str(order_type), OrderType.GTC)
        resp = self._client.post_order(signed, ot)

        order_id = resp.get("orderID") or resp.get("orderId") or resp.get("id")
        return {"order_id": order_id, "raw": resp}

    def cancel(self, order_id: str) -> Dict[str, Any]:
        return self._client.cancel(order_id)

    def get_order(self, order_id: str) -> Dict[str, Any]:
        return self._client.get_order(order_id)

    def wait_until_filled(
        self,
        *,
        order_id: str,
        target_size: Optional[float],
        timeout_seconds: float,
        poll_interval_seconds: float = 0.5,
    ) -> Tuple[bool, Dict[str, Any]]:
        deadline = time.time() + float(timeout_seconds)
        last: Dict[str, Any] = {}
        while time.time() < deadline:
            last = self.get_order(order_id)
            if _order_looks_filled(last, target_size=target_size):
                return True, last
            time.sleep(float(poll_interval_seconds))
        return False, last


def _order_looks_filled(raw: Dict[str, Any], *, target_size: Optional[float]) -> bool:
    if not raw:
        return False

    status = str(raw.get("status") or "").upper()
    if status in ("FILLED", "EXECUTED"):
        return True
    if status in ("CANCELED", "CANCELLED", "REJECTED", "FAILED"):
        return False

    # Try common numeric fields.
    def f(key: str) -> Optional[float]:
        v = raw.get(key)
        if v is None:
            return None
        try:
            return float(v)
        except Exception:
            return None

    size = f("size") or f("original_size")
    matched = f("size_matched") or f("matched") or f("filled") or f("filled_size")
    remaining = f("remaining") or f("size_remaining")

    if remaining is not None:
        if remaining <= 0:
            return True

    effective_target = float(target_size) if target_size is not None else size
    if effective_target is not None and matched is not None:
        return matched + 1e-9 >= float(effective_target)

    return False
