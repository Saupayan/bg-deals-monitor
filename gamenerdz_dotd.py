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

def fetch_dotd(use_playwright: bool = True) -> Optional[Dict]:
    """
    Fetch the GameNerdz Deal of the Day.
    Returns a dict with keys: name, price_str, url, image_url
    or None if the product couldn't be found.

    Strategy order:
      0. Playwright headless Chromium â renders the full JS page (use_playwright=True only)
      1. Magento 2 GraphQL â fast structured API (requires server to be public)
      2â5. HTML fallbacks via _parse_dotd_page (JSON-LD, x-magento-init, CSS, h1)

    use_playwright=False is used by monitor.py (15-min loop) since Playwright is
    only installed in the dedicated gamenerdz-dotd.yml daily workflow, not
    in bgg-monitor.yml.
    """
    print(f"\n  Fetching GameNerdz Deal of the Day from {GAMENERDZ_DOTD_URL} ...")

    # ââ Strategy 0: Playwright headless Chromium ââââââââââââââââââââââââââââââ
    # Full browser rendering â the only strategy that can execute Magento 2's
    # JavaScript and access the rendered product listing DOM.
    # Only used when the Playwright workflow calls this function.
    if use_playwright:
        result = _fetch_dotd_via_playwright(GAMENERDZ_DOTD_URL)
        if result:
            return result

    # ââ Strategy 1: Magento 2 GraphQL ââââââââââââââââââââââââââââââââââââââââ
    # GameNerdz runs Magento 2, which may expose /graphql for storefront queries.
    # Returns 401 on gamenerdz.com (auth required), kept as fallback in case
    # they change their configuration.
    result = _fetch_dotd_via_graphql(GAMENERDZ_DOTD_URL)
    if result:
        return result

    # ââ Fallback: HTML scraping strategies (JSON-LD, x-magento-init, CSSâ¦) ââ
    try:
        resp = requests.get(GAMENERDZ_DOTD_URL, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            print(f"  GameNerdz returned HTTP {resp.status_code}")
            return None

        soup = BeautifulSoup(resp.text, 'lxml')
        deal = _parse_dotd_page(soup, resp.url)
        if deal:
            return deal

        print("  Could not parse DotD â page structure may have changed.")
        return None

    except Exception as e:
        print(f"  Error fetching GameNerdz DotD: {e}")
        traceback.print_exc()
        return None


def _fetch_dotd_via_playwright(dotd_url: str) -> Optional[Dict]:
    """
    Use Playwright headless Chromium to render the GameNerdz DotD page.

    GameNerdz uses a fully JS-rendered Magento 2 storefront â product listings
    are NOT in the initial server HTML and cannot be fetched with requests.get().
    Playwright launches a real headless Chromium browser, executes the page JS,
    waits for products to appear in the DOM, then extracts the HTML for parsing.

    Requires: playwright>=1.42.0 installed + `playwright install chromium --with-deps`
    Falls back gracefully if Playwright is not installed.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("  DEBUG: Playwright not installed â skipping headless browser strategy")
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/122.0.0.0 Safari/537.36'
                ),
                viewport={'width': 1280, 'height': 800},
            )
            page = context.new_page()

            print(f"  DEBUG: Playwright navigating to {dotd_url} ...")
            page.goto(dotd_url, wait_until='domcontentloaded', timeout=30000)

            # Wait for Magento 2 product items to appear in the rendered DOM.
            # .product-item and .product-item-info are standard Magento 2 selectors.
            try:
                page.wait_for_selector(
                    '.product-item, .product-item-info, .product-item-name, [data-product-id]',
                    timeout=15000,
                )
                print("  DEBUG: Playwright: product selector found in DOM")
            except PWTimeout:
                print("  DEBUG: Playwright: no product selector within 15s â parsing anyway")

            rendered_html = page.content()
            final_url = page.url

            # Take a viewport screenshot before closing.
            # Saved to /tmp/gn_dotd.png so check_gamenerdz_dotd() can send it
            # via WhatsApp regardless of whether the product name could be parsed.
            try:
                page.screenshot(path='/tmp/gn_dotd.png', full_page=False)
                print("  DEBUG: Playwright screenshot saved to /tmp/gn_dotd.png")
            except Exception as _ss_err:
                print(f"  DEBUG: Screenshot failed (non-fatal): {_ss_err}")

            browser.close()

        print(f"  DEBUG: Playwright rendered {len(rendered_html)} chars")

        # Feed the fully-rendered HTML into the existing multi-strategy parser.
        # With real JS execution, Strategy 3 (CSS selectors) should now succeed.
        soup = BeautifulSoup(rendered_html, 'lxml')
        deal = _parse_dotd_page(soup, final_url)
        if deal:
            print(f"  Found DotD via Playwright: '{deal['name']}' at {deal['price_str']}")
        return deal

    except Exception as e:
        print(f"  DEBUG: Playwright error: {e}")
        traceback.print_exc()
        return None


def _fetch_dotd_via_graphql(dotd_url: str) -> Optional[Dict]:
    """
    Query the Magento 2 GraphQL endpoint for the Deal of the Day product.

    Magento 2 exposes /graphql as a public storefront API â no auth required
    for catalog/category queries.  The category URL key is derived from the
    last path segment of the DotD URL (e.g. 'deal-of-the-day').
    """
    import json as _json

    url_key = dotd_url.rstrip('/').split('/')[-1]   # "deal-of-the-day"

    query = """{
  categoryList(filters: {url_key: {eq: "%s"}}) {
    id
    name
    products {
      items {
        name
        price_range {
          minimum_price {
            final_price { value currency }
          }
        }
        url_key
        url_rewrites { url }
        small_image { url }
      }
    }
  }
}""" % url_key

    try:
        resp = requests.post(
            "https://www.gamenerdz.com/graphql",
            json={"query": query},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=15,
        )
        print(f"  DEBUG: GraphQL HTTP {resp.status_code}")

        if resp.status_code != 200:
            print(f"  DEBUG: GraphQL unavailable (HTTP {resp.status_code})")
            return None

        data = resp.json()

        if 'errors' in data:
            print(f"  DEBUG: GraphQL errors: {data['errors'][:1]}")
            return None

        categories = data.get('data', {}).get('categoryList', [])
        print(f"  DEBUG: GraphQL categories found: {len(categories)}")

        if not categories:
            return None

        products = categories[0].get('products', {}).get('items', [])
        print(f"  DEBUG: GraphQL products in DotD category: {len(products)}")

        if not products:
            return None

        product = products[0]
        name = product.get('name', '').strip()

        if not name or len(name) <= 5 or 'deal of the day' in name.lower():
            print(f"  DEBUG: GraphQL product name unusable: '{name}'")
            return None

        # Price
        price_val = (product.get('price_range', {})
                     .get('minimum_price', {})
                     .get('final_price', {})
                     .get('value'))
        price_str = f"${float(price_val):.2f}" if price_val else 'N/A'

        # Product URL â prefer url_rewrites (full path) over bare url_key
        rewrites = product.get('url_rewrites') or []
        if rewrites:
            rewrite_path = rewrites[0].get('url', '')
            product_url = (f"https://www.gamenerdz.com/{rewrite_path}"
                           if rewrite_path else dotd_url)
        else:
            url_k = product.get('url_key', '')
            product_url = (f"https://www.gamenerdz.com/{url_k}.html"
                           if url_k else dotd_url)

        # Image
        image_url = (product.get('small_image') or {}).get('url', '') or ''

        print(f"  Found DotD via GraphQL: '{name}' at {price_str}")
        return {
            'name':      name,
            'price_str': price_str,
            'url':       product_url,
            'image_url': image_url,
        }

    except Exception as e:
        print(f"  DEBUG: GraphQL error: {e}")
        return None


def _parse_dotd_page(soup: BeautifulSoup, page_url: str) -> Optional[Dict]:
    """
    Try multiple HTML patterns to extract the DotD product name and price.

    GameNerdz uses Magento 2 whose category pages are JavaScript-rendered,
    so the product listing HTML is NOT in the initial server response.
    However, Magento 2 always embeds Schema.org JSON-LD structured data
    server-side for SEO purposes â that IS present in the static HTML and
    is the most reliable extraction target.

    Fall-back chain:
      1. JSON-LD <script type="application/ld+json"> â Product or ItemList
      2. text/x-magento-init script tags (sometimes embed product config)
      3. Visible CSS selectors (only works if URL redirects to a product page)
      4. Any non-header h1
    """
    import json as _json

    name = None
    price_str = 'N/A'
    product_url = page_url
    image_url = ''

    # ââ Strategy 1: JSON-LD structured data (server-rendered, SEO-driven) ââââ
    for script in soup.find_all('script', {'type': 'application/ld+json'}):
        try:
            data = _json.loads(script.string or '')
        except Exception:
            continue

        # Handle both a single object and a list of objects
        items = data if isinstance(data, list) else [data]
        for item in items:
            # ItemList containing products (common on category pages)
            if item.get('@type') == 'ItemList':
                elements = item.get('itemListElement', [])
                if elements:
                    first = elements[0]
                    # element can be a ListItem wrapping a Product, or a Product
                    product = first.get('item', first)
                    candidate = product.get('name', '')
                    if candidate and len(candidate) > 5 and 'deal of the day' not in candidate.lower():
                        name = candidate
                        price_str = _price_from_jsonld(product)
                        product_url = product.get('url', page_url)
                        image_url = _image_from_jsonld(product)
                        break

            # Direct Product object (common on product detail pages)
            if not name and item.get('@type') == 'Product':
                candidate = item.get('name', '')
                if candidate and len(candidate) > 5 and 'deal of the day' not in candidate.lower():
                    name = candidate
                    price_str = _price_from_jsonld(item)
                    product_url = item.get('url', page_url)
                    image_url = _image_from_jsonld(item)
                    break

        if name:
            break

    # ââ Strategy 2: text/x-magento-init script tags âââââââââââââââââââââââ
    if not name:
        for script in soup.find_all('script', {'type': 'text/x-magento-init'}):
            raw = script.string or ''
            # Look for any "name" key that seems like a product title
            try:
                data = _json.loads(raw)
                candidate = _deep_find(data, 'name')
                if candidate and len(candidate) > 5 and 'deal of the day' not in candidate.lower():
                    name = candidate
                    break
            except Exception:
                pass

    # ââ Strategy 3: visible CSS selectors (works if URL is a product page) â
    if not name:
        for css in [
            'a.product-item-link',
            'strong.product-item-name',
            '.product-item-name a',
            '.product-item-name',
            '.product-name a',
            '.product-name',
            '.product-info-main h1',
            'span[itemprop="name"]',
            'h1.page-title',
        ]:
            elem = soup.select_one(css)
            if elem:
                candidate = elem.get_text(strip=True)
                if len(candidate) > 5 and 'deal of the day' not in candidate.lower():
                    name = candidate
                    break

    # ââ Strategy 4: any h1 that isn't the category page header ââââââââââââââ
    if not name:
        for elem in soup.find_all('h1'):
            candidate = elem.get_text(strip=True)
            if len(candidate) > 5 and 'deal of the day' not in candidate.lower():
                name = candidate
                break

    if not name:
        # Debug dump so future structure changes are diagnosable
        page_text = soup.get_text(separator=' ', strip=True)
        print(f"  DEBUG: page title tag = {soup.title.string if soup.title else 'N/A'}")
        print(f"  DEBUG: first 500 chars of visible text: {page_text[:500]}")
        all_h1 = [e.get_text(strip=True) for e in soup.find_all('h1')]
        print(f"  DEBUG: all h1 tags found: {all_h1}")
        ld_types = [_json.loads(s.string or '{}').get('@type', '?')
                    for s in soup.find_all('script', {'type': 'application/ld+json'})
                    if s.string]
        print(f"  DEBUG: JSON-LD @type values found: {ld_types}")
        return None

    # ââ Price fallback (if not already set from JSON-LD) âââââââââââââââââ
    if price_str == 'N/A':
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
                if '$' in candidate or candidate.replace('.', '').isdigit():
                    price_str = candidate
                    break

    # ââ Product URL fallback âââââââââââââââââââââââââââââââââââââââââââââââ
    if product_url == page_url:
        listing_link = soup.select_one('a.product-item-link')
        if listing_link and listing_link.get('href'):
            product_url = listing_link['href']
        else:
            canonical = soup.find('link', {'rel': 'canonical'})
            if canonical and canonical.get('href'):
                product_url = canonical['href']

    # ââ Image fallback âââââââââââââââââââââââââââââââââââââââââââââââââââââ
    if not image_url:
        img = (soup.find('img', {'itemprop': 'image'}) or
               soup.find('img', {'class': 'product-image-photo'}))
        if img:
            image_url = img.get('src', '')

    print(f"  Found DotD: '{name}' at {price_str}")
    return {
        'name':       name,
        'price_str':  price_str,
        'url':        product_url,
        'image_url':  image_url,
    }


def _price_from_jsonld(product: dict) -> str:
    """Extract a price string from a JSON-LD Product object."""
    import json as _json
    offers = product.get('offers', {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = offers.get('price') or offers.get('lowPrice')
    if price:
        try:
            return f"${float(price):.2f}"
        except (ValueError, TypeError):
            return str(price)
    return 'N/A'


def _image_from_jsonld(product: dict) -> str:
    """Extract an image URL from a JSON-LD Product object."""
    img = product.get('image', '')
    if isinstance(img, list):
        img = img[0] if img else ''
    if isinstance(img, dict):
        img = img.get('url', '')
    return str(img) if img else ''


def _deep_find(obj, key: str, depth: int = 0):
    """Recursively search a dict/list for a key whose value looks like a product name."""
    if depth > 5:
        return None
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], str) and len(obj[key]) > 5:
            return obj[key]
        for v in obj.values():
            result = _deep_find(v, key, depth + 1)
            if result:
                return result
    elif isinstance(obj, list):
        for item in obj[:5]:
            result = _deep_find(item, key, depth + 1)
            if result:
                return result
    return None


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

def check_gamenerdz_dotd(force: bool = False, use_playwright: bool = True) -> None:
    """
    Check GameNerdz for today's Deal of the Day and send an alert.

    force=True skips the already-sent-today guard (used in --test mode).
    use_playwright=False disables Playwright (used when called from monitor.py /
    bgg-monitor.yml which doesn't install Playwright).
    """
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"\n{'='*60}")
    print(f"  GameNerdz DotD check @ {now_str}")
    print(f"{'='*60}")

    if not force and _already_sent_today():
        print("  Already sent DotD alert today. Skipping.")
        return

    dotd = fetch_dotd(use_playwright=use_playwright)

    # ââ Screenshot fallback (Playwright workflow only) âââââââââââââââââââââââââ
    # If Playwright ran, it saves a screenshot to /tmp/gn_dotd.png regardless of
    # whether the product name could be parsed.  Send it so you always see the
    # page even when the HTML structure has changed and parsing failed.
    if use_playwright:
        import os as _os
        screenshot_path = '/tmp/gn_dotd.png'
        if _os.path.exists(screenshot_path):
            if dotd:
                ss_caption = (
                    f"ðª GameNerdz Deal of the Day: {dotd['name']} â {dotd['price_str']}\n"
                    f"ð {dotd.get('url', GAMENERDZ_DOTD_URL)}"
                )
            else:
                ss_caption = (
                    "ðª GameNerdz Deal of the Day\n"
                    "â ï¸ Couldn't parse product details â here's the live page screenshot.\n"
                    f"ð {GAMENERDZ_DOTD_URL}"
                )
            print("  Sending DotD screenshot via WhatsApp...")
            whatsapp_notifier.send_image_whatsapp(screenshot_path, ss_caption)
        else:
            print("  No screenshot file found at /tmp/gn_dotd.png â skipping image send")

    if not dotd:
        print("  No DotD found — may not be posted yet or page changed.")
        if not use_playwright:
            # Playwright isn't available in this workflow — send a thum.io
            # URL-based screenshot so you still see the live page on WhatsApp.
            thum_url = ("https://image.thum.io/get/noanimate/"
                        "https://www.gamenerdz.com/deal-of-the-day")
            print("  Sending thum.io screenshot fallback via WhatsApp...")
            whatsapp_notifier.send_image_whatsapp(
                thum_url,
                "🏪 GameNerdz Deal of the Day\n"
                "⚠️ Couldn't parse details — here's the live page.\n"
                f"🔗 {GAMENERDZ_DOTD_URL}"
            )
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
