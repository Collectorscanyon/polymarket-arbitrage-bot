from datetime import datetime, timezone

from bot.strategies.btc15_slug_source import bucket_for_datetime, candidate_buckets, slug_for_bucket


def test_bucket_for_datetime_is_multiple_of_900():
    dt = datetime(2025, 12, 12, 12, 7, 33, tzinfo=timezone.utc)
    b = bucket_for_datetime(dt)
    assert b % 900 == 0


def test_candidate_buckets_unique_and_ordered():
    dt = datetime(2025, 12, 12, 12, 0, 1, tzinfo=timezone.utc)
    bs = candidate_buckets(now=dt, offsets=(0, -1, 1, 2, 0))
    assert len(bs) == len(set(bs))
    assert bs[0] == bucket_for_datetime(dt)


def test_slug_for_bucket_format():
    assert slug_for_bucket(1765405800).startswith("btc-updown-15m-")
    assert slug_for_bucket(1765405800).endswith("1765405800")
