#!/usr/bin/env python3
"""MT5 EA link registry.

The HybridGB EA (one instance per MT5 chart) POSTs its state to
/api/mt5/state every tick (throttled) and executes the commands returned
in the response. This module is the server-side half of that link:

  token ──> { account, symbols[], per_symbol{ sym -> state/commands/exec } }

- One token per user (stored in Supabase broker_credentials, broker=mt5).
- One EA instance per chart: every POST carries its chart symbol, so
  commands for symbol S are delivered to the EA instance that posted S.
- Connection health = fresh last_seen (< LINK_TTL seconds).

Thread-safe: the EA POSTs arrive on FastAPI worker threads while grid-bot
threads read state / queue commands through MT5Bridge.
"""
import json
import pathlib
import threading
import time
from collections import deque
from typing import Dict, Optional

LINK_TTL = 90.0          # seconds without a POST => dashboard "disconnected"
COMMAND_TTL = 180.0      # close/cancel may wait for the next EA POST
PLACE_TTL = 15.0         # OPEN_* expire fast so a reconnect cannot dump a ladder
EA_QUIET_S = 40.0        # yellow: a beat is late (EA often POSTs ~30s)
EA_STALE_S = 60.0        # pause bots + drop queued OPEN commands
EA_LOST_S = 90.0         # stop MT5 bots, toast, ask user to flatten leftovers

_LOCK = threading.RLock()
_LINKS: Dict[str, dict] = {}          # token -> link dict
_PERSIST = pathlib.Path(__file__).resolve().parent.parent / "data" / "mt5_links.json"


def _now() -> float:
    return time.time()


def _blank_link() -> dict:
    return {
        "account": {}, "symbols": [], "registered_at": _now(),
        "last_seen": _now(), "per_symbol": {},
    }


def _persist_unlocked():
    """Write last-known account + symbol lists so a pm2 restart is not empty."""
    try:
        _PERSIST.parent.mkdir(parents=True, exist_ok=True)
        dump = {}
        for tok, link in _LINKS.items():
            dump[tok] = {
                "account": link.get("account") or {},
                "symbols": link.get("symbols") or [],
                "charts": list((link.get("per_symbol") or {}).keys()),
            }
        _PERSIST.write_text(json.dumps(dump), encoding="utf-8")
    except Exception:
        pass


def _hydrate():
    try:
        if not _PERSIST.exists():
            return
        dump = json.loads(_PERSIST.read_text(encoding="utf-8") or "{}")
        if not isinstance(dump, dict):
            return
        with _LOCK:
            for tok, row in dump.items():
                if not tok or tok in _LINKS:
                    continue
                link = _blank_link()
                link["account"] = (row or {}).get("account") or {}
                link["symbols"] = (row or {}).get("symbols") or []
                for s in (row or {}).get("charts") or []:
                    if s:
                        link["per_symbol"].setdefault(s, {
                            "state": {}, "commands": [], "exec": {},
                            "last_seen": 0.0})
                # persisted links are stale until the EA posts again
                link["last_seen"] = 0.0
                _LINKS[tok] = link
    except Exception:
        pass


_hydrate()


def get_link(token: str) -> Optional[dict]:
    with _LOCK:
        return _LINKS.get(token or "")


def all_tokens() -> list:
    with _LOCK:
        return list(_LINKS.keys())


def link_exists(token: str) -> bool:
    with _LOCK:
        return (token or "") in _LINKS


def is_connected(token: str) -> bool:
    with _LOCK:
        link = _LINKS.get(token or "")
    if not link:
        return False
    last = float(link.get("last_seen") or 0)
    if last < 1_000_000_000:
        return False
    return (_now() - last) < LINK_TTL


def touch_alive(token: str, symbol: str = "") -> None:
    """Count an EA POST even if the JSON body could not be parsed."""
    if not token:
        return
    with _LOCK:
        link = _LINKS.get(token)
        if link is None:
            link = _blank_link()
            _LINKS[token] = link
        link["last_seen"] = _now()
        if symbol:
            ps = link["per_symbol"].setdefault(symbol, {
                "state": {}, "commands": [], "exec": {}, "last_seen": _now(),
                "mid_hist": deque(maxlen=1800)})
            ps["last_seen"] = _now()


def last_seen_age(token: str) -> Optional[float]:
    """Seconds since the last EA POST, or None if never seen this process."""
    with _LOCK:
        link = _LINKS.get(token or "")
    if not link:
        return None
    last = float(link.get("last_seen") or 0)
    if last < 1_000_000_000:
        return None
    return max(0.0, _now() - last)


def ea_state(token: str) -> str:
    """live | quiet | stale | lost | unknown"""
    age = last_seen_age(token)
    if age is None:
        # Hydrated / never POSTed this process — not "lost". A lost call
        # would stop bots 5s after pm2 restart while the EA is still live.
        return "unknown"
    if age <= EA_QUIET_S:
        return "live"
    if age <= EA_STALE_S:
        return "quiet"
    if age <= EA_LOST_S:
        return "stale"
    return "lost"


def drop_place_commands(token: str) -> int:
    """Drop queued OPEN_* so a reconnect cannot dump a fresh ladder."""
    n = 0
    with _LOCK:
        link = _LINKS.get(token or "")
        if not link:
            return 0
        for ps in (link.get("per_symbol") or {}).values():
            cmds = ps.get("commands") or []
            keep = [c for c in cmds
                    if not str((c or {}).get("action") or "").startswith("OPEN")]
            n += len(cmds) - len(keep)
            ps["commands"] = keep
    return n


_ALERT: dict = {"at": 0.0, "msg": "", "state": "live"}


def set_alert(msg: str, state: str = "lost"):
    with _LOCK:
        _ALERT["at"] = _now()
        _ALERT["msg"] = msg
        _ALERT["state"] = state


def get_alert() -> dict:
    with _LOCK:
        return dict(_ALERT)


def ensure(token: str, symbol: str = "", account: dict = None,
           symbols: list = None) -> dict:
    """Create the in-memory link if missing (pm2 restart recovery)."""
    with _LOCK:
        link = _LINKS.get(token or "")
        created = link is None
        if created:
            link = _blank_link()
            _LINKS[token] = link
        if account:
            link["account"] = account
        if symbols:
            link["symbols"] = symbols
        if symbol:
            link["per_symbol"].setdefault(symbol, {
                "state": {}, "commands": [], "exec": {}, "last_seen": _now()})
        if created:
            _persist_unlocked()
        return link


def register(token: str, account: dict, symbols: list) -> dict:
    """Create/update the link on EA /register (init + 5-min heartbeat)."""
    with _LOCK:
        link = _LINKS.setdefault(token, _blank_link())
        link["account"] = account or {}
        link["symbols"] = symbols or []
        link["registered_at"] = _now()
        link["last_seen"] = _now()
        _persist_unlocked()
        return link


def touch_symbol(token: str, symbol: str, state: dict) -> Optional[dict]:
    """Record an EA /state POST for one chart symbol. Returns the queued
    commands for that symbol (popped). Auto-creates the link after a
    process restart so the EA does not sit in 401 until the 5-min register."""
    with _LOCK:
        link = _LINKS.get(token or "")
        new_chart = False
        if link is None:
            link = _blank_link()
            _LINKS[token] = link
            new_chart = True
        link["last_seen"] = _now()
        if symbol and symbol not in link["per_symbol"]:
            new_chart = True
        ps = link["per_symbol"].setdefault(symbol, {
            "state": {}, "commands": [], "exec": {}, "last_seen": _now(),
            "mid_hist": deque(maxlen=1800)})
        ps["state"] = state or {}
        ps["last_seen"] = _now()
        try:
            bid = float((state or {}).get("bid") or 0)
            ask = float((state or {}).get("ask") or 0)
            if bid > 0 and ask > 0:
                hist = ps.get("mid_hist")
                if hist is None or not hasattr(hist, "append"):
                    hist = deque(maxlen=1800)
                    ps["mid_hist"] = hist
                hist.append((_now(), (bid + ask) / 2.0))
        except (TypeError, ValueError):
            pass
        now = _now()
        kept = []
        for c in ps.get("commands", []) or []:
            action = str((c or {}).get("action") or "")
            ttl = PLACE_TTL if action.startswith("OPEN") else COMMAND_TTL
            if now - float((c or {}).get("queued_at") or 0) < ttl:
                kept.append(c)
        cmds = kept
        ps["commands"] = []
        if new_chart:
            _persist_unlocked()
        return cmds


def push_exec(token: str, symbol: str, exec_row: dict):
    """EA reports the result of an executed command (retcode/ticket)."""
    with _LOCK:
        link = _LINKS.get(token or "")
        if link is None:
            return
        ps = link["per_symbol"].setdefault(symbol, {
            "state": {}, "commands": [], "exec": {}, "last_seen": _now()})
        ps["exec"] = exec_row or {}
        ps["exec"]["at"] = _now()


def wait_exec(token: str, symbol: str, command_id: str, timeout: float = 8.0) -> Optional[dict]:
    """Block until the EA echoes the exec result for command_id."""
    deadline = _now() + timeout
    while _now() < deadline:
        with _LOCK:
            link = _LINKS.get(token or "")
            if link is not None:
                ps = link["per_symbol"].get(symbol)
                if ps:
                    ex = ps.get("exec") or {}
                    if ex.get("command_id") == command_id:
                        return ex
        time.sleep(0.25)
    return None


def queue_command(token: str, symbol: str, command: dict):
    """Queue a command for the EA instance attached to `symbol`."""
    with _LOCK:
        link = _LINKS.get(token or "")
        if link is None:
            raise KeyError(f"no MT5 link for token")
        ps = link["per_symbol"].setdefault(symbol, {
            "state": {}, "commands": [], "exec": {}, "last_seen": _now()})
        command = dict(command)
        command["queued_at"] = _now()
        ps["commands"].append(command)


def pop_exec(token: str, symbol: str) -> dict:
    """Return (and clear) the latest exec echo for a symbol."""
    with _LOCK:
        link = _LINKS.get(token or "")
        if link is None:
            return {}
        ps = link["per_symbol"].get(symbol, {})
        ex = ps.get("exec") or {}
        ps["exec"] = {}
        return ex


def get_state(token: str, symbol: str) -> dict:
    with _LOCK:
        link = _LINKS.get(token or "")
        if link is None:
            return {}
        return (link["per_symbol"].get(symbol) or {}).get("state", {})


def get_account(token: str) -> dict:
    with _LOCK:
        link = _LINKS.get(token or "")
    return (link or {}).get("account", {})


def account_book(token: str) -> dict:
    """Exchange-truth book for the sidebar: positions + pending orders
    across every chart this token has posted, plus last free margin."""
    positions, orders = [], []
    free_margin = 0.0
    with _LOCK:
        link = _LINKS.get(token or "") or {}
        for sym, ps in (link.get("per_symbol") or {}).items():
            st = (ps or {}).get("state") or {}
            try:
                bid = float(st.get("bid") or 0)
                ask = float(st.get("ask") or 0)
            except (TypeError, ValueError):
                bid = ask = 0.0
            mark = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0
            try:
                fm = st.get("freeMargin")
                if fm is not None:
                    free_margin = float(fm or 0)
            except (TypeError, ValueError):
                pass
            for p in st.get("positions_data") or []:
                try:
                    side = "LONG" if str(p.get("type") or "").upper() == "BUY" else "SHORT"
                    positions.append({
                        "symbol": p.get("symbol") or sym,
                        "side": side,
                        "qty": float(p.get("volume") or 0),
                        "entry": float(p.get("price_open") or 0),
                        "mark": mark or float(p.get("price_open") or 0),
                        "upnl": float(p.get("profit") or 0),
                    })
                except (TypeError, ValueError):
                    continue
            for o in st.get("orders_data") or []:
                try:
                    typ = str(o.get("type") or "LIMIT").upper()
                    orders.append({
                        "symbol": o.get("symbol") or sym,
                        "side": str(o.get("side") or "BUY").upper(),
                        "type": typ,
                        "price": float(o.get("price") or 0),
                        "stop_price": 0.0,
                        "qty": float(o.get("volume") or 0),
                        "is_conditional": typ in ("STOP", "STOP_LIMIT"),
                    })
                except (TypeError, ValueError):
                    continue
    return {"positions": positions, "open_orders": orders,
            "free_margin": free_margin}


def get_symbols(token: str) -> list:
    with _LOCK:
        link = _LINKS.get(token or "")
    return (link or {}).get("symbols", [])


def assigned_symbols(token: str) -> list:
    """Symbols with a live EA instance attached (fresh within LINK_TTL)."""
    with _LOCK:
        link = _LINKS.get(token or "")
        if link is None:
            return []
        now = _now()
        return [s for s, ps in link["per_symbol"].items()
                if now - ps.get("last_seen", 0) < LINK_TTL]


def known_chart_symbols(token: str) -> list:
    """Chart symbols this token has ever posted, including after a pm2 restart
    when last_seen is still 0 (hydrate) and the EA has not POSTed yet."""
    with _LOCK:
        link = _LINKS.get(token or "")
        if link is None:
            return []
        return [s for s in (link.get("per_symbol") or {}).keys() if s]


def mid_hist_deque(token: str, symbol: str) -> deque:
    """Live bid/ask mids from every EA /state POST (regime gate + ticks/s)."""
    with _LOCK:
        link = _LINKS.get(token or "")
        if link is None:
            d = deque(maxlen=1800)
            return d
        ps = link["per_symbol"].setdefault(symbol, {
            "state": {}, "commands": [], "exec": {}, "last_seen": 0.0,
            "mid_hist": deque(maxlen=1800)})
        hist = ps.get("mid_hist")
        if hist is None or not hasattr(hist, "append"):
            hist = deque(maxlen=1800)
            ps["mid_hist"] = hist
        return hist


def chart_symbols(token: str) -> list:
    """Every chart this token has ever posted, live or not (for the picker)."""
    with _LOCK:
        link = _LINKS.get(token or "")
        if link is None:
            return []
        return [s for s in (link.get("per_symbol") or {}).keys() if s]


def drop_stale():
    """Drop only empty unused links. Keep broker symbol lists so the
    dashboard picker still has names after a restart or a quiet EA."""
    cutoff = _now() - 3600
    with _LOCK:
        dead = []
        for t, l in _LINKS.items():
            if l.get("symbols") or l.get("per_symbol"):
                continue
            if l.get("last_seen", 0) < cutoff:
                dead.append(t)
        for t in dead:
            _LINKS.pop(t, None)
