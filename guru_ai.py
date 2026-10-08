"""
GuruAI — volatility-harvesting NEUTRAL grid on sub-$0.50 Binance futures coins.

Thesis (validated by inspection, not assumed):
  Micro-cap futures that just pumped are extremely volatile (±15-75% swings).
  A two-sided (neutral) grid sells into rises and buys into dips, harvesting
  oscillation in EITHER direction. We do NOT bet on the fall — we ride the range.

Safety:
  - Neutral grid: both BUY (below) and SELL (above) limit levels.
  - Daily 00:00 UTC reset: cancel everything, flatten, rescan fresh symbols.
  - Per-bot equity stop: flatten + stop if a bot bleeds past its limit.
  - Exchange is the source of truth for orders/positions (no ghosts).
"""
import json
import logging
import os
import threading
import time
import urllib.request
import hashlib
import hmac
from datetime import datetime, timezone
from typing import Optional, List, Dict

from bridge import BinanceBridge
from orchestrator import (BotStatus, DuplicateSymbolError, level_usd,
                          _range_gate_enabled, _session_name,
                          _env_f, _SESSION_PCT_DEFAULTS, _SESSION_USD_DEFAULTS,
                          _SPACING_FEE_FLOOR, _SPACING_CEIL)
from neutral_grid import NeutralGridCore, GridTuning, atr_pct, adx, quote_volume
from memory_guard import (MAX_BINANCE_BOTS, clamp_guru_n, remaining_binance_slots,
                           assert_binance_slots, FleetLimitError)

logger = logging.getLogger("hybrid.guruai")

# ── Tuning ───────────────────────────────────────────────────────────────────
# Binance live overhaul 2026-08-31: deep ladder 6 (was 3), spacing 0.35% min
# (was 0.8%) so TP covers fees+SL, full-volume compounding, lev 10 fallback,
# AI primary (OpenRouter) with rule fallback, exchange book = truth.
GRID_LEVELS = int(os.getenv("GURU_LEVELS", "6"))            # levels per side — deep ladder
SPACING_PCT = float(os.getenv("GURU_SPACING_PCT", "0.0035"))  # fallback spacing (ATR overrides)
MAX_PRICE = float(os.getenv("GURU_MAX_PRICE", "0.5"))
MIN_QUOTE_VOL = float(os.getenv("GURU_MIN_VOL", "5000000"))  # $5M 24h liquidity floor
MIN_GAIN_PCT = float(os.getenv("GURU_MIN_GAIN", "10.0"))     # prefer >=10% 24h gainers
# Scan universe: by default the FULL USDT-M market (liquidity + blacklist
# filtered). Set GURU_UNIVERSE to a comma list to restrict to a whitelist
# (e.g. "BTCUSDT,ETHUSDT,XAUUSDT"). AKEUSDT is permanently blacklisted below.
UNIVERSE_SYMBOLS = tuple(s.strip().upper() for s in
    os.getenv("GURU_UNIVERSE", "").split(",") if s.strip())
# Permanent blacklist — avoid meme/illiquid that bleed 10% gaps (user: avoid buying meme symbols)
BLACKLIST_SYMBOLS = tuple(s.strip().upper() for s in
    os.getenv("GURU_BLACKLIST", "AKEUSDT,PEPEUSDT,1000PEPEUSDT,BONKUSDT,DOGEUSDT,SHIBUSDT,FLOKIUSDT,WIFUSDT,MEMEUSDT,TRUMPUSDT,USELESSUSDT").split(",") if s.strip())
RECENTER_DRIFT = float(os.getenv("GURU_RECENTER", "0.05"))   # re-center if price drifts 5%
# Stale-ladder proximity re-center (2026-08-21): when flat and EVERY ladder
# level sits farther than STALE_LEVEL_PCT from price for STALE_LEVEL_MIN
# minutes, re-center at market — the grid follows the session instead of
# idling until the 5% band-drift or the daily reset.
STALE_LEVEL_PCT = float(os.getenv("GURU_STALE_LEVEL_PCT", "0.5"))   # % distance
STALE_LEVEL_MIN = float(os.getenv("GURU_STALE_LEVEL_MIN", "15"))    # minutes
EQUITY_STOP_PCT = float(os.getenv("GURU_EQUITY_STOP", "0.15"))  # -15% of allocation -> stop (was 7% — fired at 0.24% price on 6-leg XAU = the real tiny SL)
# Fixed anchors (XAUUSDT/BTCUSDT) trade ~40x their alloc in notional, so a
# -15%-of-alloc stop = a ~0.9% price wiggle (death loop: stop -> restart ->
# stop). Anchors get their own, wider stop. Movers keep GURU_EQUITY_STOP.
EQUITY_STOP_ANCHOR_PCT = float(os.getenv("GURU_EQUITY_STOP_ANCHOR", "0.25"))  # -25% of alloc for anchors
# Anchors restart on the SAME symbol after a halt; lengthening this vs the
# mover cooldown (SYM_COOLDOWN_SEC=45min) slows the taker-fee death loop.
ANCHOR_COOLDOWN_SEC = float(os.getenv("GURU_ANCHOR_COOLDOWN_SEC", "7200"))  # 2h
# Periodic re-fetch of the exchange PRICE_FILTER/LOT_SIZE (tick-size changes
# happen mid-session — Binance 2026-08-15 announcement). Rotation recreates
# bridges; this covers never-rotated anchors.
SYMBOL_INFO_REFRESH_SEC = float(os.getenv("GURU_SYM_INFO_REFRESH_SEC", "300"))
CRASH_GUARD_PCT = float(os.getenv("GURU_CRASH_GUARD", "0.05"))  # halt if price moves >5% from exposure ref
SCALE_FREEZE_PCT = float(os.getenv("GURU_SCALE_FREEZE", "0.04"))  # no scale-in if price >4% below center
SESSION_REQUOTE_HYSTERESIS = float(os.getenv("GURU_SESSION_REQUOTE_HYST", "0.20"))  # re-quote ladder only if spacing moves >20% (or session changes)
TICK_SEC = float(os.getenv("GURU_TICK_SEC", "20"))
ANALYTICS_TTL_SEC = float(os.getenv("GURU_ANALYTICS_TTL_SEC", "60"))
POS_INFO_TTL_SEC = float(os.getenv("GURU_POS_INFO_TTL_SEC", "10"))
RESET_HOUR_UTC = int(os.getenv("GURU_RESET_HOUR", "0"))      # 00:00 UTC daily reset
# Max time an open position may live before the bot force-closes it at market
# (banks the win or cuts the loss; the slot then becomes rotation-eligible).
# 0 disables. Closes the "stuck symbol forever" blind spot.
MAX_HOLD_MIN = float(os.getenv("GURU_MAX_HOLD_MIN", "360"))

# ── Global sizing controls (dashboard sliders, also .env-overridable) ─────────
# Vol %  : per-level size multiplier (50 = 1x, 100 = 2x). GB_VOL_PCT default.
# Wallet %: max share of free margin the WHOLE fleet may lock as margin
#   (capacity_total = free × lev × pct). Complements vol% (size/level vs
#   total deployed). Default 70 preserves the historical 0.7 factor.
# Leverage: snapped to {5,10,25,50,75,100}, anything else falls back to 10.
_LEV_CHOICES = (5, 10, 25, 50, 75, 100)


def clean_leverage(v):
    """Snap to {5,10,25,50,75,100}; 'MAX' passes through for per-symbol
    bracket resolution at spawn; anything else falls back to 10."""
    try:
        if isinstance(v, str) and v.strip().lower() == "max":
            return "MAX"
        iv = int(float(v))
    except (TypeError, ValueError):
        return 10
    return iv if iv in _LEV_CHOICES else 10


def cap_leverage(v) -> int:
    """Numeric leverage for capacity math (MAX -> conservative 10)."""
    return 10 if isinstance(v, str) else int(v or 10)


def run_vol_pct(v=None) -> int:
    try:
        iv = int(v if v is not None else os.getenv("GB_VOL_PCT", "50"))
    except (TypeError, ValueError):
        iv = 50
    return max(10, min(100, iv))


def wallet_use_frac(v=None) -> float:
    try:
        p = float(v if v is not None else os.getenv("GB_WALLET_USE_PCT", "70"))
    except (TypeError, ValueError):
        p = 70.0
    return max(5.0, min(100.0, p)) / 100.0


def prop_mode() -> bool:
    """Prop-firm challenge discipline: Friday-flat + news blackout.
    GB_PROP_MODE=1 enables. Default 0 (off)."""
    return (os.getenv("GB_PROP_MODE", "0") or "0").strip() == "1"


def _prop_events():
    """Parse GB_NEWS_EVENTS='YYYY-MM-DD HH:MM,...' (UTC) into epoch list."""
    out = []
    try:
        from datetime import datetime as _dt
        for part in (os.getenv("GB_NEWS_EVENTS", "") or "").split(","):
            part = part.strip()
            if not part:
                continue
            out.append(_dt.strptime(part, "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc).timestamp())
    except Exception:
        pass
    return out


def weekend_flat_reason() -> str:
    """Weekend-flat applies to BOTH modes (prop or not): flat from Fri 21:00
    UTC through Sun 22:00 UTC. '' when trading allowed."""
    try:
        now = datetime.now(timezone.utc)
        wd, hr = now.weekday(), now.hour
        if (wd == 4 and hr >= 21) or wd == 5 or (wd == 6 and hr < 22):
            return "weekend-flat"
    except Exception:
        pass
    return ""


def prop_flat_reason() -> str:
    """'' when trading allowed, else the reason. Weekend-flat is universal
    (prop or not); news blackout additionally binds when GB_PROP_MODE=1."""
    _w = weekend_flat_reason()
    if _w:
        return _w
    if not prop_mode():
        return ""
    try:
        now = datetime.now(timezone.utc)
        try:
            pre = float(os.getenv("GB_NEWS_PRE_MIN", "30") or 30) * 60
            post = float(os.getenv("GB_NEWS_POST_MIN", "15") or 15) * 60
        except (TypeError, ValueError):
            pre, post = 1800.0, 900.0
        ts = time.time()
        for ev in _prop_events():
            if ev - pre <= ts <= ev + post:
                return "prop news-blackout"
        return ""
    except Exception:
        return ""


def resolve_grid_mode(mode, symbol: str = "", user_id=None, env: str = "demo") -> str:
    """auto → scalp (ADX<25, range harvest) else swing (trend ride).
    Fail-open scalp. Called per pick so each symbol gets its own regime."""
    m = (mode or os.getenv("GB_GRID_MODE", "scalp") or "scalp").strip().lower()
    if m in ("scalp", "swing"):
        return m
    if m != "auto":
        return "scalp"
    try:
        from bridge import rest_client
        c = rest_client(user_id, env)
        if c is None:
            return "scalp"
        kl = c.futures_klines(symbol=symbol, interval="15m", limit=40)
        bars = [{"high": float(k[2]), "low": float(k[3]), "close": float(k[4]),
                 "quote_volume": float(k[7])} for k in kl]
        if len(bars) < 30:
            return "scalp"
        v = adx(bars, 14)
        return "scalp" if v < 25.0 else "swing"
    except Exception as e:
        logger.debug(f"auto-mode {symbol}: {str(e)[:80]} -> scalp")
        return "scalp"


# ── ProfitBank: bank unrealized profit, keep the bots alive ───────────────────
# Research-backed (Phemex/KuCoin: 5-10% grid profit targets; Hummingbot/Bybit
# pro systems: trailing locks off the unrealized PEAK). For a 10x account we
# bank earlier than the 5-10% spot-grid advice, and the threshold SCALES with
# the number of open positions (observed: 3-position waves ~1.7% of wallet
# kept giving back before a fixed 3% ever fired):
#   1) bank when total unrealized >= max(N x 0.5%, 1%) of wallet, cap 3%  -> a
#      3-position wave banks at 1.5% (~$4 on $267) instead of waiting for $8
#   2) trailing: if the peak reached >= max(N x 0.4%, 1%) (cap 2%) of wallet and
#      then gave back >= GIVEBACK (50%) of the peak, bank the remainder (never
#      watch a win decay). Positions are market-closed per bot, bots keep
#      running and re-ladder fresh (re-centered) grids. No bots are stopped.
BANK_ENABLED = int(os.getenv("GURU_BANK", "1"))
BANK_SCAN_SEC = float(os.getenv("GURU_BANK_SCAN_SEC", "10"))
BANK_PCT = float(os.getenv("GURU_BANK_PCT", "0.03"))
BANK_TRAIL_PCT = float(os.getenv("GURU_BANK_TRAIL_PCT", "0.02"))
BANK_TRAIL_GIVEBACK = float(os.getenv("GURU_BANK_TRAIL_GIVEBACK", "0.5"))
# Emergency collective floor (flood-only): total PnL <= -(EMERGENCY_PCT x
# wallet) closes ALL positions and stops every bot. Deliberately DEEP (-10%):
# normal bad days are handled by the individual per-bot stops (a single bot
# maxes out around -$2-3), so this only fires on a whole-book crash. It is
# the flood cap; individual stops are the everyday guards.
BANK_EMERGENCY_PCT = float(os.getenv("GURU_BANK_EMERGENCY_PCT", "0.10"))
BANK_LOSS_COOLDOWN = float(os.getenv("GURU_BANK_LOSS_COOLDOWN_SEC", "300"))
BANK_MIN_POS_PNL = float(os.getenv("GURU_BANK_MIN_POS_PNL", "0.50"))  # min per-symbol winner to bank-close
# ── Per-bot trailing profit lock (2026-09-01 rev2 — bigger wins, wide stop) ──
# User: profits were $0.05-0.16 because trail armed at $0.30 (0.15% move on
# $196 leg) and gave back 30% → locked $0.10 before TP 1.2% ($2.35) ever hit.
# New: ARM $1.00 / 1.5% of notional — arms at a real 1.5% move, floor 75% of
# peak, TP 2.5% runs; partial 50% scalp at ARM banks half immediately.
BOT_TRAIL_ARM_USD = float(os.getenv("GURU_BOT_TRAIL_ARM_USD", "1.00"))
TRAIL_ARM_FLOOR_USD = float(os.getenv("GURU_TRAIL_ARM_FLOOR_USD", "0.20"))  # small-account floor
BOT_TRAIL_ARM_PCT = float(os.getenv("GURU_BOT_TRAIL_ARM_PCT", "0.015"))  # % of notional (1.5%)
BOT_TRAIL_GIVEBACK = float(os.getenv("GURU_BOT_TRAIL_GIVEBACK", "0.25"))
BOT_TRAIL_MIN_USD = float(os.getenv("GURU_BOT_TRAIL_MIN_USD", "0.10"))
# Tier-2 trail: once peak ≥ TIER2, lock tightens to trail under peak
BOT_TRAIL_TIER2_USD = float(os.getenv("GURU_BOT_TRAIL_TIER2_USD", "2.50"))
BOT_TRAIL_TIER2_TRAIL = float(os.getenv("GURU_BOT_TRAIL_TIER2_TRAIL", "0.18"))
# Fix B (2026-08-18): while the trail is armed, keep a REAL STOP_MARKET
# order on the exchange at the lock floor (ratcheted up with the peak).
# The exchange enforces the floor even when price gaps through it between
# the 10s samples — the polling lock alone closed +$2.2 of a ~$5.5 peak
# on ACE because the gap outran the sample.
TRAIL_STOP_EXCHANGE = os.getenv("GURU_TRAIL_STOP_EXCHANGE", "1") == "1"
# Loss-side exchange stop (2026-08-18): mirror of the trail stop for the
# equity threshold. A fast adverse bar used to gap through the -10%-alloc
# bound between the 10s samples (ACE: cut at -$4.25 vs the -$1.75 bound).
LOSS_STOP_EXCHANGE = os.getenv("GURU_LOSS_STOP_EXCHANGE", "1") == "1"
# Position-scaled bank: the more legs are open, the sooner we bank — observed
# that 3-position waves (~1.7% of wallet) repeatedly gave back to <1% before
# the fixed 3% trigger ever fired. target = clamp(max(N x PER_POS, FLOOR), CAP)
# where N = number of open positions. Trailing arm scales the same way.
BANK_PCT_PER_POS = float(os.getenv("GURU_BANK_PCT_PER_POS", "0.005"))
BANK_PCT_FLOOR = float(os.getenv("GURU_BANK_PCT_FLOOR", "0.01"))
BANK_PCT_CAP = float(os.getenv("GURU_BANK_PCT_CAP", "0.03"))
BANK_TRAIL_PER_POS = float(os.getenv("GURU_BANK_TRAIL_PER_POS", "0.004"))
BANK_TRAIL_FLOOR = float(os.getenv("GURU_BANK_TRAIL_FLOOR", "0.01"))
BANK_TRAIL_CAP = float(os.getenv("GURU_BANK_TRAIL_CAP", "0.02"))

_bank_lock = threading.Lock()
_bank_thread: Optional[threading.Thread] = None
_bank_stop = threading.Event()
_bank_managers: List["GuruAIManager"] = []
_bank_state = {"peak": 0.0, "wallet": 0.0, "banks": 0, "loss_cuts": 0,
               "last_pnl": 0.0, "restart_at": 0.0, "n_pos": 0, "target_pct": 0.0,
               "trail_locks": 0}
# per-(user,env,symbol) unrealized peak for the per-bot trailing lock
_TRAIL_PEAKS: Dict[tuple, float] = {}
_TRAIL_ARMED: set = set()
_TRAIL_PARTIAL: set = set()  # already did 50% scalp at ARM, remainder trails
# per-(user,env,symbol) exchange STOP_MARKET order enforcing the lock floor
_TRAIL_STOPS: Dict[tuple, dict] = {}
# per-(user,env,symbol) exchange STOP_MARKET order enforcing the equity bound
_LOSS_STOPS: Dict[tuple, dict] = {}
_trail_lock = threading.Lock()

# ── Market-activity gate ────────────────────────────────────────────────────
# The grid only earns while the market oscillates. Dead sessions (weekends,
# quiet hours) produce zero income but still risk the occasional thin-volume
# spike halt (~-$3). The gate measures LIVE activity from the shared scan
# pool every GURU_GATE_SCAN_SEC and parks the fleet flat while the market
# sleeps; auto-resumes (fresh scan, current prices) when activity returns.
# Not calendar-based: some weekends wake up (Sunday Asia open) and some
# weekdays die (holidays). Lives inside the bank supervisor so it survives
# while bots are parked.
MARKET_GATE_ENABLED = os.getenv("GURU_MARKET_GATE", "1") == "1"
MARKET_GATE_SCAN_SEC = float(os.getenv("GURU_GATE_SCAN_SEC", "300"))
MARKET_GATE_MIN_MOVERS = int(os.getenv("GURU_GATE_MIN_MOVERS", "4"))
MARKET_GATE_MIN_MOVE = float(os.getenv("GURU_GATE_MIN_MOVE", "0.3"))   # % 15m
MARKET_GATE_BTC_MIN = float(os.getenv("GURU_GATE_BTC_MIN", "0.15"))    # % 15m
MARKET_GATE_SLEEP_CHECKS = int(os.getenv("GURU_GATE_SLEEP_CHECKS", "2"))

# ── Session lock (strict calendar, 2026-08-18) ──
# Phase-1 analysis (hourly rollup, 3 days): the dead zone is ~20:00-08:00
# UTC — near-zero fees, wallet drift only, thin-volume crash-guard
# whipsaws (2 halts on the 2026-08-18 Asia night alone). Movers now trade
# ONLY Mon-Fri GURU_ACTIVE_START_HOUR..END_HOUR UTC (default 08-22); the
# fixed anchor (XAUUSDT) keeps trading 24/7 on weekdays. Weekends:
# everything is parked, but the market-activity gate can still wake the
# fleet if the market genuinely moves (Sunday Asia pumps) — the gate
# remains the ultimate activity authority.
SESSION_LOCK_ENABLED = os.getenv("GURU_SESSION_LOCK", "1") == "1"
SESSION_START_HOUR = int(os.getenv("GURU_ACTIVE_START_HOUR", "8"))
SESSION_END_HOUR = int(os.getenv("GURU_ACTIVE_END_HOUR", "22"))

_session_lock_state = {"last_wake_attempt": 0.0}


def _session_allow() -> tuple:
    """(movers_allowed, anchors_allowed) under the strict calendar lock."""
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:                       # Sat/Sun: defer to the market gate
        awake = not _market_gate_asleep()
        return (awake, awake)
    movers = SESSION_START_HOUR <= now.hour < SESSION_END_HOUR
    return (movers, True)                        # anchors: 24/7 weekdays

_market_gate_state = {"asleep": False, "dead_checks": 0, "last_check": 0.0,
                      "parked_at": 0.0}


def _market_gate_asleep() -> bool:
    return bool(_market_gate_state.get("asleep", False))


def _market_gate_tick(managers: List["GuruAIManager"]):
    """Check market activity. 2 consecutive dead checks -> park ALL bots flat
    (they stay registered). 1 alive check -> prune + fresh start. Hysteresis:
    fast wake, slow sleep (no flip-flopping at the boundary)."""
    managers = _auto_fleet_managers(managers)
    if not MARKET_GATE_ENABLED or not managers:
        return
    now = time.time()
    if now - _market_gate_state["last_check"] < MARKET_GATE_SCAN_SEC:
        return
    _market_gate_state["last_check"] = now
    try:
        pool = _fetch_scan_pool(0)
    except Exception:
        return
    if not pool:
        return
    movers = sum(1 for c in pool
                 if abs(c.get("gain_15m", 0.0) or 0.0) >= MARKET_GATE_MIN_MOVE)
    btc = next((abs(c.get("gain_15m", 0.0) or 0.0)
                for c in pool if c["symbol"] == "BTCUSDT"), 0.0)
    awake = movers >= MARKET_GATE_MIN_MOVERS or btc >= MARKET_GATE_BTC_MIN

    if awake:
        if _market_gate_state["asleep"]:
            _market_gate_state["asleep"] = False
            _market_gate_state["dead_checks"] = 0
            mov_ok, anc_ok = _session_allow()
            logger.info(f"☀️ MARKET AWAKE (movers={movers}, BTC 15m={btc:.2f}%) — resuming GuruAI "
                        f"(session: movers={mov_ok}, anchors={anc_ok})")
            for m in managers:
                try:
                    m.prune()
                    free = _bank_free_margin(m.user_id, m.env)
                    if free <= 0:
                        free = _bank_free_margin(m.user_id, m.env)
                    r = m.start(free_margin=free,
                                include_movers=mov_ok, include_anchors=anc_ok)
                    if r.get("ok"):
                        logger.info(f"☀️ resumed {len(r['started'])} grids [{m.group}]: "
                                    f"{', '.join(s['symbol'] for s in r['started'])}")
                except Exception as e:
                    logger.warning(f"market-gate resume failed [{m.group}]: {e}")
        else:
            _market_gate_state["dead_checks"] = 0
        return

    if _market_gate_state["asleep"]:
        # keep the fleet parked; re-park anything manually started while
        # the market was asleep (the gate owns park/resume state).
        running = [b for m in managers for b in m._bots.values()
                   if b.status == BotStatus.RUNNING]
        if running:
            stopped = sum(m.stop_all() for m in managers)
            logger.info(f"😴 re-parked {stopped} manually started bot(s) — "
                        f"market still asleep")
        return
    _market_gate_state["dead_checks"] += 1
    if _market_gate_state["dead_checks"] >= MARKET_GATE_SLEEP_CHECKS:
        _market_gate_state["asleep"] = True
        _market_gate_state["parked_at"] = time.time()
        stopped = sum(m.stop_all() for m in managers)
        logger.info(f"😴 MARKET ASLEEP (movers={movers}/{MARKET_GATE_MIN_MOVERS}, "
                    f"BTC 15m={btc:.2f}%) — parked {stopped} bot(s); "
                    f"auto-resume when activity returns")


def _auto_fleet_managers(managers: List["GuruAIManager"]) -> list:
    """🧠 meme/all Binance only. ＋ Spawn is group=manual — operator picked
    one symbol; session-lock/market-gate must never fill the other slots.
    MT5 managers use their own start paths, not the USDT-M scanner."""
    return [m for m in managers
            if getattr(m, "group", "") != "manual"
            and getattr(m, "platform", "binance") in ("binance", "")]


def _session_lock_tick(managers: List["GuruAIManager"]):
    """Strict calendar lock. Parks disallowed bots (per-symbol, removed from
    the map so the rotation supervisor's dead-slot replacement can't churn
    them back in) and wakes allowed slots when the market gate is awake.
    Runs every bank tick (10s) — park is instant, wake is throttled."""
    managers = _auto_fleet_managers(managers)
    if not SESSION_LOCK_ENABLED or not managers:
        return
    movers_ok, anchors_ok = _session_allow()
    if movers_ok:
        try:
            from session_book import purge_old_sessions, trading_session_date
            today = trading_session_date()
            if _session_lock_state.get("purged") != today:
                if purge_old_sessions():
                    _session_lock_state["purged"] = today
                    logger.info(f"🗓 SESSION LOCK wiped prior session_book (keep {today})")
        except Exception as e:
            logger.debug(f"session_book purge: {e}")
    for m in managers:
        # ── park the disallowed subset ──
        parked = []
        with m._lock:
            for bid, b in list(m._bots.items()):
                if b.status != BotStatus.RUNNING:
                    continue
                is_anchor = b.symbol in m.fixed_symbols
                if (is_anchor and not anchors_ok) or (not is_anchor and not movers_ok):
                    try:
                        b.stop(close_positions=True)
                    except Exception:
                        pass
                    _guru_release(m.user_id, m.env, b.symbol)
                    m._bots.pop(bid, None)
                    try:
                        m.orch._bots.pop(bid, None)
                        m.orch._delete_from_registry(bid)
                        m.orch._registry.pop(bid, None)
                        b.bridge.disconnect()
                    except Exception:
                        pass
                    parked.append(b.symbol)
        if parked:
            logger.info(f"🗓 SESSION LOCK parked {len(parked)} bot(s) [{m.group}]: "
                        f"{', '.join(parked)}")
        # ── wake allowed slots (market gate must be awake) ──
        if _market_gate_asleep():
            continue
        if time.time() - _session_lock_state["last_wake_attempt"] < 120:
            continue
        # Count movers in ANY status: a crash-guard/equity-stopped mover
        # still owns its slot — the rotation supervisor's dead-slot
        # replacement fills it after the 45-min cooldown. Filling it here
        # too double-fills the fleet (observed 2026-08-18: 4 -> 6 bots).
        movers_present = sum(1 for b in m._bots.values()
                             if b.symbol not in m.fixed_symbols)
        anchors_present = sum(1 for b in m._bots.values()
                              if b.symbol in m.fixed_symbols)
        expected_movers = max(m.n_bots - len(m.fixed_symbols), 0)
        need_movers = movers_ok and movers_present < expected_movers
        need_anchors = anchors_ok and anchors_present <= 0
        # ── trim overfill (belt-and-suspenders for any future double-fill) ──
        # Stop+remove the OLDEST movers beyond the expected count, but only
        # when rotation-safe (level-1, flat) so a live leg is never cut.
        if movers_present > expected_movers:
            extra = [b for b in m._bots.values()
                     if b.symbol not in m.fixed_symbols][:movers_present - expected_movers]
            trimmed = 0
            for b in extra:
                if b.status == BotStatus.RUNNING and not b.rotation_safe():
                    continue
                bid = next((k for k, v in m._bots.items() if v is b), None)
                if not bid:
                    continue
                try:
                    b.stop(close_positions=True)
                except Exception:
                    pass
                _guru_release(m.user_id, m.env, b.symbol)
                with m._lock:
                    m._bots.pop(bid, None)
                try:
                    m.orch._bots.pop(bid, None)
                    m.orch._delete_from_registry(bid)
                    m.orch._registry.pop(bid, None)
                    b.bridge.disconnect()
                except Exception:
                    pass
                trimmed += 1
            if trimmed:
                logger.info(f"🗓 SESSION LOCK trimmed {trimmed} overfilled mover(s) "
                            f"[{m.group}]")
                movers_present -= trimmed
        if not (need_movers or need_anchors):
            continue
        _session_lock_state["last_wake_attempt"] = time.time()
        try:
            free = _bank_free_margin(m.user_id, m.env)
            if free <= 0:
                continue
            if need_anchors:
                r = m.start_anchors(free_margin=free)
                if r.get("ok"):
                    logger.info(f"🗓 SESSION LOCK started anchors [{m.group}]: "
                                f"{', '.join(s['symbol'] for s in r['started'])}")
            if need_movers:
                r = m.start_movers(free_margin=free)
                if r.get("ok"):
                    logger.info(f"🗓 SESSION LOCK started movers [{m.group}]: "
                                f"{', '.join(s['symbol'] for s in r['started'])}")
        except Exception as e:
            logger.warning(f"session-lock wake failed [{m.group}]: {e}")


def register_bank_manager(m: "GuruAIManager"):
    """Hook a GuruAIManager into the account-wide ProfitBank supervisor."""
    global _bank_thread
    with _bank_lock:
        if m not in _bank_managers:
            _bank_managers.append(m)
        if _bank_thread is None and BANK_ENABLED:
            _bank_stop.clear()
            _bank_thread = threading.Thread(target=_bank_loop, daemon=True,
                                            name="profit-bank")
            _bank_thread.start()


def unregister_bank_manager(m: "GuruAIManager"):
    global _bank_thread
    with _bank_lock:
        if m in _bank_managers:
            _bank_managers.remove(m)
        if not _bank_managers and _bank_thread:
            _bank_stop.set()
            if _bank_thread is not threading.current_thread():
                _bank_thread.join(timeout=5)
            _bank_thread = None


def _bot_platform(b) -> str:
    br = getattr(b, "bridge", None)
    return str(getattr(br, "platform", "") or "").lower()


def _trail_floor(peak: float, giveback: float = None, min_usd: float = None) -> float:
    """Lock floor under a uPnL peak. Giveback 20% of peak, but at least
    `min_usd` of room so a just-armed micro peak is not tick-stopped.
    Floor is 0 when peak is smaller than that room (wait for a real run)."""
    gb = BOT_TRAIL_GIVEBACK if giveback is None else giveback
    mn = BOT_TRAIL_MIN_USD if min_usd is None else min_usd
    peak = float(peak or 0)
    room = max(float(mn or 0), peak * float(gb or 0))
    return max(0.0, peak - room)


def _bank_account_pnl(bots: List["GuruAIBot"]) -> tuple:
    """(total_unrealized, wallet_balance, n_pos, per_symbol_unrealized).

    Binance: one futures_account() for the desk. MT5 stays on its own
    supervisor.
    (0,0,0,{}) on failure (rate-limit etc)."""
    from bridge import rest_client
    per_sym: dict = {}
    n_pos = 0
    total = 0.0
    wallet = 0.0
    binance_done = False
    for b in bots:
        br = getattr(b, "bridge", None)
        if br is None or not b.user_id:
            continue
        plat = _bot_platform(b)
        if plat == "mt5":
            continue
        if binance_done:
            continue
        env = "live" if getattr(br, "is_testnet", False) is False else "demo"
        try:
            c = getattr(br, "_client", None) or rest_client(b.user_id, env)
            if c is None:
                continue
            d = c.futures_account()
            for p in d.get("positions", []):
                if abs(float(p.get("positionAmt", 0) or 0)) > 1e-8:
                    n_pos += 1
                    per_sym[p["symbol"]] = float(p.get("unRealizedProfit", 0) or 0)
            total += float(d.get("totalUnrealizedProfit", 0) or 0)
            wallet = float(d.get("totalWalletBalance", 0) or 0)
            binance_done = True
        except Exception as e:
            logger.warning(f"ProfitBank account query failed: {e}")
    return (total, wallet, n_pos, per_sym)


def _bot_notional(b) -> float:
    """Notional of a bot's open position (abs qty x mark price). Uses the
    bot's cached position read (POS_INFO_TTL_SEC) — no extra API cost."""
    try:
        info = b._position_info()
        return sum(abs(float(p.get("positionAmt", 0) or 0))
                   * float(p.get("markPrice", 0) or 0) for p in info)
    except Exception:
        return 0.0


def _active_trail_stop_id(b) -> int:
    """The exchange STOP_MARKET orderId currently enforcing this bot's lock
    floor, or 0. The no-ghost reconciler must never cancel it as a ghost."""
    key = (b.user_id, getattr(b.bridge, "environment", "live"),
           getattr(b, "symbol", ""))
    with _trail_lock:
        st = _TRAIL_STOPS.get(key)
    return int(st.get("order_id", 0)) if st else 0


def _active_loss_stop_id(b) -> int:
    """The exchange STOP_MARKET orderId enforcing this bot's equity bound."""
    key = (b.user_id, getattr(b.bridge, "environment", "live"),
           getattr(b, "symbol", ""))
    with _trail_lock:
        st = _LOSS_STOPS.get(key)
    return int(st.get("order_id", 0)) if st else 0


def _exchange_stop_ids(b) -> set:
    ids = {_active_trail_stop_id(b), _active_loss_stop_id(b)}
    ids.discard(0)
    return ids


def _loss_stop_cancel(b):
    """Cancel the exchange loss stop for this bot (best effort)."""
    key = (b.user_id, getattr(b.bridge, "environment", "live"),
           getattr(b, "symbol", ""))
    with _trail_lock:
        st = _LOSS_STOPS.pop(key, None)
    if not st:
        return
    try:
        b.bridge._client.futures_cancel_order(
            symbol=b.bridge.binance_symbol, orderId=int(st["order_id"]))
    except Exception:
        pass


def _loss_stop_manage(b):
    """Keep a STOP_MARKET at the equity-threshold price for this bot's open
    position. Threshold price = entry ∓ (alloc x stop_pct + realized hole)/qty.
    closePosition=true: can only reduce, never open a reverse position;
    every flatten path (allOpenOrders DELETE) cancels it. Ratcheted on
    entry/qty/threshold changes — never cancel/place every sample.
    Silently ignore -4130 (already have GTE closePosition) to avoid spam."""
    if not LOSS_STOP_EXCHANGE:
        return
    if _bot_platform(b) == "mt5":
        return
    info = b._position_info()
    amt = sum(float(p.get("positionAmt", 0) or 0) for p in info)
    if abs(amt) < 1e-8:
        _loss_stop_cancel(b)
        return
    side_pos = 1 if amt > 0 else -1
    entry = sum(float(p.get("entryPrice", 0) or 0) * float(p.get("positionAmt", 0) or 0)
                for p in info) / amt
    stop_usd = abs(b._entry_alloc * b._stop_pct()) + max(0.0, -b.realized_pnl)
    per_q = stop_usd / abs(amt)
    px = entry - per_q if side_pos > 0 else entry + per_q
    try:
        px = b.bridge._round_price(px)
    except Exception:
        pass
    if px <= 0:
        return
    key = (b.user_id, getattr(b.bridge, "environment", "live"),
           getattr(b, "symbol", ""))
    with _trail_lock:
        st = _LOSS_STOPS.get(key)
    if st is not None:
        if (abs(st.get("px", 0.0) - px) <= max(px * 0.0005, 1e-6)
                and abs(st.get("qty", 0.0) - abs(amt)) <= 1e-6):
            return
        # already have a live stop with same qty — don't spam -4130
        if abs(st.get("qty", 0.0) - abs(amt)) <= 1e-6:
            return
    side = "SELL" if side_pos > 0 else "BUY"
    _loss_stop_cancel(b)
    try:
        o = b.bridge._client.futures_create_order(
            symbol=b.bridge.binance_symbol, side=side, type="STOP_MARKET",
            stopPrice=px, closePosition=True)
        oid = int(o.get("orderId", 0) or 0)
    except Exception as e:
        msg = str(e)
        if "-4130" in msg or "GTE and closePosition" in msg:
            # already have one — keep existing, don't spam
            return
        logger.warning(f"loss stop place failed {b.symbol}: {msg[:90]}")
        return
    if not oid:
        return
    with _trail_lock:
        _LOSS_STOPS[key] = {"order_id": oid, "px": px, "qty": abs(amt),
                            "side": side}
    logger.info(f"🛑 LOSS STOP {b.symbol}: {side} stop-market closePosition "
                f"@ {px} — equity bound ${stop_usd:.2f} "
                f"(-{b._stop_pct()*100:.0f}% alloc)")


def _trail_stop_cancel(b, key):
    """Cancel the exchange stop for this bot (best effort; a missing order
    or a just-triggered one fails silently)."""
    with _trail_lock:
        st = _TRAIL_STOPS.pop(key, None)
    if not st:
        return
    try:
        b.bridge._client.futures_cancel_order(
            symbol=b.bridge.binance_symbol, orderId=int(st["order_id"]))
    except Exception:
        pass


def _trail_stop_manage(b, key, floor):
    """Keep a STOP_MARKET (conditional) order on the exchange at the lock
    floor. The exchange enforces the floor even when price gaps through it
    between the 10s supervisor samples. Ratchet up when the floor grows
    (by >$0.05) or the position size changed — never cancel/place every
    sample. closePosition=true means the stop can only REDUCE: it can never
    open a reverse position, and it is cancelled by every flatten path
    (allOpenOrders DELETE covers conditional orders)."""
    if not TRAIL_STOP_EXCHANGE:
        return
    if _bot_platform(b) == "mt5":
        return
    info = b._position_info()
    amt = sum(float(p.get("positionAmt", 0) or 0) for p in info)
    if abs(amt) < 1e-8:
        _trail_stop_cancel(b, key)
        return
    side_pos = 1 if amt > 0 else -1
    entry = sum(float(p.get("entryPrice", 0) or 0) * float(p.get("positionAmt", 0) or 0)
                for p in info) / amt
    with _trail_lock:
        st = _TRAIL_STOPS.get(key)
    qty_changed = (st is None or abs(abs(amt) - st.get("qty", 0.0)) > 1e-6)
    floor_up = (st is None or floor > st.get("floor", 0.0) + 0.05)
    if st is not None and not qty_changed and not floor_up:
        return
    # price at which the remaining profit equals the floor
    per_q = floor / abs(amt)
    stop_px = entry + per_q if side_pos > 0 else entry - per_q
    try:
        stop_px = b.bridge._round_price(stop_px)
    except Exception:
        pass
    if stop_px <= 0:
        return
    side = "SELL" if side_pos > 0 else "BUY"
    _trail_stop_cancel(b, key)   # replace the old stop (no-op if none)
    try:
        o = b.bridge._client.futures_create_order(
            symbol=b.bridge.binance_symbol, side=side, type="STOP_MARKET",
            stopPrice=stop_px, closePosition=True)
        oid = int(o.get("orderId", 0) or 0)
    except Exception as e:
        logger.warning(f"trail stop place failed {b.symbol}: {str(e)[:90]}")
        return
    if not oid:
        return
    with _trail_lock:
        _TRAIL_STOPS[key] = {"order_id": oid, "floor": floor,
                             "side": side, "qty": abs(amt)}
    logger.info(f"⛔ TRAIL STOP {b.symbol}: {side} stop-market closePosition "
                f"@ {stop_px} — locks floor ${floor:.2f} (peak-based)")
    return (0.0, 0.0, 0, {})


def _sweep_account_flat(user_id: str, env: str) -> int:
    """Account-level residue sweep via the same python-binance Client the bots
    use: cancel EVERY open order and reduceOnly-close EVERY non-zero position
    until the book is flat. Returns residues cleaned.
    """
    from bridge import rest_client
    try:
        c = rest_client(user_id, env)
        if c is None:
            return 0
        cleaned = 0
        for _attempt in range(4):
            try:
                orders = c.futures_get_open_orders() or []
            except Exception as e:
                logger.warning(f"residue sweep orders fetch failed: {e}")
                return cleaned
            for sym in sorted({o["symbol"] for o in orders}):
                try:
                    c.futures_cancel_all_open_orders(symbol=sym)
                    cleaned += 1
                except Exception:
                    pass
            try:
                pos = c.futures_position_information() or []
            except Exception as e:
                logger.warning(f"residue sweep positions fetch failed: {e}")
                return cleaned
            open_pos = [p for p in pos
                        if abs(float(p.get("positionAmt", 0) or 0)) > 1e-8]
            for p in open_pos:
                amt = float(p["positionAmt"])
                try:
                    side = "BUY" if amt < 0 else "SELL"
                    c.futures_create_order(
                        symbol=p["symbol"], side=side, type="MARKET",
                        quantity=abs(amt), reduceOnly=True)
                    cleaned += 1
                except Exception:
                    pass
            if not orders and not open_pos:
                break
            time.sleep(1.0)
        return cleaned
    except Exception as e:
        logger.warning(f"residue sweep failed: {e}")
        return 0


def _bank_free_margin(user_id: str, env: str) -> float:
    """availableBalance via the same Client the bots use."""
    from bridge import rest_client
    try:
        c = rest_client(user_id, env)
        if c is None:
            return 0.0
        d = c.futures_account()
        return float(d.get("availableBalance", 0) or 0)
    except Exception as e:
        logger.warning(f"ProfitBank free-margin query failed: {e}")
        return 0.0


def _loss_restart_loop(managers: List["GuruAIManager"], cooldown: float):
    """After a collective loss cut: wait the cooldown (default 5m), sweep any
    residue, then relaunch both groups with a fresh scan from a clean book.
    Cancels itself if the user already restarted manually."""
    target = time.time() + cooldown
    while time.time() < target and not _bank_stop.is_set():
        _bank_stop.wait(10)
    if _bank_stop.is_set():
        return
    # user stopped everything / managers were unregistered meanwhile?
    with _bank_lock:
        registered = [m for m in _bank_managers if m is not None]
    if not registered or registered[0] is not managers[0]:
        logger.info("🧯 Loss-cut auto-restart skipped — managers unregistered")
        return
    bots = [b for m in managers for b in m._bots.values()
            if b.status == BotStatus.RUNNING]
    if bots:
        logger.info("🧯 Loss-cut auto-restart skipped — bots already running")
        return
    try:
        m0 = managers[0]
        free = _bank_free_margin(m0.user_id, m0.env)
        if free <= 0:
            free = _bank_free_margin(m0.user_id, m0.env)  # one retry on blips
        # complete reset: sweep any residue from the account (orphan orders /
        # positions the bots missed), then drop the stopped bots from the
        # UI/registry so the fresh hunt starts from a perfectly clean book.
        _sweep_account_flat(m0.user_id, m0.env)
        for m in managers:
            m.prune()
        exclude, started = set(), []
        for m in managers:
            m.prune()
            r = m.start(free_margin=free, exclude=tuple(exclude))
            if r.get("ok"):
                exclude.update(s["symbol"] for s in r["started"])
                started.extend(r["started"])
        if started:
            logger.info(f"✅ Loss-cut auto-restart launched "
                        f"{len(started)} grids: {', '.join(s['symbol'] for s in started)}")
        else:
            logger.warning("🧯 Loss-cut auto-restart found no qualifying coins")
    except Exception as e:
        logger.warning(f"🧯 Loss-cut auto-restart failed: {e}")
    finally:
        _bank_state["restart_at"] = 0.0


def _bank_loop():
    while not _bank_stop.wait(BANK_SCAN_SEC):
        try:
            _bank_tick()
        except Exception as e:
            logger.warning(f"ProfitBank tick failed: {e}")


def _bank_tick():
    global _bank_thread
    with _bank_lock:
        managers = [m for m in _bank_managers if m is not None]
    # market-activity gate first: may park or resume bots (it must run even
    # while everything is parked, so it lives here in the 10s supervisor)
    _market_gate_tick(managers)
    # strict calendar session lock second: parks out-of-session bots and
    # wakes allowed slots while the market gate is awake
    _session_lock_tick(managers)
    bots = []
    alive = []
    for m in managers:
        active = [b for b in m._bots.values() if b.status == BotStatus.RUNNING]
        if active:
            alive.append(m)
        bots.extend(active)
    if not alive:
        # loss-cut auto-restart pending? keep the thread alive for it.
        if _bank_state.get("restart_at", 0) > time.time():
            return
        # market gate parked everything? keep the supervisor alive so the
        # gate can auto-resume when activity returns.
        if MARKET_GATE_ENABLED and _market_gate_state.get("asleep", False):
            return
        # no running bots anywhere — retire the supervisors and stop the thread
        with _bank_lock:
            _bank_managers.clear()
            if _bank_thread:
                _bank_stop.set()
                if _bank_thread is not threading.current_thread():
                    _bank_thread.join(timeout=5)
                _bank_thread = None
        return
    if not bots:
        return
    pnl, wallet, n_pos, per_sym = _bank_account_pnl(bots)
    if wallet <= 0:
        return
    st = _bank_state
    st["last_pnl"] = pnl
    st["wallet"] = wallet
    st["n_pos"] = n_pos
    # ── per-bot trailing profit lock ──
    # Each bot arms when ITS unrealized reaches min($ARM_USD, ARM_PCT x
    # notional) — whichever is reached first — then the position is closed
    # the moment unrealized falls below (1-GIVEBACK) x peak. Runs BEFORE the
    # account-level arms so per-symbol wins are locked first.
    for b in bots:
        sym = getattr(b, "symbol", "")
        upnl = per_sym.get(sym, 0.0)
        key = (b.user_id, getattr(b.bridge, "environment", "live"), sym)
        notional = _bot_notional(b)
        if upnl <= 0.03 or notional <= 0:
            # flat: disarm + cancel any exchange stop left behind
            _trail_stop_cancel(b, key)
            with _trail_lock:
                _TRAIL_PEAKS.pop(key, None)
                _TRAIL_ARMED.discard(key)
                _TRAIL_PARTIAL.discard(key)
            continue
        with _trail_lock:
            peak = max(_TRAIL_PEAKS.get(key, 0.0), upnl)
            _TRAIL_PEAKS[key] = peak
        # 2026-09-02: notional-scaled arm with a low floor — small accounts
        # ($25 wallets, $26 legs) must be able to arm the trail too. Floor
        # $0.20 instead of the fixed $1.00 that never armed on small legs.
        arm_thr = min(BOT_TRAIL_ARM_USD,
                      BOT_TRAIL_ARM_PCT * notional) if notional > 0 else BOT_TRAIL_ARM_USD
        arm_thr = max(arm_thr, TRAIL_ARM_FLOOR_USD)
        if peak < arm_thr:
            _trail_stop_cancel(b, key)
            continue
        if key not in _TRAIL_ARMED:
            _TRAIL_ARMED.add(key)
            logger.info(f"🔒 TRAIL ARMED {sym}: upnl peak ${peak:.2f} "
                        f"(arm ${arm_thr:.2f} = min(${BOT_TRAIL_ARM_USD:.2f}, "
                        f"{BOT_TRAIL_ARM_PCT*100:.2f}% of ${notional:.0f})) — "
                        f"lock floor ${_trail_floor(peak):.2f}")
            # Partial scalp at ARM: take 50% now, trail the rest — captures grid TP
            # while leaving runner for +$10. Fixes "leave everything on table" missed op.
            if key not in _TRAIL_PARTIAL and peak >= arm_thr and upnl > 0.08:
                try:
                    # close 50% via market reduceOnly
                    info = b._position_info()
                    amt = sum(float(p.get("positionAmt", 0) or 0) for p in info)
                    if abs(amt) > 1e-8:
                        half = abs(amt) * 0.5
                        # round to step
                        half = b.bridge._round_qty(half) if hasattr(b.bridge, "_round_qty") else half
                        if half > 0:
                            side = "SELL" if amt > 0 else "BUY"
                            b.bridge._client.futures_create_order(symbol=b.bridge.binance_symbol, side=side, type="MARKET", quantity=half, reduceOnly=True)
                            _TRAIL_PARTIAL.add(key)
                            logger.info(f"💰 TRAIL SCALP {sym}: closed 50% {half} at ${peak:.2f} peak, trailing remainder")
                            # peak stays, floor will tighten on remainder
                except Exception as e:
                    logger.debug(f"partial scalp {sym} failed: {e}")
        if peak >= BOT_TRAIL_TIER2_USD:
            # big win: tight fixed trail under the running peak (never below
            # the tier-2 threshold itself)
            floor = max(BOT_TRAIL_TIER2_USD, peak - BOT_TRAIL_TIER2_TRAIL)
        else:
            floor = _trail_floor(peak)
        if floor <= 0 or upnl > floor:
            # fix B: keep a REAL STOP_MARKET on the exchange at the floor,
            # ratcheted up as the peak grows — the exchange closes through
            # price gaps the 10s sampling would miss.
            _trail_stop_manage(b, key, floor)
            continue
        # M5 direction hold: keep TP open for full run, close only when M5 flips
        # User approved: trail until 5m changes direction, broker TP flat 30% fat is via vol TP.
        try:
            bars5 = b._bars("5m", limit=3)
            g5 = 0.0
            if len(bars5) >= 2 and bars5[-2]["close"]:
                g5 = (bars5[-1]["close"] - bars5[-2]["close"]) / bars5[-2]["close"] * 100
            # position side
            _info2 = b._position_info()
            _amt2 = sum(float(p.get("positionAmt", 0) or 0) for p in _info2)
            _side = 1 if _amt2 > 0 else -1 if _amt2 < 0 else 0
            favorable = (_side > 0 and g5 > 0.10) or (_side < 0 and g5 < -0.10)
            # if profitable and still favorable on M5, hold — don't cut winner on floor
            if upnl > 0 and favorable and upnl > floor * 0.85:
                _trail_stop_manage(b, key, floor)
                # Re-center from peak if price extended beyond outer grid level (captures +10$ extension)
                try:
                    outer = b.core.center * (1 + b.core.spacing_pct * b.core.active_levels) if _side > 0 else b.core.center * (1 - b.core.spacing_pct * b.core.active_levels)
                    _price = b._price() or 0
                    if (_side > 0 and _price > outer) or (_side < 0 and _price < outer):
                        b._log(f"Extension beyond outer {outer:.2f} price {_price:.2f} → re-centering from peak for full run")
                        b.core.center = _price
                        b._place_grid()
                except Exception:
                    pass
                if int(time.time()) % 60 < 10:
                    logger.info(f"⏳ TRAIL HOLD {sym}: upnl ${upnl:.2f} floor ${floor:.2f} peak ${peak:.2f} but M5 g5 {g5:+.2f}% still favorable side {_side:+.0f} — holding for full run")
                continue
        except Exception:
            pass
        try:
            b.bridge.close_all()   # cancels the exchange stop too (allOpenOrders)
        except Exception as e:
            logger.warning(f"trail lock close {sym} failed: {e}")
            continue
        st["trail_locks"] += 1
        per_sym[sym] = 0.0   # just-closed: keep the account arms from double-counting
        with _trail_lock:
            _TRAIL_PEAKS.pop(key, None)
            _TRAIL_ARMED.discard(key)
            _TRAIL_PARTIAL.discard(key)
            _TRAIL_STOPS.pop(key, None)
        logger.info(f"🔒 TRAIL LOCK {sym}: ${upnl:+.2f} <= floor ${floor:.2f} "
                    f"(peak ${peak:.2f}) — closed to bank the remainder; "
                    f"bot re-ladders flat")
    st["peak"] = max(st["peak"], pnl)
    # position-scaled thresholds: more legs open -> bank sooner
    def _scale(per_pos, floor, cap):
        return max(min(max(n_pos * per_pos, floor), cap), floor)
    target_pct = _scale(BANK_PCT_PER_POS, BANK_PCT_FLOOR, BANK_PCT_CAP)
    trail_pct = _scale(BANK_TRAIL_PER_POS, BANK_TRAIL_FLOOR, BANK_TRAIL_CAP)
    target = wallet * target_pct
    st["target_pct"] = target_pct
    banked = pnl >= target
    if not banked and st["peak"] >= wallet * trail_pct \
            and pnl <= st["peak"] * (1.0 - BANK_TRAIL_GIVEBACK):
        banked = True
    if banked:
        # Bank ONLY the winning bots' positions — a healthy losing leg keeps
        # its grid and plays out; blanket close_all() on every bot was the
        # high-frequency close churn (market-close + re-ladder on ALL bots
        # every bank event). Bots with positive per-symbol unrealized are
        # closed; the rest stay untouched.
        winners = [b for b in bots
                   if per_sym.get(getattr(b, "symbol", ""), 0.0) >= BANK_MIN_POS_PNL]
        closed = sum(b.bridge.close_all() or 0 for b in winners) if winners else 0
        if winners:
            st["banks"] += 1
            st["peak"] = 0.0
            names = ", ".join(getattr(b, "symbol", "?") for b in winners)
            logger.info(f"💰 ProfitBank BANKED ${pnl:.2f} (target ${target:.2f} = "
                        f"{target_pct*100:.2f}% of wallet ${wallet:.2f}, {n_pos} open "
                        f"pos) — closed {closed} winning position(s) on {names}; "
                        f"losers keep their grids")
        else:
            if time.time() - st.get("_skip_log_ts", 0.0) > 300:
                st["_skip_log_ts"] = time.time()
                logger.info(f"💰 ProfitBank target hit (${pnl:.2f} ≥ ${target:.2f}) but "
                            f"no per-symbol winner ≥ ${BANK_MIN_POS_PNL:.2f} — skip close "
                            f"(no churn)")
        return
    # ── per-bot loss stops (INDIVIDUAL) ──
    # A negative check must be per-bot: only the bleeding bot is stopped and
    # flattened; healthy bots keep their grids. The old collective -2% cut
    # market-closed the whole book (including winners) — that was the
    # high-frequency close churn the user saw in Binance.
    stopped_any = False
    for m in managers:
        for b in [x for x in m._bots.values() if x.status == BotStatus.RUNNING]:
            upnl = per_sym.get(getattr(b, "symbol", ""), 0.0)
            bot_total = b.realized_pnl + upnl
            stop_pct = b._stop_pct()
            if bot_total <= -abs(b._entry_alloc * stop_pct):
                _loss_stop_cancel(b)
                _SYM_COOLDOWN[(b.user_id, m.env, getattr(b, "symbol", ""))] = time.time()
                try:
                    b.stop(close_positions=True)
                except Exception as e:
                    logger.warning(f"bot loss stop {getattr(b, 'symbol', '?')} failed: {e}")
                    continue
                st["loss_cuts"] += 1
                stopped_any = True
                logger.info(f"🧯 BOT LOSS STOP {getattr(b, 'symbol', '?')} ${bot_total:+.2f} "
                            f"(<= -{stop_pct*100:.0f}% alloc ${b._entry_alloc:.2f}) — "
                            f"closed ONLY this bot; replacement in next rotation cycle")
            else:
                # loss-side exchange stop (2026-08-18): keep a STOP_MARKET at
                # the equity-threshold price so a fast gap (ACE -6% bar) closes
                # at the bound instead of overshooting it (-$4.25 vs -$1.75).
                _loss_stop_manage(b)
    if stopped_any:
        alive = [b for m in managers for b in m._bots.values()
                 if b.status == BotStatus.RUNNING]
        if not alive:
            st["restart_at"] = time.time() + BANK_LOSS_COOLDOWN
            threading.Thread(target=_loss_restart_loop,
                             args=(managers, BANK_LOSS_COOLDOWN),
                             daemon=True, name="loss-restart").start()
            logger.info("🧯 all bots individually stopped — full relaunch in "
                        f"{BANK_LOSS_COOLDOWN/60:.0f}m")

    # ── emergency flood cap (whole-book crash only) ──
    # The individual stops handle everyday bad days. This deep collective
    # floor (-10% of wallet) exists ONLY for a market-wide crash: it can
    # never be reached by a single bot (max ~-$2-3), so firing it means the
    # whole book is underwater. Full sweep + 5-min relaunch, same machinery
    # as the old cut.
    emergency_at = -(wallet * BANK_EMERGENCY_PCT)
    if pnl <= emergency_at:
        st["loss_cuts"] += 1
        st["peak"] = 0.0
        st["restart_at"] = time.time() + BANK_LOSS_COOLDOWN
        mgrs = managers
        for m in mgrs:
            m.stop_all()
        uid = mgrs[0].user_id if mgrs else None
        env = mgrs[0].env if mgrs else "live"
        cleaned = 0
        if uid:
            cleaned = _sweep_account_flat(uid, env)
            for m in mgrs:
                m.prune()
        logger.info(f"🌊 EMERGENCY FLOOD STOP ${pnl:.2f} (<= ${emergency_at:.2f}, "
                    f"-{BANK_EMERGENCY_PCT*100:.0f}% of wallet ${wallet:.2f}) — whole book "
                    f"stopped, swept {cleaned} residue(s); auto-restart in "
                    f"{BANK_LOSS_COOLDOWN/60:.0f}m")
        threading.Thread(target=_loss_restart_loop, args=(mgrs, BANK_LOSS_COOLDOWN),
                         daemon=True, name="loss-restart").start()


# ── Scanner ──────────────────────────────────────────────────────────────────

def _http(url: str, timeout: int = 20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


# Cached scan pool: the ticker + klines probes are the expensive part, and the
# rotation supervisor re-reads it every cycle — share one pool within the TTL.
_SCAN_CACHE: dict = {"at": 0.0, "max_price": None, "pool": [], "probe_ok": False}
_SCAN_TTL = 120.0

# Cross-group symbol registry: (user_id, env) -> set of symbols currently
# traded by ANY guru group. The rotation supervisor must not rotate into a
# symbol the OTHER group already holds (double exposure = double the bleed,
# seen live when ALL rotated into ACE/AKE already held by MEME).
_GURU_IN_USE: dict = {}
_GURU_IN_USE_LOCK = threading.Lock()

# Per-symbol stop cooldown: (user_id, env, sym) -> epoch. After an individual
# bot loss-stop, the symbol must not be re-picked immediately (it's still
# bleeding). The rotation supervisor checks this before replacing the slot.
_SYM_COOLDOWN: dict = {}
_SYM_COOLDOWN_LOCK = threading.Lock()
SYM_COOLDOWN_SEC = float(os.getenv("GURU_SYM_COOLDOWN_SEC", "2700"))  # 45 min


def _guru_claim(user_id: str, env: str, sym: str):
    with _GURU_IN_USE_LOCK:
        _GURU_IN_USE.setdefault((user_id, env), set()).add(sym)


def _guru_release(user_id: str, env: str, sym: str):
    with _GURU_IN_USE_LOCK:
        s = _GURU_IN_USE.get((user_id, env))
        if s:
            s.discard(sym)


def _guru_in_use(user_id: str, env: str) -> set:
    with _GURU_IN_USE_LOCK:
        return set(_GURU_IN_USE.get((user_id, env), ()))


def _fetch_scan_pool(max_price: float = None) -> List[dict]:
    """Ranked, scored candidate pool (no exclusion/priority applied).

    Cached for _SCAN_TTL seconds so the two groups + the rotation loop all
    share one Binance scan (rate-limit friendly). max_price 0 = any price.
    """
    mprice = max_price if max_price is not None else MAX_PRICE
    now = time.time()
    if _SCAN_CACHE["at"] + _SCAN_TTL > now and _SCAN_CACHE["max_price"] == mprice:
        return _SCAN_CACHE["pool"]
    try:
        tk = _http("https://fapi.binance.com/fapi/v1/ticker/24hr")
    except Exception as e:
        logger.error(f"GuruAI scan failed: {e}")
        return []
    cands = []
    for t in tk:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        if UNIVERSE_SYMBOLS and sym not in UNIVERSE_SYMBOLS:
            continue
        if sym in BLACKLIST_SYMBOLS:
            continue
        try:
            price = float(t["lastPrice"])
            gain = float(t["priceChangePercent"])
            qv = float(t["quoteVolume"])
        except (ValueError, KeyError):
            continue
        if price <= 0 or (mprice > 0 and price > mprice):
            continue
        if qv < MIN_QUOTE_VOL:
            continue
        cands.append({"symbol": sym, "price": price, "gain_pct": gain, "quote_vol": qv})

    # Velocity probe: for the universe candidates, measure recent 15m/1h/4h
    # movement. 15m is the shortest Binance futures kline interval close to
    # "the last few minutes" (10m is NOT a valid fapi interval — using it
    # silently 400s and zeroes the whole freshness score, which is exactly
    # how AKE-style 24h gainers kept winning). The 15m window dominates so
    # selection tracks what is moving RIGHT NOW, with 1h/4h persistence.
    # No 24h term: yesterday's pump must not outrank a fresh mover.
    pool = sorted(cands, key=lambda c: c["quote_vol"], reverse=True)[:30]
    probe_ok = 0
    for c in pool:
        for iv, key in (("15m", "gain_15m"), ("1h", "gain_1h"), ("4h", "gain_4h")):
            try:
                k = _http(f"https://fapi.binance.com/fapi/v1/klines?symbol={c['symbol']}"
                          f"&interval={iv}&limit=2")
                if len(k) >= 2:
                    c[key] = (float(k[-1][4]) - float(k[-2][4])) / float(k[-2][4]) * 100.0
                    if key == "gain_15m":
                        probe_ok += 1
            except Exception:
                c.setdefault(key, 0.0)
    for c in pool:
        g15 = c.get("gain_15m", 0.0); g1 = c.get("gain_1h", 0.0); g4 = c.get("gain_4h", 0.0)
        # freshness-weighted velocity: 15m momentum dominates (0.5), then 1h
        # (0.3), 4h (0.2). 24h gain is deliberately NOT scored — recent
        # movement is what matters, yesterday's pump is irrelevant.
        c["score"] = 0.5 * g15 + 0.3 * g1 + 0.2 * g4
    pool.sort(key=lambda c: c["score"], reverse=True)
    _SCAN_CACHE["at"] = time.time()
    _SCAN_CACHE["max_price"] = mprice
    _SCAN_CACHE["pool"] = pool
    _SCAN_CACHE["probe_ok"] = probe_ok >= max(1, len(pool) // 2) if pool else False
    return pool


def scan_top_coins(limit: int = 3, max_price: float = None,
                   exclude: tuple = (), priority: tuple = (),
                   priority_min_score: float = 0.0) -> List[dict]:
    """Top volatile USDT-M futures coins by movement velocity.

    15m-dominant freshness score (0.5x15m + 0.3x1h + 0.2x4h — no 24h term),
    utility-universe + liquidity-filtered. `max_price` 0 = any price;
    `exclude` skips symbols already picked by another group. Priority symbols
    (e.g. BTCUSDT/XAUUSDT) join the ranking like everyone else BUT are dropped
    from contention when ranked last in the scanned top list — they must earn
    their slot by being strictly better than the worst candidate.

    Velocity-probe fallback: if the fresh 15m probe failed for most symbols
    (Binance rate-limit / outage), this returns [] — the manager then runs
    ONLY its fixed anchors (XAUUSDT/BTCUSDT) until the probe recovers.

    Returns {symbol, price, gain_pct, quote_vol, gain_15m, gain_1h, gain_4h,
    score} best-first.
    """
    ranked = _fetch_scan_pool(max_price)
    if not ranked or not _SCAN_CACHE.get("probe_ok", False):
        logger.warning("GuruAI: velocity probe failed/empty — scan yields no "
                       "mover picks (anchors-only fallback)")
        return []
    top = ranked[:max(limit + 2, 6)]           # the "scanned top list"
    # not-last guard for priority symbols: a priority symbol ranked worst in
    # the scanned list loses its slot to the best mover.
    if priority and len(top) >= 2 and top[-1]["symbol"] in priority:
        top = [c for c in top if c["symbol"] != top[-1]["symbol"]]
    # rank-based fill with a small priority boost: BTC/XAU win near-ties and
    # displace slightly-worse candidates, but a better-ranked mover always
    # beats them (boost is far smaller than real score gaps).
    def _key(c):
        return c["score"] + (0.1 if c["symbol"] in priority else 0.0)
    top = sorted(top, key=_key, reverse=True)
    picks, used = [], set()
    for c in top:
        if len(picks) >= limit:
            break
        if exclude and c["symbol"] in exclude:
            continue
        if priority and c["symbol"] in priority and abs(c["score"]) < priority_min_score:
            continue
        if c["symbol"] not in used:
            picks.append(c); used.add(c["symbol"])
    logger.info(f"GuruAI scan: {[(p['symbol'], round(p['score'], 1), round(p['gain_pct'], 1)) for p in picks]}")
    return picks


# ── Neutral Grid Bot ─────────────────────────────────────────────────────────

class GuruAIBot:
    """One neutral two-sided grid on a volatile micro-cap. Duck-type compatible
    with HermesSubBot so the orchestrator + dashboard render it uniformly."""

    def __init__(self, name: str, symbol: str, bridge: BinanceBridge, alloc_usd: float,
                 free_margin: float = 0.0, leverage: float = 10.0, capacity_usd: float = 0.0,
                 user_id: str = None, is_anchor: bool = False, vol_pct: int = 50,
                 mode: str = None, compound: bool = None, compound_frac: float = None):
        self.name = name
        self.symbol = symbol
        self.bridge = bridge
        self.user_id = user_id            # owning Supabase user (multi-tenant isolation)
        self.is_anchor = bool(is_anchor)  # fixed symbol (XAUUSDT/BTCUSDT): wider equity stop,
                                          # no dynamic initial leg, longer restart cooldown
        self.alloc_usd = alloc_usd
        self.free_margin = float(free_margin) or 0.0
        self.leverage = float(leverage) or 10.0
        # this bot's share of total notional capacity (free x leverage x safety / n bots)
        self.capacity_usd = float(capacity_usd) if capacity_usd > 0 else self.free_margin * self.leverage * 0.7
        self.vol_pct = max(10, min(100, int(vol_pct or 50)))  # global slider 10-100% of available funds for this grid
        self._compound_cfg = compound
        self._compound_frac_cfg = compound_frac
        # shared neutral-grid strategy core (ATR spacing, ADX regime, scale-in).
        # mode scalp/swing selects the preset bundle (levels, spacing, TP mult,
        # equity stop, max hold). "auto" resolves per-symbol by ADX regime
        # (scalp<25 else swing). Default scalp = current behavior, unchanged.
        _mode = (mode or os.getenv("GB_GRID_MODE", "scalp") or "scalp").strip().lower()
        if _mode == "auto":
            _mode = resolve_grid_mode(
                "auto", symbol,
                user_id, getattr(bridge, "environment", "demo"))
            logger.info(f"🤖 {symbol} auto-mode → {_mode} (ADX regime)")
        self.grid_mode = _mode if _mode in ("scalp", "swing") else "scalp"
        self.core = NeutralGridCore(GridTuning(mode=self.grid_mode))
        # levels per side, capacity-reduced in _size_qty
        self.grid_levels = self.core.grid_levels

        self.status = BotStatus.IDLE
        self.positions: List[dict] = []
        self.logs: List[str] = []

        self._thread: Optional[threading.Thread] = None
        self._shutdown = threading.Event()
        self._paused = threading.Event()

        self.qty: float = 0.0
        self.grid_orders: Dict[int, dict] = {}   # order_id -> {side, price, qty}
        # Per-level pending leg: level -> "BUY" | "SELL" | missing. After BUY_i
        # fills, BUY_i is NOT re-stocked until SELL_i (its exit) fills — the
        # slot model that bounds position growth to 1 leg per level (max
        # levels x qty per side, ever). Mirrors orchestrator.GridSubBot.
        self._pair_pending: Dict[int, str] = {}
        self.realized_pnl: float = 0.0
        self.cycles: int = 0
        self._last_reset_date: str = ""
        self._entry_alloc: float = alloc_usd
        self.bot_id: str = ""
        self._pos_opened_at: float = 0.0   # when the current position first appeared
        self._pos_ref_price: float = 0.0   # price when the current position first appeared (crash guard)
        self._bars_cache: Dict[str, tuple] = {}  # (interval, limit) -> (ts, bars)
        self._last_session: str = ""       # session name at last re-quote (hysteresis)
        self._pos_info_cache: tuple = (0.0, None)  # (ts, positions) shared by equity/max-hold checks
        self._sym_info_refresh_at: float = time.time()  # periodic PRICE_FILTER refresh
        self._stale_since: float = 0.0        # stale-ladder proximity timer
        self._guard_block_scale: bool = False
        self._last_guard_state: str = "SAFE"
        self._guard_no_entries: bool = False  # v1.0 production governor: entries only
        self._guard_leg_ts: Dict[int, float] = {}  # level -> leg-open ts (TP-arm/min-hold)
        self._prop_day: str = ""          # prop-only daily/session discipline state
        self._prop_day_base: float = 0.0
        self._prop_day_out: bool = False
        self._prop_sess: str = ""
        self._prop_sess_base: float = 0.0
        self._prop_sess_out: bool = False

    def _stop_pct(self) -> float:
        """Equity-stop threshold: anchors get the wider stop (their notional is
        ~40x alloc, so -10% alloc = noise); movers keep the default."""
        return EQUITY_STOP_ANCHOR_PCT if self.is_anchor else self.core.t.equity_stop_pct

    # ── logging ──
    def _compound_on(self) -> bool:
        """Compound sizing switch. Per-spawn override wins; else GB_COMPOUND_ON
        (default 1 = ON)."""
        if self._compound_cfg is not None:
            return bool(self._compound_cfg)
        return (os.getenv("GB_COMPOUND_ON", "1") or "1").strip() not in ("", "0")

    def _compound_frac(self) -> float:
        if self._compound_frac_cfg is not None:
            try:
                return max(0.001, min(0.25, float(self._compound_frac_cfg)))
            except (TypeError, ValueError):
                pass
        try:
            return max(0.001, min(0.25, float(os.getenv("GB_COMPOUND_FRAC", "0.02") or 0.02)))
        except (TypeError, ValueError):
            return 0.02

    def _wallet_equity(self) -> float:
        """Live wallet equity for compound sizing (falls back to spawn-time free)."""
        try:
            acct = self.bridge.get_account_info() or {} if self.bridge else {}
            eq = float(acct.get("equity") or acct.get("balance") or 0)
            if eq > 0:
                return eq
        except Exception:
            pass
        return float(self.free_margin or 0.0)

    def _log(self, msg: str):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.logs.append(f"[{ts}] {msg}")
        if len(self.logs) > 60:
            self.logs = self.logs[-40:]
        logger.info(f"[{self.name}] {msg}")

    def rotation_safe(self) -> bool:
        """True when rotating costs nothing: exchange is FLAT and the local
        ladder is still level-1. Exchange qty is the truth — never rotate
        off a live Binance position."""
        if self.core.active_levels > 1:
            return False
        fn = getattr(self.bridge, "exchange_position_qty", None)
        if callable(fn):
            try:
                return abs(float(fn() or 0)) < 1e-8
            except Exception:
                return False
        return True

    def _halt_cooldown(self):
        """Mark this symbol as untradeable for SYM_COOLDOWN_SEC after a halt
        (equity stop / crash guard), so the dead-slot replacement picks a
        FRESH symbol instead of re-entering the same bleeding coin."""
        try:
            env = getattr(self.bridge, "environment", "live")
            with _SYM_COOLDOWN_LOCK:
                _SYM_COOLDOWN[(self.user_id, env, self.symbol)] = time.time()
        except Exception:
            pass

    # ── lifecycle ──
    def start(self):
        if self.status == BotStatus.RUNNING:
            return
        if not self.bridge or not self.bridge.is_connected:
            self._log("❌ bridge offline")
            return
        self._size_qty()
        self.status = BotStatus.RUNNING
        self._shutdown.clear()
        self._paused.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"guru-{self.symbol}")
        self._thread.start()
        self._log(f"🟢 GuruAI grid started | qty={self.qty} alloc=${self.alloc_usd:.2f}")

    def pause(self):
        self._paused.set(); self.status = BotStatus.PAUSED; self._log("⏸ paused")

    def resume(self):
        self._paused.clear(); self.status = BotStatus.RUNNING; self._log("▶ resumed")

    def stop(self, close_positions: bool = True):
        self._shutdown.set()
        self.status = BotStatus.STOPPED
        if self._thread:
            # wait for the tick loop to exit BEFORE flattening, so a tick
            # mid-reconcile can't place orders after the flatten swept them
            self._thread.join(timeout=10)
        if close_positions:
            self._flatten()
        self._log(f"🛑 stopped | realized ${self.realized_pnl:+.2f} cycles={self.cycles}")

    def is_alive(self) -> bool:
        return (self.status == BotStatus.RUNNING and self._thread is not None
                and self._thread.is_alive())

    # ── sizing ──
    def _size_qty(self):
        si = getattr(self.bridge, "_symbol_info", {}) or {}
        plat = str(getattr(self.bridge, "platform", "") or "").lower()
        min_notional = float(si.get("min_notional", 5.0)) or 5.0
        # Low-budget micro-caps: 5$ notional → 1% = $0.05 useless, 6% → $0.30 but got $0.06.
        # User: PROM 6% only $0.06 should be $0.60. Bump floor: <$1 →15$, $1-10→20$ so 1% = $0.15-0.20.
        price_tmp = self._price() or 1.0
        if price_tmp < 1.0:
            min_notional = max(min_notional, 15.0)
        elif price_tmp < 10:
            min_notional = max(min_notional, 20.0)
        step_default = 1.0
        step = float(si.get("qty_step") or si.get("min_qty") or step_default) or step_default
        price = price_tmp
        try:
            atr_raw = atr_pct(self._bars("15m"), self.core.t.atr_period)
        except Exception:
            atr_raw = 0.0
        sym = (self.symbol or "").upper()
        asset = (getattr(self.bridge, "asset_symbol", None) or sym).upper()
        # Optional per-symbol fixed qty (GB_QTY_<SYMBOL>): explicit override —
        # bypasses the per-bot share shrink, hard-clamped at half the account's
        # buying power (same guard as the orchestrator grids).
        # "auto" = dynamic wallet-scaled qty (2026-08-18): qty = free_margin /
        # GB_QTY_DYN_DIV (default 2500) — grows with the account instead of
        # needing manual re-tuning (XAU at $100 wallet / 2500 = 0.04 XAU).
        fixed_qty = os.getenv(f"GB_QTY_{sym}", "") or os.getenv(f"GB_QTY_{asset}", "")
        if getattr(self, "_force_alloc_size", False):
            fixed_qty = ""   # Spawn: forced allocation sizing, ignore GB_QTY_*
        if fixed_qty:
            if fixed_qty.strip().lower() == "auto":
                div = float(os.getenv("GB_QTY_DYN_DIV", "2500") or 2500)
                dyn_qty = (self.free_margin or 0.0) / div if div > 0 else 0.0
                dyn_qty = max((int(dyn_qty / step + 0.9999)) * step, step)
                per_level_usd = dyn_qty * price
                hard_cap = self.free_margin * self.leverage / 2.0
                if hard_cap > 0 and per_level_usd > hard_cap:
                    qty_cap = max((int((hard_cap / price) / step)) * step, step)
                    per_level_usd = qty_cap * price
                    self._log(f"⚠️ {sym} auto qty clamped to {qty_cap:.3f} "
                              f"(capacity {hard_cap:.0f})")
                else:
                    self._log(f"📐 {sym} auto qty={dyn_qty:.3f} "
                              f"(wallet ${self.free_margin:.0f}/{div:.0f} → "
                              f"${per_level_usd:.0f}/level)")
            else:
                per_level_usd = float(fixed_qty) * price
                hard_cap = self.free_margin * self.leverage / 2.0
                if hard_cap > 0 and per_level_usd > hard_cap:
                    per_level_usd = hard_cap
                    self._log(f"⚠️ {sym} sized by capacity clamp "
                              f"${per_level_usd:.0f}/level (max {hard_cap:.0f})")
        else:
            # per-level notional: a slice of allocation, leveraged, but always >= min_notional.
            # GB_LEVEL_USD (if set) overrides this with a fixed per-level notional
            # (capacity-guarded by level_usd); auto mode is unchanged from before.
            fixed_level = float(os.getenv("GB_LEVEL_USD", "0") or 0)
            if fixed_level > 0:
                per_level_usd = level_usd(min_notional, self.free_margin)
            elif self._compound_on():
                # COMPOUND (default ON): per-leg = frac of LIVE wallet equity, so
                # legs grow/shrink with the account. GB_COMPOUND_FRAC=0.02 default.
                per_level_usd = max(min_notional * 1.05,
                                    self._wallet_equity() * self._compound_frac())
                self._log(f"📐 {sym} compound {self._compound_frac()*100:.1f}% "
                          f"→ ${per_level_usd:.2f}/level")
            else:
                per_level_usd = max(min_notional * 1.05, (self.alloc_usd * 5) / (GRID_LEVELS * 2))
        # EMA30 5m zone sizing — Binance: keep full size for 2× lot target (0.5$ per 1% on 25$)
        # Original Aihansu zones halved to 0.5× when expensive, causing 0.013 vs 0.026 half-size on 51$.
        # For Binance live (compounding, 2× requested) we keep 1.0× in expensive zone to double lot,
        # only skip on crash >5% or <‑5%; no halving so profit scales linear 25→250.
        plat_ema = str(getattr(self.bridge, "platform", "") or "").lower()
        if plat_ema != "binance":
            # Non-Binance (MT5) keeps original conservative zones
            try:
                ema_bars = self._bars("5m", limit=30)
                if ema_bars and len(ema_bars) >= 30:
                    closes = [float(b["close"]) for b in ema_bars]
                    _ema = sum(closes[:30]) / 30
                    _k = 2 / (30 + 1)
                    for _c in closes[30:]:
                        _ema = _c * _k + _ema * (1 - _k)
                    if _ema > 0:
                        _dist = (price - _ema) / _ema * 100
                        _mult = 1.0
                        if _dist > 5: _mult = 0.0
                        elif _dist > 0: _mult = 0.5
                        elif _dist > -1.5: _mult = 0.75
                        elif _dist > -3: _mult = 1.0
                        elif _dist > -5: _mult = 1.5
                        else: _mult = 0.0
                        if _mult != 1.0:
                            _old = per_level_usd
                            per_level_usd = per_level_usd * _mult
                            if _mult == 0.0:
                                self._log(f"EMA30 {_dist:+.2f}% → skip buy (expensive/crash zone)")
                                self.qty = 0.0
                                return
                            else:
                                self._log(f"EMA30 {_dist:+.2f}% → qty mult {_mult}x per_level ${_old:.0f}→${per_level_usd:.0f}")
            except Exception:
                pass
        else:
            # Binance: keep EMA zones (user: keep ema) — same halving for risk
            try:
                ema_bars = self._bars("5m", limit=30)
                if ema_bars and len(ema_bars) >= 30:
                    closes = [float(b["close"]) for b in ema_bars]
                    _ema = sum(closes[:30]) / 30
                    _k = 2 / (30 + 1)
                    for _c in closes[30:]:
                        _ema = _c * _k + _ema * (1 - _k)
                    if _ema > 0:
                        _dist = (price - _ema) / _ema * 100
                        _mult = 1.0
                        if _dist > 5: _mult = 0.0
                        elif _dist > 0: _mult = 0.5
                        elif _dist > -1.5: _mult = 0.75
                        elif _dist > -3: _mult = 1.0
                        elif _dist > -5: _mult = 1.5
                        else: _mult = 0.0
                        if _mult != 1.0:
                            _old = per_level_usd
                            per_level_usd = per_level_usd * _mult
                            if _mult == 0.0:
                                self._log(f"EMA30 {_dist:+.2f}% → skip buy (expensive/crash zone)")
                                self.qty = 0.0
                                return
                            else:
                                self._log(f"EMA30 {_dist:+.2f}% → qty mult {_mult}x per_level ${_old:.0f}→${per_level_usd:.0f}")
            except Exception:
                pass
        # Global volume slider 10-100% — scales per_level linear, 50% → 0.5$ per 1% on 25$ as requested, 100% → 1.0$ per 1%
        try:
            _vol = max(10, min(100, int(getattr(self, 'vol_pct', 50) or 50)))
            if _vol != 50:
                _old = per_level_usd
                per_level_usd = per_level_usd * _vol / 50.0
                self._log(f"Vol {_vol}% → per_level ${_old:.0f}→${per_level_usd:.0f}")
        except Exception:
            pass
        # Risk-based per-leg notional cap (2026-09-01): leg ≤ wallet notional.
        # Before: div-based sizing gave XAU $196/leg and BTC $3466/leg(!) —
        # 6 legs = $118 margin > wallet → equity stop fired at 0.24%/0.01% price
        # = the REAL tiny SL. Uniform ~wallet per-leg keeps SL 1.5% reachable.
        notional_cap = (self.free_margin or 0.0) * 1.0
        if notional_cap > 0 and per_level_usd > notional_cap:
            per_level_usd = notional_cap
            self._log(f"⚠️ {sym} per-leg notional clamped to ${notional_cap:.0f} "
                      f"(risk cap = wallet; was ${per_level_usd:.0f})")
        # Optional per-symbol level count (GB_LEVELS_<SYMBOL>), else the
        # capacity-driven grid_levels from above.
        sym_levels = os.getenv(f"GB_LEVELS_{sym}", "")
        if sym_levels:
            self.grid_levels = max(int(sym_levels), 1)
        # margin capacity: this bot's share of available free margin x leverage x safety,
        # split across orders. if it can't cover a full grid, shrink the per-level size
        # (never below min_notional) and reduce the number of levels so it can be placed.
        # NOTE: explicit GB_QTY_<SYMBOL> sizes bypass this per-bot share (the hard cap
        # applied above — half the account's buying power — is their only limit).
        capacity = self.capacity_usd
        orders = self.grid_levels * 2
        if capacity > 0 and orders > 0 and not fixed_qty:
            capacity_per = capacity / orders
            if capacity_per < per_level_usd:
                per_level_usd = max(capacity_per, min_notional * 1.05)
                levels = int(capacity / (per_level_usd * 2))
                self.grid_levels = max(min(levels, self.grid_levels), 1)
        if per_level_usd * 2 * self.grid_levels > capacity:
            self._log(f"⚠️ margin insufficient: grid needs ~${per_level_usd*2*self.grid_levels:.2f} "
                      f"notional but margin capacity is ${capacity:.2f} (free=${self.free_margin:.2f} "
                      f"x lev={self.leverage})")
        qty = per_level_usd / price
        # round UP to step
        qty = (int(qty / step + 0.9999)) * step
        self.qty = max(qty, step)
        min_q = float(si.get("min_qty") or step) or step
        if self.qty < min_q:
            self.qty = min_q
        self.core.grid_levels = self.grid_levels
        atr_raw = atr_pct(self._bars("15m"), self.core.t.atr_period)
        self.core.compute_spacing(atr_raw, self._log)
        ss = self._session_spacing(price, atr_raw)
        # Same 20% hysteresis as the tick path — on MT5 the EA pushes fresh
        # bars every POST, so ATR micro-wiggles would otherwise re-log and
        # micro-drift the spacing every ~30s.
        if ss:
            sess_changed = ss[0] != self._last_session
            rel = (abs(ss[1] - self.core.spacing_pct) / self.core.spacing_pct
                   if self.core.spacing_pct > 0 else 1.0)
            if sess_changed or rel > SESSION_REQUOTE_HYSTERESIS:
                self._last_session = ss[0]
                self.core.spacing_pct = ss[1]
                self._log(f"🗓 {ss[0]} session → spacing {ss[1] * 100:.3f}% "
                          f"(${ss[1] * price:.0f}/side)")
        self._log(f"sized qty={self.qty} (~${self.qty*price:.2f}/level, min_notional=${min_notional}, "
                  f"levels/side={self.grid_levels}, spacing={self.core.spacing_pct*100:.2f}%, "
                  f"free_margin=${self.free_margin:.2f})")

    def _session_spacing(self, price: float, atr_raw: float = 0.0):
        """Session-mapped grid distance → (session, spacing_pct) or None.

        Mirrors orchestrator.GridSubBot._session_spacing: symbol-specific USD
        distance (GB_SPACING_<SYM>_<SESSION>_USD) wins; otherwise the session %
        (GB_SPACING_<SESSION>_PCT) applies. The raw ATR spacing is a safety
        valve — the ladder widens above the session target during volatility
        spikes, and is never allowed below the fee floor."""
        if os.getenv("GB_SESSION_SPACING", "1") != "1":
            return None
        sess = _session_name(datetime.now(timezone.utc).hour)
        key = (getattr(self.bridge, "binance_symbol", "") or self.symbol).replace("USDT", "")
        usd = _env_f(f"GB_SPACING_{key}_{sess}_USD",
                     _SESSION_USD_DEFAULTS.get(key, {}).get(sess, 0.0))
        pct = _env_f(f"GB_SPACING_{sess}_PCT", _SESSION_PCT_DEFAULTS[sess])
        target = usd / price if usd > 0 else pct
        # AI primary for Binance: LLM may tune spacing via spacing_mult (0.7-1.8) with fallback 1.0
        mult = float(getattr(self, "_spacing_mult", 1.0) or 1.0)
        target = target * mult
        atr_sp = atr_raw * self.core.t.spacing_atr_mult
        sp = max(target, atr_sp) if atr_raw > 0 else target
        sp = min(max(sp, _SPACING_FEE_FLOOR), _SPACING_CEIL)
        return sess, sp

    def _range_open(self) -> bool:
        """True only in a ranging, liquid market (the grid's "right market time")."""
        try:
            return (self.core.regime_ok(self._adx_value(), self._log)
                    and self.core.participation_ok(self._part_volume(), self._log))
        except Exception:
            return True  # on data failure, don't block trading

    def _has_position(self) -> bool:
        """True if the exchange shows a non-zero position on this symbol
        (shared cached position read — no extra API call per check)."""
        info = self._position_info()
        return any(abs(float(p.get("positionAmt", 0))) > 1e-8 for p in info)

    def _price(self) -> float:
        td = self.bridge.get_tick() if self.bridge else None
        price = (td["bid"] + td["ask"]) / 2 if td else 0.0
        if price <= 0 and self.bridge and self.bridge._client:
            try:
                price = float(self.bridge._client.futures_mark_price(
                    symbol=self.bridge.binance_symbol)["markPrice"])
            except Exception:
                pass
        return price

    # ── market analytics (klines -> platform-agnostic bars) ──
    def _bars(self, interval: str, limit: int = None) -> List[dict]:
        """Fetch klines and normalize to {high, low, close, quote_volume} bars.

        Cached per (interval, limit) for ANALYTICS_TTL_SEC (default 60s):
        ADX / participation / ATR all share these bars, and re-fetching them
        every tick was hammering the Binance IP (rate-limit bans)."""
        limit = limit or self.core.t.adx_period * 3 + 2
        key = f"{interval}:{limit}"
        hit = self._bars_cache.get(key)
        if hit and time.time() - hit[0] < ANALYTICS_TTL_SEC:
            return hit[1]
        bars = []
        try:  # indicator history from MAINNET (testnet klines mislead ATR/ADX)
            from bridge import public_klines as _pk
            bars = _pk(self.bridge.binance_symbol, interval, limit)
        except Exception:
            bars = []
        if not bars:
            try:
                kl = self.bridge._client.futures_klines(
                    symbol=self.bridge.binance_symbol, interval=interval,
                    limit=limit) or []
                bars = [{"high": float(k[2]), "low": float(k[3]), "close": float(k[4]),
                         "quote_volume": float(k[7])} for k in kl]
            except Exception:
                return []
        self._bars_cache[key] = (time.time(), bars)
        return bars

    def _adx_value(self) -> float:
        # Wilder RMA needs ~100+ bars to warm up; a short window reads hot.
        return adx(self._bars("15m", limit=120), self.core.t.adx_period)

    def _part_volume(self) -> float:
        return quote_volume(self._bars("5m", limit=12))

    # ── order helpers (exchange = truth) ──
    def _open_orders(self) -> Dict[int, dict]:
        try:
            oo = self.bridge._client.futures_get_open_orders(symbol=self.bridge.binance_symbol)
            return {int(o["orderId"]): o for o in oo}
        except Exception as e:
            self._log(f"open_orders err: {str(e)[:60]}")
            return {}

    def _place_limit(self, side: str, price: float, qty: float) -> Optional[int]:
        if self._shutdown.is_set():
            return None
        try:
            px = self.bridge._round_price(price)
            q = self.bridge._round_qty(qty)
            if q <= 0:
                return None
            o = self.bridge._client.futures_create_order(
                symbol=self.bridge.binance_symbol,
                side="BUY" if side == "BUY" else "SELL",
                type="LIMIT", timeInForce="GTC",
                quantity=q, price=str(px),
                risk_rails=True,
                newClientOrderId=f"GURU-{int(time.time()*1000)}")
            if str((o or {}).get("status") or "").upper() == "REJECTED":
                return None
            oid = int((o or {}).get("orderId") or 0)
            if oid <= 0:
                return None
            # Identity is what Binance stored, not what we rounded locally.
            eqty = float(o.get("origQty") or q)
            epx = float(o.get("price") or px)
            self.grid_orders[oid] = {"side": side, "price": epx, "qty": eqty}
            return oid
        except Exception as e:
            self._log(f"place_limit {side}@{price}: {str(e)[:70]}")
            return None

    def _reladder(self, reason: str):
        """Flatten, reset, size, and put the grid back. Spawn/GuruAI stay
        RUNNING — STOPPED + no rotation (manual ＋) is the one-shot bug."""
        try:
            self._flatten()
        except Exception:
            pass
        self.core.reset_grid(reset_regime=False)
        self._pair_pending.clear()
        self.qty = 0.0
        self._pos_ref_price = 0.0
        self._stale_since = 0.0
        try:
            self._size_qty()
        except Exception:
            pass
        if self.qty:
            time.sleep(0.4)
            self._place_grid()
        self._log(f"🔄 re-ladder after {reason}")

    def _flatten(self):
        """Hard reset, verified: cancel EVERY open order (retry until the
        exchange shows zero), close EVERY position, clear local tracking."""
        self._pair_pending.clear()
        try:
            fn = getattr(self.bridge, "flatten_verified", None)
            if callable(fn):
                fn()
                self.grid_orders.clear()
                return
            oo = []
            for attempt in range(3):
                try:
                    self.bridge._client.futures_cancel_all_open_orders(
                        symbol=self.bridge.binance_symbol)
                except Exception:
                    pass
                try:
                    oo = self.bridge._client.futures_get_open_orders(
                        symbol=self.bridge.binance_symbol)
                except Exception:
                    oo = []
                if not oo:
                    break
                time.sleep(0.5)
            if oo:
                self._log(f"⚠️ {len(oo)} orders survived flatten — retrying next tick")
            info = self.bridge._client.futures_position_information(symbol=self.bridge.binance_symbol)
            for p in info:
                amt = float(p.get("positionAmt", 0))
                if abs(amt) < 1e-8:
                    continue
                side = "BUY" if amt < 0 else "SELL"
                self.bridge._client.futures_create_order(
                    symbol=self.bridge.binance_symbol, side=side, type="MARKET",
                    quantity=abs(amt), reduceOnly=True)
            self.grid_orders.clear()
        except Exception as e:
            self._log(f"flatten err: {str(e)[:70]}")

    # ── grid placement (no-ghost invariant: exchange == desired ladder) ──
    def _place_grid(self):
        price = self._price()
        if price <= 0:
            return
        self.core.center = price
        self._reconcile_orders()
        self._log(f"📐 grid at center={price:.5f} | "
                  f"{len(self.grid_orders)} resting | "
                  f"±{self.core.spacing_pct * self.core.active_levels * 100:.1f}%")

    # ── scale-in: add one more level per side on confirmed fill ──
    def _escalate(self):
        # Scale-in freeze: if price has fallen >SCALE_FREEZE_PCT (4%) below
        # the grid center, do NOT open the next level pair — the knife-catcher
        # guard. AKE filled 6 buy levels into a -10% freefall; this caps the
        # amplification at the levels already open.
        if self.core.center > 0:
            price = self._price()
            if price > 0 and (self.core.center - price) / self.core.center > SCALE_FREEZE_PCT:
                self._log(f"🧊 scale-in frozen — price "
                          f"{(price - self.core.center) / self.core.center * 100:+.1f}% "
                          f"below center")
                return
        if self._guard_block_scale:
            return
        # Funds cap: legs max out at what the wallet can fund. Next level
        # pair costs 2*qty*price notional => /leverage margin; skip when the
        # free balance can't cover it (keeps 20% buffer for price drift).
        try:
            _px = self._price()
            _lev = float(getattr(self, "leverage", 10.0) or 10.0)
            _need = (2.0 * float(getattr(self, "qty", 0.0) or 0.0) * _px / _lev) if _px > 0 else 0.0
            _avail = 0.0
            try:
                _acct = self.bridge.get_account_info() if self.bridge else {}
                _avail = float((_acct or {}).get("availableBalance",
                               (_acct or {}).get("free_margin", 0)) or 0)
            except Exception:
                pass
            if _avail <= 0:
                _avail = float(getattr(self, "free_margin", 0.0) or 0.0)
            if _need > 0 and _need > _avail * 0.8:
                self._log(f"💰 funds cap — pair margin ${ _need:.0f} > 80% of free ${_avail:.0f}; legs frozen at {self.core.active_levels}")
                return
        except Exception:
            pass
        pairs = self.core.escalate(self._adx_value(), self._part_volume(), self._log)
        if pairs:
            self._log(f"⬆️ scaled in → {self.core.active_levels} level(s)/side (on fill)")
            self._reconcile_orders()  # the new level pair is placed by reconcile

    # ── no-ghost reconciliation ──
    def _desired_orders(self) -> list:
        """The ladder the exchange must match EXACTLY: [(side, price, qty)].

        Slot model (pending-leg rule): when a level's BUY has filled, BUY is
        NOT re-stocked until the level's SELL (its exit) fills — and vice
        versa. One pending leg per level ⇒ position is bounded at
        levels x qty per side, no matter how often price revisits the band.
        """
        if self.core.center <= 0 or not self.qty:
            return []
        si = getattr(self.bridge, "_symbol_info", {}) or {}
        min_qty = float(si.get("min_qty", 0.001) or 0.001)
        qty = max(self.bridge._round_qty(self.qty), min_qty)
        out = []
        for idx, (side, px) in enumerate(
                self.core.level_pairs(self.core.center, self.core.active_levels)):
            level = idx // 2 + 1
            if self._pair_pending.get(level) == side:
                continue  # leg open — wait for the opposite exit before re-stocking
            _pend = self._pair_pending.get(level)
            _is_exit = _pend is not None and side != _pend
            if not _is_exit and getattr(self, "_guard_no_entries", False):
                continue  # governor: fresh entries paused, exits still managed
            if _is_exit:
                try:
                    from prod_guard import guard as _pguard
                    if not _pguard().leg_age_ok(self, level):
                        continue  # TP-arm / 150 s min hold: exit rests
                except Exception:
                    pass
            out.append((side, self.bridge._round_price(px), qty))
        return out

    def _reconcile_orders(self):
        """Enforce the no-ghost invariant: exchange orders == desired ladder.

        Pass 1: cancel every open order that does NOT match a desired slot
        (one order per slot — duplicates are extras too), and do NOT place
        anything while ghosts exist, so the count can only shrink.
        Pass 2: place each desired slot that is missing on the exchange,
        skipping any order that would fill instantly (crossing the spread).
        """
        try:
            oo = self.bridge._client.futures_get_open_orders(
                symbol=self.bridge.binance_symbol)
        except Exception as e:
            self._log(f"reconcile fetch err: {str(e)[:60]}")
            return
        desired = self._desired_orders()

        def match(o, slot):
            dside, dpx, dqty = slot
            if o.get("side") != dside:
                return False
            opx = float(o.get("price", 0) or 0)
            oqty = float(o.get("origQty", 0) or 0)
            peq = getattr(self.bridge, "price_equal", None)
            qeq = getattr(self.bridge, "qty_equal", None)
            if callable(peq) and callable(qeq):
                return peq(opx, dpx) and qeq(oqty, dqty)
            return abs(opx - dpx) < 1e-8 and abs(oqty - dqty) < 1e-9

        used = set()
        extras = []
        stop_ids = _exchange_stop_ids(self)
        for o in oo:
            if int(o.get("orderId", 0)) in stop_ids:
                continue   # exchange-side trail/loss stops are NOT ghosts
            cf = getattr(self.bridge, "classify_order", None)
            if callable(cf) and cf(o) in ("SL", "TP"):
                continue   # exchange SL/TP are the book, not ghosts
            slot_idx = next((i for i, s in enumerate(desired)
                             if i not in used and match(o, s)), None)
            if slot_idx is not None:
                used.add(slot_idx)
            else:
                extras.append(o)

        if extras:
            for o in extras:
                try:
                    self.bridge._client.futures_cancel_order(
                        symbol=self.bridge.binance_symbol, orderId=int(o["orderId"]))
                    self._log(f"🧹 ghost cancelled {o['side']} qty {o['origQty']} "
                              f"@{o['price']} — {len(extras) - 1} more to clear")
                except Exception:
                    pass  # retried every tick until the ghost is gone
                self.grid_orders.pop(int(o["orderId"]), None)
            return  # never place while ghosts exist

        if getattr(self, "_last_guard_state", "SAFE") == "PAUSE":
            # Hold a live book. If TP/trail already flattened us, still
            # allow the level-1 ladder or spawn stays "one fill then dead".
            if self.grid_orders or self._has_position():
                return

        td = self.bridge.get_tick()
        bid, ask = (td["bid"], td["ask"]) if td else (0.0, 0.0)
        for side, px, qty in desired:
            if self._shutdown.is_set():
                return  # stopped mid-reconcile — never place after stop
            if any(match(o, (side, px, qty)) for o in oo):
                continue
            if side == "BUY" and ask and px >= ask:
                self._log(f"⏸ BUY {qty} @{px} crosses ask {ask:.2f} — waiting for price to fall")
                continue
            if side == "SELL" and bid and px <= bid:
                self._log(f"⏸ SELL {qty} @{px} crosses bid {bid:.2f} — waiting for price to rise")
                continue
            if self._place_limit(side, px, qty):
                self._log(f"✅ ladder order {side} {qty} @{px}")

    # ── fill handling: pending-leg updates + fill-gated scale-in ──
    def _handle_fills(self):
        """Detect fills (orders we placed that vanished from the exchange),
        update per-level pending-leg state, and run fill-gated scale-in.
        NO new orders are ever created here — the opposite ladder level IS
        the exit, and the pending-leg rule bounds position per level."""
        try:
            oo = self.bridge._client.futures_get_open_orders(
                symbol=self.bridge.binance_symbol)
            open_ids = {int(o["orderId"]) for o in oo}
        except Exception:
            return
        filled = 0
        for oid, meta in list(self.grid_orders.items()):
            if oid in open_ids:
                continue  # still resting
            # order vanished -> filled or cancelled; check status
            try:
                o = self.bridge._client.futures_get_order(
                    symbol=self.bridge.binance_symbol, orderId=oid)
                st = o.get("status")
            except Exception:
                st = "UNKNOWN"
            self.grid_orders.pop(oid, None)
            if st == "FILLED":
                filled += 1
                self.cycles += 1
                self._log(f"{meta['side']} filled @{meta['price']:.5f} (cycle {self.cycles})")
                self._mark_pair_pending(meta["side"], meta["price"])
        if filled:
            self._escalate()  # level N+1 opens ONLY because a position leg was created

    def _mark_pair_pending(self, side: str, px: float):
        """Flip the pending-leg state for the level this fill belongs to."""
        level = self._fill_level(side, px)
        if not level:
            return
        pending = self._pair_pending.get(level)
        if pending == side:
            return  # already pending this side (duplicate fill) — no double leg
        if pending is None:
            self._pair_pending[level] = side  # leg open → exit must fill first
            try:
                from prod_guard import guard as _pguard
                _pguard().leg_open(self, level)
            except Exception:
                pass
            self._log(f"🔒 level {level}: {side} leg open — {side} re-stock paused")
        else:
            self._pair_pending.pop(level, None)  # exit filled → round trip done
            try:
                from prod_guard import guard as _pguard
                _pguard().leg_close(self, level)
            except Exception:
                pass
            self._log(f"🔓 level {level}: round trip done — re-stocking both sides")

    def _fill_level(self, side: str, px: float) -> int:
        """Which grid level a fill belongs to (tolerance = half a cell)."""
        if self.core.center <= 0 or not self.core.spacing_pct:
            return 0
        tol = self.core.center * self.core.spacing_pct * 0.5
        for level in range(1, self.core.active_levels + 1):
            ref = self.core.center * (1 + self.core.spacing_pct * level) \
                if side == "SELL" else \
                self.core.center * (1 - self.core.spacing_pct * level)
            if abs(px - ref) <= tol:
                return level
        return 0

        # ── risk: max position hold time ──
    def _position_side(self) -> int:
        """+1 net long, -1 net short, 0 flat (cached position read)."""
        info = self._position_info()
        amt = sum(float(p.get("positionAmt", 0) or 0) for p in info)
        if amt > 1e-8:
            return 1
        if amt < -1e-8:
            return -1
        return 0

    def _check_max_hold(self):
        """Time-based exit: a position may not stay open longer than the mode's
        max-hold (scalp 6h, swing 24h). On expiry close it at market — the win
        is banked, the loss is cut — and the grid re-ladders re-centered next
        tick. This also unblocks rotation (rotation_safe needs a flat bot)."""
        _mh = float(getattr(getattr(self, "core", None), "t", None) is not None
                    and getattr(self.core.t, "max_hold_min", 0) or MAX_HOLD_MIN)
        if _mh <= 0:
            return
        info = self._position_info()
        amt = sum(abs(float(p.get("positionAmt", 0))) for p in info)
        if amt < 1e-8:
            self._pos_opened_at = 0.0
            return
        if self._pos_opened_at <= 0:
            self._pos_opened_at = time.time()
            return
        age_min = (time.time() - self._pos_opened_at) / 60.0
        if age_min < _mh:
            return
        try:
            for p in info:
                q = float(p.get("positionAmt", 0))
                if abs(q) < 1e-8:
                    continue
                side = "BUY" if q < 0 else "SELL"
                self.bridge._client.futures_create_order(
                    symbol=self.bridge.binance_symbol, side=side,
                    type="MARKET", quantity=abs(q), reduceOnly=True)
        except Exception as e:
            self._log(f"max-hold close err: {str(e)[:70]}")
            return
        self._pos_opened_at = 0.0
        self._log(f"⏱ MAX HOLD ({_mh:.0f}m) — position force-closed, "
                  f"grid will re-ladder re-centered")

    # ── risk: equity stop ──
    def _position_info(self) -> list:
        """Cached futures_position_information (POS_INFO_TTL_SEC) — the
        equity stop, max-hold and crash guard all read the same positions;
        one call per TTL instead of 3 per tick."""
        ts, cached = self._pos_info_cache
        if cached is not None and time.time() - ts < POS_INFO_TTL_SEC:
            return cached
        try:
            info = self.bridge._client.futures_position_information(
                symbol=self.bridge.binance_symbol)
        except Exception:
            return []
        self._pos_info_cache = (time.time(), info)
        return info

    def _check_equity_stop(self) -> bool:
        info = self._position_info()
        upnl = sum(float(p.get("unRealizedProfit", 0)) for p in info)
        total = self.realized_pnl + upnl
        stop_at = -abs(self._entry_alloc * self._stop_pct())
        if total <= stop_at:
            self._halt_cooldown()
            self._log(f"🛑 EQUITY STOP: pnl ${total:+.2f} <= "
                      f"-{self._stop_pct()*100:.0f}% alloc"
                      f"{' (anchor)' if self.is_anchor else ''}")
            return True
        return False

    # ── daily reset ──
    def _maybe_daily_reset(self):
        # Prop-only: 0:00 UTC flatten + re-center. Without prop the bot trades
        # through midnight as usual (no forced daily flatten).
        if not prop_mode():
            return
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if now.hour == RESET_HOUR_UTC and self._last_reset_date != today:
            self._last_reset_date = today
            self._log("🌙 DAILY RESET (00:00 UTC) — flatten + re-center fresh")
            self._flatten()
            self.realized_pnl = 0.0
            self.core.reset_grid()
            self.core.compute_spacing(atr_pct(self._bars("15m"), self.core.t.atr_period), self._log)
            time.sleep(1)
            self._place_grid()

    def _prop_sitout_tick(self) -> bool:
        """Prop-only session/daily discipline (mirrors frozen backtest rules).
        Session bank +2% / loss -2% -> flatten + sit out rest of session;
        daily loss past GB_PROP_DAILY_LOSS_PCT (default 2%) -> flatten + sit
        out rest of UTC day. Returns True when the bot may trade."""
        try:
            bank_pct = float(os.getenv("GB_PROP_SESS_BANK_PCT", "0.02") or 0.02)
            loss_pct = float(os.getenv("GB_PROP_SESS_LOSS_PCT", "0.02") or 0.02)
            daily_pct = float(os.getenv("GB_PROP_DAILY_LOSS_PCT", "0.02") or 0.02)
        except (TypeError, ValueError):
            bank_pct, loss_pct, daily_pct = 0.02, 0.02, 0.02
        now = datetime.now(timezone.utc)
        day = now.strftime("%Y-%m-%d")
        sess = ("ASIA" if now.hour < 8 else ("LONDON" if now.hour < 16 else "NY")) + day
        try:
            info = self._position_info()
            upnl = sum(float(p.get("unRealizedProfit", 0)) for p in info)
        except Exception:
            upnl = 0.0
        eq = float(getattr(self, "realized_pnl", 0.0) or 0.0) + upnl
        base = float(self._wallet_equity() or 0.0) or float(getattr(self, "_entry_alloc", 0.0) or 1.0)
        if day != self._prop_day:
            self._prop_day, self._prop_day_base, self._prop_day_out = day, base, False
        if sess != self._prop_sess:
            self._prop_sess, self._prop_sess_base, self._prop_sess_out = sess, base, False
        if self._prop_day_out or self._prop_sess_out:
            return False  # sitting out: exits still managed by reconcile, no entries
        if self._prop_day_base > 0 and (eq - self._prop_day_base) / self._prop_day_base <= -abs(daily_pct):
            self._flatten()
            self._prop_day_out = True
            self._log(f"🛡 PROP daily-loss sit-out {day} — flat, done for the day")
            return False
        if self._prop_sess_base > 0:
            sp = (eq - self._prop_sess_base) / self._prop_sess_base
            if sp >= abs(bank_pct):
                self._flatten()
                self._prop_sess_out = True
                self._log(f"🛡 PROP session BANK {sess} +{sp*100:.1f}% — flat, sitting out")
                return False
            if sp <= -abs(loss_pct):
                self._flatten()
                self._prop_sess_out = True
                self._log(f"🛡 PROP session CUT {sess} {sp*100:.1f}% — flat, sitting out")
                return False
        return True

    # ── main loop ──
    def _loop(self):
        while not self._shutdown.is_set():
            if self._paused.is_set():
                self._shutdown.wait(timeout=5)
                continue
            try:
                self._neutral_tick()
            except Exception as e:
                self._log(f"loop err: {str(e)[:70]}")
            if self.status == BotStatus.STOPPED:
                break
            self._shutdown.wait(timeout=TICK_SEC)

    def _session_rest(self, price: float) -> bool:
        """True when the desk should sleep: flat AND (rest-hours clock OR dead
        tape). GB_SESSION_REST=1 means ON (unset also = ON); set 0 to disable."""
        if (os.getenv("GB_SESSION_REST", "1") or "1").strip() == "0":
            return False
        try:
            if self._has_position() or any(self._pair_pending.values()):
                return False
            # 1) clock: rest hours UTC (default late-NY wind-down)
            hrs = [h.strip() for h in
                   (os.getenv("GB_REST_HOURS", "21,22,23") or "").split(",")
                   if h.strip().isdigit()]
            if hrs and str(datetime.now(timezone.utc).hour) in hrs:
                return True
            # 2) dead tape: flat ADX + thin participation, or hollow spread
            try:
                av = float(self._adx_value() or 0)
            except Exception:
                av = 0.0
            try:
                qv = quote_volume(self._bars("5m", limit=12) or [])
            except Exception:
                qv = 0.0
            floor = float(self.core.t.min_part_vol or 0)
            if av > 0 and av < 8.0 and (floor <= 0 or qv < floor):
                return True
            try:
                td = self.bridge.get_tick() if self.bridge else None
                if td:
                    b, a = float(td.get("bid") or 0), float(td.get("ask") or 0)
                    if b > 0 and a > b and (a - b) / ((a + b) / 2) > 0.004:
                        return True
            except Exception:
                pass
            return False
        except Exception:
            return False

    def _neutral_tick(self):
        """One grid cycle: gate → size/anchor → session re-quote → fills
        (pending-leg + scale-in) → no-ghost reconcile → recenter → safety."""
        if self._shutdown.is_set():
            return
        # Periodic PRICE_FILTER/LOT_SIZE refresh (Binance changes tick sizes
        # mid-session). Cheap single-symbol call; covers anchors that never
        # rotate (rotation already recreates the bridge).
        now = time.time()
        if now - self._sym_info_refresh_at >= SYMBOL_INFO_REFRESH_SEC:
            self._sym_info_refresh_at = now
            try:
                if self.bridge:
                    self.bridge.refresh_symbol_info()
            except Exception:
                pass
        self._maybe_daily_reset()
        price = self._price()
        if price <= 0:
            return
        # Prop-only risk (both blocks prop-gated; without prop the bot trades
        # through as usual). (a) v1.0 production governor: portfolio DD/heat
        # bands gate NEW entries; emergency flattens + halts. (b) session/
        # daily discipline: bank/loss sit-outs + daily-loss sit-out. Both use
        # the entry gate so exits keep managing.
        try:
            if prop_mode():
                from prod_guard import guard as _pguard, cached_snapshot
                _snap = cached_snapshot(getattr(self, "env", "demo") or "demo")
                _v = _pguard().account_verdict(
                    _snap["desk_pnl"], _snap["floating"], _snap["legs"],
                    env=getattr(self, "env", "demo"),
                    per_symbol_float=_snap["per_symbol_float"])
                self._guard_no_entries = not _v.get("allow_new_entries", True)
                if _v.get("emergency_flatten"):
                    self._log(f"🚨 GUARD EMERGENCY: {_v.get('reason', '')} — flatten + halt")
                    self._flatten()
                    self._halt_cooldown()
                    self.pause()
                    return
                if self._guard_no_entries:
                    self._log(f"⛔ GUARD: {_v.get('reason', '')} — entries paused, exits managed")
                if not self._prop_sitout_tick():
                    self._guard_no_entries = True
            else:
                self._guard_no_entries = False
        except Exception as _ge:
            self._log(f"guard err (fail-open): {str(_ge)[:70]}")

        # Universal realtime regime gate (Binance / MT5). Emits a
        # state; this tick is the actuator. Never auto-reverses.
        self._guard_block_scale = False
        if self.bridge:
            try:
                from binance_guard import evaluate as _gate_eval
                td = self.bridge.get_tick() if hasattr(self.bridge, "get_tick") else None
                hist = getattr(self.bridge, "_mid_hist", None)
                if hist is None:
                    from collections import deque as _dq
                    self.bridge._mid_hist = _dq(maxlen=1800)
                    hist = self.bridge._mid_hist
                if td:
                    mid = 0.0
                    try:
                        mid = (float(td.get("bid") or 0) + float(td.get("ask") or 0)) / 2.0
                    except (TypeError, ValueError):
                        mid = 0.0
                    if mid <= 0:
                        try:
                            mid = float(td.get("last") or 0)
                        except (TypeError, ValueError):
                            mid = 0.0
                    if mid > 0:
                        hist.append((time.time(), mid))
                acct = {}
                try:
                    acct = self.bridge.get_account_info() or {}
                except Exception:
                    pass
                pos_qty = 0.0
                fn = getattr(self.bridge, "exchange_position_qty", None)
                if callable(fn):
                    pos_qty = float(fn() or 0)
                levels = max(int(self.core.active_levels or 1), 1)
                plat = (getattr(self.bridge, "platform", None) or "binance").lower()
                # Equity (free + locked). Investable free drops when the
                # ladder locks margin — that is not a wipeout.
                wallet = float(acct.get("equity") or acct.get("balance") or 0)
                # Desk-local peak. A $5k Binance book must not EXTREME a
                # non-Binance wallet (same process-wide _bank_state).
                if plat in ("binance", ""):
                    peak = float(_bank_state.get("peak") or _bank_state.get("wallet")
                                 or wallet or 0)
                else:
                    prev = float(getattr(self, "_guard_wallet_peak", 0) or 0)
                    peak = max(prev, wallet)
                    self._guard_wallet_peak = peak
                mkt = {"wallet": wallet,
                       "peak_wallet": peak,
                       "mid_hist": list(hist or []),
                       "adx": self._adx_value(),
                       "adx_range_max": self.core.t.adx_range_max}
                decision = _gate_eval(
                    mkt,
                    {"position_qty": pos_qty,
                     "open_orders": len(self.grid_orders),
                     # Cap exposure so a 1-level bot (BTC 0.001, only 1 level fits) is
                     # NOT "100% exposed" — high_exposure was adding 15×1.4=21 always,
                     # pushing UNWIND 91-100 on any vol spike → taker-close churn.
                     "exposure_frac": min(0.5 if levels < 3 else 1.0,
                                          levels / max(self.grid_levels, 1))},
                )
                st = decision.get("state", "SAFE")
                reasons = decision.get("reason_codes") or []
                from binance_guard import wallet_wiped as _wallet_wiped
                if _wallet_wiped(wallet, peak, mkt.get("wipeout_pct")):
                    st = "EXTREME"
                    reasons = list(reasons) + (["wallet_wipeout"] if "wallet_wipeout" not in reasons else [])
                # Book wipeout is real equity (free+locked). Do not demote it
                # to PAUSE — that is how TACINR gave back ~20% of a $3 book.
                if st != self._last_guard_state:
                    self._log(f"🛡 gate {self._last_guard_state} → {st} "
                              f"score={decision.get('risk_score')} "
                              f"{','.join(reasons)} "
                              f"wallet={wallet:.2f} peak={peak:.2f}")
                    self._last_guard_state = st
                if st == "EXTREME":
                    self._flatten()
                    self.status = BotStatus.STOPPED
                    self._halt_cooldown()
                    if plat in ("binance", ""):
                        try:
                            _sweep_account_flat(
                                self.user_id,
                                getattr(self.bridge, "environment", "live"))
                        except Exception:
                            pass
                    self._log("🛑 GATE EXTREME — account sweep")
                    return
                # ── Layer 3 standalone JEV CUT (JEV_CUT=1): evaluated EVERY tick
                # for losing positions, independent of z-guard state. The UNWIND
                # branch below only runs at score≥80; microstructure opposition
                # must cut sooner. 90s min-hold still respected (no fill→insta-cut
                # churn). Winners untouched. Fail-open. Fires on real JEV only.
                try:
                    import openrouter_client as _jgcut
                    if (os.getenv("JEV_CUT", "0") not in ("", "0")
                            and self._has_position()):
                        _ps = self._position_side()
                        try:
                            _pi = self._position_info()
                            _pu = sum(float(p.get("unRealizedProfit", 0) or 0) for p in _pi)
                        except Exception:
                            _pu = 0.0
                        if _pu <= 0.05 and time.time() - getattr(self, "_pos_opened_at", 0) >= 90:
                            _cv = _jgcut.unwind_vote(
                                self.symbol, self.user_id,
                                getattr(self.bridge, "environment", self.env), _ps)
                            if _cv == "close":
                                self._guard_block_scale = True
                                self._flatten()
                                self.core.reset_grid()
                                self._pair_pending.clear()
                                self.qty = 0.0
                                self._log(f"🛡 JEV CUT — flattened {self.symbol} (microstructure opposed, upnl {_pu:+.2f})")
                                return
                except Exception:
                    pass
                if st == "UNWIND":
                    # Profitable hold: only unwind a profitable position when direction has reversed.
                    # User rule: "allow close in profitable situation only when its changed the direction"
                    # So if long+UP (profitable up) or short+DOWN, hold — don't cut the winner.
                    if self._has_position():
                        pos_side = self._position_side()
                        direction = str(decision.get("direction") or "NONE").upper()
                        try:
                            _info = self._position_info()
                            _upnl = sum(float(p.get("unRealizedProfit", 0) or 0) for p in _info)
                        except Exception:
                            _upnl = 0.0
                        is_profitable = _upnl > 0.05  # dust threshold
                        adverse = (pos_side > 0 and direction == "DOWN") or (pos_side < 0 and direction == "UP")
                        # Favorable or flat: profitable + not adverse → hold
                        if is_profitable and not adverse:
                            self._guard_block_scale = True
                            self._log(f"🛡 GATE UNWIND held — profitable {_upnl:+.2f} direction {direction} still favorable (side {pos_side:+.0f}), not flattening")
                            return
                        # Small profit + adverse: let trailing lock handle it, not gate
                        if is_profitable and adverse and _upnl < 0.20:
                            self._guard_block_scale = True
                            self._log(f"🛡 GATE UNWIND held — small profit {_upnl:+.2f} adverse {direction}, trail will lock, not gate")
                            return
# Min-hold after fill: never gate-close a position younger than
                        # 90s unless EXTREME — prevents "fill then instant taker-close"
                        # churn that bled BTC -1.45 / ETH -0.81 / XAU -0.69.
                        if time.time() - getattr(self, "_pos_opened_at", 0) < 90:
                            self._guard_block_scale = True
                            self._log(f"🛡 GATE UNWIND held — position <90s old, letting grid breathe")
                            return
                        # 2026-09-02 min-hold: a PROFITABLE position younger than
                        # 10 min gets to breathe — noise spikes must not taker-close
                        # a green leg (pennies for commissions). EXTREME bypasses.
                        _mh_now = time.time()
                        if is_profitable and _mh_now - getattr(self, "_pos_opened_at", 0) < 600.0:
                            self._guard_block_scale = True
                            self._log(f"🛡 GATE UNWIND held — profitable {_upnl:+.2f}, min-hold {600 - (_mh_now - getattr(self, '_pos_opened_at', 0)):.0f}s")
                            return
                        # Layer 3 (JEV_CUT=1): microstructure cutoff vote. Strong JEV
                        # opposition to a LOSING position flattens even before the
                        # z-guard cooldown clears. Winners keep every hold above;
                        # vote fires on real JEV only, never the mock stand-in.
                        try:
                            import openrouter_client as _jg
                            if not is_profitable:
                                _vote = _jg.unwind_vote(
                                    self.symbol, self.user_id,
                                    getattr(self.bridge, "environment", self.env),
                                    pos_side)
                                if _vote == "close":
                                    self._guard_block_scale = True
                                    self._last_gate_flat_ts = time.time()
                                    self._flatten()
                                    self.core.reset_grid()
                                    self._pair_pending.clear()
                                    self.qty = 0.0
                                    self._log(f"🛡 JEV CUT — flattened {self.symbol} (microstructure opposed, upnl {_upnl:+.2f})")
                                    return
                        except Exception:
                            pass
                        # Real adverse + not profitable → flatten (taker close)
                        # 2026-09-02 cooldown: never gate-flatten the same symbol
                        # twice within 10 min — the z-guard flaps on noise and
                        # re-flattened ARB/SKR 6x each today. EXTREME bypasses.
                        _now = time.time()
                        _last_flat = getattr(self, "_last_gate_flat_ts", 0.0)
                        if _now - _last_flat < 600.0:
                            self._guard_block_scale = True
                            self._log(f"🛡 GATE UNWIND held — cooldown ({600 - (_now - _last_flat):.0f}s left), last flatten {_now - _last_flat:.0f}s ago")
                            return
                        self._last_gate_flat_ts = _now
                        self._flatten()
                        self.core.reset_grid()
                        self._pair_pending.clear()
                        self.qty = 0.0
                        self._log(f"🛡 GATE UNWIND — flattened {self.symbol} (adverse {direction}, upnl {_upnl:+.2f})")
                        return
                    # FLAT with resting grid: do NOT cancel+re-ladder (churn).
                    # Just block new scale-in; keep the resting ladder so fills still happen.
                    self._guard_block_scale = True
                    self._log("🛡 GATE UNWIND — flat, keeping resting ladder (no churnresting ladder (no churn)")
                    return
                if st in ("CAUTION", "REDUCE", "PAUSE"):
                    self._guard_block_scale = True
            except Exception as e:
                logger.debug(f"regime_guard: {e}")

        # Market-time gate (GB_RANGE_GATE=1): flatten + stay flat while
        # trending or dead. A neutral grid only profits in a range.
        # Bias exemption (2026-08-21): a DynamicGuruAI bot with an active
        # bias IS the trend response — its asymmetric ladder rides the move,
        # so never flatten it for trending. Neutral grids keep the range
        # discipline.
        if _range_gate_enabled() and not self._range_open():
            if getattr(self, "bias", "neutral") != "neutral":
                pass   # bias-engaged: ride the trend (asymmetric ladder handles it)
            elif self.grid_orders or self._has_position():
                self._flatten()
                self.core.reset_grid()
                self._pair_pending.clear()
                self.qty = 0.0
                self._log("🌙 trending/dead market — flattened, waiting for range")
            return

        # Crash guard: halt if price moves >CRASH_GUARD_PCT from the exposure
        # ref. Directional (fix A, 2026-08-18): a bias-engaged bot only halts
        # on ADVERSE moves (long: -5%, short: +5%) — the ACE +5.3% profit
        # move used to halt a winning long. Neutral bias keeps the symmetric
        # guard. Fix C: while biased, the ref trails the favorable side so
        # winners are never cut on their own move (longs trail the high,
        # shorts trail the low).
        if self._has_position():
            bias = getattr(self, "bias", "neutral")
            pos_side = self._position_side()
            from binance_guard import crash_threshold as _crash_thr
            crash_pct = _crash_thr(getattr(self, "crash_guard_pct", CRASH_GUARD_PCT))
            if self._pos_ref_price <= 0:
                self._pos_ref_price = price
            else:
                # fix C: favorable-side ref trailing for bias positions
                if bias == "long" and pos_side > 0 and price > self._pos_ref_price:
                    self._pos_ref_price = price
                elif bias == "short" and pos_side < 0 and price < self._pos_ref_price:
                    self._pos_ref_price = price
                drift = (price - self._pos_ref_price) / self._pos_ref_price
                adverse = abs(drift) > crash_pct
                if bias == "long":
                    adverse = drift <= -crash_pct
                elif bias == "short":
                    adverse = drift >= crash_pct
                if adverse:
                    self._halt_cooldown()
                    self._log(f"🛑 CRASH GUARD: price {drift*100:+.1f}% from exposure "
                              f"ref — flattened, re-ladder (> {crash_pct*100:.1f}%, "
                              f"bias={bias})")
                    self._reladder("crash guard")
                    return
        else:
            self._pos_ref_price = 0.0

        # Session rest gate (GB_SESSION_REST=1, default ON): flat AND (rest-hours
        # clock OR dead tape) ⇒ cancel legs once, sleep placements, keep
        # position/stops/safety running. Wakes automatically. Kills dead-hour
        # rotation churn (147 starts/6h was mostly this).
        if self._session_rest(price):
            if not getattr(self, "_resting", False):
                self._flatten()
                self._resting = True
                self._log("😴 session rest — dead tape, placements sleeping (position/stops live on)")
            return
        self._resting = False

        # Prop-firm discipline (GB_PROP_MODE=1): weekend-flat + news blackout.
        # Unlike rest (legs only), prop FLATTENS everything — challenges ban
        # weekend holds and news gaps outright.
        _prop = prop_flat_reason()
        if _prop and (self._has_position() or self.grid_orders or
                      any(self._pair_pending.values())):
            self._flatten()
            self.core.reset_grid()
            self._pair_pending.clear()
            self.qty = 0.0
            self._log(f"🛡 PROP {_prop} — flattened, staying flat")
            return
        if _prop:
            return  # flat already: hold, no new placements

        if not self.qty:
            self._size_qty()
        if self.core.center <= 0:
            self.core.center = price

        # Session spacing with HYSTERESIS: re-quote only when the session
        # actually changes (ASIA/LONDON/NY) or the spacing moved >20% from
        # what the ladder already uses. Before this, every tick's ATR wobble
        # re-quoted the whole ladder (ghost-cancel + re-place storm), which
        # kept cancelling resting orders before they could fill — BTC/XAU
        # almost never traded, and the cancel/place churn hammered Binance
        # into -1003 rate bans.
        ss = self._session_spacing(price, atr_pct(self._bars("15m"), self.core.t.atr_period))
        if ss and self.core.spacing_pct > 0:
            sess_changed = ss[0] != self._last_session
            rel = abs(ss[1] - self.core.spacing_pct) / self.core.spacing_pct
            if sess_changed or rel > SESSION_REQUOTE_HYSTERESIS:
                self._last_session = ss[0]
                self.core.spacing_pct = ss[1]
                self._log(f"🗓 {ss[0]} session → spacing {ss[1] * 100:.3f}% "
                          f"(${ss[1] * price:.0f}/side)")
        elif ss:
            self._last_session = ss[0]
            self.core.spacing_pct = ss[1]

        # Ladder-band recenter: the grid follows the price, so a stale ladder
        # can never sit outside the market and instant-fill its re-stocks.
        self.core.t.recenter_drift = max(0.006,
                                         (self.core.active_levels + 0.5) * self.core.spacing_pct)

        self._handle_fills()       # fills → pending-leg updates + fill-gated scale-in
        self._reconcile_orders()   # enforce the no-ghost invariant

        # Exchange-truth sync: if _pair_pending has entries but no position
        # exists on the exchange, those legs were closed externally (broker
        # SL/TP, manual close, margin call). Clear stale state so the grid
        # can re-center instead of being stuck thinking legs are open.
        if self._pair_pending and not self._has_position():
            n = len(self._pair_pending)
            self._pair_pending.clear()
            self.core.reset_grid(reset_regime=False)
            self._log(f"🔄 external close detected — {n} pending leg(s) cleared, "
                      f"grid resynced to exchange truth")

        # Re-center ONLY when flat (no position, no open leg). Re-centering
        # with a live position market-closes it (taker fees + spread) and
        # re-ladders — that was the constant small-bleed churn. With a leg
        # open, the pending-leg exit stays resting; the crash guard (5%) and
        # equity stop (-10% alloc) are the backstops.
        if self.core.should_recenter(price) and not self._has_position() \
                and not any(self._pair_pending.values()):
            drift = (price - self.core.center) / self.core.center * 100
            self._log(f"🔄 drift {drift:+.1f}% -> re-center")
            self._flatten()
            self.core.reset_grid(reset_regime=False)
            self.qty = 0.0
            self._size_qty()
            time.sleep(1)
            self._place_grid()
            return

        # Stale-ladder proximity re-center: flat, and every level is farther
        # than STALE_LEVEL_PCT from price for STALE_LEVEL_MIN minutes —
        # re-center at market instead of idling until the 5% band drift.
        if not self._has_position() and not any(self._pair_pending.values()):
            pairs_px = [px for _, px in self.core.level_pairs(
                self.core.center, self.core.active_levels)]
            if pairs_px:
                nearest = min(abs(px - price) / price * 100 for px in pairs_px)
                if nearest > STALE_LEVEL_PCT:
                    if self._stale_since <= 0:
                        self._stale_since = time.time()
                    elif time.time() - self._stale_since >= STALE_LEVEL_MIN * 60:
                        self._log(f"🔄 stale ladder: nearest level {nearest:.2f}% "
                                  f"away > {STALE_LEVEL_MIN:.0f}m -> re-center")
                        self._stale_since = 0.0
                        self._flatten()
                        self.core.reset_grid(reset_regime=False)
                        self.qty = 0.0
                        self._size_qty()
                        time.sleep(1)
                        self._place_grid()
                        return
                else:
                    self._stale_since = 0.0
        else:
            self._stale_since = 0.0

        if self._check_equity_stop():
            self._reladder("equity stop")
            return

        self._check_max_hold()

        # positions snapshot for UI (exchange-truth)
        self.positions = self.bridge.get_positions()

    def _levels_detail(self, price: float, upnl: float, net_qty: float) -> list:
        """Resting grid rungs + exchange position (with real uPnL)."""
        out = []
        mark = float(price or 0)
        seen = set()
        plat = str(getattr(self.bridge, "platform", "") or "").lower()
        local = list(self.grid_orders.values())[:16]
        for i, m in enumerate(local):
            px = float(m.get("price") or 0)
            qty = float(m.get("qty") or 0)
            side = (m.get("side") or "?").upper()
            dist = ((px - mark) / mark * 100) if mark > 0 and px > 0 else 0.0
            seen.add((side, round(px, 8)))
            out.append({
                "level": i + 1, "kind": "grid", "filled": False,
                "type": side, "volume": qty, "price_open": px,
                "mark_price": round(mark, 5),
                "notional": round(qty * (mark or px), 2),
                "pnl": 0.0, "dist_pct": round(dist, 3), "tp_target": 0,
                "sl": 0.0, "tp": 0.0,
            })
        try:
            oo = []
            if self.bridge and getattr(self.bridge, "_client", None):
                oo = self.bridge._client.futures_get_open_orders(
                    symbol=self.bridge.binance_symbol) or []
            classify = getattr(self.bridge, "classify_order", None)
            for o in oo:
                try:
                    px = float(o.get("order_price") or o.get("price") or 0)
                except (TypeError, ValueError):
                    px = 0.0
                qty = float(o.get("origQty") or o.get("qty") or 0)
                if px <= 0:
                    continue
                raw_kind = "LIMIT"
                if callable(classify):
                    try:
                        raw_kind = classify(o) or "LIMIT"
                    except Exception:
                        raw_kind = "LIMIT"
                if raw_kind == "SL":
                    kind, side = "sl", "SL"
                elif raw_kind == "TP":
                    kind, side = "tp", "TP"
                else:
                    kind = "grid"
                    side = (o.get("side") or "?").upper()
                try:
                    sl_px = float(o.get("stoploss_price") or 0)
                except (TypeError, ValueError):
                    sl_px = 0.0
                try:
                    tp_px = float(o.get("takeprofit_price") or 0)
                except (TypeError, ValueError):
                    tp_px = 0.0
                key = (kind, side, round(px, 4))
                if key in seen:
                    continue
                seen.add(key)
                dist = ((px - mark) / mark * 100) if mark > 0 else 0.0
                out.append({
                    "level": len(out) + 1, "kind": kind, "filled": False,
                    "type": side, "volume": qty, "price_open": px,
                    "mark_price": round(mark, 5),
                    "notional": round(qty * (mark or px), 2),
                    "pnl": 0.0, "dist_pct": round(dist, 3), "tp_target": 0,
                    "sl": sl_px, "tp": tp_px,
                })
        except Exception:
            pass
        try:
            for p in (self.bridge.get_exchange_positions() if self.bridge else []):
                side = (p.get("side") or "?").upper()
                qty = float(p.get("qty") or 0)
                entry = float(p.get("entry_price") or 0)
                pnl = float(p.get("pnl") or 0)
                mk = float(p.get("mark_price") or mark or 0)
                if abs(pnl) < 1e-12 and mk > 0 and entry > 0 and qty:
                    pnl = (mk - entry) * qty if side == "BUY" else (entry - mk) * qty
                out.append({
                    "level": 0, "kind": "pos", "filled": True,
                    "type": side, "volume": qty, "price_open": entry,
                    "mark_price": round(mk, 5),
                    "notional": round(qty * (mk or entry), 2),
                    "pnl": round(pnl, 4), "dist_pct": 0.0, "tp_target": 0,
                    "sl": 0.0, "tp": 0.0,
                })
        except Exception:
            if abs(net_qty) > 1e-8:
                out.append({
                    "level": 0, "kind": "pos", "filled": True,
                    "type": "BUY" if net_qty > 0 else "SELL",
                    "volume": abs(net_qty), "price_open": mark,
                    "mark_price": round(mark, 5),
                    "notional": round(abs(net_qty) * mark, 2),
                    "pnl": round(upnl, 4), "dist_pct": 0.0, "tp_target": 0,
                    "sl": 0.0, "tp": 0.0,
                })
        # Far opposite LIMIT is the INR TP rail, not a second grid. Painting
        # it as BUY 5.5% below gold yanked the chart scale off the tick.
        net_side = 0
        for row in out:
            if (row.get("kind") or "") == "pos":
                t = (row.get("type") or "").upper()
                if t in ("BUY", "LONG"):
                    net_side += 1
                elif t in ("SELL", "SHORT"):
                    net_side -= 1
        for row in out:
            if (row.get("kind") or "grid") != "grid":
                continue
            dist = abs(float(row.get("dist_pct") or 0))
            if dist < 1.5:
                continue
            side = (row.get("type") or "").upper()
            if (net_side < 0 and side == "BUY") or (net_side > 0 and side == "SELL") or net_side == 0:
                row["kind"] = "tp"
                row["type"] = "TP"
        for i, row in enumerate(out):
            if (row.get("kind") or "grid") != "pos":
                row["level"] = i + 1
        return out

    # ── status ──
    def status_dict(self) -> dict:
        price = self._price()
        try:
            info = self.bridge._client.futures_position_information(symbol=self.bridge.binance_symbol)
            upnl = sum(float(p.get("unRealizedProfit", 0)) for p in info)
            net_qty = sum(float(p.get("positionAmt", 0)) for p in info)
        except Exception:
            upnl, net_qty = 0.0, 0.0
        prev_q = float(getattr(self, "_chart_qty", 0.0) or 0)
        if abs(float(net_qty or 0) - prev_q) > 1e-8 and price > 0:
            try:
                from chart_marks import note_from_bot
                note_from_bot(self, prev_q, net_qty, price)
            except Exception:
                pass
        self._chart_qty = float(net_qty or 0)
        try:
            from chart_marks import list_marks as _list_chart_marks
            plat = str(getattr(self.bridge, "platform", "") or "").lower()
            cmarks = _list_chart_marks(getattr(self, "user_id", "") or "",
                                       plat, getattr(self, "symbol", "") or "")
        except Exception:
            cmarks = []
        detail = self._levels_detail(price, upnl, net_qty)
        grid_n = sum(1 for x in detail if (x.get("kind") or "grid") == "grid")
        total_pnl = round(self.realized_pnl + upnl, 4)
        now = time.time()
        if now - getattr(self, "_snap_at", 0) >= 15:
            self._snap_at = now
            try:
                import local_store
                local_store.save_snapshot(self.symbol, total_pnl, net_qty)
            except Exception:
                pass
        return {
            "name": self.name,
            "symbol": self.symbol,
            "status": self.status.value,
            "levels": grid_n,
            "levels_detail": detail,
            "total_pnl": total_pnl,
            "net_position": net_qty,
            "cycles": self.cycles,
            "log": self.logs[-5:],
            "chart_marks": cmarks,
        }


# ── Manager ──────────────────────────────────────────────────────────────────

class GuruAIManager:
    """Orchestrates the scan + neutral grids for one symbol group.
    Two groups run side-by-side:
      - "meme": sub-$1 coins (GURU_MEME_MAX_PRICE, default $1)
      - "all":  full-market scan, excludes the meme picks (GURU_ALL_MAX_PRICE=0 = any)
    Bot counts: GURU_MEME_BOTS / GURU_ALL_BOTS (default 3 each)."""

    def __init__(self, orchestrator, env: str = "demo", user_id: str = None,
                 group: str = "all", max_price: float = None, n_bots: int = None,
                 platform: str = "binance"):
        self.orch = orchestrator
        self.env = env
        self.user_id = user_id
        self.group = group
        self.platform = (platform or "binance").lower()
        gk = group.upper()
        if max_price is None:
            max_price = float(os.getenv(f"GURU_{gk}_MAX_PRICE",
                                        "1.0" if group == "meme" else "0"))
        self.max_price = max_price or 0.0            # 0 = any price
        raw_n = n_bots if n_bots is not None else int(os.getenv(f"GURU_{gk}_BOTS", "10"))
        self.n_bots = clamp_guru_n(raw_n) if group != "manual" else int(raw_n)
        # Priority symbols: reserve slots when moving (score >= floor) — the
        # all-market group defaults to XAUUSDT,BTCUSDT; meme group none.
        self.priority = tuple(s.strip().upper() for s in
            os.getenv(f"GURU_{gk}_PRIORITY",
                      ("XAUUSDT,BTCUSDT" if group == "all" else "")).split(",")
            if s.strip())
        self.priority_min_score = float(
            os.getenv(f"GURU_{gk}_PRIORITY_MIN_GAIN", "0"))
        # Fixed anchor symbols: ALWAYS kept, never rotated (e.g. XAUUSDT +
        # BTCUSDT). They occupy the first slots; the rest come from the
        # freshness scan. Empty for the meme group.
        self.fixed_symbols = tuple(s.strip().upper() for s in
            os.getenv(f"GURU_{gk}_FIXED_SYMBOLS",
                      ("XAUUSDT,BTCUSDT" if group == "all" else "")).split(",")
            if s.strip())
        # momentum rotation ("Hot Slots"): every cycle re-scan and rotate any
        # slot whose symbol decayed, so grids ride the freshest 10m mover.
        self.rotate_enabled = int(os.getenv("GURU_ROTATE", "1"))
        self.rotate_scan_sec = float(os.getenv("GURU_ROTATE_SCAN_SEC", "300"))
        self.rotate_cooldown = float(os.getenv("GURU_ROTATE_COOLDOWN_SEC", "2700"))
        self.rotate_min_score = float(os.getenv("GURU_ROTATE_MIN_SCORE", "1.0"))
        self.rotate_improve_x = float(os.getenv("GURU_ROTATE_IMPROVE_X", "2.0"))
        self.rotate_top_k = int(os.getenv("GURU_ROTATE_TOP_K", "5"))
        self.rotations = 0
        self._rot_stop = threading.Event()
        self._rot_thread: Optional[threading.Thread] = None
        self._bots: Dict[str, GuruAIBot] = {}   # bot_id -> GuruAIBot
        self._lock = threading.Lock()
        # Manual spawn: same engine/safety, user picks ONE symbol, no scan/rotation.
        if group == "manual":
            self.rotate_enabled = 0
            self.n_bots = int(os.getenv("GURU_MANUAL_MAX", str(MAX_BINANCE_BOTS)))
            self.n_bots = max(1, min(MAX_BINANCE_BOTS, self.n_bots))
            self.fixed_symbols = ()
            self.priority = ()

    def active_bots(self) -> List[GuruAIBot]:
        with self._lock:
            return [b for b in self._bots.values() if b.status != BotStatus.STOPPED]

    def _bot_cls(self):
        """Bot class factory hook — the dynamic engine (dynamic_guruai.py)
        overrides this to spawn trend-biased bots. Neutral engine untouched."""
        return GuruAIBot

    def is_running(self) -> bool:
        return len(self.active_bots()) > 0

    # ── Hot Slots: momentum rotation ─────────────────────────────────────────
    def _rotation_loop(self):
        while not self._rot_stop.wait(self.rotate_scan_sec):
            try:
                self._rotate_cycle()
            except Exception as e:
                logger.warning(f"GuruAI[{self.group}] rotation cycle failed: {e}")

    def _rotate_cycle(self):
        """Re-rank the market and rotate any slot whose symbol decayed.

        Rules (re-checked every cycle):
        - a slot keeps its symbol while it stays within the scanned top-K or
          above the absolute score floor
        - the replacement is the best-ranked candidate; priority symbols
          (BTC/XAU) win ties BUT are dropped when ranked last in the scanned
          top list, and a better-ranked mover always beats them
        - never rotate while a leg is open (active_levels > 1), before the
          cooldown, or without a >=2x score improvement over a decaying symbol
        """
        bots = [b for b in self._bots.values() if b.status == BotStatus.RUNNING]
        ranked = _fetch_scan_pool(self.max_price)
        if not ranked:
            return
        # velocity-probe fallback: never rotate on stale/zero scores.
        if not _SCAN_CACHE.get("probe_ok", False):
            return
        score_of = {c["symbol"]: c["score"] for c in ranked}
        # cross-group safety: a slot must never rotate into a symbol any OTHER
        # group is already trading (double exposure doubles the bleed).
        in_use = _guru_in_use(self.user_id, self.env)
        # not-last guard for priority symbols (same rule as scan_top_coins).
        top = ranked[:max(len(bots) + 2, 6)]
        worst = top[-1]["symbol"] if top else ""
        if worst in self.priority:
            top = [c for c in top if c["symbol"] != worst]
        for bot in bots:
            sym = bot.symbol
            # fixed anchors never rotate out.
            if sym in self.fixed_symbols:
                continue
            # candidate = best-ranked symbol not already in use (a slot never
            # doubles up on another slot's symbol); priority boost mirrors
            # scan_top_coins (BTC/XAU win near-ties only).
            cand = None
            for c in sorted(top, key=lambda c: c["score"]
                            + (0.1 if c["symbol"] in self.priority else 0.0),
                            reverse=True):
                if c["symbol"] not in in_use and c["symbol"] != sym:
                    cand = c
                    break
            if not cand:
                continue
            if cand["symbol"] == sym:
                continue
            # decay gate: rotate only when the current symbol is dying.
            cur = score_of.get(sym, 0.0)
            if cur >= self.rotate_min_score and self._rank_of(score_of, sym) <= self.rotate_top_k:
                continue
            # improvement gate: the new leader must clearly beat the old one.
            if cur > 0 and cand["score"] < cur * self.rotate_improve_x:
                continue
            if time.time() - getattr(bot, "_rotated_at", 0.0) < self.rotate_cooldown:
                continue
            # rotation-safe: no open leg (level 1 only, both sides resting).
            if not bot.rotation_safe():
                continue
            self._do_rotate(bot, cand["symbol"], cur, cand["score"])
            in_use = {b.symbol for b in self._bots.values()
                      if b.status == BotStatus.RUNNING}
            in_use.add(cand["symbol"])

        # ── dead-bot replacement ──
        # Bots halted by equity stop / crash guard / individual loss stop are
        # STOPPED. Replace them with a fresh mover (unless the slot is a fixed
        # anchor or the symbol is in cooldown) so the fleet stays full.
        dead = [b for b in self._bots.values() if b.status == BotStatus.STOPPED]
        for bot in dead:
            sym = bot.symbol
            is_anchor = sym in self.fixed_symbols
            # anchors restart after a LONGER cooldown (2h) — the death loop
            # (stop -> 45min -> restart -> stop) burned taker fees each cycle.
            cd_sec = ANCHOR_COOLDOWN_SEC if is_anchor else SYM_COOLDOWN_SEC
            with _SYM_COOLDOWN_LOCK:
                cd = _SYM_COOLDOWN.get((self.user_id, self.env, sym), 0.0)
            if time.time() - cd < cd_sec:
                continue
            if is_anchor:
                # anchor slot: restart on the SAME symbol (fresh grid) —
                # anchors always keep their slot, they just re-ladder.
                self._do_rotate(bot, sym, score_of.get(sym, 0.0), 0.0)
                continue
            cand = None
            for c in sorted(top, key=lambda c: c["score"]
                            + (0.1 if c["symbol"] in self.priority else 0.0),
                            reverse=True):
                if c["symbol"] in in_use or c["symbol"] == sym:
                    continue
                with _SYM_COOLDOWN_LOCK:
                    ccd = _SYM_COOLDOWN.get((self.user_id, self.env, c["symbol"]), 0.0)
                if time.time() - ccd < SYM_COOLDOWN_SEC:
                    continue
                cand = c
                break
            if not cand:
                continue
            self._do_rotate(bot, cand["symbol"], score_of.get(sym, 0.0), cand["score"])
            in_use.discard(sym)
            in_use.add(cand["symbol"])

    @staticmethod
    def _rank_of(score_of: dict, sym: str) -> int:
        try:
            return sorted(score_of.values(), reverse=True).index(score_of[sym]) + 1
        except (ValueError, KeyError):
            return 99

    def _do_rotate(self, bot: GuruAIBot, new_sym: str, old_score: float, new_score: float):
        old_sym = bot.symbol
        alloc = bot.alloc_usd
        capacity = bot.capacity_usd
        free = bot.free_margin
        lev = bot.leverage
        try:
            bot.stop(close_positions=True)
        except Exception as e:
            logger.warning(f"GuruAI[{self.group}] rotation stop {old_sym} failed: {e}")
            return
        with self._lock:
            bid = next((k for k, v in self._bots.items() if v is bot), None)
            if bid:
                self._bots.pop(bid, None)
                try:
                    self.orch._bots.pop(bid, None)
                    self.orch._delete_from_registry(bid)
                    self.orch._registry.pop(bid, None)
                    bot.bridge.disconnect()
                except Exception:
                    pass
        _guru_release(self.user_id, self.env, old_sym)
        plat = (self.platform or "binance").lower()
        old_br = getattr(bot, "bridge", None)
        bridge = BinanceBridge(symbol=new_sym, environment=self.env,
                               market_type="futures", user_id=self.user_id)
        if not bridge.connect():
            logger.warning(f"GuruAI[{self.group}] rotation bridge failed {new_sym}")
            return
        tag = "MEME" if self.group == "meme" else "ALL"
        nb = self._bot_cls()(name=tag, symbol=new_sym, bridge=bridge, alloc_usd=alloc,
                             free_margin=free, leverage=lev, capacity_usd=capacity,
                             user_id=self.user_id,
                             is_anchor=new_sym in self.fixed_symbols,
                             vol_pct=getattr(self, "_run_vol", 50),
                             mode=getattr(self, "_run_mode", "scalp"),
                             compound=getattr(self, "_run_compound", None),
                             compound_frac=getattr(self, "_run_compound_frac", None))
        nb._rotated_at = time.time()
        with self._lock:
            nid = self.orch.register_external_bot(name=tag, symbol=new_sym,
                                                  bot=nb, user_id=self.user_id)
            nb.bot_id = nid
            self._bots[nid] = nb
        _guru_claim(self.user_id, self.env, new_sym)
        nb.start()
        self.rotations += 1
        logger.info(f"🔄 ROTATED {old_sym} → {new_sym} "
                    f"(score {old_score:.2f} → {new_score:.2f}) [{self.group}]")

    def spawn_symbol(self, symbol: str, name: str, free_margin: float = 0.0,
                     bridge=None, margin_frac: float = None, vol_pct: int = 50,
                     leverage=None, mode: str = None, compound=None,
                     compound_frac: float = None) -> dict:
        """User-picked single symbol. Same bot class and safety as GuruAI
        (trail/loss stops, bank, crash guard, gate). No scan, no rotation.

        margin_frac: fraction of futures wallet to size with (0.5 = 50%).
        GuruAI omits this and keeps usual split.
        vol_pct: Global slider 10-100% of available funds for this grid, default 50% → 0.5$ per 1% on 25$ linear.
        """
        from memory_guard import assert_platform_slots
        symbol = (symbol or "").strip().upper()
        plat = (getattr(bridge, "platform", None) or self.platform or "binance").lower()
        if bridge is None and plat == "binance":
            if symbol.endswith("USD") and not symbol.endswith("USDT"):
                symbol += "T"
        if not symbol:
            return {"ok": False, "error": "symbol required"}
        _propm = prop_flat_reason()
        if _propm:
            return {"ok": False, "error": f"prop discipline: {_propm} — spawning blocked"}
        if prop_mode():
            try:  # v1.0 production governor: refuse new books past soft limits
                from prod_guard import guard as _pguard, cached_snapshot
                _ss = cached_snapshot(self.env or "demo")
                _sv = _pguard().account_verdict(
                    _ss["desk_pnl"], _ss["floating"], _ss["legs"],
                    env=self.env or "demo",
                    per_symbol_float=_ss["per_symbol_float"])
                if not _sv.get("allow_new_entries", True):
                    return {"ok": False, "error":
                            f"guard: {_sv.get('reason', 'risk limit')} — spawning blocked"}
            except Exception:
                pass  # fail-open on guard errors (tick gate still protects)
        taken = set(_guru_in_use(self.user_id, self.env))
        try:
            taken |= self.orch.live_symbols(self.user_id, plat, self.env)
        except Exception:
            pass
        if symbol in taken:
            return {"ok": False, "error": f"{symbol} already running on this desk"}
        try:
            assert_platform_slots(self.orch, self.user_id, plat, extra=1)
        except FleetLimitError as e:
            return {"ok": False, "error": str(e)}
        free = float(free_margin) or 0.0
        leverage = clean_leverage(leverage if leverage is not None
                                  else os.getenv("BINANCE_LEVERAGE", "10"))
        # global slider 10-100% — keep for _size_qty scaling (50% → 1×, 100% → 2×)
        try:
            vol_pct = int(vol_pct if vol_pct is not None else os.getenv("GB_VOL_PCT", "50"))
        except:
            vol_pct = 50
        vol_pct = max(10, min(100, vol_pct))
        force_alloc = False
        _wf = wallet_use_frac()
        _cap_lev = cap_leverage(leverage)  # MAX resolves per-symbol after connect
        capacity_total = free * _cap_lev * _wf
        n = max(len(self.active_bots()) + 1, 1)
        per_bot_capacity = capacity_total / n
        alloc = min(max((free * _wf) / n, 6.0), per_bot_capacity)
        if plat == "binance" and per_bot_capacity < 2 * 5.0 * 1.05:
            return {"ok": False,
                    "error": f"insufficient margin (free=${free:.2f}) for one grid"}
        if bridge is None:
            bridge = BinanceBridge(symbol=symbol, environment=self.env,
                                   market_type="futures", user_id=self.user_id)
            if not bridge.connect():
                return {"ok": False, "error": f"Binance connect failed for {symbol}"}
        elif not getattr(bridge, "is_connected", False):
            if not bridge.connect():
                return {"ok": False, "error": f"{plat} connect failed for {symbol}"}
        if plat == "binance":
            try:
                leverage = bridge.resolve_leverage(leverage)
                bridge._set_leverage(leverage, force=True)
            except Exception:
                leverage = cap_leverage(leverage)
                pass
        _cmp = compound
        _cmp_frac = compound_frac
        bot = self._bot_cls()(name=name or symbol, symbol=symbol, bridge=bridge,
                              alloc_usd=alloc, free_margin=free, leverage=leverage,
                              capacity_usd=per_bot_capacity, user_id=self.user_id,
                              is_anchor=True, vol_pct=vol_pct, mode=mode,
                              compound=_cmp, compound_frac=_cmp_frac)
        if force_alloc:
            bot._force_alloc_size = True
        try:
            with self._lock:
                bot_id = self.orch.register_external_bot(
                    name=bot.name, symbol=symbol, bot=bot, user_id=self.user_id)
                bot.bot_id = bot_id
                self._bots[bot_id] = bot
        except DuplicateSymbolError:
            try:
                bridge.disconnect()
            except Exception:
                pass
            return {"ok": False, "error": f"{symbol} already running on this desk"}
        _guru_claim(self.user_id, self.env, symbol)
        bot.start()
        return {"ok": True, "bot_id": bot_id, "symbol": symbol}

    def start(self, n: int = None, free_margin: float = 0.0,
              exclude: tuple = (), include_anchors: bool = True,
              include_movers: bool = True, vol_pct: int = None,
              leverage=None, wallet_pct: int = None, mode: str = None,
              compound=None, compound_frac: float = None) -> dict:
        if self.group == "manual":
            return {"ok": False,
                    "error": "＋ Spawn is one symbol — use spawn_symbol, not GuruAI start"}
        if getattr(self, "platform", "binance") not in ("binance", ""):
            return {"ok": False, "error": "USDT-M scan start is Binance-only"}
        if self.is_running():
            return {"ok": False, "error": "GuruAI already running"}
        n = n or self.n_bots
        if self.group == "all":
            n = clamp_guru_n(n)
            self.n_bots = n
        self._run_compound = compound
        self._run_compound_frac = compound_frac
        slots = remaining_binance_slots(self.orch, self.user_id)
        if slots <= 0:
            return {"ok": False, "error": (
                f"Fleet cap is {MAX_BINANCE_BOTS} Binance bots — stop one first")}
        if n > slots:
            logger.warning(f"GuruAI: fleet cap {MAX_BINANCE_BOTS}, reducing n={n} -> {slots}")
            n = slots
            self.n_bots = n
        # Fixed anchors first (always kept), then the freshness scan fills the
        # remaining slots. Anchors bypass ranking entirely — they are the
        # stable core (e.g. XAUUSDT + BTCUSDT). include_anchors/include_movers
        # let the session lock start partial fleets.
        picks: List[dict] = []
        exclude = set(exclude)
        exclude.update(_guru_in_use(self.user_id, self.env))
        try:
            exclude.update(self.orch.live_symbols(self.user_id, self.platform, self.env))
        except Exception:
            pass
        ranked = _fetch_scan_pool(self.max_price)
        by_sym = {c["symbol"]: c for c in ranked}
        if include_anchors:
            for fs in self.fixed_symbols:
                if fs in exclude:
                    continue
                c = by_sym.get(fs)
                if c is None:
                    c = {"symbol": fs, "price": 0.0, "gain_pct": 0.0,
                         "quote_vol": 0.0, "gain_15m": 0.0, "gain_1h": 0.0,
                         "gain_4h": 0.0, "score": 0.0}
                picks.append(c)
                exclude.add(fs)
        n_scan = (n - len(picks)) if include_movers else 0
        # Velocity-probe fallback: when the probe fails we run the fixed
        # anchors ONLY (no mover picks) — never trade on stale/zero scores.
        probe_ok = bool(ranked) and bool(_SCAN_CACHE.get("probe_ok", False))
        if n_scan > 0 and probe_ok:
            # ── Tier 2: JEV news tier (most-talked-about, any price) ──
            # Tier order: anchors (Tier 1) → news (Tier 2) → runner (Tier 3) →
            # <$1 movers (Tier 4). Empty news/runner falls back to movers-only
            # (yesterday's behavior, unchanged).
            try:
                _news_n = int(os.getenv("GURU_NEWS_SLOTS", "2") or 2)
                _news_min = float(os.getenv("GURU_NEWS_MIN_ATT", "0.60") or 0.60)
            except (TypeError, ValueError):
                _news_n, _news_min = 2, 0.60
            if _news_n > 0 and (n - len(picks)) > 0:
                try:
                    import openrouter_client as _jgnews
                    _full = _fetch_scan_pool(0)  # any price: runners live above $1
                    _news = _jgnews.attention_scan(
                        [c for c in (_full or ranked) if c["symbol"] not in exclude],
                        self.user_id, self.env,
                        top_n=min(_news_n, n - len(picks)), min_p=_news_min)
                    for _nc in _news:
                        if _nc["symbol"] not in exclude:
                            picks.append(_nc)
                            exclude.add(_nc["symbol"])
                            logger.info(f"GuruAI[{self.group}]: NEWS tier pick {_nc['symbol']}")
                except Exception as _e:
                    logger.debug(f"news tier failed: {str(_e)[:100]}")
            # ── Tier 3: runner slot (top velocity overall, no price cap) ──
            try:
                _run_n = int(os.getenv("GURU_RUNNER_SLOTS", "1") or 1)
            except (TypeError, ValueError):
                _run_n = 1
            if _run_n > 0 and (n - len(picks)) > 0:
                try:
                    _full = _fetch_scan_pool(0)
                    _cands = sorted((_full or []),
                                    key=lambda c: abs(float(c.get("gain_15m", 0) or 0)),
                                    reverse=True)
                    for _rc in _cands[:_run_n]:
                        if _rc["symbol"] not in exclude:
                            picks.append(_rc)
                            exclude.add(_rc["symbol"])
                            logger.info(f"GuruAI[{self.group}]: RUNNER pick {_rc['symbol']} "
                                        f"({float(_rc.get('gain_15m', 0) or 0):+.2f}%/15m)")
                            break
                except Exception as _e:
                    logger.debug(f"runner slot failed: {str(_e)[:100]}")
            picks += scan_top_coins(limit=max(n - len(picks), 0), max_price=self.max_price,
                                    exclude=tuple(exclude), priority=self.priority,
                                    priority_min_score=self.priority_min_score)
        elif n_scan > 0 and not probe_ok and not self.fixed_symbols:
            return {"ok": False, "error": "velocity probe failed and no fixed anchors — refusing to start"}
        if n_scan > 0 and not probe_ok:
            logger.warning(f"GuruAI[{self.group}]: velocity probe failed — "
                           f"running anchors only ({len(picks)} bot(s))")
        if not picks:
            return {"ok": False, "error": "scanner found no qualifying coins"}
        _prop0 = prop_flat_reason()
        if _prop0:
            return {"ok": False, "error": f"prop discipline: {_prop0} — no new spawns"}
        # allocation: split available free margin conservatively
        free = float(free_margin) or 0.0
        leverage = clean_leverage(leverage if leverage is not None
                                  else os.getenv("BINANCE_LEVERAGE", "10"))
        self._run_vol = run_vol_pct(vol_pct)
        self._run_lev = leverage
        self._run_compound = compound
        self._run_compound_frac = compound_frac
        _m = (mode or os.getenv("GB_GRID_MODE", "scalp") or "scalp").strip().lower()
        self._run_mode = _m if _m in ("scalp", "swing", "auto") else "scalp"
        try:
            self._run_wf = max(5.0, min(100.0, float(wallet_pct))) / 100.0 \
                if wallet_pct is not None else wallet_use_frac()
        except (TypeError, ValueError):
            self._run_wf = wallet_use_frac()
        _wf = self._run_wf
        _cap_lev = cap_leverage(self._run_lev)
        capacity_total = free * _cap_lev * _wf
        # every bot needs at least one level per side -> 2 x min_notional of capacity.
        # shrink n to what the account can actually fund; refuse to run if not even one.
        min_bot_capacity = 2 * 5.0 * 1.05  # ~$10.5 notional for a minimal 1+1 grid
        n_fit = int(capacity_total / min_bot_capacity)
        if n_fit < 1:
            return {"ok": False,
                    "error": f"GuruAI: insufficient margin (free=${free:.2f} x lev={leverage:.0f} = "
                             f"${capacity_total:.2f} capacity, need >=${min_bot_capacity:.2f} for one bot). "
                             f"Top up the account or raise BINANCE_LEVERAGE."}
        if n > n_fit:
            logger.warning(f"GuruAI: account can only fund {n_fit} bot(s), reducing n={n} -> {n_fit}")
            n = n_fit
        picks = picks[:n]
        try:  # Layer 1 (JEV_SCAN=1): re-rank velocity picks by microstructure
            import openrouter_client as _jg
            picks = _jg.apply_scan_filter(picks, self.user_id, self.env)
        except Exception:
            pass
        per_bot_capacity = capacity_total / n
        alloc = min(max((free * _wf) / max(len(picks), 1), 6.0), per_bot_capacity)

        started = self._spawn_bots(picks, free, leverage, vol_pct=self._run_vol,
                                     mode=self._run_mode, compound=self._run_compound,
                                     compound_frac=self._run_compound_frac)
        if self.rotate_enabled and started:
            self._ensure_rotation_thread()
        return {"ok": bool(started), "started": started,
                "alloc_each": round(alloc, 2)}

    def _spawn_bots(self, picks: List[dict], free: float, leverage,
                    vol_pct: int = None, wallet_pct: int = None,
                    mode: str = None, compound=None,
                    compound_frac: float = None) -> list:
        """Create, register and start bots for the given picks. Shared by
        start / start_anchors / start_movers; sizing basis is the whole
        fleet (capacity_total / n_bots) so session-spawned bots match the
        full-fleet sizing."""
        leverage = clean_leverage(leverage)
        _cap_lev = cap_leverage(leverage)  # MAX resolves per-symbol after connect
        vol_pct = run_vol_pct(vol_pct if vol_pct is not None
                              else getattr(self, "_run_vol", None))
        _cmp = (compound if compound is not None
                else getattr(self, "_run_compound", None))
        _cmp_frac = (compound_frac if compound_frac is not None
                     else getattr(self, "_run_compound_frac", None))
        _m = (mode or getattr(self, "_run_mode", None)
              or os.getenv("GB_GRID_MODE", "scalp") or "scalp").strip().lower()
        _mode = _m if _m in ("scalp", "swing", "auto") else "scalp"
        try:
            _wf = max(5.0, min(100.0, float(wallet_pct))) / 100.0 \
                if wallet_pct is not None \
                else float(getattr(self, "_run_wf", 0) or 0) or wallet_use_frac()
        except (TypeError, ValueError):
            _wf = wallet_use_frac()
        capacity_total = free * _cap_lev * _wf
        per_bot_capacity = capacity_total / max(self.n_bots, 1)
        alloc = min(max((free * _wf) / max(self.n_bots, 1), 6.0), per_bot_capacity)
        started = []
        tag = "MEME" if self.group == "meme" else "ALL"
        with self._lock:
            for c in picks:
                if any(b.symbol == c["symbol"] for b in self._bots.values()):
                    continue
                if c["symbol"] in _guru_in_use(self.user_id, self.env):
                    continue
                if self.orch.live_symbol_owner(c["symbol"], self.user_id,
                                               self.platform, self.env):
                    continue
                if remaining_binance_slots(self.orch, self.user_id) <= 0:
                    logger.warning(
                        f"GuruAI: Binance fleet cap {MAX_BINANCE_BOTS} reached — "
                        f"not spawning {c['symbol']}")
                    break
                bridge = BinanceBridge(symbol=c["symbol"], environment=self.env,
                                       market_type="futures", user_id=self.user_id)
                if not bridge.connect():
                    logger.warning(f"GuruAI: bridge connect failed {c['symbol']}")
                    continue
                try:  # Layer 2 (JEV_GATE=1): veto symbols JEV strongly opposes
                    import openrouter_client as _jg
                    if not _jg.entry_ok(c["symbol"], self.user_id, self.env,
                                        float(c.get("gain_15m", c.get("gain_pct", 0)) or 0)):
                        logger.info(f"GuruAI: JEV entry veto {c['symbol']} — skipping spawn")
                        try:
                            bridge.disconnect()
                        except Exception:
                            pass
                        continue
                except Exception:
                    pass
                try:
                    _lev = bridge.resolve_leverage(leverage)
                    bridge._set_leverage(_lev, force=True)
                except Exception:
                    _lev = cap_leverage(leverage)
                    pass
                bot = self._bot_cls()(name=f"{tag} {len(self._bots) + 1}",
                                      symbol=c["symbol"], bridge=bridge, alloc_usd=alloc,
                                      free_margin=free, leverage=_lev,
                                      capacity_usd=per_bot_capacity,
                                      user_id=self.user_id,
                                      is_anchor=c["symbol"] in self.fixed_symbols,
                                      vol_pct=vol_pct, mode=_mode,
                                      compound=_cmp, compound_frac=_cmp_frac)
                try:
                    bot_id = self.orch.register_external_bot(
                        name=bot.name, symbol=c["symbol"], bot=bot, user_id=self.user_id)
                except DuplicateSymbolError:
                    try:
                        bridge.disconnect()
                    except Exception:
                        pass
                    continue
                bot.bot_id = bot_id
                self._bots[bot_id] = bot
                _guru_claim(self.user_id, self.env, c["symbol"])
                bot.start()
                started.append({"bot_id": bot_id, "symbol": c["symbol"],
                                "gain": c.get("gain_pct", 0.0)})
        return started

    def _ensure_rotation_thread(self):
        """(Re)start the rotation supervisor if it is not alive. stop_all()
        kills it; start paths bring it back."""
        if not self.rotate_enabled:
            return
        if self._rot_thread is None or not self._rot_thread.is_alive():
            self._rot_stop.clear()
            self._rot_thread = threading.Thread(target=self._rotation_loop,
                                                daemon=True, name=f"guru-rot-{self.group}")
            self._rot_thread.start()
            logger.info(f"GuruAI[{self.group}] rotation supervisor started "
                        f"(every {self.rotate_scan_sec:.0f}s, cooldown {self.rotate_cooldown:.0f}s)")

    def start_movers(self, free_margin: float = 0.0) -> dict:
        """Start mover slots only (session-lock wake at 08:00 while anchors
        keep running). Picks from the freshness scan, excluding in-use. The
        scan cache is invalidated first so the wake always picks from a
        fresh-as-of-now pool (not a <=120s-old snapshot)."""
        if self.group == "manual":
            return {"ok": False, "error": "manual spawn does not auto-fill movers"}
        free = float(free_margin) or 0.0
        leverage = float(os.getenv("BINANCE_LEVERAGE", "10")) or 10.0
        movers_present = sum(1 for b in self._bots.values()
                             if b.symbol not in self.fixed_symbols)
        n = self.n_bots - len(self.fixed_symbols) - movers_present
        if n <= 0:
            return {"ok": False, "error": "no free mover slots"}
        _SCAN_CACHE["at"] = 0.0   # force a fresh pool at the session wake
        ranked = _fetch_scan_pool(self.max_price)
        probe_ok = bool(ranked) and bool(_SCAN_CACHE.get("probe_ok", False))
        if not probe_ok:
            return {"ok": False, "error": "velocity probe failed — refusing mover start"}
        exclude = tuple(_guru_in_use(self.user_id, self.env))
        picks = scan_top_coins(limit=n, max_price=self.max_price, exclude=exclude,
                               priority=self.priority,
                               priority_min_score=self.priority_min_score)
        try:  # Layer 1 (JEV_SCAN=1): re-rank velocity picks by microstructure
            import openrouter_client as _jg
            picks = _jg.apply_scan_filter(picks, self.user_id, self.env)
        except Exception:
            pass
        if not picks:
            return {"ok": False, "error": "scanner found no qualifying coins"}
        started = self._spawn_bots(picks, free, leverage)
        if started:
            self._ensure_rotation_thread()
        return {"ok": bool(started), "started": started}

    def start_anchors(self, free_margin: float = 0.0) -> dict:
        """Start fixed-anchor slots only (XAUUSDT 24/7 on weekdays)."""
        if self.group == "manual":
            return {"ok": False, "error": "manual spawn does not auto-start anchors"}
        free = float(free_margin) or 0.0
        leverage = float(os.getenv("BINANCE_LEVERAGE", "10")) or 10.0
        picks = [{"symbol": fs, "price": 0.0, "gain_pct": 0.0,
                  "quote_vol": 0.0, "gain_15m": 0.0, "gain_1h": 0.0,
                  "gain_4h": 0.0, "score": 0.0}
                 for fs in self.fixed_symbols
                 if not any(b.symbol == fs for b in self._bots.values())]
        if not picks:
            return {"ok": False, "error": "anchors already present"}
        started = self._spawn_bots(picks, free, leverage)
        if started:
            self._ensure_rotation_thread()
        return {"ok": bool(started), "started": started}

    def stop_all(self) -> int:
        self._rot_stop.set()
        if self._rot_thread:
            self._rot_thread.join(timeout=5)
            self._rot_thread = None
        n = 0
        with self._lock:
            for bot_id, bot in list(self._bots.items()):
                try:
                    bot.stop(close_positions=True)
                    _guru_release(self.user_id, self.env, bot.symbol)
                    n += 1
                except Exception:
                    pass
            self._bots.clear()
        return n

    def prune(self):
        """Drop stopped bots from the map AND the orchestrator registry so they
        disappear from the side panel (button re-enables when none remain)."""
        with self._lock:
            for bot_id in [bid for bid, b in self._bots.items() if b.status == BotStatus.STOPPED]:
                bot = self._bots.pop(bot_id, None)
                if bot is not None and not bot.is_alive():
                    # remove from orchestrator registry + live map so UI clears it
                    try:
                        self.orch._bots.pop(bot_id, None)
                        self.orch._delete_from_registry(bot_id)
                        self.orch._registry.pop(bot_id, None)
                        _guru_release(self.user_id, self.env, bot.symbol)
                        if bot.bridge:
                            bot.bridge.disconnect()
                    except Exception:
                        pass


# Singleton (wired in server.py)
guru_manager: Optional[GuruAIManager] = None

def init_guru(orchestrator, env: str = "demo", user_id: str = None,
              group: str = "all", max_price: float = None,
              n_bots: int = None, platform: str = "binance") -> GuruAIManager:
    global guru_manager
    guru_manager = GuruAIManager(orchestrator, env, user_id=user_id,
                                 group=group, max_price=max_price, n_bots=n_bots,
                                 platform=platform)
    return guru_manager
