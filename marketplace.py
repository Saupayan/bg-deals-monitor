"""
marketplace.py
--------------
Scrapes BGG's GeekMarket for the last 5 SOLD listings of a game in the USA.

BGG's marketplace is JavaScript-rendered, but the data behind it is served
by the GeekDo API at api.geekdo.com.  We call that internal JSON endpoint
directly (same thing the BGG website does in the background).

Endpoint (sold listings, US only):
  GET https://api.geekdo.com/api/geekmarket
  Params: objectid, objecttype=thing, status=sold, country=US, pageid=1
"""

import time
from typing import List, Dict, Optional
from datetime import datetime

import requests
import config


GEEKDO_BASE = "https://api.geekdo.com"

HEADERS = {
    'User-Agent':    'BGGDealMonitor/1.0 (personal use)',
    'Accept':        'application/json',
    'Referer':       'https://boardgamegeek.com/',
    'Authorization': f'Bearer {config.BGG_API_TOKEN}',
}


def get_sold_listings(bgg_id: str, num_listings: int = 5) -> List[Dict]:
    """
    Return the last `num_listings` sold BGG marketplace listings for a game,
    filtered to USA sales only.

    Each listing dict has:
      price       - sale price as a string e.g. "$18.00"
      condition   - "New", "Like New", "Very Good", "Good", "Acceptable", "Poor"
      date_sold   - human-readable date string e.g. "Mar 5, 2026"
      seller      - BGG username of the seller
      notes       - any notes left by the seller (truncated)
    """
    url = f"{GEEKDO_BASE}/api/geekmarket"
    params = {
        'objectid':   bgg_id,
        'objecttype': 'thing',
        'status':     'sold',
        'country':    'US',
        'currency':   'USD',
        'pageid':     1,
    }

    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=20)

        if resp.status_code == 200:
            data = resp.json()
            return _parse_listings(data, num_listings)

        elif resp.status_code == 401:
            # Try without auth header (some endpoints are public)
            headers_no_auth = {k: v for k, v in HEADERS.items() if k != 'Authorization'}
            resp2 = requests.get(url, params=params, headers=headers_no_auth, timeout=20)
            if resp2.status_code == 200:
                return _parse_listings(resp2.json(), num_listings)

        print(f"    ⚠️  Marketplace API returned HTTP {resp.status_code}")
        return _fallback_scrape(bgg_id, num_listings)

    except requests.exceptions.RequestException as e:
        print(f"    ❌ Marketplace request error: {e}")
        return []
    except Exception as e:
        print(f"    ❌ Marketplace parse error: {e}")
        return []


def _parse_listings(data: dict, limit: int) -> List[Dict]:
    """Parse GeekDo API JSON response for marketplace listings."""
    listings = []

    # The JSON structure is typically: {"items": [...]}
    items = data.get('items', data.get('results', []))

    for item in items[:limit]:
        try:
            # Price
            price_raw = item.get('price', '')
            currency  = item.get('currency', 'USD')
            if isinstance(price_raw, (int, float)):
                price_str = f"${price_raw:.2f}"
            else:
                price_str = str(price_raw)

            # Condition
            condition = item.get('condition', item.get('conditiontext', 'Unknown'))

            # Date sold
            date_raw = item.get('datesold', item.get('lastupdated', ''))
            date_str = _format_date(date_raw)

            # Seller
            seller = item.get('username', item.get('user', {}).get('username', 'Unknown'))

            # Notes (truncated)
            notes = item.get('notes', '')
            if notes and len(notes) > 100:
                notes = notes[:100] + '…'

            listings.append({
                'price':     price_str,
                'condition': condition,
                'date_sold': date_str,
                'seller':    seller,
                'notes':     notes or '—',
            })
        except Exception:
            continue

    return listings


def _format_date(date_raw: str) -> str:
    """Convert a raw date string from BGG into a friendly format."""
    if not date_raw:
        return 'Unknown'
    # BGG typically returns ISO-ish strings: "2026-03-05T14:22:00+00:00" or "2026-03-05"
    for fmt in ('%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d', '%m/%d/%Y'):
        try:
            dt = datetime.strptime(date_raw[:19], fmt[:len(fmt)])  # truncate tz
            return dt.strftime('%b %d, %Y').replace(' 0', ' ')   # e.g. "Mar 5, 2026"
        except ValueError:
            continue
    return date_raw  # return as-is if we can't parse


def _fallback_scrape(bgg_id: str, limit: int) -> List[Dict]:
    """
    Fallback: try scraping the BGG marketplace page directly with requests.
    BGG's marketplace page HTML may contain embedded JSON data in a <script> tag.
    """
    import re, json

    url = f"https://boardgamegeek.com/boardgame/{bgg_id}/geekmarket"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept':     'text/html,application/xhtml+xml',
        'Referer':    'https://boardgamegeek.com/',
    }

    try:
        resp = requests.get(url, headers=headers, timeout=20)
        if resp.status_code != 200:
            return []

        # Look for embedded JSON: window.GEEK_MARKET_DATA = {...}
        match = re.search(r'window\.GEEK_MARKET_DATA\s*=\s*(\{.*?\});', resp.text, re.DOTALL)
        if match:
            data = json.loads(match.group(1))
            return _parse_listings(data, limit)

    except Exception as e:
        print(f"    ❌ Fallback scrape failed: {e}")

    return []
