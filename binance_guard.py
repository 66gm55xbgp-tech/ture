"""Binance wipeout / regime gate — sits IN FRONT of the grid, does not replace it.

Deterministic v1 (no ML, no L2, no auto-reversal). Binance-only. Do not import
from mt5_guru.

States: SAFE → CAUTION → REDUCE → PAUSE → UNWIND → EXTREME
"""
from __future__ import annotations

import os
import time
from typing import Dict, List, Optional, Sequence, Tuple

STATES = ("SAFE", "CAUTION", "REDUCE", "PAUSE", "UNWIND", "EXTREME")


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


# Wallet vs session peak. Matches GURU_BANK_EMERGENCY_PCT default (10%).
WIPEOUT_PCT = _f("GURU_BANK_EMERGENCY_PCT", 0.10)


def crash_threshold(raw=None) -> float:
    """Adverse-move halt as a fraction (0.05 = 5%). Accepts 0.05 or 5."""
    try:
        x = float(raw if raw is not None else os.getenv("GURU_CRASH_GUARD", "0.05") or 0.05)
    except (TypeError, ValueError):
        x = 0.05
    if x > 1.0:
        x /= 100.0
    return min(max(x, 0.005), 0.15)


def wallet_wiped(wallet: float, peak: float, pct: float = None) -> bool:
    """True when equity has given back `pct` of the desk peak (default 10%)."""
    try:
        p = float(pct if pct is not None else WIPEOUT_PCT)
    except (TypeError, ValueError):
        p = WIPEOUT_PCT
    if p > 1.0:
        p /= 100.0
    p = min(max(p, 0.05), 0.15)
    try:
        w, k = float(wallet or 0), float(peak or 0)
    except (TypeError, ValueError):
        return False
    return k > 0 and w > 0 and w <= k * (1.0 - p)
# Volatility-normalized |move| (current return / recent vol) → caution/pause.
Z_CAUTION = _f("GB_GUARD_Z_CAUTION", 2.5)
Z_PAUSE = _f("GB_GUARD_Z_PAUSE", 5.0)
Z_UNWIND = _f("GB_GUARD_Z_UNWIND", 7.0)


def ticks_last_sec(hist: Sequence[Tuple[float, float]], now: float = None,
                   limit: int = 40) -> List[float]:
    """Mids from the last second, newest first. Reads `_mid_hist` in place
    — does not copy a new buffer beyond this return list."""
    now = time.time() if now is None else now
    out: List[float] = []
    for ts, mid in reversed(list(hist or [])):
        try:
            if now - float(ts) > 1.0:
                break
            px = float(mid)
        except (TypeError, ValueError):
            continue
        if px > 0:
            out.append(px)
        if len(out) >= int(limit):
            break
    return out


def ticks_per_sec(hist: Sequence[Tuple[float, float]], now: float = None) -> int:
    """How many WS mids landed in the last second (bookTicker rate)."""
    return len(ticks_last_sec(hist, now=now, limit=10_000))


def _return_over(hist: Sequence[Tuple[float, float]], seconds: float) -> Optional[float]:
    """(mid_now - mid_then) / mid_then over the last `seconds`. hist is (ts, mid)."""
    if not hist or len(hist) < 2:
        return None
    now_ts, now_mid = hist[-1]
    if now_mid <= 0:
        return None
    target = now_ts - seconds
    then_mid = None
    for ts, mid in reversed(hist):
        then_mid = mid
        if ts <= target:
            break
    if not then_mid or then_mid <= 0:
        return None
    return (now_mid - then_mid) / then_mid


def _vol(hist: Sequence[Tuple[float, float]], window_s: float = 60.0) -> float:
    """Stdev of 1s returns over `window_s`. Floor so z-scores stay defined."""
    if not hist or len(hist) < 4:
        return 1e-4
    rets = []
    prev = None
    cutoff = hist[-1][0] - window_s
    for ts, mid in hist:
        if ts < cutoff or mid <= 0:
            prev = mid
            continue
        if prev and prev > 0:
            rets.append((mid - prev) / prev)
        prev = mid
    if len(rets) < 3:
        return 1e-4
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return max(var ** 0.5, 1e-4)


def evaluate(market: dict, grid: dict) -> dict:
    """Return {state, risk_score, reason_codes, recommended_action}.

    market: {
      wallet, peak_wallet,          # exchange wallet vs session peak
      mid_hist: [(ts, mid), ...],   # optional WS mids
      adx: float,                   # optional macro
      adx_range_max: float,
    }
    grid: {
      position_qty,                 # exchange qty (signed)
      open_orders,                  # count of exchange open orders
      exposure_frac,                # 0..1 of max grid depth used
    }
    """
    reasons: List[str] = []
    score = 0.0

    wallet = float(market.get("wallet") or 0)
    peak = float(market.get("peak_wallet") or 0)
    try:
        wipe_pct = float(market.get("wipeout_pct") or WIPEOUT_PCT)
    except (TypeError, ValueError):
        wipe_pct = WIPEOUT_PCT
    wipe_pct = min(max(wipe_pct, 0.02), 0.80)
    if peak > 0 and wallet > 0 and wallet <= peak * (1.0 - wipe_pct):
        return {
            "state": "EXTREME",
            "risk_score": 100.0,
            "reason_codes": ["wallet_wipeout"],
            "recommended_action": "ACCOUNT_KILL",
            "direction": "NONE",
            "confidence": 1.0,
        }

    hist = market.get("mid_hist") or []
    try:
        vol_floor = float(market.get("vol_floor") or 1e-4)
    except (TypeError, ValueError):
        vol_floor = 1e-4
    vol = max(_vol(hist), vol_floor)
    r1 = _return_over(hist, 1) or 0.0
    r5 = _return_over(hist, 5) or 0.0
    r30 = _return_over(hist, 30) or 0.0
    z1 = abs(r1) / vol
    z5 = abs(r5) / vol
    z30 = abs(r30) / vol
    ignore_1s = bool(market.get("ignore_1s"))
    z = max(z5, z30) if ignore_1s else max(z1, z5, z30)

    # acceleration: |r1| growing vs |r5|/5 — skip on 1s-sparse feeds
    if not ignore_1s:
        accel = abs(r1) - abs(r5) / 5.0 if r5 else 0.0
        if accel > vol * 2:
            score += 15
            reasons.append("acceleration")

    if z >= Z_UNWIND:
        score += 50
        reasons.append("vol_unwind")
    elif z >= Z_PAUSE:
        score += 35
        reasons.append("vol_pause")
    elif z >= Z_CAUTION:
        score += 20
        reasons.append("vol_caution")

    adx = float(market.get("adx") or 0)
    adx_cap = float(market.get("adx_range_max") or 30)
    if adx >= adx_cap:
        score += 15
        reasons.append("adx_trend")

    qty = abs(float(grid.get("position_qty") or 0))
    exposure = min(max(float(grid.get("exposure_frac") or 0), 0.0), 1.0)
    if qty > 0 and exposure >= 0.66:
        score += 15
        reasons.append("high_exposure")
    elif qty > 0:
        score += 5

    # Same market signal is harsher when already loaded.
    score = min(100.0, score * (1.0 + 0.4 * exposure))

    if score >= 80:
        state = "UNWIND"
        action = "FLATTEN_SYMBOL"
    elif score >= 65:
        state = "PAUSE"
        action = "NO_NEW_ENTRIES"
    elif score >= 50:
        state = "REDUCE"
        action = "NO_SCALE_IN"
    elif score >= 30:
        state = "CAUTION"
        action = "NO_SCALE_IN"
    else:
        state = "SAFE"
        action = "NORMAL"

    direction = "NONE"
    if r5 < -vol:
        direction = "DOWN"
    elif r5 > vol:
        direction = "UP"

    return {
        "state": state,
        "risk_score": round(score, 1),
        "reason_codes": reasons or ["ok"],
        "recommended_action": action,
        "direction": direction,
        "confidence": min(0.95, 0.4 + score / 150.0),
        "z": round(z, 2),
        "ts": time.time(),
    }
