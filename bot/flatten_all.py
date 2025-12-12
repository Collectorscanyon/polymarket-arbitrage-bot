"""
Flatten All - Close all open positions via Bankr.

This script reads open trades from the SQLite ledger and sends close commands
for each position through the executor.

Usage:
    python -m bot.flatten_all
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from executor import close_position, BankrWalletEmptyError, BankrCapExceededError
from config import BANKR_DRY_RUN

DB_PATH = Path(__file__).resolve().parent.parent / "sidecar" / "trades.db"


def get_open_positions() -> list[dict]:
    """Fetch all open positions from the ledger."""
    if not DB_PATH.exists():
        print(f"[FlattenAll] Database not found: {DB_PATH}")
        return []

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            id,
            command_id,
            market_label,
            market_slug,
            side,
            size_usdc,
            avg_price
        FROM trades
        WHERE status = 'OPEN'
        """
    )
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows


def mark_closed(trade_ids: list[int], realized_pnl: float = 0.0) -> None:
    """Mark trades as closed in the ledger."""
    if not trade_ids:
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    # Update each trade individually to set realized_pnl
    for trade_id in trade_ids:
        cur.execute(
            """
            UPDATE trades
            SET status = 'CLOSED',
                realized_pnl = ?
            WHERE id = ?
            """,
            (realized_pnl, trade_id),
        )
    
    conn.commit()
    conn.close()
    print(f"[FlattenAll] Marked {len(trade_ids)} trades as CLOSED in ledger.")


def main():
    """Main flatten all routine."""
    print("[FlattenAll] Starting flatten all positions...")
    print(f"[FlattenAll] DRY_RUN mode: {BANKR_DRY_RUN}")

    positions = get_open_positions()
    if not positions:
        print("[FlattenAll] No open positions to close.")
        return

    print(f"[FlattenAll] Found {len(positions)} open positions.")
    closed_ids = []
    failed_ids = []

    for pos in positions:
        trade_id = pos["id"]
        command_id = pos["command_id"]
        market_label = pos["market_label"]
        market_slug = pos["market_slug"]
        side = pos["side"]
        size_usdc = float(pos["size_usdc"] or 0)
        entry_price = float(pos["avg_price"] or 0)

        print(f"\n[FlattenAll] Closing trade #{trade_id}:")
        print(f"  Market: {market_label}")
        print(f"  Side: {side}")
        print(f"  Size: ${size_usdc:.2f}")
        print(f"  Entry: {entry_price:.4f}")

        try:
            result = close_position(
                market_label=market_label,
                market_slug=market_slug,
                side=side,
                size_usdc=size_usdc,
                max_slippage_bps=50,  # 0.5% slippage tolerance
            )
            
            if result:
                print(f"  Result: {result.get('status', 'sent')}")
                closed_ids.append(trade_id)
            else:
                print(f"  Result: Failed (no response)")
                failed_ids.append(trade_id)

        except BankrWalletEmptyError as e:
            print(f"  ERROR: Wallet empty - {e}")
            print("[FlattenAll] Stopping due to empty wallet.")
            break

        except BankrCapExceededError as e:
            print(f"  ERROR: Cap exceeded - {e}")
            failed_ids.append(trade_id)

        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            failed_ids.append(trade_id)

    # Mark successfully closed trades
    if closed_ids:
        mark_closed(closed_ids)

    # Summary
    print(f"\n[FlattenAll] === Summary ===")
    print(f"  Total positions: {len(positions)}")
    print(f"  Closed: {len(closed_ids)}")
    print(f"  Failed: {len(failed_ids)}")
    print("[FlattenAll] Done.")


if __name__ == "__main__":
    main()
