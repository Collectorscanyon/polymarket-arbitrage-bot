from __future__ import annotations

import os
import sys
from typing import Optional

from eth_account import Account


def _normalize_pk(pk: str) -> str:
    pk = pk.strip()
    if not pk:
        raise ValueError("Empty private key")
    if pk.startswith("0x"):
        pk = pk[2:]
    if len(pk) != 64:
        raise ValueError("Private key must be 32 bytes (64 hex chars), optionally prefixed by 0x")
    return "0x" + pk


def derive_address_from_env(env_var: str = "POLYMARKET_PRIVATE_KEY") -> str:
    pk = os.getenv(env_var)
    if not pk:
        # Convenience: if the user put the key in repo-root .env, import config
        # (which loads .env into os.environ) and try again.
        try:
            import config  # noqa: F401
        except Exception:
            pass
        pk = os.getenv(env_var)

    if not pk:
        raise RuntimeError(
            f"Missing required env var: {env_var}. "
            "Set it in the session or add it to the repo-root .env."
        )
    normalized = _normalize_pk(pk)
    acct = Account.from_key(normalized)
    return acct.address


def main(argv: Optional[list[str]] = None) -> int:
    argv = argv or sys.argv[1:]
    expected = None
    env_var = "POLYMARKET_PRIVATE_KEY"

    for arg in argv:
        if arg.startswith("--expected="):
            expected = arg.split("=", 1)[1].strip()
        elif arg.startswith("--env="):
            env_var = arg.split("=", 1)[1].strip()

    addr = derive_address_from_env(env_var=env_var)
    print(addr)

    if expected:
        # Compare case-insensitively; checksum casing may differ.
        if addr.lower() != expected.lower():
            print(f"ERROR: derived address does not match expected {expected}", file=sys.stderr)
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
