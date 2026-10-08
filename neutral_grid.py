"""
NeutralGridCore — the single source of truth for the two-sided neutral grid.

Shared by every entry point that runs a neutral grid:
  - GuruAIBot         (Binance micro-caps, guru_ai.py)
  - GridSubBot        (regular Binance spawn, orchestrator.py)
  - UpstoxFuturesGridBot (NIFTY/MCX futures, upstox_futures_grid.py)

Only the *strategy* lives here. Sizing (Binance notional floor vs Upstox lot
size) and execution (order placement, price/klines/position fetch) stay in each
platform adapter. Adapters feed raw market bars into the pure indicator
functions below and call the core's decision methods each loop tick.

Orderflow robustness (borrowed from the Robbins World Cup champion framework):
  A) Regime gate   — ADX(period); scale-in is paused while the market trends.
  B) ATR spacing   — level distance = ATR_pct x mult, clamped to [min, max].
  C) Scale-in      — start at 1 level/side, add a level every `scale_ticks`
                     only while regime + participation allow (confirmed hold).
                     Plus a quote-volume floor that skips dead low-volume sessions.

Standard neutral-grid safety, also owned here so every bot behaves identically:
  - re-center on drift (flatten + rebuild when price leaves the grid by > `recenter_drift`)
  - equity stop (flatten + halt when PnL breaches -stop_pct of allocation)
"""
import os


# ── Tunables (env-driven, shared defaults) ───────────────────────────────────

def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


class GridTuning:
    """All tunables for one neutral grid, with env-backed defaults."""

    # Mode presets: scalp (default — tight, fast TP) vs swing (wide ladder,
    # 2.5x TP, room to run). Explicit ctor params win; then GB_MODE_* env;
    # then these presets; env-backed defaults last.
    MODES = {
        "scalp": {"grid_levels": 6, "spacing_atr_mult": 0.6,
                  "spacing_min": 0.0035, "spacing_max": 0.03,
                  "tp_mult": 1.0, "equity_stop_pct": 0.15,
                  "max_hold_min": 360},
        "swing": {"grid_levels": 4, "spacing_atr_mult": 1.5,
                  "spacing_min": 0.006, "spacing_max": 0.06,
                  "tp_mult": 2.0, "equity_stop_pct": 0.25,
                  "max_hold_min": 1440},
    }

    def __init__(self,
                 grid_levels: int = None,
                 spacing_atr_mult: float = None,
                 spacing_min: float = None,
                 spacing_max: float = None,
                 spacing_fallback: float = None,
                 atr_period: int = None,
                 adx_period: int = None,
                 adx_range_max: float = None,
                 adx_hold_max: float = None,
                 scale_ticks: int = None,
                 min_part_vol: float = None,
                 recenter_drift: float = None,
                 equity_stop_pct: float = None,
                 min_notional: float = None,
                 tp_mult: float = None,
                 max_hold_min: float = None,
                 mode: str = None):
        # Explicit (non-None) params win; None falls back to env / hardcoded default.
        # Binance live overhaul 2026-08-31: deep ladder 6, wider spacing so TP>SL+fees,
        # tighter equity stop (7% vs 20%) so SL not 10× TP. Lev 10, full-volume
        # compounding (GB_FREE_MARGIN_FRAC 0.35), exchange book = truth.
        _mode = (mode or os.getenv("GB_GRID_MODE", "scalp") or "scalp").strip().lower()
        _preset = self.MODES.get(_mode, self.MODES["scalp"])
        self.mode = _mode if _mode in self.MODES else "scalp"
        self.grid_levels = grid_levels if grid_levels is not None else _i("GURU_LEVELS", _preset["grid_levels"])
        self.spacing_atr_mult = spacing_atr_mult if spacing_atr_mult is not None else _f("GURU_SPACING_ATR_MULT", _preset["spacing_atr_mult"])
        self.spacing_min = spacing_min if spacing_min is not None else _f("GURU_SPACING_MIN", _preset["spacing_min"])
        self.spacing_max = spacing_max if spacing_max is not None else _f("GURU_SPACING_MAX", _preset["spacing_max"])
        self.spacing_fallback = spacing_fallback if spacing_fallback is not None else _f("GURU_SPACING_PCT", 0.0035)
        self.atr_period = atr_period if atr_period is not None else _i("GURU_ATR_PERIOD", 14)
        self.adx_period = adx_period if adx_period is not None else _i("GURU_ADX_PERIOD", 14)
        self.adx_range_max = adx_range_max if adx_range_max is not None else _f("GURU_ADX_RANGE_MAX", 30.0)
        self.adx_hold_max = adx_hold_max if adx_hold_max is not None else _f("GURU_ADX_HOLD_MAX", 45.0)
        self.scale_ticks = scale_ticks if scale_ticks is not None else _i("GURU_SCALE_TICKS", 2)
        self.min_part_vol = min_part_vol if min_part_vol is not None else _f("GURU_MIN_PART_VOL", 25000.0)
        self.min_notional = min_notional if min_notional is not None else _f("GURU_MIN_NOTIONAL", 5.0)
        self.recenter_drift = recenter_drift if recenter_drift is not None else _f("GURU_RECENTER", 0.05)
        self.equity_stop_pct = equity_stop_pct if equity_stop_pct is not None else _f("GURU_EQUITY_STOP", _preset["equity_stop_pct"])
        self.tp_mult = tp_mult if tp_mult is not None else _f("GURU_TP_MULT", _preset["tp_mult"])
        self.max_hold_min = max_hold_min if max_hold_min is not None else _f("GURU_MAX_HOLD_MIN", _preset["max_hold_min"])
        self.min_notional = min_notional if min_notional is not None else _f("GURU_MIN_NOTIONAL", 5.0)


# ── Pure indicators (platform-agnostic, bars = list of {high, low, close, quote_volume}) ──

def atr_pct(bars, period: int) -> float:
    """ATR(period) as a fraction of the last close. 0.0 if insufficient data."""
    if len(bars) < period + 2:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h = float(bars[i]["high"]); l = float(bars[i]["low"])
        cp = float(bars[i - 1]["close"])
        trs.append(max(h - l, abs(h - cp), abs(l - cp)))
    atr = sum(trs[-period:]) / period
    close = float(bars[-1]["close"])
    return (atr / close) if close > 0 else 0.0


def adx(bars, period: int) -> float:
    """ADX(period) with proper Wilder smoothing. 0.0 on insufficient data
    (treated as ranging). The old SMA-based DX read ~28 pts hot on live
    Binance data (a steady one-directional grind pushed DX toward 100
    regardless of magnitude); Wilder's RMA damping matches the classic
    indicator and is what industry thresholds assume."""
    if len(bars) < period * 2 + 2:
        return 0.0
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    closes = [float(b["close"]) for b in bars]
    n = len(closes)
    plus_dm = [0.0]; minus_dm = [0.0]; tr = [0.0]
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        tr.append(max(highs[i] - lows[i],
                      abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1])))

    def rma(vals):
        """Wilder's recursive moving average."""
        out = [vals[0]]
        for v in vals[1:]:
            out.append((out[-1] * (period - 1) + v) / period)
        return out

    rp = rma(plus_dm); rm = rma(minus_dm); rt = rma(tr)
    dxs = []
    for i in range(n):
        if rt[i] <= 0:
            continue
        pd = 100.0 * rp[i] / rt[i]
        md = 100.0 * rm[i] / rt[i]
        s = pd + md
        dxs.append(100.0 * abs(pd - md) / s if s > 0 else 0.0)
    return rma(dxs)[-1] if dxs else 0.0


def quote_volume(bars) -> float:
    """Sum of quote-asset volume across bars (participation gauge)."""
    return sum(float(b.get("quote_volume", 0) or 0) for b in bars)


# ── Core strategy ────────────────────────────────────────────────────────────

class NeutralGridCore:
    """Owns grid strategy state + decisions. Execution is delegated to adapters."""

    def __init__(self, tuning: GridTuning = None):
        self.t = tuning or GridTuning()

        self.center: float = 0.0
        self.spacing_pct: float = self.t.spacing_fallback
        self.active_levels: int = 1
        self.grid_levels: int = self.t.grid_levels

        self._scale_tick: int = 0
        self._adx_shown: bool = False
        self._trend_logged: bool = False
        self._range_hold: bool = False

    # ── spacing / regime / participation ──
    def compute_spacing(self, atr_pct_value: float, log=None) -> float:
        if atr_pct_value > 0:
            self.spacing_pct = max(self.t.spacing_min,
                                   min(self.t.spacing_max,
                                       atr_pct_value * self.t.spacing_atr_mult))
            if log:
                log(f"ATR {atr_pct_value*100:.2f}% → spacing {self.spacing_pct*100:.2f}%")
        return self.spacing_pct

    def regime_ok(self, adx_value: float, log=None) -> bool:
        """Range gate with hysteresis: ENTER only below the range cap, but once
        trading, HOLD until ADX exceeds the hold cap (cap * ~1.5). Prevents
        flip-flopping as ADX oscillates across a single threshold, and lets a
        range grid keep working through mild drifts instead of being gated by
        any wisp of trend."""
        cap = self.t.adx_range_max
        hold = max(self.t.adx_hold_max, cap)
        if not self._adx_shown:
            self._adx_shown = True
            if log:
                log(f"regime ADX={adx_value:.0f} (range cap {cap:.0f}, hold {hold:.0f})")
        if self._range_hold:
            if adx_value > hold:
                self._range_hold = False
                self._trend_logged = False
                if log:
                    log(f"🚫 trend strong (ADX {adx_value:.0f} > {hold:.0f}) — leaving range")
            return self._range_hold
        if adx_value < cap:
            self._range_hold = True
            self._trend_logged = False
            if log:
                log(f"✅ range regime (ADX {adx_value:.0f} < {cap:.0f})")
            return True
        # trending: log ONCE per trending episode, not every tick
        if not self._trend_logged:
            self._trend_logged = True
            if log:
                log(f"🚫 trending (ADX {adx_value:.0f} ≥ {cap:.0f}) — pausing scale-in")
        return False

    def participation_ok(self, quote_vol: float, log=None) -> bool:
        if self.t.min_part_vol <= 0:
            return True
        if quote_vol < self.t.min_part_vol:
            if log:
                log(f"⏸ low participation ${quote_vol/1000:.0f}k < ${self.t.min_part_vol/1000:.0f}k floor")
            return False
        return True

    # ── level geometry ──
    def level_pairs(self, center: float, n: int) -> list:
        """[(side, price)] for `n` levels per side around `center`."""
        pairs = []
        for i in range(1, n + 1):
            pairs.append(("BUY", center * (1 - self.spacing_pct * i)))
            pairs.append(("SELL", center * (1 + self.spacing_pct * i)))
        return pairs

    def tp_price(self, side: str, fill_price: float) -> float:
        m = float(getattr(self.t, "tp_mult", 1.0) or 1.0)
        return fill_price * (1 + self.spacing_pct * m) if side == "BUY" \
            else fill_price * (1 - self.spacing_pct * m)

    # ── scale-in decision ──
    def escalate(self, adx_value: float, quote_vol: float, log=None):
        """Fill-gated scale-in: open the next level pair ONLY when an order has
        actually FILLED (i.e. a position leg was created). No timers — a level
        never opens while its predecessor is still resting, so the resting
        order count stays bounded by the filled levels."""
        if self.active_levels >= self.grid_levels:
            return None
        if not self.regime_ok(adx_value, log):
            return None
        if not self.participation_ok(quote_vol, log):
            return None
        if self.center <= 0:
            return None
        level = self.active_levels + 1
        self.active_levels = level
        if log:
            log(f"⬆️ scaled in → {self.active_levels} level(s)/side (fill-gated)")
        return [
            ("BUY", self.center * (1 - self.spacing_pct * level)),
            ("SELL", self.center * (1 + self.spacing_pct * level)),
        ]

    def reset_grid(self, reset_regime: bool = True):
        """Fresh grid: back to 1 level/side (used on re-center + daily reset).

        `reset_regime=False` keeps the range-hold state (in-range re-centers
        must not drop it, or the bot would re-flatten mid-range on mild drift);
        a gate flatten / daily reset re-evaluates from scratch (default True)."""
        self.active_levels = 1
        self._scale_tick = 0
        if reset_regime:
            self._range_hold = False

    # ── risk ──
    def should_recenter(self, price: float) -> bool:
        return (self.center > 0 and price > 0
                and abs(price - self.center) / self.center > self.t.recenter_drift)

    def should_equity_stop(self, total_pnl: float, alloc: float) -> bool:
        return total_pnl <= -abs(alloc * self.t.equity_stop_pct)
