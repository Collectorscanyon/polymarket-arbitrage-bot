"""Two-phase commit executor for BTC15 brackets.

This module provides a conservative execution layer that:
- Persists per-bracket execution state in SQLite (idempotent resume)
- Executes leg A first and only executes leg B after leg A is confirmed

Important note about Polymarket execution in this repo:
- The bot executes via the Bankr sidecar (/prompt) which uses Bankr SDK.
- We therefore treat the sidecar prompt result as the source of truth for whether
  a leg was actually executed (and we instruct Bankr to *confirm fills*).

If you later add direct CLOB order placement + order status endpoints, you can
swap out the transport while keeping the same state machine + persistence.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Protocol, Tuple

from utils.http_client import post_json


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ExecutionConfig:
    leg_a_timeout_seconds: int = 12
    leg_b_timeout_seconds: int = 18
    max_unhedged_seconds: int = 25
    max_open_brackets: int = 2
    max_estimated_usdc_per_bracket: float = 0.0
    daily_estimated_usdc_cap: float = 0.0
    trading_enabled: bool = False
    dry_run: bool = True


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _utc_today_yyyy_mm_dd() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class BTC15ExecutionState:
    PLANNED = "PLANNED"
    LEG_A_PLACED = "LEG_A_PLACED"
    LEG_A_FILLED = "LEG_A_FILLED"
    LEG_B_PLACED = "LEG_B_PLACED"
    HEDGED_FILLED = "HEDGED_FILLED"
    DONE = "DONE"
    ABORTED = "ABORTED"


@dataclass
class BTC15ExecutionRecord:
    execution_id: str
    slug: str
    up_token_id: str
    down_token_id: str
    target_shares: float
    state: str = BTC15ExecutionState.PLANNED
    created_at: str = ""
    updated_at: str = ""
    leg_a_job_id: Optional[str] = None
    leg_b_job_id: Optional[str] = None
    leg_a_order_id: Optional[str] = None
    leg_b_order_id: Optional[str] = None
    execution_backend: Optional[str] = None
    estimated_total_usdc: Optional[float] = None
    leg_a_raw_json: Optional[str] = None
    leg_b_raw_json: Optional[str] = None


class BTC15OrdersStore:
    """SQLite persistence for two-phase BTC15 execution."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.getenv("BTC15_ORDERS_DB_PATH", "btc15_orders.sqlite3")
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS btc15_orders (
                  execution_id TEXT PRIMARY KEY,
                  slug TEXT NOT NULL,
                  up_token_id TEXT NOT NULL,
                  down_token_id TEXT NOT NULL,
                  target_shares REAL NOT NULL,
                  state TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  leg_a_job_id TEXT,
                  leg_b_job_id TEXT,
                  leg_a_order_id TEXT,
                  leg_b_order_id TEXT,
                  execution_backend TEXT,
                  estimated_total_usdc REAL,
                  leg_a_raw_json TEXT,
                  leg_b_raw_json TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_btc15_orders_state ON btc15_orders(state)")
            # Backfill columns for older DBs (best-effort, idempotent)
            for stmt in (
                "ALTER TABLE btc15_orders ADD COLUMN leg_a_order_id TEXT",
                "ALTER TABLE btc15_orders ADD COLUMN leg_b_order_id TEXT",
                "ALTER TABLE btc15_orders ADD COLUMN execution_backend TEXT",
                "ALTER TABLE btc15_orders ADD COLUMN estimated_total_usdc REAL",
            ):
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass
            conn.commit()

    def get(self, execution_id: str) -> Optional[BTC15ExecutionRecord]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM btc15_orders WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            return BTC15ExecutionRecord(
                execution_id=d["execution_id"],
                slug=d["slug"],
                up_token_id=d["up_token_id"],
                down_token_id=d["down_token_id"],
                target_shares=float(d["target_shares"]),
                state=d["state"],
                created_at=d["created_at"],
                updated_at=d["updated_at"],
                leg_a_job_id=d.get("leg_a_job_id"),
                leg_b_job_id=d.get("leg_b_job_id"),
                leg_a_order_id=d.get("leg_a_order_id"),
                leg_b_order_id=d.get("leg_b_order_id"),
                execution_backend=d.get("execution_backend"),
                estimated_total_usdc=(float(d["estimated_total_usdc"]) if d.get("estimated_total_usdc") is not None else None),
                leg_a_raw_json=d.get("leg_a_raw_json"),
                leg_b_raw_json=d.get("leg_b_raw_json"),
            )

    def upsert(self, rec: BTC15ExecutionRecord) -> None:
        now = _utcnow_iso()
        if not rec.created_at:
            rec.created_at = now
        rec.updated_at = now

        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO btc15_orders (
                  execution_id, slug, up_token_id, down_token_id, target_shares,
                  state, created_at, updated_at,
                                    leg_a_job_id, leg_b_job_id,
                                    leg_a_order_id, leg_b_order_id,
                                    execution_backend, estimated_total_usdc,
                                    leg_a_raw_json, leg_b_raw_json
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(execution_id) DO UPDATE SET
                  state=excluded.state,
                  updated_at=excluded.updated_at,
                  leg_a_job_id=excluded.leg_a_job_id,
                  leg_b_job_id=excluded.leg_b_job_id,
                                    leg_a_order_id=excluded.leg_a_order_id,
                                    leg_b_order_id=excluded.leg_b_order_id,
                                    execution_backend=excluded.execution_backend,
                                    estimated_total_usdc=excluded.estimated_total_usdc,
                  leg_a_raw_json=excluded.leg_a_raw_json,
                  leg_b_raw_json=excluded.leg_b_raw_json
                """,
                (
                    rec.execution_id,
                    rec.slug,
                    rec.up_token_id,
                    rec.down_token_id,
                    float(rec.target_shares),
                    rec.state,
                    rec.created_at,
                    rec.updated_at,
                    rec.leg_a_job_id,
                    rec.leg_b_job_id,
                                        rec.leg_a_order_id,
                                        rec.leg_b_order_id,
                                        rec.execution_backend,
                                        (float(rec.estimated_total_usdc) if rec.estimated_total_usdc is not None else None),
                    rec.leg_a_raw_json,
                    rec.leg_b_raw_json,
                ),
            )
            conn.commit()

    def count_open(self) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM btc15_orders
                WHERE state NOT IN (?, ?)
                """,
                (BTC15ExecutionState.DONE, BTC15ExecutionState.ABORTED),
            ).fetchone()
            return int(row["c"] if row else 0)

    def sum_estimated_usdc_for_day(self, utc_day: str) -> float:
        """Best-effort daily cap based on *estimated* bracket size.

        We sum both DONE and open brackets created on the given UTC day.
        """
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(COALESCE(estimated_total_usdc, 0.0)), 0.0) AS s
                FROM btc15_orders
                WHERE substr(created_at, 1, 10) = ?
                """,
                (utc_day,),
            ).fetchone()
            return float(row["s"] if row else 0.0)


class BankrTransport:
    """Minimal transport wrapper around the sidecar /prompt endpoint."""

    def __init__(self, sidecar_url: str):
        self.sidecar_url = sidecar_url.rstrip("/")

    def prompt(self, message: str, estimated_usdc: float, dry_run: bool, timeout: float) -> Dict[str, Any]:
        payload = {"message": message, "estimated_usdc": float(estimated_usdc), "dry_run": bool(dry_run)}
        return post_json(f"{self.sidecar_url}/prompt", payload, timeout=timeout) or {}


class LegExecutor(Protocol):
    def place_limit(
        self,
        *,
        slug: str,
        leg_name: str,
        token_id: str,
        side: str,
        target_shares: float,
        price_limit: float,
        estimated_usdc: float,
        timeout_seconds: int,
        dry_run: bool,
    ) -> Tuple[str, str, Dict[str, Any]]:
        """Returns (id_kind, external_id, raw_place). id_kind is 'job' or 'order'."""

    def confirm_filled(
        self,
        *,
        id_kind: str,
        external_id: str,
        target_shares: float,
        timeout_seconds: int,
        dry_run: bool,
        raw_place: Dict[str, Any],
    ) -> Tuple[bool, Dict[str, Any]]:
        """Returns (filled, raw_confirm)."""

    def cancel(self, *, id_kind: str, external_id: str) -> None:
        """Best-effort cancel."""


class BankrLegExecutor:
    def __init__(self, transport: BankrTransport):
        self.transport = transport

    def place_limit(
        self,
        *,
        slug: str,
        leg_name: str,
        token_id: str,
        side: str,
        target_shares: float,
        price_limit: float,
        estimated_usdc: float,
        timeout_seconds: int,
        dry_run: bool,
    ) -> Tuple[str, str, Dict[str, Any]]:
        outcome = "UP" if leg_name.upper() == "A" else "DOWN"
        msg = (
            f"BTC15 two-phase bracket. MARKET SLUG: {slug}.\n"
            f"LEG {leg_name}: Place a LIMIT BUY for {target_shares:.2f} shares of the {outcome} outcome. "
            f"Use token_id={token_id} if needed. Limit price <= {price_limit:.4f}.\n"
            f"CONFIRMATION REQUIREMENT: Only report success if the order FILLED (or fully executed). "
            f"If not filled within {timeout_seconds}s, cancel and report NOT_FILLED."
        )
        res = self.transport.prompt(
            msg,
            estimated_usdc=float(estimated_usdc),
            dry_run=bool(dry_run),
            timeout=max(5.0, float(timeout_seconds) + 20.0),
        )
        external_id = str(res.get("jobId") or "")
        return "job", external_id, res

    def confirm_filled(
        self,
        *,
        id_kind: str,
        external_id: str,
        target_shares: float,
        timeout_seconds: int,
        dry_run: bool,
        raw_place: Dict[str, Any],
    ) -> Tuple[bool, Dict[str, Any]]:
        # Bankr prompt is instructed to only return success if filled; we treat
        # the returned payload as the confirmation.
        return _looks_like_filled(raw_place), raw_place

    def cancel(self, *, id_kind: str, external_id: str) -> None:
        # Sidecar does not expose a stable cancel-by-job endpoint here.
        return None


class CLOBLegExecutor:
    def __init__(self):
        from bot.executors.clob_executor import DirectCLOBExecutor

        self._exec = DirectCLOBExecutor()

    def place_limit(
        self,
        *,
        slug: str,
        leg_name: str,
        token_id: str,
        side: str,
        target_shares: float,
        price_limit: float,
        estimated_usdc: float,
        timeout_seconds: int,
        dry_run: bool,
    ) -> Tuple[str, str, Dict[str, Any]]:
        placed = self._exec.place_limit(
            token_id=token_id,
            side=side,
            price=float(price_limit),
            size=float(target_shares),
            order_type="GTC",
            estimated_usdc=float(estimated_usdc),
            dry_run=bool(dry_run),
        )
        order_id = str(placed.get("order_id") or "")
        return "order", order_id, placed

    def confirm_filled(
        self,
        *,
        id_kind: str,
        external_id: str,
        target_shares: float,
        timeout_seconds: int,
        dry_run: bool,
        raw_place: Dict[str, Any],
    ) -> Tuple[bool, Dict[str, Any]]:
        if dry_run:
            return True, {"placed": raw_place, "confirmed": {"dry_run": True}}
        if not external_id:
            return False, {"placed": raw_place, "confirmed": {"error": "missing_order_id"}}

        filled, last = self._exec.wait_until_filled(
            order_id=external_id,
            target_size=float(target_shares),
            timeout_seconds=float(timeout_seconds),
        )
        if not filled:
            try:
                self._exec.cancel(external_id)
            except Exception:
                pass
        return filled, {"placed": raw_place, "last": last}

    def cancel(self, *, id_kind: str, external_id: str) -> None:
        if not external_id:
            return None
        try:
            self._exec.cancel(external_id)
        except Exception:
            return None


class BTC15TwoPhaseExecutor:
    """Two-phase commit executor with pluggable leg executor + SQLite persistence."""

    def __init__(
        self,
        store: BTC15OrdersStore,
        leg_executor: LegExecutor,
        cfg: ExecutionConfig,
        backend_name: str = "bankr",
    ):
        self.store = store
        self.leg_executor = leg_executor
        self.cfg = cfg
        self.backend_name = backend_name

    def execute_bracket(
        self,
        execution_id: str,
        slug: str,
        up_token_id: str,
        down_token_id: str,
        target_shares: float,
        up_price_limit: float,
        down_price_limit: float,
        estimated_total_usdc: float,
    ) -> bool:
        # Hard kill-switch for live trading. (Dry-run may proceed.)
        if not self.cfg.dry_run and not self.cfg.trading_enabled:
            return False

        if self.cfg.max_estimated_usdc_per_bracket and float(estimated_total_usdc) > float(self.cfg.max_estimated_usdc_per_bracket):
            return False

        if self.cfg.daily_estimated_usdc_cap:
            spent = self.store.sum_estimated_usdc_for_day(_utc_today_yyyy_mm_dd())
            if (spent + float(estimated_total_usdc)) > float(self.cfg.daily_estimated_usdc_cap):
                return False

        # Global pile-up brake
        if self.store.count_open() >= self.cfg.max_open_brackets:
            return False

        rec = self.store.get(execution_id)
        if rec is None:
            rec = BTC15ExecutionRecord(
                execution_id=execution_id,
                slug=slug,
                up_token_id=up_token_id,
                down_token_id=down_token_id,
                target_shares=float(target_shares),
            )
            rec.estimated_total_usdc = float(estimated_total_usdc)
            rec.execution_backend = self.backend_name
            self.store.upsert(rec)

        if rec.state in (BTC15ExecutionState.DONE, BTC15ExecutionState.ABORTED):
            return rec.state == BTC15ExecutionState.DONE

        start_unhedged = time.time()

        # ---- LEG A ----
        if rec.state == BTC15ExecutionState.PLANNED:
            rec.state = BTC15ExecutionState.LEG_A_PLACED
            self.store.upsert(rec)

            id_kind_a, ext_a, raw_place_a = self.leg_executor.place_limit(
                slug=slug,
                leg_name="A",
                token_id=up_token_id,
                side="BUY",
                target_shares=float(target_shares),
                price_limit=float(up_price_limit),
                estimated_usdc=float(estimated_total_usdc / 2.0),
                timeout_seconds=int(self.cfg.leg_a_timeout_seconds),
                dry_run=bool(self.cfg.dry_run),
            )
            # Persist external ID immediately (restart safety)
            if id_kind_a == "job":
                rec.leg_a_job_id = ext_a
            elif id_kind_a == "order":
                rec.leg_a_order_id = ext_a
            rec.leg_a_raw_json = json.dumps({"place": raw_place_a}, ensure_ascii=True)
            self.store.upsert(rec)

            filled_a, raw_confirm_a = self.leg_executor.confirm_filled(
                id_kind=id_kind_a,
                external_id=ext_a,
                target_shares=float(target_shares),
                timeout_seconds=int(self.cfg.leg_a_timeout_seconds),
                dry_run=bool(self.cfg.dry_run),
                raw_place=raw_place_a,
            )
            rec.leg_a_raw_json = json.dumps({"place": raw_place_a, "confirm": raw_confirm_a}, ensure_ascii=True)
            self.store.upsert(rec)

            if not filled_a:
                rec.state = BTC15ExecutionState.ABORTED
                self.store.upsert(rec)
                return False

            rec.state = BTC15ExecutionState.LEG_A_FILLED
            self.store.upsert(rec)

        if rec.state == BTC15ExecutionState.LEG_A_PLACED:
            # Resume path: try to confirm fill without placing again.
            id_kind_a = "order" if rec.leg_a_order_id else "job"
            ext_a = rec.leg_a_order_id or rec.leg_a_job_id or ""
            raw_place_a: Dict[str, Any] = {}
            try:
                raw_place_a = (json.loads(rec.leg_a_raw_json or "{}").get("place") or {})
            except Exception:
                raw_place_a = {}

            filled_a, raw_confirm_a = self.leg_executor.confirm_filled(
                id_kind=id_kind_a,
                external_id=ext_a,
                target_shares=float(target_shares),
                timeout_seconds=int(self.cfg.leg_a_timeout_seconds),
                dry_run=bool(self.cfg.dry_run),
                raw_place=raw_place_a,
            )
            rec.leg_a_raw_json = json.dumps({"place": raw_place_a, "confirm": raw_confirm_a}, ensure_ascii=True)
            if not filled_a:
                rec.state = BTC15ExecutionState.ABORTED
                self.store.upsert(rec)
                return False
            rec.state = BTC15ExecutionState.LEG_A_FILLED
            self.store.upsert(rec)

        # ---- LEG B ----
        if rec.state == BTC15ExecutionState.LEG_A_FILLED:
            if (time.time() - start_unhedged) > self.cfg.max_unhedged_seconds:
                rec.state = BTC15ExecutionState.ABORTED
                self.store.upsert(rec)
                return False

            rec.state = BTC15ExecutionState.LEG_B_PLACED
            self.store.upsert(rec)

            id_kind_b, ext_b, raw_place_b = self.leg_executor.place_limit(
                slug=slug,
                leg_name="B",
                token_id=down_token_id,
                side="BUY",
                target_shares=float(target_shares),
                price_limit=float(down_price_limit),
                estimated_usdc=float(estimated_total_usdc / 2.0),
                timeout_seconds=int(self.cfg.leg_b_timeout_seconds),
                dry_run=bool(self.cfg.dry_run),
            )
            if id_kind_b == "job":
                rec.leg_b_job_id = ext_b
            elif id_kind_b == "order":
                rec.leg_b_order_id = ext_b
            rec.leg_b_raw_json = json.dumps({"place": raw_place_b}, ensure_ascii=True)
            self.store.upsert(rec)

            filled_b, raw_confirm_b = self.leg_executor.confirm_filled(
                id_kind=id_kind_b,
                external_id=ext_b,
                target_shares=float(target_shares),
                timeout_seconds=int(self.cfg.leg_b_timeout_seconds),
                dry_run=bool(self.cfg.dry_run),
                raw_place=raw_place_b,
            )
            rec.leg_b_raw_json = json.dumps({"place": raw_place_b, "confirm": raw_confirm_b}, ensure_ascii=True)
            self.store.upsert(rec)

            if not filled_b:
                # Conservative: if hedge leg doesn't fill, abort (and rely on manual/exit manager).
                rec.state = BTC15ExecutionState.ABORTED
                self.store.upsert(rec)
                return False

            rec.state = BTC15ExecutionState.HEDGED_FILLED
            self.store.upsert(rec)

        if rec.state == BTC15ExecutionState.LEG_B_PLACED:
            id_kind_b = "order" if rec.leg_b_order_id else "job"
            ext_b = rec.leg_b_order_id or rec.leg_b_job_id or ""
            raw_place_b: Dict[str, Any] = {}
            try:
                raw_place_b = (json.loads(rec.leg_b_raw_json or "{}").get("place") or {})
            except Exception:
                raw_place_b = {}

            filled_b, raw_confirm_b = self.leg_executor.confirm_filled(
                id_kind=id_kind_b,
                external_id=ext_b,
                target_shares=float(target_shares),
                timeout_seconds=int(self.cfg.leg_b_timeout_seconds),
                dry_run=bool(self.cfg.dry_run),
                raw_place=raw_place_b,
            )
            rec.leg_b_raw_json = json.dumps({"place": raw_place_b, "confirm": raw_confirm_b}, ensure_ascii=True)
            if not filled_b:
                rec.state = BTC15ExecutionState.ABORTED
                self.store.upsert(rec)
                return False
            rec.state = BTC15ExecutionState.HEDGED_FILLED
            self.store.upsert(rec)

        if rec.state == BTC15ExecutionState.HEDGED_FILLED:
            rec.state = BTC15ExecutionState.DONE
            self.store.upsert(rec)
            return True

        return False


def _looks_like_filled(result: Dict[str, Any]) -> bool:
    """Heuristic: treat the Bankr sidecar response as successful only if it looks filled.

    We instruct Bankr to only report success when filled, but this extra check helps
    avoid false-positives if the summary clearly indicates failure.
    """
    if not result:
        return False

    status = str(result.get("status") or "").lower()
    if status and status != "ok":
        return False

    # Some responses use {success: bool}
    if result.get("success") is False:
        return False

    summary = str(result.get("summary") or "").lower()
    if any(k in summary for k in ("not_filled", "not filled", "cancel", "failed")):
        return False

    return True


_executor_singleton: Optional[BTC15TwoPhaseExecutor] = None


def get_btc15_two_phase_executor(sidecar_url: str, dry_run: bool) -> BTC15TwoPhaseExecutor:
    global _executor_singleton
    if _executor_singleton is None:
        store = BTC15OrdersStore()
        cfg = ExecutionConfig(
            leg_a_timeout_seconds=int(os.getenv("BTC15_LEG_A_TIMEOUT_SECONDS", "12")),
            leg_b_timeout_seconds=int(os.getenv("BTC15_LEG_B_TIMEOUT_SECONDS", "18")),
            max_unhedged_seconds=int(os.getenv("BTC15_MAX_UNHEDGED_SECONDS", "25")),
            max_open_brackets=int(os.getenv("BTC15_MAX_OPEN_BRACKETS", "2")),
            max_estimated_usdc_per_bracket=float(os.getenv("BTC15_MAX_ESTIMATED_USDC_PER_BRACKET", "0") or 0.0),
            daily_estimated_usdc_cap=float(os.getenv("BTC15_DAILY_ESTIMATED_USDC_CAP", "0") or 0.0),
            trading_enabled=_env_bool("TRADING_ENABLED", False),
            dry_run=bool(dry_run),
        )

        backend = (os.getenv("BTC15_EXECUTION_BACKEND") or "").strip().lower()
        if not backend:
            backend = "clob" if _env_bool("CLOB_EXECUTION_ENABLED", False) else "bankr"

        allow_bankr_fallback = _env_bool("BTC15_ALLOW_BANKR_FALLBACK", True)

        leg_executor: LegExecutor
        if backend == "clob":
            try:
                leg_executor = CLOBLegExecutor()
            except Exception:
                if not allow_bankr_fallback:
                    raise
                transport = BankrTransport(sidecar_url=sidecar_url)
                leg_executor = BankrLegExecutor(transport)
        else:
            transport = BankrTransport(sidecar_url=sidecar_url)
            leg_executor = BankrLegExecutor(transport)

        _executor_singleton = BTC15TwoPhaseExecutor(store=store, leg_executor=leg_executor, cfg=cfg, backend_name=backend)
    return _executor_singleton
