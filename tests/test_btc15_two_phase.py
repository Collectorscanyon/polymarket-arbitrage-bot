import os
import tempfile

from bot.strategies.btc15_two_phase import (
    BTC15OrdersStore,
    BTC15TwoPhaseExecutor,
    ExecutionConfig,
)


class FakeLegExecutor:
    def __init__(self):
        self.calls = []

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
    ):
        self.calls.append(
            {
                "phase": "place",
                "slug": slug,
                "leg_name": leg_name,
                "token_id": token_id,
                "side": side,
                "target_shares": target_shares,
                "price_limit": price_limit,
                "estimated_usdc": estimated_usdc,
                "timeout_seconds": timeout_seconds,
                "dry_run": dry_run,
            }
        )

        return "job", f"job-{len(self.calls)}", {"status": "ok", "success": True, "summary": "FILLED"}

    def confirm_filled(
        self,
        *,
        id_kind: str,
        external_id: str,
        target_shares: float,
        timeout_seconds: int,
        dry_run: bool,
        raw_place: dict,
    ):
        self.calls.append(
            {
                "phase": "confirm",
                "id_kind": id_kind,
                "external_id": external_id,
                "target_shares": target_shares,
                "timeout_seconds": timeout_seconds,
                "dry_run": dry_run,
            }
        )
        return True, {"status": "ok", "summary": "FILLED"}

    def cancel(self, *, id_kind: str, external_id: str) -> None:
        return None


def test_two_phase_executor_persists_and_is_idempotent():
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "btc15_orders.sqlite3")
        store = BTC15OrdersStore(db_path=db_path)
        leg_executor = FakeLegExecutor()
        cfg = ExecutionConfig(dry_run=True, max_open_brackets=10)
        ex = BTC15TwoPhaseExecutor(store=store, leg_executor=leg_executor, cfg=cfg)

        ok1 = ex.execute_bracket(
            execution_id="exec-1",
            slug="btc-updown-15m-1765405800",
            up_token_id="u",
            down_token_id="d",
            target_shares=10.0,
            up_price_limit=0.51,
            down_price_limit=0.51,
            estimated_total_usdc=10.0,
        )
        assert ok1 is True
        assert len(leg_executor.calls) == 4

        # Second call should be idempotent (already DONE)
        ok2 = ex.execute_bracket(
            execution_id="exec-1",
            slug="btc-updown-15m-1765405800",
            up_token_id="u",
            down_token_id="d",
            target_shares=10.0,
            up_price_limit=0.51,
            down_price_limit=0.51,
            estimated_total_usdc=10.0,
        )
        assert ok2 is True
        assert len(leg_executor.calls) == 4

        rec = store.get("exec-1")
        assert rec is not None
        assert rec.state == "DONE"
