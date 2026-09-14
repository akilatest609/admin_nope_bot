# database.py
import sqlite3
import random
import string
from config import DB_PATH

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                joined_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS batches (
                code TEXT PRIMARY KEY,
                owner_id INTEGER,
                file_ids TEXT,
                types TEXT,
                markups TEXT,
                captions TEXT,
                created_at INTEGER,
                auto_delete_seconds INTEGER,
                revoked INTEGER,
                deleted INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT,
                user_id INTEGER,
                ts INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS whitelist (
                user_id INTEGER PRIMARY KEY
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT,
                user_id INTEGER,
                chat_id INTEGER,
                message_id INTEGER,
                created_at INTEGER,
                delete_after_seconds INTEGER,
                deleted INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_threads (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                admin_msg_id INTEGER,
                last_updated INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_mapping (
                admin_message_id INTEGER PRIMARY KEY,
                user_id INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_msg_mapping (
                user_message_id INTEGER,
                user_id INTEGER,
                admin_message_id INTEGER,
                PRIMARY KEY (user_message_id, user_id)
            )
            """
        )
    conn.close()

def gen_code(length: int = 8) -> str:
    chars = string.ascii_letters + string.digits
    while True:
        code = "".join(random.choices(chars, k=length))
        conn = db()
        try:
            row = conn.execute("SELECT 1 FROM batches WHERE code = ?", (code,)).fetchone()
            if not row:
                return code
        finally:
            conn.close()

def encode_list(lst: list) -> str:
    import json
    return json.dumps(lst)

def decode_list(s: str) -> list:
    import json
    if not s:
        return []
    try:
        return json.loads(s)
    except Exception:
        return []

def register_user(user_id: int, first_name: str, username: str):
    import time
    conn = db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, first_name, username, joined_at) VALUES (?, ?, ?, ?)",
            (user_id, first_name, username, int(time.time()))
        )
        conn.commit()
    finally:
        conn.close()