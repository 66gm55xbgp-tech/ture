"""JEV-PORTFOLIO v1.0 — frozen strategy core + normalized signal interface.

FROZEN (see JEV-PORTFOLIO-v1.0-PRODUCTION.md). This module contains NO strategy
logic of its own: it re-exports the validated NeutralGridCore/GridTuning from
neutral_grid.py (byte-identical to backup_backtest_successful_20261007_*),
pins the validated portfolio parameters as constants, and defines the
normalized Signal every execution adapter (Binance/MT5/...) must consume.

Conventions: read-only wrapper. Any future strategy change -> v1.1 module,
never an edit here.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Literal, Optional

from neutral_grid import (  # noqa: F401  (re-exported frozen logic)
    GridTuning,
    NeutralGridCore,
    adx,
    atr_pct,
    quote_volume,
)

STRATEGY_ID = "JEV-PORTFOLIO-v1.0-FROZEN"

# ── FROZEN portfolio parameters (validated 2026-10-07, 1M Binance replay) ──
FROZEN_SYMBOLS = ("ETHUSDT", "BTCUSDT", "XAUUSDT")
FROZEN_MODE = "scalp"            # GridTuning.MODES["scalp"], tp_mult=1.0
FROZEN_PER_LEVEL_USD = 800.0
FROZEN_TP_ARM_SEC = 150
FROZEN_MIN_HOLD_SEC = 150
FROZEN_SESSION_RULES = True
FROZEN_ASIA_FRAC = 0.5
FROZEN_SESS_BANK_PCT = 0.02
FROZEN_SESS_LOSS_PCT = 0.02
FROZEN_COST_BPS = 3.0            # research cost model only (not a live fee quote)
FROZEN_COMPOUND = False
FROZEN_START_EQUITY = 10000.0

# Frozen baseline (do not "improve"; regression must reproduce within tolerance)
FROZEN_BASELINE = {
    "trades": 937, "win_rate": 0.779, "net": 284.02,
    "max_dd": 367.25, "max_float": -326.92, "profit_factor": 1.51,
    "expectancy": 0.3031, "max_legs": 15, "max_loss_streak": 11,
    "breaches": 0, "min_equity": 9825.90,
}

SignalAction = Literal["BUY", "SELL", "EXIT"]


@dataclass(frozen=True)
class Signal:
    """Normalized strategy output. Adapters translate this; they never invent it."""
    action: SignalAction
    symbol: str
    quantity: float            # base-asset units (adapter converts to lots/notional)
    entry_ref: float           # reference price the signal was computed at
    stop_info: str = ""        # free-text risk context (equity-stop %, TP arm, ...)
    strategy_id: str = STRATEGY_ID
    timestamp: float = field(default_factory=time.time)
    position_id: str = ""      # filled by adapter on execution
    order_id: str = ""         # filled by adapter on execution

    def venue_symbol(self, mapping: dict) -> str:
        return mapping.get(self.symbol, self.symbol)


def make_core(mode: str = FROZEN_MODE, **overrides) -> NeutralGridCore:
    """Build the frozen core. Overrides are FORBIDDEN in production (v1.0);
    the kwarg exists only so tests can prove wrapper fidelity."""
    if overrides:
        raise RuntimeError(
            f"strategy_core v1.0 is FROZEN; overrides rejected: {sorted(overrides)}")
    os.environ.setdefault("GB_GRID_MODE", mode)
    return NeutralGridCore(GridTuning(mode=mode))


def frozen_preset() -> dict:
    """The exact validated scalp preset (copy, so callers cannot mutate MODES)."""
    return dict(GridTuning.MODES[FROZEN_MODE])
