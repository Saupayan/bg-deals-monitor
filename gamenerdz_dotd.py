"""
gamenerdz_dotd.py
-----------------
Standalone checker for GameNerdz Deal of the Day (DotD).

GameNerdz posts a new Deal of the Day every day around 1pm ET.
This script:
  1. Scrapes the GameNerdz DotD page to find today's deal
  2. Extracts the game name and deal price
  3. Runs the full research pipeline (BGG stats, marketplace sales,
     retail prices from other stores, community reviews)
  4. Sends a single-deal email via emailer.send_deal_alert()

Usage:
  python gamenerdz_dotd.py            -- check right now, then schedule for 1:05pm ET every day
  python gamenerdz_dotd.py --test     -- check right now once and exit (no scheduling)
  python gamenerdz_dotd.py --loop     -- run in loop mode (used when integrated with main monitor)

GameNerdz DotD page: https://www.gamenerdz.com/deal-of-the-day
"""

import sys
import time
import traceback
import pytz
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Dict

import requests
from bs4 import BeautifulSoup

import schedule
import config
import bgg_api
import emailer
import whatsapp_notifier
import marketplace
import price_checker
from game_parser import extract_game_name


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# CONSTANTS
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

GAMENERDZ_DOTD_URL = "https://www.gamenerdz.com/deal-of-the-day"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}

# Track which dates we've already sent an alert for (avoids double-sends)
SENT_TODAY_FILE = Path(__file__).parent / "gamenerdz_sent.txt"


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# ALREADY-SENT STATE
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def _already_sent_today() -> bool:
    """Return True if we already sent a DotD alert today."""
    if not SENT_TODAY_FILE.exists():
        return False
    last_sent = SENT_TODAY_FILE.read_text().strip()
    return last_sent == str(date.today())


def _mark_sent_today() -> None:
    """Record that we've sent today's DotD alert."""
    SENT_TODAY_FILE.write_text(str(date.today()))


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# SCRAPE GAMENERDZ DotD
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def fetch_dotd() -> Optional[Dict]:
    """
    Scrape the GameNerdz Deal of the Day page.
    Returns a dict with keys: name, price_str, url, image_url
    or None if the page couldn't be parsed.
    """
    print(f"\n  Fetching GameNerdz Deal of the Day from {GAMENERDZ_DOTD_URL} ...")
    try:
        resp = requests.get(GAMENERDZ_DOTD_URL, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            print(f"  GameNerdz returned HTTP {resp.status_code}")
            return None

        soup = BeautifulSoup(resp.text, 'lxml')

        # Strategy 1: look for a product name in common e-commerce patterns
        # GameNerdz uses Magento-style HTML
        deal = _parse_dotd_page(soup, resp.url)
        if deal:
            return deal

        print("  Could not parse DotD â page structure may have changed.")
        return None

    except Exception as e:
        print(f"  Error fetching GameNerdz DotD: {e}")
        traceback.print_exc()
        return None


def _parse_dotd_page(soup: BeautifulSoup, page_url: str) -> Optional[Dict]:
    """
    Try multiple HTML patterns to extract the DotD product name and price.
    GameNerdz uses Magento; their DotD URL is a category page listing one
    product, so the product name is in a listing element, not a bare h1.
    """

    name = None

    # Strategy A: category/listing page â the DotD URL shows one product in a
    # product grid.  Magento listing pages put the name in these elements.
    for css in [
        'a.product-item-link',
        'strong.product-item-name',
        '.product-item-name a',
        '.product-item-name',
        '.product-name a',
        '.product-name',
        '[data-ui-id="page-title-wrapper"]',
        '.product-info-main h1',
    ]:
        elem = soup.select_one(css)
        if elem:
            candidate = elem.get_text(strip=True)
            if len(candidate) > 5 and 'deal of the day' not in candidate.lower():
                name = candidate
                break

    # Strategy B: product detail page selectors (if URL redirects to a product)
    if not name:
        for selector in [
            ('h1', {'class': 'page-title'}),
            ('span', {'itemprop': 'name'}),
            ('h1', {'class': 'product-name'}),
        ]:
            tag, attrs = selector
            elem = soup.find(tag, attrs)
            if elem:
                candidate = elem.get_text(strip=True)
                if len(candidate) > 5 and 'deal of the day' not in candidate.lower():
                    name = candidate
                    break

    # Strategy C: any h1 that isn't the page category header
    if not name:
        for elem in soup.find_all('h1'):
            candidate = elem.get_text(strip=True)
            if len(candidate) > 5 and 'deal of the day' not in candidate.lower():
                name = candidate
                break

    if not name:
        # Debug dump so we can diagnose future failures
        page_text = soup.get_text(separator=' ', strip=True)
        print(f"  DEBUG: page title tag = {soup.title.string if soup.title else 'N/A'}")
        print(f"  DEBUG: first 500 chars of visible text: {page_text[:500]}")
        all_h1 = [e.get_text(strip=True) for e in soup.find_all('h1')]
        print(f"  DEBUG: all h1 tags found: {all_h1}")
        return None

    # Price â try meta tag first (works on both listing and product pages),
    # then fall back to visible span elements.
    price_str = 'N/A'
    price_elem = soup.find('meta', {'itemprop': 'price'})
    if price_elem and price_elem.get('content'):
        try:
            price_str = f"${float(price_elem['content']):.2f}"
        except ValueError:
            pass

    if price_str == 'N/A':
        for css in [
            'span.special-price span.price',
            'span.price-wrapper span.price',
            'span.price',
            '.product-price .price',
            '.special-price',
            '.price',
        ]:
            pe = soup.select_one(css)
            if pe:
                candidate = pe.get_text(strip=True)
                if '$' in candidate or candidate.replace('.','').isdigit():
                    price_str = candidate
                    break

    # Product URL: prefer the listing link href (direct product page),
    # then canonical tag, then fall back to the DotD page URL itself.
    product_url = page_url
    listing_link = soup.select_one('a.product-item-link')
    if listing_link and listing_link.get('href'):
        product_url = listing_link['href']
    else:
        canonical = soup.find('link', {'rel': 'canonical'})
        if canonical and canonical.get('href'):
            product_url = canonical['href']

    # Image
    image_url = ''
    img = soup.find('img', {'itemprop': 'image'}) or soup.find('img', {'class': 'product-image-photo'})
    if img:
        image_url = img.get('src', '')

    print(f"  Found DotD: '{name}' at {price_str}")
    return {
        'name':       name,
        'price_str':  price_str,
        'url':        product_url,
        'image_url':  image_url,
    }


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# FULL RESEARCH PIPELINE
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def research_dotd(dotd: Dict) -> Optional[Dict]:
    """
    Run the full BGG research pipeline for a GameNerdz DotD item.
    Returns a deal dict ready for emailer.send_deal_alert(), or None.
    """
    raw_name = dotd['name']

    # Clean up the name (strip edition suffixes, etc.)
    game_name = extract_game_name(raw_name) or raw_name
    print(f"  Game name (cleaned): '{game_name}'")

    # Step 1: BGG lookup
    print(f"  Looking up '{game_name}' on BGG...")
    bgg_id = bgg_api.search_game(game_name)
    game_details = None

    if bgg_id:
        print(f"  BGG ID: {bgg_id}")
        time.sleep(1)
        game_details = bgg_api.get_game_details(bgg_id)
        if game_details:
            print(f"  Details: '{game_details['name']}' | "
                  f"Rating: {game_details['average_rating']} | "
                  f"Weight: {game_details['weight']} | "
                  f"Best at: {game_details['best_players']}p")
    else:
        print(f"  Not found on BGG. Will include with limited info.")

    # Step 2: BGG Marketplace sold listings
    sold_listings = []
    if bgg_id:
        print(f"  Fetching BGG marketplace sold listings (USA)...")
        time.sleep(1)
        sold_listings = marketplace.get_sold_listings(bgg_id, num_listings=5)
        print(f"  Found {len(sold_listings)} sold listing(s)")

    # Step 3: Retail prices from other stores
    name_for_search = (game_details or {}).get('name', game_name)
    print(f"  Checking retail prices for '{name_for_search}'...")
    retail_prices = []
    try:
        retail_prices = price_checker.get_all_prices(name_for_search, bgg_id or '')
        if retail_prices:
            print(f"  Found {len(retail_prices)} price(s). "
                  f"Cheapest: {retail_prices[0]['store']} @ {retail_prices[0]['price_str']}")
        else:
            print(f"  No retail prices found")
    except Exception as e:
        print(f"  Price check error: {e}")

    # Step 4: BGG reviews
    reviews = {'positive': [], 'negative': []}
    if bgg_id:
        print(f"  Fetching community reviews...")
        time.sleep(1)
        try:
            reviews = bgg_api.get_game_reviews(bgg_id)
            print(f"  Reviews: {len(reviews.get('positive', []))} positive, "
                  f"{len(reviews.get('negative', []))} negative")
        except Exception as e:
            print(f"  Reviews error: {e}")

    # Build the thread-like dict so emailer can use the same template
    thread = {
        'id':       '',           # no BGG thread ID for DotD
        'deal_url': dotd['url'],  # actual GameNerdz product page
        'subject':  f"GameNerdz Deal of the Day: {raw_name} â {dotd['price_str']}",
        'author':   'GameNerdz',
        'post_date': '',
    }

    return dict(
        thread        = thread,
        game_details  = game_details,
        sold_listings = sold_listings,
        retail_prices = retail_prices,
        reviews       = reviews,
        dotd_price    = dotd['price_str'],
        dotd_url      = dotd['url'],
    )


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# MAIN CHECK FUNCTION
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def check_gamenerdz_dotd(force: bool = False) -> None:
    """
    Check GameNerdz for today's Deal of the Day and send an alert.

    force=True skips the already-sent-today guard (used in --test mode).
    """
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"\n{'='*60}")
    print(f"  GameNerdz DotD check @ {now_str}")
    print(f"{'='*60}")

    if not force and _already_sent_today():
        print("  Already sent DotD alert today. Skipping.")
        return

    dotd = fetch_dotd()
    if not dotd:
        print("  No DotD found â may not be posted yet or page changed.")
        return

    deal = research_dotd(dotd)
    if not deal:
        print("  Research pipeline returned nothing. Skipping.")
        return

    print(f"\n  Sending GameNerdz DotD alert for '{dotd['name']}'...")
    sent = emailer.send_consolidated_alert([deal])
    print(f"  Sending WhatsApp alert...")
    whatsapp_notifier.send_deal_whatsapp([deal])

    if sent and not force:
        _mark_sent_today()


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# ENTRY POINT
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

if __name__ == '__main__':

    # --test: run once right now, bypass dedup, and exit
    if '--test' in sys.argv:
        print("\nTEST MODE â checking GameNerdz DotD right now...\n")
        check_gamenerdz_dotd(force=True)
        sys.exit(0)

    # --once: run once right now (respects already-sent-today guard) and exit
    if '--once' in sys.argv:
        print("\nONCE MODE â checking GameNerdz DotD (respecting dedup)...\n")
        check_gamenerdz_dotd(force=False)
        sys.exit(0)

    print("""
+----------------------------------------------------------+
|       GameNerdz Deal of the Day Monitor â Starting      |
+----------------------------------------------------------+
""")
    print("  Will check at 1:05 PM ET every day.")
    print(f"  Alerts will be sent to: {config.ALERT_EMAIL}")
    print("  Press Ctrl+C to stop.\n")

    # Run once immediately on startup (catches case where it's already past 1pm
    # and the deal is up but we haven't checked yet today)
    check_gamenerdz_dotd()

    # Schedule daily at 1:05pm ET
    # We convert to local time: schedule library uses local clock.
    # If your computer is set to ET this is simply "13:05".
    # If your clock is in a different timezone, adjust accordingly.
    schedule.every().day.at("13:05").do(check_gamenerdz_dotd)
    print("  Scheduled for 1:05 PM (local time) daily. Waiting...\n")

    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except KeyboardInterrupt:
            print("\n\n  GameNerdz DotD monitor stopped by user.")
            break
        except Exception as e:
            print(f"  Unexpected error: {e}")
            traceback.print_exc()
            time.sleep(60)
