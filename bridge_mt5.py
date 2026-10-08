#!/usr/bin/env python3
"""MT5Bridge — TradingBridge implementation over the EA pull-model link.

The HybridGB EA (one instance per MT5 chart) POSTs its state to the main
server (/api/mt5/state) and executes the commands returned in the response.
This bridge implements the SAME interface as BinanceBridge on top of that
link (services.mt5_link):

  place_limit_order / place_order  -> queue OPEN_*_LIMIT / OPEN_* command,
                                      wait for the EA's exec echo (~1-5s)
  close_position / close_all       -> CLOSE_TICKET / CLOSE_ALL
  cancel_all_orders                -> CANCEL_ALL
  get_tick / get_rates             -> cached state from the last POST
  get_positions / get_account_info -> cached positions/account

Environment: derived from the EA's ACCOUNT_TRADE_MODE at connect()
("demo" or "live") and validated against the requested env.
"""
import logging
import time
import threading
from collections import deque
from typing import Optional, List

from services import mt5_link

logger = logging.getLogger("hybrid.bridge.mt5")


class _MT5ClientShim:
    """python-binance-style client over the EA link. GridSubBot drives the
    bridge through bridge._client.<futures_*> calls — this shim translates
    them into EA commands / cached state so the unmodified Binance grid
    engine works on MT5. MT5-only: nothing here touches other platforms."""

    def __init__(self, bridge):
        self._b = bridge

    def futures_position_information(self, symbol=None):
        st = mt5_link.get_state(self._b.token, self._b.symbol)
        mark = (self._b.get_tick() or {}).get("last", 0.0)
        out = []
        for p in st.get("positions_data", []):
            try:
                vol = float(p.get("volume", 0))
                side = 1 if p.get("type") == "BUY" else -1
                out.append({
                    "symbol": self._b.symbol,
                    "positionAmt": str(vol * side),
                    "unRealizedProfit": str(float(p.get("profit", 0) or 0)),
                    "entryPrice": str(float(p.get("price_open", 0) or 0)),
                    "markPrice": str(mark),
                })
            except (ValueError, TypeError):
                continue
        return out

    def futures_klines(self, symbol=None, interval="5m", limit=100, **kw):
        tfmap = {"1m": "bars_m1", "3m": "bars_m1", "5m": "bars_m5",
                 "15m": "bars_m15", "30m": "bars_m15", "1h": "bars_h1",
                 "M1": "bars_m1", "M5": "bars_m5", "M15": "bars_m15", "H1": "bars_h1"}
        st = mt5_link.get_state(self._b.token, self._b.symbol)
        bars = st.get(tfmap.get(interval, "bars_m5"), [])[-limit:]
        out = []
        for b in bars:
            try:
                t = int(b["t"])
                close = float(b["c"])
                tv = float(b.get("v", 0) or 0)
                # quote-volume proxy: tick count x price — an activity level
                # for the participation gate (EA sends tick counts, not $)
                qv = tv * close
                out.append([t, str(b["o"]), str(b["h"]), str(b["l"]), str(b["c"]),
                            str(tv), t * 1000, str(qv), 0, "0", "0", "0"])
            except (KeyError, ValueError, TypeError):
                continue
        return out

    def futures_get_open_orders(self, symbol=None):
        st = mt5_link.get_state(self._b.token, self._b.symbol)
        out = []
        for o in st.get("orders_data", []):
            try:
                out.append({"orderId": int(o["ticket"]), "symbol": self._b.symbol,
                            "side": o.get("side", "BUY"),
                            "price": str(o.get("price", 0)),
                            "origQty": str(o.get("volume", 0)),
                            "type": "LIMIT", "status": "NEW"})
            except (ValueError, TypeError, KeyError):
                continue
        return out

    def futures_cancel_order(self, symbol=None, orderId=None, **kw):
        r = self._b._command("CANCEL_TICKET", ticket=int(orderId or 0))
        return {"status": "CANCELED"} if r else {"status": "UNKNOWN"}

    def futures_get_order(self, symbol=None, orderId=None, **kw):
        oid = int(orderId or 0)
        for o in self.futures_get_open_orders(symbol):
            if int(o["orderId"]) == oid:
                return {"orderId": oid, "status": "NEW",
                        "executedQty": "0", "origQty": o["origQty"]}
        # Missing on the book is NOT a fill on MT5 (IOC expire, cancel, stale
        # ticket). Treat as canceled so we do not scale-in on a ghost fill.
        return {"orderId": oid, "status": "CANCELED",
                "executedQty": "0", "origQty": "0"}

    def futures_cancel_all_open_orders(self, symbol=None):
        r = self._b._command("CANCEL_ALL")
        return {"status": "CANCELED"} if r else {"status": "UNKNOWN"}

    def futures_create_order(self, symbol=None, **kw):
        otype = (kw.get("type") or "MARKET").upper()
        side = kw.get("side", "BUY")
        qty = float(kw.get("quantity", 0) or 0)
        if otype == "MARKET":
            action = "OPEN_BUY" if side == "BUY" else "OPEN_SELL"
            r = self._b._command(action, volume=qty)
            ticket = int(r.get("ticket", 0)) if r else 0
            return {"orderId": ticket, "status": "FILLED" if ticket else "REJECTED"}
        if otype == "LIMIT":
            action = "OPEN_BUY_LIMIT" if side == "BUY" else "OPEN_SELL_LIMIT"
            r = self._b._command(action, volume=qty,
                                 price=float(kw.get("price", 0) or 0))
            ticket = int(r.get("ticket", 0)) if r else 0
            return {"orderId": ticket, "status": "NEW" if ticket else "REJECTED"}
        if otype == "STOP_MARKET":
            # Exchange-enforced trail/loss stops (GuruAI safety layer):
            # translate to an EA pending STOP at the stop price. `side` is
            # already the closing side (SELL for a long, BUY for a short).
            # closePosition=True without quantity -> use current position size.
            qty = float(kw.get("quantity", 0) or 0)
            if qty <= 0 and kw.get("closePosition"):
                info = self._b._position_info()
                qty = abs(sum(float(p.get("positionAmt", 0) or 0) for p in info))
            action = "OPEN_SELL_STOP" if side == "SELL" else "OPEN_BUY_STOP"
            r = self._b._command(action, volume=qty,
                                 price=float(kw.get("stopPrice", 0) or 0))
            ticket = int(r.get("ticket", 0)) if r else 0
            return {"orderId": ticket, "status": "NEW" if ticket else "REJECTED"}
        raise ValueError(f"MT5 grid path does not support order type {otype}")


class MT5Bridge:
    def __init__(self, token: str, symbol: str, environment: str = "demo",
                 user_id: str = None):
        self.token = (token or "").strip()
        self.symbol = symbol
        self.requested_env = environment
        self.user_id = user_id
        self.platform = "mt5"
        self._connected = False
        self._env_detected = ""
        self._symbol_info: dict = {}
        self._lock = threading.Lock()
        self.binance_symbol = symbol          # GridSubBot compat
        self._client = _MT5ClientShim(self)   # GridSubBot compat (binance-style calls)

    # ── lifecycle ────────────────────────────────────────────────────────
    @property
    def is_testnet(self) -> bool:
        return self._env_detected == "demo"

    @property
    def environment(self) -> str:
        return self._env_detected or self.requested_env

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _client(self):
        """Compat shim — some shared code touches bridge._client; the MT5
        link has no HTTP client object."""
        return None

    def connect(self, require_heartbeat: bool = True) -> bool:
        if not mt5_link.link_exists(self.token):
            return False
        acct = mt5_link.get_account(self.token) or {}
        mode = (acct.get("trade_mode") or "").lower()
        if mode:
            self._env_detected = "live" if mode == "live" else "demo"
            if (require_heartbeat and self.requested_env
                    and self._env_detected != self.requested_env):
                return False
        else:
            self._env_detected = self.requested_env or "demo"
        if require_heartbeat and not mt5_link.is_connected(self.token):
            return False
        # symbol specs from registration (or last persisted broker list)
        for s in mt5_link.get_symbols(self.token):
            name = s.get("name") if isinstance(s, dict) else s
            if name == self.symbol:
                if not isinstance(s, dict):
                    break
                digits = int(s.get("digits", 5))
                point = float(s.get("point", 0.0)) or 10 ** -digits
                lot_step = float(s.get("lot_step", 0.01)) or 0.01
                self._symbol_info = {
                    "digits": digits, "point": point,
                    "price_precision": digits,
                    "qty_precision": max(0, round(-1.0 * __import__("math").log10(lot_step))),
                    "qty_step": lot_step,
                    "min_qty": float(s.get("lot_min", 0.01)),
                    "max_qty": float(s.get("lot_max", 100.0)),
                    "tick_size": point,
                    "stops_level": int(s.get("stops_level", 0)),
                }
                break
        self._connected = True
        return True

    def disconnect(self):
        self._connected = False

    def price_equal(self, a, b) -> bool:
        tick = float(self._symbol_info.get("tick_size") or self._symbol_info.get("point") or 0.01)
        if tick <= 0:
            return abs(float(a) - float(b)) < 1e-6
        return abs(float(a) - float(b)) <= tick * 1.01

    def qty_equal(self, a, b) -> bool:
        step = float(self._symbol_info.get("qty_step") or 0.01)
        return abs(float(a) - float(b)) <= max(step, 1e-8) * 0.51

    def validate_liveness(self) -> dict:
        ok = mt5_link.is_connected(self.token)
        acct = mt5_link.get_account(self.token)
        return {"ok": ok, "balance": float(acct.get("balance", 0) or 0),
                "equity": float(acct.get("equity", 0) or 0),
                "error": None if ok else "MT5 EA not connected"}

    # ── market data ──────────────────────────────────────────────────────
    def _rest_tick(self) -> Optional[dict]:
        """Compat: the EA pull model has no websocket — state is always the
        freshest view, so this is just get_tick()."""
        return self.get_tick()

    @property
    def _mid_hist(self) -> deque:
        """Same deque the EA fills on every /state POST (~4/s). Guard + chart."""
        return mt5_link.mid_hist_deque(self.token, self.symbol)

    def get_tick(self) -> Optional[dict]:
        st = mt5_link.get_state(self.token, self.symbol)
        bid = float(st.get("bid", 0) or 0)
        ask = float(st.get("ask", 0) or 0)
        if bid <= 0 or ask <= 0:
            return None
        last = (bid + ask) / 2.0
        now = time.time()
        return {"bid": bid, "ask": ask, "last": last, "time": int(now)}

    def get_rates(self, timeframe: str = "M15", count: int = 50) -> List[dict]:
        tf = (timeframe or "M15").upper()
        key = {"M1": "bars_m1", "M5": "bars_m5", "M15": "bars_m15",
               "H1": "bars_h1"}.get(tf, "bars_m15")
        st = mt5_link.get_state(self.token, self.symbol)
        bars = st.get(key, [])
        out = []
        for b in bars[-count:]:
            try:
                out.append({"time": int(b["t"]), "open": float(b["o"]),
                            "high": float(b["h"]), "low": float(b["l"]),
                            "close": float(b["c"]), "volume": float(b.get("v", 0))})
            except (KeyError, ValueError, TypeError):
                continue
        return out

    def get_symbol_info(self) -> dict:
        return dict(self._symbol_info)

    # ── order helpers ────────────────────────────────────────────────────
    def _round_price(self, price: float) -> float:
        digits = int(self._symbol_info.get("digits", 5))
        return round(price, digits)

    def _round_qty(self, qty: float) -> float:
        step = float(self._symbol_info.get("qty_step", 0.01)) or 0.01
        return max(int(qty / step + 1e-9) * step, step)

    def _command(self, action: str, wait: bool = True, timeout: float = 20.0,
                 **params) -> Optional[dict]:
        cmd_id = f"mt5-{int(time.time() * 1000)}-{id(object())%99999}"
        if str(action).startswith("OPEN") and not mt5_link.is_connected(self.token):
            logger.warning(f"MT5 refuse {action} — EA offline, not queueing")
            return None
        try:
            mt5_link.queue_command(self.token, self.symbol,
                                   {"action": action, "command_id": cmd_id, **params})
        except KeyError:
            return None
        if not wait:
            return {"queued": True}
        ex = mt5_link.wait_exec(self.token, self.symbol, cmd_id, timeout=timeout)
        if ex is None:
            return None
        retcode = int(ex.get("retcode", 0))
        if retcode != 10009:   # TRADE_RETCODE_DONE
            return None
        ticket = int(ex.get("ticket", 0) or 0)
        return {"ticket": ticket} if ticket else {"ok": True}

    # ── orders ───────────────────────────────────────────────────────────
    def place_limit_order(self, order_type: str, volume: float, price: float,
                          sl: float = None, comment: str = "") -> Optional[int]:
        """order_type: BUY (buy limit) | SELL (sell limit). Returns ticket."""
        action = "OPEN_BUY_LIMIT" if order_type.upper() == "BUY" else "OPEN_SELL_LIMIT"
        r = self._command(action, volume=self._round_qty(volume),
                          price=self._round_price(price))
        if r and r.get("ticket"):
            return r["ticket"]
        return None

    def place_order(self, order_type: str, volume: float, sl: float = None,
                    tp: float = None, comment: str = "") -> Optional[int]:
        """Market order. order_type: BUY | SELL. Returns position ticket."""
        action = "OPEN_BUY" if order_type.upper() == "BUY" else "OPEN_SELL"
        r = self._command(action, volume=self._round_qty(volume), sl=float(sl or 0))
        if r and r.get("ticket"):
            return r["ticket"]
        return None

    def close_position(self, ticket: int) -> Optional[float]:
        r = self._command("CLOSE_TICKET", ticket=int(ticket))
        return float(r.get("ticket", 0) or 0) if r else None

    def close_all(self) -> int:
        before = len(self.get_positions())
        r = self._command("CLOSE_ALL", timeout=20.0)
        if r is None:
            # still queued for the next EA POST
            return before
        return max(before, 1 if r else 0)

    def cancel_all_orders(self):
        self._command("CANCEL_ALL", timeout=20.0)

    # ── reads ────────────────────────────────────────────────────────────
    def get_positions(self) -> List[dict]:
        st = mt5_link.get_state(self.token, self.symbol)
        mark = (self.get_tick() or {}).get("last", 0.0)
        out = []
        for p in st.get("positions_data", []):
            try:
                side = p.get("type", "BUY")
                vol = float(p.get("volume", 0))
                op = float(p.get("price_open", 0))
                profit = float(p.get("profit", 0))
                out.append({"ticket": int(p.get("ticket", 0)), "type": side,
                            "volume": vol, "price_open": op, "profit": profit,
                            "mark_price": mark})
            except (ValueError, TypeError):
                continue
        return out

    def get_exchange_positions(self) -> List[dict]:
        return self.get_positions()

    def sync_with_exchange(self, force: bool = False) -> None:
        pass   # state IS the exchange view (EA posts it)

    def reconcile_positions(self) -> dict:
        pos = self.get_positions()
        return {"tracked_qty": sum(p["volume"] for p in pos),
                "exchange_qty": sum(p["volume"] for p in pos),
                "match": True, "dropped": []}

    def get_account_info(self) -> dict:
        st = mt5_link.get_state(self.token, self.symbol)
        acct = mt5_link.get_account(self.token)
        balance = float(st.get("balance", 0) or acct.get("balance", 0) or 0)
        equity = float(st.get("equity", 0) or acct.get("equity", 0) or 0)
        free_margin = float(st.get("freeMargin", 0) or 0)
        return {"balance": balance, "equity": equity,
                "free_margin": free_margin if free_margin > 0 else balance,
                "currency": acct.get("currency", "USD"),
                "leverage": acct.get("leverage", 100)}

    def get_open_orders(self) -> List[dict]:
        st = mt5_link.get_state(self.token, self.symbol)
        return st.get("orders_data", [])

    def get_min_lot(self) -> float:
        return float(self._symbol_info.get("min_qty", 0.01))
