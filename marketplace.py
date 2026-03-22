"""
marketplace.py
--------------
Fetches BGG GeekMarket listings for a game.

CURRENT LISTINGS (for sale):
  Uses the public GeekDo API endpoint:
    GET https://api.geekdo.com/api/market/products
    Params: objectid, objecttype=thing, country=US, pageid=1, nosession=1
  No authentication needed. Returns JSON with a 'products' array.
  Results are sorted cheapest-first client-side.

SOLD LISTINGS:
  BGG does not expose sold listing history through any public or token-auth API.
  The sold data requires a live BGG session cookie (interactive login).
  Since GitHub Actions has no interactive session, sold listings return empty.
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


def get_current_listings(bgg_id: str, num_listings: int = 5) -> List[Dict]:
    """
    Return current (for-sale) BGG Marketplace listings for a game,
    filtered to US sellers only, sorted cheapest first.

    Each listing dict:
      price       - asking price e.g. "$18.00"
      condition   - e.g. "Like New", "Very Good", "Good"
      location    - e.g. "United States"
      date_listed - e.g. "Mar 5, 2026"
      notes       - seller notes (not available anonymously — shown as "—")
      seller      - seller username (not available anonymously — shown as "—")
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

        listings = []
        for item in products:
            try:
                # Only include active/for-sale listings
                if item.get('productstate', 'active') != 'active':
                    continue

                # Price — format as "$XX.XX"
                price_val  = float(item.get('price', 0))
                symbol     = item.get('currencysymbol', '$')
                price_str  = f"{symbol}{price_val:.2f}"

                condition   = item.get('prettycondition', item.get('condition', 'Unknown'))
                location    = item.get('itemlocation', 'Unknown')
                date_listed = _format_date(item.get('listdate', ''))

                listings.append({
                    'price':       price_str,
                    'condition':   condition,
                    'location':    location,
                    'date_listed': date_listed,
                    'notes':       '—',    # not available without session
                    'seller':      '—',    # not available without session
                    # raw float for sorting
                    '_price_raw':  price_val,
                })
            except Exception:
                continue

        # Sort cheapest first, then trim
        listings.sort(key=lambda x: x['_price_raw'])
        for l in listings:
            del l['_price_raw']

        return listings[:num_listings]

    except requests.exceptions.RequestException as e:
        print(f"    ❌ Marketplace (forsale) request error: {e}")
        return []
    except Exception as e:
        print(f"    ❌ Marketplace (forsale) parse error: {e}")
        return []


def get_sold_listings(bgg_id: str, num_listings: int = 5) -> List[Dict]:
    """
    Return recently sold BGG Marketplace listings for a game.

    NOTE: BGG's sold listing history is not available via any public or
    token-authenticated API — it requires an active BGG session cookie.
    This function always returns an empty list in automated/CI environments.

    Each listing dict (if data were available) would have:
      price     - sale price e.g. "$18.00"
      condition - e.g. "Like New"
      date_sold - e.g. "Mar 5, 2026"
      seller    - BGG username
      notes     - seller notes
    """
    # Sold listings are not accessible without a BGG session cookie.
    # The api.geekdo.com/api/market/products endpoint only returns active listings.
    # The api.geekdo.com/api/market/sales endpoint requires POST + session auth.
    return []


def _format_date(date_raw: str) -> str:
    """Convert a raw date string from BGG into a friendly format."""
    if not date_raw:
        return 'Unknown'
    # BGG returns "2026-01-14 13:43:48" or ISO formats
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S%z',
                '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            dt = datetime.strptime(date_raw[:19], fmt[:19])
            return dt.strftime('%b %-d, %Y')   # e.g. "Jan 14, 2026"
        except ValueError:
            continue
    return date_raw
