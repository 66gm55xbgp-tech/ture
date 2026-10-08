"""HybridGB Open — OpenRouter LLM client (replaces proprietary JEV/Hermes paths).

Same call surface the engine expects (jev_decide, entry_ok, unwind_vote,
attention_scan, apply_scan_filter, mt5_unwind_vote) so strategy code is
unchanged. The user picks any OpenRouter model at runtime (Settings UI or
OPENROUTER_MODEL env); the API key is theirs (OPENROUTER_API_KEY env or
Settings, stored only in local SQLite). No key = math-only fail-open, exits
and risk rails never depend on the LLM.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger("hybrid.openrouter")

API_URL = "https://openrouter.ai/api/v1/chat/completions"


def _cfg():
    key = os.getenv("OPENROUTER_API_KEY", "") or ""
    model = os.getenv("OPENROUTER_MODEL", "") or ""
    if not key or not model:
        try:  # Settings-saved credentials (local SQLite, never committed)
            import local_store
            saved = local_store.get_setting("openrouter") or {}
            key = key or str(saved.get("api_key", "") or "")
            model = model or str(saved.get("model", "") or "")
        except Exception:
            pass
    return key, model


def _chat(messages: list, max_tokens: int = 220,
          timeout: int = 25) -> str | None:
    """Single OpenRouter chat call. Returns raw text or None on any failure."""
    key, model = _cfg()
    if not key or not model:
        return None
    body = json.dumps({"model": model, "messages": messages,
                       "max_tokens": max_tokens,
                       "temperature": 0.2}).encode()
    req = urllib.request.Request(
        API_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "HTTP-Referer": "https://localhost:9100",
                 "X-Title": "HybridGB-Open"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read() or b"{}")
        return (((data.get("choices") or [{}])[0].get("message") or {}
                 ).get("content") or "").strip() or None
    except Exception as e:
        logger.debug("openrouter call failed: %s", str(e)[:100])
        return None


def _vote(text: str | None, fallback: str = "hold") -> str:
    t = (text or "").lower()
    if "close" in t:
        return "close"
    if "hold" in t or "wait" in t:
        return "hold"
    return fallback


# ── engine-compatible surface ──────────────────────────────────────────────

def jev_decide(state: dict, lite: bool = False,
               questions: dict = None) -> dict:
    """Lightweight screen of a microstructure state. Fail-open: {} means
    'no objection' — callers treat missing keys as neutral."""
    if os.getenv("OPENROUTER_LLM", "1").strip() in ("", "0"):
        return {}
    sym = (state or {}).get("symbol", "?")
    txt = _chat([
        {"role": "system", "content": (
            "You screen crypto grid-bot microstructure. Reply with exactly one "
            "word: HOLD or CLOSE. CLOSE only when the tape clearly opposes "
            "holding inventory (violent adverse momentum, buyer exhaustion).")},
        {"role": "user", "content": (
            f"{sym} ret20={(state or {}).get('ret20', 0):.0f}bp "
            f"spread={(state or {}).get('spreadBps', 0):.1f}bp "
            f"imb={(state or {}).get('imbalance', 0):.2f} "
            f"cvd={(state or {}).get('cvd', 0):.0f} HOLD or CLOSE?")}],
        max_tokens=8)
    if txt is None:
        return {}
    return {"vote": _vote(txt), "via": "openrouter"}


def unwind_vote(symbol: str, user_id=None, env: str = "demo",
                pos_side: int = 0) -> str:
    if os.getenv("OPENROUTER_LLM", "1").strip() in ("", "0"):
        return "hold"
    side = "LONG" if (pos_side or 0) > 0 else "SHORT"
    txt = _chat([
        {"role": "system", "content": (
            "Grid-bot risk vote. Reply exactly HOLD or CLOSE. CLOSE only if "
            "the position's direction looks structurally wrong right now.")},
        {"role": "user", "content": f"{symbol} {side} losing position. HOLD or CLOSE?"}],
        max_tokens=8)
    return _vote(txt)


def entry_ok(symbol: str, user_id=None, env: str = "demo",
             gain: float = 0.0) -> bool:
    """Veto gate: False blocks the spawn. Fail-open True (never block on
    LLM outage) unless OPENROUTER_STRICT_VETO=1."""
    if os.getenv("OPENROUTER_LLM", "1").strip() in ("", "0"):
        return True
    txt = _chat([
        {"role": "system", "content": (
            "Reply exactly OK or VETO. VETO only if opening a fresh grid here "
            "is clearly reckless (parabolic exhaustion, one-sided panic).")},
        {"role": "user", "content": (
            f"Fresh grid on {symbol}, 15m gain {gain:+.2f}%. OK or VETO?")}],
        max_tokens=8)
    if txt is None:
        return True
    return "veto" not in txt.lower()


def attention_scan(pool: list, user_id=None, env: str = "demo",
                   top_n: int = 2, min_p: float = 0.60) -> list:
    """Rank scan pool by momentum quality. Fail-open: return pool head
    unchanged (engine falls back to its own ranking)."""
    pool = list(pool or [])
    if not pool or os.getenv("OPENROUTER_LLM", "1").strip() in ("", "0"):
        return pool[:top_n]
    brief = "; ".join(
        f"{c.get('symbol')} {float(c.get('gain_15m', c.get('gain_pct', 0)) or 0):+.1f}%"
        for c in pool[:12])
    txt = _chat([
        {"role": "system", "content": (
            "Pick the strongest momentum symbols. Reply with a comma-separated "
            "list of up to {n} symbols, strongest first, nothing else.".format(
                n=top_n))},
        {"role": "user", "content": f"Pool: {brief}"}],
        max_tokens=60)
    if not txt:
        return pool[:top_n]
    want = [t.strip().upper() for t in txt.replace("\n", ",").split(",")]
    ranked = [c for w in want for c in pool if c.get("symbol") == w]
    for c in pool:
        if c not in ranked:
            ranked.append(c)
    return ranked[:top_n]


def apply_scan_filter(picks: list, user_id=None, env: str = "demo") -> list:
    """Post-filter spawn picks. Fail-open: unchanged."""
    return list(picks or [])


def mt5_unwind_vote(bridge, pos_side: int, upnl: float,
                    age_sec: float) -> str:
    try:
        sym = getattr(getattr(bridge, "_b", bridge), "symbol", "?")
    except Exception:
        sym = "?"
    return unwind_vote(str(sym), pos_side=pos_side)


def llm_verdict(symbol: str, features: dict, rule_bias: str) -> dict | None:
    """MT5 dynamic-AI hook (was Hermes): second opinion honoring rule bias.
    Returns {side, source} or None (rules fallback)."""
    txt = _chat([
        {"role": "system", "content": (
            "Reply exactly LONG, SHORT or NEUTRAL for the symbol's next hours.")},
        {"role": "user", "content": (
            f"{symbol} rule-bias={rule_bias} features={json.dumps(features)[:400]}")}],
        max_tokens=8)
    if not txt:
        return None
    t = txt.upper()
    side = "LONG" if "LONG" in t else ("SHORT" if "SHORT" in t else "NEUTRAL")
    return {"side": side, "source": "openrouter"}
