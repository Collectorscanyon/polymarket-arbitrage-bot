from bot.debug_inspect_market_url import extract_slug


def test_extract_slug_from_slug():
    assert extract_slug("btc-updown-15m-1765405800") == "btc-updown-15m-1765405800"


def test_extract_slug_from_url_path():
    url = "https://polymarket.com/event/btc-updown-15m-1765405800"
    assert extract_slug(url) == "btc-updown-15m-1765405800"


def test_extract_slug_from_url_query():
    url = "https://polymarket.com/?slug=btc-updown-15m-1765405800"
    assert extract_slug(url) == "btc-updown-15m-1765405800"
