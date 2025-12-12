import logging
import os


log3 = logging.getLogger(__name__)


def set_minimum_price_gap() -> float:
    # Check for environment variable first (for automated runs)
    env_val = os.getenv("MINIMUM_PRICE_GAP")
    if env_val:
        try:
            gap = float(env_val)
            if gap > 0:
                log3.info("Using MINIMUM_PRICE_GAP from environment: %s", gap)
                print(f"Set minimum price gap number: {gap} (from env)")
                return gap
        except ValueError:
            pass
    
    # A minimum of 1.5 is recommended
    log3.info('Requested user to set a minimum price gap number')
    while True:
        try:
            minimum_price_gap = input('Set minimum price gap number: ')
            minimum_price_gap = float(minimum_price_gap)
            if minimum_price_gap <= 0:
                raise ValueError
        except ValueError:
            log3.error("The minimum price gap must be a float")
            pass
        else:
            return minimum_price_gap
    