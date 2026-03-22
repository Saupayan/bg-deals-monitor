"""
marketplace.py
--------------
Fetches BGG GeekMarket listings for a game.

CURRENT LISTINGS (for sale):
  Uses the official BGG XML API v2 endpoint:
    GET https://boardgamegeek.com/xmlapi2/thing?id={bgg_id}&marketplace=1
  This is the documented way to get active for-sale listings. It returns
  ONLY active listings (not sold), in XML with price, condition, and date.
  Filtered to USD-priced listings (US sellers). Retries on HTTP 202.

SOLD LISTINGS:
  Uses the public GeekDo API endpoint:
    GET https://api.geekdo.com/api/market/products
    Params: objectid, objecttype=thing, country=US, status=sold, pageid=1, nosession=1
  Returns recently sold listings. Debug logging on first run will confirm
  exact field names and whether the endpoint requires session auth.
"""

import time
import xml.etree.ElementTree as ET
from typing import List, Dict
from datetime import datetime

import requests


BGG_XML_BASE = "https://boardgamegeek.com/xmlapi2"
GEEKDO_BASE  = "https://api.geekdo.com"

XML_HEADERS = {
    'User-Agent': 'BGGDealMonitor/1.0 (personal use)',
    'Accept':     'application/xml, text/xml',
}

JSON_HEADERS = {
    'User-Agent': 'BGGDealMonitor/1.0 (personal use)',
    'Accept':     'application/json',
    'Referer':    'https://boardgamegeek.com/',
}

# BGG XML API condition codes → readable strings
_CONDITION_MAP = {
    'new':        'New',
    'likenew':    'Like New',
    'verygood':   'Very Good',
    'good':       'Good',
    'acceptable': 'Acceptable',
    'poor':       'Poor',
}


# ─────────────────────────────────────────────────────────────────────────────
# CURRENT (FOR SALE) LISTINGS  —  BGG XML API v2
# ─────────────────────────────────────────────────────────────────────────────

def get_current_listings(bgg_id: str, num_listings: int = 5) -> List[Dict]:
    """
    Return current (for-sale) BGG Marketplace listings for a game.

    Uses the official BGG XML API /thing?marketplace=1 which returns ONLY
    active for-sale listings — not sold/expired ones. The previous GeekDo
    JSON approach was using a wrong status field name ('productstate') that
    doesn't exist in the response, causing the default 'active' to pass
    every item including historical sold listings.

    Filters to USD-priced listings (proxy for US sellers), sorted cheapest first.

    Each listing dict:
      price       - asking price e.g. "$18.00"
      condition   - e.g. "Like New", "Very Good", "Good"
      location    - "United States" (inferred from USD currency)
      date_listed - e.g. "Mar 5, 2026"
      notes       - seller notes (available in XML response)
      seller      - seller username (not in XML response — shown as "—")
    """
    url = f"{BGG_XML_BASE}/thing"
    params = {'id': bgg_id, 'marketplace': 1}

    # BGG sometimes returns HTTP 202 ("processing") — retry up to 5 times
    for attempt in range(5):
        try:
            resp = requests.get(url, params=params, headers=XML_HEADERS, timeout=30)

            if resp.status_code == 202:
                wait = 5 * (attempt + 1)
                print(f"    Marketplace XML: BGG processing... retry {attempt+1}/5 in {wait}s")
                time.sleep(wait)
                continue

            if resp.status_code != 200:
                print(f"    ⚠️  Marketplace XML API returned HTTP {resp.status_code}")
                return []

            root = ET.fromstring(resp.content)
            item = root.find('item')
            if item is None:
                print(f"    ⚠️  Marketplace XML: no <item> element in response")
                return []

            listings_elem = item.find('marketplacelistings')
            if listings_elem is None:
                print(f"    Marketplace XML: no <marketplacelistings> — game has no active listings")
                return []

            all_listings = listings_elem.findall('listing')
            print(f"    Marketplace XML: {len(all_listings)} total active listing(s) found")

            listings = []
            for listing in all_listings:
                try:
                    price_elem = listing.find('price')
                    if price_elem is None:
                        continue

                    currency = price_elem.get('currency', '')
                    if currency != 'USD':
                        continue  # US sellers only

                    price_val = float(price_elem.get('value', 0))
                    if price_val <= 0:
                        continue

                    condition_elem = listing.find('condition')
                    condition_raw  = (condition_elem.get('value', '') or '').lower() if condition_elem is not None else ''
                    condition      = _CONDITION_MAP.get(condition_raw, condition_raw.capitalize() or 'Unknown')

                    listdate_elem = listing.find('listdate')
                    date_listed   = _format_date(
                        listdate_elem.get('value', '') if listdate_elem is not None else ''
                    )

                    notes_elem = listing.find('notes')
                    notes      = (notes_elem.get('value', '') or '').strip() if notes_elem is not None else ''
                    notes      = notes or '—'

                    listings.append({
                        'price':       f"${price_val:.2f}",
                        'condition':   condition,
                        'location':    'United States',
                        'date_listed': date_listed,
                        'notes':       notes,
                        'seller':      '—',    # not included in XML marketplace response
                        '_price_raw':  price_val,
                    })
                except Exception:
                    continue

            listings.sort(key=lambda x: x['_price_raw'])
            for lst in listings:
                del lst['_price_raw']

            print(f"    Marketplace: {len(listings)} USD listing(s) (showing top {min(len(listings), num_listings)})")
            return listings[:num_listings]

        except requests.exceptions.RequestException as e:
            print(f"    ❌ Marketplace XML request error: {e}")
            return []
        except ET.ParseError as e:
            print(f"    ❌ Marketplace XML parse error: {e}")
            return []
        except Exception as e:
            print(f"    ❌ Marketplace error: {e}")
            return []

    print(f"    ❌ Marketplace XML: gave up after 5 retries")
    return []


# ─────────────────────────────────────────────────────────────────────────────
# SOLD LISTINGS  —  GeekDo API with status=sold
# ─────────────────────────────────────────────────────────────────────────────

def get_sold_listings(bgg_id: str, num_listings: int = 5) -> List[Dict]:
    """
    Return recently sold BGG Marketplace listings for a game (US, most recent first).

    Uses the GeekDo API with status=sold. This replaces the previous stub that
    always returned [] based on an incorrect assumption that sold data requires
    session auth. Sold listings are publicly visible on the BGG marketplace web
    page without login, so the API should expose them the same way.

    Debug logging is included so the first run will confirm the exact field
    names in the response and whether session auth is actually required.

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
        resp = requests.get(url, params=params, headers=JSON_HEADERS, timeout=20)

        if resp.status_code != 200:
            print(f"    ⚠️  Marketplace (sold) API returned HTTP {resp.status_code}")
            return []

        data = resp.json()
        products = data.get('products', [])
        print(f"    Marketplace (sold) API: {len(products)} item(s) returned")

        # Debug: log full field names on first item so we can verify the schema
        if products:
            print(f"    DEBUG sold item keys: {list(products[0].keys())}")

        listings = []
        for item in products:
            try:
                price_val = float(item.get('price', 0))
                if price_val <= 0:
                    continue

                symbol    = item.get('currencysymbol', '$')
                price_str = f"{symbol}{price_val:.2f}"
                condition = item.get('prettycondition') or item.get('condition') or 'Unknown'

                # Try several plausible field names for the sale date
                raw_date = (item.get('saledate') or item.get('solddate') or
                            item.get('lastmodified') or item.get('listdate') or '')
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

        # Sort by price (cheapest first) as a proxy for recency when no date available
        listings.sort(key=lambda x: x['_price_raw'])
        for lst in listings:
            del lst['_price_raw']

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
