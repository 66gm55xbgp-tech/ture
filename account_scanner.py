"""
Exchange Account Scanner — discovers all trading accounts from .env at boot.
Supports multiple platforms (binance, ctrader, bybit, kucoin, mt5) across live/demo.
Returns structured account list for dashboard sidebar.
"""
import os
import logging
import threading
import time
from typing import Dict, List, Any
from dataclasses import dataclass, field

logger = logging.getLogger("hybrid.accounts")

POSITIONS_TTL = 180.0  # seconds between positions/orders REST refreshes (rate-limit safe)

# Binance IP rate-limit ban (-1003): once banned, the whole IP is throttled for
# N minutes. Track the ban window and skip ALL Binance refreshes until it
# passes, so a busy session stops hammering and recovers fast.
_BANNED_UNTIL = 0.0

def _banned() -> float:
    """Seconds still left in a Binance IP ban (0 = not banned)."""
    return max(0.0, _BANNED_UNTIL - time.time())

def _note_ban(err: Exception):
    """If the error is a Binance -1003 ban, record when it lifts."""
    global _BANNED_UNTIL
    msg = str(err)
    if "-1003" in msg and "banned until" in msg:
        try:
            until_ms = float(msg.split("banned until ")[1].split(".")[0].split(" ")[0])
            _BANNED_UNTIL = max(_BANNED_UNTIL, until_ms / 1000.0)
            logger.warning(f"Binance -1003 ban noted — pausing live refreshes "
                           f"{_banned():.0f}s")
        except Exception:
            pass

# Reused Binance clients: creating a fresh Client (and its HTTP session) every
# refresh leaked connection pools — reuse per (env, owner) instead.
_CLIENTS: Dict[str, any] = {}


def _get_client(key: str, api_key: str, api_secret: str, testnet: bool):
    from binance.client import Client
    c = _CLIENTS.get(key)
    if c is None:
        c = Client(api_key, api_secret, testnet=testnet)
        _CLIENTS[key] = c
    return c


@dataclass
class ExchangeAccount:
    """Discovered trading account."""
    platform: str      # "binance", "ctrader", "bybit", "kucoin", "mt5"
    env: str           # "live" or "demo"
    label: str         # "Binance LIVE", "Binance DEMO", etc
    balance: float = 0.0
    equity: float = 0.0
    margin: float = 0.0
    free_margin: float = 0.0
    positions: List[dict] = field(default_factory=list)
    open_orders: List[dict] = field(default_factory=list)
    connected: bool = False
    error: str = ""
    last_updated: float = 0.0
    positions_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "env": self.env,
            "label": self.label,
            "balance": round(self.balance, 2),
            "equity": round(self.equity, 2),
            "margin": round(self.margin, 2),
            "free_margin": round(self.free_margin, 2),
            "positions": self.positions,
            "open_orders": self.open_orders,
            "connected": self.connected,
            "error": self.error,
        }


class AccountScanner:
    """Scans .env for exchange API keys and fetches live balances periodically."""

    def __init__(self):
        self.accounts: Dict[str, ExchangeAccount] = {}
        self._lock = threading.Lock()
        self._update_interval = 300  # seconds between balance refreshes (was 60 — 60s polling + guru REST was tripping Binance -1003 IP bans)

    def discover(self):
        """Scan .env for all available exchange accounts."""
        with self._lock:
            self.accounts = {}
            env_vars = dict(os.environ)

            # ── Binance ─────────────────────────────────────────────────
            if env_vars.get("BINANCE_LIVE_API_KEY"):
                self.accounts["binance_live"] = ExchangeAccount(
                    platform="binance", env="live", label="🔴 Binance LIVE")
            if env_vars.get("BINANCE_DEMO_API_KEY"):
                self.accounts["binance_demo"] = ExchangeAccount(
                    platform="binance", env="demo", label="🟡 Binance DEMO")

            # ── cTrader ────────────────────────────────────────────────
            if env_vars.get("CTRADER_LIVE_CLIENT_ID"):
                self.accounts["ctrader_live"] = ExchangeAccount(
                    platform="ctrader", env="live", label="🔴 cTrader LIVE")
            if env_vars.get("CTRADER_DEMO_CLIENT_ID"):
                self.accounts["ctrader_demo"] = ExchangeAccount(
                    platform="ctrader", env="demo", label="🟡 cTrader DEMO")

            # ── Bybit ───────────────────────────────────────────────────
            if env_vars.get("BYBIT_LIVE_API_KEY"):
                self.accounts["bybit_live"] = ExchangeAccount(
                    platform="bybit", env="live", label="🔴 Bybit LIVE")
            if env_vars.get("BYBIT_DEMO_API_KEY"):
                self.accounts["bybit_demo"] = ExchangeAccount(
                    platform="bybit", env="demo", label="🟡 Bybit DEMO")

            # ── KuCoin ──────────────────────────────────────────────────
            if env_vars.get("KUCOIN_LIVE_API_KEY"):
                self.accounts["kucoin_live"] = ExchangeAccount(
                    platform="kucoin", env="live", label="🔴 KuCoin LIVE")
            if env_vars.get("KUCOIN_DEMO_API_KEY"):
                self.accounts["kucoin_demo"] = ExchangeAccount(
                    platform="kucoin", env="demo", label="🟡 KuCoin DEMO")

            # ── MT5 ─────────────────────────────────────────────────────
            if env_vars.get("MT5_LIVE_LOGIN"):
                self.accounts["mt5_live"] = ExchangeAccount(
                    platform="mt5", env="live", label="🔴 MT5 LIVE")
            if env_vars.get("MT5_DEMO_LOGIN"):
                self.accounts["mt5_demo"] = ExchangeAccount(
                    platform="mt5", env="demo", label="🟡 MT5 DEMO")

            logger.info(f"🔍 Discovered {len(self.accounts)} accounts: {list(self.accounts.keys())}")

    def refresh_all(self):
        """Update balances for all discovered accounts."""
        with self._lock:
            for key, acct in self.accounts.items():
                self._refresh_one(key, acct)

    def refresh_platform(self, platform: str):
        """Update balances for a single platform only (e.g. 'binance')."""
        with self._lock:
            for key, acct in self.accounts.items():
                if acct.platform == platform:
                    self._refresh_one(key, acct)

    def _refresh_one(self, key: str, acct: ExchangeAccount):
        """Fetch balance for a single account via its platform connector."""
        try:
            acct.last_updated = time.time()

            if acct.platform == "binance":
                self._refresh_binance(acct)
            elif acct.platform == "bybit":
                # Placeholder — implement when bybit connector exists
                acct.connected = True
                acct.balance = 0.0
                acct.error = "bybit connector not implemented"
            elif acct.platform == "kucoin":
                acct.connected = True
                acct.balance = 0.0
                acct.error = "kucoin connector not implemented"
            elif acct.platform == "ctrader":
                acct.connected = True
                acct.balance = 0.0
                acct.error = "ctrader MCP connector not implemented"
            elif acct.platform == "mt5":
                acct.connected = True
                acct.balance = 0.0
                acct.error = "MT5 connector not implemented"
        except Exception as e:
            acct.error = str(e)[:100]
            acct.connected = False

    def _refresh_binance(self, acct: ExchangeAccount, user_id: str = None):
        """Fetch Binance balance, open positions and open/conditional orders
        using REST API (no WebSocket).

        Balance refreshes on the normal cadence; positions/orders are heavier
        (3 extra REST calls each) so they carry their own longer TTL to avoid
        Binance IP rate-limit bans (-1003)."""
        if _banned():
            acct.error = f"rate-limited (ban ~{_banned():.0f}s)"
            acct.connected = False
            return
        from dotenv import load_dotenv as _ld; _ld(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
        from binance.client import Client
        # local keys (Settings UI / env) take precedence; fall back to .env
        try:
            import local_store
            _k, _sec = local_store.resolve_binance_keys(acct.env)
            user_keys = {"api_key": _k, "api_secret": _sec} if (_k or _sec) else None
        except Exception:
            user_keys = None
        if user_keys:
            api_key, api_secret = user_keys["api_key"], user_keys["api_secret"]
        elif user_id and user_id != "local":
            # Strict isolation: an unknown user must not fall back to .env.
            acct.error = "No API credentials"; acct.connected = False
            return
        else:
            # Legacy unscoped path: DEMO .env keys only. LIVE is never a
            # fallback (a user-less account must not silently read the
            # operator's or another user's live balance).
            if acct.env != "demo":
                acct.error = "Live requires a user account"; acct.connected = False
                return
            api_key = os.getenv("BINANCE_DEMO_API_KEY", "")
            api_secret = os.getenv("BINANCE_DEMO_API_SECRET", "")
        if not api_key or not api_secret:
            acct.error = "No API credentials"; acct.connected = False
            return
        key = f"binance:{acct.env}:{user_id or '-'}"
        try:
            c = _get_client(key, api_key, api_secret, testnet=(acct.env == "demo"))
            f = c.futures_account()
            acct.balance = float(f.get("totalWalletBalance", 0))
            acct.equity = float(f.get("totalMarginBalance", 0))
            acct.margin = float(f.get("totalInitialMargin", 0))
            acct.free_margin = float(f.get("availableBalance", 0))
            now = time.time()
            if now - getattr(acct, "positions_at", 0.0) >= POSITIONS_TTL:
                acct.positions = self._fetch_positions(c)
                acct.open_orders = self._fetch_open_orders(c)
                acct.positions_at = now
            acct.connected = True
            acct.error = ""
        except Exception as e:
            _note_ban(e)
            # Drop the client on failure — it may hold a stale/broken session.
            _CLIENTS.pop(key, None)
            raise e

    def _fetch_positions(self, c) -> List[dict]:
        """All non-zero futures positions, exchange truth."""
        try:
            info = c.futures_position_information()
        except Exception as e:
            logger.warning(f"positions fetch failed: {e}")
            return []
        out = []
        for p in info:
            amt = float(p.get("positionAmt", 0) or 0)
            if abs(amt) < 1e-9:
                continue
            mark = float(p.get("markPrice", 0) or 0)
            entry = float(p.get("entryPrice", 0) or 0)
            upnl = float(p.get("unRealizedProfit", 0) or 0)
            lev = p.get("leverage", "")
            side = "LONG" if amt > 0 else "SHORT"
            out.append({
                "symbol": p.get("symbol", ""),
                "side": side,
                "qty": abs(amt),
                "entry": round(entry, 6),
                "mark": round(mark, 6),
                "liquidation": round(float(p.get("liquidationPrice", 0) or 0), 6),
                "leverage": lev,
                "upnl": round(upnl, 2),
            })
        return out

    def _fetch_open_orders(self, c) -> List[dict]:
        """All open orders incl. conditional (STOP_MARKET / TAKE_PROFIT_MARKET),
        which Binance keeps in a separate conditional/algo order book."""
        out = []
        try:
            oo = c.futures_get_open_orders()
        except Exception as e:
            logger.warning(f"open orders fetch failed: {e}")
            oo = []
        for o in oo:
            otype = o.get("type", "")
            out.append({
                "order_id": o.get("orderId", 0),
                "symbol": o.get("symbol", ""),
                "side": o.get("side", ""),
                "type": otype,
                "price": round(float(o.get("price", 0) or 0), 6),
                "stop_price": round(float(o.get("stopPrice", 0) or 0), 6),
                "qty": float(o.get("origQty", 0) or 0),
                "reduce_only": bool(o.get("reduceOnly", False)),
                "is_conditional": False,
            })
        try:
            ca = c.futures_get_open_algo_orders()
        except Exception as e:
            logger.warning(f"conditional orders fetch failed: {e}")
            ca = []
        for o in ca:
            out.append({
                "order_id": o.get("algoId", o.get("orderId", 0)),
                "symbol": o.get("symbol", ""),
                "side": o.get("side", ""),
                "type": o.get("orderType", o.get("type", "CONDITIONAL")),
                "price": round(float(o.get("price", 0) or 0), 6),
                "stop_price": round(float(o.get("triggerPrice", o.get("stopPrice", 0)) or 0), 6),
                "qty": float(o.get("quantity", o.get("origQty", 0)) or 0),
                "reduce_only": bool(o.get("reduceOnly", False)),
                "is_conditional": True,
            })
        return out

    def get_all(self) -> List[dict]:
        """Return all accounts as dicts for API response."""
        with self._lock:
            return [a.to_dict() for a in self.accounts.values()]

    def get_by_platform(self, platform: str) -> List[dict]:
        with self._lock:
            return [a.to_dict() for a in self.accounts.values() if a.platform == platform]


# ── Singleton ────────────────────────────────────────────────────────────────
scanner = AccountScanner()
scanner.discover()


def _close_all_binance_positions(env: str = "demo") -> int:
    """Close ALL futures positions on Binance (live or demo). Returns count."""
    return close_all_binance(env)["positions"]


def invalidate_positions(platform: str = "binance"):
    """Force the next refresh to re-fetch positions/orders (bypass the 180s TTL)."""
    for acct in scanner.accounts.values():
        if acct.platform == platform:
            acct.positions_at = 0.0


def fetch_accounts(user_id: str = None) -> List[dict]:
    """Multi-tenant account list for the dashboard.

    When `user_id` is provided (Supabase JWT), accounts are built from that
    user's own broker credentials — never another user's, and never the shared
    active-user global — so concurrent users see only their own balances.

    Falls back to the legacy .env scanner for the single-operator admin token
    path (user_id=None).
    """
    if user_id:
        return _fetch_user_accounts(user_id)
    with scanner._lock:
        if not scanner.accounts:
            scanner.discover()
        scanner.refresh_all()
        return scanner.get_all()


def _fetch_user_accounts(user_id: str) -> List[dict]:
    """Discover + refresh local accounts from the local credential store."""
    import local_store
    out: List[dict] = []
    for env in ("live", "demo"):
        try:
            _k, _sec = local_store.resolve_binance_keys(env)
            keys = {"api_key": _k, "api_secret": _sec} if (_k or _sec) else None
        except Exception:
            keys = None
        if not keys:
            continue
        acct = ExchangeAccount(
            platform="binance", env=env,
            label="🔴 Binance LIVE" if env == "live" else "🟡 Binance DEMO")
        try:
            scanner._refresh_binance(acct, user_id=user_id)
        except Exception as e:
            acct.connected = False
            acct.error = str(e)[:100]
        out.append(acct.to_dict())
    return out


def close_all_binance(env: str = "live", user_id: str = None) -> dict:
    """Close ALL futures positions + cancel ALL open orders + conditional (algo)
    orders on Binance. Returns {"positions", "orders", "conditional"} counts."""
    from dotenv import load_dotenv as _ld; _ld(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    from binance.client import Client
    try:
        import local_store
        _k, _sec = local_store.resolve_binance_keys(env)
        user_keys = {"api_key": _k, "api_secret": _sec} if (_k or _sec) else None
    except Exception:
        user_keys = None
    if user_keys:
        api_key, api_secret = user_keys["api_key"], user_keys["api_secret"]
    else:
        # Legacy unscoped path only; a Supabase user without keys gets nothing.
        # LIVE is never a fallback — an unscoped live count must not silently
        # read another account.
        if user_id or env != "demo":
            return {"positions": 0, "orders": 0, "conditional": 0}
        api_key = os.getenv("BINANCE_DEMO_API_KEY", "")
        api_secret = os.getenv("BINANCE_DEMO_API_SECRET", "")
    if not api_key:
        return {"positions": 0, "orders": 0, "conditional": 0}
    c = Client(api_key, api_secret, testnet=(env == "demo"))

    symbols = set()
    closed = 0
    # 1) flatten every open position (market, reduceOnly)
    try:
        for p in c.futures_position_information():
            amt = float(p.get("positionAmt", 0) or 0)
            if abs(amt) < 1e-8:
                continue
            sym = p.get("symbol", "")
            symbols.add(sym)
            side = "BUY" if amt < 0 else "SELL"
            try:
                c.futures_create_order(symbol=sym, side=side, type="MARKET",
                                       quantity=abs(amt), reduceOnly=True)
                closed += 1
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"close-all positions fetch failed: {e}")

    # 2) cancel regular open orders (symbols with resting orders too)
    try:
        for o in c.futures_get_open_orders():
            symbols.add(o.get("symbol", ""))
    except Exception:
        pass
    cancelled = 0
    for sym in symbols:
        try:
            c.futures_cancel_all_open_orders(symbol=sym)
            cancelled += 1
        except Exception:
            pass

    # 3) cancel conditional / algo orders (STOP_MARKET, TAKE_PROFIT_MARKET)
    cancelled_cond = 0
    try:
        r = c.futures_cancel_all_algo_open_orders()
        if r and r.get("code") == 200:
            cancelled_cond = 1
    except Exception:
        pass

    return {"positions": closed, "orders": cancelled, "conditional": cancelled_cond}


scanner.refresh_all()

# Start background refresh thread
def _refresh_loop():
    while True:
        time.sleep(60)
        try:
            scanner.refresh_all()
        except Exception:
            pass

_refresh_thread = threading.Thread(target=_refresh_loop, daemon=True)
_refresh_thread.start()
