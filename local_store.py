"""HybridGB Open — local credential + settings store (SQLite).

Replaces cloud credential tables. Single-user: keys live in local SQLite
(data/hybrid_gb.db, git-ignored) or env vars. Env vars win when set.
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import threading

DB_PATH = os.getenv("HYBRID_DB",
                    str(pathlib.Path(__file__).parent / "data" / "hybrid_gb.db"))

_lock = threading.Lock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    venue TEXT NOT NULL, env TEXT NOT NULL,
    api_key TEXT NOT NULL DEFAULT '', api_secret TEXT NOT NULL DEFAULT '',
    extra TEXT NOT NULL DEFAULT '{}', updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (venue, env));
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    symbol TEXT NOT NULL DEFAULT '', total_pnl REAL NOT NULL DEFAULT 0,
    net_position REAL NOT NULL DEFAULT 0);
"""


def _db() -> sqlite3.Connection:
    pathlib.Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    with _lock:
        c.executescript(_SCHEMA)
    return c


# ── venue credentials (Binance demo/live, MT5 token) ─────────────────────────

def save_credential(venue: str, env: str, api_key: str, api_secret: str,
                    extra: dict = None) -> None:
    import time
    c = _db()
    with _lock:
        c.execute(
            "INSERT OR REPLACE INTO credentials(venue,env,api_key,api_secret,extra,updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (venue, env, api_key or "", api_secret or "",
             json.dumps(extra or {}), time.time()))
        c.commit()
    c.close()


def get_credential(venue: str, env: str) -> dict:
    c = _db()
    with _lock:
        row = c.execute(
            "SELECT api_key,api_secret,extra FROM credentials WHERE venue=? AND env=?",
            (venue, env)).fetchone()
    c.close()
    if not row:
        return {}
    try:
        extra = json.loads(row[2] or "{}")
    except Exception:
        extra = {}
    return {"api_key": row[0] or "", "api_secret": row[1] or "",
            "extra": extra}


def resolve_binance_keys(env: str = "demo") -> tuple[str, str]:
    """Local resolution order: env vars -> Settings-saved -> empty."""
    if env == "live":
        k = os.getenv("BINANCE_LIVE_API_KEY", "")
        s = os.getenv("BINANCE_LIVE_API_SECRET", "")
    else:
        k = os.getenv("BINANCE_DEMO_API_KEY", "")
        s = os.getenv("BINANCE_DEMO_API_SECRET", "")
    if k or s:
        return k, s
    c = get_credential("binance", env)
    return c.get("api_key", ""), c.get("api_secret", "")


def get_credentials(user_id=None) -> list:
    """Engine-compatible stub (single user): returns saved venues."""
    out = []
    for venue, env in (("binance", "demo"), ("binance", "live"),
                       ("mt5", "demo"), ("mt5", "live")):
        c = get_credential(venue, env)
        if c.get("api_key") or c.get("api_secret"):
            out.append({"venue": venue, "env": env, **c})
    return out


# ── generic settings (openrouter model/key, UI prefs) ────────────────────────

def save_setting(key: str, value) -> None:
    c = _db()
    with _lock:
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
                  (key, value if isinstance(value, str) else json.dumps(value)))
        c.commit()
    c.close()


def get_setting(key: str, default=None):
    c = _db()
    with _lock:
        row = c.execute("SELECT value FROM settings WHERE key=?",
                        (key,)).fetchone()
    c.close()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except Exception:
        return row[0]


# ── grid snapshots (was cloud upsert) ─────────────────────────────────────────

def save_snapshot(symbol: str, total_pnl: float,
                  net_position: float = 0.0) -> None:
    import time
    c = _db()
    with _lock:
        c.execute("INSERT INTO snapshots(ts,symbol,total_pnl,net_position)"
                  " VALUES (?,?,?,?)",
                  (time.time(), symbol, total_pnl, net_position))
        c.execute("DELETE FROM snapshots WHERE id NOT IN "
                  "(SELECT id FROM snapshots ORDER BY id DESC LIMIT 5000)")
        c.commit()
    c.close()
