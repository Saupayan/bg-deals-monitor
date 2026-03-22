"""
game_parser.py
--------------
Extracts a clean board game name from a BGG Hot Deals thread title.

BGG deal titles look like:
  "GameNerdz DotD (Fri 20-Mar-2026): Prehistories $11.97"
  "[Amazon] Fields of Arle $40.99 (DEAD)"
  "Nova Era on Amazon for $17.09"
  "Beyond the Sun $41.99 (51% off) Amazon"
  "DEAD - [Amazon] Ginkgopolis - $33.96"

We strip out:
  - Retailer prefixes/tags  (Amazon, GameNerdz DotD, Miniature Market DotD, etc.)
  - Status markers          (DEAD, Sold Out, Expired)
  - Price strings           ($12.99, 51% off, w/ coupon, etc.)
  - Trailing noise          (free shipping, add to cart, etc.)

What remains should be the game name.
"""

import re
from typing import Optional


# ── Retailer / source prefixes to strip ──────────────────────────────────────
RETAILER_PREFIXES = [
    r'gamenerdz\s+dotd\s*\([^)]+\)\s*:?\s*',          # GameNerdz DotD (date):
    r'miniature\s+market\s+dotd\s*[-–]?\s*',            # Miniature Market DotD -
    r'miniature\s+market\s*[-–]?\s*',                   # Miniature Market -
    r'\[mm\s+dotd[^\]]*\]\s*',                          # [MM DotD Sun 20-Apr-2025]
    r'\[amazon\]\s*',                                   # [Amazon]
    r'\[target[^\]]*\]\s*',                             # [Target]  [Target* via eBay]
    r'\[walmart[^\]]*\]\s*',                            # [Walmart]
    r'\[coolstuffinc[^\]]*\]\s*',                       # [CoolStuffInc]
    r'\[allplay[^\]]*\]\s*',                            # [AllPlay]
    r'\[portal\s+games[^\]]*\]\s*',                     # [portal games]
    r'\[bgg\s+store[^\]]*\]\s*',                        # [BGG STORE]
    r'\[many\s*realms[^\]]*\]\s*',                      # [ManyRealms]
    r'\[nintendo\s+eshop[^\]]*\]\s*',                   # [Nintendo eShop]
    r'\[fantasywelt[^\]]*\]\s*',                        # [fantasywelt.de]
    r'amazon\s+us\s*[-–]?\s*',                         # Amazon US -
    r'amazon\s*:\s*',                                   # Amazon:
    r'\[[^\]]{1,40}\]\s*',                              # any other short [tag]
]

# ── Status markers ────────────────────────────────────────────────────────────
STATUS_PREFIXES = [
    r'^\s*dead\s*[-–]?\s*',
    r'^\s*\(dead\)\s*[-–]?\s*',
    r'^\s*\[dead\]\s*',
    r'^\s*sold\s*out\s*[-–]?\s*',
    r'^\s*\[sold\s*out\]\s*',
    r'^\s*expired\s*[-–]?\s*',
    r'^\s*\[expired\]\s*',
]

# ── Price and deal-noise patterns ─────────────────────────────────────────────
NOISE_PATTERNS = [
    r'\s*\(dead\)\s*$',
    r'\s*\(sold\s*out\)\s*$',
    r'\s*\(expired\)\s*$',
    r'\s*dead\s*$',
    r'\s+for\s+\$[\d,]+\.?\d*.*$',          # " for $17.09..."
    r'\s*[-–]\s*\$[\d,]+\.?\d*.*$',         # " - $33.96..."
    r'\s+\$[\d,]+\.?\d*.*$',                # " $11.97..."
    r'\s+\d+%\s*off.*$',                    # " 51% off..."
    r'\s+w/\s+coupon.*$',                   # " w/ coupon..."
    r'\s+with\s+coupon.*$',                 # " with coupon..."
    r'\s+free\s+shipping.*$',               # " free shipping..."
    r'\s+add\s+to\s+cart.*$',              # " add to cart..."
    r'\s+\(atl\).*$',                       # " (ATL)"
    r'\s+\(ymmv\).*$',                      # " (YMMV)"
    r'\s+\([^\)]{1,40}\)\s*$',             # trailing short parenthetical
    r'\s+on\s+(amazon|target|walmart|ebay|coolstuffinc|miniature\s*market|gamenerdz|tabletop\s*merchant|allplay)[^\$]*$',
    r'\s+at\s+(amazon|target|walmart|ebay|coolstuffinc|miniature\s*market|gamenerdz|tabletop\s*merchant|allplay)[^\$]*$',
    r'\s+via\s+(amazon|target|walmart|ebay)[^\$]*$',
    r'\s*,\s*(amazon|target|walmart)[^\$]*$',
]


def extract_game_name(thread_title: str) -> Optional[str]:
    """
    Extract the board game name from a BGG Hot Deals thread title.
    Returns None if the title doesn't look like a standard deal post.
    """
    if not thread_title:
        return None

    title = thread_title.strip()

    # 1. Strip status markers from the front
    for pattern in STATUS_PREFIXES:
        title = re.sub(pattern, '', title, flags=re.IGNORECASE)

    # 2. Strip retailer prefixes
    for pattern in RETAILER_PREFIXES:
        title = re.sub(r'^\s*' + pattern, '', title, flags=re.IGNORECASE)

    # 3. Strip price/noise from the end
    for pattern in NOISE_PATTERNS:
        title = re.sub(pattern, '', title, flags=re.IGNORECASE)

    # 4. Clean up remaining punctuation and whitespace
    title = title.strip(' -–,.:')
    title = re.sub(r'\s+', ' ', title).strip()

    # 5. Reject if result is too short or looks like a sentence fragment
    if len(title) < 2 or title.lower() in ('now', 'dead', 'sold', 'expired'):
        return None

    return title


def is_active_deal(thread_title: str) -> bool:
    """Returns True if the deal is still active (not dead/expired/sold out)."""
    lower = thread_title.lower()
    markers = ['dead', 'sold out', 'expired', '[sold out]', '[dead]', '[expired]']
    return not any(m in lower for m in markers)



def extract_deal_price(thread_title: str) -> 'Optional[float]':
    """
    Extract the deal price (in USD) from a BGG Hot Deals thread title.
    Returns the price as a float, or None if no price is found.

    Handles formats like:
      "[Amazon] Wingspan $40.99"          → 40.99
      "Nova Era on Amazon for $17.09"     → 17.09
      "Beyond the Sun $41.99 (51% off)"   → 41.99
      "Gloomhaven $120"                   → 120.0
    """
    if not thread_title:
        return None
    match = re.search(r'\$([\d,]+\.?\d*)', thread_title)
    if match:
        try:
            return float(match.group(1).replace(',', ''))
        except ValueError:
            pass
    return None


def extract_multi_game_deals(title: str):
    """
    Detect and parse a BGG Hot Deals thread that lists multiple games in the title.

    Returns a list of (name, price_or_None) tuples when 2+ price patterns are found
    (e.g. "Fate of the Fellowship ($56), Hot Streak ($35), Emerald Echoes ($52.50)").
    Returns None if this looks like a single-game thread.

    Handles both "$56" and "21$" price formats.
    Game names containing commas (e.g. "Istanbul, the Dice Game") are handled
    correctly because we split on "), " not on "," alone.
    """
    from typing import List, Tuple, Optional as Opt
    if not title:
        return None

    # Count price-like patterns — fewer than 2 means single deal
    price_hits = re.findall(r'\$[\d,]+\.?\d*|[\d,]+\.?\d*\$', title)
    if len(price_hits) < 2:
        return None

    # Split on "), " — each chunk is "Game Name ($price" or "Game Name (price$"
    # The very last chunk may still have its closing ) — handled in the regex below
    parts = re.split(r'\)\s*,\s*', title)
    results: List[Tuple[str, Opt[float]]] = []

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Extract price from the opening-paren block at the end:  ($56  or  (21$
        # Allow optional trailing ) for the last item
        price_m = (re.search(r'\(\s*\$([\d,]+\.?\d*)\s*\)?\s*$', part) or
                   re.search(r'\(\s*([\d,]+\.?\d*)\$\s*\)?\s*$', part))
        price = None
        if price_m:
            try:
                price = float(price_m.group(1).replace(',', ''))
            except ValueError:
                pass

        # Game name: everything before the opening paren (or the whole string
        # when there is no paren — e.g. the last item with no price)
        name_m = re.match(r'^(.+?)\s*\(', part)
        name = name_m.group(1).strip() if name_m else part.strip()
        name = name.strip(' -\u2013\u2014,.:')

        if name and len(name) > 1:
            results.append((name, price))

    return results if len(results) >= 2 else None

# ── Quick self-test ───────────────────────────────────────────────────────────
if __name__ == '__main__':
    test_titles = [
        "GameNerdz DotD (Fri 20-Mar-2026): Prehistories $11.97",
        "[Amazon] Fields of Arle $40.99 (DEAD)",
        "Nova Era on Amazon for $17.09",
        "Beyond the Sun $41.99 (51%off) Amazon",
        "DEAD - Amazon - Nunatak: Temple of Ice - $16.99 (or $13.59 w/ coupon)) - ATL",
        "[Amazon] Challengers - $22.49",
        "[amazon] Pan Am $14.98",
        "Miniature Market DotD - Sanibel $27.99",
        "[portal games] Gutenberg 90% off",
        "Chronicles of crime $21 on Amazon",
        "Unconditional Mind (Amazon, $39.99, 47% off)",
        "[Sold Out] GameNerdz DotD (Tue 17-Mar-2026): Flamme Rouge: Grand Tour Expansion $34.97",
        "Bass Pro Organizers 4 for $10",
        "1830: Railways & Robber Barons Board Game - Revised Edition - English - $29.99",
    ]

    print("Game name extraction test:\n")
    for t in test_titles:
        name = extract_game_name(t)
        active = is_active_deal(t)
        print(f"  IN : {t}")
        print(f"  OUT: {name}  ({'active' if active else 'DEAD'})")
        print()
