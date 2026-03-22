"""
marketplace.py
--------------
Fetches BGG GeekMarket listings for a game.

CURRENT LISTINGS (for sale):
  GET https://api.geekdo.com/api/market/products
  Params: objectid, objecttype=thing, country=US, pageid=1, nosession=1
  Response key: 'products'
  Fields used: price, prettycondition, listdate, currencysymbol

SOLD LISTINGS (price history):
  GET https://boardgamegeek.com/api/market/products/pricehistory
  Params: ajax=1, condition=any, currency=USD, objectid, objecttype=thing, pageid=1, nosession=1
  Response key: 'items'
  Fields used: price, condition, saledate, currencysymbol
  Returns most-recently-sold first (API default order preserved).
"""

from typing import List, Dict
from datetime import datetime

import requests


GEEKDO_BASE = "https://api.geekdo.com"
BGG_BASE    = "https://boardgamegeek.com"

HEADERS = {
    'User-Agent': 'BGGDealMonitor/1.0 (personal use)',
    'Accept':     'application/json',
    'Referer':    'https://boardgamegeek.com/',
}


# ─────────────────────────────────────────────────────────────────────────────
# CURRENT (FOR SALE) LISTINGS
# ─────────────────────────────────────────────────────────────────────────────

def get_current_listings(bgg_id: str, num_listings: int = 5) -> List[Dict]:
    """
    Return current (for-sale) BGG Marketplace listings for a game,
    filtered to US sellers, sorted cheapest first.

    Each listing dict:
      price       - asking price e.g. "$45.00"
      condition   - e.g. "Like New", "Very Good"
      date_listed - e.g. "Mar 22, 2026"
    """
    url = f"{GEEKDO_BASE}/api/market/products"
    params = {
        'objectid':   bgg_id,
        'objecttype': 'thing',
        'country':    'US',
        'pageid':     1,
        'nosession':  1,
    }

    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=20)

        if resp.status_code != 200:
            print(f"    ⚠️  Marketplace (forsale) API returned HTTP {resp.status_code}")
            return []

        data = resp.json()
        products = data.get('products', [])
        print(f"    Marketplace (forsale): {len(products)} item(s) returned by API")

        # API returns items sorted by most-recently-listed first.
        # Cap at 20 recent listings to exclude months-old stale listings
        # (e.g. $9.74 listed Sep 2025 still unsold) before sorting cheapest-first.
        recent_products = products[:20]

        listings = []
        for item in recent_products:
            try:
                price_val = float(item.get('price') or 0)
                if price_val <= 0:
                    continue

                symbol    = item.get('currencysymbol', '$')
                listings.append({
                    'price':       f"{symbol}{price_val:.2f}",
                    'condition':   item.get('prettycondition') or item.get('condition') or 'Unknown',
                    'date_listed': _format_date(item.get('listdate', '')),
                    '_price_raw':  price_val,
                })
            except Exception:
                continue

        listings.sort(key=lambda x: x['_price_raw'])
        for lst in listings:
            del lst['_price_raw']

        print(f"    Marketplace (forsale): {len(listings)} valid listing(s) after parsing (from {len(recent_products)} most recent)")
        return listings[:num_listings]

    except requests.exceptions.RequestException as e:
        print(f"    ❌ Marketplace (forsale) request error: {e}")
        return []
    except Exception as e:
        print(f"    ❌ Marketplace (forsale) parse error: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# SOLD LISTINGS (price history)
# ─────────────────────────────────────────────────────────────────────────────

def get_sold_listings(bgg_id: str, num_listings: int = 5) -> List[Dict]:
    """
    Return recently sold BGG Marketplace listings for a game (USD only).

    Uses BGG's internal price history endpoint — publicly accessible without
    a session. Returns most-recently-sold items first (API default).

    Each listing dict:
      price     - sale price e.g. "$20.00"
      condition - e.g. "Very Good"
      date_sold - e.g. "Mar 18, 2026"
    """
    url = f"{BGG_BASE}/api/market/products/pricehistory"
    params = {
        'ajax':       1,
        'condition':  'any',
        'currency':   'USD',
        'objectid':   bgg_id,
        'objecttype': 'thing',
        'pageid':     1,
        'nosession':  1,
    }

    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=20)

        if resp.status_code != 200:
            print(f"    ⚠️  Marketplace (pricehistory) API returned HTTP {resp.status_code}")
            return []

        data = resp.json()
        items = data.get('items', [])
        print(f"    Marketplace (sold): {len(items)} item(s) returned by API")

        listings = []
        for item in items:
            try:
                price_val = float(item.get('price') or 0)
                if price_val <= 0:
                    continue

                symbol = item.get('currencysymbol', '$')
                listings.append({
                    'price':     f"{symbol}{price_val:.2f}",
                    'condition': item.get('condition') or 'Unknown',
                    'date_sold': _format_date(item.get('saledate', '')),
                })
            except Exception:
                continue

        # API already returns most-recent-first — preserve that order
        print(f"    Marketplace (sold): {len(listings)} valid listing(s) after parsing")
        return listings[:num_listings]

    except requests.exceptions.RequestException as e:
        print(f"    ❌ Marketplace (sold) request error: {e}")
        return []
    except Exception as e:
        print(f"    ❌ Marketplace (sold) parse error: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _format_date(date_raw: str) -> str:
    """Convert a raw date string from BGG into a friendly format e.g. 'Mar 18, 2026'."""
    if not date_raw:
        return 'Unknown'
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S%z',
                '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            dt = datetime.strptime(date_raw[:19], fmt[:19])
            return dt.strftime('%b %-d, %Y')
        except ValueError:
            continue
    return date_raw
