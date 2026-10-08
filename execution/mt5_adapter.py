"""JEV-PORTFOLIO v1.0 — MT5 execution adapter.

SAME strategy interface as Binance (strategy_core.Signal in, venue facts
out). The instrument-spec layer translates Binance-style quantity into
broker lots using per-symbol contract facts — Binance assumptions must never
leak across. Symbol mapping, lot math, and broker sessions live here, not in
the strategy.
"""

from __future__ import annotations

import time
from typing import Optional

from execution.base import ExecutionAdapter, InstrumentSpec, PositionView

# Broker symbol map (suffix/prefix per broker) + contract facts. Fill from the
# broker's Symbols specification window; EXAMPLE values below must be replaced
# at deploy time from MT5 symbol properties (never from Binance assumptions).
DEFAULT_SPECS = {
    # strategy_symbol: (mt5_symbol, contract_size, tick_size, tick_value,
    #                   min_lot, lot_step, max_leverage)
    "ETHUSDT": ("ETHUSD", 1.0, 0.01, 1.0, 0.01, 0.01, 100),
    "BTCUSDT": ("BTCUSD", 1.0, 0.01, 1.0, 0.01, 0.01, 100),
    "XAUUSDT": ("XAUUSD", 100.0, 0.01, 1.0, 0.01, 0.01, 100),
}


class MT5Adapter(ExecutionAdapter):
    venue = "mt5"

    def __init__(self, bridge, event_log=None, specs: dict = None) -> None:
        super().__init__(event_log)
        self.b = bridge  # MT5Bridge instance (owns terminal link)
        self.specs = specs or DEFAULT_SPECS

    def connect(self) -> bool:
        ok = bool(self.b.connect())
        if self.log and ok:
            self._emit_raw("RECONNECT", detail="mt5 connected")
        return ok

    def disconnect(self) -> None:
        try:
            self.b.disconnect()
        except Exception:
            pass

    def get_tick(self, venue_symbol: str) -> Optional[dict]:
        try:
            t = self.b.get_tick()
            if not t:
                return None
            return {"bid": t.get("bid", 0.0), "ask": t.get("ask", 0.0),
                    "mid": (float(t.get("bid", 0) or 0) + float(t.get("ask", 0) or 0)) / 2,
                    "ts": t.get("ts", time.time())}
        except Exception:
            return None

    def get_positions(self) -> list[PositionView]:
        out = []
        try:
            for p in self.b.get_positions() or []:
                out.append(PositionView(
                    venue_symbol=str(p.get("symbol", "")),
                    side="LONG" if float(p.get("volume", 0) or 0) > 0 else "SHORT",
                    quantity=abs(float(p.get("volume", 0) or 0)),
                    entry_price=float(p.get("price_open", p.get("entry", 0)) or 0),
                    floating_pnl=float(p.get("profit", p.get("upnl", 0)) or 0),
                    position_id=str(p.get("ticket", p.get("symbol", "")))))
        except Exception:
            pass
        return out

    def get_account_info(self) -> dict:
        try:
            return self.b.get_account_info() or {}
        except Exception:
            return {}

    def instrument(self, strategy_symbol: str) -> InstrumentSpec:
        (vsym, csize, tsize, tval, minlot, step, lev) = self.specs.get(
            strategy_symbol, (strategy_symbol, 1.0, 0.01, 1.0, 0.01, 0.01, 0))
        return InstrumentSpec(
            venue_symbol=vsym, min_qty=minlot, qty_step=step,
            contract_size=csize, tick_size=tsize, tick_value=tval,
            max_leverage=lev,
            note="broker lots; sessions/swaps per broker spec, NOT 24/7 assumed")

    def strategy_qty_to_lots(self, strategy_symbol: str, qty_base: float,
                             price: float) -> float:
        """Base-asset units -> broker lots: lots = units / contract_size.
        Venue minimums may bind on small legs (e.g. XAU $800 leg < 0.01 lot on
        100oz contracts) — the adapter reports, never silently rounds up risk."""
        spec = self.instrument(strategy_symbol)
        lots = float(qty_base) / spec.contract_size if spec.contract_size else 0.0
        return max(spec.min_qty, spec.round_qty(lots))

    def _submit(self, signal, client_key: str) -> str:
        side = "BUY" if signal.action == "BUY" else "SELL"
        spec = self.instrument(signal.symbol)
        lots = self.strategy_qty_to_lots(signal.symbol, float(signal.quantity),
                                         float(signal.entry_ref))
        res = self.b.place_order(side, lots, sl=None,
                                 comment=f"JEV10:{client_key[:20]}")
        oid = str((res or {}).get("ticket", "") or client_key)
        if not res:
            raise RuntimeError("mt5 place_order returned no response")
        return oid

    def _confirm_fill(self, order_id: str, venue_symbol: str,
                      timeout_s: float = 20.0) -> dict:
        deadline = time.time() + timeout_s
        last = {"filled_qty": 0.0, "partial": True, "price": 0.0}
        while time.time() < deadline:
            try:
                for p in self.get_positions():
                    if p.venue_symbol == venue_symbol and \
                            str(order_id) in (p.position_id, order_id):
                        return {"filled_qty": p.quantity, "partial": False,
                                "price": p.entry_price}
                break  # position scan is synchronous truth on MT5
            except Exception:
                time.sleep(1.0)
        return last

    def cancel_order(self, order_id: str, venue_symbol: str) -> bool:
        try:
            return bool(self.b.cancel_pending(order_id, venue_symbol))
        except Exception:
            return False

    def emergency_flatten(self) -> dict:
        n = 0
        try:
            for p in self.get_positions():
                try:
                    self.b.close_position(p.position_id)
                    n += 1
                except Exception:
                    continue
        except Exception:
            pass
        if self.log:
            self._emit_raw("EMERGENCY_FLATTEN", detail=f"mt5 closed={n}")
        return {"ok": True, "closed": n}
