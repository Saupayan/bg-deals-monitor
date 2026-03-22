"""
facebook_monitor.py
-------------------
Scrapes Facebook board game deal groups for selling posts.

For each new post that contains board game prices:
  1. Parse game names + prices from freeform text
  2. Look up each game on BGG — skip if rating < 7.0 or not found
  3. For qualifying games: retail prices + BGG Marketplace sold/current listings
  4. Send a WhatsApp card with price comparison and verdict

Modes:
  run_fb_monitor_once()  -- periodic check (only new posts since last run)
  run_fb_force_mode()    -- WhatsApp --force (recent qualifying deals, ignore seen state)

State:
  seen_fb_posts.json tracks processed post IDs (persisted via GitHub Actions cache).

Credentials:
  FB_EMAIL and FB_PASSWORD environment variables (GitHub Secrets).
"""

import hashlib
import json
import os
import re
import time
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import bgg_api
import marketplace
import price_checker
import whatsapp_notifier


# ─── Config ───────────────────────────────────────────────────────────────────

FB_GROUPS = [
    "https://www.facebook.com/groups/boardgameexchange",
]

BGG_RATING_MIN = 7.0       # Only alert on games rated this or higher
MAX_POSTS_PER_GROUP = 25   # Posts to scrape per periodic run
MAX_POSTS_FORCE = 40       # Posts to consider for --force mode

SEEN_FB_POSTS_FILE = Path(__file__).parent / "seen_fb_posts.json"

# Matches: $35, $52.50, $1,200
PRICE_RE = re.compile(r'\$(\d[\d,]*\.?\d*)')

# Lines that are expansion/component details (start with dash/bullet/asterisk)
DETAIL_LINE_RE = re.compile(r'^\s*[-\u2022*]')


# ─── State management ─────────────────────────────────────────────────────────

def _load_seen() -> Set[str]:
    if SEEN_FB_POSTS_FILE.exists():
        try:
            return set(json.loads(SEEN_FB_POSTS_FILE.read_text()))
        except Exception:
            pass
    return set()


def _save_seen(seen: Set[str]) -> None:
    SEEN_FB_POSTS_FILE.write_text(json.dumps(sorted(seen), indent=2))


# ─── Post text parsing ────────────────────────────────────────────────────────

def parse_post_games(text: str) -> List[Tuple[str, Optional[float]]]:
    """
    Extract (game_name, price) tuples from a freeform Facebook selling post.

    Strategy:
      - Only process lines that contain a price ($XX)
      - Skip lines starting with - / bullet / * (expansion/component bullet points)
      - Game name = everything before the first '(' or '$' on the line
      - Deduplicate by name (case-insensitive)

    Example input line:  "Dragon Castle (Played 1x $45)"
    Extracted:           ("Dragon Castle", 45.0)
    """
    results = []
    seen_names: Set[str] = set()

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if DETAIL_LINE_RE.match(stripped):
            continue  # expansion/component bullet — skip

        price_m = PRICE_RE.search(stripped)
        if not price_m:
            continue  # no price on this line

        price = float(price_m.group(1).replace(',', ''))

        # Game name: text before first '(' or '$'
        name_m = re.match(r'^(.+?)(?:\s*\(|\s*\$)', stripped)
        if not name_m:
            continue
        name = name_m.group(1).strip()

        # Clean trailing artifacts: quantity markers (x2, X2), separator chars
        name = re.sub(r'\s+[Xx]\d+$', '', name).strip()
        name = re.sub(r'[\s\u2013\-:]+$', '', name).strip()

        if len(name) < 3:
            continue
        key = name.lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        results.append((name, price))

    return results


def _post_uid(post: dict) -> str:
    """Return a stable identifier for a post (numeric ID or content hash)."""
    return post.get('id', 'unknown')


# ─── Price helpers (mirrors monitor.py logic) ─────────────────────────────────

def _parse_price_float(price_str: str) -> Optional[float]:
    if not price_str:
        return None
    m = re.search(r'[\d,]+\.?\d*', price_str.replace(',', ''))
    if m:
        try:
            return float(m.group())
        except ValueError:
            pass
    return None


def _deal_verdict(deal_price: Optional[float],
                  retail_prices: list,
                  sold_listings: list,
                  current_listings: list) -> str:
    """Return a short emoji + label comparing deal_price to available price data."""
    if deal_price is None:
        return "❓ No price listed"

    in_stock = [p for p in retail_prices if p.get('in_stock')]
    if not in_stock:
        in_stock = retail_prices  # fall back to any retail entry

    if not in_stock:
        # No retail data — try cheapest current BGG listing
        cur_floats = [_parse_price_float(s['price']) for s in current_listings]
        cur_floats = [p for p in cur_floats if p]
        if cur_floats:
            cheapest = min(cur_floats)
            ratio = deal_price / cheapest
            if ratio <= 0.70:
                return f"🔥 Steal — {int((1-ratio)*100)}% below cheapest BGG listing"
            if ratio <= 0.85:
                return f"✅ Good — {int((1-ratio)*100)}% below cheapest BGG listing"
            if ratio <= 1.15:
                return f"😐 Fair — near cheapest BGG listing (${cheapest:.2f})"
            return f"🤔 Pricey — others listing at ${cheapest:.2f} on BGG"

        # Last resort: average BGG sold price
        sold_floats = [_parse_price_float(s['price']) for s in sold_listings]
        sold_floats = [p for p in sold_floats if p]
        if not sold_floats:
            return "❓ No price data to compare"
        avg = sum(sold_floats) / len(sold_floats)
        ratio = deal_price / avg
        if ratio <= 0.70:
            return f"🔥 Steal — {int((1-ratio)*100)}% below avg BGG sold"
        if ratio <= 0.85:
            return f"✅ Good — {int((1-ratio)*100)}% below avg BGG sold"
        if ratio <= 1.15:
            return f"😐 Fair — near avg BGG sold (${avg:.2f})"
        return f"🤔 Pricey — above avg BGG sold (${avg:.2f})"

    cheapest_retail = in_stock[0]['price_usd']
    ratio = deal_price / cheapest_retail
    if ratio <= 0.70:
        return f"🔥 Steal — {int((1-ratio)*100)}% below retail"
    if ratio <= 0.85:
        return f"✅ Good — {int((1-ratio)*100)}% below retail"
    if ratio <= 1.15:
        return f"😐 Fair — near retail"
    return f"🤔 Pricey — cheapest online is ${cheapest_retail:.2f}"


def _format_game_card(game_name: str, rating: float, deal_price: Optional[float],
                      retail_prices: list, sold_listings: list,
                      current_listings: list) -> List[str]:
    """Return a list of WhatsApp-formatted lines for one qualifying game."""
    lines = [f"🟢 *{game_name}* — ⭐ {rating:.1f}/10"]

    if deal_price is not None:
        lines.append(f"  💰 Asking: ${deal_price:.2f}")

    # Retail prices
    in_stock = [p for p in retail_prices if p.get('in_stock')]
    if in_stock:
        lo, hi = in_stock[0]['price_usd'], in_stock[-1]['price_usd']
        store = in_stock[0]['store']
        if lo == hi:
            lines.append(f"  🏪 Retail: ${lo:.2f} ({store})")
        else:
            lines.append(f"  🏪 Retail: ${lo:.2f}–${hi:.2f} (cheapest: {store})")
    elif retail_prices:
        lines.append(f"  🏪 Retail: out of stock everywhere")
    else:
        lines.append(f"  🏪 Retail: no data")

    # BGG current listings
    cur_floats = [_parse_price_float(s['price']) for s in current_listings]
    cur_floats = [p for p in cur_floats if p]
    if cur_floats:
        lo_c, hi_c = min(cur_floats), max(cur_floats)
        n = len(cur_floats)
        label = f"${lo_c:.2f}" if lo_c == hi_c else f"${lo_c:.2f}–${hi_c:.2f}"
        lines.append(f"  🏷️ BGG listed: {label} ({n} cop{'ies' if n != 1 else 'y'})")
    else:
        lines.append(f"  🏷️ BGG listed: none")

    # BGG sold listings
    sold_floats = [_parse_price_float(s['price']) for s in sold_listings]
    sold_floats = [p for p in sold_floats if p]
    if sold_floats:
        lo_s, hi_s = min(sold_floats), max(sold_floats)
        n = len(sold_floats)
        label = f"${lo_s:.2f}" if lo_s == hi_s else f"${lo_s:.2f}–${hi_s:.2f}"
        lines.append(f"  📦 BGG sold: {label} ({n} sale{'s' if n != 1 else ''})")
    else:
        lines.append(f"  📦 BGG sold: no recent data")

    verdict = _deal_verdict(deal_price, retail_prices, sold_listings, current_listings)
    lines.append(f"  {verdict}")

    return lines


# ─── Facebook scraping via Playwright ────────────────────────────────────────

def _make_session() -> "requests.Session":
    """
    Create an authenticated requests Session using browser cookies from the
    FB_COOKIES environment variable (JSON array exported from Cookie-Editor).
    Falls back to email/password login if FB_COOKIES is not set.
    """
    import requests as _req
    s = _req.Session()
    # mbasic.facebook.com requires a basic/mobile UA — desktop Chrome triggers
    # a "browser not supported" wall on group pages even when authenticated.
    # An old Android Firefox UA gets plain HTML group content without issues.
    # Use an older Android WebKit UA — mbasic.facebook.com serves plain HTML
    # to older mobile browsers. Desktop or modern UAs trigger a browser-wall page.
    s.headers.update({
        'User-Agent': (
            'Mozilla/5.0 (Linux; U; Android 4.0.3; en-us; KFTT Build/IML74K) '
            'AppleWebKit/535.19 (KHTML, like Gecko) Silk/3.13 Safari/535.19 '
            'Silk-Accelerated=true'
        ),
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    })

    cookies_json = os.getenv('FB_COOKIES', '')
    if cookies_json:
        try:
            cookies = json.loads(cookies_json)
            imported = 0
            for c in cookies:
                name = c.get('name') or c.get('Name')
                value = c.get('value') or c.get('Value', '')
                # Always use .facebook.com domain so cookies apply to ALL
                # facebook subdomains including mbasic.facebook.com
                domain = '.facebook.com'
                if name and value:
                    s.cookies.set(name, value, domain=domain)
                    imported += 1
            print(f"  FB: Loaded {imported} cookies from FB_COOKIES secret.")
        except Exception as e:
            print(f"  FB: Failed to parse FB_COOKIES — {e}")

    return s


def _fb_login_requests(session, email: str, password: str) -> bool:
    """
    Verify the session is authenticated by checking mbasic.facebook.com.
    If FB_COOKIES was loaded, we should already be logged in.
    Falls back to form-based login using email/password if needed.
    Returns True if authenticated.
    """
    from bs4 import BeautifulSoup

    try:
        print("  FB: Checking authentication on mbasic.facebook.com ...")
        r = session.get('https://mbasic.facebook.com', timeout=30)
        r.raise_for_status()
        print(f"  FB: Status {r.status_code}, URL: {r.url}")

        soup = BeautifulSoup(r.text, 'html.parser')
        page_text = soup.get_text()
        print(f"  FB: Home page snippet: {page_text[:400]!r}")

        # Detect browser-wall pages — these return 200 but have NO real content.
        # They fool a naive "no login form = authenticated" check.
        browser_wall_phrases = [
            'not available on this browser',
            'get one of the browsers below',
            'update your browser',
            'browser is not supported',
        ]
        if any(p in page_text.lower() for p in browser_wall_phrases):
            print("  FB: Got a browser-wall page — UA not accepted by mbasic. Aborting.")
            return False

        # If we're not on a login page, cookies worked
        if 'login' not in r.url and 'checkpoint' not in r.url:
            form = soup.find('form', id='login_form')
            if not form:
                print("  FB: Already authenticated via cookies.")
                return True

        # Cookie auth didn't work — fall back to form login
        print("  FB: Cookie auth failed or not set — attempting form login ...")
        if not email or not password:
            print("  FB: No email/password to fall back to.")
            return False

        soup = BeautifulSoup(r.text, 'html.parser')
        form = soup.find('form', id='login_form') or soup.find('form')
        if not form:
            print(f"  FB: No login form found. Page: {soup.get_text()[:200]!r}")
            return False

        action = form.get('action', '/login/device-based/regular/login/')
        if action.startswith('/'):
            action = 'https://mbasic.facebook.com' + action

        data: Dict[str, str] = {}
        for inp in form.find_all('input'):
            name = inp.get('name')
            if name:
                data[name] = inp.get('value', '')
        data['email'] = email
        data['pass']  = password

        print(f"  FB: POSTing credentials to {action} ...")
        r2 = session.post(action, data=data, timeout=30)
        print(f"  FB: Post-login status {r2.status_code}, URL: {r2.url}")

        if 'login' in r2.url or 'checkpoint' in r2.url:
            soup2 = BeautifulSoup(r2.text, 'html.parser')
            print(f"  FB: Still on login/checkpoint: {soup2.get_text()[:200]!r}")
            return False

        print("  FB: Form login successful.")
        return True

    except Exception as e:
        print(f"  FB: Auth error — {e}")
        traceback.print_exc()
        return False


def _scrape_group_requests(session, group_url: str, max_posts: int) -> List[Dict]:
    """
    Scrape a Facebook group's posts using the mbasic site via HTTP requests.
    Returns list of {id, text, url, group_url}.
    """
    from bs4 import BeautifulSoup

    posts: List[Dict] = []
    mbasic_url = group_url.replace('www.facebook.com', 'mbasic.facebook.com')

    try:
        pages_fetched = 0
        next_url: Optional[str] = mbasic_url

        while next_url and pages_fetched < 3 and len(posts) < max_posts:
            print(f"  FB: Fetching {next_url[:80]} ...")
            r = session.get(next_url, timeout=30)
            r.raise_for_status()
            print(f"  FB: Status {r.status_code}, final URL: {r.url}")

            if 'login' in r.url or 'checkpoint' in r.url:
                print("  FB: Redirected to login/checkpoint — session expired.")
                break

            soup = BeautifulSoup(r.text, 'html.parser')

            # Each post on mbasic groups is a <div> that contains a story link.
            # story_fbid appears in links like /?story_fbid=...&id=...
            # We collect the outermost divs that wrap each story.
            story_divs = []

            # Primary: divs containing story_fbid links (each is one post)
            for a in soup.find_all('a', href=re.compile(r'story_fbid')):
                parent = a.find_parent('div')
                if parent and parent not in story_divs:
                    story_divs.append(parent)

            # Fallback: divs with data-ft attribute
            if not story_divs:
                story_divs = soup.find_all('div', attrs={'data-ft': True})

            print(f"  FB: Page {pages_fetched+1}: found {len(story_divs)} story divs.")

            if not story_divs and pages_fetched == 0:
                text_sample = soup.get_text()[:800]
                print(f"  FB: Page text snippet: {text_sample!r}")
                print(f"  FB: Page URL after redirect: {r.url}")
                break

            for div in story_divs:
                if len(posts) >= max_posts:
                    break
                try:
                    text = div.get_text(separator='\n').strip()
                    if not text or len(text) < 30:
                        continue

                    # Extract post ID from story_fbid link
                    post_id = None
                    for a in div.find_all('a', href=True):
                        href = a['href']
                        m = re.search(r'story_fbid[=%]3D?(\d+)', href)
                        if not m:
                            m = re.search(r'/posts/(\d+)', href)
                        if m:
                            post_id = m.group(1)
                            break

                    if not post_id:
                        post_id = 'h_' + hashlib.md5(text[:300].encode()).hexdigest()[:10]

                    post_url = (f"{group_url.rstrip('/')}/posts/{post_id}"
                                if not post_id.startswith('h_') else group_url)

                    posts.append({
                        'id': post_id,
                        'text': text,
                        'url': post_url,
                        'group_url': group_url,
                    })
                except Exception as e:
                    print(f"  FB: Error parsing story div: {e}")
                    continue

            pages_fetched += 1

            # Follow pagination link if we need more posts
            next_url = None
            if len(posts) < max_posts:
                for link_text in ['See More Posts', 'More Posts']:
                    a = soup.find('a', string=re.compile(link_text, re.I))
                    if a and a.get('href'):
                        href = a['href']
                        next_url = ('https://mbasic.facebook.com' + href
                                    if href.startswith('/') else href)
                        print(f"  FB: Following pagination to {next_url[:80]}...")
                        break

    except Exception as e:
        print(f"  FB: Error scraping group: {e}")
        traceback.print_exc()

    print(f"  FB: Collected {len(posts)} posts from {group_url}.")
    return posts


def _scrape_all_groups_playwright(cookies_json: str, max_posts: int) -> List[Dict]:
    """
    Use Playwright (real Chrome) with pre-loaded cookies to scrape FB groups.
    This is the primary approach — it bypasses UA issues and skips the login form
    entirely by injecting the user's real session cookies directly.
    """
    from playwright.sync_api import sync_playwright

    raw_cookies = json.loads(cookies_json)
    pw_cookies = []
    for c in raw_cookies:
        name = c.get('name') or c.get('Name')
        value = c.get('value') or c.get('Value', '')
        if name and value:
            pw_cookies.append({
                'name': name,
                'value': value,
                'domain': '.facebook.com',
                'path': '/',
            })
    print(f"  FB[PW]: Launching Playwright with {len(pw_cookies)} cookies ...")

    all_posts: List[Dict] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage',
                      '--disable-blink-features=AutomationControlled'],
            )
            context = browser.new_context(
                # Old Android WebKit UA — mbasic serves plain HTML directly
                # without redirecting to m.facebook.com.
                # Mobile Chrome and modern UAs cause mbasic → m.facebook.com
                # redirect which then shows "This browser isn't supported".
                user_agent=(
                    'Mozilla/5.0 (Linux; U; Android 2.3.3; en-us; '
                    'HTC_DesireS_S510e Build/GRI40) AppleWebKit/533.1 '
                    '(KHTML, like Gecko) Version/4.0 Mobile Safari/533.1'
                ),
                viewport={'width': 320, 'height': 480},
            )
            context.add_cookies(pw_cookies)

            for group_url in FB_GROUPS:
                mbasic_url = group_url.replace('www.facebook.com', 'mbasic.facebook.com')
                print(f"  FB[PW]: Navigating to {mbasic_url} ...")
                try:
                    page = context.new_page()
                    page.goto(mbasic_url, wait_until='domcontentloaded', timeout=30000)
                    final_url = page.url
                    print(f"  FB[PW]: Final URL: {final_url}")

                    if 'login' in final_url or 'checkpoint' in final_url:
                        print("  FB[PW]: Redirected to login — cookies may be expired.")
                        page.close()
                        break

                    content = page.content()
                    page.close()

                    from bs4 import BeautifulSoup as _BS
                    soup = _BS(content, 'html.parser')
                    page_text = soup.get_text()
                    print(f"  FB[PW]: Page snippet: {page_text[:600]!r}")

                    story_divs = []
                    for a in soup.find_all('a', href=re.compile(r'story_fbid')):
                        parent = a.find_parent('div')
                        if parent and parent not in story_divs:
                            story_divs.append(parent)
                    if not story_divs:
                        story_divs = soup.find_all('div', attrs={'data-ft': True})
                    print(f"  FB[PW]: Found {len(story_divs)} story divs.")

                    for div in story_divs[:max_posts]:
                        text = div.get_text(separator='\n').strip()
                        if not text or len(text) < 30:
                            continue
                        post_id = 'h_' + hashlib.md5(text[:300].encode()).hexdigest()[:10]
                        all_posts.append({
                            'id': post_id,
                            'text': text,
                            'url': group_url,
                            'group_url': group_url,
                        })

                except Exception as e:
                    print(f"  FB[PW]: Error on {group_url}: {e}")
                    traceback.print_exc()

            browser.close()

    except Exception as e:
        print(f"  FB[PW]: Playwright launch failed: {e}")
        traceback.print_exc()

    print(f"  FB[PW]: Total posts collected: {len(all_posts)}")
    return all_posts


def _scrape_all_groups(max_posts: int) -> List[Dict]:
    """
    Scrape all FB groups for selling posts.
    Primary path: Playwright + pre-loaded cookies (real browser, no login form).
    Fallback: requests + BeautifulSoup via mbasic (when Playwright unavailable).
    Returns a flat list of post dicts.
    """
    email = os.getenv('FB_EMAIL', '')
    password = os.getenv('FB_PASSWORD', '')
    if not email or not password:
        print("  FB: FB_EMAIL / FB_PASSWORD not set — skipping Facebook monitoring.")
        return []

    cookies_json = os.getenv('FB_COOKIES', '')

    # ── Primary: Playwright with pre-loaded cookies ──────────────────────────
    if cookies_json:
        try:
            posts = _scrape_all_groups_playwright(cookies_json, max_posts)
            if posts is not None:  # even empty list means it ran OK
                return posts
        except ImportError:
            print("  FB: Playwright not available — falling back to requests.")
        except Exception as e:
            print(f"  FB: Playwright approach error: {e} — falling back to requests.")

    # ── Fallback: requests + BeautifulSoup ──────────────────────────────────
    print("  FB: Using requests fallback ...")
    session = _make_session()

    if not _fb_login_requests(session, email, password):
        print("  FB: Login failed — aborting Facebook scrape.")
        return []

    all_posts: List[Dict] = []
    for group_url in FB_GROUPS:
        group_posts = _scrape_group_requests(session, group_url, max_posts=max_posts)
        all_posts.extend(group_posts)
        time.sleep(1)

    return all_posts


# ─── Per-game analysis ────────────────────────────────────────────────────────

def _analyse_game(game_name: str, deal_price: Optional[float]) -> Optional[Dict]:
    """
    BGG lookup + price research for one game.
    Returns a result dict, or None if the game is not found / below rating threshold.
    """
    print(f"    FB: Researching '{game_name}' ...")

    bgg_id = bgg_api.search_game(game_name)
    if not bgg_id:
        print(f"    FB: No BGG match for '{game_name}'")
        return None

    time.sleep(0.5)
    details = bgg_api.get_game_details(bgg_id)
    if not details:
        return None

    rating = details.get('average_rating', 0.0)
    name = details.get('name', game_name)

    if rating < BGG_RATING_MIN:
        print(f"    FB: '{name}' BGG rating {rating:.2f} < {BGG_RATING_MIN} — skipping")
        return None

    print(f"    FB: '{name}' qualifies ({rating:.2f}/10) — fetching prices ...")

    retail_prices, sold_listings, current_listings = [], [], []
    try:
        retail_prices = price_checker.get_all_prices(game_name, '') or []
    except Exception as e:
        print(f"    FB: Retail price error: {e}")
    try:
        sold_listings = marketplace.get_sold_listings(bgg_id, num_listings=5) or []
        time.sleep(0.5)
        current_listings = marketplace.get_current_listings(bgg_id, num_listings=10) or []
    except Exception as e:
        print(f"    FB: Marketplace error: {e}")

    return {
        'name': name,
        'bgg_id': bgg_id,
        'rating': rating,
        'deal_price': deal_price,
        'retail_prices': retail_prices,
        'sold_listings': sold_listings,
        'current_listings': current_listings,
    }


def _analyse_post(post: Dict) -> Optional[Dict]:
    """
    Parse a post's text, find qualifying games (BGG >= 7.0), fetch price data.
    Returns {post_id, post_url, group_url, qualified_games} or None.
    """
    games = parse_post_games(post['text'])
    if not games:
        return None

    qualified = []
    for game_name, deal_price in games:
        try:
            result = _analyse_game(game_name, deal_price)
            if result:
                qualified.append(result)
        except Exception as e:
            print(f"    FB: Error analysing '{game_name}': {e}")
            traceback.print_exc()
        time.sleep(1)

    if not qualified:
        return None

    return {
        'post_id': post['id'],
        'post_url': post['url'],
        'group_url': post.get('group_url', ''),
        'qualified_games': qualified,
    }


# ─── WhatsApp message formatting ──────────────────────────────────────────────

def _group_display_name(group_url: str) -> str:
    """Turn a Facebook group URL into a short display name."""
    slug = group_url.rstrip('/').split('/')[-1]
    # e.g. "boardgameexchange" -> "Board Game Exchange"
    slug = re.sub(r'([a-z])([A-Z])', r'\1 \2', slug)
    return slug.replace('-', ' ').replace('_', ' ').title()


def _format_post_message(post_result: Dict) -> str:
    """Format a complete WhatsApp message for one FB post."""
    group_name = _group_display_name(post_result.get('group_url', ''))
    n = len(post_result['qualified_games'])
    lines = [
        f"🔵 *Facebook: {group_name}*",
        f"_{n} qualifying game{'s' if n != 1 else ''}_",
        "",
    ]

    for game in post_result['qualified_games']:
        lines.extend(_format_game_card(
            game['name'], game['rating'], game['deal_price'],
            game['retail_prices'], game['sold_listings'], game['current_listings'],
        ))
        lines.append("")

    if post_result.get('post_url'):
        lines.append(f"🔗 {post_result['post_url']}")

    return "\n".join(lines)


# ─── Public run modes ─────────────────────────────────────────────────────────

def run_fb_monitor_once() -> None:
    """
    Periodic monitoring mode (--fb-once).

    Scrapes all configured Facebook groups, processes only posts not yet seen,
    and sends a WhatsApp message for each post that contains qualifying games
    (BGG average rating >= BGG_RATING_MIN).

    Saves processed post IDs to seen_fb_posts.json so they are not re-alerted.
    """
    print("\n=== FACEBOOK MONITOR (periodic) ===\n")

    seen = _load_seen()
    posts = _scrape_all_groups(max_posts=MAX_POSTS_PER_GROUP)

    if not posts:
        print("  FB: No posts scraped — nothing to do.")
        return

    new_posts = [p for p in posts if _post_uid(p) not in seen]
    print(f"  FB: {len(posts)} total posts, {len(new_posts)} new since last check.")

    for post in new_posts:
        pid = _post_uid(post)
        # Mark seen immediately so a crash mid-run doesn't cause double-alerting
        seen.add(pid)
        _save_seen(seen)

        try:
            result = _analyse_post(post)
            if result:
                msg = _format_post_message(result)
                print(f"\n  FB: Sending alert for post {pid} ...")
                print(msg)
                whatsapp_notifier.send_whatsapp(msg)
                time.sleep(2)
            else:
                print(f"  FB: Post {pid} — no qualifying games, skipping.")
        except Exception as e:
            print(f"  FB: Error processing post {pid}: {e}")
            traceback.print_exc()


def run_fb_force_mode() -> None:
    """
    Force/manual mode (triggered by WhatsApp --force command).

    Scrapes recent posts from all configured Facebook groups and analyses ALL of
    them for qualifying games, regardless of seen_fb_posts.json state.
    Sends one WhatsApp message per qualifying post.
    If nothing qualifies, sends a brief summary message.
    """
    print("\n=== FACEBOOK FORCE MODE ===\n")

    posts = _scrape_all_groups(max_posts=MAX_POSTS_FORCE)

    if not posts:
        whatsapp_notifier.send_whatsapp(
            "🔵 *Facebook Deals*\n\n"
            "Could not fetch posts — login may have failed or Playwright is not available."
        )
        return

    found_any = False
    for post in posts:
        try:
            result = _analyse_post(post)
            if result:
                msg = _format_post_message(result)
                print(f"\n  FB: Sending WhatsApp for post {post['id']} ...")
                print(msg)
                whatsapp_notifier.send_whatsapp(msg)
                found_any = True
                time.sleep(2)
        except Exception as e:
            print(f"  FB: Error on post {post['id']}: {e}")
            traceback.print_exc()

    if not found_any:
        whatsapp_notifier.send_whatsapp(
            f"🔵 *Facebook Deals*\n\n"
            f"No qualifying games (BGG \u2265 {BGG_RATING_MIN}) found "
            f"in the {len(posts)} most recent posts."
        )
