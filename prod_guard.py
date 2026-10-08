"""JEV-PORTFOLIO v1.0 — live production guard (backtest checks -> live).

Adapts the critical validated checks into the live path WITHOUT touching
strategy logic:
  1. Account DD governor (30/50/75% of $1,000) — gates spawns + new entries,
     emergency-flattens. Active when prop mode (GB_PROP_MODE=1) is checked.
  2. Portfolio floating-heat governor (-$300/-$400/-$500) + ETH+BTC heat
     monitor (-$250/-$350). Same activation.
  3. TP-arm / 150 s minimum hold — frozen rule, enforced on live exits
     (exit order withheld until the leg ages). GB_MIN_HOLD_SEC=0 disables.
  4. Peak-equity persistence across restarts (data/prod_guard.json).

Wire-in points in guru_ai.py: spawn_symbol (spawn gate), _neutral_tick
(entry gate + emergency), _desired_orders (entry/exit split), _mark_pair_pending
(leg timestamps). /api/prop/status serves the live verdict.
"""

from __future__ import annotations

import json
import os
import pathlib
import time

_PEAK_FILE = pathlib.Path(__file__).parent / "data" / "prod_guard.json"


def min_hold_sec() -> int:
    try:
        return max(0, int(os.getenv("GB_MIN_HOLD_SEC", "150") or 150))
    except (TypeError, ValueError):
        return 150


def _load_peaks() -> dict:
    try:
        return json.loads(_PEAK_FILE.read_text())
    except Exception:
        return {}


def _save_peaks(d: dict) -> None:
    try:
        _PEAK_FILE.parent.mkdir(exist_ok=True)
        _PEAK_FILE.write_text(json.dumps(d))
    except Exception:
        pass


class ProdGuard:
    """Process-singleton guard. All thresholds come from risk_engine +
    config/prop_maven_10k.yaml (PORTFOLIO SAFETY PARAMETERS)."""

    def __init__(self) -> None:
        from risk_engine import PortfolioGovernor, load_prop_config
        self.gov = PortfolioGovernor(profile=load_prop_config())
        self._peaks = _load_peaks()

    def _peak_key(self, env: str) -> str:
        return f"desk:{env or 'demo'}"

    def account_verdict(self, desk_pnl: float, floating: float,
                        legs: dict, env: str = "demo",
                        per_symbol_float: dict = None) -> dict:
        """desk_pnl = summed live bot P&L (realized+unrealized) vs $0 start.
        Equity is reconstructed as account_size + desk_pnl."""
        base = self.gov.profile.account_size
        equity = base + desk_pnl
        k = self._peak_key(env)
        peak = max(float(self._peaks.get(k, equity)), equity)
        self._peaks[k] = peak
        _save_peaks(self._peaks)
        psf = per_symbol_float or {}
        return self.gov.tick(
            equity, peak, floating,
            eth_float=float(psf.get("ETHUSDT", 0) or 0),
            btc_float=float(psf.get("BTCUSDT", 0) or 0),
            xau_float=float(psf.get("XAUUSDT", 0) or 0)) | {
            "equity": round(equity, 2), "floating": round(floating, 2),
            "peak": round(peak, 2)}

    # ── leg timestamps (TP-arm / min-hold) ──
    def leg_open(self, bot, level: int) -> None:
        try:
            ts = getattr(bot, "_guard_leg_ts", None)
            if ts is None:
                ts = bot._guard_leg_ts = {}
            ts[int(level)] = time.time()
        except Exception:
            pass

    def leg_close(self, bot, level: int) -> None:
        try:
            getattr(bot, "_guard_leg_ts", {}).pop(int(level), None)
        except Exception:
            pass

    def leg_age_ok(self, bot, level: int) -> bool:
        mh = min_hold_sec()
        if mh <= 0:
            return True
        try:
            ts = getattr(bot, "_guard_leg_ts", {}).get(int(level))
        except Exception:
            ts = None
        if not ts:
            return True  # legacy/unknown age: fail-open (logged by caller)
        return (time.time() - ts) >= mh


_GUARD: "ProdGuard | None" = None


def guard() -> ProdGuard:
    global _GUARD
    if _GUARD is None:
        _GUARD = ProdGuard()
    return _GUARD


_SNAP_CACHE: dict = {}


def cached_snapshot(env: str = "demo", max_age: float = 60.0) -> dict:
    """Desk snapshot cached to avoid per-tick REST storms (3 bots × tick)."""
    now = time.time()
    hit = _SNAP_CACHE.get(env)
    if hit and now - hit[0] < max_age:
        return hit[1]
    snap = desk_snapshot(env)
    _SNAP_CACHE[env] = (now, snap)
    return snap


def desk_snapshot(env: str = "demo") -> dict:
    """Summed live P&L across running bots (realized + unrealized)."""
    total = floating = 0.0
    legs: dict = {}
    per: dict = {}
    try:
        import guru_ai
        for m in list(getattr(guru_ai, "_bank_managers", []) or []):
            if m is None or getattr(m, "env", "demo") != env:
                continue
            for b in (m.active_bots() or []):
                try:
                    sd = b.status_dict()
                except Exception:
                    continue
                pnl = float(sd.get("total_pnl", 0) or 0)
                total += pnl
                sym = str(sd.get("symbol", ""))
                legs[sym] = int(sd.get("levels", 0) or 0)
                # floating approx: upnl embedded in levels_detail positions
                fl = 0.0
                for row in sd.get("levels_detail", []) or []:
                    if row.get("kind") == "pos":
                        try:
                            fl += float(row.get("pnl", 0) or 0)
                        except (TypeError, ValueError):
                            pass
                per[sym] = per.get(sym, 0.0) + fl
                floating += fl
    except Exception:
        pass
    return {"desk_pnl": round(total, 2), "floating": round(floating, 2),
            "legs": legs, "per_symbol_float": per}
