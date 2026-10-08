#!/usr/bin/env python3
"""backtest — offline grid replay over historical bars.

Uses ONLY NeutralGridCore math + gate/session logic. NO LLM anywhere on this
path (rules bias only). JEV is used ONLY for the 1-week horizon (user-confirmed);
2+ weeks run pure math + gates.

Fills assume touch (no queue/slippage model) — live results typically land
5-15% below backtest. State for JEV calls uses bars <= the replayed timestamp
only (no lookahead).
"""
import logging
import math
import time

logger = logging.getLogger("gb.backtest")

HORIZONS = {"1W": 7, "2W": 14, "3W": 21, "4W": 28, "1M": 30,
            "2M": 60, "3M": 90, "5M": 150, "6M": 180}
JEV_EVERY_N = 12  # JEV mode read every 12th bar (~3h on M15) in 1W mode


def _bars_binance(symbol: str, days: int, user_id=None, env: str = "demo",
                  end_days_ago: int = 0):
    """Paginated 15m klines (1000/call). ALWAYS mainnet public data — klines
    need no keys, and testnet history is thin/stale (would mislead the replay).
    Returns oldest-first [{...}] with ms time."""
    from binance.client import Client
    c = Client("", "", testnet=False)  # public only; no keys needed/touched
    need = min(int(days * 96) + 10, 20000)
    off = max(int(end_days_ago or 0), 0)
    if off:
        need += int(off * 96)
    out, end = [], None
    _empty_hits = 0
    while len(out) < need:
        kw = {"symbol": symbol, "interval": "15m", "limit": 1000}
        if end is not None:
            kw["endTime"] = end
        try:
            kl = c.futures_klines(**kw)
        except Exception:
            kl = []
            time.sleep(5)
        if not kl:
            # empty page = rate limit or end of history; retry a few times,
            # then accept partial (caller validates minimums)
            _empty_hits += 1
            if _empty_hits >= 3:
                break
            time.sleep(5)
            continue
        _empty_hits = 0
        chunk = [{"time": k[0] // 1000, "high": float(k[2]), "low": float(k[3]),
                  "close": float(k[4]), "quote_volume": float(k[7]),
                  "volume": float(k[5])} for k in kl]
        out = chunk + out
        end = kl[0][0] - 1
        if len(kl) < 1000:
            break
        time.sleep(0.2)
    if off:
        cut = int(off * 96)
        return out[-(need):-cut or None] if cut < len(out) else []
    return out[-need:]


def _bars_mt5(symbol: str, days: int, token: str):
    """M15 bars: on-disk store first (accumulates across EA POSTs), live link
    tail second. Needs EA BarsToSend>=~700 for a 1W window otherwise."""
    import json as _js
    import pathlib as _pl
    need = min(int(days * 96) + 10, 3000)
    seen, out = {}, []

    def _push(b):
        t = int(b.get("t", 0) or 0)
        if t > 0 and t not in seen:
            seen[t] = True
            out.append({"time": t, "high": float(b.get("h", 0) or 0),
                        "low": float(b.get("l", 0) or 0),
                        "close": float(b.get("c", 0) or 0),
                        "quote_volume": float(b.get("v", 0) or 0),
                        "volume": float(b.get("v", 0) or 0)})

    try:
        fp = _pl.Path(__file__).parent / "data" / f"mt5_bars_{symbol.upper()}.jsonl"
        if fp.exists():
            for line in fp.read_text().splitlines()[-4000:]:
                try:
                    _push(_js.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    try:
        from services import mt5_link
        for sym, s in ((link.get("per_symbol") or {}).items()
                       for link in [mt5_link.get_link(token) or {}]):
            if sym.upper() == symbol.upper():
                for b in (s.get("bars_m15", []) or []):
                    _push({"t": b.get("t"), "h": b.get("h"), "l": b.get("l"),
                           "c": b.get("c"), "v": b.get("v", 0)})
    except Exception:
        pass
    out.sort(key=lambda x: x["time"])
    if len(out) < 60:
        raise RuntimeError(f"only {len(out)} MT5 bars — raise EA BarsToSend "
                           f"(need ~{need} for {days}d) and let history accumulate")
    return out[-need:]


def run_backtest(symbol: str, horizon: str = "1W", platform: str = "binance",
                 user_id=None, env: str = "demo", token: str = None,
                 per_level_usd: float = 100.0, leverage: int = 10,
                 use_jev: bool = None, mode: str = "scalp",
                 ov_levels: int = None, ov_spacing_min: float = None,
                 ov_spacing_mult: float = None, ov_tp_mult: float = None,
                 ov_equity_stop: float = None, compound: bool = True,
                 compound_frac: float = 0.02, start_equity: float = 10000.0,
                 jev_force: bool = False, range_stand_down: bool = True,
                 sizing_mode: str = "fixed", prop_daily_pct: float = 0.0,
                 prop_max_pct: float = 0.0, prop_min_day_pct: float = 0.005,
                 prop_target_pct: float = 0.0, end_days_ago: int = 0,
                 weekly_flat: bool = False, session_rules: bool = False,
                 sess_bank_pct: float = 0.02, sess_loss_pct: float = 0.02,
                 asia_frac: float = 0.5, tp_arm_sec: int = 0,
                 float_cap: float = 0.0, trail_pct: float = 0.0,
                 cost_bps: float = 0.0, curve_every: int = 24) -> dict:
    """Replay a neutral grid. use_jev defaults True ONLY for 1W."""
    from neutral_grid import NeutralGridCore, GridTuning, atr_pct, adx, quote_volume
    t0 = time.time()
    days = HORIZONS.get((horizon or "1W").upper(), 7)
    if platform == "mt5":
        if not token:
            raise RuntimeError("MT5 token required (attach EA first)")
        bars = _bars_mt5(symbol, days, token)
    else:
        bars = _bars_binance(symbol, days, user_id, env,
                             end_days_ago=end_days_ago)
    if len(bars) < 60:
        raise RuntimeError(f"only {len(bars)} bars — need >= 60 (raise EA BarsToSend?)")
    _need_min = int(days * 96 * 0.8)
    if len(bars) < _need_min:
        raise RuntimeError(f"only {len(bars)} bars for {days}d window (need ~{int(days*96)}) — data shortfall, refusing short run")
    jev_on = (days <= 7) if use_jev is None else bool(use_jev)
    if use_jev and days > 7 and not jev_force:
        raise RuntimeError("JEV is 1-week-only by policy (pure math beyond)")

    _m = (mode or "scalp").strip().lower()
    from neutral_grid import GridTuning as _GT
    _p = dict(_GT.MODES.get(_m, _GT.MODES["scalp"]))
    if ov_levels is not None:
        _p["grid_levels"] = ov_levels
    if ov_spacing_min is not None:
        _p["spacing_min"] = ov_spacing_min
    if ov_spacing_mult is not None:
        _p["spacing_atr_mult"] = ov_spacing_mult
    if ov_tp_mult is not None:
        _p["tp_mult"] = ov_tp_mult
    if ov_equity_stop is not None:
        _p["equity_stop_pct"] = ov_equity_stop
    core = NeutralGridCore(GridTuning(grid_levels=_p["grid_levels"],
                                      spacing_atr_mult=_p["spacing_atr_mult"],
                                      spacing_min=_p["spacing_min"],
                                      spacing_max=_p["spacing_max"],
                                      equity_stop_pct=_p["equity_stop_pct"],
                                      tp_mult=_p["tp_mult"],
                                      min_part_vol=0.0, mode=_m))
    _is_auto = (_m == "auto")
    _cores = {}
    if _is_auto:
        for _mk, _mp in (("scalp", _GT.MODES["scalp"]), ("swing", _GT.MODES["swing"])):
            _cores[_mk] = NeutralGridCore(GridTuning(
                grid_levels=ov_levels if ov_levels is not None else _mp["grid_levels"],
                spacing_atr_mult=ov_spacing_mult if ov_spacing_mult is not None else _mp["spacing_atr_mult"],
                spacing_min=ov_spacing_min if ov_spacing_min is not None else _mp["spacing_min"],
                spacing_max=_mp["spacing_max"],
                equity_stop_pct=ov_equity_stop if ov_equity_stop is not None else _mp["equity_stop_pct"],
                tp_mult=ov_tp_mult if ov_tp_mult is not None else _mp["tp_mult"],
                min_part_vol=0.0, mode=_mk))
    _cfr = max(0.001, min(0.25, float(compound_frac or 0.02)))
    params = {"mode": _m, **{k: _p[k] for k in
              ("grid_levels", "spacing_atr_mult", "spacing_min", "spacing_max",
               "equity_stop_pct", "tp_mult")},
              "per_level_usd": per_level_usd, "start_equity": start_equity,
              "compound_frac": _cfr if compound else 0.0}
    hist = []
    mode, mode_p = "NEUTRAL", 0.5
    center = bars[30]["close"]
    core.center = center
    try:
        for _cc in _cores.values():
            _cc.center = center
    except Exception:
        pass
    active = 1
    # resting rungs + open legs (slot model — see per-bar block below)
    resting, legs = {}, {}
    pos_qty, pos_cost = 0.0, 0.0
    realized, peak_eq, max_dd = 0.0, 0.0, 0.0
    liquidated = False
    _leg0 = None
    # prop 2-step pack: UTC-day tracking, daily breach counts, max breach ends run
    from datetime import datetime as _dtm, timezone as _tz
    _day = None
    _day_start = 0.0
    _day_breached = False
    _day_banked = False
    _banked_days = 0
    _breach_days, _prof_days = 0, []
    _breached_max = False
    _weeks = []
    _week_start = None
    _week_trades0 = 0
    _week_done = False
    _sessions = []
    _sess_key, _sess_start = None, 0.0
    _sess_over = False
    _asia_mult = 1.0
    try:
        _asia_frac = max(0.0, min(1.0, float(asia_frac if asia_frac is not None else 0.5)))
    except (TypeError, ValueError):
        _asia_frac = 0.5
    _act = {"scalp": 1, "swing": 1}
    _auto_mode_now = "scalp"
    _fill_pnls, _eval_at, _tier, _war, _mult, _last_real = [], 0, 1, False, 1.0, 0.0
    _holds = []
    _trail_peak, _trail_floor_hit = 0.0, False
    _inst_breach = None
    # MEASUREMENT ONLY (no strategy change): realistic per-fill cost model
    # (commission + spread + slippage) + gross/trade-population stats.
    _fee_r = (cost_bps or 0.0) / 10000.0
    _fees, _gw, _gl = 0.0, 0.0, 0.0
    _tpnls = []
    fills, wins, losses, jev_calls = 0, 0, 0, 0
    equity_curve = []
    hist_mids = []
    upnl = 0.0
    # compound starts where fixed legs would: first leg == per_level_usd,
    # then legs track running equity from there.
    _start_eq = per_level_usd / _cfr if compound else start_equity

    for i in range(30, len(bars)):
        b = bars[i]
        window = bars[max(0, i - 60):i + 1]
        wb = [{"high": x["high"], "low": x["low"], "close": x["close"],
               "quote_volume": x.get("quote_volume", 0)} for x in window]
        a = atr_pct(wb, 14)
        v = adx(wb, 14)
        if a > 0:
            core.compute_spacing(a)
        if _is_auto:
            # AUTO: regime routes geometry per bar (scalp<25 else swing).
            # Position/inventory shared; each core keeps own levels + regime.
            _auto_mode_now = "scalp" if v < 25.0 else "swing"
            core = _cores[_auto_mode_now]
            if a > 0:
                core.compute_spacing(a)
        active = _act[_auto_mode_now] if _is_auto else active
        ranging = core.regime_ok(v)
        mid = b["close"]
        # compound leg: % of running equity (start + realized + open), else fixed.
        # Equity-stop anchor stays at the OPENING scale (like production alloc):
        # a shrinking _leg must not shrink the stop into uselessness.
        _leg = max(5.0, (_start_eq + realized + upnl) * _cfr) if compound \
            else per_level_usd
        _leg = max(5.0, _leg * _asia_mult)
        if _leg0 is None:
            _leg0 = _leg
        # conviction sizing: v1 tiers (0.5/1/2x on trailing-20 realized),
        # v2 scout/warrior (0.5x scout; +1.5x warrior when trailing hot).
        _sm = (sizing_mode or "fixed").strip().lower()
        if _sm in ("tiered", "scout"):
            _trail = _fill_pnls[-20:] if len(_fill_pnls) >= 20 else []
            if len(_trail) >= 20 and _eval_at <= i - 20:
                _eval_at = i
                _t = sum(_trail)
                if _sm == "tiered":
                    _tier = min(2, _tier + 1) if _t > 0 else max(0, _tier - 1)
                    _mult = (0.5, 1.0, 2.0)[_tier]
                else:
                    _war = True if _t > 2 * _leg0 else (False if _t < -_leg0 else _war)
                    _mult = 0.5 + (1.5 if _war else 0.0)
            _leg = max(5.0, _leg * _mult)
        hist_mids.append(mid)
        if len(hist_mids) > 400:
            hist_mids = hist_mids[-400:]

        # JEV mode read (1W only, throttled; state strictly <= now)
        if jev_on and i % JEV_EVERY_N == 0:
            try:
                import openrouter_client as _jg
                n = len(hist_mids)
                r20 = ((hist_mids[-1] - hist_mids[-1 - 20]) / hist_mids[-1 - 20] * 10000) if n > 20 else 0.0
                d = _jg.jev_decide({"symbol": symbol, "mid": mid, "spreadBps": 1.0,
                                    "imbalance": 0.0, "taker_buy": 0, "taker_sell": 0,
                                    "cvd": 0.0, "ret20": r20, "recentDecisions": []},
                                   lite=True)
                jev_calls += 1
                p = d["buy"]
                mode = "NEUTRAL" if 0.35 <= p <= 0.65 else ("SHORT" if p < 0.35 else "LONG")
                mode_p = p
            except Exception:
                pass

        # grid geometry for active levels/mode (slot model: a rung fills ONCE;
        # its TP exit must fill before the rung re-arms — mirrors production).
        # SESSION RULES: rollover flattens + restarts; bank/loss sit out the rest
        # of the session; Asia rests outright. Sessions: ASIA 00-08, LON 08-16,
        # NY 16-24 UTC. Bank/loss thresholds are % of session-open equity.
        _sess_easy_n = 0.0
        _h = _dtm.fromtimestamp(b["time"], tz=_tz.utc).hour
        _sess = "ASIA" if _h < 8 else ("LONDON" if _h < 16 else "NY")
        _skey = _dtm.fromtimestamp(b["time"], tz=_tz.utc).strftime("%Y-%m-%d-") + _sess
        _eq_now = realized + (pos_qty * (mid - pos_cost / pos_qty) if abs(pos_qty) > 1e-9 else 0.0)
        if session_rules and _sess_key is not None and _skey != _sess_key:
            _sessions.append({"session": _sess_key,
                              "pnl": round(_eq_now - _sess_start, 2)})
            if abs(pos_qty) > 1e-9:
                entry = pos_cost / pos_qty
                pnl = pos_qty * (mid - entry)
                realized += pnl
                fills += 1
                wins, losses = wins + (pnl > 0), losses + (pnl <= 0)
                _tpnls.append(pnl)
                if pnl > 0:
                    _gw += pnl
                else:
                    _gl -= pnl
                _f = abs(pos_qty) * mid * _fee_r
                realized -= _f
                _fees += _f
                pos_qty, pos_cost = 0.0, 0.0
            resting, legs, active = {}, {}, 1
            center = mid
            core.center = center
            if "_cores" in dir() and _cores:
                try:
                    for _cc in _cores.values():
                        _cc.center = center
                except Exception:
                    pass
            try:
                _act.update({"scalp": 1, "swing": 1})
            except Exception:
                pass
        _sess_over = False
        if session_rules:
            if _sess_key is None or _skey != _sess_key:
                _sess_key = _skey
                _sess_start = _eq_now
                _sess_over = False
            else:
                _base_s = ((per_level_usd / max(0.001, min(0.25, float(compound_frac or 0.02)))) if compound else start_equity) or 5000.0
                _sp = (_eq_now - _sess_start) / _base_s
                if _sp >= sess_bank_pct:
                    _sessions.append({"session": _skey + "-BANKED",
                                      "pnl": round(_eq_now - _sess_start, 2)})
                    _sess_over = True
                    if abs(pos_qty) > 1e-9:
                        entry = pos_cost / pos_qty
                        pnl = pos_qty * (mid - entry)
                        realized += pnl
                        fills += 1
                        wins, losses = wins + (pnl > 0), losses + (pnl <= 0)
                        _tpnls.append(pnl)
                        if pnl > 0:
                            _gw += pnl
                        else:
                            _gl -= pnl
                        _f = abs(pos_qty) * mid * _fee_r
                        realized -= _f
                        _fees += _f
                        pos_qty, pos_cost = 0.0, 0.0
                    resting, legs = {}, {}
                elif _sp <= -sess_loss_pct:
                    _sessions.append({"session": _skey + "-CUT",
                                      "pnl": round(_eq_now - _sess_start, 2)})
                    _sess_over = True
                    if abs(pos_qty) > 1e-9:
                        entry = pos_cost / pos_qty
                        pnl = pos_qty * (mid - entry)
                        realized += pnl
                        fills += 1
                        wins, losses = wins + (pnl > 0), losses + (pnl <= 0)
                        _tpnls.append(pnl)
                        if pnl > 0:
                            _gw += pnl
                        else:
                            _gl -= pnl
                        _f = abs(pos_qty) * mid * _fee_r
                        realized -= _f
                        _fees += _f
                        pos_qty, pos_cost = 0.0, 0.0
                    resting, legs = {}, {}
            _asia_mult = _asia_frac if _sess == "ASIA" else 1.0
        # Range stand-down (pure math, $0 LLM): trending tape ⇒ flatten + sit out.
        # Directional modes are EXEMPT (mirrors production bias exemption):
        # a SHORT lean in a rundown IS the trade — stand-down only binds NEUTRAL.
        _directional = mode in ("LONG", "SHORT")
        stood_down = bool(range_stand_down) and not ranging and not _directional
        if stood_down and (abs(pos_qty) > 1e-9 or resting or legs):
            if abs(pos_qty) > 1e-9:
                entry = pos_cost / pos_qty
                pnl = pos_qty * (mid - entry)
                realized += pnl
                fills += 1
                wins, losses = wins + (pnl > 0), losses + (pnl <= 0)
                _tpnls.append(pnl)
                if pnl > 0:
                    _gw += pnl
                else:
                    _gl -= pnl
                _f = abs(pos_qty) * mid * _fee_r
                realized -= _f
                _fees += _f
            pos_qty, pos_cost, active = 0.0, 0.0, 1
            resting, legs = {}, {}
            try:
                _act.update({"scalp": 1, "swing": 1})
            except Exception:
                pass
        sides = ("BUY", "SELL") if mode == "NEUTRAL" else (("BUY",) if mode == "LONG" else ("SELL",))
        want = set() if (stood_down or _day_banked or _day_breached or _sess_over or _asia_mult <= 0) else {(s, l) for s in sides for l in range(1, active + 1)}
        for key in [k for k in resting if k not in want]:
            del resting[key]
        for s, l in sorted(want):
            if (s, l) not in resting and (s, l) not in legs:
                px = center * (1 - core.spacing_pct * l) if s == "BUY" \
                    else center * (1 + core.spacing_pct * l)
                resting[(s, l)] = px
        # 1) TP exits first (they free their rungs). TP-arm delay: exits rest
        # only once the leg is older than tp_arm_sec (Instant min-hold rule).
        for key in list(legs.keys()):
            side, lvl = key
            ex = "SELL" if side == "BUY" else "BUY"
            _li = legs[key]
            tp = _li["tp"] if isinstance(_li, dict) else _li
            if tp_arm_sec > 0 and isinstance(_li, dict) and \
                    b["time"] - _li.get("t", 0) < tp_arm_sec:
                continue
            hit = (ex == "SELL" and b["high"] >= tp) or (ex == "BUY" and b["low"] <= tp)
            if not hit:
                continue
            signed = (_leg / tp) if ex == "BUY" else -(_leg / tp)
            closing = min(abs(signed), abs(pos_qty)) * (1 if signed > 0 else -1) if abs(pos_qty) > 1e-9 else 0.0
            if closing != 0:
                entry = pos_cost / pos_qty
                pnl = -closing * (tp - entry)
                realized += pnl
                fills += 1
                wins, losses = wins + (pnl > 0), losses + (pnl <= 0)
                _tpnls.append(pnl)
                if pnl > 0:
                    _gw += pnl
                else:
                    _gl -= pnl
                _f = abs(signed) * tp * _fee_r
                realized -= _f
                _fees += _f
                pos_cost += closing * entry
                pos_cost += (signed - closing) * tp
            pos_qty += signed
            try:
                _holds.append(b["time"] - _li.get("t", b["time"])
                              if isinstance(_li, dict) else 900)
            except Exception:
                pass
            del legs[key]
        # 2) grid fills consume their rung + arm TP
        for key in list(resting.keys()):
            side, lvl = key
            px = resting[key]
            hit = (side == "BUY" and b["low"] <= px) or (side == "SELL" and b["high"] >= px)
            if not hit:
                continue
            signed = _leg / px if side == "BUY" else -_leg / px
            if pos_qty == 0 or (pos_qty > 0) == (signed > 0):
                pos_cost += signed * px
            else:
                closing = min(abs(signed), abs(pos_qty)) * (1 if signed > 0 else -1)
                entry = pos_cost / pos_qty
                pnl = -closing * (px - entry)
                realized += pnl
                fills += 1
                wins, losses = wins + (pnl > 0), losses + (pnl <= 0)
                _tpnls.append(pnl)
                if pnl > 0:
                    _gw += pnl
                else:
                    _gl -= pnl
                _f = _leg * _fee_r
                realized -= _f
                _fees += _f
                pos_cost += closing * entry
                pos_cost += (signed - closing) * px
            pos_qty += signed
            del resting[key]
            legs[key] = {"tp": core.tp_price(side, px), "t": b["time"]}
            if active < core.grid_levels and ranging:
                active += 1
                if _is_auto:
                    _act[_auto_mode_now] = active
        # equity stop
        upnl = pos_qty * (mid - pos_cost / pos_qty) if abs(pos_qty) > 1e-9 else 0.0
        eq = realized + upnl
        peak_eq = max(peak_eq, eq)
        max_dd = max(max_dd, peak_eq - eq)
        _fill_pnls.append(realized - _last_real)
        _last_real = realized
        if len(_fill_pnls) > 60:
            _fill_pnls = _fill_pnls[-60:]
        if core.should_equity_stop(eq, max(_leg, _leg0 or _leg) * active):
            realized = eq
            pos_qty, pos_cost, active = 0.0, 0.0, 1
            resting, legs = {}, {}
            try:
                _act.update({"scalp": 1, "swing": 1})
            except Exception:
                pass
        # LIQUIDATION (cross-margin honesty): book equity <= 0 → exchange flattens
        # everything and the run ENDS. Without this, underwater inventory rides to
        # fictional -$29k; live you'd be wiped at ~-100%. Reported, not hidden.
        _eq0 = (_start_eq if compound else start_equity)
        if _eq0 > 0 and (_eq0 + eq) <= 0:
            realized = -_eq0
            upnl = 0.0
            pos_qty, pos_cost = 0.0, 0.0
            resting, legs = {}, {}
            liquidated = True
            equity_curve.append({"t": b["time"], "eq": round(realized, 2)})
            break
        # PROP 2-step pack: UTC-day breach counting + max-loss hard stop.
        # Breach flattens and counts the day (run continues flat-lined for
        # recovery shape); max breach ends the run. Profitable days (>=0.5%)
        # counted at rollover. All $0-cost math, no LLM.
        if prop_max_pct > 0 or prop_daily_pct > 0:
            _base = _eq0 if _eq0 > 0 else start_equity
            _d = _dtm.fromtimestamp(b["time"], tz=_tz.utc).date().isoformat()
            if _day is None:
                _day, _day_start = _d, eq
            if _d != _day:
                if eq - _day_start >= prop_min_day_pct * _base:
                    _prof_days.append(_day)
                _day, _day_start, _day_breached = _d, eq, False
                _day_banked = False
            if prop_max_pct > 0 and eq <= -prop_max_pct * _base:
                _breached_max = True
                realized = eq
                pos_qty, pos_cost = 0.0, 0.0
                resting, legs = {}, {}
                equity_curve.append({"t": b["time"], "eq": round(eq, 2)})
                break
            # INSTANT pack: floating cap ($ upnl floor) + trailing floor (ratchet
            # on peak eq). Either breach ends the run — account dead. $0 = off.
            _trail_peak = max(_trail_peak, eq)
            _inst_breach = None
            if float_cap > 0 and upnl <= -float_cap:
                _inst_breach = "float-cap"
            elif trail_pct > 0 and _trail_peak > 0 and eq <= _trail_peak * (1 - trail_pct):
                _inst_breach = "trail"
            if _inst_breach:
                _breached_max = True
                realized = eq
                pos_qty, pos_cost = 0.0, 0.0
                resting, legs = {}, {}
                equity_curve.append({"t": b["time"], "eq": round(eq, 2)})
                break
            # Weekly flat (Friday 21:00 UTC): close all, ledger the week, restart
            # Monday flat. Bounds bleed accumulation to ~7 days by construction.
            _wdt = _dtm.fromtimestamp(b["time"], tz=_tz.utc)
            if _week_start is None:
                _week_start, _week_trades0 = eq, fills
            if _wdt.weekday() == 0 and _week_done:
                _week_done = False
            if weekly_flat and _wdt.weekday() == 4 and _wdt.hour >= 21 and not _week_done:
                _week_done = True
                _wpnl = eq - _week_start
                _weeks.append({"week": _wdt.strftime("%Y-%m-%d"),
                               "pnl": round(_wpnl, 2),
                               "trades": fills - _week_trades0})
                _week_start, _week_trades0 = eq, fills
                if abs(pos_qty) > 1e-9:
                    entry = pos_cost / pos_qty
                    pnl = pos_qty * (mid - entry)
                    realized += pnl
                    fills += 1
                    wins, losses = wins + (pnl > 0), losses + (pnl <= 0)
                    _tpnls.append(pnl)
                    if pnl > 0:
                        _gw += pnl
                    else:
                        _gl -= pnl
                    _f = abs(pos_qty) * mid * _fee_r
                    realized -= _f
                    _fees += _f
                    pos_qty, pos_cost = 0.0, 0.0
                    resting, legs = {}, {}
            if prop_daily_pct > 0 and not _day_breached and (eq - _day_start) <= -prop_daily_pct * _base:
                _day_breached = True
                _breach_days += 1
                if abs(pos_qty) > 1e-9:
                    entry = pos_cost / pos_qty
                    pnl = pos_qty * (mid - entry)
                    realized += pnl
                    fills += 1
                    wins, losses = wins + (pnl > 0), losses + (pnl <= 0)
                    _tpnls.append(pnl)
                    if pnl > 0:
                        _gw += pnl
                    else:
                        _gl -= pnl
                    _f = abs(pos_qty) * mid * _fee_r
                    realized -= _f
                    _fees += _f
                    pos_qty, pos_cost = 0.0, 0.0
                    resting, legs = {}, {}
            # PROP profit target: bank the day at +target (flatten, sit out rest
            # of UTC day). Locks a qualifying day instead of gambling it back.
            if prop_target_pct > 0 and not _day_banked and (eq - _day_start) >= prop_target_pct * _base:
                _day_banked = True
                _banked_days += 1
                if _day not in _prof_days:
                    _prof_days.append(_day)
                if abs(pos_qty) > 1e-9:
                    entry = pos_cost / pos_qty
                    pnl = pos_qty * (mid - entry)
                    realized += pnl
                    fills += 1
                    wins, losses = wins + (pnl > 0), losses + (pnl <= 0)
                    _tpnls.append(pnl)
                    if pnl > 0:
                        _gw += pnl
                    else:
                        _gl -= pnl
                    _f = abs(pos_qty) * mid * _fee_r
                    realized -= _f
                    _fees += _f
                    pos_qty, pos_cost = 0.0, 0.0
                    resting, legs = {}, {}
        if i % curve_every == 0:
            equity_curve.append({"t": b["time"], "eq": round(eq, 2),
                                 "upnl": round(upnl, 2), "nlegs": len(legs)})
    # mark to market at last close
    upnl = pos_qty * (bars[-1]["close"] - pos_cost / pos_qty) if abs(pos_qty) > 1e-9 else 0.0
    total = realized + upnl
    if weekly_flat and _week_start is not None:
        _weeks.append({"week": "partial-final",
                       "pnl": round(total - _week_start, 2),
                       "trades": fills - _week_trades0})
    _mls, _run = 0, 0
    for _p in _tpnls:
        _run = _run + 1 if _p <= 0 else 0
        _mls = max(_mls, _run)
    out = {"symbol": symbol, "horizon": horizon, "platform": platform,
            "bars": len(bars), "jev_calls": jev_calls, "jev_on": jev_on,
            "trades": fills, "wins": wins, "losses": losses,
            "win_rate": round(wins / fills, 3) if fills else 0.0,
            "compound": bool(compound), "compound_frac": _cfr,
            "fees": round(_fees, 2), "cost_bps": cost_bps,
            "profit_factor": round(_gw / _gl, 3) if _gl > 0 else None,
            "expectancy": round(sum(_tpnls) / len(_tpnls), 4) if _tpnls else 0.0,
            "avg_trade": round(sum(_tpnls) / len(_tpnls), 4) if _tpnls else 0.0,
            "max_loss_streak": _mls,
            "liquidated": liquidated,
            "breached_max": _breached_max,
            "breach_reason": _inst_breach,
            "hold_p150": round(sum(1 for h in _holds if h >= 150) / max(len(_holds), 1), 3),
            "hold_n": len(_holds),
            "sessions": _sessions,
            "weeks": _weeks,
            "breach_days": _breach_days,
            "banked_days": _banked_days,
            "profitable_days": len(_prof_days),
            "realized": round(realized, 2), "open_upnl": round(upnl, 2),
            "total": round(total, 2), "max_dd": round(max_dd, 2),
            "elapsed_s": round(time.time() - t0, 1),
            "params": params,
            "note": "fills assume touch; live lands ~5-15% lower",
            "equity_curve": equity_curve}
    try:
        save_edge(out)
    except Exception as e:
        logger.debug(f"edge-save failed: {e}")
    try:
        log_to_md(out)
    except Exception as e:
        logger.debug(f"ledger failed: {e}")
    return out


def save_edge(result: dict) -> dict:
    """Merge one backtest result into GBV4/data/edge.json (per symbol/mode).
    Returns the stored entry. Never raises."""
    import json as _js
    import pathlib as _pl
    from datetime import datetime, timezone
    try:
        fp = _pl.Path(__file__).parent / "data" / "edge.json"
        db = _js.loads(fp.read_text()) if fp.exists() else {}
    except Exception:
        db = {}
    try:
        mode = (result.get("params") or {}).get("mode", "scalp")
        key = f"{result.get('symbol')}/{mode}/{result.get('horizon')}"
        db[key] = {"win_rate": result.get("win_rate", 0),
                   "net": result.get("total", 0),
                   "max_dd": result.get("max_dd", 0),
                   "trades": result.get("trades", 0),
                   "liquidated": bool(result.get("liquidated", False)),
                   "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                   "bars": result.get("bars", 0)}
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(_js.dumps(db, indent=1))
        return db[key]
    except Exception as e:
        logger.debug(f"edge-save write failed: {e}")
        return {}


def run_signal_backtest(symbol: str, horizon: str = "1W", platform: str = "binance",
                        adx_gate: bool = False, fee_bps: float = 5.0,
                        every_n: int = 15) -> dict:
    """Pure JEV signal test, NO grid: position = sign of JEV direction on M1
    tape (signal every 15th bar), optional ADX>=20 gate. Flips pay spread cost.
    Caveat: historical bars carry no book/CVD — JEV reads ret/range/ATR/ADX
    only, weaker than live microstructure. Never raises (returns error dict)."""
    from neutral_grid import adx as _adx
    t0 = time.time()
    try:
        days = HORIZONS.get((horizon or "1W").upper(), 7)
        if platform == "mt5":
            raise RuntimeError("signal backtest is Binance-klines only for now")
        from binance.client import Client as _BC
        c = _BC("", "", testnet=False)
        need = min(int(days * 1440) + 10, 20000)
        out, end = [], None
        while len(out) < need:
            kw = {"symbol": symbol, "interval": "1m", "limit": 1000}
            if end is not None:
                kw["endTime"] = end
            kl = c.futures_klines(**kw)
            if not kl:
                break
            out = [{"time": k[0] // 1000, "high": float(k[2]), "low": float(k[3]),
                    "close": float(k[4])} for k in kl] + out
            end = kl[0][0] - 1
            if len(kl) < 1000:
                break
            time.sleep(0.2)
        bars = out[-need:]
        if len(bars) < 200:
            return {"ok": False, "error": f"only {len(bars)} M1 bars"}
        import openrouter_client as _jg
        pos, entry, realized, peak, max_dd = 0, 0.0, 0.0, 0.0, 0.0
        flips, wins, losses, jev_calls = 0, 0, 0, 0
        p = 0.5
        closes = [b["close"] for b in bars]
        step = max(1, int(every_n or 15))
        for i in range(60, len(bars)):
            px = bars[i]["close"]
            if i % step == 0:
                win = bars[max(0, i - 60):i + 1]
                wb = [{"high": x["high"], "low": x["low"], "close": x["close"],
                       "quote_volume": 0} for x in win]
                r20 = ((closes[i] - closes[i - 20]) / closes[i - 20] * 10000) if closes[i - 20] else 0.0
                hi = max(x["high"] for x in win[-20:])
                lo = min(x["low"] for x in win[-20:])
                rg = (px - lo) / (hi - lo) if hi > lo else 0.5
                d = _jg.jev_decide({"symbol": symbol, "mid": px, "spreadBps": 1.0,
                                    "imbalance": 0.0, "taker_buy": 0, "taker_sell": 0,
                                    "cvd": 0.0, "ret20": round(r20, 2),
                                    "range_pos": round(rg, 2),
                                    "recentDecisions": []}, lite=True)
                jev_calls += 1
                p = d["buy"]
            want = 0
            if p >= 0.65:
                want = 1
            elif p <= 0.35:
                want = -1
            if adx_gate and want != 0:
                win = bars[max(0, i - 60):i + 1]
                wb = [{"high": x["high"], "low": x["low"], "close": x["close"],
                       "quote_volume": 0} for x in win]
                if _adx(wb, 14) < 20.0:
                    want = 0
            if want != pos:
                if pos != 0:
                    pnl = pos * (px - entry) - px * fee_bps / 10000
                    realized += pnl
                    flips += 1
                    wins, losses = wins + (pnl > 0), losses + (pnl <= 0)
                pos, entry = want, px
            upnl = pos * (px - entry)
            eq = realized + upnl
            peak = max(peak, eq)
            max_dd = max(max_dd, peak - eq)
        if pos != 0:
            pnl = pos * (bars[-1]["close"] - entry)
            realized += pnl
            flips += 1
            wins, losses = wins + (pnl > 0), losses + (pnl <= 0)
        return {"ok": True, "symbol": symbol, "horizon": horizon,
                "adx_gate": adx_gate, "bars": len(bars), "jev_calls": jev_calls,
                "flips": flips, "wins": wins, "losses": losses,
                "win_rate": round(wins / flips, 3) if flips else 0.0,
                "total_pct": round(realized / bars[60]["close"] * 100, 2),
                "max_dd_pct": round(max_dd / bars[60]["close"] * 100, 2),
                "elapsed_s": round(time.time() - t0, 1),
                "note": "unit-size % returns; no book/CVD in hindsight"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def log_to_md(result: dict) -> None:
    """Append one backtest row to GBV4/tools/BACKTESTS.md (common ledger).
    Creates the file with header on first use. Never raises."""
    import pathlib as _pl
    from datetime import datetime, timezone
    try:
        fp = _pl.Path(__file__).parent / "tools" / "BACKTESTS.md"
        if not fp.exists():
            fp.write_text(
                "# Backtest ledger — every run, one row\n\n"
                "Fill model: touch-fill, slot rungs + TP exits. No funding modeled. "
                "Live lands ~5-15% below printed net.\n\n"
                "| date | symbol | horizon | mode | legs | WR | net | maxDD | liq | "
                "bmax | bdays | banked | profdays | jev | note |\n"
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
        p = result.get("params", {}) or {}
        leg = p.get("per_level_usd", "?")
        if result.get("compound"):
            leg = f"{leg}*{result.get('compound_frac', '?')}"
        with open(fp, "a") as f:
            f.write("| %s | %s | %s | %s | %s | %.2f | %+.2f | %.2f | %s | %s | %s | %s | %s | %s | %s |\n" % (
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                result.get("symbol", "?"), result.get("horizon", "?"),
                p.get("mode", "?"), leg,
                result.get("win_rate", 0), result.get("total", 0),
                result.get("max_dd", 0),
                "YES" if result.get("liquidated") else "no",
                "YES" if result.get("breached_max") else str(result.get("breach_days", 0)),
                result.get("breach_days", 0), result.get("banked_days", 0),
                result.get("profitable_days", 0),
                result.get("jev_calls", 0),
                ("prop" if (result.get("breach_days", 0) or result.get("banked_days", 0) or result.get("breached_max")) else "plain")))
    except Exception:
        pass
