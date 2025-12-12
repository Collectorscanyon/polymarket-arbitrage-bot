import pytest
from utils.arbitrage_probability_calculator import ProbabilityCalculator

def test_calculate_probability():
    # Arbitrage opportunities

    test1 = ProbabilityCalculator([2.10, 2.10])
    assert test1.calculate_probability() == pytest.approx(95.24, rel=0.01)

    test2 = ProbabilityCalculator([1.95, 2.10])
    assert test2.calculate_probability() == pytest.approx(98.90, rel=0.01)

    test3 = ProbabilityCalculator([2.05, 2.05])
    assert test3.calculate_probability() == pytest.approx(97.56, rel=0.01)

    # Non-arbitrage cases

    test4 = ProbabilityCalculator([1.80, 2.10])
    assert test4.calculate_probability() == pytest.approx(103.18, rel=0.01)

    test5 = ProbabilityCalculator([1.70, 2.40])
    assert test5.calculate_probability() == pytest.approx(100.49, rel=0.01)
