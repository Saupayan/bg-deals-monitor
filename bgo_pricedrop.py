"""
bgo_pricedrop.py
----------------
Monitors Board Game Oracle (boardgameoracle.com) Daily Price Drops.

What it does each run:
  1. Fetches https://www.boardgameoracle.com/pricedrop/daily
  2. Extracts structured game data from the embedded __NEXT_DATA__ JSON
     (the site is Next.js SSR — no Playwright needed)
  3. Filters to games with BGG rating >= 7.3
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

import enrichment
import whatsapp_notifier

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

BGO_DAILY_URL    = "https://www.boardgameoracle.com/pricedrop/daily"
BGO_BASE_URL     = "https://www.boardgameoracle.com"
# Pre-filter using BGO's own rating field before running the full BGG lookup.
# This avoids unnecessary API calls for clearly low-rated games.
# The unified enrichment pipeline will re-confirm with BGG's live rating.
BGO_PREFILTER_RATING = 7.3
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

            # Explicitly cast to float so a string value, nested dict, or None
            # from BGO's JSON never silently bypasses the rating filter.
            try:
                bgg_rating = float(detail.get('bgg_rating') or 0)
            except (TypeError, ValueError):
                bgg_rating = 0.0
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
# LIGHTWEIGHT RESEARCH PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def _research_drop_compact(drop: Dict, min_bgg_rating: float = None) -> Optional[Dict]:
    """
    Run the unified BGG enrichment pipeline for a qualifying BGO price drop.

    Uses enrichment.enrich_game() — the same pipeline as all other sources.
    Reviews are skipped (include_reviews=False) to keep the compact one-liner
    format fast. Retail prices are included for comparison.

    Returns None if the game is below the rating threshold or not on BGG.

    min_bgg_rating: pass config.BGG_MIN_RATING_FORCE (7.0) for manual triggers,
                    config.BGG_MIN_RATING_AUTO (7.5) for scheduled runs.
    """
    import config as _cfg
    if min_bgg_rating is None:
        min_bgg_rating = _cfg.BGG_MIN_RATING_AUTO

    game_name = drop['title']
    print(f"  BGO: Researching '{game_name}'...")

    enriched = enrichment.enrich_game(
        game_name,
        filter_by_rating=True,
        min_bgg_rating=min_bgg_rating,
        include_reviews=False,   # BGO compact format doesn't show reviews
    )
    if enriched is None:
        return None

    return {
        **drop,
        'bgg_id':           enriched['bgg_id'],
        'game_details':     enriched['game_details'],
        'current_listings': enriched['current_listings'],
        'sold_listings':    enriched['sold_listings'],
    }


def _format_compact_line(r: Dict) -> str:
    """
    Format one BGO price drop as a single compact line.

    Format:
    *Name* — $XX.XX (-YY%) @ Store [badge] | BGG X.X | MinP-MaxPp best@B | wt W.W | Sold: $a,$b,$c | Listed: $x,$y,$z
    """
    gd = r.get('game_details') or {}

    name = gd.get('name') or r['title']

    badge = ' 🏆' if r['is_lowest_52w'] else (' 📉' if r['is_lowest_30d'] else '')
    price_part = f"${r['lowest_price']:.2f} (-{r['discount_pct']:.0f}%) @ {r['store']}{badge}"

    # Use BGG's confirmed rating if available, else fall back to BGO's
    rating = gd.get('average_rating') or r['bgg_rating']
    rating_part = f"BGG {float(rating):.1f}"

    # Players
    min_p  = gd.get('min_players')  or r.get('min_players')  or '?'
    max_p  = gd.get('max_players')  or r.get('max_players')  or '?'
    best_p = gd.get('best_players') or '?'
    players_part = (f"{min_p}p best@{best_p}" if min_p == max_p
                    else f"{min_p}-{max_p}p best@{best_p}")

    # Weight
    weight = gd.get('weight') or r.get('bgg_complexity') or 0
    weight_part = f"wt {float(weight):.1f}" if weight else "wt ?"

    # Sold prices — comma-separated list, price field only
    sold = r.get('sold_listings', [])
    sold_part = ('Sold: ' + ', '.join(s['price'] for s in sold)) if sold else 'Sold: —'

    # Listed (for-sale) prices
    listed = r.get('current_listings', [])
    listed_part = ('Listed: ' + ', '.join(l['price'] for l in listed)) if listed else 'Listed: —'

    return (f"*{name}* — {price_part} | {rating_part} | {players_part} | "
            f"{weight_part} | {sold_part} | {listed_part}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN CHECK FUNCTION
# ─────────────────────────────────────────────────────────────────────────────

def check_bgo_price_drops(force: bool = False) -> None:
    """
    Fetch BGO daily price drops, filter by BGG rating >= 7.3, research
    qualifying NEW drops (lightweight: BGG details + marketplace only),
    then send ONE combined WhatsApp message with all drops as compact one-liners.

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

    qualifying = [d for d in drops if d['bgg_rating'] >= BGO_PREFILTER_RATING]
    print(f"  BGO: {len(drops)} drops total, {len(qualifying)} with BGO rating >= {BGO_PREFILTER_RATING}")

    if not qualifying:
        print("  BGO: Nothing qualifies today.")
        return

    sent_today = _load_sent_today() if not force else set()
    new_alerts = [d for d in qualifying if d['id'] not in sent_today]

    if not new_alerts:
        print("  BGO: All qualifying drops already sent today.")
        return

    import config as _cfg
    threshold = _cfg.BGG_MIN_RATING_FORCE if force else _cfg.BGG_MIN_RATING_AUTO
    print(f"  BGO: Researching {len(new_alerts)} new qualifying drop(s) "
          f"(BGG threshold: {threshold})...")

    deal_lines: List[str] = []
    for drop in new_alerts:
        try:
            print(f"\n  BGO: Processing '{drop['title']}' "
                  f"(BGG {drop['bgg_rating']}, -{drop['discount_pct']:.0f}%)")

            researched = _research_drop_compact(drop, min_bgg_rating=threshold)
            if not researched:
                continue

            deal_lines.append(_format_compact_line(researched))

            if not force:
                _mark_sent(drop['id'])

            time.sleep(1)

        except Exception as e:
            print(f"  BGO: Error processing '{drop['title']}': {e}")
            traceback.print_exc()

    if not deal_lines:
        print("  BGO: Nothing to send after research.")
        return

    from datetime import datetime as _dt
    now_str = _dt.now().strftime('%H:%M')
    n = len(deal_lines)
    msg = (
        f"🎲 *Board Game Oracle — Daily Price Drops* "
        f"({n} new deal{'s' if n != 1 else ''})\n\n"
        + "\n".join(deal_lines)
        + f"\n\n_Checked at {now_str}_"
    )

    print(f"  BGO: Sending combined WhatsApp ({len(msg)} chars, {n} deal(s))...")
    whatsapp_notifier.send_whatsapp(msg)
