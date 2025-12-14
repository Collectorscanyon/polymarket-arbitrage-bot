from __future__ import annotations

from bot.strategies.btc15_wss import BTC15WSBookCache


def test_wss_cache_drains_dirty_on_book() -> None:
    cache = BTC15WSBookCache()

    msg = {
        "event_type": "book",
        "asset_id": "tok1",
        "bids": [{"price": "0.40", "size": "10"}],
        "asks": [{"price": "0.60", "size": "12"}],
        "timestamp": 1234567890,
    }

    cache.apply_market_event(msg)

    dirty = cache.drain_dirty_token_ids()
    assert dirty == {"tok1"}

    # Second drain should be empty.
    assert cache.drain_dirty_token_ids() == set()


def test_wss_cache_drains_dirty_on_price_change_multi() -> None:
    cache = BTC15WSBookCache()

    msg = {
        "event_type": "price_change",
        "timestamp": 1234567890,
        "price_changes": [
            {"asset_id": "tokA", "price": "0.41", "size": "5", "side": "BUY"},
            {"asset_id": "tokB", "price": "0.59", "size": "7", "side": "SELL"},
        ],
    }

    cache.apply_market_event(msg)

    dirty = cache.drain_dirty_token_ids()
    assert dirty == {"tokA", "tokB"}

    # books should exist for both after price_change
    assert cache.get_orderbook("tokA") is not None
    assert cache.get_orderbook("tokB") is not None
