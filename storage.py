"""
SQLite storage — trade history, config, heartbeat.
Replaces Supabase. Auto-creates tables on first use.
"""
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional, List, Dict

logger = logging.getLogger("hybrid.storage")

DB_PATH = os.path.join(os.path.dirname(__file__), "hybrid_gb.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    level INTEGER NOT NULL,
    direction TEXT NOT NULL,
    lot_size REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    pnl REAL NOT NULL,
    reason TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS heartbeat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    daily_pnl REAL DEFAULT 0,
    drawdown_pct REAL DEFAULT 0,
    levels_count INTEGER DEFAULT 0,
    equity REAL DEFAULT 0,
    price REAL DEFAULT 0,
    entry_status TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_heartbeat_ts ON heartbeat(timestamp);
"""

CHAT_DB_PATH = os.path.join(os.path.dirname(__file__), "hybrid_chat.db")

CHAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'web',
    author TEXT NOT NULL DEFAULT 'user',
    question TEXT NOT NULL,
    answer TEXT NOT NULL DEFAULT '',
    discord_msg_id TEXT DEFAULT NULL,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    platform TEXT NOT NULL DEFAULT 'binance'
);
CREATE INDEX IF NOT EXISTS idx_chat_ts ON chat_messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_chat_discord ON chat_messages(discord_msg_id);
CREATE INDEX IF NOT EXISTS idx_chat_platform ON chat_messages(platform);
"""


def _get_chat_db() -> sqlite3.Connection:
    """Shared chat DB — all bots + Discord share this."""
    conn = sqlite3.connect(CHAT_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # Migrate old tables missing new columns — MUST run before schema/index
    # creation or CREATE INDEX fails on the missing column.
    for col in ("discord_msg_id TEXT DEFAULT NULL",
                "is_deleted INTEGER NOT NULL DEFAULT 0",
                "platform TEXT NOT NULL DEFAULT 'binance'"):
        try:
            conn.execute(f"ALTER TABLE chat_messages ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.executescript(CHAT_SCHEMA)
    conn.commit()
    return conn


def save_chat_message(source: str, author: str, question: str, answer: str, discord_msg_id: str = None, platform: str = "binance"):
    """Save a chat exchange to the shared DB. Returns the row id."""
    ts = datetime.now(timezone.utc).isoformat()
    conn = _get_chat_db()
    try:
        conn.execute("ALTER TABLE chat_messages ADD COLUMN platform TEXT NOT NULL DEFAULT 'binance'")
    except sqlite3.OperationalError:
        pass
    try:
        cur = conn.execute(
            "INSERT INTO chat_messages (timestamp, source, author, question, answer, discord_msg_id, platform) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, source, author, question, answer, discord_msg_id, platform))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def save_chat_answer(discord_msg_id: str, question: str, answer: str):
    """Save or update answer for a specific Discord message (bot's reply)."""
    ts = datetime.now(timezone.utc).isoformat()
    conn = _get_chat_db()
    try:
        conn.execute(
            "INSERT INTO chat_messages (timestamp, source, author, question, answer, discord_msg_id) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, "discord", "bot", question, answer, discord_msg_id))
        conn.commit()
    finally:
        conn.close()


def get_chat_history(limit: int = 40, platform: str = None) -> List[dict]:
    """Get recent chat history (non-deleted), optionally filtered by platform."""
    conn = _get_chat_db()
    try:
        conn.row_factory = sqlite3.Row
        if platform:
            rows = conn.execute(
                "SELECT * FROM chat_messages WHERE is_deleted=0 AND platform=? ORDER BY id ASC LIMIT ?",
                (platform, limit * 2)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM chat_messages WHERE is_deleted=0 ORDER BY id ASC LIMIT ?",
                (limit * 2,)).fetchall()
        result = [dict(r) for r in rows]
        return result[-limit:] if len(result) > limit else result
    finally:
        conn.close()


def hide_deleted_messages(active_discord_ids: set):
    """Mark chat messages as deleted if their discord_msg_id is no longer in Discord."""
    conn = _get_chat_db()
    try:
        # Get all discord-sourced messages that have a discord_msg_id
        rows = conn.execute(
            "SELECT id, discord_msg_id FROM chat_messages WHERE discord_msg_id IS NOT NULL AND is_deleted=0").fetchall()
        for row in rows:
            if row[1] not in active_discord_ids:
                conn.execute("UPDATE chat_messages SET is_deleted=1 WHERE id=?", (row[0],))
        conn.commit()
    finally:
        conn.close()


class Storage:
    """Thread-safe SQLite storage for the grid bot."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.executescript(SCHEMA)
                conn.commit()
                logger.info(f"SQLite ready — {self.db_path}")
            finally:
                conn.close()

    # ── Trades ─────────────────────────────────────────────────────────────

    def record_trade(self, level: int, direction: str, lot: float,
                     entry: float, exit_px: float, pnl: float,
                     reason: str, opened_at: str = None):
        ts = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT INTO trades (timestamp, level, direction, lot_size, entry_price, exit_price, pnl, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ts, level, direction, lot, entry, exit_px, round(pnl, 2), reason))
                conn.commit()
            finally:
                conn.close()

    def get_trades(self, limit: int = 50) -> List[dict]:
        with self._lock:
            conn = self._get_conn()
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def get_pnl_summary(self) -> dict:
        with self._lock:
            conn = self._get_conn()
            try:
                total = conn.execute("SELECT COALESCE(SUM(pnl), 0) FROM trades").fetchone()[0]
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                daily = conn.execute(
                    "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE timestamp LIKE ?", (f"{today}%",)).fetchone()[0]
                win = conn.execute("SELECT COUNT(*) FROM trades WHERE pnl > 0").fetchone()[0]
                loss = conn.execute("SELECT COUNT(*) FROM trades WHERE pnl < 0").fetchone()[0]
                return {"total_pnl": total, "daily_pnl": daily,
                        "total_trades": win + loss, "wins": win, "losses": loss}
            finally:
                conn.close()

    # ── Heartbeat ──────────────────────────────────────────────────────────

    def record_heartbeat(self, state: dict):
        ts = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT INTO heartbeat (timestamp, daily_pnl, drawdown_pct, levels_count, equity, price, entry_status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (ts, state.get("daily_pnl", 0), state.get("drawdown_pct", 0),
                     state.get("levels", 0), state.get("equity", 0),
                     state.get("price", 0), state.get("entry_status", "")))
                # Keep only last 1000 heartbeats
                conn.execute("DELETE FROM heartbeat WHERE id NOT IN (SELECT id FROM heartbeat ORDER BY id DESC LIMIT 1000)")
                conn.commit()
            finally:
                conn.close()

    def get_recent_heartbeats(self, limit: int = 100) -> List[dict]:
        with self._lock:
            conn = self._get_conn()
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT * FROM heartbeat ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    # ── Config ─────────────────────────────────────────────────────────────

    def get_config(self, key: str, default: str = None) -> Optional[str]:
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
                return row[0] if row else default
            finally:
                conn.close()

    def set_config(self, key: str, value: str):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
                conn.commit()
            finally:
                conn.close()
