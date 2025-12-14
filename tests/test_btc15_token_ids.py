import pytest

from bot.strategies.btc15_cache import normalize_token_ids


def test_normalize_token_ids_none_empty():
    assert normalize_token_ids(None) == []
    assert normalize_token_ids("") == []
    assert normalize_token_ids("   ") == []


def test_normalize_token_ids_json_string_list():
    assert normalize_token_ids('["123","456"]') == ["123", "456"]


def test_normalize_token_ids_json_null_and_empty_list():
    assert normalize_token_ids("null") == []
    assert normalize_token_ids("[]") == []


def test_normalize_token_ids_json_string_scalar():
    assert normalize_token_ids('"123"') == ["123"]


def test_normalize_token_ids_plain_string():
    assert normalize_token_ids("123") == ["123"]


def test_normalize_token_ids_list_mixed():
    assert normalize_token_ids([123, "456", None, "  789  "]) == ["123", "456", "789"]
