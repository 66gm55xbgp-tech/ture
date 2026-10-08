"""JEV-PORTFOLIO v1.0 — structured production event log (JSONL).

Every venue adapter and the risk engine emit here. Each event carries the
full context row required by the production spec: timestamp, strategy
version, mode, symbol, side, quantity, price, position/order ids, account
equity, floating + realized PnL, and risk state.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from typing import Optional

from strategy_core import STRATEGY_ID

EVENTS = ("SIGNAL", "ORDER_SUBMITTED", "ORDER_FILLED", "ORDER_PARTIAL",
          "ORDER_CANCELLED", "POSITION_OPEN", "POSITION_CLOSE", "RISK_WARNING",
          "RISK_LIMIT", "EMERGENCY_FLATTEN", "API_ERROR", "BROKER_ERROR",
          "RECONNECT", "RECONCILIATION", "SESSION_START", "SESSION_END",
          "PROP_TARGET", "PROP_BREACH")


class EventLog:
    def __init__(self, path: str = None, mode: str = "prop") -> None:
        root = pathlib.Path(__file__).parent / "logs"
        root.mkdir(exist_ok=True)
        self.path = path or str(root / "production_events.jsonl")
        self.mode = mode

    def emit(self, event: str, symbol: str = "", side: str = "",
             quantity: float = 0.0, price: float = 0.0,
             position_id: str = "", order_id: str = "",
             account_equity: float = 0.0, floating_pnl: float = 0.0,
             realized_pnl: float = 0.0, risk_state: str = "",
             detail: str = "") -> dict:
        assert event in EVENTS, f"unknown production event {event!r}"
        row = {"ts": time.time(), "event": event,
               "strategy_version": STRATEGY_ID, "mode": self.mode,
               "symbol": symbol, "side": side, "quantity": quantity,
               "price": price, "position_id": str(position_id),
               "order_id": str(order_id), "account_equity": account_equity,
               "floating_pnl": floating_pnl, "realized_pnl": realized_pnl,
               "risk_state": risk_state, "detail": detail}
        with open(self.path, "a") as f:
            f.write(json.dumps(row) + "\n")
        return row
