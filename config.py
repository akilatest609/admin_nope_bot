# config.py
import os
import logging

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))

DB_PATH = "bot.db"
GROUP_SIZE = 10                  # Telegram max per media group
GROUP_DELAY_SECONDS = 3.5          # delay between groups
ITEM_RETRY_DELAY_SECONDS = 2.0     # delay between per-item fallback retries
MAX_ITEM_RETRIES = 3
COUNTER_DEBOUNCE_SECONDS = 1.2     # batches rapid forwards into fewer edits
PENDING_SESSION_TTL_SECONDS = 3600  # abandoned /get_link sessions auto-expire

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("videobot")

DURATION_OPTIONS = [
    ("1 min", 60),
    ("5 min", 300),
    ("1 hr", 3600),
    ("1 day", 86400),
]