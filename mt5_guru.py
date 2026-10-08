#!/usr/bin/env python3
"""MT5 GuruAI controller — DynamicGuruAI on a single chart symbol with
FTMO-style risk governance.

Wraps ONE DynamicGuruAIBot (bias engine + LLM advisor + asymmetric ladder
+ initial leg + full safety stack) over an MT5Bridge, and adds the
supervision layer that Binance's multi-bot bank supervisor would provide:

  FTMO risk layer (env-tunable):
    GURU_MT5_DAILY_LOSS_PCT    5.0   day PnL <= -x% of day-start -> flatten,
                                     stop, auto-restart next trading day
    GURU_MT5_DAILY_PROFIT_PCT  5.0   day PnL >= +x% -> flatten (bank);
                                     DEMO keeps trading, LIVE stops until
                                     next day
    GURU_MT5_MAX_LOSS_PCT      10.0  equity <= (1-x) x initial_balance ->
                                     flatten + PERMANENT halt (manual reset)
    GURU_MT5_RISK_PCT          1.5   sizing: worst-case ladder loss budget
                                     as % of balance (start small!)
    GURU_MT5_SESSION_START/END 8/22  trading window UTC (.env)
    GURU_MT5_ALLOW_LIVE        0     LIVE trading requires explicit opt-in

  Day boundary: 22:00 UTC (FTMO midnight CEUT). State persists in Supabase
  (guru_mt5_risk_state) so restarts never lose the day/overall baselines.
"""
import json
import os
import threading
import time
import logging
from datetime import datetime, timezone

logger = logging.getLogger("hybrid.guruai.mt5")

RISK_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               ".mt5_risk_state.json")

DAY_LOSS_PCT = float(os.getenv("GURU_MT5_DAILY_LOSS_PCT", "5.0"))
DAY_PROFIT_PCT = float(os.getenv("GURU_MT5_DAILY_PROFIT_PCT", "5.0"))
MAX_LOSS_PCT = float(os.getenv("GURU_MT5_MAX_LOSS_PCT", "10.0"))
RISK_PCT = float(os.getenv("GURU_MT5_RISK_PCT", "1.5"))
SAFETY = 0.7                      # budget headroom inside the daily cap
SESSION_START = int(os.getenv("GURU_MT5_SESSION_START", "8"))
SESSION_END = int(os.getenv("GURU_MT5_SESSION_END", "22"))
ALLOW_LIVE = os.getenv("GURU_MT5_ALLOW_LIVE", "0") == "1"
DAY_BOUNDARY_HOUR = int(os.getenv("GURU_MT5_DAY_BOUNDARY_UTC", "22"))

# ── Trailing profit lock (ported from the Binance bank layer, 2026-08-21).
# The +$11 run had NO protection on MT5 — the Binance-era trail lives in the
# bank supervisor which reads the Binance account API. MT5 edition polls the
# EA link state instead. Same tiers: arm -> giveback 20% (min $0.25 room),
# tight trail above the tier-2 threshold.
MT5_TRAIL_ARM_USD = float(os.getenv("GURU_BOT_TRAIL_ARM_USD", "1.54"))
MT5_TRAIL_ARM_PCT = float(os.getenv("GURU_MT5_TRAIL_ARM_PCT", "1.5")) / 100.0
MT5_TRAIL_GIVEBACK = float(os.getenv("GURU_BOT_TRAIL_GIVEBACK", "0.2"))
MT5_TRAIL_MIN_USD = float(os.getenv("GURU_BOT_TRAIL_MIN_USD", "0.25"))
MT5_TRAIL_TIER2_USD = float(os.getenv("GURU_BOT_TRAIL_TIER2_USD", "5.00"))
MT5_TRAIL_TIER2_TRAIL = float(os.getenv("GURU_BOT_TRAIL_TIER2_TRAIL", "0.25"))

# ── FTMO discipline layer (from ftmo.com breach-prevention guidance +
# winner stories, 2026-08-22): your OWN limits tighter than the account's.
MT5_SOFT_DAILY_PCT = float(os.getenv("GURU_MT5_SOFT_DAILY_PCT", "2.5"))  # half the hard -5%
MAX_CONSEC_LOSSES = int(os.getenv("GURU_MT5_MAX_CONSEC_LOSSES", "3"))    # losing cycles -> stop for day
XAU_ASIA_WINDOW = os.getenv("GURU_MT5_XAU_ASIA_WINDOW", "1") == "1"      # Asia liquidity 20:00-03:00 UTC

_LOCK = threading.Lock()


def _day_key(now=None) -> str:
    """Trading-day key: days roll at DAY_BOUNDARY_HOUR UTC."""
    now = now or datetime.now(timezone.utc)
    d = now
    if now.hour < DAY_BOUNDARY_HOUR:
        from datetime import timedelta
        d = now - timedelta(days=1)
    return d.strftime("%Y-%m-%d")


class MT5RiskState:
    """Persisted FTMO governance state (local file; survives restarts)."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self._path = RISK_STATE_FILE
        self._lock = threading.Lock()
        self.st = self._load()

    def _load(self) -> dict:
        try:
            with open(self._path) as f:
                all_states = json.load(f)
            return all_states.get(self.user_id, {})
        except Exception:
            return {}

    def save(self):
        try:
            with self._lock:
                try:
                    with open(self._path) as f:
                        all_states = json.load(f)
                    if not isinstance(all_states, dict):
                        all_states = {}
                except (FileNotFoundError, json.JSONDecodeError):
                    all_states = {}          # first-ever save (or corrupt file)
                all_states[self.user_id] = self.st
                tmp = self._path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(all_states, f)
                import os as _os
                _os.replace(tmp, self._path)
        except Exception as e:
            logger.warning(f"mt5 risk state save failed: {e}")

    def get(self, key, default=None):
        return self.st.get(key, default)

    def set(self, key, value):
        with self._lock:
            self.st[key] = value


def mt5_sizing(balance: float, contract_size: float, price: float,
               lot_min: float, lot_step: float, levels: int) -> dict:
    """FTMO-safe sizing: worst-case ladder loss (all levels filled, price at
    the crash-guard distance) must stay inside the daily-loss budget.

      budget       = balance x DAILY_LOSS_PCT x SAFETY
      crash_pct    = GURU_MT5_CRASH_PCT (tightened for multi-level grids)
      loss_per_lot = contract_size x price x crash_pct
      max_total    = budget / loss_per_lot
      qty_level    = floor(max_total / levels) floored to lot_step

    Reduces `levels` before exceeding lot_min — start small, no gambling.
    """
    crash = float(os.getenv("GURU_MT5_CRASH_PCT", "3.5")) / 100.0
    budget = balance * (DAY_LOSS_PCT / 100.0) * SAFETY
    loss_per_lot = contract_size * price * crash
    if loss_per_lot <= 0 or levels <= 0:
        return {"qty": lot_min, "levels": 1, "budget": budget}
    max_total = budget / loss_per_lot
    while levels > 1 and (max_total / levels) < lot_min:
        levels -= 1
    qty = max_total / levels
    qty = max((int(qty / lot_step + 1e-9)) * lot_step, 0.0)
    if qty < lot_min:
        # even one min-lot leg breaches the budget — clamp to lot_min and
        # shrink levels to 1 (the honest minimum), flag via budget ratio
        qty = lot_min
        levels = 1
    return {"qty": round(qty, 8), "levels": levels, "budget": budget}


class MT5GuruController:
    """DynamicGuruAI on one MT5 chart symbol + optional FTMO risk supervision.

    ftmo_rules=True (default): daily/overall loss, profit bank, consecutive-loss
    breaker, Asia gold window — the prop-challenge layer.
    ftmo_rules=False: Binance-style clean GuruAI (regime gate + crash + trail).
    EA heartbeat is always on (link infra, not an FTMO rule).
    """

    def __init__(self, user_id: str, token: str, symbol: str, env: str = "demo",
                 ftmo_rules: bool = True, require_heartbeat: bool = True):
        from bridge_mt5 import MT5Bridge
        from dynamic_guruai import DynamicGuruAIBot
        from services import mt5_link

        if not symbol:
            raise RuntimeError("no symbol selected")
        self.user_id = user_id
        self.env = env
        self.token = token
        self.symbol = symbol.strip().upper()
        self.ftmo_rules = bool(ftmo_rules)
        self.risk = MT5RiskState(user_id)

        # Live start needs a fresh EA POST. Resume after pm2 uses persisted
        # chart names so we can re-attach before the next /state arrives.
        assigned = (mt5_link.assigned_symbols(token) if require_heartbeat
                    else mt5_link.known_chart_symbols(token))
        if self.symbol not in assigned:
            raise RuntimeError(
                f"No EA attached to {self.symbol} — attach the HybridGB EA "
                f"to that chart in MT5 and try again")

        self.bridge = MT5Bridge(token=token, symbol=self.symbol,
                                environment=env, user_id=user_id)
        if not self.bridge.connect(require_heartbeat=require_heartbeat):
            raise RuntimeError("MT5 EA not connected — check terminal/token")
        si = self.bridge.get_symbol_info()
        contract_size = float(si.get("contract_size", 100.0) or 100.0)

        acct = mt5_link_account(self.user_id, env)
        balance = float(acct.get("balance", 0) or 0)
        free_margin = float(acct.get("freeMargin", 0) or balance)
        leverage = int(acct.get("leverage", 100) or 100)

        alloc_usd = max(free_margin * 0.25, 10.0)   # conservative share
        last_px = (self.bridge.get_tick() or {}).get("last", 0.0) or 1.0

        self.bot = DynamicGuruAIBot(
            name=f"MT5 {self.symbol}", symbol=self.symbol, bridge=self.bridge,
            alloc_usd=alloc_usd, free_margin=free_margin, leverage=leverage,
            capacity_usd=free_margin * leverage * 0.7, user_id=user_id)

        if self.ftmo_rules:
            sized = mt5_sizing(balance, contract_size, last_px,
                               float(si.get("min_qty", 0.01) or 0.01),
                               float(si.get("qty_step", 0.01) or 0.01),
                               levels=int(os.getenv("GURU_LEVELS", "3")))
            self.bot.crash_guard_pct = float(
                os.getenv("GURU_MT5_CRASH_PCT", "3.5")) / 100.0
            os.environ[f"GB_QTY_{self.symbol}"] = str(sized["qty"])
            os.environ[f"GB_LEVELS_{self.symbol}"] = str(sized["levels"])
            self.bot._size_qty()
        else:
            sized = {"qty": self.bot.qty, "levels": self.bot.grid_levels,
                     "budget": free_margin}
            self.bot.crash_guard_pct = float(os.getenv("GURU_CRASH_GUARD", "0.05"))

        self._stop = threading.Event()
        self._thread: threading.Thread = None
        self._trail_peak = 0.0                # trailing profit lock state
        self._pause_reason = None             # window | session | ea-offline
        self.halted = bool(self.risk.get("halted", False))
        logger.info(f"[MT5-GURU] ready sym={self.symbol} qty={sized['qty']} "
                    f"levels={sized['levels']} budget=${sized['budget']:.0f} "
                    f"env={env} halted={self.halted} ftmo={self.ftmo_rules}")

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self):
        if self.ftmo_rules and self.halted:
            logger.warning("[MT5-GURU] start refused — overall max loss halt "
                           "is active (manual reset required)")
            return False
        self.bot.start()
        # register with the orchestrator so the side panel tracks the bot
        try:
            from orchestrator import orchestrator as orch
            bid = orch.register_external_bot(
                name=f"MT5 {self.symbol}", symbol=self.symbol, bot=self.bot,
                user_id=self.user_id,
                config={"kind": "mt5_guru", "ftmo_rules": self.ftmo_rules})
            self.bot.bot_id = bid
            self.bot_id = bid
        except Exception as e:
            logger.warning(f"mt5 guru register failed: {e}")
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervise, daemon=True,
                                        name="mt5-guru-supervise")
        self._thread.start()
        return True

    def resume(self, bot_id: str, orch=None) -> bool:
        """Re-attach after process restart without minting a new bot_id.

        Does not flatten — the EA still holds the ladder. The bot tick
        reconciles against exchange-truth open orders."""
        if self.ftmo_rules and self.halted:
            logger.warning("[MT5-GURU] resume refused — overall max loss halt")
            return False
        self.bot.bot_id = bot_id
        self.bot_id = bot_id
        try:
            from orchestrator import BotStatus
            from orchestrator import orchestrator as _orch
            orch = orch or _orch
            with orch._lock:
                orch._bots[bot_id] = self.bot
                entry = orch._registry.get(bot_id)
                if entry is not None:
                    entry.status = BotStatus.RUNNING
                    orch._save_registry(bot_id)
        except Exception as e:
            logger.warning(f"mt5 guru resume register failed: {e}")
        self.bot.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervise, daemon=True,
                                        name="mt5-guru-supervise")
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        try:
            self.bot.stop(close_positions=True)
        except Exception as e:
            logger.warning(f"mt5 guru bot stop failed: {e}")
        # deregister from the orchestrator (side panel cleanup)
        try:
            from orchestrator import orchestrator as orch
            bid = getattr(self, "bot_id", None)
            if bid:
                with orch._lock:
                    orch._bots.pop(bid, None)
                    orch._registry.pop(bid, None)
                orch._delete_from_registry(bid)
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=12)

    @property
    def running(self) -> bool:
        return self.bot.status.value == "running" or (
            self._thread is not None and self._thread.is_alive())

    # ── supervisor: FTMO guards + heartbeat watchdog + trail lock ────────
    def _supervise(self):
        while not self._stop.wait(10):
            try:
                self._supervise_once()
            except Exception as e:
                logger.warning(f"mt5 supervise err: {e}")

    def _supervise_once(self):
        from services import mt5_link as L
        now = datetime.now(timezone.utc)

        # ── trading window (08-22 UTC default + optional XAU Asia extension
        #    20:00-03:00 UTC — Gold/Silver liquidity window per FTMO's
        #    precious-metals winner story) + weekend gate ──
        hour = now.hour
        weekend = now.weekday() >= 5
        in_window = (SESSION_START <= hour < SESSION_END)
        if not in_window and self.ftmo_rules and XAU_ASIA_WINDOW and not weekend:
            in_window = hour >= 20 or hour < 3
        if weekend:
            in_window = False

        acct = mt5_link_account(self.user_id, self.env)
        equity = float(acct.get("equity", 0) or 0)
        balance = float(acct.get("balance", 0) or 0)

        # ── EA heartbeat watchdog (runs even when window-blocked) ──
        last_seen = 0.0
        link = L.get_link(self.token)
        if link:
            ps = link["per_symbol"].get(self.symbol) or {}
            last_seen = ps.get("last_seen", link.get("last_seen", 0))
        ea_stale = (time.time() - last_seen) > L.EA_STALE_S
        ea_off = float(self.risk.get("ea_offline_since", 0.0) or 0.0)
        if ea_stale and not ea_off:
            self.risk.set("ea_offline_since", time.time())
            self.risk.save()
        elif not ea_stale and ea_off:
            self.risk.set("ea_offline_since", 0.0)
            self.risk.save()

        # ── single pause/resume authority ──
        blocked = weekend or (not in_window) or ea_stale
        reason = ("weekend" if weekend else
                  "session" if not in_window else "ea-offline")
        if blocked and self.bot.status.value == "running":
            self.bot.pause()
            self._pause_reason = reason
            self._log(f"🌙 paused [{reason}]")
        elif (not blocked) and self.bot.status.value == "paused" \
                and self._pause_reason in (None, "session", "weekend",
                                           "ea-offline"):
            dur = int(time.time() - ea_off) if ea_off else 0
            self.bot.resume()
            self._pause_reason = None
            self._log(f"▶ resumed (EA offline {dur}s)" if ea_off else "▶ resumed")

        # ── session rest (dead tape): flat + (rest-hours clock OR dead market)
        # ⇒ pause grid legs; trail/stops/this supervisor keep running.
        # GB_SESSION_REST default ON (unset = ON); 0 disables. Mirrors Binance.
        try:
            _has_pos = True  # unknown: never rest blind
            try:
                _has_pos = any(abs(float(p.get("volume", 0) or 0)) > 0
                               for p in (self.bridge.get_positions() or []))
            except Exception:
                pass
            _rest = False
            if (os.getenv("GB_SESSION_REST", "1") or "1").strip() != "0" and not _has_pos:
                _rest = _mt5_dead_tape(self.bridge)
            if _rest and self.bot.status.value == "running":
                self.bot.pause()
                self._pause_reason = "dead-tape"
                self._log("😴 session rest — dead tape, placements sleeping")
            elif (not _rest) and self.bot.status.value == "paused" \
                    and self._pause_reason == "dead-tape":
                self.bot.resume()
                self._pause_reason = None
                self._log("▶ resumed — tape alive again")
        except Exception as e:
            logger.debug(f"mt5 session-rest: {e}")

        # prolonged EA loss: flatten + self-stop
        if ea_stale and ea_off and time.time() - ea_off > L.EA_LOST_S \
                and self.bot.status.value in ("running", "paused"):
            self._flatten_stop("🛑 EA connection lost — bots stopped. "
                               "Close leftover positions/orders in MT5 yourself.")
            self._stop.set()
            return

        # ── trailing profit lock (runs while running OR paused-with-position:
        #    held overnight positions stay protected) ──
        st5 = L.get_state(self.token, self.symbol)
        upnl = sum(float(p.get("profit", 0) or 0)
                   for p in st5.get("positions_data", []))
        last5 = float((self.bridge.get_tick() or {}).get("last", 0) or 0)
        notional = sum(abs(float(p.get("volume", 0) or 0)) * last5
                       for p in st5.get("positions_data", []))
        if upnl <= 0.03 or notional <= 0:
            self._trail_peak = 0.0
        else:
            self._trail_peak = max(self._trail_peak, upnl)
            arm = min(MT5_TRAIL_ARM_USD, MT5_TRAIL_ARM_PCT * notional)
            if self._trail_peak >= arm:
                if self._trail_peak >= MT5_TRAIL_TIER2_USD:
                    floor = max(MT5_TRAIL_TIER2_USD,
                                self._trail_peak - MT5_TRAIL_TIER2_TRAIL)
                else:
                    room = max(MT5_TRAIL_MIN_USD,
                               self._trail_peak * MT5_TRAIL_GIVEBACK)
                    floor = max(0.0, self._trail_peak - room)
                if floor > 0 and upnl <= floor:
                    logger.warning(
                        f"[MT5-GURU] 🔒 TRAIL LOCK {self.symbol}: "
                        f"${upnl:+.2f} <= floor ${floor:.2f} "
                        f"(peak ${self._trail_peak:.2f}) — closing")
                    self.bot.stop(close_positions=True)
                    self._log(f"🔒 trail locked ~${upnl:+.2f} — "
                              f"re-spawn to continue")
                    self._trail_peak = 0.0

        # ── JEV CUT (rates-adapted, no book on MT5): losing + old + strongly
        # opposed → flatten. Winners/trail above untouched. Fail-open. Fires on
        # real JEV only, never the mock stand-in.
        try:
            if upnl < -0.05 and notional > 0:
                _raw = st5.get("positions_data", []) or []
                _signed = sum((1 if str(p.get("type", "BUY")).upper() == "BUY" else -1)
                              * abs(float(p.get("volume", 0) or 0)) for p in _raw)
                _side = 1 if _signed > 0 else (-1 if _signed < 0 else 0)
                _ts = [int(p.get("time", 0) or 0) for p in _raw if p.get("time")]
                _age = time.time() - min(_ts) if _ts else 0
                if _side and _age >= 90:
                    import openrouter_client as _jg
                    if _jg.mt5_unwind_vote(self.bridge, _side, upnl, _age) == "close":
                        self._flatten_stop(
                            f"🛡 JEV CUT — flattened {self.symbol} "
                            f"(microstructure opposed, upnl ${upnl:+.2f})")
                        return
        except Exception as e:
            logger.debug(f"mt5 jev-cut: {e}")

        # ── day roll ──
        dk = _day_key(now)
        if self.risk.get("day_key") != dk:
            self.risk.set("day_key", dk)
            self.risk.set("day_start_equity", equity)
            self.risk.save()
            self._log(f"🌅 new trading day {dk} — day-start equity ${equity:.2f}")
            self.risk.set("consec_losses", 0)
            if self.risk.get("stopped_for_day"):
                self.risk.set("stopped_for_day", False)
                self.risk.save()
                self._log("🌅 daily stop lifted — trading resumes")
            if not self.bot.is_alive() and not self.halted \
                    and not self.risk.get("stopped_for_day"):
                self.bot.start()
                self._log("🌅 new day — grid restarted")

        day_start = float(self.risk.get("day_start_equity", equity) or equity)
        initial = float(self.risk.get("initial_balance", 0) or 0)
        if initial <= 0 and balance > 0:
            self.risk.set("initial_balance", balance)
            self.risk.save()
            initial = balance
        day_pnl = equity - day_start

        if not self.ftmo_rules:
            self.risk.set("last_cycles", int(self.bot.cycles or 0))
            self.risk.set("last_realized", float(self.bot.realized_pnl or 0))
            return

        # ── soft internal daily stop (FTMO rule #5: own limit < account's) ──
        soft_dn = day_start * (MT5_SOFT_DAILY_PCT / 100.0)
        if day_pnl <= -soft_dn and not self.risk.get("stopped_for_day"):
            self._flatten_stop(f"🟠 SOFT DAILY STOP: day PnL ${day_pnl:+.2f} "
                               f"<= -${soft_dn:.2f} (-{MT5_SOFT_DAILY_PCT}% of "
                               f"${day_start:.2f}) — half the daily cap used; "
                               f"stopped until next day")
            self.risk.set("stopped_for_day", True)
            self.risk.save()
            return

        # ── overall max loss (permanent) ──
        if initial > 0 and equity <= initial * (1 - MAX_LOSS_PCT / 100.0):
            self._flatten_stop("🌊 OVERALL MAX LOSS: equity ${:.2f} <= {:.0f}% "
                               "of initial ${:.2f} — challenge breached".format(
                                   equity, MAX_LOSS_PCT, initial))
            self.risk.set("halted", True)
            self.risk.set("halt_reason", f"overall max loss {MAX_LOSS_PCT}%")
            self.risk.save()
            self.halted = True
            self.stop()
            return

        # ── daily hard loss guard ──
        limit_dn = day_start * (DAY_LOSS_PCT / 100.0)
        if day_pnl <= -limit_dn and not self.risk.get("stopped_for_day"):
            self._flatten_stop(f"🛑 DAILY LOSS LIMIT: day PnL ${day_pnl:+.2f} "
                               f"<= -${limit_dn:.2f} (-{DAY_LOSS_PCT}% of "
                               f"${day_start:.2f}) — stopped until next day")
            self.risk.set("stopped_for_day", True)
            self.risk.save()
            return

        # ── consecutive-loss circuit breaker (FTMO rule #5) ──
        cyc = int(self.bot.cycles or 0)
        real = float(self.bot.realized_pnl or 0)
        lc = self.risk.get("last_cycles")
        lr = self.risk.get("last_realized")
        if lc is not None and cyc > int(lc):
            seg = real - float(lr or 0)
            consec = int(self.risk.get("consec_losses", 0) or 0)
            if seg < -0.005:
                consec += 1
                self.risk.set("consec_losses", consec)
                self.risk.save()
                if consec >= MAX_CONSEC_LOSSES \
                        and self.bot.status.value == "running":
                    self._flatten_stop(
                        f"🔴 CIRCUIT BREAKER: {consec} consecutive losing "
                        f"cycle(s) — stopped until next day")
                    self.risk.set("stopped_for_day", True)
                    self.risk.save()
                    return
            elif consec:
                self.risk.set("consec_losses", 0)
                self.risk.save()
        self.risk.set("last_cycles", cyc)
        self.risk.set("last_realized", real)
        self.risk.save()

        # ── daily profit bank (LIVE stops for the day; DEMO keeps trading) ──
        limit_up = day_start * (DAY_PROFIT_PCT / 100.0)
        if day_pnl >= limit_up and not self.risk.get("banked_for_day"):
            self._flatten_stop(f"💰 DAILY PROFIT TARGET: day PnL ${day_pnl:+.2f} "
                               f">= +${limit_up:.2f} (+{DAY_PROFIT_PCT}% of "
                               f"${day_start:.2f}) — banked")
            self.risk.set("banked_for_day", True)
            self.risk.save()
            if self.env == "live":
                self.risk.set("stopped_for_day", True)
                self.risk.save()
                self._log("💰 LIVE account — done for the day, resume tomorrow")
            return

    def _flatten_stop(self, reason: str):
        logger.warning(f"[MT5-GURU] {reason}")
        try:
            self.bot.stop(close_positions=True)
        except Exception as e:
            logger.warning(f"mt5 flatten failed: {e}")

    def _log(self, msg: str):
        logger.info(f"[MT5-GURU] {msg}")
        try:
            self.bot._log(msg)
        except Exception:
            pass


# ── helpers the controller needs without importing server.py ────────────────
def _mt5_dead_tape(bridge) -> bool:
    """True when an MT5 grid should sleep: flat is checked by the caller.
    Rest-hours clock (GB_REST_HOURS, default 21,22,23 UTC) OR dead market
    (ADX<8 with compressed range, or spread>40bp). Fail-open False."""
    try:
        hrs = [h.strip() for h in
               (os.getenv("GB_REST_HOURS", "21,22,23") or "").split(",")
               if h.strip().isdigit()]
        if hrs and str(datetime.now(timezone.utc).hour) in hrs:
            return True
        bars = bridge.get_rates("M15", 30) or []
        if len(bars) >= 20:
            from neutral_grid import adx as _adx
            core = [{"high": float(b["high"]), "low": float(b["low"]),
                     "close": float(b["close"]),
                     "quote_volume": float(b.get("volume", 0) or 0)}
                    for b in bars]
            if _adx(core, 14) < 8.0:
                hi = max(b["high"] for b in core[-10:])
                lo = min(b["low"] for b in core[-10:])
                atr = sum(hi - lo for hi, lo in
                          [(b["high"], b["low"]) for b in core[-14:]]) / 14.0
                rng = (core[-1]["close"] - lo)
                if atr > 0 and abs(hi - lo) / atr < 0.5:
                    return True
        td = bridge.get_tick() or {}
        b, a = float(td.get("bid", 0) or 0), float(td.get("ask", 0) or 0)
        if b > 0 and a > b and (a - b) / ((a + b) / 2) > 0.004:
            return True
        return False
    except Exception:
        return False


def mt5_link_account(user_id: str, env: str) -> dict:
    """Account snapshot for the user's EA link (from services.mt5_link)."""
    from services import mt5_link
    import local_store
    c = local_store.get_credential("mt5", env) or local_store.get_credential("mt5", "live")
    tok = (c.get("api_secret") or "") if c else ""
    if not tok:
        return {}
    link = mt5_link.get_link(tok)
    return (link or {}).get("account", {})


def mt5_link_state(token: str, symbol: str) -> dict:
    from services import mt5_link
    return mt5_link.get_state(token, symbol)
