import os
from dotenv import load_dotenv
from pathlib import Path

# Load .env from the same directory as this script
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / '.env')

# --- BGG ---
BGG_API_TOKEN        = os.getenv('BGG_API_TOKEN', '')
BGG_FORUM_ID         = 10   # Hot Deals forum on BGG

# --- Email ---
ALERT_EMAIL          = os.getenv('ALERT_EMAIL', '')       # where to SEND alerts TO
GMAIL_USER           = os.getenv('GMAIL_USER', '')        # Gmail account to send FROM
GMAIL_APP_PASSWORD   = os.getenv('GMAIL_APP_PASSWORD', '') # Gmail App Password (not your regular password)

# --- Monitoring ---
CHECK_INTERVAL_MINUTES = int(os.getenv('CHECK_INTERVAL_MINUTES', '15'))

# --- File Paths ---
SEEN_THREADS_FILE    = BASE_DIR / 'seen_threads.json'     # tracks threads we've already processed
