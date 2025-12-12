from utils.outcome_prices_checker import OutcomePricesChecker
import pytest

def test_count_outcome_prices():
    assert OutcomePricesChecker([]).count_outcome_prices() == False
    assert OutcomePricesChecker([0.1234, 0.1234878]).count_outcome_prices() == True

def test_check_outcome_prices():
    with pytest.raises(ValueError):
        # Create checker with invalid data and call check
        checker = OutcomePricesChecker(["Hello", 0.12])
        if not checker.check_outcome_prices():
            raise ValueError("Invalid outcome prices")

    assert OutcomePricesChecker([0.1234, 0.2341]).check_outcome_prices() == True

