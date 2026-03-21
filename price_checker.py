"""
price_checker.py
----------------
Gets current US retail prices for a board game via Board Game Oracle's API.

Flow (two steps):
  1. GET boardgameoracle.com/boardgame/search?q={name}
     Parse HTML to extract the BGO internal key (e.g. "rfU7EacnYy")

  2. GET boardgameoracle.com/api/trpc/price.list?input={"key":"...","region":"us"}
     Returns clean JSON with all retailer prices — no HTML parsing needed.

This covers ~10 US retailers: Game Nerdz, Tabletop Merchant, Amazon,
Gamers Guild AZ, Miniature Market, Cape Fear Games, Noble Knight Games,
Game Kastle, Pandemonium, and more.
"""

import re
import json
import time
from typing import List, Dict, Optional

import requests
from bs4 import BeautifulSoup


BGO_BASE   = "https://www.boardgameoracle.com"
BGO_SEARCH = f"{BGO_BASE}/boardgame/search"
BGO_API    = f"{BGO_BASE}/api/trpc/price.list"

HEADERS = {
    'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Referer':         BGO_BASE,
}

API_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept':     'application/json',
    'Referer':    BGO_BASE,
}


def get_all_prices(game_name: str, bgg_id: str) -> List[Dict]:
    """
    Fetch current US retail prices for a game from Board Game Oracle.
    Returns list of {store, price_usd, price_str, url, in_stock}, cheapest first.
    """

    # Step 1: find the BGO key for this game
    bgo_key, game_url = _find_bgo_key(game_name)
    if not bgo_key:
        print(f"    Could not find '{game_name}' on Board Game Oracle.")
        return []

    time.sleep(0.4)

    # Step 2: call the price API
    prices = _fetch_prices(bgo_key, game_url)

    # Sort: in-stock cheapest first, out-of-stock at end
    prices.sort(key=lambda x: (not x.get('in_stock', True), x.get('price_usd', 9999)))
    return prices


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — find BGO key via search page HTML
# ─────────────────────────────────────────────────────────────────────────────

def _find_bgo_key(game_name: str) -> tuple:
    """
    Search BGO and return (bgo_key, game_page_url) for the best match.
    BGO game page URLs look like: /boardgame/price/rfU7EacnYy/prehistories
    The key is the alphanumeric segment.
    """
    try:
        resp = requests.get(
            BGO_SEARCH,
            params={'q': game_name},
            headers=HEADERS,
            timeout=20,
        )
        if resp.status_code != 200:
            print(f"    BGO search returned HTTP {resp.status_code}")
            return None, None

        soup = BeautifulSoup(resp.text, 'lxml')

        # Find first link matching /boardgame/price/{key}/{slug}
        for a in soup.find_all('a', href=True):
            match = re.match(r'/boardgame/price/([^/]+)/(.+)', a['href'])
            if match:
                bgo_key  = match.group(1)
                game_url = BGO_BASE + a['href']
                print(f"    BGO key: {bgo_key}  ({game_url})")
                return bgo_key, game_url

        print(f"    No BGO results found for '{game_name}'")
        return None, None

    except Exception as e:
        print(f"    BGO search error: {e}")
        return None, None


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — fetch prices via tRPC JSON API
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_prices(bgo_key: str, game_url: str) -> List[Dict]:
    """
    Call BGO's internal price.list tRPC endpoint.
    Returns clean list of retailer price dicts.
    """
    input_param = json.dumps({"key": bgo_key, "region": "us"})

    try:
        resp = requests.get(
            BGO_API,
            params={'input': input_param},
            headers=API_HEADERS,
            timeout=20,
        )
        if resp.status_code != 200:
            print(f"    BGO price API returned HTTP {resp.status_code}")
            return []

        data = resp.json()
        items = (
            data.get('result', {}).get('data', {}).get('items') or
            data.get('pages', [{}])[0].get('items', [])
        )

        if not items:
            print(f"    BGO price API returned no items")
            return []

        prices = []
        for item in items:
            try:
                store     = item['merchant']['name']
                price_usd = float(item['price'])
                in_stock  = item.get('availability', '') == 'in_stock'
                # Build a direct store URL if possible, otherwise link to BGO page
                store_url = game_url

                prices.append({
                    'store':     store,
                    'price_usd': price_usd,
                    'price_str': f"${price_usd:.2f}",
                    'url':       store_url,
                    'in_stock':  in_stock,
                })
            except (KeyError, ValueError):
                continue

        print(f"    Board Game Oracle: {len(prices)} prices found")
        return prices

    except Exception as e:
        print(f"    BGO price API error: {e}")
        return []
