"""
marketplace.py
--------------
Fetches BGG GeekMarket listings for a game via the GeekDo JSON API.

CURRENT LISTINGS (for sale):
  GET https://api.geekdo.com/api/market/products
  Params: objectid, objecttype=thing, country=US, pageid=1, nosession=1
  No status filter needed — the API returns only active for-sale listings
  by default. The previous code checked item.get('productstate', 'active')
  but 'productstate' is not a real field in the response, so the default
  'active' was returned for every item and the filter never fired (causing
  sold/expired listings to pass through). Fixed by removing that check.

SOLD LISTINGS:
  Same endpoint with status=sold added.
  Sold listings are publicly visible on BGG without login, so the API
  should expose them the same way. Full debug logging on first run will
  confirm the exact response schema.
"""

import re
from typing import List, Dict
from datetime import datetime

import requests


GEEKDO_BASE = "https://api.geekdo.com"

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

    Uses the GeekDo JSON API. The API returns only active for-sale listings
    by default — no client-side state filtering needed or applied.

    Each listing dict:
      price       - asking price e.g. "$18.00"
      condition   - e.g. "Like New", "Very Good", "Good"
      location    - e.g. "United States"
      date_listed - e.g. "Mar 5, 2026"
      notes       - seller notes (not available without session — shown as "—")
      seller      - seller username (not available without session — shown as "—")
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

        # Debug: log field names on first call so we can verify the schema
        if products:
            print(f"    DEBUG forsale keys: {list(products[0].keys())}")

        listings = []
        for item in products:
            try:
                price_val  = float(item.get('price', 0) or 0)
                if price_val <= 0:
                    continue

                symbol     = item.get('currencysymbol', '$')
                price_str  = f"{symbol}{price_val:.2f}"
                condition  = item.get('prettycondition') or item.get('condition') or 'Unknown'
                location   = item.get('itemlocation', 'Unknown')
                date_listed = _format_date(item.get('listdate', ''))

                listings.append({
                    'price':       price_str,
                    'condition':   condition,
                    'location':    location,
                    'date_listed': date_listed,
                    'notes':       '—',
                    'seller':      '—',
                    '_price_raw':  price_val,
                })
            except Exception:
                continue

        listings.sort(key=lambda x: x['_price_raw'])
        for lst in listings:
            del lst['_price_raw']

        print(f"    Marketplace (forsale): {len(listings)} valid listing(s) after parsing")
        return listings[:num_listings]

    except requests.exceptions.RequestException as e:
        print(f"    ❌ Marketplace (forsale) request error: {e}")
        return []
    except Exception as e:
        print(f"    ❌ Marketplace (forsale) parse error: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# SOLD LISTINGS
# ─────────────────────────────────────────────────────────────────────────────

def get_sold_listings(bgg_id: str, num_listings: int = 5) -> List[Dict]:
    """
    Return recently sold BGG Marketplace listings for a game (US).

    Uses the GeekDo API with status=sold. Sold listings are publicly visible
    on BGG without login. Full debug logging included so we can verify the
    response schema from GitHub Actions logs on first run.

    Each listing dict:
      price     - sale price e.g. "$18.00"
      condition - e.g. "Like New"
      date_sold - e.g. "Mar 5, 2026"
      seller    - BGG username (may not be available without session)
      notes     - seller notes (may not be available without session)
    """
    url = f"{GEEKDO_BASE}/api/market/products"
    params = {
        'objectid':   bgg_id,
        'objecttype': 'thing',
        'country':    'US',
        'status':     'sold',
        'pageid':     1,
        'nosession':  1,
    }

    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=20)

        if resp.status_code != 200:
            print(f"    ⚠️  Marketplace (sold) API returned HTTP {resp.status_code}")
            return []

        data = resp.json()
        products = data.get('products', [])
        print(f"    Marketplace (sold): {len(products)} item(s) returned by API")

        # Debug: log the FULL first item so we can see every field and value
        if products:
            print(f"    DEBUG sold keys: {list(products[0].keys())}")
            print(f"    DEBUG sold first item: {products[0]}")

        listings = []
        for item in products:
            try:
                # Try multiple plausible price field names
                price_val = float(
                    item.get('price') or
                    item.get('saleprice') or
                    item.get('listprice') or
                    0
                )
                if price_val <= 0:
                    continue

                symbol    = item.get('currencysymbol', '$')
                price_str = f"{symbol}{price_val:.2f}"
                condition = item.get('prettycondition') or item.get('condition') or 'Unknown'

                # Try multiple plausible date field names for the sale date
                raw_date = (
                    item.get('saledate') or
                    item.get('solddate') or
                    item.get('saletime') or
                    item.get('lastmodified') or
                    item.get('listdate') or
                    ''
                )
                date_sold = _format_date(raw_date)

                listings.append({
                    'price':      price_str,
                    'condition':  condition,
                    'date_sold':  date_sold,
                    'seller':     item.get('bggusername') or item.get('username') or '—',
                    'notes':      (item.get('notes') or '').strip() or '—',
                    '_price_raw': price_val,
                })
            except Exception:
                continue

        listings.sort(key=lambda x: x['_price_raw'])
        for lst in listings:
            del lst['_price_raw']

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
    """Convert a raw date string from BGG into a friendly format."""
    if not date_raw:
        return 'Unknown'
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S%z',
                '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            dt = datetime.strptime(date_raw[:19], fmt[:19])
            return dt.strftime('%b %-d, %Y')    # e.g. "Jan 14, 2026"
        except ValueError:
            continue
    return date_raw
