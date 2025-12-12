from pathlib import Path


def get_api() -> str:
    """Return the contents of the local Gamma API notes file."""
    path = Path(__file__).resolve().parent / "markets_gamma_api.txt"
    return path.read_text(encoding="utf-8")