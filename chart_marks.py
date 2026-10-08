"""Day-session trade marks for the desk chart.

TV Lightweight Charts pins series markers to a *bar time* (docs:
tradingview/lightweight-charts setMarkers). Pinning to the last candle is
what made arrows dance. Opens/closes are stored at the fill minute and
wiped with guru_session_book at the next session open.

In-memory FIFO is the live source; session_book.levels (kind=open/close)
is the restart backup (same table, fail-soft).
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict
from typing import List

_LOCK = threading.Lock()
_MARKS: dict = {}          # (user, plat, symbol, day) -> list[dict]
_OPEN: dict = defaultdict(list)  # unmatched opens
_LAST_QTY: dict = {}


def _day() -> str:
    try:
        from session_book import trading_session_date
        return trading_session_date()
    except Exception:
        return time.strftime("%Y-%m-%d", time.gmtime())


def _key(user_id: str, platform: str, symbol: str):
    return (user_id or "", (platform or "binance").lower(),
            (symbol or "").upper(), _day())


def _bar_time(ts: float = None) -> int:
    t = int(ts or time.time())
    return t - (t % 60)


def list_marks(user_id: str, platform: str, symbol: str) -> List[dict]:
    with _LOCK:
        rows = list(_MARKS.get(_key(user_id, platform, symbol), []))
    if rows:
        return rows
    try:
        from session_book import fetch_symbol
        row = (fetch_symbol(user_id, platform, symbol, "live")
               or fetch_symbol(user_id, platform, symbol, "demo"))
        lv = (row or {}).get("levels") or []
        out = [x for x in lv if isinstance(x, dict)
               and (x.get("kind") or "") in ("open", "close")]
        if out:
            with _LOCK:
                _MARKS.setdefault(_key(user_id, platform, symbol), list(out))
        return out
    except Exception:
        return []


def _persist(user_id, platform, symbol):
    uid = str(user_id or "")
    if len(uid) < 32 or uid.count("-") < 4:
        return
    try:
        from session_book import fetch_symbol, upsert_symbol
        live = (fetch_symbol(user_id, platform, symbol, "live")
                or fetch_symbol(user_id, platform, symbol, "demo") or {})
        ladder = [x for x in (live.get("levels") or [])
                  if isinstance(x, dict)
                  and (x.get("kind") or "grid") not in ("open", "close")]
        marks = list_marks(user_id, platform, symbol)
        upsert_symbol(
            user_id=user_id, env=live.get("env") or "live",
            platform=platform, symbol=symbol,
            bot_id=live.get("bot_id") or "",
            bot_name=live.get("bot_name") or "",
            last_price=float(live.get("last_price") or 0),
            net_qty=float(live.get("net_qty") or 0),
            realized_pnl=float(live.get("realized_pnl") or 0),
            unrealized_pnl=float(live.get("unrealized_pnl") or 0),
            levels=ladder + marks)
    except Exception:
        pass


def note_delta(user_id: str, platform: str, symbol: str,
               prev_qty: float, new_qty: float, price: float,
               ts: float = None) -> list:
    """Record a position qty change as open and/or close marks (FIFO)."""
    try:
        prev = float(prev_qty or 0)
        new = float(new_qty or 0)
        px = float(price or 0)
    except (TypeError, ValueError):
        return []
    d = new - prev
    if abs(d) < 1e-8 or px <= 0 or not user_id or not symbol:
        return []
    t = _bar_time(ts)
    plat = (platform or "binance").lower()
    sym = symbol.upper()
    k = _key(user_id, plat, sym)
    added = []
    with _LOCK:
        bucket = _MARKS.setdefault(k, [])
        opens = _OPEN[k]
        qty = abs(d)
        if (prev >= 0 and d > 0) or (prev <= 0 and new < 0 and d < 0):
            side = "BUY" if d > 0 else "SELL"
            m = {"id": uuid.uuid4().hex[:12], "kind": "open", "side": side,
                 "price": px, "time": t, "qty": qty}
            bucket.append(m)
            opens.append(m)
            added.append(m)
        else:
            side = "SELL" if d < 0 else "BUY"
            remain = qty
            while remain > 1e-8 and opens:
                o = opens[0]
                take = min(float(o.get("qty") or remain), remain)
                o_side = (o.get("side") or "BUY").upper()
                o_px = float(o.get("price") or px)
                if o_side in ("BUY", "LONG"):
                    pnl = (px - o_px) * take
                else:
                    pnl = (o_px - px) * take
                c = {"id": uuid.uuid4().hex[:12], "kind": "close",
                     "side": side, "price": px, "time": t, "qty": take,
                     "pnl": round(pnl, 4), "open_id": o.get("id"),
                     "open_time": o.get("time"), "open_price": o_px,
                     "open_side": o_side}
                bucket.append(c)
                added.append(c)
                oq = float(o.get("qty") or 0) - take
                if oq <= 1e-8:
                    opens.pop(0)
                else:
                    o["qty"] = oq
                remain -= take
            if remain > 1e-8:
                m = {"id": uuid.uuid4().hex[:12], "kind": "open", "side": side,
                     "price": px, "time": t, "qty": remain}
                bucket.append(m)
                opens.append(m)
                added.append(m)
        if len(bucket) > 400:
            _MARKS[k] = bucket[-400:]
    if added:
        _persist(user_id, plat, sym)
    return added


def note_from_bot(bot, prev_qty: float, new_qty: float, price: float):
    uid = getattr(bot, "user_id", "") or ""
    br = getattr(bot, "bridge", None)
    plat = str(getattr(br, "platform", "") or getattr(bot, "platform", "")
               or "binance").lower()
    sym = getattr(bot, "symbol", "") or ""
    return note_delta(uid, plat, sym, prev_qty, new_qty, price)
