"""
GB Orchestrator v3 — Multi-Bot Orchestrator
- Each bot = isolated sub-agent thread
- Tagged by UUID, tracked in SQLite registry
- BOOT: close all positions, fresh start
- RESTART: reconcile positions, resume
- Scale: spawn N bots limited only by memory
"""
import json
import logging
import os
import threading
import time
import uuid
import signal
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, List, Any

logger = logging.getLogger("gb.orchestrator")

from neutral_grid import NeutralGridCore, GridTuning, atr_pct, adx, quote_volume

DEEPSEEK_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "deepseek/deepseek-chat"
BOOT_MARKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "boot_marker")
REGISTRY_DB = os.path.join(os.path.dirname(__file__), "hybrid_gb.db")


def level_usd(min_notional: float, free_margin: float) -> float:
    """Per-level notional (USD) for a single grid order.

    Env-tunable sizing (set these in .env, not in code):
      GB_LEVEL_USD          fixed per-level notional (0 = auto-size from margin).
      GB_FREE_MARGIN_FRAC   fraction of free margin per level in auto mode (default 0.35, was 0.05).
      GB_LEVEL_USD_CAP      ceiling on the auto-sized per-level notional (default 100).

    Two guards apply on top: the exchange min-notional floor, and a capacity
    guard so a full grid (levels × 2 sides) can always be funded from free
    margin at the configured leverage.

    Binance live compounding: GB_FREE_MARGIN_FRAC=0.35 + GURU_LEVELS=6 deep
    ladder utilizes ~70% of wallet at lev 10; capacity guard prevents
    over-leverage. Exchange book is ultimate truth (sync_with_exchange).
    """
    fixed = float(os.getenv("GB_LEVEL_USD", "0") or 0)
    frac = float(os.getenv("GB_FREE_MARGIN_FRAC", "0.35") or 0.35)
    cap = float(os.getenv("GB_LEVEL_USD_CAP", "100") or 100)

    if fixed > 0:
        per_level = fixed
    else:
        per_level = min(free_margin * frac, cap)
    per_level = max(min_notional * 1.05, per_level)

    # Capacity guard: a full grid must fit in free margin × leverage.
    if free_margin > 0:
        lev = float(os.getenv("GB_LEVERAGE", os.getenv("BINANCE_LEVERAGE", "10")) or 10)
        levels = int(os.getenv("GB_LEVELS", os.getenv("GURU_LEVELS", "6")) or 6)
        budget = free_margin * lev / max(levels * 2, 1)
        if budget > min_notional:
            per_level = min(per_level, budget)
    return per_level


def _range_gate_enabled() -> bool:
    """GB_RANGE_GATE=1 → flatten + stay flat while the market is trending
    (ADX > range cap) or in a dead/low-volume session; resume when ranging.
    This is the "wrong market time" guard: a neutral grid only makes money in
    ranging conditions, so it sits out trends instead of getting run over."""
    return os.getenv("GB_RANGE_GATE", "0") == "1"


# ── session-mapped grid distance ─────────────────────────────────────────────
# The grid ladder is sized to the market session, not one fixed ATR spacing:
#   ASIA   00:00-08:00 UTC  -> tight grid  (quiet chop)
#   LONDON 08:00-16:00 UTC  -> medium grid
#   NY     16:00-24:00 UTC  -> wider grid  (most volatile)
# Distance per session, per symbol (USD for BTCUSDT; % fallback for all other
# symbols so ADA/XAU get the same tight-but-proportional treatment).
_SESSION_PCT_DEFAULTS = {"ASIA": 0.0030, "LONDON": 0.0040, "NY": 0.0040}
_SESSION_USD_DEFAULTS = {"BTC": {"ASIA": 120.0, "LONDON": 160.0, "NY": 200.0}}
_SPACING_FEE_FLOOR = 0.0012   # below this, fees eat the grid profit (0.12% ≈ 3× maker fee)
_SPACING_CEIL = 0.0250        # sanity cap (2.5% — ATR mult + session targets live below this)


def _session_name(hour: int) -> str:
    if hour < 8:
        return "ASIA"
    if hour < 16:
        return "LONDON"
    return "NY"


def _env_f(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default

GRID_SYSTEM_PROMPT = """You control ONE Binance trading sub-bot running a two-sided neutral grid on {symbol}. Follow this EXACT loop:

1. Observe state (price, equity, position size, grid levels, unfilled orders)
2. Choose EXACTLY ONE action:
   - PLACE_ORDER: {"action": "place_order", "params": {"direction": "BUY"|"SELL", "qty": <float>, "level": <int>}, "reasoning": "..."}
   - WIDEN_GRID:  {"action": "widen_grid", "params": {"spacing_mult": <1.0-3.0>}, "reasoning": "..."}
   - CLOSE_ALL:   {"action": "close_all", "params": {}, "reasoning": "..."}
   - WAIT:        {"action": "wait", "params": {"seconds": 30}, "reasoning": "..."}

GRID RULES:
- Maintain a two-sided grid: BUY limits below price, SELL limits above
- Grid spacing should widen during high volatility (ATR > 2%) and tighten during low (< 0.5%)
- Never risk more than 2% of allocated capital per complete grid cycle
- If equity drops -5% from peak, reduce position sizes by 50%
- If equity drops -10% from peak, CLOSE_ALL and WAIT

RISK MANAGEMENT:
- action MUST be lowercase: "place_order", "widen_grid", "close_all", "wait"
- qty * price MUST be >= min_notional. If not, increase qty.
- Limit orders (maker) preferred for grid entries
- Use market orders only for emergency exit
- If order fails 3 times -> WAIT, don't retry forever

SESSION RULES:
- Asian session (00:00-08:00 UTC): narrow grid, lower risk
- London session (08:00-16:00 UTC): normal grid, directional bias may apply
- New York session (13:00-21:00 UTC): wider grid, higher volatility expected
- During high-impact news (NFP, CPI, FOMC): pause grid, CLOSE_ALL if exposed

Reply JSON ONLY. No markdown, no code blocks."""


class BotStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


class DuplicateSymbolError(RuntimeError):
    """A live bot already owns this symbol on the same desk/env."""
    def __init__(self, symbol: str, bot_id: str):
        super().__init__(f"{symbol} already running ({bot_id})")
        self.symbol = symbol
        self.bot_id = bot_id


@dataclass
class BotEntry:
    """Registry entry for one sub-bot."""
    bot_id: str          # UUID
    name: str            # display name (e.g., "BOT 1")
    symbol: str
    market_type: str     # "spot" | "futures"
    strategy: str        # "neutral_grid" | "dca"
    fee_mode: str        # "maker" | "taker"
    status: BotStatus = BotStatus.IDLE
    pid: int = 0         # OS pid (0 = thread, not separate process)
    thread_id: str = ""  # threading identifier
    created_at: str = ""
    last_active: str = ""
    config: dict = field(default_factory=dict)
    user_id: str = ""    # owning Supabase user id (multi-tenant isolation)
    env: str = ""        # "live" | "demo" | "sandbox" (needed to resume a bot)
    platform: str = ""   # "binance" | "mt5"

    def to_dict(self) -> dict:
        return {
            "bot_id": self.bot_id,
            "name": self.name,
            "symbol": self.symbol,
            "market_type": self.market_type,
            "strategy": self.strategy,
            "fee_mode": self.fee_mode,
            "status": self.status.value,
            "pid": self.pid,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "config": self.config,
            "user_id": self.user_id,
            "env": self.env,
            "platform": self.platform,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator — manages lifecycle of all sub-bots
# ─────────────────────────────────────────────────────────────────────────────

class BotOrchestrator:
    """Master controller. Spawns/pauses/resumes/stops sub-bots."""

    def __init__(self):
        self._bots: Dict[str, 'GridSubBot'] = {}  # bot_id → instance
        self._lock = threading.Lock()
        self._registry: Dict[str, BotEntry] = {}
        self._running = False
        self._boot_mode = "BOOT"  # "BOOT" or "RESTART"
        self._reconciler_stop = threading.Event()
        self._reconciler_thread: Optional[threading.Thread] = None
        self._mt5_gurus: Dict[str, Any] = {}

        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    # ── Boot Detection ──────────────────────────────────────────────────────

    def detect_boot_mode(self) -> str:
        """Determine if this is a fresh boot (close all) or restart (reconcile)."""
        if os.path.exists(BOOT_MARKER):
            with open(BOOT_MARKER) as f:
                ts = f.read().strip()
            now = time.time()
            # If marker is older than 5 minutes, treat as reboot
            try:
                marker_time = float(ts)
                if now - marker_time > 3600:  # 1h: pm2 restarts/deploys must never wipe bots
                    self._boot_mode = "BOOT"
                else:
                    self._boot_mode = "RESTART"
            except ValueError:
                self._boot_mode = "BOOT"
        else:
            self._boot_mode = "BOOT"

        logger.info(f"🧠 Orchestrator boot mode: {self._boot_mode}")
        return self._boot_mode

    def mark_boot(self):
        """Write boot timestamp marker."""
        with open(BOOT_MARKER, "w") as f:
            f.write(str(time.time()))

    # ── Registry (SQLite) ───────────────────────────────────────────────────

    def _load_registry(self):
        """Load persisted bot entries from SQLite."""
        try:
            import sqlite3
            conn = sqlite3.connect(REGISTRY_DB)
            conn.execute("""CREATE TABLE IF NOT EXISTS orchestrator_bots (
                bot_id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            conn.commit()
            rows = conn.execute("SELECT bot_id, data FROM orchestrator_bots").fetchall()
            for bot_id, data in rows:
                entry = BotEntry(**json.loads(data))
                entry.status = BotStatus.STOPPED  # reset to stopped on load
                self._registry[bot_id] = entry
            conn.close()
            logger.info(f"📋 Loaded {len(self._registry)} bots from registry")
        except Exception as e:
            logger.warning(f"Registry load failed: {e}")

    def _save_registry(self, bot_id: str):
        """Persist one bot entry to SQLite."""
        try:
            import sqlite3
            entry = self._registry.get(bot_id)
            if not entry:
                return
            conn = sqlite3.connect(REGISTRY_DB)
            # Ensure the table exists even when _load_registry hasn't run first
            # (spawn happens before any boot hook; without this the INSERT fails).
            conn.execute("""CREATE TABLE IF NOT EXISTS orchestrator_bots (
                bot_id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            # BotStatus is an Enum and cannot be encoded by json.dumps directly.
            # Persist the same primitive representation consumed by _load_registry.
            data = json.dumps(entry.to_dict() if hasattr(entry, 'to_dict') else entry)
            conn.execute(
                "INSERT OR REPLACE INTO orchestrator_bots (bot_id, data, updated_at) VALUES (?, ?, ?)",
                (bot_id, data, datetime.now(timezone.utc).isoformat()))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Registry save failed: {e}")

    def _delete_from_registry(self, bot_id: str):
        """Remove bot from SQLite registry."""
        try:
            import sqlite3
            conn = sqlite3.connect(REGISTRY_DB)
            conn.execute("DELETE FROM orchestrator_bots WHERE bot_id = ?", (bot_id,))
            conn.commit()
            conn.close()
        except Exception:
            pass

    # ── Boot Flow ───────────────────────────────────────────────────────────

    def boot(self, bridges: Dict[str, Any] = None):
        """Initialize orchestrator. On BOOT: closes all positions. On RESTART: resumes."""
        self.detect_boot_mode()
        self._load_registry()

        if self._boot_mode == "BOOT":
            logger.info("🧹 BOOT: detecting & closing all exchange positions...")
            self._close_all_exchange_positions()
            self._clear_registry()
            self.mark_boot()
            logger.info("✅ Orchestrator ready — all positions closed, 0 bots")
        else:
            logger.info(f"🔄 RESTART: {len(self._registry)} bots in registry, reconciling...")
            self.mark_boot()
            logger.info(f"✅ Orchestrator ready — {len(self._registry)} bots available for resume")

        self._start_reconciler()

    # ── Continuous exchange reconciler (ghost self-heal) ───────────────────

    def _start_reconciler(self):
        """Background loop that keeps every bot's bridge in sync with the exchange,
        so ghost positions clear even when no one is watching the dashboard."""
        if self._reconciler_thread and self._reconciler_thread.is_alive():
            return
        self._reconciler_stop.clear()
        self._reconciler_thread = threading.Thread(
            target=self._reconciler_loop, daemon=True, name="orchestrator-reconciler")
        self._reconciler_thread.start()
        logger.info("🔄 Exchange reconciler started (60s interval)")

    def _reconciler_loop(self):
        while not self._reconciler_stop.is_set():
            try:
                with self._lock:
                    subs = list(self._bots.values())
                for sub in subs:
                    br = getattr(sub, "bridge", None)
                    if br and br.is_connected:
                        try:
                            br.sync_with_exchange(force=True)
                        except Exception:
                            pass
            except Exception:
                pass
            # UI health is separately cached; a minute cadence prevents routine
            # reconciliation from exhausting Binance REST weight.
            self._reconciler_stop.wait(timeout=60)

    def _close_all_exchange_positions(self):
        """Close ALL positions on Binance across every symbol we know about."""
        symbols = ["BTCUSDT", "DEXEUSDT", "ETHUSDT", "SOLUSDT", "INJUSDT", "XAUUSDT", "BNBUSDT", "DOGEUSDT", "BONKUSDT", "PEPEUSDT", "EULUSDT", "1000PEPEUSDT", "AKEUSDT"]
        for sym in symbols:
            try:
                from bridge import BinanceBridge
                b = BinanceBridge(symbol=sym, environment=_active_env, market_type="futures")
                b.connect()
                info = b._client.futures_position_information(symbol=b.binance_symbol)
                for p in info:
                    amt = float(p.get("positionAmt", 0))
                    if abs(amt) < 1e-8:
                        continue
                    side = "BUY" if amt < 0 else "SELL"
                    try:
                        order = b._client.futures_create_order(symbol=b.binance_symbol, side=side, type="MARKET", quantity=abs(amt), reduceOnly=True)
                        logger.info(f"  🧹 Closed {sym}: {abs(amt)} {side} order={order.get('orderId')}")
                    except Exception as e2:
                        logger.warning(f"  ⚠️ Failed to close {sym} position: {e2}")
                b.cancel_all_orders()
                b.disconnect()
            except Exception as e:
                logger.debug(f"  {sym}: no positions or bridge error ({str(e)[:60]})")

    def _clear_registry(self):
        """Wipe all bots from registry (BOOT only)."""
        try:
            import sqlite3
            conn = sqlite3.connect(REGISTRY_DB)
            conn.execute("DELETE FROM orchestrator_bots")
            conn.commit()
            conn.close()
        except Exception:
            pass
        self._registry = {}
        logger.info("🧹 Registry cleared")

    # ── Bot Lifecycle ───────────────────────────────────────────────────────

    def spawn(self, name: str, symbol: str, market_type: str = "futures",
              strategy: str = "neutral_grid", fee_mode: str = "maker",
              config: dict = None, bridge = None, storage = None,
              platform: str = "binance", user_id: str = None) -> Optional[str]:
        """Create and start a new sub-bot. Returns bot_id or None."""
        bot_id = f"bot-{uuid.uuid4().hex[:8]}"
        env = (bridge.environment if bridge is not None else
               (config or {}).get("env", ""))

        with self._lock:
            taken = self.live_symbol_owner(symbol, user_id, platform, env)
            if taken:
                logger.warning(f"⛔ {symbol} already running for this user ({taken}) — spawn rejected")
                return None
            for bid, be in self._registry.items():
                if (be.symbol == symbol and be.status == BotStatus.RUNNING
                        and (be.user_id or "") == (user_id or "")
                        and (be.platform or "binance") == (platform or "binance")
                        and (not env or not (be.env or "") or (be.env or "") == env)):
                    logger.warning(f"⛔ {symbol} already running for this user ({bid}) — spawn rejected")
                    return None
            entry = BotEntry(
                bot_id=bot_id,
                name=name,
                symbol=symbol,
                market_type=market_type,
                strategy=strategy,
                fee_mode=fee_mode,
                status=BotStatus.RUNNING,
                created_at=datetime.now(timezone.utc).isoformat(),
                last_active=datetime.now(timezone.utc).isoformat(),
                config=config or {},
                user_id=user_id or "",
                env=env,
                platform=platform,
            )
            self._registry[bot_id] = entry
            self._save_registry(bot_id)

        sub = self._build_sub(entry, bridge, config or {}, user_id=user_id)
        self._bots[bot_id] = sub
        sub.start()

        logger.info(f"🟢 Spawned {name} ({symbol}) → {bot_id} [user={user_id or '-'}]")
        return bot_id

    def _build_sub(self, entry: BotEntry, bridge, config: dict, user_id: str = None):
        """Construct the right sub-bot class for a registry entry (shared by
        spawn() and resume_from_registry())."""
        return GridSubBot(entry.bot_id, entry, bridge, None, self, user_id=user_id)

    def register_external_bot(self, name: str, symbol: str, bot,
                              user_id: str = None, bot_id: str = None,
                              config: dict = None) -> str:
        """Register a pre-built bot instance (e.g., GuruAIBot) so the
        orchestrator + dashboard manage it uniformly. Returns bot_id."""
        bot_id = bot_id or f"guru-{uuid.uuid4().hex[:8]}"
        env = getattr(getattr(bot, "bridge", None), "environment", "")
        platform = getattr(getattr(bot, "bridge", None), "platform",
                           "binance") or "binance"
        entry = BotEntry(
            bot_id=bot_id, name=name, symbol=symbol,
            market_type="futures", strategy="guru_grid", fee_mode="maker",
            status=BotStatus.RUNNING,
            created_at=datetime.now(timezone.utc).isoformat(),
            last_active=datetime.now(timezone.utc).isoformat(),
            config=dict(config or {}),
            user_id=user_id or "",
            env=env, platform=platform,
        )
        with self._lock:
            taken = self.live_symbol_owner(symbol, user_id, platform, env)
            if taken:
                raise DuplicateSymbolError(symbol, taken)
            self._registry[bot_id] = entry
            self._save_registry(bot_id)
            self._bots[bot_id] = bot
        logger.info(f"🟢 Registered external bot {name} ({symbol}) → {bot_id}")
        return bot_id

    def live_symbol_owner(self, symbol: str, user_id: str = None,
                          platform: str = "binance", env: str = None) -> Optional[str]:
        """bot_id of a live bot on this symbol for the same user/desk/env, else None.

        One book per symbol: two XAUUSDT grids on the same Binance demo account
        would share positions and double the ladder.
        """
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return None
        platform = (platform or "binance").lower()
        env = (env or "").lower()
        for bid, sub in list(self._bots.items()):
            entry = self._registry.get(bid)
            suid = (getattr(sub, "user_id", None)
                    or (entry.user_id if entry else "") or "")
            if user_id and suid and suid != user_id:
                continue
            sub_sym = (getattr(sub, "symbol", None)
                       or (entry.symbol if entry else "") or "").strip().upper()
            if sub_sym != symbol:
                continue
            plat = self._bot_platform(entry, sub)
            if plat != platform:
                continue
            br = getattr(sub, "bridge", None)
            sub_env = (getattr(br, "environment", None)
                       or getattr(sub, "env", None)
                       or (entry.env if entry else "") or "").lower()
            if env and sub_env and env != sub_env:
                continue
            st = getattr(getattr(sub, "status", None), "value", None) or ""
            alive = True
            try:
                alive = bool(sub.is_alive())
            except Exception:
                alive = st == "running"
            if st in ("stopped", "idle") and not alive:
                continue
            if not alive and st not in ("running", "paused", ""):
                continue
            return bid
        return None

    def live_symbols(self, user_id: str = None, platform: str = "binance",
                     env: str = None) -> set:
        out = set()
        platform = (platform or "binance").lower()
        env = (env or "").lower()
        for bid, sub in list(self._bots.items()):
            entry = self._registry.get(bid)
            suid = (getattr(sub, "user_id", None)
                    or (entry.user_id if entry else "") or "")
            if user_id and suid and suid != user_id:
                continue
            plat = self._bot_platform(entry, sub)
            if plat != platform:
                continue
            br = getattr(sub, "bridge", None)
            sub_env = (getattr(br, "environment", None)
                       or getattr(sub, "env", None)
                       or (entry.env if entry else "") or "").lower()
            if env and sub_env and env != sub_env:
                continue
            st = getattr(getattr(sub, "status", None), "value", None) or ""
            alive = True
            try:
                alive = bool(sub.is_alive())
            except Exception:
                alive = st == "running"
            if st in ("stopped", "idle") and not alive:
                continue
            sym = (getattr(sub, "symbol", None)
                   or (entry.symbol if entry else "") or "").strip().upper()
            if sym:
                out.add(sym)
        return out

    def dedupe_live_symbols(self):
        """If two live bots share a symbol on the same desk/env, keep one.

        Extra Python threads are stopped WITHOUT flattening — they were
        writing the same exchange book.
        """
        groups: Dict[tuple, List[str]] = {}
        for bid, sub in list(self._bots.items()):
            entry = self._registry.get(bid)
            key = (
                (getattr(sub, "user_id", None) or (entry.user_id if entry else "") or ""),
                self._bot_platform(entry, sub),
                (getattr(getattr(sub, "bridge", None), "environment", None)
                 or (entry.env if entry else "") or "").lower(),
                (getattr(sub, "symbol", None) or (entry.symbol if entry else "") or "").upper(),
            )
            groups.setdefault(key, []).append(bid)
        for key, bids in groups.items():
            if len(bids) < 2 or not key[3]:
                continue
            keep = None
            for bid in bids:
                sub = self._bots.get(bid)
                st = getattr(getattr(sub, "status", None), "value", None) if sub else ""
                if st == "running":
                    keep = bid
                    break
            keep = keep or bids[-1]
            for bid in bids:
                if bid == keep:
                    continue
                logger.warning(f"dedupe: extra {key[3]} {bid} — detach, keep {keep}")
                try:
                    self.stop_bot(bid, close_positions=False)
                except Exception as e:
                    logger.warning(f"dedupe stop {bid}: {e}")

    def _bot_platform(self, entry, sub=None) -> str:
        plat = (getattr(entry, "platform", None) or "") if entry is not None else ""
        if sub is not None:
            br = getattr(sub, "bridge", None)
            plat = plat or getattr(br, "platform", None) or getattr(sub, "platform", None) or ""
        return (plat or "binance").lower()

    def _mt5_token_for(self, user_id: str) -> str:
        try:  # single-user: MT5 EA token from local store or env
            import local_store
            c = local_store.get_credential("mt5", "live") or local_store.get_credential("mt5", "demo")
            if c.get("api_secret"):
                return c["api_secret"]
            import os as _os
            return _os.getenv("MT5_EA_TOKEN", "")
        except Exception as e:
            logger.warning(f"mt5 token lookup failed: {e}")
        return ""

    def _rebuild_mt5_bridge(self, entry, require_heartbeat: bool = False):
        from bridge_mt5 import MT5Bridge
        tok = self._mt5_token_for(entry.user_id or "")
        if not tok:
            return None
        bridge = MT5Bridge(token=tok, symbol=entry.symbol,
                           environment=entry.env or "demo",
                           user_id=entry.user_id or None)
        if not bridge.connect(require_heartbeat=require_heartbeat):
            return None
        return bridge

    def _resume_mt5_guru(self, entry) -> bool:
        """Rebuild MT5 GuruAI against the persisted EA ladder (no flatten)."""
        from mt5_guru import MT5GuruController
        tok = self._mt5_token_for(entry.user_id or "")
        if not tok:
            logger.warning(f"resume: no MT5 token for {entry.name}")
            return False
        cfg = entry.config if isinstance(entry.config, dict) else {}
        ftmo = True if "ftmo_rules" not in cfg else bool(cfg.get("ftmo_rules"))
        try:
            ctrl = MT5GuruController(
                user_id=entry.user_id, token=tok, symbol=entry.symbol,
                env=entry.env or "demo", ftmo_rules=ftmo,
                require_heartbeat=False)
        except Exception as e:
            logger.warning(f"resume: MT5 GuruAI rebuild failed for {entry.name}: {e}")
            return False
        if not ctrl.resume(entry.bot_id, orch=self):
            return False
        if not hasattr(self, "_mt5_gurus"):
            self._mt5_gurus = {}
        if entry.user_id:
            self._mt5_gurus[entry.user_id] = ctrl
        logger.info(f"🔄 Resumed MT5 GuruAI {entry.name} ({entry.symbol}) → {entry.bot_id}")
        return True

    def _schedule_mt5_guru_resume(self, entry):
        """EA may not have POSTed yet after pm2 restart — keep the registry
        row and retry so the side panel does not forget a live ladder."""
        def _retry():
            for _ in range(24):  # ~2 minutes
                time.sleep(5)
                if entry.bot_id in self._bots:
                    return
                if self._resume_mt5_guru(entry):
                    return
            logger.warning(f"resume: gave up waiting for EA on {entry.name} "
                           f"({entry.symbol}) — registry kept")
        threading.Thread(target=_retry, daemon=True,
                         name=f"resume-mt5-{entry.symbol}").start()

    def resume_from_registry(self, bridge_factory=None):
        """Recreate + restart every persisted bot after a process restart.

        Binance GuruAI (strategy=guru_grid) is NOT resumed — it re-scans via
        the GuruAI button. MT5 GuruAI IS resumed and must NOT flatten: the
        EA still holds the limit ladder across the process bounce.
        """
        if not hasattr(self, "_mt5_gurus"):
            self._mt5_gurus = {}
        for bot_id, entry in list(self._registry.items()):
            plat = self._bot_platform(entry)
            if entry.strategy == "guru_grid":
                if plat == "mt5":
                    if not self._resume_mt5_guru(entry):
                        self._schedule_mt5_guru_resume(entry)
                    continue
                self._registry.pop(bot_id, None)
                self._delete_from_registry(bot_id)
                continue
            try:
                bridge = None
                if plat == "mt5":
                    try:
                        bridge = self._rebuild_mt5_bridge(entry, require_heartbeat=False)
                    except Exception as e:
                        logger.warning(f"resume: MT5 bridge rebuild failed for {entry.name}: {e}")
                    if not bridge:
                        logger.warning(f"resume: MT5 EA not ready for {entry.name} "
                                       f"({entry.symbol}) — keeping registry, retry on next spawn")
                        continue
                elif plat in ("binance", ""):
                    if bridge_factory:
                        bridge = bridge_factory(entry.symbol, entry.market_type,
                                                entry.env or None, entry.user_id or None)
                    if not bridge or not getattr(bridge, "is_connected", False):
                        logger.warning(f"resume: bridge unavailable for {entry.name} ({entry.symbol})")
                        continue
                sub = self._build_sub(entry, bridge, entry.config or {}, entry.user_id or None)
                self._bots[entry.bot_id] = sub
                entry.status = BotStatus.RUNNING
                sub.start()
                logger.info(f"🔄 Resumed {entry.name} ({entry.symbol}) → {entry.bot_id} "
                            f"[user={entry.user_id or '-'}]")
            except Exception as e:
                logger.warning(f"resume {entry.name} failed: {e}")
        try:
            self.dedupe_live_symbols()
        except Exception as e:
            logger.warning(f"resume dedupe failed: {e}")

    def _owns(self, bot_id: str, user_id: str) -> bool:
        """True if `user_id` is allowed to operate this bot.

        Multi-tenant isolation: a user may only touch their own bots. The legacy
        single-operator admin-token path (user_id=None) is unrestricted.
        """
        if not user_id:
            return True
        entry = self._registry.get(bot_id)
        if entry is None:
            return True  # unknown bot — let the caller surface 404 naturally
        return (entry.user_id or "") == "" or entry.user_id == user_id

    def pause(self, bot_id: str, user_id: str = None) -> bool:
        """Pause a running bot."""
        if not self._owns(bot_id, user_id):
            return False
        sub = self._bots.get(bot_id)
        if sub:
            sub.pause()
            if bot_id in self._registry:
                self._registry[bot_id].status = BotStatus.PAUSED
            return True
        return False

    def resume(self, bot_id: str, user_id: str = None) -> bool:
        """Resume a paused bot."""
        if not self._owns(bot_id, user_id):
            return False
        sub = self._bots.get(bot_id)
        if sub:
            sub.resume()
            if bot_id in self._registry:
                self._registry[bot_id].status = BotStatus.RUNNING
            return True
        return False

    def stop_bot(self, bot_id: str, close_positions: bool = True,
                 user_id: str = None) -> bool:
        if not self._owns(bot_id, user_id):
            return False
        sub = self._bots.get(bot_id)
        if sub:
            sub.stop(close_positions=close_positions)
            self._bots.pop(bot_id, None)
            self._delete_from_registry(bot_id)
            self._registry.pop(bot_id, None)
            if hasattr(sub, "bridge") and sub.bridge:
                try:
                    sub.bridge.disconnect()
                except Exception:
                    pass
            return True
        if bot_id in self._registry:
            self._delete_from_registry(bot_id)
            self._registry.pop(bot_id, None)
            return True
        return False

    def list_bots(self, user_id: str = None, platform: str = None) -> List[dict]:
        """Return registered bots with TRUE status + level details.

        When `user_id` is provided, only that user's bots are returned.
        When `platform` is provided, only that desk (binance/mt5).
        """
        result = []
        ghosts = []
        for bot_id, entry in list(self._registry.items()):
            sub = self._bots.get(bot_id)
            if sub is None and (getattr(entry, "platform", "") or "") != "mt5":
                ghosts.append(bot_id)
                continue
            if user_id and (entry.user_id or "") and entry.user_id != user_id:
                continue
            d = entry.to_dict() if hasattr(entry, 'to_dict') else entry
            if sub:
                try:
                    sd = sub.status_dict() or {}
                except Exception as e:
                    logger.warning(f"list_bots status {bot_id}: {e}")
                    sd = {}
                status = getattr(getattr(sub, "status", None), "value", None) or d.get("status")
                alive = True
                try:
                    alive = bool(sub.is_alive())
                except Exception:
                    alive = True
                if str(status) == "running" and not alive:
                    status = BotStatus.ERROR.value
                d["status"] = status
                d["alive"] = alive
                d["levels"] = sd.get("levels", 0)
                d["log"] = (getattr(sub, "logs", None) or [])[-5:]
                d["levels_detail"] = sd.get("levels_detail", [])
                d["total_pnl"] = sd.get("total_pnl", 0)
                d["net_position"] = sd.get("net_position", 0)
                br = getattr(sub, "bridge", None)
                d["env"] = getattr(br, "environment", None) or getattr(sub, "env", "unknown")
                d["platform"] = (getattr(br, "platform", None)
                                 or getattr(sub, "platform", None)
                                 or "binance")
            else:
                # Registry entry but no live sub-bot. MT5 rows are kept across
                # a restart until the EA POSTs again — still show them so the
                # side panel does not flash empty while the ladder is live.
                plat_e = (getattr(entry, "platform", "") or "binance")
                waiting_mt5 = plat_e == "mt5"
                d["status"] = (BotStatus.RUNNING.value if waiting_mt5
                               else BotStatus.STOPPED.value)
                d["alive"] = False
                d["levels"] = 0
                d["levels_detail"] = []
                d["env"] = getattr(entry, "env", None) or "unknown"
            plat = (d.get("platform") if isinstance(d, dict) else "") or getattr(entry, "platform", "") or ""
            plat = plat or "binance"
            if isinstance(d, dict):
                d["platform"] = plat
            if platform:
                if platform == "binance" and plat in ("mt5",):
                    continue
                if platform == "mt5" and plat != platform:
                    continue
            result.append(d)
        # Safety net: a live bot missing from the SQLite row still belongs
        # in the side panel (GuruAI register race, or registry drop).
        seen = {r.get("bot_id") for r in result if isinstance(r, dict)}
        for bot_id, sub in list(self._bots.items()):
            if bot_id in seen:
                continue
            br = getattr(sub, "bridge", None)
            plat = (getattr(br, "platform", None) or getattr(sub, "platform", None)
                    or "binance")
            suid = getattr(sub, "user_id", None) or ""
            if user_id and suid and suid != user_id:
                continue
            if platform:
                if platform == "binance" and plat in ("mt5",):
                    continue
                if platform == "mt5" and plat != platform:
                    continue
            try:
                sd = sub.status_dict() or {}
            except Exception:
                sd = {}
            status = getattr(getattr(sub, "status", None), "value", None) or "running"
            result.append({
                "bot_id": bot_id,
                "name": getattr(sub, "name", bot_id),
                "symbol": getattr(sub, "symbol", ""),
                "market_type": "futures",
                "strategy": "guru_grid",
                "status": status,
                "alive": True,
                "levels": sd.get("levels", 0),
                "levels_detail": sd.get("levels_detail", []),
                "total_pnl": sd.get("total_pnl", 0),
                "net_position": sd.get("net_position", 0),
                "env": getattr(br, "environment", None) or getattr(sub, "env", "unknown"),
                "platform": plat,
                "user_id": suid,
            })
        for bid in ghosts:
            self._registry.pop(bid, None)
            self._delete_from_registry(bid)
        return result

    def get_bot(self, bot_id: str) -> Optional[dict]:
        """Get single bot details."""
        entry = self._registry.get(bot_id)
        if not entry:
            return None
        d = entry.to_dict() if hasattr(entry, 'to_dict') else entry
        sub = self._bots.get(bot_id)
        if sub:
            d.update(sub.status_dict())
        return d

    # ── Shutdown ────────────────────────────────────────────────────────────

    def _handle_shutdown(self, signum=None, frame=None):
        logger.info("🛑 Orchestrator shutdown — persisting bots for resume...")
        for bot_id, sub in list(self._bots.items()):
            entry = self._registry.get(bot_id)
            plat = self._bot_platform(entry, sub)
            # MT5: the EA owns the resting ladder. Flattening on every pm2
            # restart leaves the EA trading while the side panel forgets the bot.
            flatten = plat != "mt5"
            try:
                sub.stop(close_positions=flatten)
            except Exception as e:
                logger.warning(f"shutdown {'flatten' if flatten else 'detach'} {bot_id}: {e}")
            # Keep the registry entry (status RUNNING) so the next boot can
            # rebuild this bot. stop_bot() would delete it — we avoid that.
            if entry is not None:
                entry.status = BotStatus.RUNNING
                self._save_registry(bot_id)
        self._bots.clear()
        logger.info("✅ Orchestrator shutdown complete (bots persisted for resume)")

    def shutdown(self):
        self._handle_shutdown()


# ─────────────────────────────────────────────────────────────────────────────
# Sub-Bot — one isolated trading agent
# ─────────────────────────────────────────────────────────────────────────────

class GridSubBot:
    """One isolated trading agent with its own event loop."""

    def __init__(self, bot_id: str, entry: BotEntry, bridge, storage, orchestrator: BotOrchestrator,
                 user_id: str = None):
        self.bot_id = bot_id
        self.entry = entry
        self.bridge = bridge
        self.storage = storage
        self.orchestrator = orchestrator
        self.user_id = user_id or (entry.user_id if entry else "")
        self.status = BotStatus.IDLE
        self.positions: List[dict] = []
        self.logs: List[str] = []
        self._thread: threading.Thread = None
        self._shutdown = threading.Event()
        self._paused = threading.Event()
        self._retry_count = 0
        self._max_retries = 3
        # Binance REST: 30s is enough. MT5 is pull-only — a 30s think cycle
        # makes a limit look "stuck in the sidebar" until the next tick.
        self._decision_interval = 5.0 if (entry.platform or "") == "mt5" else 30.0
        self._last_session = ""

        self._api_key = os.getenv("DEEPSEEK_API_KEY", "")
        self._enabled = bool(self._api_key)

        # shared neutral-grid core (ATR spacing, ADX regime, scale-in, safety)
        self.core = NeutralGridCore(GridTuning())
        self.grid_orders: Dict[int, dict] = {}   # order_id -> {side, price, qty}
        # Per-level pending leg: level -> "BUY" | "SELL" | missing.
        # After BUY_i fills, BUY_i is NOT re-stocked until SELL_i (its exit)
        # fills — this is the slot model that bounds position growth to
        # 1 leg per level (max levels x qty per side, ever).
        self._pair_pending: Dict[int, str] = {}
        self.qty: float = 0.0
        self.cycles: int = 0
        self.realized_pnl: float = 0.0

    def start(self):
        if self.status == BotStatus.RUNNING:
            return
        if not self.bridge or not self.bridge.is_connected:
            self._log("❌ Bridge offline — cannot start")
            return
        # Clean slate: never inherit resting orders/positions from a prior
        # instance (crash, kill -9, respawn). The exchange must start empty.
        try:
            self._flatten()
        except Exception as e:
            self._log(f"start cleanup: {str(e)[:70]}")
        self.status = BotStatus.RUNNING
        self._shutdown.clear()
        self._paused.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True,
            name=f"gb-{self.bot_id}")
        self._thread.start()
        self.entry.thread_id = self._thread.name
        self._log("🧠 Sub-bot started")

    def pause(self):
        self._paused.set()
        self.status = BotStatus.PAUSED
        self._log("⏸ Paused")

    def resume(self):
        self._paused.clear()
        self.status = BotStatus.RUNNING
        self._log("▶️ Resumed")

    def stop(self, close_positions: bool = True):
        self._shutdown.set()
        self._paused.clear()
        self.status = BotStatus.STOPPED
        # Force-close via direct REST client (instant, no WebSocket)
        if close_positions and self.bridge and self.bridge._client:
            try:
                sym = self.bridge.binance_symbol
                info = self.bridge._client.futures_position_information(symbol=sym)
                for p in info:
                    amt = float(p.get("positionAmt", 0))
                    if abs(amt) < 1e-8: continue
                    side = "BUY" if amt < 0 else "SELL"
                    self.bridge._client.futures_create_order(
                        symbol=sym, side=side, type="MARKET",
                        quantity=abs(amt), reduceOnly=True)
                self.bridge.cancel_all_orders()
            except Exception as e:
                self._log(f"Stop cleanup: {e}")
        if self._thread:
            self._thread.join(timeout=2)
        self._log("🛑 Stopped")

    def _log(self, msg: str):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.logs.append(entry)
        if len(self.logs) > 100:
            self.logs = self.logs[-50:]
        logger.info(f"[{self.entry.name}] {msg}")

    def _loop(self):
        """Main loop — neutral grid tick (observe → place/manage → safety)."""
        while not self._shutdown.is_set():
            if self._paused.is_set():
                self._shutdown.wait(timeout=5)
                continue

            try:
                self._neutral_grid_tick()
                self.entry.last_active = datetime.now(timezone.utc).isoformat()
                self._retry_count = 0
            except Exception as e:
                self._log(f"❌ Loop error: {e}")
                self._retry_count += 1
                if self._retry_count >= self._max_retries:
                    self._log(f"⚠️ Max retries — pausing for intervention")
                    self._paused.set()
                    self._retry_count = 0

            self._shutdown.wait(timeout=self._decision_interval)

    def _gather_context(self) -> dict:
        """Collect state snapshot. Uses live bridge positions to stay in sync."""
        # Force tick refresh if WebSocket hasn't populated yet
        td = None
        if self.bridge:
            td = self.bridge.get_tick()
            if not td:
                # Ensure REST fallback works by forcing a fresh tick
                try:
                    self.bridge._rest_tick()
                    td = self.bridge.get_tick()
                except:
                    pass
        acct = self.bridge.get_account_info() if self.bridge else {}
        # get_positions() reconciles against the exchange — exchange is truth.
        positions = self.bridge.get_positions() if self.bridge else []
        self.positions = positions
        si = getattr(self.bridge, '_symbol_info', {}) if self.bridge else {}

        return {
            "symbol": self.entry.symbol,
            "bot_name": self.entry.name,
            "market_type": self.entry.market_type,
            "price": td["bid"] if td else 0,
            "equity": acct.get("equity", 0),
            "balance": acct.get("balance", 0),
            "free_margin": acct.get("free_margin", 0),
            "active_levels": len(positions),
            "open_positions": [{
                "level": i, "ticket": p.get("ticket", 0),
                "direction": p.get("type", "?"),
                "qty": p.get("volume", 0),
                "entry": p.get("price_open", 0),
                "pnl": p.get("profit", 0),
            } for i, p in enumerate(positions)],
            "min_notional": min(si.get("min_notional", 5.0) or 99, 5.0),  # Binance default is 5, not 50
            "qty_step": si.get("qty_step", 0.001) or 0.001,
            "price_precision": si.get("price_precision", 2) or 2,
            "fee_mode": self.entry.fee_mode,
            "trend": 0,
            "last_error": self.logs[-1] if self.logs else "",
            "retry_count": self._retry_count,
        }

    def _neutral_grid_tick(self):
        """One tick of the shared two-sided neutral grid (limit orders).

        NO-GHOST INVARIANT: after every tick the exchange's open orders for
        this symbol EXACTLY equal the desired ladder (2 x active_levels max).
        Anything else is a ghost — from crashes, restarts, failed cancels or
        old instances — and is cancelled on every tick until gone; new ladder
        orders are only placed once the exchange is clean. So the order count
        can only ever shrink toward the ladder, never grow from strays.
        """
        price = self._price()
        if price <= 0:
            return

        # Same book + crash stops as GuruAI (spawn and 🧠 share this path
        # when GridSubBot is the executor — MT5 spawn).
        try:
            from binance_guard import crash_threshold, wallet_wiped, evaluate
            plat = (getattr(self.bridge, "platform", None) or "binance").lower()
            acct = {}
            try:
                acct = self.bridge.get_account_info() or {}
            except Exception:
                pass
            wallet = float(acct.get("equity") or acct.get("balance") or 0)
            prev = float(getattr(self, "_guard_wallet_peak", 0) or 0)
            peak = max(prev, wallet)
            self._guard_wallet_peak = peak
            wipe = 0.10
            if wallet_wiped(wallet, peak, wipe):
                self._flatten()
                self.status = BotStatus.STOPPED
                self._log(f"🛑 GATE EXTREME — wallet ${wallet:.2f} <= "
                          f"{wipe*100:.0f}% of peak ${peak:.2f}")
                return
            if self._has_position():
                thr = crash_threshold(getattr(self, "crash_guard_pct", None))
                ref = float(getattr(self, "_pos_ref_price", 0) or 0)
                if ref <= 0:
                    self._pos_ref_price = price
                else:
                    drift = (price - ref) / ref
                    if abs(drift) > thr:
                        self._flatten()
                        self.status = BotStatus.STOPPED
                        self._log(f"🛑 CRASH GUARD: price {drift*100:+.1f}% "
                                  f"from exposure ref — halted (> {thr*100:.1f}%)")
                        return
            else:
                self._pos_ref_price = 0.0
        except Exception:
            pass

        # Market-time gate (opt-in via GB_RANGE_GATE=1): flatten + stay flat
        # while trending or dead. A neutral grid only profits in a range.
        if _range_gate_enabled() and not self._range_open():
            if self.grid_orders or self._has_position():
                self._flatten()
                self.core.reset_grid()
                self._pair_pending.clear()
                self.qty = 0.0
                self._log("🌙 trending/dead market — flattened, waiting for range")
            return

        if not self.qty:
            self.qty = self._size_qty(price)
        if self.core.center <= 0:
            self.core.center = price

        # Session re-quote with hysteresis (same 20% as GuruAI). A 0.001% ATR
        # wiggle must not cancel a live MT5 ladder every 30s.
        sess_sp = self._session_spacing(price, atr_pct(self._bars("15m"),
                                                       self.core.t.atr_period))
        hyst = _env_f("GURU_SESSION_REQUOTE_HYST", 0.20)
        if sess_sp and self.core.spacing_pct > 0:
            sess_changed = sess_sp[0] != self._last_session
            rel = abs(sess_sp[1] - self.core.spacing_pct) / self.core.spacing_pct
            if sess_changed or rel > hyst:
                self._last_session = sess_sp[0]
                self.core.spacing_pct = sess_sp[1]
                self._log(f"🗓 {sess_sp[0]} session → spacing {sess_sp[1] * 100:.3f}% "
                          f"(${sess_sp[1] * price:.0f}/side)")
        elif sess_sp:
            self._last_session = sess_sp[0]
            self.core.spacing_pct = sess_sp[1]

        # Ladder-band recenter: the grid follows the price, so a stale ladder
        # can never sit outside the market and instant-fill its re-stocks.
        # Threshold = half a cell beyond the outermost level.
        self.core.t.recenter_drift = max(0.0015,
                                         (self.core.active_levels + 0.5) * self.core.spacing_pct)

        self._handle_fills(price)       # fills → pending-leg updates + fill-gated scale-in
        self._reconcile_orders()        # enforce the no-ghost invariant

        if self.core.should_recenter(price):
            self._recenter(price)
            return

        if self._check_equity_stop():
            self._flatten()
            self.status = BotStatus.STOPPED
            self._log("🛑 Equity stop — halted")

    # ── neutral-grid helpers ──
    def _price(self) -> float:
        td = self.bridge.get_tick() if self.bridge else None
        return (td["bid"] + td["ask"]) / 2 if td else 0.0

    def _has_position(self) -> bool:
        return bool(self.bridge.get_positions()) if self.bridge else False

    def _range_open(self) -> bool:
        """True only in a ranging, liquid market (the grid's "right market time").

        Ranging = ADX below the trend cap; liquid = participation above the
        quote-volume floor. Either being false means we should be flat.
        """
        try:
            return (self.core.regime_ok(self._adx_value(), self._log)
                    and self.core.participation_ok(self._part_volume(), self._log))
        except Exception:
            return True  # on data failure, don't block trading

    def _bars(self, interval: str, limit: int = None) -> List[dict]:
        # Indicator history from MAINNET (testnet klines mislead ATR/ADX).
        # Execution (book/orders) always stays on the venue client.
        try:
            from bridge import public_klines as _pk
            return _pk(self.bridge.binance_symbol, interval,
                       limit or self.core.t.adx_period * 3 + 2)
        except Exception:
            pass
        try:
            kl = self.bridge._client.futures_klines(
                symbol=self.bridge.binance_symbol, interval=interval,
                limit=limit or self.core.t.adx_period * 3 + 2) or []
        except Exception:
            return []
        return [{"high": float(k[2]), "low": float(k[3]), "close": float(k[4]),
                 "quote_volume": float(k[7])} for k in kl]

    def _adx_value(self) -> float:
        # Wilder RMA needs ~100+ bars to warm up; a short window reads hot.
        return adx(self._bars("15m", limit=120), self.core.t.adx_period)

    def _part_volume(self) -> float:
        return quote_volume(self._bars("5m", limit=12))

    def _size_qty(self, price: float) -> float:
        si = getattr(self.bridge, "_symbol_info", {}) or {}
        min_notional = float(si.get("min_notional", 5.0)) or 5.0
        step = float(si.get("qty_step", 0.001)) or 0.001
        acct = self.bridge.get_account_info() if self.bridge else {}
        free = float(acct.get("free_margin", 0) or 0)
        sym = (self.entry.symbol or "").upper()
        # Optional per-symbol fixed qty (GB_QTY_<SYMBOL>), else auto-size.
        fixed_qty = os.getenv(f"GB_QTY_{sym}", "")
        if fixed_qty:
            qty = float(fixed_qty)
        else:
            qty = level_usd(min_notional, free) / price
        # Optional per-symbol level count (GB_LEVELS_<SYMBOL>).
        sym_levels = os.getenv(f"GB_LEVELS_{sym}", "")
        if sym_levels:
            self.core.grid_levels = max(int(sym_levels), 1)
        qty = (int(qty / step + 0.9999)) * step
        qty = max(qty, step)
        # Hard capacity clamp (never exceeds half the account's buying power):
        # keeps margin headroom so a full grid + adverse move can't liquidate.
        lev = float(os.getenv("GB_LEVERAGE", os.getenv("BINANCE_LEVERAGE", "10")) or 10)
        cap_notional = (free * lev) / 2.0
        if free > 0 and qty * price > cap_notional:
            qty = (int((cap_notional / price) / step)) * step
            qty = max(qty, step)
            self._log(f"⚠️ {sym} sized by capacity clamp "
                      f"${qty * price:.0f}/level (max {cap_notional:.0f})")
        # B) ATR-adaptive spacing (recomputed on recenter too, since qty is reset)
        atr_raw = atr_pct(self._bars("15m"), self.core.t.atr_period)
        self.core.compute_spacing(atr_raw, self._log)
        # C) session-mapped distance overrides the ATR floor for the current
        # market session (Asia tight / London medium / NY wide).
        sess_sp = self._session_spacing(price, atr_raw)
        if sess_sp:
            self.core.spacing_pct = sess_sp[1]
            self._log(f"🗓 {sess_sp[0]} session → spacing {sess_sp[1] * 100:.3f}% "
                      f"(${sess_sp[1] * price:.0f}/side)")
        # Re-center whenever price leaves the ladder by ~2.5 grid spacings
        # (never more than 1.5% away) — keeps the ladder fresh and bounded.
        self.core.t.recenter_drift = max(0.015, 2.5 * self.core.spacing_pct)
        self._log(f"sized qty={qty} (~${qty * price:.2f}/level, min_notional=${min_notional:.2f}, "
                  f"free_margin=${free:.2f}, spacing={self.core.spacing_pct * 100:.2f}%)")
        return qty

    def _session_spacing(self, price: float, atr_raw: float = 0.0):
        """Session-mapped grid distance → (session, spacing_pct) or None.

        Symbol-specific USD distance (e.g. GB_SPACING_BTC_ASIA_USD=60) wins;
        otherwise the session % (GB_SPACING_ASIA_PCT etc.) applies to all
        symbols. The raw ATR spacing is a safety valve: during volatility
        spikes the ladder automatically widens above the session target, and
        it is never allowed below the fee floor."""
        if os.getenv("GB_SESSION_SPACING", "1") != "1":
            return None
        sess = _session_name(datetime.now(timezone.utc).hour)
        key = (getattr(self.bridge, "binance_symbol", "") or self.entry.symbol).replace("USDT", "")
        usd = _env_f(f"GB_SPACING_{key}_{sess}_USD",
                     _SESSION_USD_DEFAULTS.get(key, {}).get(sess, 0.0))
        pct = _env_f(f"GB_SPACING_{sess}_PCT", _SESSION_PCT_DEFAULTS[sess])
        target = usd / price if usd > 0 else pct
        atr_sp = atr_raw * self.core.t.spacing_atr_mult
        sp = max(target, atr_sp) if atr_raw > 0 else target
        sp = min(max(sp, _SPACING_FEE_FLOOR), _SPACING_CEIL)
        return sess, sp

    def _place_limit(self, side: str, price: float, qty: float):
        if (getattr(self.bridge, "platform", "") or "") == "mt5" \
                and not getattr(self.bridge, "is_connected", True):
            return None
        oid = self.bridge.place_limit_order(side, qty, price, comment="gb-grid")
        if oid:
            self.grid_orders[oid] = {"side": side, "price": price, "qty": qty}
        return oid

    def _place_grid(self, price: float):
        self.core.center = price
        self._reconcile_orders()
        self._log(f"📐 grid at center={price:.5f} | "
                  f"{len(self.grid_orders)} resting | "
                  f"±{self.core.spacing_pct * self.core.active_levels * 100:.1f}%")

    def _escalate(self):
        pairs = self.core.escalate(self._adx_value(), self._part_volume(), self._log)
        if pairs:
            self._log(f"⬆️ scaled in → {self.core.active_levels} level(s)/side (on fill)")
            self._reconcile_orders()  # the new level pair is placed by reconcile

    # ── no-ghost reconciliation ──
    def _desired_orders(self) -> list:
        """The ladder the exchange must match EXACTLY: [(side, price, qty)].

        Prices/quantities use the bridge's own rounding so a placed order
        always matches its desired slot (no cancel/re-place churn).

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
            out.append((side, self.bridge._round_price(px), qty))
        return out

    def _reconcile_orders(self):
        """Enforce the no-ghost invariant: exchange orders == desired ladder.

        Pass 1: cancel every open order that does NOT match a desired slot
        (one order per slot — duplicates are extras too), and do NOT place
        anything while ghosts exist, so the count can only shrink.
        Pass 2: place each desired slot that is missing on the exchange.
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
        for o in oo:
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

        # Crossing guard: never place an order that would fill instantly (BUY
        # at/above the ask, SELL at/below the bid) — e.g. after a price gap,
        # so a stale ladder can't burn slots (and margin) on market orders.
        td = self.bridge.get_tick()
        bid, ask = (td["bid"], td["ask"]) if td else (0.0, 0.0)
        for side, px, qty in desired:
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

    def _handle_fills(self, price: float):
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
                continue
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
            self._log(f"🔒 level {level}: {side} leg open — {side} re-stock paused")
        else:
            self._pair_pending.pop(level, None)  # exit filled → round trip done
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

    def _check_equity_stop(self) -> bool:
        try:
            info = self.bridge._client.futures_position_information(symbol=self.bridge.binance_symbol)
            upnl = sum(float(p.get("unRealizedProfit", 0)) for p in info)
        except Exception:
            upnl = 0.0
        total = self.realized_pnl + upnl
        acct = self.bridge.get_account_info() if self.bridge else {}
        equity = float(acct.get("equity", 0) or 0)
        return self.core.should_equity_stop(total, equity if equity > 0 else 100.0)

    def _recenter(self, price: float):
        drift = (price - self.core.center) / self.core.center * 100
        self._log(f"🔄 drift {drift:+.1f}% -> re-center")
        self._flatten()
        self.core.reset_grid(reset_regime=False)
        self._pair_pending.clear()
        self.qty = self._size_qty(price)
        time.sleep(1)
        self._place_grid(price)

    def _flatten(self):
        """Hard reset, verified: cancel EVERY open order (retry until the
        exchange shows zero), close EVERY position, clear local tracking."""
        self._pair_pending.clear()
        try:
            oo = []
            for attempt in range(3):
                try:
                    self.bridge.cancel_all_orders()
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
            info = self.bridge._client.futures_position_information(
                symbol=self.bridge.binance_symbol)
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

    def is_alive(self) -> bool:
        """True only if the bot thread is actually running right now."""
        return (
            self.status == BotStatus.RUNNING
            and self._thread is not None
            and self._thread.is_alive()
        )

    def status_dict(self) -> dict:
        # Refresh positions from exchange truth on every read (kills stale ghosts)
        if self.bridge:
            try:
                self.positions = self.bridge.get_positions()
            except Exception:
                pass
        price = 0
        if hasattr(self, 'bridge') and self.bridge:
            td = self.bridge.get_tick()
            price = td["bid"] if td else 0
        # surface resting grid limit orders + filled positions
        levels_detail = []
        seen = set()
        # MT5 panel is exchange-truth only. Local grid_orders can show a SELL
        # that IOC already expired — that is what the operator saw vs an empty
        # Trade tab.
        is_mt5 = (getattr(self.bridge, "platform", "") or "") == "mt5"
        if not is_mt5:
            for oid, m in list(self.grid_orders.items())[:12]:
                side = m.get("side", "?")
                px = round(m.get("price", 0), 6)
                seen.add((str(side).upper(), px))
                levels_detail.append({
                    "level": len(levels_detail) + 1,
                    "ticket": oid,
                    "type": side,
                    "volume": round(m.get("qty", 0), 6),
                    "price_open": px,
                    "mark_price": round(price, 6),
                    "notional": round(m.get("qty", 0) * (price or m.get("price", 0)), 2),
                    "pnl": 0,
                    "tp_target": 0,
                })
        try:
            for o in (self.bridge.get_open_orders() if self.bridge else []) or []:
                side = (o.get("side") or o.get("type") or "?").upper()
                if side in ("LIMIT", "STOP", "STOP_LIMIT"):
                    side = (o.get("side") or "?").upper()
                px = round(float(o.get("price") or 0), 6)
                if px <= 0 or (side, px) in seen:
                    continue
                seen.add((side, px))
                qty = float(o.get("volume") or o.get("qty") or 0)
                levels_detail.append({
                    "level": len(levels_detail) + 1,
                    "ticket": o.get("ticket", 0),
                    "type": side,
                    "volume": round(qty, 6),
                    "price_open": px,
                    "mark_price": round(price, 6),
                    "notional": round(qty * (price or px), 2),
                    "pnl": 0,
                    "tp_target": 0,
                })
        except Exception:
            pass
        for p in self.positions:
            entry = p.get("price_open", 0)
            qty = p.get("volume", 0)
            levels_detail.append({
                "level": len(levels_detail) + 1,
                "ticket": p.get("ticket", 0),
                "type": p.get("type", "?"),
                "volume": round(qty, 6),
                "price_open": round(entry, 6),
                "mark_price": round(price, 6),
                "notional": round(qty * (price or entry), 2),
                "pnl": round(p.get("profit", 0), 4),
                "tp_target": round(p.get("tp_target", 0), 6),
            })
        total_pnl = round(sum(p.get("profit", 0) for p in self.positions), 4)
        return {
            "bot_id": self.bot_id,
            "name": self.entry.name,
            "symbol": self.entry.symbol,
            "status": self.status.value,
            "market_type": self.entry.market_type,
            "fee_mode": self.entry.fee_mode,
            "levels": len(levels_detail),
            "levels_detail": levels_detail,
            "total_pnl": total_pnl,
            "retry_count": self._retry_count,
            "log": self.logs[-5:],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────────────────────

orchestrator = BotOrchestrator()
