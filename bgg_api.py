"""
bgg_api.py
----------
Wrapper around the BGG XML API v2.
Handles:
  - Forum thread listings (Hot Deals)
  - Game search by name
  - Game details (rating, weight, best player count, rank)
  - Game reviews / community comments (rating-comments + forum reviews)

BGG XML API docs: https://boardgamegeek.com/wiki/page/BGG_XML_API2
Key behaviour: BGG sometimes returns HTTP 202 ("we're processing your request,
try again in a moment"). We handle this with automatic retries.
"""

import time
import xml.etree.ElementTree as ET
from typing import Optional, Dict, List

import requests
import config


BGG_API_BASE = "https://boardgamegeek.com/xmlapi2"

HEADERS = {
    'User-Agent': 'BGGDealMonitor/1.0 (personal use)',
    'Accept':     'application/xml, text/xml',
    'Authorization': f'Bearer {config.BGG_API_TOKEN}',
}


# -----------------------------------------------------------------------------
# LOW-LEVEL REQUEST
# -----------------------------------------------------------------------------

def _bgg_get(url: str, params: dict = None, max_retries: int = 6) -> Optional[ET.Element]:
    """
    Make a GET request to the BGG XML API.
    Automatically retries on HTTP 202 (queued) and 429 (rate limited).
    Returns the parsed XML root element, or None on failure.
    """
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=30)

            if resp.status_code == 200:
                return ET.fromstring(resp.content)

            elif resp.status_code == 202:
                wait = 5 * (attempt + 1)
                print(f"    BGG processing... retry {attempt + 1}/{max_retries} in {wait}s")
                time.sleep(wait)

            elif resp.status_code == 429:
                print(f"    BGG rate limit hit. Waiting 60s...")
                time.sleep(60)

            else:
                print(f"    BGG returned HTTP {resp.status_code} for {url}")
                return None

        except requests.exceptions.RequestException as e:
            print(f"    Network error: {e}")
            time.sleep(5)

    print(f"    Gave up after {max_retries} attempts: {url}")
    return None


# -----------------------------------------------------------------------------
# FORUM THREADS
# -----------------------------------------------------------------------------

def get_forum_threads(forum_id: int = 10, page: int = 1) -> List[Dict]:
    """
    Return a list of thread dicts from the specified BGG forum.
    Default forum_id=10 is the main Hot Deals forum.
    Each dict: id, subject, author, post_date, last_post_date, num_articles
    """
    root = _bgg_get(f"{BGG_API_BASE}/forum", {'id': forum_id, 'page': page})
    if root is None:
        return []

    threads_elem = root.find('threads')
    if threads_elem is None:
        return []

    threads = []
    for t in threads_elem.findall('thread'):
        threads.append({
            'id':             t.get('id', ''),
            'subject':        t.get('subject', ''),
            'author':         t.get('author', ''),
            'post_date':      t.get('postdate', ''),
            'last_post_date': t.get('lastpostdate', ''),
            'num_articles':   t.get('numarticles', '0'),
        })
    return threads


# -----------------------------------------------------------------------------
# GAME SEARCH
# -----------------------------------------------------------------------------

def search_game(game_name: str) -> Optional[str]:
    """
    Search BGG for a board game by name.
    Returns the BGG ID (as a string) of the best match, or None.
    """
    root = _bgg_get(f"{BGG_API_BASE}/search",
                    {'query': game_name, 'type': 'boardgame'})
    if root is None:
        return None

    items = root.findall('item')
    if not items:
        return None

    name_lower = game_name.lower().strip()

    # Pass 1: exact primary-name match
    for item in items:
        name_elem = item.find('name')
        if name_elem is not None:
            if name_elem.get('value', '').lower().strip() == name_lower:
                return item.get('id')

    # Pass 2: first item with a year published
    for item in items:
        if item.find('yearpublished') is not None:
            return item.get('id')

    # Pass 3: first result
    return items[0].get('id')


# -----------------------------------------------------------------------------
# GAME DETAILS
# -----------------------------------------------------------------------------

def get_game_details(bgg_id: str) -> Optional[Dict]:
    """
    Fetch full game info for a given BGG ID.
    Returns a dict with keys:
      id, name, year, description,
      min_players, max_players, playtime, best_players,
      average_rating, weight, num_ratings, bgg_rank, bgg_url
    """
    root = _bgg_get(f"{BGG_API_BASE}/thing", {'id': bgg_id, 'stats': 1})
    if root is None:
        return None

    item = root.find('item')
    if item is None:
        return None

    result = {
        'id':      bgg_id,
        'bgg_url': f"https://boardgamegeek.com/boardgame/{bgg_id}",
    }

    # Primary name
    result['name'] = ''
    for n in item.findall('name'):
        if n.get('type') == 'primary':
            result['name'] = n.get('value', '')
            break

    # Year published
    y = item.find('yearpublished')
    result['year'] = y.get('value', 'Unknown') if y is not None else 'Unknown'

    # Description (first 500 chars, cleaned up)
    d = item.find('description')
    if d is not None and d.text:
        desc = d.text.replace('&#10;', ' ').replace('&mdash;', '-').strip()
        result['description'] = (desc[:500] + '...') if len(desc) > 500 else desc
    else:
        result['description'] = ''

    # Player counts and play time
    for key, tag in [('min_players', 'minplayers'),
                     ('max_players', 'maxplayers'),
                     ('playtime',    'playingtime')]:
        e = item.find(tag)
        result[key] = e.get('value', '?') if e is not None else '?'

    # Best player count (from community poll)
    result['best_players'] = _best_player_count(item)

    # Default stats
    result.update({'average_rating': 0.0, 'weight': 0.0,
                   'num_ratings': 0, 'bgg_rank': 'Not ranked'})

    # Stats block
    stats = item.find('statistics')
    if stats is not None:
        ratings = stats.find('ratings')
        if ratings is not None:
            for key, tag in [('average_rating', 'average'),
                              ('weight',         'averageweight')]:
                e = ratings.find(tag)
                if e is not None:
                    try:
                        result[key] = round(float(e.get('value', 0)), 2)
                    except ValueError:
                        pass

            nr = ratings.find('usersrated')
            if nr is not None:
                try:
                    result['num_ratings'] = int(nr.get('value', 0))
                except ValueError:
                    pass

            # Overall BGG rank
            ranks = ratings.find('ranks')
            if ranks is not None:
                for rank in ranks.findall('rank'):
                    if rank.get('name') == 'boardgame':
                        rv = rank.get('value', 'Not ranked')
                        result['bgg_rank'] = f"#{rv}" if rv not in ('Not ranked', 'N/A') else 'Not ranked'
                        break

    return result


def _best_player_count(item_elem) -> str:
    """Parse the 'suggested_numplayers' poll and return the count with most 'Best' votes."""
    best_count, best_votes = None, 0

    for poll in item_elem.findall('poll'):
        if poll.get('name') == 'suggested_numplayers':
            for results in poll.findall('results'):
                num_players = results.get('numplayers', '')
                for r in results.findall('result'):
                    if r.get('value') == 'Best':
                        try:
                            votes = int(r.get('numvotes', 0))
                            if votes > best_votes:
                                best_votes = votes
                                best_count = num_players
                        except ValueError:
                            pass
            break

    return best_count or 'Unknown'


# -----------------------------------------------------------------------------
# GAME REVIEWS
# -----------------------------------------------------------------------------

def get_game_reviews(bgg_id: str) -> Dict:
    """
    Fetch BGG community reviews, split into positive and negative.

    Two sources:
      1. Rating-comments via /thing API (up to 3 pages x 100 comments)
         - positive: rating >= 7.5
         - negative: rating <= 5.0
      2. Written reviews from the game's BGG Reviews forum subforum

    Returns {'positive': [...], 'negative': [...]}
    Each entry: {user, rating (float or None), text, source}
    """
    positive, negative = [], []

    # -- Source 1: rating-comments (up to 3 pages) ---------------------------
    for page in range(1, 4):
        root = _bgg_get(f"{BGG_API_BASE}/thing", {
            'id':             bgg_id,
            'ratingcomments': 1,
            'pagesize':       100,
            'page':           page,
        })
        if root is None:
            break

        item = root.find('item')
        if item is None:
            break

        comments_elem = item.find('comments')
        if comments_elem is None:
            break

        page_comments = comments_elem.findall('comment')
        if not page_comments:
            break   # no more pages

        for c in page_comments:
            text = c.get('value', '').strip()
            username = c.get('username', 'Anonymous')

            if not text or len(text) < 50:
                continue

            try:
                rating = float(c.get('rating', ''))
            except (ValueError, TypeError):
                continue

            snippet = (text[:300] + '...') if len(text) > 300 else text
            entry = {'user': username, 'rating': rating, 'text': snippet, 'source': 'rating'}

            if rating >= 7.5:
                positive.append(entry)
            elif rating <= 5.0:
                negative.append(entry)

        # Stop fetching more pages once we have enough
        if len(positive) >= 3 and len(negative) >= 3:
            break

        time.sleep(0.8)

    # -- Source 2: written forum reviews ------------------------------------
    forum_reviews = _get_forum_reviews(bgg_id)
    positive.extend(forum_reviews.get('positive', []))
    negative.extend(forum_reviews.get('negative', []))

    return {
        'positive': positive[:3],
        'negative': negative[:3],
    }


def _get_forum_reviews(bgg_id: str) -> Dict:
    """
    Fetch written reviews from the game's BGG Reviews forum subforum.
    Uses tone-signal keywords to classify each review as positive or negative.
    """
    empty = {'positive': [], 'negative': []}

    # Get the list of forums for this game
    root = _bgg_get(f"{BGG_API_BASE}/forumlist", {'id': bgg_id, 'type': 'thing'})
    if root is None:
        return empty

    # Find the Reviews forum ID
    reviews_forum_id = None
    for forum in root.findall('forum'):
        title = forum.get('title', '').lower()
        if 'review' in title:
            reviews_forum_id = forum.get('id')
            break

    if not reviews_forum_id:
        return empty

    time.sleep(0.5)

    # Get first page of review threads
    root2 = _bgg_get(f"{BGG_API_BASE}/forum", {'id': reviews_forum_id, 'page': 1})
    if root2 is None:
        return empty

    threads_elem = root2.find('threads')
    if threads_elem is None:
        return empty

    positive, negative = [], []

    for thread in threads_elem.findall('thread')[:5]:
        thread_id = thread.get('id')
        if not thread_id:
            continue

        time.sleep(0.5)
        root3 = _bgg_get(f"{BGG_API_BASE}/thread", {'id': thread_id})
        if root3 is None:
            continue

        articles = root3.find('articles')
        if articles is None:
            continue

        first = articles.find('article')
        if first is None:
            continue

        body_elem = first.find('body')
        username  = first.get('username', 'Anonymous')
        body_text = body_elem.text.strip() if body_elem is not None and body_elem.text else ''

        if len(body_text) < 80:
            continue

        body_lower = body_text.lower()
        pos_signals = ['recommend', 'love', 'great', 'excellent', 'fantastic',
                       'fun', 'enjoyed', 'gem', 'favorite', 'brilliant', 'worth']
        neg_signals = ['disappoint', 'boring', 'not worth', 'avoid', 'bad',
                       'waste', 'mediocre', 'weak', 'problem', 'issue', 'frustrat']

        pos_score = sum(1 for w in pos_signals if w in body_lower)
        neg_score = sum(1 for w in neg_signals if w in body_lower)

        snippet = (body_text[:300] + '...') if len(body_text) > 300 else body_text
        entry = {'user': username, 'rating': None, 'text': snippet, 'source': 'forum review'}

        if pos_score > neg_score:
            positive.append(entry)
        elif neg_score > pos_score:
            negative.append(entry)

    return {'positive': positive, 'negative': negative}
