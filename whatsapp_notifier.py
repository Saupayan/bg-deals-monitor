"""
whatsapp_notifier.py
--------------------
Sends WhatsApp messages via Green API.

Green API docs: https://green-api.com/en/docs/api/sending/SendMessage/

Required env vars:
  GREEN_API_INSTANCE_ID    - your instance ID (e.g. 7107557070)
  GREEN_API_TOKEN          - your apiTokenInstance
  WHATSAPP_PHONE           - recipient phone number with country code, no + or spaces
                             e.g. 14155551234  (for +1 415 555 1234)
"""

import requests
import traceback
import config

# ─────────────────────────────────────────────────────────────────────────────
# GREEN API ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────
# Format: https://7107.api.greenapi.com/waInstance{id}/sendMessage/{token}
_BASE_URL = "https://7107.api.greenapi.com"


def send_whatsapp(message: str) -> bool:
    """
    Send a WhatsApp message to the configured phone number.
    Returns True if sent successfully, False otherwise.
    """
    instance_id = config.GREEN_API_INSTANCE_ID
    token       = config.GREEN_API_TOKEN
    phone       = config.WHATSAPP_PHONE

    if not all([instance_id, token, phone]):
        print("  WhatsApp: missing GREEN_API credentials — skipping")
        return False

    # Green API chat ID format: phonenumber@c.us
    chat_id = f"{phone}@c.us"

    url = f"{_BASE_URL}/waInstance{instance_id}/sendMessage/{token}"
    payload = {
        "chatId": chat_id,
        "message": message,
    }

    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("idMessage"):
                print(f"  WhatsApp sent ✓ (id: {data['idMessage']})")
                return True
            else:
                print(f"  WhatsApp: unexpected response: {data}")
                return False
        else:
            print(f"  WhatsApp: HTTP {resp.status_code} — {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"  WhatsApp: error — {e}")
        traceback.print_exc()
        return False


def send_deal_whatsapp(deals: list) -> bool:
    """
    Send a full-detail WhatsApp alert for one or more BGG Hot Deals.

    Each deal includes:
      - Game stats (rating, weight, best players, BGG rank)
      - BGG forum thread link (the actual deal post)
      - BGG game page link
      - Last sold prices from BGG marketplace
      - Current retail prices across stores
      - 1 positive + 1 negative community review snippet
    """
    if not deals:
        return False

    lines = []
    lines.append("🎲 *BGG Hot Deal Alert!*")

    for i, deal in enumerate(deals, 1):
        thread        = deal.get("thread", {})
        game          = deal.get("game_details") or {}
        sold_listings = deal.get("sold_listings", [])
        retail_prices = deal.get("retail_prices", [])
        reviews       = deal.get("reviews", {})
        subject       = thread.get("subject", "Unknown Deal")
        thread_id     = thread.get("id", "")

        name      = game.get("name") or subject
        rating    = game.get("average_rating", "N/A")
        weight    = game.get("weight", "N/A")
        best_at   = game.get("best_players", "N/A")
        bgg_rank  = game.get("bgg_rank", "")
        bgg_url   = game.get("bgg_url", "")
        thread_url = f"https://boardgamegeek.com/thread/{thread_id}" if thread_id else ""

        lines.append("")
        lines.append(f"*{i}. {name}*")

        # Stats row
        rank_str = f"  |  🏆 {bgg_rank}" if bgg_rank and bgg_rank != "Not ranked" else ""
        lines.append(f"⭐ {rating}/10  |  🧠 Weight: {weight}/5  |  👥 Best: {best_at}p{rank_str}")

        # Deal thread link (the actual forum post)
        num_replies = thread.get("num_articles", "")
        reply_str = f" ({num_replies} replies)" if num_replies and num_replies != "0" else ""
        lines.append(f"📋 _{subject}{reply_str}_")
        if thread_url:
            lines.append(f"🔗 {thread_url}")

        # BGG game page
        if bgg_url:
            lines.append(f"📊 {bgg_url}")

        # Last sold prices from BGG marketplace (up to 3)
        if sold_listings:
            lines.append("")
            lines.append("🏷️ *Last sold (BGG marketplace):*")
            for s in sold_listings[:3]:
                lines.append(f"  {s['price']} — {s['condition']} ({s['date_sold']})")
        else:
            lines.append("🏷️ No recent BGG marketplace sales found")

        # Retail prices (up to 5 stores)
        if retail_prices:
            lines.append("")
            lines.append("💰 *Retail prices:*")
            for p in retail_prices[:5]:
                lines.append(f"  {p['store']}: {p['price_str']}")
        else:
            lines.append("💰 No retail prices found")

        # Community reviews — 1 positive, 1 negative
        pos = (reviews.get("positive") or [])
        neg = (reviews.get("negative") or [])

        lines.append("")
        if pos:
            r = pos[0]
            rating_tag = f" ({r['rating']:.1f})" if r.get("rating") else ""
            snippet = r["text"][:150].rstrip() + ("…" if len(r["text"]) > 150 else "")
            lines.append(f"👍 \"{snippet}\" — {r['user']}{rating_tag}")
        if neg:
            r = neg[0]
            rating_tag = f" ({r['rating']:.1f})" if r.get("rating") else ""
            snippet = r["text"][:150].rstrip() + ("…" if len(r["text"]) > 150 else "")
            lines.append(f"👎 \"{snippet}\" — {r['user']}{rating_tag}")

        if i < len(deals):
            lines.append("")
            lines.append("─────────────────────")

    message = "\n".join(lines)
    return send_whatsapp(message)


def send_dotd_whatsapp(deal: dict) -> bool:
    """
    Send a compact WhatsApp alert for a GameNerdz Deal of the Day.
    """
    if not deal:
        return False

    game    = deal.get("game_details") or {}
    thread  = deal.get("thread", {})
    subject = thread.get("subject", "GameNerdz Deal of the Day")
    name    = game.get("name") or subject
    rating  = game.get("average_rating", "N/A")
    weight  = game.get("weight", "N/A")
    best_at = game.get("best_players", "N/A")
    bgg_url = game.get("bgg_url", "")
    dotd_price = deal.get("dotd_price", "")
    dotd_url   = deal.get("dotd_url", "")

    lines = [
        "🏪 *GameNerdz Deal of the Day!*",
        "",
        f"*{name}*",
        f"⭐ {rating}/10  |  🧠 Weight: {weight}/5  |  👥 Best: {best_at}p",
    ]
    if dotd_price:
        lines.append(f"💰 GameNerdz: {dotd_price}")
    if dotd_url:
        lines.append(f"🔗 {dotd_url}")
    if bgg_url:
        lines.append(f"📊 BGG: {bgg_url}")

    message = "\n".join(lines)
    return send_whatsapp(message)
