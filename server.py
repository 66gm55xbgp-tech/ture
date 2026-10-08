"""
HybridGB Open — all-in-one server: JSON API + serves dashboard-v2/ statically.
Single-user, local SQLite store, no cloud accounts. See README.md.
"""
import json, logging, os, time
import asyncio
import threading
from typing import Dict, Optional, Union

# Load .env BEFORE any scanning
from dotenv import load_dotenv; load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

from storage import save_chat_message, get_chat_history
from orchestrator import orchestrator
from account_scanner import scanner as account_scanner
from memory_guard import (
    snapshot as _mem_snapshot, assert_capacity, MemoryLimitError,
    clamp_guru_n, count_running_binance, assert_binance_slots,
    MAX_BINANCE_BOTS, MAX_GURU_BOTS,
)
import activity  # plain-language activity feed ("chatter") for the dashboard

# Set active env from account scanner discovery
_detected = account_scanner.get_all()
if any(a["env"] == "demo" for a in _detected):
    _active_env = "demo"
elif any(a["env"] == "live" for a in _detected):
    _active_env = "live"

logger = logging.getLogger("hybrid.server")
app = FastAPI(title="GB Orchestrator API", version="3.0.0")

# The dashboard is an operator console, not a public trading API.  Keep the
# credential out of git and provision it only in the production .env.
ADMIN_TOKEN = os.getenv("DASHBOARD_ADMIN_TOKEN", "")
ALLOWED_ORIGINS = [
    origin.strip() for origin in os.getenv(
        "DASHBOARD_ALLOWED_ORIGINS",
        "http://localhost:9100,http://127.0.0.1:9100",
    ).split(",") if origin.strip()
]

_PUBLIC_PATHS = {"/", "/main.js", "/vendor/lightweight-charts.js",
                 "/api/auth/login", "/api/auth/session",
                 "/api/mt5/state", "/api/mt5/register"}   # EA-facing: own X-GB-Auth token auth


def _req_user(request: Request):
    """The authenticated Supabase user id for a request (or None for admin-token)."""
    return getattr(request.state, "user", None) and request.state.user.get("id")


def _check_capacity():
    """Refuse to spawn when system memory is at the critical threshold."""
    try:
        assert_capacity()
    except MemoryLimitError as e:
        raise HTTPException(status_code=429, detail=str(e))


def _check_binance_fleet(user_id: str = None, extra: int = 1):
    """Refuse when this user already has the 1.9Gi Binance fleet cap."""
    try:
        assert_capacity()
        assert_binance_slots(orchestrator, user_id, extra=extra)
    except MemoryLimitError as e:
        raise HTTPException(status_code=429, detail=str(e))


_SESSIONS: dict = {}  # Bearer token -> {"email": ...} (in-memory, single user)


def _local_user(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth[7:].strip() in _SESSIONS:
        return {"id": "local",
                "email": _SESSIONS[auth[7:].strip()].get("email", "local")}
    if ADMIN_TOKEN and request.headers.get("X-GB-Admin-Token") == ADMIN_TOKEN:
        return {"id": "local", "email": "local"}
    return None


@app.middleware("http")
async def require_dashboard_admin(request: Request, call_next):
    """Single-user auth. Localhost is trusted (default bind 127.0.0.1);
    remote callers need a login session or the admin token."""
    request.state.user = None
    if request.method == "OPTIONS" or request.url.path in _PUBLIC_PATHS:
        return await call_next(request)
    u = _local_user(request)
    if u:
        request.state.user = u
        return await call_next(request)
    try:
        host = (request.client.host if request.client else "")
    except Exception:
        host = ""
    if host in ("127.0.0.1", "::1", "localhost", ""):
        request.state.user = {"id": "local", "email": "local"}
        return await call_next(request)
    return JSONResponse(status_code=401, content={"detail": "Sign in required"})

# Allow only the deployed dashboard and explicit local development origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

from bridge import BinanceBridge
import threading
_bridges: Dict[str, BinanceBridge] = {}
_bridges_lock = threading.Lock()
_active_env = "demo"  # Default — overridden by request.env from dashboard
_account_cache: Dict[str, any] = {}
_last_account_fetch: float = 0
_health_cache: dict = {}  # cache_key -> {at, data}  (one slot per user+desk)
_HEALTH_TTL = 2.0
_health_lock = threading.Lock()


def _invalidate_health():
    with _health_lock:
        _health_cache.clear()


# ═══════════════════════════════════════════════════════════════════════════
# Auth — Supabase login + per-user session
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/auth/login")
async def auth_login(body: dict = None):
    """Local single-operator login. If ADMIN_PASSWORD is set (env or Settings),
    it must match; otherwise localhost is trusted (see README)."""
    import secrets as _secrets
    body = body or {}
    email = (body.get("email") or "").strip() or "local"
    password = body.get("password") or ""
    import local_store
    need = os.getenv("ADMIN_PASSWORD", "") or str(
        local_store.get_setting("admin_password") or "")
    if need and password != need:
        raise HTTPException(401, "Invalid password")
    tok = _secrets.token_urlsafe(24)
    _SESSIONS[tok] = {"email": email}
    return {"access_token": tok, "refresh_token": "",
            "user": {"id": "local", "email": email}}


@app.get("/api/auth/me")
async def auth_me(request: Request):
    """Current user profile + which credentials are configured (masked)."""
    user = request.state.user
    if not user:
        raise HTTPException(401, "Not authenticated")
    import local_store
    credential_summary = [
        {"broker": c.get("venue"), "env": c.get("env"),
         "credential_type": "api_key", "configured": True}
        for c in local_store.get_credentials()
    ]
    return {
        "user": {"id": "local", "email": user.get("email", "local")},
        "profile": {},
        "credentials": credential_summary,
    }


@app.get("/api/auth/session")
async def auth_session():
    """Public no-op health used by the dashboard to probe login state."""
    return {"ok": True}

# GuruAI managers (lazy-init with env, re-init if env changed)
# Two groups per (user_id, env): "meme" (sub-$1 coins) + "all" (full-market).
_gurus: Dict[str, any] = {}
_GURU_GROUPS = ("meme", "all")
def _get_guru(env: str = None, user_id: str = None, group: str = "all",
              platform: str = "binance"):
    e = env or _active_env
    platform = (platform or "binance").lower()
    key = f"{user_id or '-'}:{e}:{group}:{platform}"
    if env is None:
        # prefer the manager that actually has bots running (for this user)
        for gk, g in _gurus.items():
            if (user_id is None or gk.startswith(f"{user_id}:")) and g.is_running():
                if platform and not gk.endswith(f":{platform}"):
                    continue
                return g
    if key not in _gurus:
        engine = os.getenv("GURU_ENGINE", "dynamic")
        if engine == "dynamic":
            from dynamic_guruai import init_guru
        else:
            from guru_ai import init_guru
        _gurus[key] = init_guru(orchestrator, e, user_id=user_id, group=group,
                                platform=platform)
    return _gurus[key]

def _guru_all(user_id: str = None, platform: str = None) -> list:
    """All guru managers for this user (both groups), across cached envs."""
    plat = (platform or "").lower()
    out = []
    for k, g in _gurus.items():
        if user_id and not k.startswith(f"{user_id}:"):
            continue
        if plat and not k.endswith(f":{plat}"):
            continue
        out.append(g)
    return out

def _get_bridge(symbol: str, market_type: str = "futures", env: str = None,
                user_id: str = None) -> BinanceBridge:
    e = env or _active_env
    # Key includes the owner so concurrent users never share (and cross-credit) a bridge.
    key = f"{user_id or '-'}:{symbol}_{market_type}_{e}"
    with _bridges_lock:
        old = _bridges.get(key)
        if old is not None and old.is_connected:
            return old
        if old is not None:
            try:
                old.disconnect()
            except Exception:
                pass
        b = BinanceBridge(symbol=symbol, environment=e, market_type=market_type,
                          user_id=user_id)
        b.connect()
        _bridges[key] = b
        return b

def _fetch_account_raw(env: str = None, user_id: str = None):
    """Fetch account info from Binance REST (no WebSocket). Updates cache."""
    global _account_cache, _last_account_fetch
    mode = env or _active_env
    cache_key = f"acct:{user_id or '-'}:{mode}"
    if _last_account_fetch > 0 and time.time() - _last_account_fetch < 30 and cache_key in _account_cache:
        return _account_cache[cache_key]
    try:
        from dotenv import load_dotenv; load_dotenv()
        from binance.client import Client
        try:
            import local_store
            _k, _sec = local_store.resolve_binance_keys(mode)
            uk = {"api_key": _k, "api_secret": _sec} if (_k or _sec) else None
        except Exception:
            uk = None
        if uk:
            api_key, api_secret = uk["api_key"], uk["api_secret"]
        elif user_id and user_id != "local":
            # Strict isolation: an unknown user must not fall back to .env.
            return _account_cache.get(cache_key, {})
        elif mode == "demo":
            api_key, api_secret = os.getenv("BINANCE_DEMO_API_KEY",""), os.getenv("BINANCE_DEMO_API_SECRET","")
        else:
            # Never fall back to the .env LIVE operator keys for an unscoped
            # request — live requires an explicit user's keys (see bridge).
            return _account_cache.get(cache_key, {})
        c = Client(api_key, api_secret, testnet=(mode == "demo"))
        if not api_key:
            return _account_cache.get(cache_key, {})
        acct = c.futures_account()
        _last_account_fetch = time.time()
        _account_cache[cache_key] = {
            "balance": round(float(acct.get("totalWalletBalance", 0)), 2),
            "equity": round(float(acct.get("totalMarginBalance", 0)), 2),
            "margin": round(float(acct.get("totalInitialMargin", 0)), 2),
            "free_margin": round(float(acct.get("availableBalance", 0)), 2),
        }
    except Exception:
        pass
    return _account_cache.get(cache_key, {})


class ChatRequest(BaseModel):
    question: str
    source: str = "web"
    author: str = "you"
    platform: str = "binance"

class SpawnRequest(BaseModel):
    name: str = "BOT 1"
    symbol: str = "BTCUSDT"
    market_type: str = "futures"
    fee_mode: str = "maker"
    platform: str = "binance"
    env: str = "demo"
    config: dict = {}
    vol_pct: int = 50  # global slider 10-100% of available funds for this grid, default 50%
    leverage: Union[int, str] = 10  # 5/10/25/50/75/100/MAX, invalid -> 10
    mode: str = "scalp"  # scalp | swing
    compound: bool = True  # compound legs as % of live equity
    compound_frac: float = 0.02  # 2% of equity per leg


# ═══════════════════════════════════════════════════════════════════════════
# Bots
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health(request: Request):
    # list_bots refreshes exchange state.  A dashboard polling every few
    # seconds must not turn into a Binance rate-limit incident.
    now = time.time()
    user_id = _req_user(request)
    platform = (request.query_params.get("platform") or "").strip().lower() or None
    cache_key = f"{user_id or '-'}:{platform or '*'}"
    with _health_lock:
        slot = _health_cache.get(cache_key)
        if slot and now - slot["at"] < _HEALTH_TTL:
            return slot["data"]
    data = {
        "bots": orchestrator.list_bots(user_id=user_id, platform=platform),
        "boot_mode": orchestrator._boot_mode,
        "platform": platform or "all",
    }
    if platform == "mt5":
        try:
            tok = _mt5_token_for((user_id or "local"))
            if tok:
                from services import mt5_link
                data["ea_book"] = mt5_link.account_book(tok)
        except Exception:
            pass
    with _health_lock:
        _health_cache[cache_key] = {"at": time.time(), "data": data}
    return data

@app.get("/api/system/resources")
async def system_resources(request: Request):
    """Current host memory usage + Binance fleet cap (3 bots on this VM)."""
    s = _mem_snapshot()
    uid = _req_user(request)
    running = count_running_binance(orchestrator, uid)
    fleet_ok = running < MAX_BINANCE_BOTS
    can = bool(s.get("available_for_spawn", True)) and fleet_ok
    return {
        "memory": s,
        "can_spawn": can,
        "fleet": {
            "running": running,
            "max": MAX_BINANCE_BOTS,
            "guru_max": MAX_GURU_BOTS,
            "remaining": max(0, MAX_BINANCE_BOTS - running),
            "platform": "binance",
        },
    }

# ═══════════════════════════════════════════════════════════════════════════
# Activity feed — plain-language chatter per platform (dashboard ticker)
# ═══════════════════════════════════════════════════════════════════════════

def _all_bot_snapshots() -> list:
    """Bot log snapshots for the feed ingester (in-memory only — no API calls)."""
    def _sym(sub):
        sym = getattr(sub, "symbol", None)
        if sym:
            return sym
        e = getattr(sub, "entry", None)
        return getattr(e, "symbol", "?") if e is not None else "?"
    out = []
    seen = set()
    for bid, sub in list(orchestrator._bots.items()):
        seen.add(bid)
        out.append({
            "id": bid,
            "symbol": _sym(sub),
            "platform": activity.bot_platform(sub),
            "logs": list(getattr(sub, "logs", [])),
        })
    for g in _guru_all(None):
        for bid, b in list(g._bots.items()):
            if bid in seen:
                continue
            out.append({
                "id": bid, "symbol": getattr(b, "symbol", "?"),
                "platform": "binance", "logs": list(getattr(b, "logs", [])),
            })
    return out


def _feed_ingester():
    while True:
        try:
            activity.ingest(_all_bot_snapshots())
        except Exception:
            pass
        time.sleep(4)


def _rss_watch():
    """Lightweight RSS/swap log. Does not use tracemalloc (that itself leaks)."""
    import gc
    while True:
        time.sleep(30)
        try:
            s = _mem_snapshot()
            rss = s.get("process_rss_mb") or 0
            if s.get("critical") or rss >= 500:
                logger.warning(
                    f"mem rss={rss}MB used={s.get('used_pct')}% "
                    f"avail={s.get('available_mb')}MB swap={s.get('swap_used_pct')}% "
                    f"bots={count_running_binance(orchestrator)}")
            if rss >= 600:
                gc.collect()
        except Exception:
            pass


def _mem_watch():
    """Temporary leak hunter: tracemalloc top-allocation log (GB_MEM_WATCH=1)."""
    import tracemalloc
    try:
        tracemalloc.start(15)
    except Exception:
        return
    while True:
        time.sleep(90)
        try:
            snap = tracemalloc.take_snapshot()
            stats = snap.statistics("lineno")
            total = sum(s.size for s in stats) / 1048576.0
            big = sorted(stats, key=lambda s: -s.size)[:12]
            parts = []
            for s in big:
                f = s.traceback[0].filename.replace(os.path.dirname(__file__), "")
                parts.append(f"{s.size/1048576:.1f}MB {f}:{s.traceback[0].lineno} x{s.count}")
            logger.info(f"🧠 memwatch total={total:.0f}MB :: " + " | ".join(parts))
            by_tb = snap.statistics("traceback")
            by_tb = sorted(by_tb, key=lambda s: -s.size)[:5]
            for s in by_tb:
                frames = " <- ".join(
                    f"{f.filename.replace(os.path.dirname(__file__), '')}:{f.lineno}" for f in s.traceback[:6])
                logger.info(f"🧠 memwatch stack {s.size/1048576:.1f}MB x{s.count} :: {frames}")
            by_cnt = sorted(snap.statistics("traceback"), key=lambda s: -s.count)[:8]
            for s in by_cnt:
                frames = " <- ".join(
                    f"{f.filename.replace(os.path.dirname(__file__), '')}:{f.lineno}" for f in s.traceback[:6])
                logger.info(f"🧠 memwatch hot x{s.count} :: {frames}")
        except Exception:
            pass


@app.get("/api/feed")
async def feed_api(platform: str = "all", after: float = 0.0, limit: int = 60,
                   request: Request = None):
    """Chatter items for one platform tab (binance / mt5 / system / all)."""
    if request:
        _req_user(request)
    return {"items": activity.recent(platform if platform != "all" else None,
                                     after=after, limit=min(int(limit), 200))}


@app.get("/api/feed/summarize")
async def feed_summarize(platform: str = "binance", minutes: int = 30,
                         request: Request = None):
    """Optional AI summary of recent activity (plain language, non-technical)."""
    if request:
        _req_user(request)
    return activity.summarize(platform, minutes=min(int(minutes), 1440))


@app.post("/spawn")
async def spawn_bot(request: SpawnRequest, http_request: Request):
    _check_capacity()
    user_id = _req_user(http_request)
    if request.platform == "mt5":
        # MT5: build the bridge over the user's EA link. The EA's
        # ACCOUNT_TRADE_MODE decides demo/live — a mismatch is rejected.
        token = _mt5_token_for(user_id or "")
        if not token:
            raise HTTPException(400, "No MT5 token — generate one in Settings ▸ MT5")
        from bridge_mt5 import MT5Bridge
        from services import mt5_link
        if request.symbol not in mt5_link.assigned_symbols(token):
            raise HTTPException(
                400, f"No EA attached to {request.symbol} — attach the "
                     f"HybridGB EA to that chart and try again")
        bridge = MT5Bridge(token=token, symbol=request.symbol,
                           environment=request.env, user_id=user_id)
        if not bridge.connect():
            raise HTTPException(
                400, f"MT5 EA not connected for {request.symbol} "
                     f"(or EA account is not {request.env}) — attach the EA "
                     f"to the {request.symbol} chart and check the token")
        bot_id = orchestrator.spawn(
            name=request.name, symbol=request.symbol,
            market_type=request.market_type, fee_mode=request.fee_mode,
            bridge=bridge, platform="mt5", user_id=user_id)
    else:
        # Binance spawn = GuruAI engine, one user-picked symbol (no scan/rotation).
        # Admin-token (dashboard health) fallback to GURU_MONITOR_USER_ID so manual
        # spawn via API still resolves live wallet (live requires user keys).
        if not user_id:
            user_id = "local"
        _check_binance_fleet(user_id, extra=1)
        from guru_ai import register_bank_manager
        acct = _fetch_account_raw(env=request.env, user_id=user_id)
        free = float(acct.get("free_margin", 0) or 0)
        # global slider 10-100% of available funds for this grid, default 50% → 0.5$ per 1% on 25$ linear to 250$
        vol_pct = int(getattr(request, 'vol_pct', 50) or 50)
        vol_pct = max(10, min(100, vol_pct))
        from guru_ai import clean_leverage
        lev = clean_leverage(getattr(request, 'leverage', 10))
        g = _get_guru(env=request.env, user_id=user_id, group="manual")
        # pass original free + vol_pct to size with vol% of free, best for account scaling (per_level scaled vol/50)
        result = g.spawn_symbol(request.symbol, request.name, free, vol_pct=vol_pct, leverage=lev,
                                mode=(getattr(request, 'mode', 'scalp') or 'scalp'),
                                compound=getattr(request, 'compound', True),
                                compound_frac=getattr(request, 'compound_frac', 0.02))
        if not result.get("ok"):
            raise HTTPException(400, result.get("error") or "Spawn failed")
        register_bank_manager(g)
        bot_id = result["bot_id"]
    if bot_id:
        _invalidate_health()
        plat = (request.platform or "binance").lower()
        activity.feed(plat, f"Bot started on {request.symbol}",
                      bot=bot_id, symbol=request.symbol)
        return {"status": "spawned", "bot_id": bot_id}
    raise HTTPException(500, "Spawn failed")

@app.post("/stop/{bot_id}")
async def stop_bot(bot_id: str, request: Request):
    if orchestrator.stop_bot(bot_id, user_id=_req_user(request)):
        _invalidate_health()
        activity.feed("system", f"Bot {bot_id} stopped and flattened")
        return {"status": "stopped"}
    raise HTTPException(404)

@app.delete("/api/bots/{bot_id}")
async def delete_bot(bot_id: str, request: Request):
    """Remove a bot definition from the registry (side panel cleanup).
    Only allowed when the bot is not running — a live bot must be stopped
    (its stop already removes the row) or have no process attached."""
    user_id = _req_user(request)
    if not orchestrator._owns(bot_id, user_id):
        raise HTTPException(404)
    sub = orchestrator._bots.get(bot_id)
    if sub and sub.is_alive():
        raise HTTPException(400, "Stop the bot before deleting it")
    orchestrator._delete_from_registry(bot_id)
    orchestrator._registry.pop(bot_id, None)
    return {"status": "deleted"}

@app.post("/pause/{bot_id}")
async def pause_bot(bot_id: str, request: Request):
    if orchestrator.pause(bot_id, user_id=_req_user(request)):
        return {"status": "paused"}
    raise HTTPException(404)

@app.post("/resume/{bot_id}")
async def resume_bot(bot_id: str, request: Request):
    if orchestrator.resume(bot_id, user_id=_req_user(request)):
        return {"status": "resumed"}
    raise HTTPException(404)


# ═══════════════════════════════════════════════════════════════════════════
# GuruAI — volatility-harvesting neutral grid (top sub-$0.50 movers)
# ═══════════════════════════════════════════════════════════════════════════

class GuruStartRequest(BaseModel):
    env: str = "live"
    n_bots: int = 10
    vol_pct: int = 50  # per-level size multiplier (50=1x, 100=2x)
    leverage: Union[int, str] = 10  # 5/10/25/50/75/100/MAX, invalid -> 10
    wallet_pct: int = 70  # max share of free margin the fleet may lock (10-100)
    mode: str = "scalp"  # scalp | swing
    compound: bool = True
    compound_frac: float = 0.02


class BacktestRequest(BaseModel):
    symbol: str = "BTCUSDT"
    horizon: str = "1W"  # 1W/2W/3W/4W/1M (JEV only for 1W)
    platform: str = "binance"  # binance | mt5
    env: str = "demo"
    per_level_usd: float = 1000.0
    leverage: Union[int, str] = 10
    mode: str = "scalp"  # scalp | swing
    compound: bool = True
    compound_frac: float = 0.02
    start_equity: float = 10000.0  # backtest account size = liquidation floor


class PropModeRequest(BaseModel):
    enabled: bool = False


@app.get("/api/prop/mode")
async def prop_mode_get():
    from guru_ai import prop_mode, prop_flat_reason
    return {"ok": True, "enabled": prop_mode(), "blocked_reason": prop_flat_reason()}


@app.post("/api/prop/mode")
async def prop_mode_set(body: PropModeRequest):
    os.environ["GB_PROP_MODE"] = "1" if body.enabled else "0"
    from guru_ai import prop_mode, prop_flat_reason
    return {"ok": True, "enabled": prop_mode(), "blocked_reason": prop_flat_reason()}


@app.get("/api/prop/status")
async def prop_status():
    """v1.0 production: Maven $10K evaluation snapshot for the Prop UI panel.
    Risk-only view (strategy untouched): target progress, drawdown vs limit,
    floating heat, governor state, per-symbol open legs."""
    from guru_ai import prop_mode
    try:
        from risk_engine import PortfolioGovernor, load_prop_config
        gov = PortfolioGovernor(profile=load_prop_config())
        cfg = gov.profile
    except Exception as e:
        return {"ok": False, "error": f"risk engine unavailable: {e}"[:200]}
    equity = peak = floating = 0.0
    legs: dict = {}
    tick = {"risk_state": "UNKNOWN", "reason": "",
            "allow_new_entries": True, "emergency_flatten": False}
    try:  # live desk snapshot: summed bot P&L + governor verdict (real data)
        from prod_guard import guard as _pguard, desk_snapshot
        _env = "demo"  # desk snapshot aggregates the demo desk; live per user keys
        _snap = desk_snapshot(_env)
        _v = _pguard().account_verdict(
            _snap["desk_pnl"], _snap["floating"], _snap["legs"], env=_env,
            per_symbol_float=_snap["per_symbol_float"])
        equity, floating, legs = _v["equity"], _v["floating"], _snap["legs"]
        peak = _v.get("peak", equity)
        tick = _v
    except Exception as e:
        tick = {"risk_state": "ERROR", "reason": str(e)[:120],
                "allow_new_entries": True, "emergency_flatten": False}
    return {"ok": True, "prop_enabled": prop_mode(),
            "strategy_version": "JEV-PORTFOLIO-v1.0-FROZEN",
            "firm": getattr(cfg, "firm", "maven"), "account_size": cfg.account_size,
            "profit_target": cfg.profit_target, "max_drawdown": cfg.max_drawdown,
            "equity": equity, "floating": floating, "open_legs": legs, **tick}


_BT_JOBS: dict = {}


@app.post("/api/backtest/run")
async def backtest_run(body: BacktestRequest, request: Request):
    """Start an offline grid replay (background thread). Poll result endpoint."""
    import threading, uuid
    from guru_ai import clean_leverage
    user_id = _req_user(request)
    horizon = (body.horizon or "1W").upper()
    if horizon not in ("1W", "2W", "3W", "4W", "1M", "2M", "3M", "5M", "6M"):
        raise HTTPException(400, "horizon must be 1W/2W/3W/4W/1M/2M/3M/5M/6M")
    token = None
    if (body.platform or "binance") == "mt5":
        if not user_id:
            raise HTTPException(400, "MT5 backtest needs a logged-in user")
        from services import mt5_link
        import local_store
        for r in local_store.get_credentials():
            if r.get("venue") == "mt5" and r.get("api_secret"):
                token = r["api_secret"]
                break
        if not token:
            raise HTTPException(400, "No MT5 token — generate one in Settings ▸ MT5")
    jid = uuid.uuid4().hex[:12]
    _BT_JOBS[jid] = {"status": "running", "result": None, "error": None}

    def _work():
        try:
            import backtest as _bt
            _BT_JOBS[jid]["result"] = _bt.run_backtest(
                (body.symbol or "BTCUSDT").upper(), horizon, body.platform or "binance",
                user_id=user_id, env=body.env or "demo", token=token,
                per_level_usd=float(body.per_level_usd or 1000.0),
                leverage=clean_leverage(getattr(body, "leverage", 10)),
                mode=(getattr(body, "mode", "scalp") or "scalp"),
                compound=getattr(body, "compound", True),
                compound_frac=getattr(body, "compound_frac", 0.02),
                cost_bps=float(getattr(body, "cost_bps", 3.0) or 3.0),
                start_equity=float(getattr(body, "start_equity", 10000.0) or 10000.0))
            _BT_JOBS[jid]["status"] = "done"
        except Exception as e:
            _BT_JOBS[jid]["status"] = "error"
            _BT_JOBS[jid]["error"] = str(e)[:300]
    threading.Thread(target=_work, daemon=True, name=f"backtest-{jid}").start()
    return {"ok": True, "job_id": jid}


@app.get("/api/backtest/result/{jid}")
async def backtest_result(jid: str):
    job = _BT_JOBS.get(jid)
    if not job:
        raise HTTPException(404, "unknown backtest job")
    return {"ok": True, "status": job["status"], "result": job["result"],
            "error": job["error"]}


class GuruAdminStartRequest(BaseModel):
    env: str = "live"
    user_id: str


@app.post("/api/guru/admin_start")
async def guru_admin_start(body: GuruAdminStartRequest):
    """Admin-token equivalent of the dashboard 🧠 button: start GuruAI for a
    given user_id without a Supabase JWT (used to automate the restart from
    the server side / cron after a loss cut). Sweeps residue first so the
    fresh hunt always starts from a clean book."""
    _check_binance_fleet(body.user_id, extra=1)
    from guru_ai import _sweep_account_flat
    _sweep_account_flat(body.user_id, body.env)
    user_id = body.user_id
    acct, free_margin = {}, 0.0
    for attempt in range(4):          # rate-limit blips: retry the account fetch
        acct = _fetch_account_raw(env=body.env, user_id=user_id)
        free_margin = float(acct.get("free_margin", 0))
        if free_margin > 0:
            break
        time.sleep(5)
    if free_margin <= 0:
        raise HTTPException(400, "could not read live account (rate-limited?) — retry in a minute")
    started, errors = [], []
    exclude = set()
    try:
        exclude |= orchestrator.live_symbols(user_id, "binance", body.env)
    except Exception:
        pass
    for grp in _GURU_GROUPS:
        g = _get_guru(env=body.env, user_id=user_id, group=grp)
        g.prune()
        if g.n_bots <= 0:                       # disabled group (e.g. meme=0)
            continue
        result = g.start(free_margin=free_margin, exclude=tuple(exclude))
        if result.get("ok"):
            from guru_ai import register_bank_manager
            register_bank_manager(g)
            exclude.update(s["symbol"] for s in result["started"])
            started.extend(result["started"])
        else:
            errors.append(f"{grp}: {result.get('error')}")
    if not started:
        raise HTTPException(400, "; ".join(errors) or "GuruAI start failed")
    _invalidate_health()
    return {"ok": True, "started": started, "errors": errors}


@app.post("/api/guru/start")
async def guru_start(body: GuruStartRequest = GuruStartRequest(), request: Request = None):
    _check_capacity()
    user_id = _req_user(request) if request else None
    # admin-token fallback so dashboard health check and manual spawn share same wallet
    if not user_id:
        user_id = "local"
    acct, free_margin = {}, 0.0
    for attempt in range(4):          # rate-limit blips: retry the account fetch
        acct = _fetch_account_raw(env=body.env, user_id=user_id)
        free_margin = float(acct.get("free_margin", 0))
        if free_margin > 0:
            break
        time.sleep(5)
    if free_margin <= 0:
        raise HTTPException(400, "could not read live account (rate-limited?) — retry in a minute")
    # meme group first (sub-$1 movers), then the full-market group excluding
    # whatever the meme group picked up (no duplicate bots).
    n_all = clamp_guru_n(body.n_bots or 10)
    from guru_ai import run_vol_pct, clean_leverage, wallet_use_frac
    _vol = run_vol_pct(getattr(body, 'vol_pct', 50))
    _lev = clean_leverage(getattr(body, 'leverage', 10))
    try:
        _wall = max(10, min(100, int(getattr(body, 'wallet_pct', 70) or 70)))
    except (TypeError, ValueError):
        _wall = 70
    _check_binance_fleet(user_id, extra=1)
    started, errors = [], []
    exclude = set()
    try:
        exclude |= orchestrator.live_symbols(user_id, "binance", body.env)
    except Exception:
        pass
    for grp in _GURU_GROUPS:
        g = _get_guru(env=body.env, user_id=user_id, group=grp)
        g.prune()
        if grp == "all":
            g.n_bots = n_all
        if g.n_bots <= 0:                       # disabled group (e.g. meme=0)
            continue
        result = g.start(n=(n_all if grp == "all" else None),
                         free_margin=free_margin, exclude=tuple(exclude),
                         vol_pct=_vol, leverage=_lev, wallet_pct=_wall,
                         mode=(getattr(body, 'mode', 'scalp') or 'scalp'),
                         compound=getattr(body, 'compound', True),
                         compound_frac=getattr(body, 'compound_frac', 0.02))
        if result.get("ok"):
            from guru_ai import register_bank_manager
            register_bank_manager(g)
            exclude.update(s["symbol"] for s in result["started"])
            started.extend(result["started"])
        else:
            errors.append(f"{grp}: {result.get('error')}")
    if not started:
        raise HTTPException(400, "; ".join(errors) or "GuruAI start failed")
    _invalidate_health()
    return {"ok": True, "started": started, "errors": errors}


@app.post("/api/guru/stop")
async def guru_stop(request: Request):
    """Stop auto GuruAI (meme+all). Manual Binance spawns stay up — stop those
    with the per-bot Stop button."""
    user_id = _req_user(request)
    from guru_ai import unregister_bank_manager
    stopped = 0
    for g in _guru_all(user_id, platform="binance"):
        if getattr(g, "group", "all") == "manual":
            continue
        unregister_bank_manager(g)
        stopped += g.stop_all()
    return {"status": "stopped", "count": stopped}


class GuruKillRequest(BaseModel):
    env: str = "live"
    user_id: str = None


@app.post("/api/guru/kill")
async def guru_kill(request: Request, body: GuruKillRequest = None):
    """EMERGENCY KILL SWITCH: stop every guru bot, cancel every open order,
    close every position (account-level sweep), and cancel any pending
    loss-cut auto-restart. One click = the account goes flat."""
    body = body or GuruKillRequest()
    user_id = _req_user(request) or body.user_id
    from guru_ai import (unregister_bank_manager, _sweep_account_flat,
                         _bank_state, _bank_stop)
    # cancel any pending loss-cut auto-restart immediately
    _bank_state["restart_at"] = 0.0
    _bank_stop.set()
    gs = _guru_all(user_id, platform="binance")
    for g in gs:
        unregister_bank_manager(g)
    stopped = sum(g.stop_all() for g in gs)
    uid, env = user_id, body.env
    if not uid and gs:
        uid, env = gs[0].user_id, gs[0].env
    cleaned = _sweep_account_flat(uid, env) if uid else 0
    for g in gs:
        g.prune()
    # VERIFY the book is actually flat (no stale/missing residue).
    flat_ok = True
    residue = {"orders": 0, "positions": 0}
    if uid:
        try:
            from bridge import rest_client
            c = rest_client(uid, env)
            if c is not None:
                residue["orders"] = len(c.futures_get_open_orders() or [])
                op = [p for p in (c.futures_position_information() or [])
                      if abs(float(p.get("positionAmt", 0) or 0)) > 1e-8]
                residue["positions"] = len(op)
                flat_ok = residue["orders"] == 0 and residue["positions"] == 0
                if not flat_ok:
                    # one more sweep pass, then re-verify
                    cleaned += _sweep_account_flat(uid, env)
                    residue["orders"] = len(c.futures_get_open_orders() or [])
                    op = [p for p in (c.futures_position_information() or [])
                          if abs(float(p.get("positionAmt", 0) or 0)) > 1e-8]
                    residue["positions"] = len(op)
                    flat_ok = residue["orders"] == 0 and residue["positions"] == 0
        except Exception as e:
            flat_ok = False
    _invalidate_health()
    return {"status": "killed", "stopped": stopped, "cleaned": cleaned,
            "flat": flat_ok, "residue": residue}


def _guru_bot_row(b) -> dict:
    sd = {}
    try:
        sd = b.status_dict() or {}
    except Exception:
        sd = {}
    return {
        "bot_id": getattr(b, "bot_id", ""),
        "name": getattr(b, "name", ""),
        "symbol": getattr(b, "symbol", ""),
        "status": b.status.value if getattr(b, "status", None) else "unknown",
        "cycles": getattr(b, "cycles", 0),
        "pnl": sd.get("total_pnl", 0),
        "net_position": sd.get("net_position", 0),
        "levels_detail": sd.get("levels_detail") or [],
        "bias": getattr(b, "bias", "neutral"),
        "bias_source": getattr(b, "_bias_source", "rules"),
    }


@app.get("/api/guru/status")
async def guru_status(request: Request):
    user_id = _req_user(request)
    from guru_ai import (_bank_state, _market_gate_asleep, _session_allow,
                         SESSION_LOCK_ENABLED, _SCAN_CACHE)

    def _session_status() -> str:
        if not SESSION_LOCK_ENABLED:
            return "lock-off"
        mov_ok, anc_ok = _session_allow()
        if mov_ok and anc_ok:
            return "open"
        if anc_ok:
            return "movers-parked"
        return "parked"
    bots = []
    rotations = 0
    for g in _guru_all(user_id):
        # NOTE: do NOT prune here. The rotation supervisor's dead-slot
        # replacement needs STOPPED bots to remain in the manager map so it
        # can swap them for fresh movers after the 45-min cooldown. Pruning
        # on every status poll (the learning-memory monitor polls 1/min)
        # silently deleted halted bots before rotation could replace them,
        # shrinking the fleet 6 -> 2 (observed 2026-08-16). A GET must not
        # mutate state; start/kill/stop paths still prune explicitly.
        rotations += g.rotations
        bots.extend(g.active_bots())
    return {
        "running": len(bots) > 0,
        "count": len(bots),
        "rotations": rotations,
        "banks": _bank_state.get("banks", 0),
        "loss_cuts": _bank_state.get("loss_cuts", 0),
        "trail_locks": _bank_state.get("trail_locks", 0),
        "session": _session_status(),
        "bank_pnl": round(_bank_state.get("last_pnl", 0), 2),
        "open_positions": _bank_state.get("n_pos", 0),
        "bank_target_pct": round(_bank_state.get("target_pct", 0), 4),
        "bank_wallet": round(_bank_state.get("wallet", 0), 2),
        "restart_in_sec": max(0, int(_bank_state.get("restart_at", 0) - time.time())),
        "market": "asleep" if _market_gate_asleep() else "awake",
        "scan": [{"symbol": c.get("symbol", ""), "price": c.get("price", 0),
                  "gain_pct": c.get("gain_pct", 0), "gain_15m": c.get("gain_15m", 0),
                  "gain_1h": c.get("gain_1h", 0), "gain_4h": c.get("gain_4h", 0),
                  "quote_vol": c.get("quote_vol", 0), "score": c.get("score", 0)}
                 for c in _SCAN_CACHE.get("pool", [])[:10]],
        "bots": [_guru_bot_row(b) for b in bots],
    }


# ═══════════════════════════════════════════════════════════════════════════
# Account
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/mode")
async def set_mode(request: dict = None):
    global _active_env, _account_cache, _last_account_fetch
    body = request or {}
    new_mode = body.get("mode", "live")
    _active_env = new_mode
    _account_cache = {}
    _last_account_fetch = 0
    return {"mode": _active_env}


@app.get("/api/account")
async def account_info():
    """Legacy: return single active account."""
    a = _fetch_account_raw()
    return {
        "balance": a.get("balance", 0),
        "equity": a.get("equity", 0),
        "margin": a.get("margin", 0),
        "free_margin": a.get("free_margin", 0),
        "mode": _active_env.upper(),
    }


_symbols_cache = {"at": 0.0, "symbols": []}

@app.get("/api/symbols")
async def binance_symbols():
    """All Binance USDT-M futures symbols, top performers first, top-5 pinned.
    Cached for 10 min to avoid hammering the exchange."""
    global _symbols_cache
    now = time.time()
    if now - _symbols_cache["at"] < 600 and _symbols_cache["symbols"]:
        return _symbols_cache
    try:
        import urllib.request as _ur
        with _ur.urlopen("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=20) as r:
            tk = json.load(r)
        pinned = ["XAUUSDT", "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
        rows = []
        for t in tk:
            sym = t.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            try:
                rows.append({
                    "symbol": sym,
                    "price": float(t.get("lastPrice", 0) or 0),
                    "change": float(t.get("priceChangePercent", 0) or 0),
                    "quote_vol": float(t.get("quoteVolume", 0) or 0),
                })
            except (ValueError, TypeError):
                continue
        rows.sort(key=lambda s: s["quote_vol"], reverse=True)
        pinned_rows = [next((s for s in rows if s["symbol"] == p), None) for p in pinned]
        pinned_rows = [s for s in pinned_rows if s is not None]
        rest = [s for s in rows if s["symbol"] not in pinned]
        _symbols_cache = {"at": now, "symbols": pinned_rows + rest}
    except Exception as e:
        logger.warning(f"symbols fetch failed: {e}")
    return _symbols_cache


_movers_cache = {"at": 0.0, "platform": "", "movers": []}


def _binance_10m_movers(limit: int = 24) -> list:
    """Top-volume USDT-M names by ~10-min % (1m klines). XAU always first."""
    import urllib.request as _ur
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with _ur.urlopen("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=20) as r:
        tk = json.load(r)
    rows = []
    for t in tk:
        sym = t.get("symbol") or ""
        if not str(sym).endswith("USDT"):
            continue
        try:
            rows.append((sym, float(t.get("quoteVolume") or 0),
                         float(t.get("lastPrice") or 0)))
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda x: -x[1])
    names = ["XAUUSDT"] + [s for s, _, _ in rows if s != "XAUUSDT"]
    names = names[:max(8, min(int(limit or 24), 30))]
    px = {s: p for s, _, p in rows}

    def _chg(sym):
        try:
            with _ur.urlopen(
                    f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}"
                    f"&interval=1m&limit=12", timeout=8) as r:
                k = json.load(r)
            if not k:
                return 0.0
            o = float(k[0][1] or 0)
            c = float(k[-1][4] or 0)
            return ((c - o) / o * 100.0) if o > 0 else 0.0
        except Exception:
            return 0.0

    out = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(_chg, s): s for s in names}
        got = {}
        for fut in as_completed(futs):
            got[futs[fut]] = fut.result()
    for s in names:
        out.append({"symbol": s, "chg_10m": round(got.get(s, 0.0), 3),
                    "price": px.get(s, 0.0), "trade_currency": "USDT"})
    return out


@app.get("/api/movers")
async def api_movers(request: Request, platform: str = "binance"):
    """10-min movers strip (Binance). Cached ~45s."""
    plat = (platform or "binance").strip().lower()
    if plat != "binance":
        plat = "binance"
    now = time.time()
    if (_movers_cache.get("platform") == plat
            and now - float(_movers_cache.get("at") or 0) < 45
            and _movers_cache.get("movers")):
        return {"platform": plat, "movers": _movers_cache["movers"]}
    movers = []
    try:
        movers = _binance_10m_movers(24)
    except Exception as e:
        logger.warning(f"binance movers failed: {e}")
        movers = []
    _movers_cache.update({"at": now, "platform": plat, "movers": movers})
    return {"platform": plat, "movers": movers}


_chart_cache: dict = {}
_CHART_TTL = 45.0
_CHART_TF = {"1s": "1s", "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
             "1h": "1h", "4h": "4h", "1d": "1d"}


def _ohlc_from_mids(hist) -> list:
    buckets = {}
    for ts, mid in hist or []:
        try:
            t = int(float(ts))
            px = float(mid)
        except (TypeError, ValueError):
            continue
        if t <= 0 or px <= 0:
            continue
        if t not in buckets:
            buckets[t] = [px, px, px, px]
        else:
            o, h, l, _c = buckets[t]
            buckets[t] = [o, max(h, px), min(l, px), px]
    return [{"time": t, "open": o, "high": h, "low": l, "close": c}
            for t, (o, h, l, c) in sorted(buckets.items())]


def _same_chart_sym(a: str, b: str) -> bool:
    a = (a or "").upper().strip()
    b = (b or "").upper().strip()
    if not a or not b:
        return False
    if a == b:
        return True
    def _base(s):
        s = s.replace("/", "")
        if "." in s:
            s = s.split(".")[0]
        if s.endswith("INR") and not s.endswith("USDT"):
            s = s[:-3] + "USDT"
        return s
    return _base(a) == _base(b)


def _mt5_uid(request: Request):
    uid = _req_user(request)
    if uid:
        return uid
    return "local"
    return None


def _mt5_ohlc(user_id: str, symbol: str, interval: str) -> tuple:
    """Candles + last from the EA link / running MT5 bot. Never Binance."""
    tf_key = {"1s": "bars_m1", "1m": "bars_m1", "5m": "bars_m5",
              "15m": "bars_m15", "1h": "bars_h1", "4h": "bars_h1"}.get(
                  interval, "bars_m15")
    tf_rates = {"1s": "M1", "1m": "M1", "5m": "M5", "15m": "M15",
                "1h": "H1", "4h": "H1"}.get(interval, "M15")
    candles, last = [], 0.0
    try:
        for sub in list(orchestrator._bots.values()):
            plat = (getattr(getattr(sub, "bridge", None), "platform", None)
                    or getattr(sub, "platform", "") or "")
            if str(plat).lower() != "mt5":
                continue
            bsym = (getattr(sub, "symbol", None)
                    or getattr(getattr(sub, "entry", None), "symbol", "")
                    or "")
            if not _same_chart_sym(bsym, symbol):
                continue
            uid = getattr(sub, "user_id", None) or getattr(
                getattr(sub, "entry", None), "user_id", "")
            if user_id and uid and uid != user_id:
                continue
            br = getattr(sub, "bridge", None)
            if br and hasattr(br, "get_rates"):
                for b in (br.get_rates(tf_rates, 300) or []):
                    t = int(b.get("time") or 0)
                    if t > 1_000_000_000_000:
                        t //= 1000
                    if t <= 0:
                        continue
                    candles.append({
                        "time": t, "open": float(b["open"]),
                        "high": float(b["high"]), "low": float(b["low"]),
                        "close": float(b["close"]),
                    })
            if br and hasattr(br, "get_tick"):
                td = br.get_tick() or {}
                last = float(td.get("last") or 0)
            if not last and candles:
                last = candles[-1]["close"]
            if candles or last:
                return candles, last
    except Exception as e:
        logger.debug(f"chart mt5 bot: {e}")
    try:
        from services import mt5_link
        token = _mt5_token_for(user_id or "")
        if not token:
            return candles, last
        st = mt5_link.get_state(token, symbol) or {}
        if not st:
            for asy in mt5_link.assigned_symbols(token):
                if _same_chart_sym(asy, symbol):
                    st = mt5_link.get_state(token, asy) or {}
                    symbol = asy
                    break
        bars = st.get(tf_key) or []
        for b in bars:
            try:
                t = int(b.get("t") or 0)
                if t > 1_000_000_000_000:
                    t //= 1000
                if t <= 0:
                    continue
                candles.append({
                    "time": t, "open": float(b["o"]), "high": float(b["h"]),
                    "low": float(b["l"]), "close": float(b["c"]),
                })
            except (TypeError, ValueError, KeyError):
                continue
        bid = float(st.get("bid") or 0)
        ask = float(st.get("ask") or 0)
        if bid > 0 and ask > 0:
            last = (bid + ask) / 2.0
        elif candles:
            last = candles[-1]["close"]
    except Exception as e:
        logger.debug(f"chart mt5 link: {e}")
    return candles, last


def _ohlc_from_aggtrades(symbol: str, limit: int = 1000) -> list:
    import urllib.request as _ur
    url = (f"https://fapi.binance.com/fapi/v1/aggTrades"
           f"?symbol={symbol}&limit={limit}")
    with _ur.urlopen(url, timeout=12) as r:
        raw = json.load(r)
    buckets = {}
    for tr in raw or []:
        try:
            t = int(tr.get("T") or 0) // 1000
            px = float(tr.get("p") or 0)
        except (TypeError, ValueError):
            continue
        if t <= 0 or px <= 0:
            continue
        if t not in buckets:
            buckets[t] = [px, px, px, px]
        else:
            o, h, l, _c = buckets[t]
            buckets[t] = [o, max(h, px), min(l, px), px]
    return [{"time": t, "open": o, "high": h, "low": l, "close": c}
            for t, (o, h, l, c) in sorted(buckets.items())]



@app.get("/api/chart")
async def chart_snapshot(request: Request, symbol: str = "BTCUSDT",
                         platform: str = "binance", interval: str = "1m"):
    """Candles + grid overlay from in-process memory (and a short public
    kline fallback). Not a live stream — whatever the desk last knew."""
    user_id = _req_user(request)
    symbol = (symbol or "BTCUSDT").strip().upper()
    platform = (platform or "binance").strip().lower()
    interval = _CHART_TF.get((interval or "1m").strip().lower(), "1m")
    key = f"{user_id or '-'}:{platform}:{symbol}:{interval}"
    now = time.time()
    ttl = 8.0 if interval == "1s" else (20.0 if interval == "1m" else _CHART_TTL)
    hit = _chart_cache.get(key)
    candles, last = [], 0.0
    if hit and now - hit["at"] < ttl:
        candles = list(hit.get("candles") or [])
        last = float(hit.get("last") or 0)

    # MT5: candles + last from the EA (never Binance fapi — XAUUSD ≠ XAUUSDT).
    if platform == "mt5":
        mc, ml = _mt5_ohlc(user_id, symbol, interval)
        if mc:
            candles = mc
        if ml:
            last = ml
        if not candles:
            try:
                for sub in list(orchestrator._bots.values()):
                    br = getattr(sub, "bridge", None)
                    bsym = (getattr(sub, "symbol", None) or "")
                    if str(getattr(br, "platform", "") or "").lower() != "mt5":
                        continue
                    if not _same_chart_sym(bsym, symbol):
                        continue
                    candles = _ohlc_from_mids(list(getattr(br, "_mid_hist", []) or []))
                    if candles:
                        last = candles[-1]["close"]
                        break
            except Exception as e:
                logger.debug(f"chart mt5 mids: {e}")


    # Market candles ALWAYS come from LIVE public Binance (fapi.binance.com).
    # Demo/testnet REST is rate-limited and would starve bot tests; the graph
    # is display-only. Grid/PnL overlays below still come from the selected bot.
    if not candles and interval == "1s" and platform == "binance":
        try:
            candles = _ohlc_from_aggtrades(symbol)
            if candles:
                last = candles[-1]["close"]
        except Exception as e:
            logger.debug(f"chart 1s live agg: {e}")
        if not candles:
            try:
                for sub in list(orchestrator._bots.values()):
                    br = getattr(sub, "bridge", None)
                    bsym = (getattr(br, "binance_symbol", None) if br else "") or ""
                    if bsym.upper() != symbol:
                        continue
                    # bookTicker WS is already live fstream, not testnet
                    candles = _ohlc_from_mids(list(getattr(br, "_mid_hist", []) or []))
                    if candles:
                        last = candles[-1]["close"]
                        break
            except Exception as e:
                logger.debug(f"chart 1s mids: {e}")

    if not candles and platform == "binance":
        try:
            import urllib.request as _ur
            lim = 300 if interval in ("1s", "1m") else 120
            url = (f"https://fapi.binance.com/fapi/v1/klines"
                   f"?symbol={symbol}&interval={interval}&limit={lim}")
            with _ur.urlopen(url, timeout=12) as r:
                raw = json.load(r)
            for k in raw:
                candles.append({
                    "time": int(k[0]) // 1000,
                    "open": float(k[1]), "high": float(k[2]),
                    "low": float(k[3]), "close": float(k[4]),
                })
        except Exception as e:
            logger.debug(f"chart live klines: {e}")
        if candles and not last:
            try:
                import urllib.request as _ur
                with _ur.urlopen(
                        f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}",
                        timeout=8) as r:
                    last = float(json.load(r).get("price") or 0)
            except Exception:
                last = candles[-1]["close"]

    if candles and platform != "mt5":
        try:
            import urllib.request as _ur
            with _ur.urlopen(
                    f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}",
                    timeout=6) as r:
                live = float(json.load(r).get("price") or 0)
                if live > 0:
                    last = live
        except Exception:
            pass
    if candles and not last:
        last = candles[-1]["close"]

    # Overlays always fresh (do not cache empty grid from a previous miss).
    levels, pnl, pos, bot_name, marks = [], 0.0, 0.0, "", []
    try:
        for sub in list(orchestrator._bots.values()):
            bsym = (getattr(sub, "symbol", None)
                    or getattr(getattr(sub, "entry", None), "symbol", "")
                    or "").upper()
            if not _same_chart_sym(bsym, symbol):
                continue
            plat = (getattr(getattr(sub, "bridge", None), "platform", None)
                    or getattr(sub, "platform", "") or "")
            if platform == "mt5" and str(plat).lower() != "mt5":
                continue
            if platform == "binance" and str(plat).lower() == "mt5":
                continue
            uid = getattr(sub, "user_id", None) or getattr(
                getattr(sub, "entry", None), "user_id", "")
            if user_id and uid and uid != user_id:
                continue
            bot_name = getattr(sub, "name", None) or getattr(
                getattr(sub, "entry", None), "name", "") or ""
            sd = {}
            try:
                sd = sub.status_dict() or {}
            except Exception:
                sd = {}
            pnl = float(sd.get("total_pnl") or 0)
            pos = float(sd.get("net_position") or 0)
            marks.extend(sd.get("chart_marks") or [])
            # Binance: prefer exchange truth for grid overlay so chart correlates with order book
            # (bot's levels_detail can lag during recenter / ghost cancel window)
            _use_exchange = False
            if platform == "binance":
                try:
                    br = getattr(sub, "bridge", None)
                    if br and getattr(br, "_client", None):
                        _use_exchange = True
                        # force fresh sync so chart sees same as /api/accounts
                        try: br.sync_with_exchange(force=True)
                        except: pass
                        # build levels from exchange open orders + positions
                        for o in (br.list_open_orders() or []):
                            px = float(o.get("price") or o.get("stopPrice") or 0)
                            if px <= 0:
                                # conditional with trigger
                                px = float(o.get("stopPrice") or o.get("triggerPrice") or 0)
                            if px <= 0: continue
                            qty = float(o.get("origQty") or o.get("quantity") or o.get("qty") or 0)
                            otype = str(o.get("type") or o.get("orderType") or "").upper()
                            side = str(o.get("side") or "").upper()
                            kind = "grid"
                            if otype in ("STOP","STOP_MARKET","STOP_LOSS","STOP_LOSS_MARKET"): kind="sl"
                            elif otype in ("TAKE_PROFIT","TAKE_PROFIT_MARKET"): kind="tp"
                            if kind in ("sl","tp"): continue  # don't plot stops as grid
                            levels.append({"side": side, "price": px, "qty": qty, "pnl": 0, "filled": False, "kind": kind, "dist_pct": round(((px-last)/last*100) if last else 0,3), "sl":0,"tp":0})
                        for p in (br.get_exchange_positions() or []):
                            side = str(p.get("side") or "").upper()
                            qty = float(p.get("qty") or 0)
                            entry = float(p.get("entry_price") or 0)
                            pnlp = float(p.get("pnl") or 0)
                            if qty<=0 or entry<=0: continue
                            levels.append({"side": side, "price": entry, "qty": qty, "pnl": round(pnlp,4), "filled": True, "kind": "pos", "dist_pct": 0, "sl":0,"tp":0})
                        # also include marks already extended above
                        # skip the generic levels_detail loop below when using exchange
                        if levels:
                            break
                        _use_exchange = False
                except Exception as e:
                    logger.debug(f"chart exchange fallback {symbol}: {e}")
                    _use_exchange = False
            if _use_exchange:
                break
            for lv in (sd.get("levels_detail") or []):
                px = float(lv.get("price_open") or lv.get("price") or 0)
                if px <= 0:
                    continue
                kind = (lv.get("kind") or "grid").lower()
                side = (lv.get("type") or lv.get("leg") or "").upper()
                if side == "LONG":
                    side = "BUY"
                elif side == "SHORT":
                    side = "SELL"
                filled = bool(lv.get("filled") or kind == "pos")
                lpnl = float(lv.get("pnl") or 0)
                qty = float(lv.get("volume") or lv.get("qty") or 0)
                if filled and abs(lpnl) < 1e-12 and last > 0 and qty:
                    lpnl = (last - px) * qty if side == "BUY" else (px - last) * qty
                if kind in ("open", "close"):
                    continue
                levels.append({
                    "side": side, "price": px, "qty": qty,
                    "pnl": round(lpnl, 4),
                    "filled": filled,
                    "kind": kind or ("pos" if filled else "grid"),
                    "dist_pct": float(lv.get("dist_pct") or 0),
                    "sl": float(lv.get("sl") or 0),
                    "tp": float(lv.get("tp") or 0),
                })
                sl = float(lv.get("sl") or 0)
                tp = float(lv.get("tp") or 0)
                if sl > 0:
                    levels.append({"side": side, "price": sl, "qty": qty,
                                   "pnl": 0, "filled": False, "kind": "sl",
                                   "dist_pct": 0})
                if tp > 0:
                    levels.append({"side": side, "price": tp, "qty": qty,
                                   "pnl": 0, "filled": False, "kind": "tp",
                                   "dist_pct": 0})
            break
    except Exception as e:
        logger.debug(f"chart overlay: {e}")

    if not levels and user_id:
        try:
            import session_book
            row = (session_book.fetch_symbol(user_id, platform, symbol, "live")
                   or session_book.fetch_symbol(user_id, platform, symbol, "demo"))
            if row:
                bot_name = bot_name or (row.get("bot_name") or "")
                pnl = float(row.get("realized_pnl") or 0) + float(row.get("unrealized_pnl") or 0)
                pos = float(row.get("net_qty") or 0)
                for lv in (row.get("levels") or []):
                    if isinstance(lv, dict) and float(lv.get("price") or 0) > 0:
                        levels.append(lv)
        except Exception as e:
            logger.debug(f"chart session_book: {e}")

    if not marks and user_id:
        try:
            from chart_marks import list_marks
            marks = list_marks(user_id, platform, symbol)
        except Exception:
            marks = []
    data = {
        "symbol": symbol, "interval": interval, "platform": platform,
        "last": last, "candles": candles, "levels": levels,
        "marks": marks,
        "pnl": round(pnl, 4), "position": pos, "bot": bot_name,
        "source": "memory" if candles else "empty",
    }
    # Cache candles only; overlays stay live on the next poll.
    _chart_cache[key] = {"at": now, "candles": candles, "last": last}
    if len(_chart_cache) > 24:
        oldest = min(_chart_cache, key=lambda k: _chart_cache[k].get("at", 0))
        _chart_cache.pop(oldest, None)
    return data


@app.get("/api/chart/tick")
async def chart_live_tick(request: Request, symbol: str = "BTCUSDT",
                         platform: str = "binance"):
    """LIVE last + ticks/sec. Prefer in-process bookTicker (even for DEMO
    bots — that stream is public fstream). REST last is fallback only."""
    symbol = (symbol or "BTCUSDT").strip().upper()
    platform = (platform or "binance").strip().lower()
    last = 0.0
    tps = 0
    ticks = []
    now = time.time()
    from binance_guard import ticks_last_sec as _ticks
    try:
        for sub in list(orchestrator._bots.values()):
            br = getattr(sub, "bridge", None)
            bsym = ((getattr(br, "binance_symbol", None) if br else None)
                    or getattr(sub, "symbol", None) or "")
            if not _same_chart_sym(str(bsym), symbol):
                continue
            plat = str(getattr(br, "platform", "") or getattr(sub, "platform", "") or "").lower()
            if platform == "mt5" and plat != "mt5":
                continue
            if platform == "binance" and plat == "mt5":
                continue
            hist = list(getattr(br, "_mid_hist", []) or []) if br else []
            if hist:
                try:
                    last = float(hist[-1][1] or 0)
                except (TypeError, ValueError, IndexError):
                    last = 0.0
                ticks = _ticks(hist, now)
                tps = len(ticks)
                if last > 0:
                    break
            if br and hasattr(br, "get_tick"):
                try:
                    td = br.get_tick() or {}
                    last = float(td.get("last") or last or 0)
                except Exception:
                    pass
            if last > 0:
                break
    except Exception as e:
        logger.debug(f"chart tick ws: {e}")
    if last <= 0 and platform == "mt5":
        try:
            _, last = _mt5_ohlc(_mt5_uid(request), symbol, "1m")
        except Exception:
            last = 0.0
    if last <= 0 and platform != "mt5":
        try:
            import urllib.request as _ur
            with _ur.urlopen(
                    f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}",
                    timeout=6) as r:
                last = float(json.load(r).get("price") or 0)
        except Exception as e:
            logger.debug(f"chart tick: {e}")
    if tps <= 0 and last > 0 and platform != "mt5":
        try:
            import urllib.request as _ur
            with _ur.urlopen(
                    f"https://fapi.binance.com/fapi/v1/aggTrades?symbol={symbol}&limit=80",
                    timeout=6) as r:
                raw = json.load(r) or []
            cut = (now * 1000) - 1000
            ticks = []
            for tr in reversed(raw):
                if float(tr.get("T") or 0) < cut:
                    continue
                try:
                    px = float(tr.get("p") or 0)
                except (TypeError, ValueError):
                    continue
                if px > 0:
                    ticks.append(px)
                if len(ticks) >= 40:
                    break
            tps = len(ticks)
        except Exception:
            pass
    return {"symbol": symbol, "last": last, "tps": int(tps),
            "ticks": ticks, "ts": int(now)}


@app.get("/api/accounts")
async def all_accounts(request: Request):
    """Return ALL discovered exchange accounts for the CURRENT user (multi-tenant).

    With a Supabase JWT, only the caller's own broker accounts are returned.
    `?fresh=1` forces an immediate refresh. Falls back to the legacy .env scanner
    for the single-operator admin-token path.
    """
    force = request.query_params.get("fresh") == "1"
    platform = request.query_params.get("platform", "")
    user_id = _req_user(request)
    from account_scanner import fetch_accounts

    if user_id:
        return {"accounts": fetch_accounts(user_id)}

    accts = account_scanner.get_all()
    stale = not accts or not any(a.get("last_updated", 0) > time.time() - 70 for a in accts)
    if force:
        if platform:
            account_scanner.refresh_platform(platform)
        else:
            account_scanner.refresh_all()
        accts = account_scanner.get_all()
    elif stale:
        account_scanner.refresh_all()
        accts = account_scanner.get_all()
    return {"accounts": accts}


@app.post("/api/binance/close-all")
async def binance_close_all(body: dict = None, request: Request = None):
    """Close all positions + cancel all open/conditional orders on one Binance env."""
    body = body or {}
    env = body.get("env", "live")
    if env not in ("live", "demo"):
        raise HTTPException(400, "env must be 'live' or 'demo'")
    from account_scanner import close_all_binance, invalidate_positions
    result = await asyncio.to_thread(close_all_binance, env, _req_user(request) if request else None)
    invalidate_positions("binance")
    account_scanner.refresh_platform("binance")
    return {"ok": True, "env": env, **result}


CHAT_SYSTEM = """You are GB, the Chief Trading Assistant — a calm, knowledgeable trading partner. You speak directly to the admin in natural, conversational language — like a human trading desk veteran, not a robot.

YOUR PERSONALITY:
- Speak naturally: use contractions ("it's" not "it is"), vary sentence length, be warm but professional
- Never say "the user" or "the admin" — you're talking TO the person, use "you"
- When giving numbers, round to human-friendly amounts: "$1,234" not "$1,234.00"
- If the answer is complex, break it into 2-3 short paragraphs
- Use occasional trading-floor language: "we're flat", "sitting in profit", "riding the trend"

CURRENT SYSTEM FACTS:
- Platform: multi-platform grid trading system (Binance futures/spot + MT5)
- Binance: Demo ($2,000 testnet) + Live (funded account)
- Bot strategy: two-sided neutral limit grids — BUY below price, SELL above
- AI: DeepSeek via OpenRouter for bot decisions + chat
- Orchestrator: manages bot lifecycle, position reconciliation, risk management
- Dashboard: web UI with live artifact streaming
- External: Nous Hermes Agent handles cron, Discord gateway, and scheduled tasks

When asked about the other platform's data, say "I'll switch you to the {platform} tab for that" rather than guessing.
Be concise but not terse. A 2-3 sentence answer is usually enough. Never hallucinate data."""


# ═══════════════════════════════════════════════════════════════════════════
# Chat
# ═══════════════════════════════════════════════════════════════════════════

def _handle_chat_command(q: str, user_id: str = None) -> Optional[str]:
    """Process built-in chat commands. Returns answer string if handled, None if fallthrough to LLM."""
    q_lower = q.lower().strip()

    # ── stop all / close all bots ──
    stop_keywords = ("stop all", "close all", "shutdown all", "kill all", "stop bots", "close bots",
                     "stop everything", "kill bots")
    if any(cmd in q_lower for cmd in stop_keywords):
        bots = orchestrator.list_bots(user_id=user_id)
        # 1) stop orchestrator-owned bots
        for b in bots:
            orchestrator.stop_bot(b.get("bot_id", ""), user_id=user_id)
        # 2) stop GuruAI bots (both groups)
        for g in _guru_all(user_id):
            g.stop_all()
            g.prune()
        total = 0
        for b in bots:
            total += 1
        # count after prune
        remaining = len(orchestrator.list_bots(user_id=user_id))
        return f"✅ Stopped all bots — {total} terminated, {remaining} remain in panel (will clear on next refresh). "

    # ── spawn a bot ──
    spawn_keywords = ("spawn", "create bot", "start bot", "launch", "new bot")
    if any(cmd in q_lower for cmd in spawn_keywords):
        for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XAUUSDT", "INJUSDT", "DEXEUSDT", "BNBUSDT", "DOGEUSDT"]:
            if sym.lower() in q_lower:
                try:
                    _check_binance_fleet(user_id, extra=1)
                except HTTPException as e:
                    return f"❌ {e.detail}"
                from guru_ai import register_bank_manager
                acct = _fetch_account_raw(user_id=user_id)
                g = _get_guru(user_id=user_id, group="manual")
                r = g.spawn_symbol(sym, "CMD", float(acct.get("free_margin", 0) or 0))
                if r.get("ok"):
                    register_bank_manager(g)
                    return f"✅ Spawned GuruAI grid {r['bot_id'][:12]} on {sym} (same engine as 🧠, your symbol)."
                return f"❌ Failed to spawn {sym}: {r.get('error')}"
        return "I need a symbol to spawn. Try: 'spawn BTCUSDT'."

    # ── flatten / close exchange positions ──
    flatten_keywords = ("close position", "close my position", "close manual", "flatten",
                        "close all trades", "close open position")
    if any(cmd in q_lower for cmd in flatten_keywords):
        from account_scanner import close_all_binance
        closed = close_all_binance("live", user_id=user_id)["positions"]
        if closed > 0:
            return f"✅ Closed {closed} position(s) on LIVE Binance. Account is flat."
        return "No open positions found. Account is already flat."

    # ── guru start / stop ──
    if "guru" in q_lower and ("start" in q_lower or "launch" in q_lower):
        try:
            _check_binance_fleet(user_id, extra=1)
        except HTTPException as e:
            return f"❌ {e.detail}"
        acct = _fetch_account_raw(user_id=user_id)
        free_margin = float(acct.get("free_margin", 0))
        started, exclude, errs = [], set(), []
        for grp in _GURU_GROUPS:
            g = _get_guru(user_id=user_id, group=grp)
            g.prune()
            r = g.start(free_margin=free_margin, exclude=tuple(exclude))
            if r.get("ok"):
                exclude.update(s["symbol"] for s in r["started"])
                started.extend(r["started"])
            else:
                errs.append(f"{grp}: {r.get('error')}")
        if started:
            syms = [s["symbol"] for s in started]
            return f"✅ GuruAI launched {len(syms)} grids: {', '.join(syms)}"
        return f"❌ GuruAI start failed: {'; '.join(errs)}"

    if "guru" in q_lower and "stop" in q_lower:
        g = _get_guru(user_id=user_id)
        stopped = g.stop_all()
        return f"✅ Stopped {stopped} GuruAI grid(s). All positions flattened."

    return None  # fall through to LLM


@app.post("/api/chat")
async def chat(request: ChatRequest, req: Request):
    q = request.question.strip()
    if not q:
        return JSONResponse({"answer": "Ask me anything."})
    user_id = _req_user(req)

    # Try command handler first
    cmd_result = _handle_chat_command(q, user_id=user_id)
    if cmd_result is not None:
        return JSONResponse({"answer": cmd_result})

    # Build live context — platform-aware
    platform = getattr(request, 'platform', 'binance')
    bots = orchestrator.list_bots(user_id=user_id)
    from account_scanner import fetch_accounts
    accts = fetch_accounts(user_id)

    ctx = f"LIVE STATE ({platform.upper()}):\n"
    for a in accts:
        ctx += f"- {a['label']}: ${a['balance']:.2f} balance, ${a['free_margin']:.2f} free\n"
    if bots:
        ctx += f"- Active bots: {len(bots)}\n"
        for b in bots:
            ctx += f"  • {b.get('name','?')} ({b['symbol']} {b['market_type']}): {b['status']}, levels={b.get('levels',0)}, PnL=${b.get('basket_pnl',0):+.2f}\n"
    else:
        ctx += "- Active bots: 0 (none running)\n"
    if platform in ("binance", "", None):
        try:
            from mcp_binance import snapshot_for_chat
            ctx += "\n" + snapshot_for_chat(user_id, "live") + "\n"
        except Exception as e:
            ctx += f"\n(exchange book unavailable: {str(e)[:80]})\n"

    try:
        import httpx
        r = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json={
                "model": "deepseek/deepseek-chat",
                "messages": [
                    {"role": "system", "content": CHAT_SYSTEM},
                    {"role": "user", "content": f"{ctx}\n\nADMIN: {q}"},
                ],
                "temperature": 0.7,
                "max_tokens": 800,
            },
            headers={
                "Authorization": f"Bearer {os.getenv('DEEPSEEK_API_KEY', '')}",
                "HTTP-Referer": "http://localhost:9100",
                "X-Title": "gb-api",
            },
            timeout=20,
        )
        r.raise_for_status()
        answer = r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        answer = f"Unavailable ({str(e)[:60]})"

    save_chat_message(request.source, request.author, q, answer, platform=getattr(request, 'platform', 'binance'))
    return JSONResponse({"answer": answer})


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest, req: Request):
    """SSE streaming endpoint for real-time token-by-token responses."""
    q = request.question.strip()
    if not q:
        async def _empty(): yield "data: Ask me anything.\n\n"
        return StreamingResponse(_empty(), media_type="text/event-stream")
    user_id = _req_user(req)

    # Try built-in command handler first — must match before falling through to LLM
    cmd_result = _handle_chat_command(q, user_id=user_id)
    if cmd_result is not None:
        async def _cmd(): yield f"data: {cmd_result}\n\ndata: [DONE]\n\n"
        return StreamingResponse(_cmd(), media_type="text/event-stream")

    # Build live context — platform-aware
    platform = getattr(request, 'platform', 'binance')
    bots = orchestrator.list_bots(user_id=user_id)
    from account_scanner import fetch_accounts
    accts = fetch_accounts(user_id)

    ctx = f"LIVE STATE ({platform.upper()}):\n"
    for a in accts:
        ctx += f"- {a['label']}: ${a['balance']:.2f} balance, ${a['free_margin']:.2f} free\n"
    if bots:
        ctx += f"- Active bots: {len(bots)}\n"
        for b in bots:
            ctx += f"  • {b.get('name','?')} ({b['symbol']} {b['market_type']}): {b['status']}, lv={b.get('levels',0)}\n"
    else:
        ctx += "- Active bots: 0\n"
    if platform in ("binance", "", None):
        try:
            from mcp_binance import snapshot_for_chat
            ctx += "\n" + snapshot_for_chat(user_id, "live") + "\n"
        except Exception as e:
            ctx += f"\n(exchange book unavailable: {str(e)[:80]})\n"

    async def _stream():
        full_answer = ""
        try:
            import httpx
            body = {
                "model": "deepseek/deepseek-chat",
                "messages": [
                    {"role": "system", "content": CHAT_SYSTEM},
                    {"role": "user", "content": f"{ctx}\n\nADMIN: {q}"},
                ],
                "temperature": 0.7,
                "max_tokens": 800,
                "stream": True,
            }
            async with httpx.AsyncClient(timeout=30) as client:
                async with client.stream(
                    "POST",
                    "https://openrouter.ai/api/v1/chat/completions",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {os.getenv('DEEPSEEK_API_KEY', '')}",
                        "HTTP-Referer": "http://localhost:9100",
                        "X-Title": "gb-api",
                    },
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:]
                            if data == "[DONE]":
                                yield "data: [DONE]\n\n"
                                break
                            try:
                                chunk = json.loads(data)
                                token = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                if token:
                                    full_answer += token
                                    yield f"data: {token}\n\n"
                            except json.JSONDecodeError:
                                pass
        except Exception as e:
            yield f"data: ⚠️ Stream error: {str(e)[:80]}\n\n"
            yield "data: [DONE]\n\n"

        # Save to chat DB after stream completes
        if full_answer:
            save_chat_message(request.source, request.author, q, full_answer, platform=getattr(request, 'platform', 'binance'))

    return StreamingResponse(_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/chat/history")
async def chat_history(limit: int = 40, platform: str = "binance"):
    return JSONResponse({"messages": get_chat_history(limit, platform)})


@app.get("/api/chat/messages")
async def chat_messages(limit: int = 40, platform: str = "binance"):
    return JSONResponse(get_chat_history(limit, platform))


@app.delete("/api/chat/messages")
async def clear_chat(request: Request):
    from storage import _get_chat_db
    import json
    try:
        body = await request.json()
        platform = body.get("platform", "")
    except Exception:
        raise HTTPException(400, "A platform is required")
    if platform not in {"binance"}:
        raise HTTPException(400, "Invalid platform")
    c = _get_chat_db()
    try:
        c.execute("DELETE FROM chat_messages WHERE platform=?", (platform,))
        c.commit()
        return JSONResponse({"status": "cleared", "platform": platform})
    finally:
        c.close()


# ── Assistant feed (local stub; the cloud agent bus is not in this build) ──
# The dashboard panel polls these; empty feed keeps the UI functional.
# Connect an OpenRouter key in Settings for AI screening (openrouter_client).

@app.post("/api/hermes/send")
async def hermes_send(body: dict = None, request: Request = None):
    """Kept for UI compatibility. Returns a pointer to local AI screening."""
    return JSONResponse({"ok": False,
                         "detail": "Agent chat is not in the open build — "
                                   "AI screening runs via your OpenRouter key "
                                   "(Settings).",
                         "messages": []})


@app.get("/api/hermes/messages")
async def hermes_messages(limit: int = 60, platform: str = "binance",
                          request: Request = None):
    """Empty feed stub for UI compatibility."""
    return JSONResponse({"messages": []})


@app.post("/api/hermes/mark-read")
async def hermes_mark_read(request: Request = None):
    """No-op stub for UI compatibility."""
    return JSONResponse({"ok": True})


@app.post("/api/binance/keys")
async def binance_set_keys(body: dict = None, request: Request = None):
    """Persist Binance API keys (live + demo) to the local SQLite store."""
    body = body or {}
    import local_store
    saved = []
    for env in ("live", "demo"):
        k = (body.get(env, {}) or {}).get("api_key", "").strip() if isinstance(body.get(env), dict) else ""
        sec = (body.get(env, {}) or {}).get("api_secret", "").strip() if isinstance(body.get(env), dict) else ""
        if not k and not sec:
            continue
        if k and sec:
            local_store.save_credential("binance", env, k, sec)
            saved.append(env)
    from account_scanner import invalidate_positions
    invalidate_positions("binance")
    return {"ok": True, "saved": saved}


# ═══════════════════════════════════════════════════════════════════════════
# MT5 platform — EA link + dashboard endpoints (2026-08-19)
# ═══════════════════════════════════════════════════════════════════════════

def _mt5_token_for(user_id: str) -> str:
    import local_store
    for env in ("live", "demo"):
        c = local_store.get_credential("mt5", env)
        if c.get("api_secret"):
            return c["api_secret"]
    return os.getenv("MT5_EA_TOKEN", "")


def _mt5_user_for_token(token: str) -> Optional[str]:
    """Single-user reverse lookup: the stored EA token belongs to 'local'."""
    if not token:
        return None
    try:
        if token == _mt5_token_for("local"):
            return "local"
    except Exception:
        pass
    return None


async def _mt5_accept_token(token: str) -> bool:
    """Known (persisted) tokens skip Supabase so /state never blocks the EA."""
    from services import mt5_link
    if not token:
        return False
    if mt5_link.link_exists(token):
        return True
    uid = await asyncio.to_thread(_mt5_user_for_token, token)
    return bool(uid)


_mt5_beat_at: dict = {}


def _mt5_log_beat(symbol: str, n_pos: int = 0, n_ord: int = 0):
    now = time.time()
    if now - _mt5_beat_at.get(symbol, 0) < 30:
        return
    _mt5_beat_at[symbol] = now
    logger.info("MT5 EA beat chart=%s pos=%s orders=%s", symbol, n_pos, n_ord)


@app.post("/api/mt5/register")
async def mt5_register(request: Request):
    """EA registration (init + 5-min heartbeat): account details + symbol
    list. Auth = X-GB-Auth header (the user's MT5 token)."""
    from services import mt5_link
    token = request.headers.get("x-gb-auth", "").strip()
    if not await _mt5_accept_token(token):
        raise HTTPException(401, "unknown MT5 token")
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "client disconnected"}
    account = body.get("account") or {}
    symbols = body.get("symbols") or []
    mt5_link.register(token.strip(), account, symbols)
    logger.info("MT5 EA registered login=%s server=%s symbols=%s chart=%s",
                account.get("login"), account.get("server"),
                len(symbols), body.get("chart_symbol", ""))
    return {"ok": True, "chart_symbol": body.get("chart_symbol", ""),
            "server_time": time.time()}


def _mt5_loads_state(raw: bytes) -> dict:
    """Parse EA /state JSON. v3.10 omitted a comma after ticket in
    orders_data, which made FastAPI drop the whole POST (offline flicker
    + empty grid whenever a limit was resting). Repair that in place."""
    import re
    text = (raw or b"").decode("utf-8", "replace")
    if not text.strip():
        raise ValueError("empty body")
    text = re.sub(r'("ticket"\s*:\s*-?\d+)\s*"type"', r'\1,"type"', text)
    return json.loads(text)


@app.post("/api/mt5/state")
async def mt5_state(request: Request):
    """EA state POST → returns the queued commands for this chart symbol."""
    from services import mt5_link
    token = request.headers.get("x-gb-auth", "").strip()
    if not await _mt5_accept_token(token):
        raise HTTPException(401, "unknown MT5 token")
    try:
        raw = await request.body()
    except Exception:
        # EA gave up mid-upload (5s WebRequest timeout vs big BarsToSend
        # payload). Count the heartbeat, skip this tick — no 500s.
        mt5_link.touch_alive(token)
        return JSONResponse({"commands": []}, status_code=204)
    try:
        body = _mt5_loads_state(raw)
    except Exception as e:
        mt5_link.touch_alive(token)
        logger.warning("MT5 /state JSON parse failed (%s) — counted as heartbeat; "
                       "compile EA v3.14. snippet=%r",
                       e, (raw or b"")[:180])
        return {"commands": []}
    symbol = (body.get("symbol") or "").strip()
    if not symbol:
        mt5_link.touch_alive(token)
        raise HTTPException(400, "missing symbol")
    if not mt5_link.link_exists(token):
        mt5_link.ensure(token, symbol=symbol)
    cmds = mt5_link.touch_symbol(token, symbol, body)
    if cmds is None:
        cmds = []
    ex = body.get("exec")
    if isinstance(ex, dict) and ex.get("command_id"):
        mt5_link.push_exec(token, symbol, ex)
    # Bar store: accumulate M15 history across POSTs (backtests need 700-3000
    # bars; EA BarsToSend alone can't cover a month). Dedupe+cap at read time.
    try:
        _bars = body.get("bars_m15") or []
        if _bars:
            global _BAR_LAST_T
            try:
                _BAR_LAST_T
            except NameError:
                _BAR_LAST_T = {}
            _lt = _BAR_LAST_T.get(symbol, 0)
            _new = [b for b in _bars if (b.get("t") or 0) > _lt]
            if _new:
                _BAR_LAST_T[symbol] = max(b.get("t", 0) for b in _new)
                _d = pathlib.Path(__file__).parent / "data"
                _d.mkdir(parents=True, exist_ok=True)
                _fp = _d / f"mt5_bars_{symbol.upper()}.jsonl"
                with open(_fp, "a") as _f:
                    for _b in _new[-10:]:
                        _f.write(json.dumps({"t": _b.get("t"), "o": _b.get("o"),
                                             "h": _b.get("h"), "l": _b.get("l"),
                                             "c": _b.get("c"), "v": _b.get("v", 0)}) + "\n")
    except Exception as e:
        logger.debug(f"bar store append failed: {e}")
    _mt5_log_beat(symbol,
                  len(body.get("positions_data") or []),
                  len(body.get("orders_data") or []))
    # EA v3.11 reads a single top-level "action" (not commands[]). Promote
    # the first queued command so CLOSE_ALL / CANCEL_ALL actually fire.
    payload = {"commands": cmds, "server_time": time.time()}
    if cmds:
        c0 = {k: v for k, v in cmds[0].items() if k != "queued_at"}
        payload.update(c0)
    return payload


@app.get("/api/mt5/status")
async def mt5_status(request: Request):
    """Dashboard: MT5 connection + account card (auto-filled on register).
    Also returns the stored token so Settings auto-populates it.
    Admin-token path resolves the configured monitor account."""
    user = getattr(request, "state", None) and request.state.user
    uid = (user["id"] if user else None) or "local"
    if not uid:
        raise HTTPException(401, "Sign in for MT5 status")
    from services import mt5_link
    token = _mt5_token_for(uid)
    if not token:
        return {"connected": False, "has_token": False, "token": ""}
    acct = mt5_link.get_account(token)
    connected = mt5_link.is_connected(token)
    age = mt5_link.last_seen_age(token)
    book = mt5_link.account_book(token)
    if book.get("free_margin") and acct:
        acct = dict(acct)
        acct["free_margin"] = book["free_margin"]
    return {
        "has_token": True,
        "token": token,
        "connected": connected,
        "last_seen_s": None if age is None else round(age, 1),
        "ea_server_url": _public_api_url() + "/api/mt5",
        "account": acct if connected else (acct or {}),
        "symbols_count": len(mt5_link.get_symbols(token)),
        "assigned_symbols": mt5_link.assigned_symbols(token),
        "positions": book.get("positions") or [],
        "open_orders": book.get("open_orders") or [],
        "free_margin": book.get("free_margin") or 0,
        "ea_state": mt5_link.ea_state(token),
        "alert": mt5_link.get_alert(),
        "n_pos_raw": sum(len((mt5_link.get_state(token, s) or {}).get("positions_data") or [])
                         for s in (mt5_link.known_chart_symbols(token) or [])),
        "n_ord_raw": sum(len((mt5_link.get_state(token, s) or {}).get("orders_data") or [])
                         for s in (mt5_link.known_chart_symbols(token) or [])),
        "mt5_orders_total": (mt5_link.get_state(token, (mt5_link.known_chart_symbols(token) or [""])[0]) or {}).get("mt5_orders_total"),
        "mt5_positions_total": (mt5_link.get_state(token, (mt5_link.known_chart_symbols(token) or [""])[0]) or {}).get("mt5_positions_total"),
    }


def _public_api_url() -> str:
    """This host's public base URL (tunnel.url when present), for user-facing
    messages. Never hardcode IPs here — the GCP address haunted us twice."""
    try:
        p = pathlib.Path(__file__).parent / "logs" / "tunnel.url"
        u = p.read_text().strip() if p.exists() else ""
        if u:
            return u.rstrip("/")
    except Exception:
        pass
    return ""


@app.post("/api/mt5/test")
async def mt5_test_connection(request: Request):
    """Settings ▸ MT5 ▸ Test Connection: read the linked EA's account.
    ok=true only when the EA is registered AND posting fresh state."""
    user = getattr(request, "state", None) and request.state.user
    if not user:
        raise HTTPException(401, "Sign in for MT5 test")
    from services import mt5_link
    token = _mt5_token_for(user["id"])
    if not token:
        return {"ok": False, "error": "No MT5 token yet — click Generate first"}
    if not mt5_link.link_exists(token) or not mt5_link.is_connected(token):
        age = mt5_link.last_seen_age(token)
        _pub = _public_api_url()
        _ea_hint = (f"Set EA input ServerURL to {_pub}/api/mt5 "
                    f"and add that URL in Tools ▸ Options ▸ Expert Advisors ▸ Allow WebRequest. "
                    if _pub else
                    "Set EA input ServerURL to this host's public /api/mt5 URL "
                    "and add that URL in Tools ▸ Options ▸ Expert Advisors ▸ Allow WebRequest. ")
        return {"ok": False,
                "error": (
                    "No EA heartbeat on this host. "
                    + _ea_hint +
                    "Then re-attach / recompile if the default was the old IP. "
                    + (f"Last POST {int(age)}s ago." if age is not None else
                       "This server has never received a POST from the EA.")
                )}
    acct = mt5_link.get_account(token)
    syms = mt5_link.get_symbols(token)
    return {"ok": True, "account": acct, "symbols_count": len(syms)}


@app.get("/api/mt5/symbols")
async def mt5_symbols(request: Request):
    """Dashboard: the MT5 broker's tradeable symbol list (from registration)."""
    uid = _mt5_uid(request)
    if not uid:
        raise HTTPException(401, "Sign in for MT5 symbols")
    from services import mt5_link
    token = _mt5_token_for(uid)
    if not token:
        return {"symbols": [], "connected": False, "assigned": [], "charts": [],
                "has_token": False}
    assigned = mt5_link.assigned_symbols(token)
    charts = mt5_link.chart_symbols(token)
    symbols = mt5_link.get_symbols(token) or []
    if not symbols:
        names = list(dict.fromkeys(list(assigned) + list(charts)))
        symbols = [{"name": s} for s in names]
    return {"symbols": symbols, "assigned": assigned, "charts": charts,
            "connected": mt5_link.is_connected(token), "has_token": True}


@app.post("/api/mt5/token")
async def mt5_set_token(request: Request):
    """Generate (or rotate) the local MT5 EA token and store it (SQLite)."""
    import secrets
    import local_store
    token = "mt5_" + secrets.token_hex(16)
    local_store.save_credential("mt5", "live", "ea", token)
    return {"ok": True, "token": token}


@app.post("/api/mt5/close-all")
async def mt5_close_all(request: Request, body: dict = None):
    """Close all MT5 positions + cancel pending orders for the user's EA."""
    user = getattr(request, "state", None) and request.state.user
    uid = (user["id"] if user else None) or "local"
    if not uid:
        raise HTTPException(401, "Sign in")
    token = _mt5_token_for(uid)
    from bridge_mt5 import MT5Bridge
    from services import mt5_link

    def _queue():
        queued = 0
        book = mt5_link.account_book(token)
        # EA v3.11 runs ONE top-level action per POST. Send cancel first
        # while limits exist, then CLOSE_ALL.
        action = "CANCEL_ALL" if (book.get("open_orders")) else "CLOSE_ALL"
        syms = (body or {}).get("symbols", []) or []
        if not syms:
            syms = mt5_link.assigned_symbols(token) or mt5_link.known_chart_symbols(token)
        for sym in syms:
            br = MT5Bridge(token=token, symbol=sym, user_id=uid)
            if not br.connect(require_heartbeat=False):
                continue
            br._command(action, wait=False)
            queued += 1
        return queued, action, mt5_link.account_book(token)

    queued, action, book = await asyncio.to_thread(_queue)
    return {"ok": True, "queued": queued, "action": action,
            "positions_left": len(book.get("positions") or []),
            "orders_left": len(book.get("open_orders") or [])}


# ── MT5 GuruAI (DynamicGuruAI on the chart symbol, FTMO risk layer) ─────────

_mt5_guru_lock = threading.RLock()
_mt5_guru: dict = {}   # user_id -> MT5GuruController


def _mt5_guru_get(user_id: str):
    with _mt5_guru_lock:
        ctrl = _mt5_guru.get(user_id)
    if ctrl:
        return ctrl
    return getattr(orchestrator, "_mt5_gurus", {}).get(user_id)


def _mt5_guru_set(user_id: str, ctrl):
    with _mt5_guru_lock:
        _mt5_guru[user_id] = ctrl
    if not hasattr(orchestrator, "_mt5_gurus"):
        orchestrator._mt5_gurus = {}
    orchestrator._mt5_gurus[user_id] = ctrl


def _mt5_guru_pop(user_id: str):
    with _mt5_guru_lock:
        ctrl = _mt5_guru.pop(user_id, None)
    extra = getattr(orchestrator, "_mt5_gurus", None)
    if extra:
        ctrl = ctrl or extra.pop(user_id, None)
    return ctrl


@app.post("/api/mt5/guru/start")
async def mt5_guru_start(request: Request, body: dict = None):
    """🧠 Start DynamicGuruAI on the selected MT5 chart symbol (FTMO risk
    layer active). Env auto-follows the EA account type; LIVE requires
    GURU_MT5_ALLOW_LIVE=1."""
    user = getattr(request, "state", None) and request.state.user
    uid = (user["id"] if user else None) or "local"
    if not uid:
        raise HTTPException(401, "Sign in to start MT5 GuruAI")
    symbol = ((body or {}).get("symbol") or "").strip().upper() or "XAUUSD"
    with _mt5_guru_lock:
        existing = _mt5_guru_get(uid)
        if existing and existing.running:
            return {"ok": True, "status": "already-running",
                    "symbol": existing.symbol}
        import os as _os
        from bridge_mt5 import MT5Bridge
        from mt5_guru import MT5GuruController
        from services import mt5_link
        token = _mt5_token_for(uid)
        if not token:
            raise HTTPException(400, "No MT5 token — Settings ▸ MT5 ▸ Generate")
        env = (body or {}).get("env") or ""
        br = MT5Bridge(token=token, symbol=symbol, environment=env,
                       user_id=uid)
        if not br.connect():
            raise HTTPException(400, "MT5 EA not connected")
        env = br.environment          # auto-detected from ACCOUNT_TRADE_MODE
        if env == "live" and os.getenv("GURU_MT5_ALLOW_LIVE", "0") != "1":
            raise HTTPException(400,
                                "LIVE trading on MT5 is disabled "
                                "(GURU_MT5_ALLOW_LIVE=0). Run DEMO first.")
        if symbol not in mt5_link.assigned_symbols(token):
            raise HTTPException(
                400, f"No EA attached to {symbol} — attach the HybridGB EA "
                     f"to that chart and try again")
        ftmo_rules = True
        if body and "ftmo_rules" in body:
            ftmo_rules = bool(body.get("ftmo_rules"))
        try:
            ctrl = MT5GuruController(user_id=uid, token=token,
                                     symbol=symbol, env=env,
                                     ftmo_rules=ftmo_rules)
        except RuntimeError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(400, f"MT5 GuruAI init failed: {str(e)[:140]}")
        if not ctrl.start():
            raise HTTPException(400, "Overall max-loss halt is active — "
                                     "reset required in Settings")
        _mt5_guru_set(uid, ctrl)
        activity.feed("mt5", f"GuruAI started on {ctrl.symbol} ({env})",
                      symbol=ctrl.symbol)
        _invalidate_health()
        return {"ok": True, "symbol": ctrl.symbol, "env": env,
                "qty": ctrl.bot.qty, "levels": ctrl.bot.grid_levels,
                "ftmo_rules": ctrl.ftmo_rules}


@app.post("/api/mt5/guru/stop")
async def mt5_guru_stop(request: Request):
    user = getattr(request, "state", None) and request.state.user
    uid2 = (user["id"] if user else None) or "local"
    if not uid2:
        raise HTTPException(401, "Sign in")
    ctrl = _mt5_guru_pop(uid2)
    if not ctrl:
        return {"ok": True, "status": "not-running"}
    ctrl.stop()
    activity.feed("mt5", f"GuruAI stopped on {ctrl.symbol}", symbol=ctrl.symbol)
    return {"ok": True, "status": "stopped"}


@app.get("/api/mt5/guru/status")
async def mt5_guru_status(request: Request):
    user = getattr(request, "state", None) and request.state.user
    uid = (user["id"] if user else None) or "local"
    if not uid:
        raise HTTPException(401, "Sign in")
    ctrl = _mt5_guru_get(uid)
    if not ctrl:
        return {"running": False}
    b = ctrl.bot
    return {"running": True, "symbol": b.symbol, "env": ctrl.env,
            "bias": getattr(b, "bias", "neutral"),
            "bias_source": getattr(b, "_bias_source", "rules"),
            "status": b.status.value, "cycles": b.cycles,
            "pnl": round(b.realized_pnl, 4), "halted": ctrl.halted,
            "ftmo_rules": getattr(ctrl, "ftmo_rules", True)}


@app.post("/api/mt5/kill")
async def mt5_kill(request: Request, body: dict = None):
    """🛑 MT5 KILL: close all positions + cancel all orders + stop GuruAI +
    cancel any scheduled risk restart."""
    user = getattr(request, "state", None) and request.state.user
    uid = (user["id"] if user else None) or "local"
    if not uid:
        raise HTTPException(401, "Sign in")
    ctrl = _mt5_guru_pop(uid)
    if ctrl:
        ctrl.stop()
    token = _mt5_token_for(uid)
    from bridge_mt5 import MT5Bridge
    from services import mt5_link
    link = mt5_link.get_link(token) if token else None
    syms = list((link or {}).get("per_symbol", {}).keys()) if link else []
    closed = 0
    for sym in syms:
        br = MT5Bridge(token=token, symbol=sym, user_id=uid)
        if br.connect():
            br.cancel_all_orders()
            closed += br.close_all()
    activity.feed("mt5", f"KILL SWITCH — {closed} position(s) closed", )
    return {"ok": True, "closed": closed, "symbols": syms}



# ═══════════════════════════════════════════════════════════════════════════
# Dashboard — served directly from VM (static HTML + JS)
# ═══════════════════════════════════════════════════════════════════════════
import pathlib

_NO_STORE = {"Cache-Control": "no-store, max-age=0, must-revalidate"}

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    p = pathlib.Path(__file__).parent / "dashboard-v2" / "index.html"
    if not p.exists():
        return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)
    return HTMLResponse(p.read_text(), headers=_NO_STORE)

@app.get("/main.js")
async def dashboard_js():
    p = pathlib.Path(__file__).parent / "dashboard-v2" / "main.js"
    if not p.exists():
        return Response(status_code=404)
    return Response(content=p.read_text(), media_type="application/javascript",
                    headers=_NO_STORE)

@app.get("/vendor/lightweight-charts.js")
async def dashboard_tv():
    p = pathlib.Path(__file__).parent / "dashboard-v2" / "vendor" / "lightweight-charts.js"
    return Response(content=p.read_bytes(),
                    media_type="application/javascript") if p.exists() else Response(status_code=404)


# ═══════════════════════════════════════════════════════════════════════════
# Local settings (OpenRouter key/model, admin password) + static dashboard
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/settings/openrouter")
async def settings_openrouter_get():
    """Presence-only (never returns the key). Model is shown (not secret)."""
    import local_store
    saved = local_store.get_setting("openrouter") or {}
    return {"ok": True, "configured": bool(saved.get("api_key")),
            "model": saved.get("model", "") or os.getenv("OPENROUTER_MODEL", "")}


@app.post("/api/settings/openrouter")
async def settings_openrouter_set(body: dict = None):
    """Save the user's own OpenRouter key + model choice (local SQLite)."""
    body = body or {}
    import local_store
    key = str(body.get("api_key", "") or "").strip()
    model = str(body.get("model", "") or "").strip()
    if key:
        local_store.save_setting("openrouter", {"api_key": key,
                                                "model": model})
        return {"ok": True, "configured": True, "model": model}
    if model:
        prev = local_store.get_setting("openrouter") or {}
        prev["model"] = model
        local_store.save_setting("openrouter", prev)
        return {"ok": True, "configured": bool(prev.get("api_key")),
                "model": model}
    raise HTTPException(400, "api_key or model required")


@app.post("/api/settings/password")
async def settings_password_set(body: dict = None):
    """Set the local operator password (required for non-localhost access)."""
    body = body or {}
    pw = str(body.get("password", "") or "")
    if len(pw) < 8:
        raise HTTPException(400, "password must be 8+ characters")
    import local_store
    local_store.save_setting("admin_password", pw)
    return {"ok": True}


try:
    from fastapi.staticfiles import StaticFiles
    import pathlib as _pl
    _DASH = _pl.Path(__file__).parent / "dashboard-v2"
    if _DASH.exists():
        app.mount("/", StaticFiles(directory=str(_DASH), html=True),
                  name="dashboard")
except Exception as _e:
    logger.warning("static dashboard mount failed: %s", str(_e)[:100])


# ═══════════════════════════════════════════════════════════════════════════
# Run
# ═══════════════════════════════════════════════════════════════════════════

def run(host: str = None, port: int = None):
    if port is None:
        port = int(os.getenv("API_PORT", "9100"))
    if host is None:
        host = os.getenv("API_HOST", "127.0.0.1")  # localhost-only by default

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
                        datefmt="%H:%M:%S")

    # Silence noisy third-party loggers — httpx logs every Supabase/OpenRouter
    # request at INFO and python-binance's WS threads spam "Read loop has been
    # closed" at ERROR, which together balloon the logs to ~16MB/min.
    for _noisy in ("httpx", "httpcore", "binance", "websockets", "urllib3", "websocket"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)
    # The "Read loop has been closed" flood is ERROR-level, so it needs CRITICAL.
    logging.getLogger("binance.ws.threaded_stream").setLevel(logging.CRITICAL)

    # Restart: rebuild persisted bots. MT5 GuruAI is re-attached without
    # flattening — the EA still holds the ladder.
    try:
        orchestrator._load_registry()
        orchestrator.resume_from_registry(bridge_factory=_get_bridge)
        for uid, ctrl in list(getattr(orchestrator, "_mt5_gurus", {}).items()):
            _mt5_guru_set(uid, ctrl)
    except Exception as e:
        logger.error(f"Bot resume on boot failed: {e}")

    # Start Discord bot thread (if configured)
    _start_discord()

    # Activity feed ingester (plain-language chatter for the dashboard)
    activity.feed("system", "Server started — bots resumed")
    threading.Thread(target=_feed_ingester, daemon=True,
                     name="feed-ingester").start()
    threading.Thread(target=_rss_watch, daemon=True, name="rss-watch").start()
    if os.getenv("GB_MEM_WATCH") == "1":
        threading.Thread(target=_mem_watch, daemon=True, name="mem-watch").start()
    threading.Thread(target=_mt5_ea_watch, daemon=True, name="mt5-ea-watch").start()

    uvicorn.run(app, host=host, port=port, log_level="warning")


_mt5_watch_handled: dict = {}


def _mt5_ea_watch():
    """EA pull-model watchdog.

    The server cannot push into MT5. If the laptop EA stops POSTing:
      stale (~60s) — drop queued OPEN commands (no ladder dump on reconnect)
      lost  (~90s) — stop MT5 bots, queue cancel/close, toast the operator
                     to flatten leftovers in the terminal.
    """
    while True:
        time.sleep(5)
        try:
            _mt5_ea_watch_once()
        except Exception as e:
            logger.warning(f"mt5 ea-watch: {e}")


def _mt5_ea_watch_once():
    from services import mt5_link
    from bridge_mt5 import MT5Bridge
    for tok in mt5_link.all_tokens():
        st = mt5_link.ea_state(tok)
        if st == "unknown":
            continue
        if st in ("stale", "lost"):
            n = mt5_link.drop_place_commands(tok)
            if n:
                logger.info(f"mt5 watch: dropped {n} OPEN cmd(s) (EA {st})")
        if st != "lost":
            _mt5_watch_handled.pop(tok, None)
            continue
        if _mt5_watch_handled.get(tok) != "lost":
            stopped = 0
            for bid, sub in list(orchestrator._bots.items()):
                entry = orchestrator._registry.get(bid)
                if orchestrator._bot_platform(entry, sub) != "mt5":
                    continue
                try:
                    if orchestrator.stop_bot(bid, close_positions=False):
                        stopped += 1
                except Exception as e:
                    logger.warning(f"mt5 watch stop {bid}: {e}")
            msg = ("EA connection lost — bots stopped. "
                   "Close leftover positions/orders in MT5 yourself.")
            if stopped:
                msg = (f"EA connection lost — {stopped} MT5 bot(s) stopped. "
                       "Close leftover positions/orders in MT5 yourself.")
            mt5_link.set_alert(msg, "lost")
            try:
                activity.feed("mt5", msg)
            except Exception:
                pass
            logger.warning(msg)
            _mt5_watch_handled[tok] = "lost"
        book = mt5_link.account_book(tok)
        if not (book.get("open_orders") or book.get("positions")):
            continue
        n = int(_mt5_watch_handled.get(tok + "#n", 0) or 0)
        action = ("CANCEL_ALL", "CLOSE_ALL", "CLOSE_CHART")[n % 3]
        _mt5_watch_handled[tok + "#n"] = n + 1
        for sym in mt5_link.known_chart_symbols(tok):
            try:
                br = MT5Bridge(token=tok, symbol=sym)
                if br.connect(require_heartbeat=False):
                    br._command(action, wait=False)
            except Exception:
                pass


def _start_discord():
    """Start Discord bot in a background thread if credentials are configured."""
    token = os.getenv("DISCORD_BOT_TOKEN", "")
    ch = os.getenv("DISCORD_CHANNEL_ID", "")
    if not token or not ch:
        logger.info("Discord bot disabled — set DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID")
        return
    try:
        from discord_bot import start_discord_thread
        start_discord_thread()
        logger.info(f"Discord bot started in channel {ch}")
    except Exception as e:
        logger.warning(f"Discord bot failed to start: {e}")


if __name__ == "__main__":
    run()
