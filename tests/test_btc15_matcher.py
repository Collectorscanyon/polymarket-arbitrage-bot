from config import BTC15Config
from bot.strategies.btc15_loop import BTC15Loop, is_candidate_btc15_market


def _cfg(**overrides):
    base = BTC15Config(
        enabled=True,
        market_substr="btc-up-or-down-15m",
        min_volume_usdc=100.0,
        cheap_side_trigger_max=0.35,
        target_avg_max=0.30,
        max_bracket_usdc=40.0,
        min_total_edge_cents=1.0,
        max_time_to_hedge_sec=600,
        min_orderbook_liq_usdc=50.0,
        max_open_brackets=3,
        cooldown_sec=300,
        daily_max_loss=50.0,
        max_losses_before_pause=3,
        force_test_slug="",
    )
    return base._replace(**overrides)  # NamedTuple supports _replace


def test_candidate_matches_btc_updown_slug():
    m = {"slug": "btc-updown-15m-1765495800", "question": "Bitcoin Up or Down"}
    assert is_candidate_btc15_market(m) is True


def test_is_btc15_market_allows_zero_volume_for_real_15m():
    loop = BTC15Loop(_cfg())
    m = {"slug": "btc-updown-15m-1765495800", "question": "Bitcoin Up or Down", "closed": False}
    assert loop._is_btc15_market(m, volume_usdc=0.0) is True


def test_is_btc15_market_rejects_closed_markets():
    loop = BTC15Loop(_cfg())
    m = {"slug": "btc-updown-15m-1765495800", "question": "Bitcoin Up or Down", "closed": True}
    assert loop._is_btc15_market(m, volume_usdc=9999.0) is False


def test_force_test_slug_is_exclusive():
    loop = BTC15Loop(_cfg(force_test_slug="abc"))
    assert loop._is_btc15_market({"slug": "abc", "closed": False}, volume_usdc=0.0) is True
    assert loop._is_btc15_market({"slug": "def", "closed": False}, volume_usdc=0.0) is False
