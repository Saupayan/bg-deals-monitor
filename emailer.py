"""
emailer.py
----------
Formats and sends deal alert emails via Gmail SMTP.

Two modes:
  send_consolidated_alert(deals)  -- ONE email with ALL deals from a check cycle
  send_deal_alert(...)            -- single-deal email (used for GameNerdz DotD)
"""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from typing import List, Dict, Optional
from datetime import datetime

import config


# ─────────────────────────────────────────────────────────────────────────────
# SMTP HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _send(subject: str, html_body: str, text_body: str) -> bool:
    if not config.GMAIL_USER or not config.GMAIL_APP_PASSWORD:
        print("    Gmail credentials missing in .env — cannot send email.")
        return False
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = config.GMAIL_USER
    msg['To']      = config.ALERT_EMAIL
    msg.attach(MIMEText(text_body, 'plain'))
    msg.attach(MIMEText(html_body, 'html'))
    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.ehlo()
            server.starttls()
            server.login(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
            server.sendmail(config.GMAIL_USER, config.ALERT_EMAIL, msg.as_string())
        print(f"    Email sent to {config.ALERT_EMAIL}")
        return True
    except smtplib.SMTPAuthenticationError:
        print("    Gmail auth failed. Check GMAIL_USER and GMAIL_APP_PASSWORD in .env")
        return False
    except Exception as e:
        print(f"    Failed to send email: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# CONSOLIDATED EMAIL  (one email, multiple deals)
# ─────────────────────────────────────────────────────────────────────────────

def send_consolidated_alert(deals: List[Dict]) -> bool:
    """
    Send ONE email containing all deals found in a single check cycle.

    Each item in `deals` is a dict with keys:
      thread, game_details, sold_listings, retail_prices, reviews
    """
    if not deals:
        return False

    count = len(deals)
    names = [(d.get('game_details') or {}).get('name') or d['thread']['subject'] for d in deals]
    subject = f"🎲 {count} New Deal{'s' if count > 1 else ''}: {', '.join(names[:3])}"
    if count > 3:
        subject += f" +{count - 3} more"

    sections_html = []
    sections_text = []

    for i, deal in enumerate(deals, 1):
        sections_html.append(_deal_section_html(deal, i, count))
        sections_text.append(_deal_section_text(deal, i))

    divider = '<hr style="border:none;border-top:2px solid #e0e0e0;margin:40px 0">'

    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:700px;margin:0 auto;padding:20px;color:#222;background:#fff">

<div style="background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%);border-radius:12px;padding:20px 24px;margin-bottom:28px">
  <div style="font-size:12px;color:#aaa;margin-bottom:4px;text-transform:uppercase;letter-spacing:1px">Board Game Deals Monitor</div>
  <div style="font-size:20px;color:#fff;font-weight:bold">{count} New Deal{'s' if count > 1 else ''} Found</div>
  <div style="font-size:12px;color:#888;margin-top:4px">{datetime.now().strftime('%B %d, %Y at %I:%M %p').replace(' 0', ' ')}</div>
</div>

{divider.join(sections_html)}

<div style="margin-top:32px;padding-top:16px;border-top:1px solid #eee;font-size:11px;color:#aaa;text-align:center">
  Board Game Deals Monitor · {datetime.now().strftime('%b %d, %Y at %I:%M %p').replace(' 0', ' ')}
</div>
</body></html>"""

    text_body = f"BOARD GAME DEALS — {count} New Deal{'s' if count > 1 else ''}\n{'='*60}\n\n" + \
                "\n\n".join(sections_text)

    return _send(subject, html_body, text_body)


# ─────────────────────────────────────────────────────────────────────────────
# SINGLE DEAL EMAIL  (kept for backwards compat; prefer send_consolidated_alert)
# ─────────────────────────────────────────────────────────────────────────────

def send_deal_alert(
    thread: Dict,
    game_details: Optional[Dict],
    sold_listings: List[Dict],
    retail_prices: List[Dict],
    reviews: Dict,
    tag: str = "New Deal",
) -> bool:
    deal = dict(thread=thread, game_details=game_details,
                sold_listings=sold_listings, retail_prices=retail_prices,
                reviews=reviews)
    name    = (game_details or {}).get('name', '') or thread['subject']
    subject = f"🎲 {tag}: {name}"

    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:700px;margin:0 auto;padding:20px;color:#222;background:#fff">
{_deal_section_html(deal, 1, 1)}
<div style="margin-top:32px;padding-top:16px;border-top:1px solid #eee;font-size:11px;color:#aaa;text-align:center">
  Board Game Deals Monitor · {datetime.now().strftime('%b %d, %Y at %I:%M %p').replace(' 0', ' ')}
</div>
</body></html>"""

    text_body = _deal_section_text(deal, 1)
    return _send(subject, html_body, text_body)


# ─────────────────────────────────────────────────────────────────────────────
# DEAL SECTION BUILDER  (shared by both consolidated and single)
# ─────────────────────────────────────────────────────────────────────────────

def _deal_section_html(deal: Dict, index: int, total: int) -> str:
    thread        = deal['thread']
    gd            = deal.get('game_details') or {}
    sold_listings = deal.get('sold_listings', [])
    retail_prices = deal.get('retail_prices', [])
    reviews       = deal.get('reviews', {})

    name         = gd.get('name', thread['subject'])
    bgg_url      = gd.get('bgg_url', 'https://boardgamegeek.com/forum/10/bgg/hot-deals')
    deal_url     = thread.get('deal_url') or (f"https://boardgamegeek.com/thread/{thread['id']}" if thread.get('id') else '#')
    rating       = gd.get('average_rating', 'N/A')
    weight       = gd.get('weight', 'N/A')
    best_players = gd.get('best_players', 'Unknown')
    bgg_rank     = gd.get('bgg_rank', 'Not ranked')
    num_ratings  = gd.get('num_ratings', 0)
    year         = gd.get('year', '')
    min_p        = gd.get('min_players', '?')
    max_p        = gd.get('max_players', '?')
    playtime     = gd.get('playtime', '?')
    description  = gd.get('description', '')

    rating_color = '#27ae60' if isinstance(rating, float) and rating >= 7.5 else \
                   '#e67e22' if isinstance(rating, float) and rating >= 6.0 else '#e74c3c'

    counter = f'<div style="font-size:11px;color:#888;margin-bottom:6px">Deal {index} of {total}</div>' if total > 1 else ''

    # Stats rows
    weight_label = 'Light' if isinstance(weight, float) and weight < 2 else \
                   'Medium' if isinstance(weight, float) and weight < 3.5 else 'Heavy'
    stats_rows = ''.join(f'''
    <tr>
      <td style="padding:5px 12px;color:#666;font-size:13px;white-space:nowrap">{lbl}</td>
      <td style="padding:5px 12px;font-size:13px">{val}</td>
    </tr>''' for lbl, val in [
        ('BGG Rating',  f'<span style="color:{rating_color};font-weight:bold">{rating}/10</span> ({num_ratings:,} ratings)' if num_ratings else f'{rating}/10'),
        ('BGG Rank',    bgg_rank),
        ('Complexity',  f'{weight}/5 ({weight_label})'),
        ('Best At',     f'{best_players} players'),
        ('Players',     f'{min_p}–{max_p}'),
        ('Play Time',   f'{playtime} min'),
        ('Year',        year),
    ])

    # Marketplace
    if sold_listings:
        mkt_rows = ''.join(f'''
        <tr>
          <td style="padding:5px 10px;font-size:13px">{s.get("date_sold","?")}</td>
          <td style="padding:5px 10px;font-size:13px;font-weight:bold;color:#27ae60">{s.get("price","?")}</td>
          <td style="padding:5px 10px;font-size:13px">{s.get("condition","?")}</td>
          <td style="padding:5px 10px;font-size:13px;color:#888">{s.get("seller","?")}</td>
        </tr>''' for s in sold_listings)
        marketplace_html = f'''
<h3 style="margin:22px 0 8px;color:#333;font-size:15px">📦 BGG Marketplace — Last {len(sold_listings)} USA Sales</h3>
<table style="width:100%;border-collapse:collapse;background:#f9f9f9;border-radius:8px;overflow:hidden">
  <thead><tr style="background:#ecf0f1">
    <th style="padding:6px 10px;text-align:left;font-size:12px;color:#666">Date Sold</th>
    <th style="padding:6px 10px;text-align:left;font-size:12px;color:#666">Price</th>
    <th style="padding:6px 10px;text-align:left;font-size:12px;color:#666">Condition</th>
    <th style="padding:6px 10px;text-align:left;font-size:12px;color:#666">Seller</th>
  </tr></thead>
  <tbody>{mkt_rows}</tbody>
</table>'''
    else:
        marketplace_html = '<p style="color:#aaa;font-size:13px;margin-top:16px">No recent BGG marketplace USA sales found.</p>'

    # Retail prices
    if retail_prices:
        price_rows = ''.join(f'''
        <tr>
          <td style="padding:5px 10px;font-size:13px">
            <a href="{p.get("url","#")}" style="color:#2980b9;text-decoration:none">{p["store"]}</a>
          </td>
          <td style="padding:5px 10px;font-size:13px;font-weight:bold">{p.get("price_str","?")}</td>
          <td style="padding:5px 10px;font-size:12px">{"<span style='color:#27ae60'>✓ In Stock</span>" if p.get("in_stock",True) else "<span style='color:#e74c3c'>✗ Out of Stock</span>"}</td>
        </tr>''' for p in retail_prices[:12])
        prices_html = f'''
<h3 style="margin:22px 0 8px;color:#333;font-size:15px">🛒 Current Retail Prices (USA)</h3>
<table style="width:100%;border-collapse:collapse;background:#f9f9f9;border-radius:8px;overflow:hidden">
  <thead><tr style="background:#ecf0f1">
    <th style="padding:6px 10px;text-align:left;font-size:12px;color:#666">Store</th>
    <th style="padding:6px 10px;text-align:left;font-size:12px;color:#666">Price</th>
    <th style="padding:6px 10px;text-align:left;font-size:12px;color:#666">Stock</th>
  </tr></thead>
  <tbody>{price_rows}</tbody>
</table>'''
    else:
        prices_html = '<p style="color:#aaa;font-size:13px;margin-top:16px">Could not retrieve current retail prices.</p>'

    # Reviews
    def review_cards(entries, color):
        if not entries:
            return '<p style="color:#aaa;font-size:13px">None found.</p>'
        cards = ''
        for r in entries:
            rating_badge = f' · rated {r["rating"]}/10' if r.get('rating') is not None else f' · {r.get("source","")}'
            cards += f'''<div style="background:#fafafa;border-left:3px solid {color};padding:8px 12px;margin:5px 0;border-radius:0 6px 6px 0">
              <div style="font-size:11px;color:#999;margin-bottom:3px"><strong>{r["user"]}</strong>{rating_badge}</div>
              <div style="font-size:13px;color:#444;line-height:1.5">{r["text"]}</div>
            </div>'''
        return cards

    reviews_html = f'''
<h3 style="margin:22px 0 6px;color:#333;font-size:15px">👍 What People Love</h3>
{review_cards(reviews.get("positive",[]), "#27ae60")}
<h3 style="margin:18px 0 6px;color:#333;font-size:15px">👎 Common Complaints</h3>
{review_cards(reviews.get("negative",[]), "#e74c3c")}'''

    return f'''
{counter}
<div style="background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%);border-radius:10px;padding:20px 24px;margin-bottom:16px">
  <h2 style="margin:0 0 6px;color:#fff;font-size:20px">{name}</h2>
  <div style="font-size:13px;color:#aab">
    By <strong style="color:#ddd">{thread.get("author","?")}</strong>
    &nbsp;·&nbsp; <a href="{deal_url}" style="color:#74b9ff;text-decoration:none">View Deal ↗</a>
    &nbsp;·&nbsp; <a href="{bgg_url}" style="color:#74b9ff;text-decoration:none">BGG Page ↗</a>
  </div>
  <div style="font-size:12px;color:#666;margin-top:5px">"{thread.get("subject","")}"</div>
</div>

<h3 style="margin:0 0 8px;color:#333;font-size:15px">📊 BGG Stats</h3>
<table style="border-collapse:collapse;background:#f9f9f9;border-radius:8px;overflow:hidden">
  <tbody>{stats_rows}</tbody>
</table>
{"<p style='margin:10px 0;font-size:13px;color:#555;line-height:1.6'>" + description + "</p>" if description else ""}
{marketplace_html}
{prices_html}
{reviews_html}'''


def _deal_section_text(deal: Dict, index: int) -> str:
    thread        = deal['thread']
    gd            = deal.get('game_details') or {}
    sold_listings = deal.get('sold_listings', [])
    retail_prices = deal.get('retail_prices', [])
    reviews       = deal.get('reviews', {})

    name = gd.get('name', thread['subject'])
    lines = [
        f"[{index}] {name}",
        f"Deal: {thread.get('deal_url') or (('https://boardgamegeek.com/thread/' + thread['id']) if thread.get('id') else '#')}",
        f"BGG:  {gd.get('bgg_url','')}",
        f"Rating: {gd.get('average_rating','N/A')}/10  |  Weight: {gd.get('weight','N/A')}/5  |  Best at: {gd.get('best_players','?')}p  |  Rank: {gd.get('bgg_rank','?')}",
        "",
    ]
    if sold_listings:
        lines.append("--- Last BGG USA Sales ---")
        for s in sold_listings:
            lines.append(f"  {s.get('date_sold','?')}  {s.get('price','?')}  [{s.get('condition','?')}]")
        lines.append("")
    if retail_prices:
        lines.append("--- Current Retail Prices ---")
        for p in retail_prices[:10]:
            stock = "In Stock" if p.get('in_stock', True) else "Out of Stock"
            lines.append(f"  {p['store']:<30}  {p.get('price_str','?'):<10}  {stock}")
        lines.append("")
    if reviews.get('positive'):
        lines.append("--- What People Love ---")
        for r in reviews['positive']:
            lines.append(f"  {r['user']}: {r['text'][:150]}")
        lines.append("")
    if reviews.get('negative'):
        lines.append("--- Common Complaints ---")
        for r in reviews['negative']:
            lines.append(f"  {r['user']}: {r['text'][:150]}")
        lines.append("")
    return "\n".join(lines)
