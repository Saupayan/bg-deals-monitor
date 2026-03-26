"""
ttm_dotd.py
-----------
Monitors Tabletop Merchant's Deal of the Day.
https://tabletopmerchant.com/collections/deal-of-the-day

How it works:
  1. Fetches the Shopify collection JSON endpoint — no scraping, clean API response.
  2. Strips the " (DEAL OF THE DAY)" title suffix to get the real game name.
  3. Looks up the game on BGG; skips if BGG rating < 7.0.
  4. If qualifying, runs the full research pipeline:
       - BGG rating, ranking, weight, best player count, player range
       - BGG marketplace current USA listings (for-sale, cheapest first)
       - BGG marketplace recently sold USA prices
       - Retail prices across US stores
       - Community reviews (positive + negative)
  5. Sends a WhatsApp alert with all of the above via format_full_deal().

De-duplication:
  Tracks the Shopify product handle in ttm_sent.json (resets daily).
  The --force path bypasses de-duplication.
"""

import json
import re
import time
import traceback
from datetime import date
from pathlib import Path
from typing import Optional, Dict, List

import requests

import enrichment
import whatsapp_notifier

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

TTM_COLLECTION_JSON = (
    "https://tabletopmerchant.com/collections/deal-of-the-day/products.json"
)
TTM_PRODUCT_BASE    = "https://tabletopmerchant.com/products"
TTM_DOTD_PAGE       = "https://tabletopmerchant.com/collections/deal-of-the-day"
SENT_STATE_FILE     = Path(__file__).parent / "ttm_sent.json"

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/122.0.0.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}


# ─────────────────────────────────────────────────────────────────────────────
# DE-DUPLICATION STATE
# ─────────────────────────────────────────────────────────────────────────────

def _load_sent_today() -> set:
    """Return the set of Shopify handles already sent today."""
    if not SENT_STATE_FILE.exists():
        return set()
    try:
        data = json.loads(SENT_STATE_FILE.read_text())
        if data.get('date') == str(date.today()):
            return set(data.get('handles', []))
    except Exception:
        pass
    return set()


def _mark_sent(handle: str) -> None:
    """Record a product handle as sent today."""
    sent = _load_sent_today()
    sent.add(handle)
    SENT_STATE_FILE.write_text(json.dumps({
        'date':    str(date.today()),
        'handles': list(sent),
    }))


# ─────────────────────────────────────────────────────────────────────────────
# FETCH DEAL
# ─────────────────────────────────────────────────────────────────────────────

def fetch_dotd() -> Optional[Dict]:
    """
    Fetch today's Tabletop Merchant Deal of the Day via the Shopify JSON API.

    Returns a dict with keys:
      title, clean_name, handle, deal_price, was_price, discount_pct, url
    or None if the collection is empty or the request fails.
    """
    print(f"\n  TTM: Fetching Deal of the Day from {TTM_COLLECTION_JSON} ...")
    try:
        resp = requests.get(TTM_COLLECTION_JSON, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            print(f"  TTM: HTTP {resp.status_code}")
            return None
        data = resp.json()
    except Exception as e:
        print(f"  TTM: Request failed — {e}")
        return None

    products = data.get('products', [])
    if not products:
        print("  TTM: No products in Deal of the Day collection.")
        return None

    product = products[0]
    title   = product.get('title', '')
    handle  = product.get('handle', '')

    # Strip the "(DEAL OF THE DAY)" suffix to get the real game name
    clean_name = re.sub(r'\s*\(DEAL OF THE DAY\)\s*', '', title, flags=re.IGNORECASE).strip()
    if not clean_name:
        clean_name = title

    # Price info from the first (usually only) variant
    variants = product.get('variants', [])
    if not variants:
        print("  TTM: No variants found.")
        return None

    variant       = variants[0]
    deal_price    = float(variant.get('price') or 0)
    compare_price = float(variant.get('compare_at_price') or 0)

    discount_pct = 0.0
    if compare_price > 0 and deal_price > 0:
        discount_pct = round((1 - deal_price / compare_price) * 100, 1)

    product_url = f"{TTM_PRODUCT_BASE}/{handle}"

    print(f"  TTM: Found — '{clean_name}' at ${deal_price:.2f} "
          f"(was ${compare_price:.2f}, -{discount_pct:.0f}%)")

    return {
        'title':        title,
        'clean_name':   clean_name,
        'handle':       handle,
        'deal_price':   deal_price,
        'was_price':    compare_price,
        'discount_pct': discount_pct,
        'url':          product_url,
    }


# ─────────────────────────────────────────────────────────────────────────────
# FULL RESEARCH PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def _research_deal(dotd: Dict, min_bgg_rating: float = None) -> Optional[Dict]:
    """
    Run the unified BGG enrichment pipeline for a TTM deal.
    Returns None if the game is below the rating threshold or not found on BGG.

    Uses enrichment.enrich_game() — the same pipeline as all other sources.

    min_bgg_rating: pass config.BGG_MIN_RATING_FORCE (7.0) for manual triggers,
                    config.BGG_MIN_RATING_AUTO (7.5) for scheduled runs.
    """
    import config as _cfg
    if min_bgg_rating is None:
        min_bgg_rating = _cfg.BGG_MIN_RATING_AUTO

    game_name = dotd['clean_name']
    print(f"  TTM: Researching '{game_name}'...")

    enriched = enrichment.enrich_game(
        game_name,
        filter_by_rating=True,
        min_bgg_rating=min_bgg_rating,
        include_reviews=True,
    )
    if enriched is None:
        return None

    return {
        **dotd,
        'bgg_id':           enriched['bgg_id'],
        'game_details':     enriched['game_details'],
        'bgg_rating':       (enriched.get('game_details') or {}).get('average_rating') or 0.0,
        'current_listings': enriched['current_listings'],
        'sold_listings':    enriched['sold_listings'],
        'retail_prices':    enriched['retail_prices'],
        'reviews':          enriched['reviews'],
    }


# ─────────────────────────────────────────────────────────────────────────────
# MAIN CHECK FUNCTION
# ─────────────────────────────────────────────────────────────────────────────

def check_ttm_dotd(force: bool = False) -> None:
    """
    Fetch the Tabletop Merchant DotD, filter by BGG rating >= 7.0,
    and send a full-detail WhatsApp alert if it's new today (or force=True).

    force=True: bypass de-duplication (used by --force / WhatsApp trigger).
    """
    print(f"\n{'='*60}")
    print(f"  Tabletop Merchant Deal of the Day check")
    print(f"{'='*60}")

    dotd = fetch_dotd()
    if not dotd:
        print("  TTM: No deal found today.")
        return

    # De-duplication check
    if not force:
        sent_today = _load_sent_today()
        if dotd['handle'] in sent_today:
            print(f"  TTM: '{dotd['clean_name']}' already sent today — skipping.")
            return

    import config as _cfg
    threshold = _cfg.BGG_MIN_RATING_FORCE if force else _cfg.BGG_MIN_RATING_AUTO
    deal = _research_deal(dotd, min_bgg_rating=threshold)
    if not deal:
        # Below rating threshold or not on BGG — mark sent to avoid re-checking,
        # and send a screenshot so the user can see the deal and decide.
        if not force:
            _mark_sent(dotd['handle'])
        enrichment.send_screenshot_fallback(
            dotd.get('url', TTM_DOTD_PAGE),
            f"🏪 Tabletop Merchant Deal of the Day: {dotd['clean_name']}\n"
            f"💰 ${dotd['deal_price']:.2f}"
            + (f" (was ${dotd['was_price']:.2f}, -{dotd['discount_pct']:.0f}%)"
               if dotd.get('was_price') else '')
            + f"\n⚠️ Below rating threshold or not on BGG.\n"
            f"🔗 {dotd.get('url', TTM_DOTD_PAGE)}",
        )
        return

    # Build price line
    disc_str  = f", -{deal['discount_pct']:.0f}%" if deal['discount_pct'] else ''
    was_str   = f"  (was ${deal['was_price']:.2f}{disc_str})" if deal['was_price'] else ''
    price_line = f"🏷 *Now: ${deal['deal_price']:.2f}*{was_str}  —  Tabletop Merchant"

    msg = whatsapp_notifier.format_full_deal(
        source_header    = '🏪 *Tabletop Merchant — Deal of the Day*',
        deal_price_line  = price_line,
        deal_url         = deal['url'],
        game_details     = deal['game_details'],
        sold_listings    = deal['sold_listings'],
        current_listings = deal['current_listings'],
        retail_prices    = deal['retail_prices'],
        reviews          = deal['reviews'],
    )

    print(f"\n  TTM: Sending WhatsApp alert for '{deal['clean_name']}'...")
    print(msg)
    whatsapp_notifier.send_whatsapp(msg)

    if not force:
        _mark_sent(dotd['handle'])
