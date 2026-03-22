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

def _fb_login(page, email: str, password: str) -> bool:
    """Log in to Facebook. Returns True on success."""
    try:
        print("  FB: Navigating to facebook.com ...")
        page.goto("https://www.facebook.com", wait_until="domcontentloaded", timeout=30_000)
        time.sleep(2)

        # Dismiss cookie/privacy consent banner if shown
        for sel in [
            '[data-cookiebanner="accept_button"]',
            'button[title="Allow all cookies"]',
            '[aria-label="Allow all cookies"]',
            'button:has-text("Allow all cookies")',
            'button:has-text("Accept all")',
        ]:
            try:
                page.click(sel, timeout=2_000)
                time.sleep(1)
                break
            except Exception:
                pass

        # Enter credentials
        page.fill('#email', email, timeout=10_000)
        page.fill('#pass', password, timeout=5_000)
        page.click('[name="login"]', timeout=5_000)

        # Wait until redirected away from login page
        page.wait_for_function(
            "() => !window.location.href.includes('/login')",
            timeout=25_000,
        )
        time.sleep(2)

        # Dismiss "Save login info?" / "Not Now" prompt if present
        for sel in ['[aria-label="Not Now"]', 'button:has-text("Not Now")']:
            try:
                page.click(sel, timeout=2_000)
                break
            except Exception:
                pass

        print("  FB: Login successful.")
        return True

    except Exception as e:
        print(f"  FB: Login failed — {e}")
        return False


def _scrape_group_posts(page, group_url: str, max_posts: int) -> List[Dict]:
    """
    Navigate to a Facebook group and extract text + ID from recent posts.
    Returns list of {id, text, url, group_url}.
    """
    posts = []
    try:
        print(f"  FB: Loading {group_url} ...")
        page.goto(group_url, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(3)

        # Scroll to trigger lazy-loaded posts
        for _ in range(3):
            page.keyboard.press("End")
            time.sleep(2)

        articles = page.query_selector_all('div[role="article"]')
        print(f"  FB: Found {len(articles)} post articles.")

        for article in articles[:max_posts]:
            try:
                text = article.inner_text()
                if not text or len(text) < 30:
                    continue

                # Extract numeric post ID from any /posts/ link
                post_id = None
                for link in article.query_selector_all('a[href*="/posts/"]'):
                    href = link.get_attribute('href') or ''
                    m = re.search(r'/posts/(\d+)', href)
                    if m:
                        post_id = m.group(1)
                        break

                # Fallback: stable hash of post text
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
                print(f"  FB: Error reading article: {e}")
                continue

    except Exception as e:
        print(f"  FB: Error scraping group: {e}")
        traceback.print_exc()

    return posts


def _scrape_all_groups(max_posts: int) -> List[Dict]:
    """
    Login to Facebook and scrape all groups in FB_GROUPS.
    Returns a flat list of post dicts.
    """
    email = os.getenv('FB_EMAIL', '')
    password = os.getenv('FB_PASSWORD', '')
    if not email or not password:
        print("  FB: FB_EMAIL / FB_PASSWORD not set — skipping Facebook monitoring.")
        return []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  FB: Playwright not installed — skipping Facebook monitoring.")
        return []

    all_posts = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/122.0.0.0 Safari/537.36'
                ),
                viewport={'width': 1366, 'height': 768},
            )
            page = ctx.new_page()

            if not _fb_login(page, email, password):
                print("  FB: Login failed — aborting Facebook scrape.")
                browser.close()
                return []

            for group_url in FB_GROUPS:
                group_posts = _scrape_group_posts(page, group_url, max_posts=max_posts)
                all_posts.extend(group_posts)
                time.sleep(2)

            browser.close()

    except Exception as e:
        print(f"  FB: Fatal Playwright error: {e}")
        traceback.print_exc()

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
