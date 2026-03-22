# PROJECT BIBLE — bg-deals-monitor
*Last updated: 2026-03-22*

---

## 1. What Is This?

A personal board game deal monitor that runs on GitHub Actions and sends WhatsApp notifications about:
- **BGG Hot Deals** — new threads on BoardGameGeek's Hot Deals forum
- **GameNerdz Deal of the Day (DotD)** — the daily discounted game on gamenerdz.com
- **Board Game Oracle (BGO) Daily Price Drops** — games with significant price drops, filtered to BGG ≥ 7.0
- **Tabletop Merchant Deal of the Day** — daily deal from tabletopmerchant.com, filtered to BGG ≥ 7.0

All deal alerts include a full research package: BGG stats, current marketplace listings, recently sold prices, retail prices, and community reviews.

---

## 2. Repository Structure

```
bg-deals-monitor/
├── .github/workflows/
│   ├── bgg-monitor.yml          # Main monitor: all deal sources (every 15 min)
│   └── heartbeat.yml            # Keep-alive / heartbeat workflow
├── monitor.py                   # Entry point for bgg-monitor.yml
├── gamenerdz_dotd.py            # GameNerdz Deal of the Day checker
├── bgo_pricedrop.py             # Board Game Oracle daily price drop checker
├── ttm_dotd.py                  # Tabletop Merchant Deal of the Day checker
├── whatsapp_notifier.py         # WhatsApp notification functions (Green API)
├── bgg_api.py                   # BoardGameGeek API wrapper
├── game_parser.py               # Game data parsing utilities
├── marketplace.py               # BGG marketplace scraper (current + sold listings)
├── price_checker.py             # Retail price checker (multiple stores)
├── emailer.py                   # Email notification via Gmail SMTP
├── config.py                    # Reads secrets from environment variables
├── requirements.txt             # Python dependencies
└── PROJECT_BIBLE.md             # This file
```

---

## 3. Workflows

### 3a. bgg-monitor.yml — Board Game Deals Monitor

- **Trigger**: Every 15 minutes (cron `*/15 * * * *`) + `workflow_dispatch` (manual) + `repository_dispatch` (WhatsApp trigger)
- **What it does**: Runs `monitor.py`, which checks all deal sources in sequence
- **No Playwright** — all sources use `requests` + `BeautifulSoup` only
- **Force mode**: When triggered by `repository_dispatch` (WhatsApp "go" command), runs `monitor.py --force` to re-send all current qualifying deals regardless of deduplication state

**State cache** — persisted between runs via `actions/cache`:
```
seen_threads.json     # BGG hot deal thread IDs already sent
gamenerdz_sent.txt    # GameNerdz DotD last-sent product handle
bgo_sent.json         # BGO game IDs sent today (resets daily)
ttm_sent.json         # TTM product handles sent today (resets daily)
```

### 3b. heartbeat.yml — BGG Monitor Heartbeat

- **Trigger**: Periodic (keep-alive)
- **What it does**: Prevents GitHub from disabling scheduled workflows due to inactivity

---

## 4. Deal Sources

### 4a. BGG Hot Deals (`monitor.py` + `bgg_api.py`)

- Polls the BGG Hot Deals forum RSS/API every 15 minutes
- Sends a WhatsApp alert for each new thread not already seen
- Deduplication tracked in `seen_threads.json`

### 4b. GameNerdz Deal of the Day (`gamenerdz_dotd.py`)

- Fetches `gamenerdz.com/deal-of-the-day` via requests + BeautifulSoup
- Runs the **full research pipeline** (see Section 6) for qualifying deals
- Deduplication tracked in `gamenerdz_sent.txt`

### 4c. Board Game Oracle Daily Price Drops (`bgo_pricedrop.py`)

**URL**: `https://www.boardgameoracle.com/pricedrop/daily`

**How it works**:
1. Fetches the page with `requests.get()` — works cleanly because the site is Next.js SSR
2. Extracts all game data from the embedded `__NEXT_DATA__` JSON (`props → pageProps → trpcState → queries → pages[0].items`)
3. Parses store names from SSR-rendered MUI `<span>` elements via BeautifulSoup (span index 3 in each card = store name)
4. Pre-filters to games with `bgg_rating >= 7.0` (using BGO's embedded rating)
5. For each qualifying new game: runs the full research pipeline and sends a WhatsApp alert

**Price calculations**:
- `was_price = lowest_price - price_drop_day_change_value` (day_change is negative, so subtraction gives the old price)
- `discount_pct = abs(price_drop_day_change_percent) * 100`

**Badges**:
- `is_lowest_52w` → "🏆 52-week low!"
- `is_lowest_30d` → "📉 30-day low!"

**Deduplication**: `bgo_sent.json` keyed by BGO game ID, resets daily.

**Source header in WhatsApp**: `🎲 *Board Game Oracle — Daily Price Drop*`

### 4d. Tabletop Merchant Deal of the Day (`ttm_dotd.py`)

**URL**: `https://tabletopmerchant.com/collections/deal-of-the-day/products.json` (Shopify JSON API)

**How it works**:
1. Fetches the Shopify collection JSON endpoint — clean structured data, no scraping
2. Takes `products[0]`, strips the `(DEAL OF THE DAY)` suffix from the title via regex
3. Gets deal price from `variants[0].price`, was-price from `variants[0].compare_at_price`
4. Looks up the clean game name on BGG; skips if rating < 7.0
5. Runs the full research pipeline and sends a WhatsApp alert

**Deduplication**: `ttm_sent.json` keyed by Shopify product `handle`, resets daily.
Below-threshold games (BGG < 7.0) are also marked sent to avoid re-researching every 15 minutes.

**Source header in WhatsApp**: `🏪 *Tabletop Merchant — Deal of the Day*`

---

## 5. Full Research Pipeline

All three DotD sources (GameNerdz, BGO, TTM) run the same full research pipeline for every qualifying deal, then format the result with `format_full_deal()`.

**Steps**:
1. **BGG lookup** — `bgg_api.search_game(name)` + `bgg_api.get_game_details(bgg_id)` → rating, rank, weight, best players, player range, BGG URL
2. **Current BGG marketplace listings** — `marketplace.get_current_listings(bgg_id, num_listings=5)` — for-sale listings in USA, cheapest first
3. **Recently sold BGG listings** — `marketplace.get_sold_listings(bgg_id, num_listings=5)` — sold listings in USA
4. **Retail prices** — `price_checker.get_all_prices(name, bgg_id)` — price check across US retail stores
5. **Community reviews** — `bgg_api.get_game_reviews(bgg_id)` → `{'positive': [...], 'negative': [...]}`

---

## 6. WhatsApp Formatting

### `format_full_deal()` — Shared Full-Detail Formatter

Located in `whatsapp_notifier.py`. Used by all three DotD sources (GameNerdz, BGO, TTM).

```python
def format_full_deal(
    source_header: str,      # e.g. '🎲 *Board Game Oracle — Daily Price Drop*'
    deal_price_line: str,    # e.g. '🏷 *Now: $24.99*  (was $39.99, -37%)  —  Amazon'
    deal_url: str,           # Link to the deal page
    game_details: dict,      # From bgg_api.get_game_details()
    sold_listings: list,     # From marketplace.get_sold_listings()
    current_listings: list,  # From marketplace.get_current_listings()
    retail_prices: list,     # From price_checker.get_all_prices()
    reviews: dict,           # From bgg_api.get_game_reviews()
) -> str:
```

**Output order**:
1. Source header (e.g. `🎲 *Board Game Oracle — Daily Price Drop*`)
2. Game name (bold)
3. BGG stats: rating, weight, best players, player range, BGG rank
4. Deal price line + URL
5. 🛒 Current BGG marketplace listings (for sale, USA)
6. 💸 Recently sold BGG prices (USA)
7. 🏬 Retail prices across stores
8. 👍 One positive community review
9. 👎 One negative community review

### `send_whatsapp(message)` / `send_image_whatsapp(image_source, caption)`

- `send_whatsapp` — sends a text message via Green API
- `send_image_whatsapp` — sends an image; if URL: uses `sendFileByUrl`; if local path: uses `sendFileByUpload`

---

## 7. Environment Variables / Secrets

Set in GitHub Actions secrets:

| Variable               | Purpose                                          |
|------------------------|--------------------------------------------------|
| GREEN_API_INSTANCE_ID  | Green API instance ID for WhatsApp               |
| GREEN_API_TOKEN        | Green API token                                  |
| WHATSAPP_PHONE         | Phone number to notify (digits only, no +)       |
| GMAIL_USER             | Gmail address for sending email alerts           |
| GMAIL_APP_PASSWORD     | Gmail App Password (not account password)        |
| ALERT_EMAIL            | Recipient email address                          |
| BGG_API_TOKEN          | BGG API token (if required)                      |

---

## 8. Cloudflare Worker (WhatsApp Trigger)

A Cloudflare Worker at `bgg-whatsapp-trigger` receives incoming WhatsApp messages via Green API webhook. When the user sends "go", it triggers `bgg-monitor.yml` via the GitHub Actions API using a `repository_dispatch` event with type `check-deals`. This causes `monitor.py --force` to run, re-sending all current qualifying deals.

---

## 9. Commit History (Key Changes)

| Commit    | Description                                                                    |
|-----------|--------------------------------------------------------------------------------|
| e1e21c2   | Add send_image_whatsapp() to whatsapp_notifier.py                              |
| dc6c3d8   | Add Playwright screenshot capture + WhatsApp send in gamenerdz_dotd.py         |
| 3702e41   | Fix indentation artifact at line 715 in gamenerdz_dotd.py                      |
| ec8570b   | Add thum.io fallback for GameNerdz DotD in monitor.py                          |
| (session) | Remove all Facebook monitoring (facebook_monitor.py deleted, FB workflow gone)  |
| (session) | Add Board Game Oracle daily price drop monitor (bgo_pricedrop.py)              |
| (session) | Add Tabletop Merchant Deal of the Day monitor (ttm_dotd.py)                    |
| (session) | Add format_full_deal() shared WhatsApp formatter to whatsapp_notifier.py       |
| (session) | Add full research pipeline to all DotD sources (current + sold listings, etc.) |
| (session) | Update bgg-monitor.yml cache to include bgo_sent.json + ttm_sent.json          |
| (session) | Remove Playwright from requirements.txt and bgg-monitor.yml                    |

---

## 10. Known Issues / Quirks

- **GitHub Actions scheduled workflows**: GitHub may disable scheduled workflows after 60 days of repo inactivity. The heartbeat workflow prevents this.
- **Green API sendFileByUrl**: Requires a publicly accessible URL. Local file paths must use `sendFileByUpload` instead.
- **BGO store name parsing**: Store names are extracted from SSR-rendered MUI `<span>` elements (span index 3 per card). If BGO changes their layout, this may break and fall back to `'Unknown'`.
- **TTM title format**: The regex `\s*\(DEAL OF THE DAY\)\s*` strips the suffix (case-insensitive). If Tabletop Merchant changes the suffix format, `clean_name` will fall back to the full title.
- **BGO `was_price` calculation**: Computed as `lowest_price - price_drop_day_change_value` (the day change value is negative). If BGO changes their JSON schema, this field may break.
- **Actions cache key**: Both restore and save use `monitor-state-latest` as the key. This means the cache is always overwritten, keeping only the latest state. Acceptable trade-off for a monitor bot.

---

## 11. Facebook Monitoring (REMOVED)

Facebook group monitoring was attempted but ultimately abandoned due to persistent session/cookie expiry issues that could not be reliably solved without interactive login. All related code has been removed:

- `facebook_monitor.py` — deleted
- `.github/workflows/facebook-monitor.yml` — deleted
- `playwright>=1.42.0` and `playwright-stealth>=1.0.6` — removed from `requirements.txt`
- Facebook-related secrets (`FB_EMAIL`, `FB_PASSWORD`, `FB_COOKIES`) — no longer used
