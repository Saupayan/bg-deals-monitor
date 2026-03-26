"""
enrichment.py
-------------
Unified BGG enrichment pipeline — shared by ALL deal sources.

Given a game name, runs the same sequence of steps regardless of where
the deal came from (BGG Hot Deals, GameNerdz, Tabletop Merchant, BGO, etc.):

  1. BGG search  → BGG ID
  2. BGG details → rating, rank, weight, player count, best-at
  3. Rating gate → return None if rating < min_bgg_rating  (when filter_by_rating=True)
  4. BGG Marketplace → current USA listings + recently sold prices
  5. Retail prices   → via Board Game Oracle
  6. Community reviews (optional, slower)

Returns:
  - dict  on success (game_details may be None if BGG lookup failed and
          filter_by_rating=False — callers should send a screenshot fallback)
  - None  if the game was intentionally skipped (below rating threshold, or
          not found on BGG when filter_by_rating=True)

Screenshot fallback:
  Call send_screenshot_fallback(page_url, caption) whenever you want to
  send a live thum.io page screenshot via WhatsApp as a fallback.
"""

import time
from typing import Optional, Dict, List

import bgg_api
import marketplace
import price_checker
import whatsapp_notifier
import config


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def enrich_game(
    game_name: str,
    *,
    filter_by_rating: bool = True,
    min_bgg_rating: Optional[float] = None,
    include_reviews: bool = True,
    num_listings: int = 5,
) -> Optional[Dict]:
    """
    Run the full BGG enrichment pipeline for a game.

    Args:
        game_name:        Game title to look up.
        filter_by_rating: If True, return None when rating < min_bgg_rating
                          (or when the game can't be found on BGG at all).
        min_bgg_rating:   Minimum BGG rating required. Defaults to
                          config.BGG_MIN_RATING when not specified.
        include_reviews:  If True, fetch community reviews (adds ~3–5 s).
        num_listings:     How many marketplace listings to fetch per category.

    Returns:
        Dict with keys:
          bgg_id, game_details, current_listings, sold_listings,
          retail_prices, reviews
        or None if the game should be skipped (filtered or not found).
    """
    threshold = min_bgg_rating if min_bgg_rating is not None else config.BGG_MIN_RATING
    print(f"  [Enrich] '{game_name}' (threshold: {threshold}, reviews: {include_reviews})")

    # ── Step 1: BGG search ────────────────────────────────────────────────────
    print(f"  [Enrich] Searching BGG...")
    bgg_id = bgg_api.search_game(game_name)

    if not bgg_id:
        print(f"  [Enrich] '{game_name}' not found on BGG.")
        if filter_by_rating:
            # Can't confirm quality without BGG data — skip
            return None
        # Caller wants best-effort data even without BGG — return empty shell
        return {
            'bgg_id':           None,
            'game_details':     None,
            'current_listings': [],
            'sold_listings':    [],
            'retail_prices':    _get_retail(game_name, ''),
            'reviews':          {'positive': [], 'negative': []},
        }

    print(f"  [Enrich] BGG ID: {bgg_id}")
    time.sleep(1)

    # ── Step 2: BGG game details ──────────────────────────────────────────────
    game_details = bgg_api.get_game_details(bgg_id)
    if game_details:
        print(f"  [Enrich] {game_details['name']} | "
              f"Rating: {game_details['average_rating']} | "
              f"Weight: {game_details['weight']} | "
              f"Best@: {game_details['best_players']}p | "
              f"Rank: {game_details.get('bgg_rank', '?')}")
    else:
        print(f"  [Enrich] Could not fetch details for BGG ID {bgg_id}.")

    # ── Step 3: Rating gate ───────────────────────────────────────────────────
    if filter_by_rating:
        rating = (game_details or {}).get('average_rating') or 0.0
        if rating < threshold:
            print(f"  [Enrich] Rating {rating:.1f} < {threshold} — skipping.")
            return None

    # ── Step 4: BGG Marketplace ───────────────────────────────────────────────
    current_listings: List[Dict] = []
    sold_listings: List[Dict] = []
    if bgg_id:
        print(f"  [Enrich] Fetching BGG Marketplace listings...")
        time.sleep(1)
        try:
            current_listings = marketplace.get_current_listings(bgg_id, num_listings=num_listings)
            print(f"  [Enrich] Current listings: {len(current_listings)}")
        except Exception as e:
            print(f"  [Enrich] Current listings error: {e}")
        time.sleep(0.5)
        try:
            sold_listings = marketplace.get_sold_listings(bgg_id, num_listings=num_listings)
            print(f"  [Enrich] Sold listings: {len(sold_listings)}")
        except Exception as e:
            print(f"  [Enrich] Sold listings error: {e}")

    # ── Step 5: Retail prices ─────────────────────────────────────────────────
    name_for_search = (game_details or {}).get('name') or game_name
    retail_prices = _get_retail(name_for_search, bgg_id or '')

    # ── Step 6: Community reviews (optional) ──────────────────────────────────
    reviews: Dict = {'positive': [], 'negative': []}
    if include_reviews and bgg_id:
        print(f"  [Enrich] Fetching community reviews...")
        time.sleep(1)
        try:
            reviews = bgg_api.get_game_reviews(bgg_id)
            print(f"  [Enrich] Reviews: "
                  f"{len(reviews.get('positive', []))} positive, "
                  f"{len(reviews.get('negative', []))} negative")
        except Exception as e:
            print(f"  [Enrich] Reviews error: {e}")

    return {
        'bgg_id':           bgg_id,
        'game_details':     game_details,
        'current_listings': current_listings,
        'sold_listings':    sold_listings,
        'retail_prices':    retail_prices,
        'reviews':          reviews,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SCREENSHOT FALLBACK
# ─────────────────────────────────────────────────────────────────────────────

def send_screenshot_fallback(page_url: str, caption: str) -> None:
    """
    Send a live page screenshot via WhatsApp using thum.io (free, no API key).

    A Unix timestamp in the query string forces thum.io to render a fresh
    screenshot on every call, bypassing its app cache and CDN edge cache.

    Use this when enrichment fails and you still want to notify about a deal.
    """
    ts = int(time.time())
    separator = '&' if '?' in page_url else '?'
    target_url = f"{page_url}{separator}_t={ts}"
    thum_url = f"https://image.thum.io/get/noanimate/nocache/{target_url}"
    print(f"  [Fallback] Sending screenshot of {page_url} via WhatsApp...")
    whatsapp_notifier.send_image_whatsapp(thum_url, caption)


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _get_retail(game_name: str, bgg_id: str) -> List[Dict]:
    """Fetch retail prices, returning an empty list on failure."""
    print(f"  [Enrich] Checking retail prices for '{game_name}'...")
    try:
        prices = price_checker.get_all_prices(game_name, bgg_id)
        if prices:
            print(f"  [Enrich] {len(prices)} retail price(s). "
                  f"Cheapest: {prices[0]['store']} @ {prices[0]['price_str']}")
        else:
            print(f"  [Enrich] No retail prices found.")
        return prices
    except Exception as e:
        print(f"  [Enrich] Retail prices error: {e}")
        return []
