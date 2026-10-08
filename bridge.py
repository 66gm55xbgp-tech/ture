"""
Binance Futures bridge — WebSocket price feed + REST orders.
Supports demo (testnet) and live with API liveness validation.
"""
import json
import logging
import math
import os
import threading
import time
from collections import deque
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger("hybrid.bridge")

# Process-wide futures exchangeInfo (python-binance rejects symbol=).
_EXINFO = {"at": 0.0, "info": None}
_EXINFO_LOCK = threading.Lock()
_EXINFO_TTL = 300.0


def steps_equal(a: float, b: float, step: float, tol_steps: int = 1) -> bool:
    """True if a and b are within `tol_steps` LOT_SIZE steps.

    0.0095 vs 0.0096 at step 0.0001 is 1 step — treat as the same slot so
    reconcile does not cancel/replace forever.
    """
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    step = float(step or 0)
    if step <= 0:
        return abs(a - b) < 1e-8
    return abs(round(a / step) - round(b / step)) <= int(tol_steps)


def tick_equal(a: float, b: float, tick: float) -> bool:
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    tick = float(tick or 0)
    if tick <= 0:
        return abs(a - b) < 1e-8
    return round(a / tick) == round(b / tick)


def resolve_binance_keys(user_id: Optional[str], env: str) -> Tuple[str, str, bool]:
    """(api_key, api_secret, is_testnet). Live never falls back to .env."""
    is_testnet = env in ("demo", "paper", "testnet")
    try:  # single-user: local SQLite store, then env (Settings UI or file)
        import local_store
        k, sec = local_store.resolve_binance_keys(
            "demo" if is_testnet else "live")
        if k or sec:
            return k, sec, is_testnet
    except Exception:
        pass
    if user_id:
        return "", "", is_testnet
    if is_testnet:
        return (os.getenv("BINANCE_DEMO_API_KEY", ""),
                os.getenv("BINANCE_DEMO_API_SECRET", ""), True)
    return "", "", False


def rest_client(user_id: Optional[str], env: str):
    """Signed python-binance Client, no WebSocket. None if keys missing."""
    from binance.client import Client
    key, secret, testnet = resolve_binance_keys(user_id, env)
    if not key or not secret:
        return None
    c = Client(key, secret, testnet=testnet)
    if testnet:
        c.FUTURES_URL = os.getenv("BINANCE_DEMO_URL", "https://testnet.binancefuture.com")
    return c


def futures_exchange_info(client, force: bool = False) -> dict:
    """Cached GET /fapi/v1/exchangeInfo — never pass symbol= (old python-binance)."""
    now = time.time()
    with _EXINFO_LOCK:
        if (not force and _EXINFO["info"] is not None
                and now - _EXINFO["at"] < _EXINFO_TTL):
            return _EXINFO["info"]
    info = client.futures_exchange_info()
    with _EXINFO_LOCK:
        _EXINFO["info"] = info
        _EXINFO["at"] = now
    return info

TF_MAP = {"M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m", "H1": "1h", "H4": "4h", "D1": "1d"}

_PUBLIC_CLIENT = None


def public_klines(symbol: str, interval: str, limit: int):
    """Mainnet public klines (no keys). Indicator history must come from real
    market depth — testnet klines are thin/stale and mislead ATR/ADX. Execution
    (book/orders) always stays on the venue client; only multi-bar history
    comes from here. Fail-open: raises on error, callers fall back."""
    global _PUBLIC_CLIENT
    if _PUBLIC_CLIENT is None:
        from binance.client import Client as _BC
        _PUBLIC_CLIENT = _BC("", "", testnet=False)
    kl = _PUBLIC_CLIENT.futures_klines(symbol=symbol, interval=interval,
                                        limit=limit) or []
    return [{"high": float(k[2]), "low": float(k[3]), "close": float(k[4]),
             "quote_volume": float(k[7])} for k in kl]


class BinanceBridge:
    """WebSocket-driven price feed + REST order execution for Binance Futures & Spot."""

    def __init__(self, symbol: str = "XAUUSDT", environment: str = "demo", market_type: str = "futures",
                 user_id: Optional[str] = None):
        self.symbol = symbol
        self.binance_symbol = symbol if symbol.endswith("USDT") else f"{symbol}T"
        self.environment = environment  # "demo" or "live"
        self.market_type = market_type  # "spot" or "futures"
        self.user_id = user_id          # owning Supabase user (multi-tenant isolation)
        self.platform = "binance"
        self._client = None
        self._connected = False
        self._lock = threading.Lock()

        # WebSocket price cache. bookTicker is high-frequency — bound the
        # ring so a reconnect storm cannot grow RSS. ~90s of mids at 20/s.
        self._bid: float = 0.0
        self._ask: float = 0.0
        self._last_update: float = 0.0
        self._mid_hist: deque = deque(maxlen=int(os.getenv("GB_MID_HIST_MAX", "1800")))
        self._ws = None
        self._ws_thread = None
        self._ws_stop = threading.Event()

        # Symbol info cache
        self._symbol_info: Dict = {}
        self._order_prefix = f"HG-{int(time.time())}"

        # Position ledger (reconciled against exchange — exchange is truth)
        self._tracked: Dict[int, dict] = {}
        self._last_sync: float = 0.0
        self._sync_interval: float = 3.0  # seconds between exchange reconciles (was 6 — tighter for lev10)
        # AI-tunable risk rails (% of notional). LLM may override via bridge.tp_vol_pct/sl_vol_pct.
        # 2026-09-01 rev2: SL 1.5% / TP 2.5% (R:R 1.67) — wider stop space per user, bigger wins.
        self.tp_vol_pct: float = float(os.getenv("BINANCE_TP_VOL_PCT", "0.025") or 0.025)
        self.sl_vol_pct: float = float(os.getenv("BINANCE_SL_VOL_PCT", "0.015") or 0.015)
        self.entry_offset_pct: float = float(os.getenv("BINANCE_ENTRY_PCT", "0.0004") or 0.0004)

    # ── Connection & API Liveness ──────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None

    @property
    def is_testnet(self) -> bool:
        return self.environment in ("demo", "paper", "testnet")

    def validate_liveness(self) -> dict:
        """Check API key validity and return account info. Returns {ok, balance, equity, error}."""
        try:
            from binance.client import Client

            api_key, api_secret = self._get_credentials()
            if not api_key or not api_secret:
                return {"ok": False, "balance": 0, "equity": 0,
                        "error": f"No {self.environment.upper()} API credentials in .env"}

            client = Client(api_key, api_secret, testnet=self.is_testnet)
            if self.is_testnet and self.market_type == "futures":
                demo_url = os.getenv("BINANCE_DEMO_URL", "https://testnet.binancefuture.com")
                client.FUTURES_URL = demo_url

            if self.market_type == "spot":
                acct = client.get_account()
                balances = acct.get("balances", [])
                # Find USDT balance
                for b in balances:
                    if b["asset"] == "USDT":
                        bal = float(b["free"]) + float(b.get("locked", 0))
                        logger.info(f"✅ SPOT {self.environment.upper()} OK — USDT: ${bal:.2f}")
                        return {"ok": True, "balance": bal, "equity": bal,
                                "futures_url": "https://api.binance.com"}
                return {"ok": True, "balance": 0, "equity": 0, "futures_url": "https://api.binance.com"}
            else:
                acct = client.futures_account()
                bal = float(acct.get("totalWalletBalance", 0))
                eq = float(acct.get("totalMarginBalance", 0))
                logger.info(f"✅ {self.environment.upper()} API liveness OK — balance: ${bal:.2f}")
                return {"ok": True, "balance": bal, "equity": eq,
                        "futures_url": client.FUTURES_URL if self.is_testnet else "https://fapi.binance.com"}
        except Exception as e:
            err = str(e)
            if "-2015" in err:
                return {"ok": False, "balance": 0, "equity": 0, "error": "Invalid API key"}
            elif "-1022" in err:
                return {"ok": False, "balance": 0, "equity": 0, "error": "Invalid API secret"}
            return {"ok": False, "balance": 0, "equity": 0, "error": err}

    def connect(self) -> bool:
        try:
            from binance.client import Client

            api_key, api_secret = self._get_credentials()
            if not api_key or not api_secret:
                logger.error(f"No {self.environment.upper()} API credentials")
                return False

            # Reconnect must kill the previous Client + WS thread first.
            # Clearing _ws_stop while the old loop is still in run_forever
            # used to leak a bookTicker thread per connect().
            self._stop_ws()
            if self._client:
                try:
                    self._client.close_connection()
                except Exception:
                    pass
                self._client = None

            self._client = Client(api_key, api_secret, testnet=self.is_testnet)
            if self.is_testnet and self.market_type == "futures":
                self._client.FUTURES_URL = os.getenv("BINANCE_DEMO_URL", "https://testnet.binancefuture.com")

            if self.market_type == "spot":
                self._client.get_account()
            else:
                self._client.futures_account()
            self._load_symbol_info()
            if self.market_type == "futures":
                self._set_leverage()

            self._connected = True
            self._start_ws()
            self._recover_positions()

            mkt = "SPOT" if self.market_type == "spot" else "Futures"
            env = "TESTNET" if self.is_testnet else "LIVE"
            logger.info(f"Binance {mkt} connected ({env}) | {self.binance_symbol}")
            return True

        except Exception as e:
            logger.error(f"Binance connect failed: {e}")
            self._connected = False
            return False

    def disconnect(self):
        self._stop_ws()
        try:
            if self._client:
                self._client.close_connection()
        except Exception:
            pass
        self._client = None
        self._connected = False

    # ── WebSocket Price Feed (raw websocket-client, default ON) ────────────
    #
    # v2 fix: python-binance's ThreadedWebsocketManager leaked TimerHandle
    # objects → RSS ~1.3GB → OOM. Replaced with raw `websocket-client`
    # library (already a dependency) — no asyncio, no leak, full lifecycle
    # control. Subscribes to bookTicker for real-time bid/ask.
    #
    # Enabled by default (GB_WS_FEED=1). Set GB_WS_FEED=0 to disable.

    def _stop_ws(self):
        """Close the live socket so run_forever returns, then join the thread."""
        self._ws_stop.set()
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                ws.keep_running = False
                ws.close()
            except Exception:
                pass
        t = self._ws_thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3)
        self._ws_thread = None

    def _start_ws(self):
        if os.getenv("GB_WS_FEED", "1") != "1":
            logger.info("WS price feed disabled (GB_WS_FEED=0)")
            return
        self._stop_ws()
        self._ws_stop.clear()
        self._ws_thread = threading.Thread(
            target=self._ws_loop, daemon=True,
            name=f"ws-{self.binance_symbol}")
        self._ws_thread.start()

    def _ws_loop(self):
        """Raw websocket-client loop for bookTicker stream. Auto-reconnects
        with 5s backoff on disconnect. Stops when _ws_stop is set."""
        import websocket as ws_lib

        stream = f"{self.binance_symbol.lower()}@bookTicker"
        url = f"wss://fstream.binance.com/ws/{stream}"

        while not self._ws_stop.is_set():
            try:
                ws = ws_lib.WebSocketApp(
                    url,
                    on_message=self._on_ws_message,
                    on_error=lambda w, e: logger.warning(f"WS err: {str(e)[:80]}"),
                    on_open=lambda w: logger.info(f"WS connected: {stream}"))
                self._ws = ws
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                logger.warning(f"WS exception: {str(e)[:80]}")
            finally:
                self._ws = None
            if not self._ws_stop.is_set():
                logger.info(f"WS reconnecting in 5s…")
                self._ws_stop.wait(5)

    def _on_ws_message(self, ws, message):
        """Called on every bookTicker update — updates cached bid/ask."""
        try:
            d = json.loads(message)
            self._bid = float(d["b"])
            self._ask = float(d["a"])
            now = time.time()
            self._last_update = now
            mid = (self._bid + self._ask) / 2.0
            if mid > 0:
                self._mid_hist.append((now, mid))
                cutoff = now - 90.0
                while self._mid_hist and self._mid_hist[0][0] < cutoff:
                    self._mid_hist.popleft()
        except Exception as e:
            if "json" in type(e).__name__.lower() or isinstance(e, NameError):
                logger.warning(f"WS parse: {e}")

    def _handle_ws_tick(self, msg):
        """Legacy handler — kept for backward compat."""
        self._on_ws_message(None, msg)

    # ── REST Fallback (when WS is down) ─────────────────────────────────────

    def _rest_tick(self) -> Optional[dict]:
        if not self._client:
            return None
        try:
            # Try orderbook ticker first
            if self.market_type == "spot":
                book = self._client.get_orderbook_ticker(symbol=self.binance_symbol)
                self._bid = float(book["bidPrice"])
                self._ask = float(book["askPrice"])
            else:
                book = self._client.futures_orderbook_ticker(symbol=self.binance_symbol)
                self._bid = float(book["bidPrice"])
                self._ask = float(book["askPrice"])
            self._last_update = time.time()
        except Exception:
            # Fallback: use mark price for futures, symbol ticker for spot
            try:
                mark = self._get_mark_price()
                if mark > 0:
                    self._bid = mark * 0.9995
                    self._ask = mark * 1.0005
                    self._last_update = time.time()
            except Exception:
                pass
        if self._bid > 0:
            return {"bid": self._bid, "ask": self._ask, "last": (self._bid + self._ask) / 2,
                    "time": int(self._last_update)}
        return None

    def get_tick(self) -> Optional[dict]:
        ts = time.time()
        if self._bid > 0 and (ts - self._last_update) < 5:
            return {"bid": self._bid, "ask": self._ask, "last": (self._bid + self._ask) / 2,
                    "time": int(self._last_update)}
        return self._rest_tick()

    # ── Order Execution ─────────────────────────────────────────────────────

    def place_limit_order(self, order_type: str, volume: float, price: float,
                          sl: float = None, comment: str = "") -> Optional[int]:
        """Place a LIMIT order at a specific price (for grid spacing)."""
        if not self._client:
            return None
        try:
            from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_LIMIT
            from binance.enums import TIME_IN_FORCE_GTC

            qty = self._round_qty(volume)
            qty = max(qty, self._symbol_info.get("min_qty", 0.001))
            px = self._round_price(price)
            px_str = f"{px:.{self._price_decimals()}f}"

            side = SIDE_BUY if order_type == "BUY" else SIDE_SELL
            oid = f"{self._order_prefix}-LMT-{int(time.time() * 1000)}"

            order = self._client.futures_create_order(
                symbol=self.binance_symbol, side=side, type=ORDER_TYPE_LIMIT,
                timeInForce=TIME_IN_FORCE_GTC, quantity=qty, price=px_str,
                newClientOrderId=oid)

            order_id = int(order["orderId"])
            status = order.get("status", "NEW")
            px_filled = float(order.get("avgPrice", 0)) or 0

            logger.info(f"{order_type} LIMIT {qty} {self.binance_symbol} @ {px} | id={order_id} | "
                       f"status={status} | {comment}")

            with self._lock:
                self._tracked[order_id] = {
                    "side": order_type, "qty": qty, "entry_price": px_filled if status == "FILLED" else px,
                    "sl_order_id": None, "open_time": time.time(),
                    "comment": comment, "is_limit": True, "filled": status == "FILLED"}

            return order_id
        except Exception as e:
            logger.error(f"place_limit_order failed: {e}")
            return None

    def place_order(self, order_type: str, volume: float, sl: float = None,
                    tp: float = None, comment: str = "", fee_mode: str = "taker") -> Optional[int]:
        if not self._client:
            return None
        try:
            from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT

            qty = self._round_qty(volume)
            qty = max(qty, self._symbol_info.get("min_qty", 0.001))

            min_notional = float(self._symbol_info.get("min_notional", 5.0)) or 5.0
            mark = self._get_mark_price() or 0.0
            if mark > 0 and qty * mark < min_notional:
                step = float(self._symbol_info.get("qty_step", 0.001))
                qty = math.ceil((min_notional * 1.02) / mark / step) * step
                qty = round(qty, self._symbol_info.get("qty_precision", 3))

            side = SIDE_BUY if order_type == "BUY" else SIDE_SELL
            oid = f"{self._order_prefix}-{int(time.time() * 1000)}"

            # Mode: maker (limit order) or taker (market order)
            order_price = 0.0
            if fee_mode == "maker":
                # Get current mark price
                mark = self._get_mark_price() or ((self._bid + self._ask) / 2 if self._bid > 0 and self._ask > 0 else 0.0)
                if mark <= 0:
                    mark = self._rest_tick()
                    mark = self._get_mark_price() or ((self._bid + self._ask) / 2 if self._bid > 0 and self._ask > 0 else 0.0)
                if mark <= 0:
                    return None
                offset = mark * 0.002  # 0.2% away from mark
                limit_raw = (mark - offset) if order_type == "BUY" else (mark + offset)
                ticksize = self._symbol_info.get("tick_size") or 0.1
                limit_price = round(limit_raw / ticksize) * ticksize
                limit_price = round(limit_price, self._price_decimals())
                if limit_price <= 0:
                    return None
                if self.market_type == "spot":
                    order = self._client.create_order(symbol=self.binance_symbol, side=side, type=ORDER_TYPE_LIMIT,
                        quantity=qty, price=str(limit_price), timeInForce="GTC", newClientOrderId=oid)
                else:
                    order = self._client.futures_create_order(symbol=self.binance_symbol, side=side, type=ORDER_TYPE_LIMIT,
                        quantity=qty, price=f"{limit_price:.{self._price_decimals()}f}", timeInForce="GTC", newClientOrderId=oid)
                order_id = int(order.get("orderId"))
                order_price = float(order.get("avgPrice", 0) or 0) or limit_price
                logger.info(f"MAKER {order_type} {qty} {self.binance_symbol} limit @ {limit_price} | id={order_id} | {comment}")
            else:
                if self.market_type == "spot":
                    order = self._client.create_order(symbol=self.binance_symbol, side=side, type=ORDER_TYPE_MARKET, quantity=qty, newClientOrderId=oid)
                else:
                    order = self._client.futures_create_order(symbol=self.binance_symbol, side=side, type=ORDER_TYPE_MARKET, quantity=qty, newClientOrderId=oid)
                order_id = int(order["orderId"])
                order_price = float(order.get("avgPrice", 0)) or self._get_mark_price()
                logger.info(f"TAKER {order_type} {qty} {self.binance_symbol} MARKET @ {order_price} | id={order_id} | {comment}")

            sl_id = None
            try:
                if sl:
                    sl_id = self._place_sl(sl, qty, order_type)
            except Exception as e:
                logger.warning(f"SL placement skipped: {e}")

            with self._lock:
                self._tracked[order_id] = {
                    "side": order_type, "qty": qty, "entry_price": order_price,
                    "sl_order_id": sl_id, "open_time": time.time(),
                    "comment": comment}

            return order_id
        except Exception as e:
            logger.error(f"place_order failed: {e}")
            return None

    def _place_sl(self, stop_price: float, qty: float, side: str) -> Optional[int]:
        try:
            from binance.enums import SIDE_BUY, SIDE_SELL
            s = SIDE_SELL if side == "BUY" else SIDE_BUY
            sl = self._client.futures_create_order(
                symbol=self.binance_symbol, side=s, type="STOP_MARKET",
                stopPrice=self._round_price(stop_price), quantity=qty,
                reduceOnly=True, newClientOrderId=f"{self._order_prefix}-SL-{int(time.time()*1000)}")
            return int(sl.get("algoId", sl.get("orderId")))
        except Exception as e:
            logger.warning(f"SL placement failed: {e}")
            return None

    def close_position(self, ticket: int) -> Optional[float]:
        if not self._client:
            return None
        pos = self._tracked.get(ticket)
        if not pos:
            return None

        # If it's an unfilled limit order, cancel it instead of trying to reduce
        if pos.get("is_limit"):
            try:
                self._client.futures_cancel_order(symbol=self.binance_symbol, orderId=ticket)
                logger.info(f"Cancelled unfilled limit order {ticket} ({pos.get('comment','')})")
            except Exception as e:
                # Order may have already filled or been cancelled
                er = str(e)
                if "UNKNOWN_ORDER" in er or "does not exist" in er:
                    pass
                else:
                    logger.warning(f"Cancel limit {ticket}: {e}")
            with self._lock:
                self._tracked.pop(ticket, None)
            return 0.0  # no PnL — no fill

        try:
            from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET
            cs = SIDE_SELL if pos["side"] == "BUY" else SIDE_BUY
            order = self._client.futures_create_order(
                symbol=self.binance_symbol, side=cs, type=ORDER_TYPE_MARKET,
                quantity=self._round_qty(pos["qty"]), reduceOnly=True,
                newClientOrderId=f"{self._order_prefix}-CL-{int(time.time()*1000)}")

            exit_px = float(order.get("avgPrice", 0)) or self._get_mark_price()
            d = 1.0 if pos["side"] == "BUY" else -1.0
            pnl = (exit_px - pos["entry_price"]) * pos["qty"] * d

            if pos.get("sl_order_id"):
                try:
                    self._client.futures_cancel_order(symbol=self.binance_symbol, orderId=pos["sl_order_id"])
                except Exception:
                    try:
                        self._client.futures_cancel_algo_order(symbol=self.binance_symbol, algoid=pos["sl_order_id"])
                    except Exception:
                        pass

            with self._lock:
                self._tracked.pop(ticket, None)
            logger.info(f"Closed {ticket} @ {exit_px} | PnL=${pnl:.2f}")
            return pnl
        except Exception as e:
            logger.error(f"close_position {ticket} failed: {e}")
            return None

    def close_all(self) -> int:
        count = 0
        for ticket in list(self._tracked.keys()):
            if self.close_position(ticket) is not None:
                count += 1
        self.cancel_all_orders()
        return count

    # ── Queries ──────────────────────────────────────────────────────────────

    def get_positions(self) -> List[dict]:
        """TRUE positions, reconciled against the exchange on every read.

        The exchange is the single source of truth. `_tracked` is only a
        bookkeeping cache for entry prices / tickets — it is dropped or
        adopted to match the exchange so the UI never shows ghosts.
        """
        self.sync_with_exchange()
        mark = self._get_mark_price()
        result = []
        with self._lock:
            for oid, pos in self._tracked.items():
                d = 1.0 if pos["side"] == "BUY" else -1.0
                pnl = (mark - pos["entry_price"]) * pos["qty"] * d
                result.append({"ticket": oid, "type": pos["side"], "volume": pos["qty"],
                               "price_open": pos["entry_price"], "profit": pnl,
                               "mark_price": mark})
        return result

    def get_exchange_positions(self) -> List[dict]:
        """Raw open positions straight from the exchange (authoritative)."""
        if not self._client:
            return []
        try:
            if self.market_type == "spot":
                asset = self.binance_symbol.replace("USDT", "")
                out = []
                for b in self._client.get_account().get("balances", []):
                    if b["asset"] == asset and float(b["free"]) > 0:
                        qty = float(b["free"])
                        px = float(self._client.get_symbol_ticker(symbol=self.binance_symbol)["price"])
                        if qty * px >= 5.0:
                            out.append({"side": "BUY", "qty": qty, "entry_price": px,
                                        "mark_price": px, "pnl": 0.0})
                return out
            info = self._client.futures_position_information(symbol=self.binance_symbol)
            out = []
            for p in info:
                amt = float(p.get("positionAmt", 0))
                if abs(amt) < 1e-8:
                    continue
                out.append({
                    "side": "BUY" if amt > 0 else "SELL",
                    "qty": abs(amt),
                    "entry_price": float(p.get("entryPrice", 0)),
                    "mark_price": float(p.get("markPrice", 0)),
                    "pnl": float(p.get("unRealizedProfit", 0)),
                })
            return out
        except Exception as e:
            logger.debug(f"get_exchange_positions error: {e}")
            return []

    def sync_with_exchange(self, force: bool = False) -> None:
        """Reconcile the internal `_tracked` ledger against exchange truth.

        - Exchange FLAT  -> drop every tracked ticket (kills ghost positions).
        - Exchange has a position we don't track -> adopt it so the UI shows it
          (kills the 'exchange has it but UI shows nothing' ghost).
        Throttled to `_sync_interval` seconds unless force=True.
        """
        if not self._client:
            return
        now = time.time()
        if not force and (now - self._last_sync) < self._sync_interval:
            return
        self._last_sync = now

        real = self.get_exchange_positions()
        real_qty = sum(p["qty"] for p in real)

        with self._lock:
            tracked_qty = sum(v["qty"] for v in self._tracked.values())

            # Exchange is flat -> everything tracked is a ghost
            if real_qty < 1e-8:
                if tracked_qty > 1e-8:
                    logger.warning(
                        f"🧹 sync[{self.binance_symbol}]: exchange FLAT, clearing "
                        f"{len(self._tracked)} ghost ticket(s)")
                    self._tracked.clear()
                return

            # Exchange has a position but we track nothing -> adopt it
            if tracked_qty < 1e-8:
                for p in real:
                    oid = int(time.time() * 1000) + hash(p["side"]) % 1000
                    self._tracked[oid] = {
                        "side": p["side"], "qty": p["qty"],
                        "entry_price": p["entry_price"], "sl_order_id": None,
                        "open_time": time.time(), "comment": "adopted-exchange"}
                logger.info(
                    f"🔄 sync[{self.binance_symbol}]: adopted {len(real)} exchange "
                    f"position(s) qty={real_qty}")

    def get_account_info(self) -> dict:
        if not self._client:
            return {"balance": 0, "equity": 0, "margin": 0, "free_margin": 0}
        try:
            if self.market_type == "spot":
                a = self._client.get_account()
                for b in a.get("balances", []):
                    if b["asset"] == "USDT":
                        bal = float(b["free"]) + float(b.get("locked", 0))
                        return {"balance": round(bal, 2), "equity": round(bal, 2),
                                "margin": 0, "free_margin": round(float(b["free"]), 2)}
                return {"balance": 0, "equity": 0, "margin": 0, "free_margin": 0}
            else:
                a = self._client.futures_account()
                return {"balance": float(a.get("totalWalletBalance", 0)),
                        "equity": float(a.get("totalMarginBalance", 0)),
                        "margin": float(a.get("totalInitialMargin", 0)),
                        "free_margin": float(a.get("availableBalance", 0))}
        except Exception:
            return {"balance": 0, "equity": 0, "margin": 0, "free_margin": 0}

    def get_rates(self, timeframe: str = "H1", count: int = 100) -> List[dict]:
        if not self._client:
            return []
        try:
            if self.market_type == "spot":
                interval = TF_MAP.get(timeframe, "1h")
                klines = self._client.get_klines(symbol=self.binance_symbol, interval=interval, limit=count)
                return [{"time": int(k[0] / 1000), "open": float(k[1]), "high": float(k[2]),
                         "low": float(k[3]), "close": float(k[4])} for k in klines]
            interval = TF_MAP.get(timeframe, "1h")
            klines = self._client.futures_klines(symbol=self.binance_symbol, interval=interval, limit=count)
            return [{"time": int(k[0] / 1000), "open": float(k[1]), "high": float(k[2]),
                     "low": float(k[3]), "close": float(k[4])} for k in klines]
        except Exception:
            return []

    # ── Risk rails (% of notional) ────────────────────────────────────────
    def _vol_pcts(self):
        """TP/SL as fraction of this order's USDT notional. Defaults 2.5%/1.5% (1.67:1 R/R) — wide stop space.
        Bounds 0.8-5% TP, 0.6-2% SL so LLM cannot invert the edge."""
        try:
            tp = float(getattr(self, "tp_vol_pct", None) or os.getenv("BINANCE_TP_VOL_PCT", "0.025") or 0.025)
            sl = float(getattr(self, "sl_vol_pct", None) or os.getenv("BINANCE_SL_VOL_PCT", "0.015") or 0.015)
        except (TypeError, ValueError):
            tp, sl = 0.025, 0.015
        if tp > 1: tp /= 100.0
        if sl > 1: sl /= 100.0
        tp = min(max(tp, 0.008), 0.05)
        sl = min(max(sl, 0.006), 0.02)
        return tp, sl

    def _sl_tp(self, side: str, entry: float, qty: float = None):
        """Hard rails from % of USDT order notional."""
        entry = float(entry or 0)
        if entry <= 0: return None, None
        tp_pct, sl_pct = self._vol_pcts()
        d_tp, d_sl = entry * tp_pct, entry * sl_pct
        long = str(side).upper() in ("BUY", "LONG")
        if long: sl, tp = entry - d_sl, entry + d_tp
        else: sl, tp = entry + d_sl, entry - d_tp
        if sl <= 0 or tp <= 0: return None, None
        return self._round_price(sl), self._round_price(tp)

    def cancel_all_orders(self):
        if not self._client:
            return
        try:
            self._client.futures_cancel_all_open_orders(symbol=self.binance_symbol)
        except Exception:
            pass
        try:
            algos = self._client.futures_get_open_algo_orders(symbol=self.binance_symbol)
            for a in algos:
                try:
                    self._client.futures_cancel_algo_order(symbol=self.binance_symbol, algoid=a["algoId"])
                except Exception:
                    pass
        except Exception:
            pass

    def reconcile_positions(self) -> dict:
        """Compare internal ledger vs exchange."""
        if not self._client:
            return {"tracked_qty": 0, "exchange_qty": 0, "match": True, "dropped": []}

        try:
            p = self._client.futures_position_information(symbol=self.binance_symbol)
            exchange_qty = sum(abs(float(x.get("positionAmt", 0))) for x in p)

            dropped = []
            with self._lock:
                tracked_qty = sum(v["qty"] for v in self._tracked.values())
                if exchange_qty < 1e-8 and tracked_qty > 1e-8:
                    dropped = list(self._tracked.keys())
                    self._tracked.clear()
                    logger.warning(f"Exchange shows zero — cleared {len(dropped)} tracked tickets")

            return {"tracked_qty": tracked_qty, "exchange_qty": exchange_qty,
                    "match": abs(tracked_qty - exchange_qty) < 0.0001,
                    "dropped": dropped}
        except Exception:
            return {"tracked_qty": 0, "exchange_qty": 0, "match": True, "dropped": []}

    # ── Internal Helpers ─────────────────────────────────────────────────────

    def get_exchange_min_notional(self) -> float:
        """External helper: return cached min_notional or default."""
        return float(getattr(self, '_symbol_info', {}).get('min_notional', 5.0) or 5.0)

    def qty_equal(self, a: float, b: float) -> bool:
        return steps_equal(a, b, self._symbol_info.get("qty_step", 0.001), tol_steps=1)

    def price_equal(self, a: float, b: float) -> bool:
        return tick_equal(a, b, self._symbol_info.get("tick_size") or 0)

    def exchange_position_qty(self) -> float:
        """Signed net positionAmt from the exchange (0 if flat / error)."""
        if not self._client:
            return 0.0
        try:
            info = self._client.futures_position_information(symbol=self.binance_symbol)
            return sum(float(p.get("positionAmt", 0) or 0) for p in info)
        except Exception:
            return 0.0

    def list_open_orders(self) -> List[dict]:
        """All working orders including STOP_MARKET / TAKE_PROFIT / algo."""
        if not self._client:
            return []
        out = []
        try:
            out.extend(self._client.futures_get_open_orders(symbol=self.binance_symbol) or [])
        except Exception:
            pass
        try:
            algos = self._client.futures_get_open_algo_orders(symbol=self.binance_symbol) or []
            out.extend(algos)
        except Exception:
            pass
        return out

    def classify_order(self, o: dict) -> str:
        """LADDER | TP | SL | UNKNOWN — based on Binance order type, not local memory."""
        otype = str(o.get("type") or o.get("orderType") or "LIMIT").upper()
        if otype in ("STOP", "STOP_MARKET", "STOP_LOSS", "STOP_LOSS_MARKET"):
            return "SL"
        if otype in ("TAKE_PROFIT", "TAKE_PROFIT_MARKET"):
            return "TP"
        if o.get("reduceOnly") in (True, "true", "True") and otype == "LIMIT":
            return "TP"
        if otype == "LIMIT":
            return "LADDER"
        return "UNKNOWN"

    def flatten_verified(self, attempts: int = 3) -> int:
        """Cancel every order + reduceOnly-close the position. Re-read until empty."""
        if not self._client:
            return 0
        cleaned = 0
        for _ in range(attempts):
            self.cancel_all_orders()
            amt = self.exchange_position_qty()
            if abs(amt) > 1e-8:
                try:
                    side = "BUY" if amt < 0 else "SELL"
                    self._client.futures_create_order(
                        symbol=self.binance_symbol, side=side, type="MARKET",
                        quantity=abs(amt), reduceOnly=True)
                    cleaned += 1
                except Exception as e:
                    logger.warning(f"flatten {self.binance_symbol}: {e}")
            oo = self.list_open_orders()
            if abs(self.exchange_position_qty()) < 1e-8 and not oo:
                break
            time.sleep(0.4)
        with self._lock:
            self._tracked.clear()
        return cleaned

    def ensure_stop_market(self, side: str, stop_price: float, close_position: bool = True) -> Optional[int]:
        """Place (or keep) a reduce-only STOP_MARKET on the exchange."""
        if not self._client:
            return None
        try:
            px = self._round_price(stop_price)
            kw = dict(symbol=self.binance_symbol, side=side, type="STOP_MARKET",
                      stopPrice=px, newClientOrderId=f"{self._order_prefix}-SL-{int(time.time()*1000)}")
            if close_position:
                kw["closePosition"] = True
            else:
                kw["reduceOnly"] = True
            o = self._client.futures_create_order(**kw)
            return int(o.get("orderId") or o.get("algoId") or 0) or None
        except Exception as e:
            logger.warning(f"ensure_stop_market {self.binance_symbol}: {e}")
            return None

    def _get_credentials(self) -> tuple:
        # Multi-tenant isolation (hard rule, never relaxed): a bridge bound to
        # a user resolves ONLY that user's Supabase keys. The unscoped path
        # (user_id=None) may use .env DEMO keys only — LIVE is NEVER a
        # fallback, so an unscoped bot can never silently trade the operator's
        # or another user's live account.
        env = "demo" if self.is_testnet else "live"
        key, secret, _ = resolve_binance_keys(self.user_id, env)
        return key, secret

    def _load_symbol_info(self):
        try:
            if self.market_type == "spot":
                info = self._client.get_symbol_info(self.binance_symbol)
                if info:
                    self._apply_spot_filters(info)
                    logger.info(f"Spot {self.binance_symbol}: step={self._symbol_info['qty_step']} min_notional={self._symbol_info['min_notional']}")
                    return
            else:
                info = futures_exchange_info(self._client)
                for s in info["symbols"]:
                    if s["symbol"] == self.binance_symbol:
                        self._apply_futures_filters(s)
                        logger.info(f"Symbol {self.binance_symbol}: step={self._symbol_info['qty_step']} min_notional={self._symbol_info['min_notional']}")
                        return
        except Exception as e:
            logger.error(f"Symbol info load failed: {e}")

    def _apply_spot_filters(self, info):
        self._symbol_info = {
            "price_precision": int(info.get("quoteAssetPrecision", 2)),
            "qty_precision": int(info.get("baseAssetPrecision", 3)),
            "min_qty": 0.001, "qty_step": 0.001, "min_notional": 5.0}
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                self._symbol_info.update(min_qty=float(f["minQty"]),
                    max_qty=float(f["maxQty"]), qty_step=float(f["stepSize"]))
            elif f["filterType"] == "NOTIONAL":
                self._symbol_info["min_notional"] = float(f.get("minNotional", 5.0))
            elif f["filterType"] == "PRICE_FILTER":
                self._symbol_info["tick_size"] = float(f["tickSize"])

    def _apply_futures_filters(self, s):
        self._symbol_info = {
            "price_precision": s.get("pricePrecision", 2),
            "qty_precision": s.get("quantityPrecision", 3),
            "min_qty": 0.001, "qty_step": 0.001, "min_notional": 5.0}
        for f in s["filters"]:
            if f["filterType"] == "LOT_SIZE":
                self._symbol_info.update(min_qty=float(f["minQty"]),
                    max_qty=float(f["maxQty"]), qty_step=float(f["stepSize"]))
            elif f["filterType"] == "MIN_NOTIONAL":
                self._symbol_info["min_notional"] = float(f.get("notional", 5.0))
            elif f["filterType"] == "PRICE_FILTER":
                self._symbol_info["tick_size"] = float(f["tickSize"])

    def refresh_symbol_info(self):
        """Lightweight periodic re-fetch of THIS symbol's filters.

        Binance can change the tick size mid-session (announcement
        2026-08-15: ONE/W/AKE/ON) — a stale PRICE_FILTER makes every new
        ladder order fail -4014. Rotation recreates the bridge (movers
        self-heal); this covers never-rotated anchors. Called by the bot
        every GURU_SYM_INFO_REFRESH_SEC (default 300s)."""
        if not self._client:
            return
        try:
            if self.market_type == "spot":
                info = self._client.get_symbol_info(self.binance_symbol)
                if info:
                    self._apply_spot_filters(info)
            else:
                info = futures_exchange_info(self._client, force=True)
                for s in (info or {}).get("symbols", []):
                    if s["symbol"] == self.binance_symbol:
                        self._apply_futures_filters(s)
                        return
        except Exception as e:
            logger.warning(f"Symbol info refresh failed: {e}")

    def _set_leverage(self, lev: int = None, force: bool = False):
        """Leverage with robust fallback. Exchange is truth — if the proposed
        lev is rejected, fall back to 10× and log.
        force=True (explicit user/global choice): allow 1-125×, exchange decides
        availability; unwatched values still fall back to 10×."""
        try:
            if lev is None:
                # LLM may have set bridge.leverage via _apply_llm_order_plan
                lev = int(getattr(self, "leverage", 0) or os.getenv("BINANCE_LEVERAGE", "10") or 10)
            if force:
                lev = self.resolve_leverage(lev)
            else:
                # LLM-proposed leverage is advisory; clamp to exchange-safe 3-15×
                # and prefer 10× on any error (robust monitoring).
                lev = max(3, min(int(lev), 15))
            self._client.futures_change_leverage(symbol=self.binance_symbol, leverage=lev)
            # keep instance in sync so sizing (capacity_usd) compounds correctly
            self.leverage = lev  # type: ignore
            logger.info(f"Leverage: {lev}x (robust fallback 10× if AI proposes invalid)")
        except Exception as e:
            # Fallback chain: symbol max -> 10x. Exchange brackets are truth.
            fell_back = False
            try:
                mx = self.max_leverage()
                if mx and mx != 10:
                    self._client.futures_change_leverage(symbol=self.binance_symbol, leverage=mx)
                    self.leverage = mx  # type: ignore
                    logger.warning(f"Leverage fallback {mx}x (symbol max) after {str(e)[:60]}")
                    fell_back = True
            except Exception:
                pass
            if not fell_back:
                try:
                    self._client.futures_change_leverage(symbol=self.binance_symbol, leverage=10)
                    self.leverage = 10  # type: ignore
                    logger.warning(f"Leverage fallback 10× after {str(e)[:60]}")
                except Exception:
                    pass

    _LEV_BRACKETS: dict = {}
    _LEV_BRACKETS_AT: float = 0.0

    def max_leverage(self) -> int:
        """Per-symbol bracket ceiling (cached 1h). 0 = unknown."""
        try:
            now = time.time()
            if now - BinanceBridge._LEV_BRACKETS_AT > 3600:
                BinanceBridge._LEV_BRACKETS = {}
                BinanceBridge._LEV_BRACKETS_AT = now
            if self.binance_symbol in BinanceBridge._LEV_BRACKETS:
                return BinanceBridge._LEV_BRACKETS[self.binance_symbol]
            br = self._client.futures_leverage_bracket(symbol=self.binance_symbol)
            mx = 0
            for row in (br if isinstance(br, list) else [br]):
                for b in row.get("brackets", []):
                    mx = max(mx, int(b.get("initialLeverage", 0) or 0))
            mx = max(1, min(int(mx or 0), 125))
            BinanceBridge._LEV_BRACKETS[self.binance_symbol] = mx
            return mx
        except Exception:
            return BinanceBridge._LEV_BRACKETS.get(self.binance_symbol, 0)

    def resolve_leverage(self, v) -> int:
        """'MAX'/None -> bracket ceiling (fallback 10); numbers snap to
        {5,10,25,50,75,100}, anything else -> 10."""
        try:
            if v is None:
                v = os.getenv("BINANCE_LEVERAGE", "10")
            if isinstance(v, str) and v.strip().lower() == "max":
                return self.max_leverage() or 10
            iv = int(float(v))
            return iv if iv in (5, 10, 25, 50, 75, 100) else 10
        except (TypeError, ValueError):
            return 10

    def _round_qty(self, qty: float) -> float:
        step = self._symbol_info.get("qty_step", 0.001)
        p = self._symbol_info.get("qty_precision", 3)
        return round(math.floor(qty / step) * step, p)

    def _price_decimals(self) -> int:
        tick = self._symbol_info.get("tick_size")
        if tick:
            s = f"{tick:.10f}".rstrip("0")
            return len(s.split(".")[1]) if "." in s else 0
        return self._symbol_info.get("price_precision", 2)

    def _round_price(self, price: float) -> float:
        # Binance validates LIMIT prices against the PRICE_FILTER tickSize, NOT
        # against a decimal precision. Rounding to pricePrecision can produce a
        # price that is not a multiple of the tick (e.g. -4014 rejects).
        tick = self._symbol_info.get("tick_size")
        if tick:
            dec = self._price_decimals()
            return round(round(price / tick) * tick, dec)
        return round(price, self._symbol_info.get("price_precision", 2))

    def _get_mark_price(self) -> float:
        try:
            if self.market_type == "spot":
                ticker = self._client.get_symbol_ticker(symbol=self.binance_symbol)
                return float(ticker["price"])
            return float(self._client.futures_mark_price(symbol=self.binance_symbol)["markPrice"])
        except Exception:
            return (self._bid + self._ask) / 2 if self._bid > 0 else 0.0

    def _recover_positions(self):
        """Rebuild tracked positions from exchange on restart."""
        if not self._client:
            return
        try:
            if self.market_type == "spot":
                # Spot positions = nonzero balances
                balances = self._client.get_account().get("balances", [])
                for b in balances:
                    asset = self.binance_symbol.replace("USDT", "")
                    if b["asset"] == asset and float(b["free"]) > 0:
                        qty = float(b["free"])
                        price = float(self._client.get_symbol_ticker(symbol=self.binance_symbol)["price"])
                        # Only track if it's a meaningful amount
                        if qty * price >= 5.0:
                            with self._lock:
                                self._tracked[int(time.time())] = {
                                    "side": "BUY", "qty": qty, "entry_price": price,
                                    "sl_order_id": None, "open_time": time.time(),
                                    "comment": "recovered-spot"}
                            logger.info(f"Recovered spot position: {qty} {asset} @ {price}")
                return

            p = self._client.futures_position_information(symbol=self.binance_symbol)
            open_qty = sum(abs(float(x.get("positionAmt", 0))) for x in p)
            if open_qty < 1e-8:
                return

            orders = self._client.futures_get_all_orders(symbol=self.binance_symbol, limit=100)
            recovered = 0
            for o in reversed(orders):
                coid = o.get("clientOrderId", "")
                if not coid.startswith(self._order_prefix):
                    continue
                if "-SL-" in coid or "-CL-" in coid:
                    continue
                if o["status"] != "FILLED":
                    continue
                sid = "BUY" if o["side"] == "BUY" else "SELL"
                oid = int(o["orderId"])
                if oid in self._tracked:
                    continue
                with self._lock:
                    self._tracked[oid] = {"side": sid, "qty": float(o["executedQty"]),
                        "entry_price": float(o["avgPrice"]), "sl_order_id": None,
                        "open_time": int(o.get("time", 0) / 1000), "comment": "recovered"}
                recovered += 1
            if recovered:
                logger.info(f"Recovered {recovered} positions")
        except Exception as e:
            logger.warning(f"Recovery failed: {e}")
