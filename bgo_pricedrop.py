"""
bgo_pricedrop.py
----------------
Monitors Board Game Oracle (boardgameoracle.com) Daily Price Drops.

What it does each run:
  1. Fetches https://www.boardgameoracle.com/pricedrop/daily
  2. Extracts structured game data from the embedded __NEXT_DATA__ JSON
     (the site is Next.js SSR — no Playwright needed)
  3. Filters to games with BGG rating >= 7.0
  4. For each qualifying NEW game (not already sent today):
       a. Looks up BGG ID and fetches recent marketplace sold listings
       b. Sends a WhatsApp alert with:
          - Game name, BGG rating, weight, player count
          - Deal: current price, was price, % drop, store, badge (30d/52w low)
          - BGG marketplace recent sold prices for comparison
          - Link to BGO page

De-duplication:
  Tracks sent game IDs in bgo_sent.json (resets daily).
  The --force path bypasses de-duplication and re-sends everything.
"""

import json
import re
import time
import traceback
from datetime import date
from pathlib import Path
from typing import Optional, List, Dict

import requests
from bs4 import BeautifulSoup

import bgg_api
import marketplace
import price_checker
import whatsapp_notifier

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

BGO_DAILY_URL    = "https://www.boardgameoracle.com/pricedrop/daily"
BGO_BASE_URL     = "https://www.boardgameoracle.com"
BGG_RATING_MIN   = 7.0
SENT_STATE_FILE  = Path(__file__).parent / "bgo_sent.json"

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/122.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}


# ─────────────────────────────────────────────────────────────────────────────
# DE-DUPLICATION STATE
# ─────────────────────────────────────────────────────────────────────────────

def _load_sent_today() -> set:
    """Return the set of BGO game IDs already sent today."""
    if not SENT_STATE_FILE.exists():
        return set()
    try:
        data = json.loads(SENT_STATE_FILE.read_text())
        if data.get('date') == str(date.today()):
            return set(data.get('ids', []))
    except Exception:
        pass
    return set()


def _mark_sent(game_id: str) -> None:
    """Record a game ID as sent today."""
    sent = _load_sent_today()
    sent.add(game_id)
    SENT_STATE_FILE.write_text(json.dumps({
        'date': str(date.today()),
        'ids':  list(sent),
    }))


# ─────────────────────────────────────────────────────────────────────────────
# FETCH + PARSE
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_page_html() -> Optional[str]:
    """Fetch the BGO daily price drop page HTML."""
    try:
        resp = requests.get(BGO_DAILY_URL, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            print(f"  BGO: HTTP {resp.status_code} from {BGO_DAILY_URL}")
            return None
        return resp.text
    except Exception as e:
        print(f"  BGO: Failed to fetch page — {e}")
        return None


def _extract_next_data(html: str) -> Optional[List[Dict]]:
    """
    Extract the game list from the __NEXT_DATA__ JSON embedded in the page.
    Returns a list of raw item dicts, or None on failure.
    """
    soup = BeautifulSoup(html, 'lxml')
    tag = soup.find('script', id='__NEXT_DATA__')
    if not tag or not tag.string:
        print("  BGO: __NEXT_DATA__ script tag not found.")
        return None
    try:
        next_data = json.loads(tag.string)
    except json.JSONDecodeError as e:
        print(f"  BGO: Failed to parse __NEXT_DATA__ JSON — {e}")
        return None

    # Navigate: props → pageProps → trpcState → queries → pricedrop.listDaily
    queries = (next_data
               .get('props', {})
               .get('pageProps', {})
               .get('trpcState', {})
               .get('queries', []))

    for query in queries:
        pages = query.get('state', {}).get('data', {}).get('pages', [])
        if pages and pages[0].get('items'):
            return pages[0]['items']

    print("  BGO: Could not find items in __NEXT_DATA__ queries.")
    return None


def _extract_store_names(html: str) -> List[str]:
    """
    Parse store names from the HTML MUI card spans (SSR-rendered).
    Each card has the same span order:
      [name, 'year • N offers', 'Lowest price', STORE_NAME, current_price, ...]
    Returns a list of store names, one per card, in the same order as the items.
    """
    soup = BeautifulSoup(html, 'lxml')
    stores: List[str] = []
    for card in soup.find_all('a', href=re.compile(r'/boardgame/price/')):
        spans = card.find_all('span', class_=re.compile(r'MuiTypography'))
        # Span index 3 = store name (0-indexed: name, year, "Lowest price", store)
        if len(spans) >= 4:
            stores.append(spans[3].get_text(strip=True))
        else:
            stores.append('Unknown')
    return stores


def fetch_price_drops() -> List[Dict]:
    """
    Fetch and parse the BGO daily price drops.

    Returns a list of dicts, each containing:
      id, title, slug, key, bgg_rating, bgg_complexity,
      min_players, max_players, lowest_price, was_price,
      discount_pct, store, is_lowest_30d, is_lowest_52w, bgo_url
    """
    print(f"\n  BGO: Fetching daily price drops from {BGO_DAILY_URL} ...")
    html = _fetch_page_html()
    if not html:
        return []

    items = _extract_next_data(html)
    if not items:
        return []

    store_names = _extract_store_names(html)
    print(f"  BGO: Found {len(items)} price drops, {len(store_names)} store names.")

    results = []
    for i, item in enumerate(items):
        try:
            detail = item.get('detail', {})
            ps     = item.get('price_stats', {})

            bgg_rating = detail.get('bgg_rating') or 0.0
            lowest     = ps.get('lowest_price') or 0.0
            day_change = ps.get('price_drop_day_change_value') or 0.0
            day_pct    = ps.get('price_drop_day_change_percent') or 0.0  # negative

            was_price  = round(lowest - day_change, 2)      # e.g. 33.99 - (-10) = 43.99
            disc_pct   = round(abs(day_pct) * 100, 1)       # e.g. 22.7 → 23%

            item_key   = item.get('key', '')
            slug       = item.get('slug', '')
            bgo_url    = f"{BGO_BASE_URL}/boardgame/price/{item_key}/{slug}"

            results.append({
                'id':            item.get('id', ''),
                'title':         item.get('title', ''),
                'slug':          slug,
                'key':           item_key,
                'bgg_rating':    round(bgg_rating, 1),
                'bgg_complexity': round(detail.get('bgg_complexity') or 0, 1),
                'min_players':   detail.get('min_players'),
                'max_players':   detail.get('max_players'),
                'lowest_price':  lowest,
                'was_price':     was_price,
                'discount_pct':  disc_pct,
                'store':         store_names[i] if i < len(store_names) else 'Unknown',
                'is_lowest_30d': bool(ps.get('is_lowest_30d')),
                'is_lowest_52w': bool(ps.get('is_lowest_52w')),
                'bgo_url':       bgo_url,
            })
        except Exception as e:
            print(f"  BGO: Error parsing item {i}: {e}")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# FULL RESEARCH PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def _research_drop(drop: Dict) -> Optional[Dict]:
    """
    Full research pipeline for a qualifying BGO price drop:
      1. BGG lookup (rating/rank/weight/players) — use bgg_rating from BGO as pre-filter,
         then fetch full details for rank, best players, bgg_url
      2. BGG marketplace current USA listings
      3. BGG marketplace recently sold USA prices
      4. Retail prices across US stores
      5. Community reviews (positive + negative)
    Returns an enriched dict, or None if BGG lookup fails entirely.
    """
    game_name = drop['title']

    print(f"  BGO: Looking up '{game_name}' on BGG for full details...")
    bgg_id       = bgg_api.search_game(game_name)
    game_details = None

    if bgg_id:
        time.sleep(1)
        game_details = bgg_api.get_game_details(bgg_id)
        if game_details:
            print(f"  BGO: BGG — Rank: {game_details.get('bgg_rank','N/A')} | "
                  f"Best: {game_details.get('best_players','?')}p | "
                  f"Players: {game_details.get('min_players','?')}–{game_details.get('max_players','?')}")
    else:
        print(f"  BGO: '{game_name}' not found on BGG — will use BGO rating data only.")

    # BGG marketplace current listings
    current_listings: List[Dict] = []
    if bgg_id:
        print(f"  BGO: Fetching BGG marketplace current listings (USA)...")
        time.sleep(1)
        try:
            current_listings = marketplace.get_current_listings(bgg_id, num_listings=5)
            print(f"  BGO: Found {len(current_listings)} current listing(s).")
        except Exception as e:
            print(f"  BGO: Current listings error — {e}")

    # BGG marketplace recently sold
    sold_listings: List[Dict] = []
    if bgg_id:
        print(f"  BGO: Fetching BGG marketplace sold listings (USA)...")
        time.sleep(1)
        try:
            sold_listings = marketplace.get_sold_listings(bgg_id, num_listings=5)
            print(f"  BGO: Found {len(sold_listings)} sold listing(s).")
        except Exception as e:
            print(f"  BGO: Sold listings error — {e}")

    # Retail prices
    name_for_search = (game_details or {}).get('name', game_name)
    retail_prices: List[Dict] = []
    print(f"  BGO: Checking retail prices for '{name_for_search}'...")
    try:
        retail_prices = price_checker.get_all_prices(name_for_search, bgg_id or '')
        print(f"  BGO: Found {len(retail_prices)} retail price(s).")
    except Exception as e:
        print(f"  BGO: Retail prices error — {e}")

    # Community reviews
    reviews: Dict = {'positive': [], 'negative': []}
    if bgg_id:
        print(f"  BGO: Fetching community reviews...")
        time.sleep(1)
        try:
            reviews = bgg_api.get_game_reviews(bgg_id)
            print(f"  BGO: {len(reviews.get('positive',[]))} positive, "
                  f"{len(reviews.get('negative',[]))} negative review(s).")
        except Exception as e:
            print(f"  BGO: Reviews error — {e}")

    return {
        **drop,
        'bgg_id':           bgg_id,
        'game_details':     game_details,
        'current_listings': current_listings,
        'sold_listings':    sold_listings,
        'retail_prices':    retail_prices,
        'reviews':          reviews,
    }


# ─────────────────────────────────────────────────────────────────────────────
# MAIN CHECK FUNCTION
# ─────────────────────────────────────────────────────────────────────────────

def check_bgo_price_drops(force: bool = False) -> None:
    """
    Fetch BGO daily price drops, filter by BGG rating >= 7.0, and send
    full-detail WhatsApp alerts for new qualifying deals.

    force=True: bypass de-duplication (re-send everything that qualifies).
    force=False: skip games already sent today.
    """
    print(f"\n{'='*60}")
    print(f"  BGO Daily Price Drop check")
    print(f"{'='*60}")

    drops = fetch_price_drops()
    if not drops:
        print("  BGO: No price drops fetched.")
        return

    qualifying = [d for d in drops if d['bgg_rating'] >= BGG_RATING_MIN]
    print(f"  BGO: {len(drops)} drops total, {len(qualifying)} with BGG rating >= {BGG_RATING_MIN}")

    if not qualifying:
        print("  BGO: Nothing qualifies today.")
        return

    sent_today = _load_sent_today() if not force else set()
    new_alerts = [d for d in qualifying if d['id'] not in sent_today]

    if not new_alerts:
        print("  BGO: All qualifying drops already sent today.")
        return

    print(f"  BGO: Sending alerts for {len(new_alerts)} new qualifying drop(s).")

    for drop in new_alerts:
        try:
            print(f"\n  BGO: Processing '{drop['title']}' "
                  f"(BGO rating: {drop['bgg_rating']}, -{drop['discount_pct']:.0f}%)")

            researched = _research_drop(drop)
            if not researched:
                continue

            # Price line — include badge if applicable
            badge = ''
            if drop['is_lowest_52w']:
                badge = '  🏆 52-week low!'
            elif drop['is_lowest_30d']:
                badge = '  📉 30-day low!'

            price_line = (
                f"🏷 *Now: ${drop['lowest_price']:.2f}*"
                f"  (was ${drop['was_price']:.2f}, -{drop['discount_pct']:.0f}%){badge}"
                f"  —  {drop['store']}"
            )

            msg = whatsapp_notifier.format_full_deal(
                source_header    = '🎲 *Board Game Oracle — Daily Price Drop*',
                deal_price_line  = price_line,
                deal_url         = drop['bgo_url'],
                game_details     = researched['game_details'],
                sold_listings    = researched['sold_listings'],
                current_listings = researched['current_listings'],
                retail_prices    = researched['retail_prices'],
                reviews          = researched['reviews'],
            )

            print(f"  BGO: Sending WhatsApp...")
            whatsapp_notifier.send_whatsapp(msg)

            if not force:
                _mark_sent(drop['id'])

            time.sleep(2)

        except Exception as e:
            print(f"  BGO: Error processing '{drop['title']}': {e}")
            traceback.print_exc()
