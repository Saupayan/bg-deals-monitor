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

# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# GREEN API ENDPOINT
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
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
        print("  WhatsApp: missing GREEN_API credentials â skipping")
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
                print(f"  WhatsApp sent â (id: {data['idMessage']})")
                return True
            else:
                print(f"  WhatsApp: unexpected response: {data}")
                return False
        else:
            print(f"  WhatsApp: HTTP {resp.status_code} â {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"  WhatsApp: error â {e}")
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
    lines.append("ð² *BGG Hot Deal Alert!*")

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
        rank_str = f"  |  ð {bgg_rank}" if bgg_rank and bgg_rank != "Not ranked" else ""
        lines.append(f"â­ {rating}/10  |  ð§  Weight: {weight}/5  |  ð¥ Best: {best_at}p{rank_str}")

        # Deal thread link (the actual forum post)
        num_replies = thread.get("num_articles", "")
        reply_str = f" ({num_replies} replies)" if num_replies and num_replies != "0" else ""
        lines.append(f"ð _{subject}{reply_str}_")
        if thread_url:
            lines.append(f"ð {thread_url}")

        # BGG game page
        if bgg_url:
            lines.append(f"ð {bgg_url}")

        # Last sold prices from BGG marketplace (up to 3)
        if sold_listings:
            lines.append("")
            lines.append("ð·ï¸ *Last sold (BGG marketplace):*")
            for s in sold_listings[:3]:
                lines.append(f"  {s['price']} â {s['condition']} ({s['date_sold']})")
        else:
            lines.append("ð·ï¸ No recent BGG marketplace sales found")

        # Retail prices (up to 5 stores)
        if retail_prices:
            lines.append("")
            lines.append("ð° *Retail prices:*")
            for p in retail_prices[:5]:
                lines.append(f"  {p['store']}: {p['price_str']}")
        else:
            lines.append("ð° No retail prices found")

        # Community reviews â 1 positive, 1 negative
        pos = (reviews.get("positive") or [])
        neg = (reviews.get("negative") or [])

        lines.append("")
        if pos:
            r = pos[0]
            rating_tag = f" ({r['rating']:.1f})" if r.get("rating") else ""
            snippet = r["text"][:150].rstrip() + ("â¦" if len(r["text"]) > 150 else "")
            lines.append(f"ð \"{snippet}\" â {r['user']}{rating_tag}")
        if neg:
            r = neg[0]
            rating_tag = f" ({r['rating']:.1f})" if r.get("rating") else ""
            snippet = r["text"][:150].rstrip() + ("â¦" if len(r["text"]) > 150 else "")
            lines.append(f"ð \"{snippet}\" â {r['user']}{rating_tag}")

        if i < len(deals):
            lines.append("")
            lines.append("âââââââââââââââââââââ")

    message = "\n".join(lines)
    return send_whatsapp(message)


def format_full_deal(
    source_header: str,
    deal_price_line: str,
    deal_url: str,
    game_details: dict,
    sold_listings: list,
    current_listings: list,
    retail_prices: list,
    reviews: dict,
) -> str:
    """
    Build a full-detail WhatsApp message for any deal source (TTM, BGO, GameNerdz, etc.).

    Includes:
      - Source header + deal price line
      - BGG stats: rating, ranking, weight, player range, best player count
      - BGG marketplace: current USA listings (cheapest first)
      - BGG marketplace: recently sold USA prices
      - Retail store prices
      - 1 positive + 1 negative community review
    """
    game    = game_details or {}
    name    = game.get('name', 'Unknown')
    rating  = game.get('average_rating', 'N/A')
    weight  = game.get('weight', 'N/A')
    best_at = game.get('best_players', '')
    bgg_rank = game.get('bgg_rank', '')
    bgg_url  = game.get('bgg_url', '')
    min_p    = game.get('min_players', '')
    max_p    = game.get('max_players', '')

    # Player range string
    if min_p and max_p and min_p != max_p:
        player_range = f"{min_p}–{max_p}"
    elif min_p:
        player_range = str(min_p)
    else:
        player_range = ''

    rank_str   = f"  |  🏆 Rank: {bgg_rank}" if bgg_rank and bgg_rank != 'Not ranked' else ''
    best_str   = f"  |  👥 Best: {best_at}p" if best_at else ''
    range_str  = f"  ({player_range}p range)" if player_range else ''

    lines = [
        source_header,
        '',
        f'*{name}*',
        f'⭐ BGG: {rating}/10  |  📊 Weight: {weight}/5{best_str}{range_str}{rank_str}',
        '',
        deal_price_line,
    ]
    if deal_url:
        lines.append(f'🔗 {deal_url}')
    if bgg_url:
        lines.append(f'📋 BGG: {bgg_url}')

    # Current BGG marketplace listings (for sale, USA, cheapest first)
    if current_listings:
        lines.append('')
        lines.append('🛒 *BGG Marketplace — current listings (USA):*')
        for c in current_listings[:5]:
            cond   = c.get('condition', '?')
            price  = c.get('price', '?')
            seller = c.get('seller', '')
            lines.append(f'  • {cond}: {price}' + (f'  (@{seller})' if seller else ''))
    else:
        lines.append('')
        lines.append('🛒 BGG Marketplace: no current USA listings.')

    # Recently sold BGG marketplace listings
    if sold_listings:
        lines.append('')
        lines.append('💸 *BGG Marketplace — recently sold (USA):*')
        for s in sold_listings[:5]:
            cond  = s.get('condition', '?')
            price = s.get('price', '?')
            d     = s.get('date') or s.get('date_sold', '')
            date_short = d[:7] if d else ''
            lines.append(f'  • {cond}: {price}' + (f'  ({date_short})' if date_short else ''))
    else:
        lines.append('')
        lines.append('💸 BGG Marketplace: no recent USA sold listings.')

    # Retail store prices
    if retail_prices:
        lines.append('')
        lines.append('🏬 *Retail prices (USA):*')
        for p in retail_prices[:6]:
            lines.append(f'  • {p["store"]}: {p["price_str"]}')
    else:
        lines.append('')
        lines.append('🏬 No retail prices found.')

    # Community reviews — 1 positive, 1 negative
    pos = (reviews or {}).get('positive') or []
    neg = (reviews or {}).get('negative') or []
    if pos or neg:
        lines.append('')
    if pos:
        r       = pos[0]
        rtag    = f' ({r["rating"]:.1f})' if r.get('rating') else ''
        snippet = r['text'][:160].rstrip() + ('…' if len(r['text']) > 160 else '')
        lines.append(f'👍 "{snippet}" — {r["user"]}{rtag}')
    if neg:
        r       = neg[0]
        rtag    = f' ({r["rating"]:.1f})' if r.get('rating') else ''
        snippet = r['text'][:160].rstrip() + ('…' if len(r['text']) > 160 else '')
        lines.append(f'👎 "{snippet}" — {r["user"]}{rtag}')

    return '\n'.join(lines)


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
        "ðª *GameNerdz Deal of the Day!*",
        "",
        f"*{name}*",
        f"â­ {rating}/10  |  ð§  Weight: {weight}/5  |  ð¥ Best: {best_at}p",
    ]
    if dotd_price:
        lines.append(f"ð° GameNerdz: {dotd_price}")
    if dotd_url:
        lines.append(f"ð {dotd_url}")
    if bgg_url:
        lines.append(f"ð BGG: {bgg_url}")

    message = "\n".join(lines)
    return send_whatsapp(message)


def send_image_whatsapp(image_source: str, caption: str = "") -> bool:
    """
    Send an image via WhatsApp using Green API.

    image_source:
      - A URL (starts with http/https) â uses sendFileByUrl (no upload needed)
      - A local file path               â uses sendFileByUpload (multipart POST)

    caption: optional text shown below the image in WhatsApp.
    Returns True if sent successfully, False otherwise.

    Green API docs:
      sendFileByUrl:    https://green-api.com/en/docs/api/sending/SendFileByUrl/
      sendFileByUpload: https://green-api.com/en/docs/api/sending/SendFileByUpload/
    """
    import os

    instance_id = config.GREEN_API_INSTANCE_ID
    token       = config.GREEN_API_TOKEN
    phone       = config.WHATSAPP_PHONE

    if not all([instance_id, token, phone]):
        print("  WhatsApp: missing GREEN_API credentials â skipping image send")
        return False

    chat_id = f"{phone}@c.us"

    # ââ URL-based image (e.g. thum.io screenshot, product image URL) ââââââââââ
    if image_source.startswith('http://') or image_source.startswith('https://'):
        url = f"{_BASE_URL}/waInstance{instance_id}/sendFileByUrl/{token}"
        payload = {
            "chatId":   chat_id,
            "urlFile":  image_source,
            "fileName": "gn_dotd.png",
            "caption":  caption,
        }
        try:
            resp = requests.post(url, json=payload, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("idMessage"):
                    print(f"  WhatsApp image (URL) sent â (id: {data['idMessage']})")
                    return True
                else:
                    print(f"  WhatsApp image: unexpected response: {data}")
                    return False
            else:
                print(f"  WhatsApp image: HTTP {resp.status_code} â {resp.text[:200]}")
                return False
        except Exception as e:
            print(f"  WhatsApp image: error â {e}")
            traceback.print_exc()
            return False

    # ââ Local file (e.g. Playwright screenshot at /tmp/gn_dotd.png) ââââââââââ
    else:
        if not os.path.exists(image_source):
            print(f"  WhatsApp image: file not found: {image_source}")
            return False

        url = f"{_BASE_URL}/waInstance{instance_id}/sendFileByUpload/{token}"
        try:
            with open(image_source, 'rb') as f:
                files = {'file': (os.path.basename(image_source), f, 'image/png')}
                data  = {'chatId': chat_id, 'caption': caption}
                resp  = requests.post(url, data=data, files=files, timeout=60)

            if resp.status_code == 200:
                result = resp.json()
                if result.get("idMessage"):
                    print(f"  WhatsApp image (upload) sent â (id: {result['idMessage']})")
                    return True
                else:
                    print(f"  WhatsApp image: unexpected response: {result}")
                    return False
            else:
                print(f"  WhatsApp image: HTTP {resp.status_code} â {resp.text[:200]}")
                return False
        except Exception as e:
            print(f"  WhatsApp image: error â {e}")
            traceback.print_exc()
            return False
