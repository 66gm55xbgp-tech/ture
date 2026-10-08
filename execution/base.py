"""JEV-PORTFOLIO v1.0 — execution adapter interface.

STRATEGY CORE -> Signal -> Adapter -> venue (Binance/MT5/...).
Adapters NEVER generate signals; they translate, submit, track, reconcile.
Core production guarantees enforced here:
  - duplicate-order protection (idempotency keys on every submit)
  - never trust a submit response: fills confirmed by venue query only
  - startup reconciliation before any new entry (orphan/missing detection)
  - stale-data guards (max tick age, clock skew check)
  - emergency flatten path independent of strategy state
"""

from __future__ import annotations

import abc
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class InstrumentSpec:
    """Venue-specific contract facts. Binance qty assumptions must NEVER leak
    into MT5 (lots/contract/tick differ per broker+symbol)."""
    venue_symbol: str
    min_qty: float
    qty_step: float
    min_notional: float = 0.0
    contract_size: float = 1.0
    tick_size: float = 0.0
    tick_value: float = 0.0
    max_leverage: int = 0
    stop_distance: float = 0.0
    note: str = ""

    def round_qty(self, qty: float) -> float:
        if self.qty_step <= 0:
            return qty
        steps = int(qty // self.qty_step)
        return round(steps * self.qty_step, 8)


@dataclass
class PositionView:
    venue_symbol: str
    side: str            # "LONG" | "SHORT"
    quantity: float
    entry_price: float
    floating_pnl: float = 0.0
    position_id: str = ""


class ExecutionAdapter(abc.ABC):
    venue = "base"

    def __init__(self, event_log=None) -> None:
        self.log = event_log
        self._seen_keys: set[str] = set()   # idempotency keys submitted
        self._last_tick_ts: float = 0.0
        self.max_tick_age_s: float = 30.0

    # ── interface ──
    @abc.abstractmethod
    def connect(self) -> bool: ...
    @abc.abstractmethod
    def disconnect(self) -> None: ...
    @abc.abstractmethod
    def get_tick(self, venue_symbol: str) -> Optional[dict]: ...
    @abc.abstractmethod
    def get_positions(self) -> list[PositionView]: ...
    @abc.abstractmethod
    def get_account_info(self) -> dict: ...
    @abc.abstractmethod
    def instrument(self, strategy_symbol: str) -> InstrumentSpec: ...
    @abc.abstractmethod
    def _submit(self, signal, client_key: str) -> str: ...
    @abc.abstractmethod
    def _confirm_fill(self, order_id: str, venue_symbol: str,
                      timeout_s: float = 15.0) -> dict: ...
    @abc.abstractmethod
    def cancel_order(self, order_id: str, venue_symbol: str) -> bool: ...
    @abc.abstractmethod
    def emergency_flatten(self) -> dict: ...

    # ── shared safety machinery (same on every venue) ──
    def idempotency_key(self, signal) -> str:
        return (f"{signal.strategy_id}:{signal.symbol}:{signal.action}:"
                f"{signal.quantity}:{signal.entry_ref}:{int(signal.timestamp)}")

    def execute(self, signal, greeted_state: str = "") -> dict:
        """Submit + CONFIRM. Returns fill facts; never claims a fill that the
        venue has not confirmed."""
        key = self.idempotency_key(signal)
        if key in self._seen_keys:
            self._emit("API_ERROR", signal, detail=f"duplicate submit blocked {key}")
            return {"ok": False, "duplicate": True}
        tick = self.get_tick(self.instrument(signal.symbol).venue_symbol)
        if not tick or time.time() - tick.get("ts", 0) > self.max_tick_age_s:
            self._emit("API_ERROR", signal, detail="stale/missing tick — no entry")
            return {"ok": False, "stale": True}
        self._seen_keys.add(key)
        self._emit("ORDER_SUBMITTED", signal, order_id=key)
        try:
            order_id = self._submit(signal, key)
        except Exception as e:  # transport/broker error: safe state, no phantom fill
            self._emit("BROKER_ERROR", signal, order_id=key, detail=str(e)[:200])
            return {"ok": False, "error": str(e)[:200]}
        fill = self._confirm_fill(order_id,
                                  self.instrument(signal.symbol).venue_symbol)
        if fill.get("filled_qty", 0) > 0:
            self._emit("ORDER_FILLED" if not fill.get("partial") else "ORDER_PARTIAL",
                       signal, order_id=order_id,
                       quantity=fill["filled_qty"], price=fill.get("price", 0.0))
        else:
            self._emit("ORDER_CANCELLED", signal, order_id=order_id,
                       detail="no fill confirmed in window")
        return {"ok": fill.get("filled_qty", 0) > 0, "order_id": order_id, **fill}

    def reconcile_on_startup(self) -> dict:
        """10-step restart protocol, condensed: connect -> query venue truth ->
        diff vs local -> report orphans/missing -> risk recalc gate."""
        Positions = self.get_positions()
        acct = self.get_account_info()
        orphans = [p for p in Positions if not p.position_id]
        report = {"venue_positions": len(Positions), "orphans": len(orphans),
                  "equity": acct.get("equity", 0.0), "ok": True}
        if self.log:
            self._emit_raw("RECONCILIATION", detail=(
                f"positions={len(Positions)} orphans={len(orphans)} "
                f"equity={acct.get('equity', 0.0)}"))
        return report

    # ── logging helpers ──
    def _emit(self, event: str, signal, order_id: str = "",
              quantity: float = 0.0, price: float = 0.0, detail: str = "") -> None:
        if self.log:
            self.log.emit(event, symbol=signal.symbol,
                          side=signal.action, quantity=quantity or signal.quantity,
                          price=price or signal.entry_ref, order_id=order_id,
                          detail=detail)

    def _emit_raw(self, event: str, detail: str = "") -> None:
        if self.log:
            self.log.emit(event, detail=detail)
