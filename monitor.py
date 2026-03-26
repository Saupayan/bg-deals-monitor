"""
monitor.py
----------
Main entry point for the BGG Hot Deals Monitor.

What it does (every CHECK_INTERVAL_MINUTES minutes):
  1. Fetch the first page of BGG Hot Deals forum threads via the BGG XML API
  2. Find NEW threads (not in seen_threads.json)
  3. For each new thread:
       a. Extract the game name from the thread title
       b. Look up BGG rating, weight, best player count, rank
       c. Fetch last 5 BGG marketplace USA sold listings
       d. Fetch current retail prices across US stores
       e. Fetch community reviews (pros/cons)
       f. Send a formatted HTML email with all of the above
  4. Save the new thread IDs to seen_threads.json

First run behaviour:
  Processes all deals posted in the last 24 hours (sends emails for each).
  Marks all older threads as already seen.
  From the next run onwards, only brand-new threads trigger alerts.

Usage:
  python monitor.py            -- run normally (loops forever, checks every 15 min)
  python monitor.py --once      -- run one check then exit (used by GitHub Actions scheduled/manual)
  python monitor.py --force     -- send compact WhatsApp list of ALL live deals from the last
                                   7 days (used by WhatsApp manual trigger via repository_dispatch)
                                   No email. No seen_threads.json changes.
  python monitor.py --test      -- full research pipeline on last 24h deals (local dev)
  python monitor.py --heartbeat -- send compact "monitor alive" WhatsApp with live deal list
                                   (used by hourly GitHub Actions heartbeat job)
"""

import json
import re
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Set, List, Dict, Optional

import schedule

import config
import bgg_api
import enrichment
import emailer
import whatsapp_notifier
import gamenerdz_dotd
import bgo_pricedrop
import ttm_dotd
from game_parser import extract_game_name, is_active_deal, extract_deal_price, extract_multi_game_deals


# Pinned/sticky posts that are NOT real deals â always skip these
SKIP_SUBJECTS = {
    'what is a hot deal?',
}


# -----------------------------------------------------------------------------
# DATE PARSING
# -----------------------------------------------------------------------------

def _parse_thread_date(date_str: str) -> datetime:
    """
    Parse a BGG thread postdate string into a timezone-aware datetime.
    BGG returns dates like: "Fri, 20 Mar 2026 13:01:00 +0000"
    Falls back to epoch if unparseable (so the thread won't be treated as recent).
    """
    if not date_str:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        return parsedate_to_datetime(date_str)
    except Exception:
        pass
    # Try ISO format fallback
    for fmt in ('%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            dt = datetime.strptime(date_str[:19], fmt[:len(fmt)])
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _is_within_hours(date_str: str, hours: int = 24) -> bool:
    """Return True if the thread was posted within the last `hours` hours."""
    dt = _parse_thread_date(date_str)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt >= cutoff


# -----------------------------------------------------------------------------
# SEEN-THREADS STATE
# -----------------------------------------------------------------------------

def load_seen_threads() -> Set[str]:
    if config.SEEN_THREADS_FILE.exists():
        try:
            return set(json.loads(config.SEEN_THREADS_FILE.read_text()))
        except Exception:
            pass
    return set()


def save_seen_threads(seen: Set[str]) -> None:
    config.SEEN_THREADS_FILE.write_text(json.dumps(sorted(seen), indent=2))


# -----------------------------------------------------------------------------
# RESEARCH A SINGLE DEAL THREAD  (returns data dict, does NOT send email)
# -----------------------------------------------------------------------------

def research_thread(thread: dict) -> Optional[Dict]:
    """
    Run the full research pipeline for one deal thread.
    Returns a deal dict ready for emailer, or None if the thread should be skipped.

    Uses enrichment.enrich_game() — the unified pipeline shared by all sources.
    If the game can't be found on BGG or fails the rating threshold, a screenshot
    of the BGG thread is sent as a fallback before returning None.
    """
    title = thread['subject']
    print(f"\n  Processing: '{title}'")

    # Step 1: Extract game name
    game_name = extract_game_name(title)
    if not game_name:
        print(f"    Could not extract game name -- skipping.")
        return None
    print(f"    Game name: '{game_name}'")

    # Steps 2–6: unified enrichment pipeline
    enriched = enrichment.enrich_game(
        game_name,
        filter_by_rating=True,
        include_reviews=True,
    )

    if enriched is None:
        # Below rating threshold or not found on BGG — send a screenshot so
        # the user can still see the deal post and decide for themselves.
        thread_url = (f"https://boardgamegeek.com/thread/{thread['id']}"
                      if thread.get('id') else '')
        if thread_url:
            enrichment.send_screenshot_fallback(
                thread_url,
                f"🎲 BGG Hot Deal (below threshold or not on BGG):\n"
                f"_{title}_\n🔗 {thread_url}",
            )
        return None

    return dict(
        thread        = thread,
        game_details  = enriched['game_details'],
        sold_listings = enriched['sold_listings'],
        retail_prices = enriched['retail_prices'],
        reviews       = enriched['reviews'],
    )


# -----------------------------------------------------------------------------
# MAIN CHECK  (handles BGG + GameNerdz DotD every run)
# -----------------------------------------------------------------------------

def check_for_new_deals(first_run: bool = False) -> None:
    """
    Fetch the latest Hot Deals threads and process any new ones.
    Also checks GameNerdz Deal of the Day on every run (dedup guard prevents
    double-sends within the same calendar day).

    first_run=True: process threads posted in the last 24 hours,
                    mark everything else as seen.
    first_run=False: process only threads not yet in seen_threads.json.
    """
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"\n{'='*60}")
    print(f"  Deal Monitor check @ {now_str}")
    print(f"{'='*60}")

    print("  Fetching BGG Hot Deals forum...")
    threads = bgg_api.get_forum_threads(forum_id=config.BGG_FORUM_ID, page=1)
    if not threads:
        print("  No threads returned -- BGG may be down or rate-limiting us.")
    else:
        print(f"  Got {len(threads)} threads from BGG.")

        seen = load_seen_threads()

        # Filter out pinned/sticky non-deal posts
        real_threads = [t for t in threads
                        if t['subject'].lower().strip() not in SKIP_SUBJECTS]

        if first_run:
            recent = [t for t in real_threads if _is_within_hours(t['post_date'], hours=24)]
            older  = [t for t in real_threads if not _is_within_hours(t['post_date'], hours=24)]
            for t in older:
                seen.add(t['id'])
            save_seen_threads(seen)

            if not recent:
                print("  No deals in the last 24 hours. Waiting for new ones...")
            else:
                print(f"  First run: {len(recent)} deal(s) from the last 24 hours.")
                threads_to_process = list(reversed(recent))
                _process_and_send_bgg(threads_to_process, seen)
        else:
            new_threads = [t for t in real_threads if t['id'] not in seen]
            if not new_threads:
                print("  No new BGG deals since last check.")
            else:
                print(f"  Found {len(new_threads)} new thread(s)!")
                threads_to_process = list(reversed(new_threads))
                _process_and_send_bgg(threads_to_process, seen)

    # Always check GameNerdz Deal of the Day.
    # check_gamenerdz_dotd(force=False) has its own dedup guard (gamenerdz_sent.txt)
    # so it only sends once per calendar day regardless of how often this runs.
    try:
        gamenerdz_dotd.check_gamenerdz_dotd(force=False, use_playwright=False)
    except Exception as e:
        print(f"\n  GameNerdz DotD check error: {e}")
        traceback.print_exc()

    # BGO daily price drops — dedup guard in bgo_sent.json (sends once per game per day)
    try:
        bgo_pricedrop.check_bgo_price_drops(force=False)
    except Exception as e:
        print(f"\n  BGO price drop check error: {e}")
        traceback.print_exc()

    # Tabletop Merchant Deal of the Day — dedup guard in ttm_sent.json
    try:
        ttm_dotd.check_ttm_dotd(force=False)
    except Exception as e:
        print(f"\n  Tabletop Merchant DotD check error: {e}")
        traceback.print_exc()


def _process_and_send_bgg(threads_to_process: list, seen: Set[str]) -> None:
    """Research all threads and send a consolidated alert. Updates seen set."""
    deals = []
    for thread in threads_to_process:
        try:
            deal = research_thread(thread)
            if deal:
                deals.append(deal)
        except Exception as e:
            print(f"  Error processing '{thread['subject']}': {e}")
            traceback.print_exc()
        finally:
            seen.add(thread['id'])
            save_seen_threads(seen)
        time.sleep(2)

    if deals:
        print(f"\n  Sending consolidated email for {len(deals)} deal(s)...")
        emailer.send_consolidated_alert(deals)
        print(f"  Sending WhatsApp alert...")
        whatsapp_notifier.send_deal_whatsapp(deals)


# -----------------------------------------------------------------------------
# FORCE MODE  (--force)  — WhatsApp manual trigger
# -----------------------------------------------------------------------------

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS FOR PRICE ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def _parse_price_float(price_str: str) -> Optional[float]:
    """Parse a price string like '$18.00' or '18.0' into a float."""
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
                  current_listings: list = None) -> str:
    """
    Return a short verdict emoji + label comparing the deal price to retail,
    recent BGG sold prices, and current BGG for-sale listings.

    Priority of comparison baseline (best data wins):
      1. Cheapest in-stock retail price
      2. Cheapest current BGG listing (if no retail data)
      3. Average recent BGG sold price (last resort)

    Tiers:
      🔥 Steal    — deal is ≥30% below baseline
      ✅ Good     — deal is ≥15% below baseline
      😐 Fair     — deal is within ±15% of baseline
      🤔 Pricey   — deal is ≥15% above baseline
      ❓ No data  — not enough price info to call it
    """
    if current_listings is None:
        current_listings = []
    if deal_price is None:
        return "❓ No price in post"

    # Find cheapest in-stock retail price
    in_stock = [p for p in retail_prices if p.get('in_stock')]
    if not in_stock:
        in_stock = retail_prices  # fall back to any price if nothing in stock

    if not in_stock:
        # No retail data — try cheapest current BGG listing as baseline
        cur_floats = [_parse_price_float(s['price']) for s in current_listings]
        cur_floats = [p for p in cur_floats if p]
        if cur_floats:
            cheapest_listed = min(cur_floats)
            ratio = deal_price / cheapest_listed
            if ratio <= 0.70:
                return f"🔥 Steal — {int((1 - ratio)*100)}% below cheapest BGG listing"
            if ratio <= 0.85:
                return f"✅ Good — {int((1 - ratio)*100)}% below cheapest BGG listing"
            if ratio <= 1.15:
                return f"😐 Fair — near cheapest BGG listing (${cheapest_listed:.2f})"
            return f"🤔 Pricey — others listing at ${cheapest_listed:.2f} on BGG"

        # Last resort: average recent sold price
        sold_floats = [_parse_price_float(s['price']) for s in sold_listings]
        sold_floats = [p for p in sold_floats if p]
        if not sold_floats:
            return "❓ No price data to compare"
        avg_sold = sum(sold_floats) / len(sold_floats)
        ratio = deal_price / avg_sold
        if ratio <= 0.70:
            return f"🔥 Steal — {int((1 - ratio)*100)}% below avg BGG sold price"
        if ratio <= 0.85:
            return f"✅ Good — {int((1 - ratio)*100)}% below avg BGG sold price"
        if ratio <= 1.15:
            return f"😐 Fair — near avg BGG sold price"
        return f"🤔 Pricey — above avg BGG sold price (${avg_sold:.2f})"

    cheapest_retail = in_stock[0]['price_usd']
    ratio = deal_price / cheapest_retail

    if ratio <= 0.70:
        return f"🔥 Steal — {int((1 - ratio)*100)}% below cheapest retail"
    if ratio <= 0.85:
        return f"✅ Good — {int((1 - ratio)*100)}% below cheapest retail"
    if ratio <= 1.15:
        return f"😐 Fair — near retail price"
    return f"🤔 Pricey — cheapest online is ${cheapest_retail:.2f}"


def _format_deal_card(thread: dict, game_name: str,
                       retail_prices: list, sold_listings: list,
                       current_listings: list = None) -> list:
    """
    Build the lines for one deal's WhatsApp card.

    If the thread title contains multiple games (e.g. bundle posts like
    "Game A ($30), Game B ($25), Game C ($40)"), a compact multi-game card
    is returned listing each name and price — no per-game research is done
    because running the full pipeline on 5–6 games per thread would be too slow.

    For single-game threads, a full card with retail/sold/verdict is built.
    current_listings: current (unsold) BGG Marketplace for-sale listings.
    Returns a list of strings (one per line).
    """
    if current_listings is None:
        current_listings = []
    active = is_active_deal(thread['subject'])
    status = "🟢" if active else "🔴"

    try:
        age_h = int((datetime.now(timezone.utc) - _parse_thread_date(thread['post_date']))
                    .total_seconds() / 3600)
        age_str = f"{age_h}h ago" if age_h < 48 else f"{age_h // 24}d ago"
    except Exception:
        age_str = ""

    thread_url = (f"https://boardgamegeek.com/thread/{thread['id']}"
                  if thread.get('id') else '')

    # ── Dead deal: compact one-liner ─────────────────────────────────────────
    if not active:
        lines = [f"{status} *{game_name}* ({age_str}) — expired"]
        if thread_url:
            lines.append(f"  {thread_url}")
        return lines

    # ── Multi-game thread detection ───────────────────────────────────────────
    multi = extract_multi_game_deals(thread['subject'])
    if multi:
        lines = [f"{status} *[{len(multi)} games in this post]* ({age_str})"]
        for name, price in multi:
            price_str = f"${price:.2f}" if price is not None else "price?"
            lines.append(f"  • {name} — {price_str}")
        if thread_url:
            lines.append(f"  🔗 {thread_url}")
        return lines

    # ── Single live deal: full card ───────────────────────────────────────────
    deal_price = extract_deal_price(thread['subject'])
    lines = [f"{status} *{game_name}* ({age_str})"]

    if deal_price is not None:
        lines.append(f"  💰 Deal price: ${deal_price:.2f}")

    # Retail prices (in-stock, cheapest first)
    in_stock = [p for p in retail_prices if p.get('in_stock')]
    if in_stock:
        lo, hi = in_stock[0]['price_usd'], in_stock[-1]['price_usd']
        cheapest_store = in_stock[0]['store']
        if lo == hi:
            lines.append(f"  🏪 Retail: ${lo:.2f} ({cheapest_store})")
        else:
            lines.append(f"  🏪 Retail: ${lo:.2f}–${hi:.2f} (cheapest: {cheapest_store})")
    elif retail_prices:
        lines.append(f"  🏪 Retail: (out of stock everywhere)")
    else:
        lines.append(f"  🏪 Retail: no data")

    # BGG Marketplace current listings (for sale right now, US only)
    current_floats = [_parse_price_float(s['price']) for s in current_listings]
    current_floats = [p for p in current_floats if p]
    if current_floats:
        lo_c, hi_c = min(current_floats), max(current_floats)
        n_c = len(current_floats)
        if lo_c == hi_c:
            lines.append(f"  🏷️ BGG listed: ${lo_c:.2f} ({n_c} cop{'ies' if n_c != 1 else 'y'})")
        else:
            lines.append(f"  🏷️ BGG listed: ${lo_c:.2f}–${hi_c:.2f} ({n_c} cop{'ies' if n_c != 1 else 'y'})")
    else:
        lines.append(f"  🏷️ BGG listed: none for sale")

    # BGG Marketplace sold listings
    sold_floats = [_parse_price_float(s['price']) for s in sold_listings]
    sold_floats = [p for p in sold_floats if p]
    if sold_floats:
        lo_s, hi_s = min(sold_floats), max(sold_floats)
        n = len(sold_floats)
        if lo_s == hi_s:
            lines.append(f"  📦 BGG sold: ${lo_s:.2f} ({n} sale{'s' if n != 1 else ''})")
        else:
            lines.append(f"  📦 BGG sold: ${lo_s:.2f}–${hi_s:.2f} ({n} sale{'s' if n != 1 else ''})")
    else:
        lines.append(f"  📦 BGG sold: no recent data")

    # Verdict
    verdict = _deal_verdict(deal_price, retail_prices, sold_listings, current_listings)
    lines.append(f"  {verdict}")

    if thread_url:
        lines.append(f"  🔗 {thread_url}")

    return lines

# -----------------------------------------------------------------------------
# FORCE MODE  (--force)  — WhatsApp manual trigger
# -----------------------------------------------------------------------------

def run_force_mode() -> None:
    """
    Triggered when the user sends the WhatsApp trigger word.

    - Fetches up to 3 pages of BGG Hot Deals (stops when all threads > 7 days old)
    - Shows ALL deals from the last 7 days (live and expired)
    - For LIVE deals: runs retail price lookup + BGG marketplace sold listings,
      then gives a deal verdict (🔥 Steal / ✅ Good / 😐 Fair / 🤔 Pricey / ❓)
    - For EXPIRED/DEAD deals: compact single-line entry (no research)
    - Does NOT update seen_threads.json — scheduled runs continue unaffected
    """
    now_str = datetime.now().strftime('%H:%M')
    print(f"\n=== FORCE / MANUAL TRIGGER @ {now_str} — deals from the last 7 days ===\n")

    # ── 1. Fetch up to 3 pages ────────────────────────────────────────────────
    all_threads: List[Dict] = []
    for page in range(1, 4):
        print(f"  Fetching BGG Hot Deals page {page}...")
        page_threads = bgg_api.get_forum_threads(forum_id=config.BGG_FORUM_ID, page=page)
        if not page_threads:
            print(f"  Page {page} returned nothing — stopping.")
            break
        page_has_recent = any(_is_within_hours(t['post_date'], hours=7 * 24)
                               for t in page_threads)
        all_threads.extend(page_threads)
        if not page_has_recent:
            print(f"  Page {page}: all threads older than 7 days — done fetching.")
            break
        time.sleep(1)

    # ── 2. Filter to 7 days, sort newest first ────────────────────────────────
    week_deals = [
        t for t in all_threads
        if t['subject'].lower().strip() not in SKIP_SUBJECTS
        and _is_within_hours(t['post_date'], hours=7 * 24)
    ]
    week_deals.sort(key=lambda t: _parse_thread_date(t['post_date']), reverse=True)

    live_deals  = [t for t in week_deals if is_active_deal(t['subject'])]
    dead_deals  = [t for t in week_deals if not is_active_deal(t['subject'])]
    print(f"  {len(week_deals)} total: {len(live_deals)} live, {len(dead_deals)} expired\n")

    if not week_deals:
        whatsapp_notifier.send_whatsapp(
            f"🎲 *BGG Hot Deals — last 7 days*\n\n"
            f"No deals found.\n\n_Checked at {now_str}_"
        )
        return

    # ── 3. Research each LIVE deal ────────────────────────────────────────────
    deal_cards: Dict[str, list] = {}   # thread_id → lines

    for thread in week_deals:
        game_name = extract_game_name(thread['subject']) or thread['subject']

        if not is_active_deal(thread['subject']):
            # Dead: no research needed
            deal_cards[thread['id']] = _format_deal_card(thread, game_name, [], [])
            continue

        # Multi-game threads: skip API research — the card lists all games & prices
        if extract_multi_game_deals(thread['subject']):
            print(f"  Multi-game thread — listing items, skipping research")
            deal_cards[thread['id']] = _format_deal_card(thread, game_name, [], [])
            continue

        print(f"  Researching: '{game_name}'")

        # Unified enrichment pipeline — same steps as all other sources.
        # Rating filter always applied: games below config.BGG_MIN_RATING are skipped.
        enriched = enrichment.enrich_game(
            game_name,
            filter_by_rating=True,
            include_reviews=False,   # force mode is compact — no reviews needed
            num_listings=10,         # show more options for comparison
        )
        retail_prices    = (enriched or {}).get('retail_prices', [])
        sold_listings    = (enriched or {}).get('sold_listings', [])
        current_listings = (enriched or {}).get('current_listings', [])

        deal_cards[thread['id']] = _format_deal_card(
            thread, game_name, retail_prices, sold_listings, current_listings
        )
        time.sleep(1)  # be polite between deals

    # ── 4. Assemble and send WhatsApp message ─────────────────────────────────
    lines = [
        f"🎲 *BGG Hot Deals — last 7 days*",
        f"_{len(live_deals)} live · {len(dead_deals)} expired_",
        "",
    ]

    for thread in week_deals:
        lines.extend(deal_cards.get(thread['id'], []))
        lines.append("")   # blank line between deals

    lines.append(f"_Checked at {now_str}_")
    msg = "\n".join(lines)

    print(f"\n  Sending BGG deals WhatsApp ({len(msg)} chars)...")
    print(msg)
    whatsapp_notifier.send_whatsapp(msg)

    # Also check GameNerdz DotD — force=True bypasses the once-per-day guard
    # so you always get the current DotD when you manually trigger
    print("\n  Checking GameNerdz Deal of the Day...")
    try:
        gamenerdz_dotd.check_gamenerdz_dotd(force=True, use_playwright=False)
    except Exception as e:
        print(f"  GameNerdz DotD error: {e}")
        traceback.print_exc()

    # BGO daily price drops — force=False so only NEW deals (not already sent today)
    # are sent. Re-sending the full research pipeline for every qualifying drop on
    # every manual trigger is too noisy. You'll still get new ones as they appear.
    print("\n  Checking BGO Daily Price Drops (new only)...")
    try:
        bgo_pricedrop.check_bgo_price_drops(force=False)
    except Exception as e:
        print(f"  BGO price drop error: {e}")
        traceback.print_exc()

    # Tabletop Merchant DotD — same logic: only new deals, not a full re-send
    print("\n  Checking Tabletop Merchant Deal of the Day (new only)...")
    try:
        ttm_dotd.check_ttm_dotd(force=False)
    except Exception as e:
        print(f"  Tabletop Merchant DotD error: {e}")
        traceback.print_exc()


# -----------------------------------------------------------------------------
# TEST MODE  (--test)  — local development / full research
# -----------------------------------------------------------------------------

def run_test_mode() -> None:
    """
    Process all deals posted in the last 24 hours right now, plus today's
    GameNerdz Deal of the Day. Does NOT update seen_threads.json.
    Used by --test for local development (runs the full research pipeline).
    """
    mode = "TEST"
    print(f"\n=== {mode} â sending all deals from the last 24 hours ===\n")

    threads = bgg_api.get_forum_threads(forum_id=config.BGG_FORUM_ID, page=1)
    if not threads:
        print("Could not fetch threads from BGG.")
    else:
        recent = [
            t for t in threads
            if t['subject'].lower().strip() not in SKIP_SUBJECTS
            and _is_within_hours(t['post_date'], hours=24)
        ]

        if not recent:
            print("No deals found in the last 24 hours on BGG Hot Deals.")
        else:
            print(f"Found {len(recent)} deal(s) from the last 24 hours:\n")
            for t in recent:
                print(f"  - {t['subject']}")
            print()

        recent.reverse()  # oldest first
        deals = []
        for thread in recent:
            try:
                deal = research_thread(thread)
                if deal:
                    deals.append(deal)
            except Exception as e:
                print(f"Error processing '{thread['subject']}': {e}")
                traceback.print_exc()
            time.sleep(2)

    # Also check GameNerdz Deal of the Day (force=True bypasses dedup)
    print("\n--- Checking GameNerdz Deal of the Day ---")
    try:
        dotd = gamenerdz_dotd.fetch_dotd(use_playwright=False)
        if dotd:
            dotd_deal = gamenerdz_dotd.research_dotd(dotd)
            if dotd_deal:
                deals.append(dotd_deal)
                print(f"  Added GameNerdz DotD: '{dotd['name']}'")
        else:
            print("  No GameNerdz DotD found right now.")
            # Fallback: send a screenshot of the DotD page via thum.io.
            # thum.io is a free screenshot-as-a-service — no API key needed.
            # This runs even in the main bgg-monitor.yml workflow where Playwright
            # is not available, because it only requires an HTTP request.
            _ts = int(time.time())
            thum_url = (
                "https://image.thum.io/get/noanimate/nocache/"
                f"https://www.gamenerdz.com/deal-of-the-day?_t={_ts}"
            )
            print("  Sending GameNerdz DotD page screenshot via WhatsApp (thum.io)...")
            whatsapp_notifier.send_image_whatsapp(
                thum_url,
                "🏪 GameNerdz Deal of the Day\n"
                "⚠️ Couldn't parse details — here's the live page.\n"
                "🔗 https://www.gamenerdz.com/deal-of-the-day",
            )
    except Exception as e:
        print(f"  GameNerdz DotD error: {e}")
        traceback.print_exc()

    if deals:
        print(f"\n  Sending consolidated email for {len(deals)} deal(s)...")
        emailer.send_consolidated_alert(deals)
        print(f"  Sending WhatsApp alert...")
        whatsapp_notifier.send_deal_whatsapp(deals)

    print(f"\nTest complete. Processed {len(deals)} deal(s).")


# -----------------------------------------------------------------------------
# HEARTBEAT MODE
# -----------------------------------------------------------------------------

def run_heartbeat_mode() -> None:
    """
    Send a compact "monitor alive" WhatsApp showing what's currently live on BGG Hot Deals
    and today's GameNerdz DotD. No email. No seen_threads.json changes.
    Runs hourly so you can verify the monitor is healthy even on quiet days.
    """
    from datetime import datetime
    now_str = datetime.now().strftime('%H:%M')
    print(f"\n=== HEARTBEAT @ {now_str} ===\n")

    threads = bgg_api.get_forum_threads(forum_id=config.BGG_FORUM_ID, page=1)
    if not threads:
        msg = f"â ï¸ Monitor heartbeat @ {now_str}\nCould not reach BGG â may be down or rate-limiting."
        print("BGG unreachable â sending warning heartbeat")
        whatsapp_notifier.send_whatsapp(msg)
        return

    recent = [
        t for t in threads
        if t['subject'].lower().strip() not in SKIP_SUBJECTS
        and _is_within_hours(t['post_date'], hours=24)
    ]

    lines = [f"ð¢ *Monitor alive @ {now_str}*", ""]

    if not recent:
        lines.append("No deals on BGG Hot Deals in the last 24h.")
    else:
        lines.append(f"*BGG Hot Deals ({len(recent)}):*")
        for t in recent:
            game_name = extract_game_name(t['subject']) or t['subject']
            thread_url = f"https://boardgamegeek.com/thread/{t['id']}" if t.get('id') else ''
            lines.append(f"â¢ {game_name}: {thread_url}")

    # Also show today's GameNerdz DotD if available
    try:
        dotd = gamenerdz_dotd.fetch_dotd()
        if dotd:
            lines.append("")
            lines.append(f"*GameNerdz Deal of the Day:*")
            lines.append(f"â¢ {dotd['name']} â {dotd['price_str']}: {dotd['url']}")
    except Exception:
        pass  # Heartbeat should never crash on GameNerdz errors

    lines.append("")
    lines.append("_Next check in ~15 min_")

    msg = "\n".join(lines)
    print(msg)
    whatsapp_notifier.send_whatsapp(msg)


# -----------------------------------------------------------------------------
# ENTRY POINT
# -----------------------------------------------------------------------------

if __name__ == '__main__':

    # --heartbeat: compact "alive" WhatsApp with live deal list. No email, no seen update.
    # Used by the hourly heartbeat GitHub Actions job.
    if '--heartbeat' in sys.argv:
        run_heartbeat_mode()
        sys.exit(0)

    # --force: compact WhatsApp list of all live deals from the last 7 days.
    # Used by the WhatsApp manual trigger (repository_dispatch).
    if '--force' in sys.argv:
        # WhatsApp manual trigger: compact list of all live deals from the last 7 days.
        # No email, no full research, no seen_threads.json changes.
        run_force_mode()
        sys.exit(0)

    if '--test' in sys.argv:
        # Local dev: full research pipeline on the last 24h of deals.
        run_test_mode()
        sys.exit(0)

    # --once: single check and exit (for GitHub Actions / cron jobs)
    if '--once' in sys.argv:
        print("=== Deal Monitor â single check ===")
        is_first_run = not config.SEEN_THREADS_FILE.exists()
        check_for_new_deals(first_run=is_first_run)
        print("=== Done ===")
        sys.exit(0)

    print("""
+----------------------------------------------------------+
|          Board Game Deals Monitor -- Starting Up        |
+----------------------------------------------------------+
""")
    print(f"  Checking every {config.CHECK_INTERVAL_MINUTES} minutes.")
    print(f"  Alerts will be sent to: {config.ALERT_EMAIL}")
    print(f"  Press Ctrl+C to stop.\n")

    # First run: process last-24h deals, snapshot older ones
    is_first_run = not config.SEEN_THREADS_FILE.exists()
    check_for_new_deals(first_run=is_first_run)

    # Schedule subsequent checks
    schedule.every(config.CHECK_INTERVAL_MINUTES).minutes.do(check_for_new_deals)
    print(f"\n  Next check in {config.CHECK_INTERVAL_MINUTES} minutes. Waiting...\n")

    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except KeyboardInterrupt:
            print("\n\n  Monitor stopped by user.")
            break
        except Exception as e:
            print(f"  Unexpected error in main loop: {e}")
            traceback.print_exc()
            time.sleep(60)
