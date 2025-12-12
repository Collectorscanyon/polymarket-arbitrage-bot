import logging
import json
import requests
import re


log2 = logging.getLogger(__name__)

class MultiMarketsDataParser:

    # Removed topic/category filters, bumped limit to 500
    querystrings = {
        "active": "true",
        "closed": "false",
        "limit": "500",
    }
    
    def __init__(self, event_gamma_api_url: str):
        self.event_gamma_api_url = event_gamma_api_url

    def get_events(self) -> list[dict[str, any]]:
        response = requests.request("GET", self.event_gamma_api_url, params=self.querystrings)
        response = response.text
        response_json = json.loads(response)

        decoded_events_markets = []

        for event in response_json:
            # get the list of multi-markets events of the recent events
            if len(event.get("markets", [])) >= 1:
                log2.debug("Found an event with at least 1 market")

                event_id = event.get("id")
                event_slug = event.get("slug")
                tags = event.get("tags", [])
                event_tid = None
                for tag in tags:
                    event_tid = tag.get("id")

                multi_markets = []

                for market in event.get("markets", []):    

                    outcome_prices = market.get("outcomePrices")
                    outcome_prices_str = str(outcome_prices)

                    # The outcomePrices must be given as a formatted string of two elements, if not pass
                    match = re.search(r'\[\"([0-9]+\.[0-9]+)\", \"([0-9]+\.[0-9]+)\"\]', outcome_prices_str)

                    if match:
                        log2.debug("Found outcomePrices")
                        outcome_prices = [float(match.group(1)), float(match.group(2))]
                        
                        # Preserve all useful fields from the market
                        multi_markets.append({
                            "id": market.get("id"),
                            "slug": market.get("slug"),
                            "question": market.get("question"),
                            "outcomePrices": outcome_prices,
                            "volume": market.get("volume"),
                            "volumeNum": market.get("volumeNum"),
                            "endDate": market.get("endDate"),
                            "endDateIso": market.get("endDateIso"),
                            "liquidity": market.get("liquidity"),
                            "outcomes": market.get("outcomes"),
                        })
                    else: 
                        log2.debug("Didn't find outcomePrices")
                        pass
                
                decoded_events_markets.append({
                    "id": event_id,
                    "tid": event_tid,
                    "slug": event_slug,
                    "markets": multi_markets,
                })
            
            else:
                log2.debug("Event with no markets")
                pass

        return decoded_events_markets
