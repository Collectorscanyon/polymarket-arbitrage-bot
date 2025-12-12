import logging
import time
import requests
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from config import (
	ARB_THRESHOLD,
	BANKR_MAX_COMMANDS_PER_HOUR,
	BTC15_CONFIG,
	CHEAP_BUY_THRESHOLD,
	LOOP_SLEEP_SECONDS,
	MARKETS_TO_WATCH,
	MAX_BANKR_COMMANDS_PER_LOOP,
	MIN_EDGE_BPS,
	SCAN_INTERVAL,
)
from executor import (
	BankrCapExceededError,
	BankrWalletEmptyError,
	execute_arb,
	hedge_cheap_buy,
	reset_bankr_command_budget,
)
from utils import ArbitrageDetector
from utils import DecimalOddsSetter
from utils import MarketsDataParser
from utils import MultiMarketsDataParser
from utils import OutcomePricesChecker
from utils import ProbabilityCalculator
from utils import set_minimum_price_gap

# BTC 15-minute loop strategy
from bot.strategies.btc15_loop import BTC15Loop


logger = logging.getLogger(__name__)
logging.basicConfig(
	level=logging.DEBUG,
	filename="log.log",
	filemode="w",
	format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


# ─────────────────────────────────────────────────────────────
# BTC Updown 15m Direct Fetch (these markets don't appear in standard API)
# ─────────────────────────────────────────────────────────────
def fetch_btc_updown_15m_markets() -> List[dict]:
	"""
	Fetch btc-updown-15m markets directly by timestamp.
	These short-lived intraday markets don't appear in standard queries.
	"""
	results = []
	now = datetime.now(timezone.utc)
	
	# Check for events in the next 2 hours (every 15 min = 8 potential events)
	for minutes_ahead in range(-15, 120, 15):  # Include recent past too
		target_time = now + timedelta(minutes=minutes_ahead)
		timestamp = int(target_time.timestamp())
		# Round to nearest 15 min boundary
		timestamp = (timestamp // 900) * 900
		
		slug = f"btc-updown-15m-{timestamp}"
		try:
			resp = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=3)
			if resp.ok:
				data = resp.json()
				for event in data:
					for m in event.get("markets", []):
						if not m.get("closed"):  # Only include open markets
							m["_source"] = "btc-updown-direct"
							results.append(m)
		except Exception:
			pass
	
	return results


def fetch_active_events_latest(limit: int = 50) -> List[dict]:
	"""
	Fetch the newest open events (order by id descending).
	Returns open markets from these events - catches intraday btc-updown-15m.
	"""
	results = []
	try:
		url = f"https://gamma-api.polymarket.com/events?order=id&ascending=false&closed=false&limit={limit}"
		resp = requests.get(url, timeout=5)
		if resp.ok:
			events = resp.json()
			for ev in events:
				for m in ev.get("markets", []):
					if not m.get("closed"):
						m["_source"] = "events-latest"
						results.append(m)
			logging.info(f"[FETCH] events-latest: {len(events)} events → {len(results)} open markets")
	except Exception as e:
		logging.warning(f"[FETCH] events-latest failed: {e}")
	return results


# ─────────────────────────────────────────────────────────────
# Hourly rate limiting
# ─────────────────────────────────────────────────────────────
_bankr_commands_last_hour: List[float] = []


def _can_send_bankr_command_now() -> bool:
	"""Check if we're under the hourly command cap."""
	if BANKR_MAX_COMMANDS_PER_HOUR <= 0:
		return True
	now = time.time()
	# Prune commands older than 1 hour
	while _bankr_commands_last_hour and now - _bankr_commands_last_hour[0] > 3600:
		_bankr_commands_last_hour.pop(0)
	return len(_bankr_commands_last_hour) < BANKR_MAX_COMMANDS_PER_HOUR


def _record_bankr_command() -> None:
	"""Record that we sent a Bankr command (for hourly tracking)."""
	_bankr_commands_last_hour.append(time.time())


def _market_label(market: dict) -> str:
	"""Return a readable identifier for Bankr prompts."""
	return (
		str(market.get("question"))
		if market.get("question")
		else str(market.get("slug") or market.get("id") or "Unknown Polymarket market")
	)


def _is_watchlisted(market: dict) -> bool:
	if not MARKETS_TO_WATCH:
		return True
	identifiers = {
		str(market.get("id")),
		str(market.get("slug")),
	}
	return any(identifier in MARKETS_TO_WATCH for identifier in identifiers if identifier != "None")


def _process_market(market: dict, minimum_price_gap: float, slots_left: int) -> int:
	"""Process a single market for arb/hedge opportunities.
	
	Returns the number of Bankr commands sent.
	"""
	if slots_left <= 0:
		return 0
	if not _is_watchlisted(market):
		return 0

	outcome_prices = market.get("outcomePrices")
	if not outcome_prices:
		return 0

	checker = OutcomePricesChecker(outcome_prices)
	if not (checker.check_outcome_prices() and checker.count_outcome_prices()):
		return 0

	try:
		yes_price, no_price = float(outcome_prices[0]), float(outcome_prices[1])
	except (TypeError, ValueError, IndexError):
		logger.debug("Skipping market %s due to malformed prices", _market_label(market))
		return 0

	decimal_odds_setter = DecimalOddsSetter([yes_price, no_price])
	outcome_odds_decimals = decimal_odds_setter.convert_to_decimal()

	probability_calculator = ProbabilityCalculator(outcome_odds_decimals)
	arbitrage_probability = probability_calculator.calculate_probability()

	detector = ArbitrageDetector(arbitrage_probability, minimum_price_gap)
	detector.detect_arbitrage_opportunity()

	total_price = yes_price + no_price
	edge = 1.0 - total_price
	edge_bps = edge * 10000  # Convert to basis points
	market_label = _market_label(market)

	logger.debug(
		"Market %s: yes=%.4f no=%.4f total=%.4f edge=%.4f (%.1f bps) min_gap=%.4f threshold=%.4f",
		market_label,
		yes_price,
		no_price,
		total_price,
		edge,
		edge_bps,
		minimum_price_gap,
		ARB_THRESHOLD,
	)

	# Filter: only proceed if edge meets minimum threshold
	if edge_bps < MIN_EDGE_BPS:
		logger.debug(
			"Skipping %s: edge %.1f bps < MIN_EDGE_BPS %.1f",
			market_label,
			edge_bps,
			MIN_EDGE_BPS,
		)
		return 0

	commands_used = 0
	
	try:
		if total_price < ARB_THRESHOLD and slots_left - commands_used > 0:
			result_msg = execute_arb(yes_price, no_price, market_label)
			logger.info("Bankr execute_arb result: %s", result_msg)
			print(result_msg)
			# Only count as command if we actually sent something
			if not result_msg.startswith(("[SKIP]", "[COOLDOWN]", "[DRY]", "No arb", "Failed")):
				commands_used += 1
				_record_bankr_command()
			return commands_used

		cheap_results = []
		if yes_price < CHEAP_BUY_THRESHOLD and slots_left - commands_used > 0:
			result = hedge_cheap_buy(market_label, "YES", yes_price)
			cheap_results.append(result)
			# Only count as command if we actually sent something
			if not result.startswith(("[SKIP]", "[COOLDOWN]", "[DRY]", "No cheap", "Failed")):
				commands_used += 1
				_record_bankr_command()
		if (
			no_price < CHEAP_BUY_THRESHOLD
			and slots_left - commands_used > 0
		):
			result = hedge_cheap_buy(market_label, "NO", no_price)
			cheap_results.append(result)
			# Only count as command if we actually sent something
			if not result.startswith(("[SKIP]", "[COOLDOWN]", "[DRY]", "No cheap", "Failed")):
				commands_used += 1
				_record_bankr_command()

		for message in cheap_results:
			logger.info("Bankr hedge result: %s", message)
			print(message)
			
	except BankrCapExceededError as e:
		# Guardrail hit — log and return what we've sent so far
		# This is non-fatal: just skip further ops for this market
		logger.warning("Guardrail hit while processing %s: %s", market_label, e)
		print(f"[GUARDRAIL] {market_label}: {e}")
		# Don't re-raise - let the loop continue with next market

	return commands_used


def main() -> None:
	minimum_price_gap = set_minimum_price_gap()
	single_markets_data_parser = MarketsDataParser("https://gamma-api.polymarket.com/markets")
	events_data_parser = MultiMarketsDataParser("https://gamma-api.polymarket.com/events")

	max_commands = max(1, MAX_BANKR_COMMANDS_PER_LOOP)
	
	# Initialize BTC 15-minute loop strategy
	btc15_loop: Optional[BTC15Loop] = None
	if BTC15_CONFIG.enabled:
		btc15_loop = BTC15Loop(BTC15_CONFIG)
		print(f"[BOT] BTC15 Loop enabled: max_bracket=${BTC15_CONFIG.max_bracket_usdc}, trigger<={BTC15_CONFIG.cheap_side_trigger_max}")
	
	print(f"[BOT] Starting with caps: {max_commands}/loop, {BANKR_MAX_COMMANDS_PER_HOUR}/hour, MIN_EDGE_BPS={MIN_EDGE_BPS}")
	print(f"[BOT] Loop sleep: {LOOP_SLEEP_SECONDS}s, Scan interval: {SCAN_INTERVAL}s")
	
	while True:
		try:
			# Check hourly rate limit before scanning
			if not _can_send_bankr_command_now():
				commands_left = BANKR_MAX_COMMANDS_PER_HOUR - len(_bankr_commands_last_hour)
				print(f"[LOOP] Hourly cap reached ({BANKR_MAX_COMMANDS_PER_HOUR}/hour). Sleeping...")
				logger.info("Hourly Bankr cap reached (%d). Waiting for cooldown.", BANKR_MAX_COMMANDS_PER_HOUR)
				time.sleep(LOOP_SLEEP_SECONDS)
				continue

			reset_bankr_command_budget()
			decoded_markets = single_markets_data_parser.get_markets()
			
			# Also fetch btc-updown-15m markets directly (they don't appear in standard API)
			btc_updown_markets = fetch_btc_updown_15m_markets()
			if btc_updown_markets:
				logger.info("[BOT] Fetched %d btc-updown-15m markets", len(btc_updown_markets))
				decoded_markets = decoded_markets + btc_updown_markets
			
			# Fetch newest events (order by id desc) to catch intraday markets
			latest_events_markets = fetch_active_events_latest(limit=50)
			if latest_events_markets:
				# Dedupe by condition_id
				existing_ids = {m.get("conditionId") for m in decoded_markets}
				new_markets = [m for m in latest_events_markets if m.get("conditionId") not in existing_ids]
				if new_markets:
					logger.info("[BOT] Added %d new markets from events-latest", len(new_markets))
					decoded_markets = decoded_markets + new_markets
			
			sent_commands = 0
			
			# ─────────────────────────────────────────────────────────────
			# BTC 15-minute Loop Strategy (runs first, has priority)
			# ─────────────────────────────────────────────────────────────
			if btc15_loop and BTC15_CONFIG.enabled:
				for market in decoded_markets:
					if sent_commands >= max_commands:
						break
					
					# Extract prices and volume for BTC15 processing
					outcome_prices = market.get("outcomePrices", [])
					volume_usdc = float(market.get("volume", 0) or market.get("volumeNum", 0) or 0)
					
					try:
						used = btc15_loop.process_market(market, outcome_prices, volume_usdc)
						if used > 0:
							sent_commands += used
							_record_bankr_command()
							logger.info("[BTC15] Sent %d command(s) for %s", used, market.get("slug", "?"))
					except Exception as e:
						logger.warning("[BTC15] Error processing market: %s", e)
			
			# ─────────────────────────────────────────────────────────────
			# Standard Arb/Hedge Logic
			# ─────────────────────────────────────────────────────────────
			for market in decoded_markets:
				if sent_commands >= max_commands:
					logger.debug("Reached command cap (%d) for this scan loop", max_commands)
					break
				sent_commands += _process_market(
					market,
					minimum_price_gap,
					max_commands - sent_commands,
				)

			decoded_events_markets = events_data_parser.get_events()
			for event in decoded_events_markets:
				if sent_commands >= max_commands:
					logger.debug("Reached command cap (%d) while scanning events", max_commands)
					break
				for market in event.get("markets", []):
					if sent_commands >= max_commands:
						break
					sent_commands += _process_market(
						market,
						minimum_price_gap,
						max_commands - sent_commands,
					)
				if sent_commands >= max_commands:
					break

			# Use LOOP_SLEEP_SECONDS for faster iteration, SCAN_INTERVAL for deeper sleep
			sleep_time = LOOP_SLEEP_SECONDS if sent_commands > 0 else SCAN_INTERVAL
			time.sleep(sleep_time)
			
		except BankrWalletEmptyError:
			print("[FATAL] Bankr wallet is empty. Stopping trading loop.")
			logger.error("Bankr wallet empty. Exiting main loop.")
			break
		except BankrCapExceededError as e:
			print(f"[INFO] Bankr cap exceeded: {e}. Sleeping briefly...")
			logger.info("Bankr cap exceeded: %s", e)
			time.sleep(LOOP_SLEEP_SECONDS)
		except KeyboardInterrupt:
			print("[BOT] Interrupted by user. Exiting...")
			logger.info("Interrupted by user. Exiting main loop.")
			break
		except Exception as e:
			print(f"[ERROR] Unexpected exception in main loop: {e}")
			logger.exception("Unexpected exception in main loop: %s", e)
			time.sleep(LOOP_SLEEP_SECONDS)


if __name__ == "__main__":
	main()