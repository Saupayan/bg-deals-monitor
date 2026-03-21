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
  python monitor.py --once     -- run one check then exit (used by GitHub Actions scheduled/manual)
  python monitor.py --force    -- send ALL deals from last 24h right now, skip seen filter
                                  (used by WhatsApp manual trigger via repository_dispatch)
  python monitor.py --test     -- same as --force (alias for local development)
"""

import json
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
import marketplace
import price_checker
import emailer
import whatsapp_notifier
from game_parser import extract_game_name, is_active_deal


# Pinned/sticky posts that are NOT real deals — always skip these
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
    """
    title = thread['subject']
    print(f"\n  Processing: '{title}'")

    # Step 1: Extract game name
    game_name = extract_game_name(title)
    if not game_name:
        print(f"    Could not extract game name -- skipping.")
        return None
    print(f"    Game name: '{game_name}'")

    # Step 2: BGG lookup
    print(f"    Looking up '{game_name}' on BGG...")
    bgg_id = bgg_api.search_game(game_name)
    game_details = None

    if bgg_id:
        print(f"    BGG ID: {bgg_id}")
        time.sleep(1)
        game_details = bgg_api.get_game_details(bgg_id)
        if game_details:
            print(f"    Details: '{game_details['name']}' | "
                  f"Rating: {game_details['average_rating']} | "
                  f"Weight: {game_details['weight']} | "
                  f"Best at: {game_details['best_players']}p")
    else:
        print(f"    Not found on BGG. Will include with limited info.")

    # Step 3: BGG Marketplace sold listings
    sold_listings = []
    if bgg_id:
        print(f"    Fetching BGG marketplace sold listings (USA)...")
        time.sleep(1)
        sold_listings = marketplace.get_sold_listings(bgg_id, num_listings=5)
        print(f"    Found {len(sold_listings)} sold listing(s)")

    # Step 4: Retail prices
    retail_prices = []
    name_for_search = (game_details or {}).get('name', game_name)
    print(f"    Checking retail prices for '{name_for_search}'...")
    try:
        retail_prices = price_checker.get_all_prices(name_for_search, bgg_id or '')
        if retail_prices:
            print(f"    Found {len(retail_prices)} price(s). "
                  f"Cheapest: {retail_prices[0]['store']} @ {retail_prices[0]['price_str']}")
        else:
            print(f"    No retail prices found")
    except Exception as e:
        print(f"    Price check error: {e}")

    # Step 5: BGG reviews
    reviews = {'positive': [], 'negative': []}
    if bgg_id:
        print(f"    Fetching community reviews...")
        time.sleep(1)
        try:
            reviews = bgg_api.get_game_reviews(bgg_id)
            print(f"    Reviews: {len(reviews.get('positive',[]))} positive, "
                  f"{len(reviews.get('negative',[]))} negative")
        except Exception as e:
            print(f"    Reviews error: {e}")

    return dict(
        thread        = thread,
        game_details  = game_details,
        sold_listings = sold_listings,
        retail_prices = retail_prices,
        reviews       = reviews,
    )


# -----------------------------------------------------------------------------
# MAIN CHECK
# -----------------------------------------------------------------------------

def check_for_new_deals(first_run: bool = False) -> None:
    """
    Fetch the latest Hot Deals threads and process any new ones.

    first_run=True: process threads posted in the last 24 hours,
                    mark everything else as seen.
    first_run=False: process only threads not yet in seen_threads.json.
    """
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"\n{'='*60}")
    print(f"  BGG Deal Monitor check @ {now_str}")
    print(f"{'='*60}")

    print("  Fetching BGG Hot Deals forum...")
    threads = bgg_api.get_forum_threads(forum_id=config.BGG_FORUM_ID, page=1)
    if not threads:
        print("  No threads returned -- BGG may be down or rate-limiting us.")
        return

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
            return

        print(f"  First run: {len(recent)} deal(s) from the last 24 hours.")
        threads_to_process = list(reversed(recent))
    else:
        new_threads = [t for t in real_threads if t['id'] not in seen]
        if not new_threads:
            print("  No new deals since last check.")
            return
        print(f"  Found {len(new_threads)} new thread(s)!")
        threads_to_process = list(reversed(new_threads))

    # Research all threads, then send ONE consolidated email
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
# TEST MODE
# -----------------------------------------------------------------------------

def run_test_mode() -> None:
    """
    Process all deals posted in the last 24 hours right now.
    Does NOT update seen_threads.json, so you can re-run it freely.
    Used by both --test (local dev) and --force (WhatsApp manual trigger).
    """
    mode = "FORCE / MANUAL TRIGGER" if '--force' in sys.argv else "TEST"
    print(f"\n=== {mode} — sending all deals from the last 24 hours ===\n")

    threads = bgg_api.get_forum_threads(forum_id=config.BGG_FORUM_ID, page=1)
    if not threads:
        print("Could not fetch threads from BGG.")
        return

    recent = [
        t for t in threads
        if t['subject'].lower().strip() not in SKIP_SUBJECTS
        and _is_within_hours(t['post_date'], hours=24)
    ]

    if not recent:
        print("No deals found in the last 24 hours on BGG Hot Deals.")
        print("(Tip: try increasing the window by editing run_test_mode in monitor.py)")
        return

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

    if deals:
        print(f"\n  Sending consolidated email for {len(deals)} deal(s)...")
        emailer.send_consolidated_alert(deals)
        print(f"  Sending WhatsApp alert...")
        whatsapp_notifier.send_deal_whatsapp(deals)

    print(f"\nTest complete. Processed {len(deals)} deal(s).")


# -----------------------------------------------------------------------------
# ENTRY POINT
# -----------------------------------------------------------------------------

if __name__ == '__main__':

    # --force: send all deals from last 24h regardless of seen state
    # Used by the WhatsApp manual trigger (repository_dispatch).
    # Does NOT update seen_threads.json — scheduled runs continue unaffected.
    if '--force' in sys.argv or '--test' in sys.argv:
        run_test_mode()
        sys.exit(0)

    # --once: single check and exit (for GitHub Actions / cron jobs)
    if '--once' in sys.argv:
        print("=== BGG Deal Monitor — single check ===")
        is_first_run = not config.SEEN_THREADS_FILE.exists()
        check_for_new_deals(first_run=is_first_run)
        print("=== Done ===")
        sys.exit(0)

    print("""
+----------------------------------------------------------+
|          BGG Hot Deals Monitor -- Starting Up           |
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
