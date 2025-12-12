import pytest
import os
from unittest.mock import patch
from utils.set_minimum_price_gap import set_minimum_price_gap

def test_set_minimum_price_gap():
    # Clear env var so it uses input
    with patch.dict(os.environ, {"MINIMUM_PRICE_GAP": ""}, clear=False):
        with patch('builtins.input', return_value='1.8'):
            assert set_minimum_price_gap() == 1.8


def test_set_minimum_price_gap_from_env():
    with patch.dict(os.environ, {"MINIMUM_PRICE_GAP": "2.5"}):
        assert set_minimum_price_gap() == 2.5





