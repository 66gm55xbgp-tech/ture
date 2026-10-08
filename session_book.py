"""HybridGB Open — today-only session book (SQLite, local).

Same public API as the cloud version: trading_session_date,
purge_old_sessions, upsert_symbol, fetch_symbol, fetch_session.
Holds the current session's ladder/position so the chart keeps indicators
after a restart. Older rows are purged at the next session open.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

logger = logging.getLogger("hybrid.session_book")

SESSION_START_HOUR = int(os.getenv("GURU_ACTIVE_START_HOUR", "8"))
DB_PATH = os.getenv("HYBRID_DB",
                    str(pathlib.Path(__file__).parent / "data" / "hybrid_gb.db"))

_lock = threading.Lock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_book (
    symbol TEXT NOT NULL, env TEXT NOT NULL, platform TEXT NOT NULL,
    session_date TEXT NOT NULL, levels_json TEXT NOT NULL DEFAULT '[]',
    updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, env, platform));
"""


def _db() -> sqlite3.Connection:
    pathlib.Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    with _lock:
        c.executescript(_SCHEMA)
    return c


def trading_session_date(now: datetime = None) -> str:
    now = now or datetime.now(timezone.utc)
    d = now.date()
    if now.hour < SESSION_START_HOUR:
        d = (now - timedelta(days=1)).date()
    return d.isoformat()


def purge_old_sessions(now: datetime = None) -> bool:
    today = trading_session_date(now)
    c = _db()
    with _lock:
        cur = c.execute("DELETE FROM session_book WHERE session_date < ?",
                        (today,))
        c.commit()
    c.close()
    return cur.rowcount >= 0


def upsert_symbol(user_id: str, env: str, platform: str, symbol: str,
                  levels=None, session_date: str = None) -> bool:
    c = _db()
    with _lock:
        c.execute(
            "INSERT OR REPLACE INTO session_book(symbol,env,platform,session_date,levels_json,updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (symbol, env, platform, session_date or trading_session_date(),
             json.dumps(levels or []), time.time()))
        c.commit()
    c.close()
    return True


def fetch_symbol(user_id: str, platform: str, symbol: str,
                 env: str = "demo") -> list:
    c = _db()
    with _lock:
        row = c.execute(
            "SELECT levels_json FROM session_book WHERE symbol=? AND platform=? AND env=?",
            (symbol, platform, env)).fetchone()
    c.close()
    if not row:
        return []
    try:
        return json.loads(row[0] or "[]")
    except Exception:
        return []


def fetch_session(user_id: str, platform: str = "binance",
                  env: str = "demo") -> list:
    c = _db()
    with _lock:
        rows = c.execute(
            "SELECT symbol,levels_json,updated_at FROM session_book"
            " WHERE platform=? AND env=? AND session_date=?",
            (platform, env, trading_session_date())).fetchall()
    c.close()
    out = []
    for sym, lj, ts in rows:
        try:
            lv = json.loads(lj or "[]")
        except Exception:
            lv = []
        out.append({"symbol": sym, "levels": lv, "updated_at": ts})
    return out
