# PROJECT BIBLE — bg-deals-monitor
*Last updated: 2026-03-21*

---

## 1. What Is This?

A personal board game deal monitor that runs on GitHub Actions and sends WhatsApp (and email) notifications about:
- **BGG Hot Deals** — new threads on BoardGameGeek's Hot Deals forum
- **GameNerdz Deal of the Day (DotD)** — the daily discounted game on gamenerdz.com

---

## 2. Repository Structure

```
bg-deals-monitor/
├── .github/workflows/
│   ├── bgg-monitor.yml          # Main monitor: BGG hot deals + GN DotD (every 15 min)
│   ├── gamenerdz-dotd.yml       # Dedicated daily DotD checker with Playwright
│   └── heartbeat.yml            # Keep-alive / heartbeat workflow
├── monitor.py                   # Entry point for bgg-monitor.yml
├── gamenerdz_dotd.py            # Standalone GameNerdz DotD checker
├── whatsapp_notifier.py         # WhatsApp notification functions (Green API)
├── bgg_api.py                   # BoardGameGeek API wrapper
├── game_parser.py               # Game data parsing utilities
├── marketplace.py               # BGG marketplace scraper
├── price_checker.py             # Retail price checker (multiple stores)
├── emailer.py                   # Email notification via Gmail SMTP
├── config.py                    # Reads secrets from environment variables
├── requirements.txt             # Python dependencies
└── PROJECT_BIBLE.md             # This file
```

---

## 3. Workflows

### 3a. bgg-monitor.yml — Board Game Deals Monitor
- **Trigger**: Every 15 minutes (cron) + workflow_dispatch (manual)
- **What it does**: Runs monitor.py, which checks BGG hot deals and the GameNerdz DotD
- **Playwright**: Not available — uses requests/BeautifulSoup only
- **DotD fallback**: When GN DotD page can't be parsed, sends a thum.io screenshot via WhatsApp
- **WhatsApp trigger**: Also triggered by Cloudflare Worker when user sends "go" message

### 3b. gamenerdz-dotd.yml — GameNerdz Deal of the Day
- **Trigger**: Daily at 1:30 PM ET + workflow_dispatch (manual)
- **What it does**: Runs gamenerdz_dotd.py --test using Playwright to render the JS-heavy DotD page
- **Playwright**: Full Playwright/Chromium — takes an actual browser screenshot
- **Screenshot**: Always saves /tmp/gn_dotd.png, sends via sendFileByUpload (Green API)
- **State file**: Tracks last-sent DotD to avoid duplicate messages

### 3c. heartbeat.yml — BGG Monitor Heartbeat
- **Trigger**: Periodic (keep-alive)
- **What it does**: Prevents GitHub from disabling scheduled workflows due to inactivity

---

## 4. Key Files

### whatsapp_notifier.py
Central WhatsApp notification module using Green API.

Key functions:
- send_whatsapp(message) — sends a text message
- send_image_whatsapp(image_source, caption) — sends an image
  - If image_source starts with http: uses sendFileByUrl (Green API JSON endpoint)
  - If image_source is a local path: uses sendFileByUpload (multipart POST)

Config vars needed: GREEN_API_INSTANCE_ID, GREEN_API_TOKEN, WHATSAPP_PHONE

### gamenerdz_dotd.py
Standalone checker for GameNerdz Deal of the Day. Uses Playwright by default.

Key function: check_gamenerdz_dotd(use_playwright=True)
- Fetches DotD page via Playwright (headless Chromium)
- Takes a screenshot: page.screenshot(path='/tmp/gn_dotd.png', full_page=False)
- Attempts to parse product name + price from rendered HTML
- Regardless of parse success/failure, sends screenshot via WhatsApp if file exists
- If parse succeeds: caption includes product name + price
- If parse fails: caption says "Couldn't parse product details — here's the live page screenshot"

### monitor.py
Main entry point for the bgg-monitor.yml workflow. Checks BGG hot deals and GameNerdz DotD.

GameNerdz DotD handling (no Playwright):
- Attempts to parse DotD via requests + BeautifulSoup
- If parsing fails, sends a thum.io screenshot as fallback:
    thum_url = "https://image.thum.io/get/noanimate/https://www.gamenerdz.com/deal-of-the-day"
    whatsapp_notifier.send_image_whatsapp(thum_url, caption)
- thum.io is a free screenshot-as-a-service — no API key needed

---

## 5. Screenshot Fallback Architecture

Two fallback mechanisms depending on whether Playwright is available:

| Workflow            | Playwright | Screenshot Method                        | Green API Call    |
|---------------------|------------|------------------------------------------|-------------------|
| gamenerdz-dotd.yml  | Yes        | page.screenshot() -> /tmp/gn_dotd.png   | sendFileByUpload  |
| bgg-monitor.yml     | No         | thum.io URL                              | sendFileByUrl     |

thum.io URL format: https://image.thum.io/get/noanimate/{url}

---

## 6. Environment Variables / Secrets

Set in GitHub Actions secrets (and Heroku config vars for the web dyno):

| Variable               | Purpose                                          |
|------------------------|--------------------------------------------------|
| GREEN_API_INSTANCE_ID  | Green API instance ID for WhatsApp               |
| GREEN_API_TOKEN        | Green API token                                  |
| WHATSAPP_PHONE         | Phone number to notify (digits only, no +)       |
| GMAIL_USER             | Gmail address for sending email alerts           |
| GMAIL_APP_PASSWORD     | Gmail App Password (not account password)        |
| ALERT_EMAIL            | Recipient email address                          |

---

## 7. Cloudflare Worker (WhatsApp Trigger)

A Cloudflare Worker at bgg-whatsapp-trigger receives incoming WhatsApp messages via Green API webhook. When the user sends "go", it triggers the bgg-monitor.yml workflow via the GitHub Actions API (workflow_dispatch event).

---

## 8. Commit History (Key Changes)

| Commit    | Description                                                          |
|-----------|----------------------------------------------------------------------|
| e1e21c2   | Add send_image_whatsapp() to whatsapp_notifier.py                   |
| dc6c3d8   | Add Playwright screenshot capture + WhatsApp send in gamenerdz_dotd.py |
| 3702e41   | Fix indentation artifact at line 715 in gamenerdz_dotd.py           |
| ec8570b   | Add thum.io fallback for GameNerdz DotD in monitor.py               |

---

## 9. Known Issues / Quirks

- **GameNerdz DotD parsing**: The DotD page is heavily JS-rendered. Even with Playwright, product details sometimes can't be parsed (GraphQL 401, JSON-LD missing). The screenshot fallback handles this gracefully — you always get at least a visual of the page.
- **GitHub Actions scheduled workflows**: GitHub may disable scheduled workflows after 60 days of repo inactivity. The heartbeat workflow prevents this.
- **Green API sendFileByUrl**: Requires a publicly accessible URL. thum.io URLs work well for this. Local file paths must use sendFileByUpload instead.
- **Playwright in CI**: Install step takes ~23s on GitHub Actions (chromium download). This is normal.
