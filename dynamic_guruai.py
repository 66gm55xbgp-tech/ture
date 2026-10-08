#!/usr/bin/env python3
"""Dynamic (trend-following) GuruAI engine.

A drop-in subclass engine that turns the neutral grid into a
trend-following dynamic grid, WITHOUT touching guru_ai.py:

  - DynamicGuruAIBot  : same grid + ALL safety (crash guard, equity stop,
                        scale freeze, max-hold, individual stops), but the
                        ladder LEANS with the trend instead of being
                        symmetric:
                          long    -> BUY ladder rests below; SELLs appear
                                     only as exits for filled BUY legs
                          short   -> mirror
                          neutral -> base symmetric behavior
                        Optionally opens ONE initial leg (market) when a
                        directional bias activates while flat, so the move
                        is ridden from the start (GURU_DYN_INITIAL_LEGS=1).

  - Optional LLM layer (GURU_LLM=1): provider-switchable consult
    (GURU_LLM_PROVIDER=openrouter|deepseek, default openrouter) with
    reasoning enabled that may confirm/override the rule-based bias at
    pick/flip events ONLY. Strict JSON verdict. Timeout + retry +
    circuit breaker; on any failure the rules decide. The LLM NEVER touches
    safety, sizing, or order placement — bias only.

Rule-based bias:
  - neutral -> long/short : |15m gain| >= GURU_BIAS_ENTER (2%)
  - flip long<->short     : opposite 15m >= GURU_BIAS_FLIP (2.5%) AND
                            ADX >= GURU_BIAS_FLIP_ADX (30)
  - -> neutral            : |15m| < GURU_BIAS_NEUTRAL (1%) for
                            GURU_BIAS_NEUTRAL_HOLD (3) consecutive checks
                            (hysteresis — no flip-flopping)
"""

import json
import os
import time
import urllib.request
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from guru_ai import (GuruAIBot, GuruAIManager, BinanceBridge, BotStatus,
                     SCALE_FREEZE_PCT,
                     logger, _guru_claim, _guru_release, _guru_in_use)

# ── tuning (all % thresholds are in PERCENT units: 2.0 = 2%) ────────────────
BIAS_SCAN_SEC = float(os.getenv("GURU_BIAS_SCAN_SEC", "60"))
BIAS_ENTER = float(os.getenv("GURU_BIAS_ENTER", "2.0"))        # |15m|% to go directional
BIAS_FLIP = float(os.getenv("GURU_BIAS_FLIP", "2.5"))          # opposite |15m|% to flip
BIAS_FLIP_ADX = float(os.getenv("GURU_BIAS_FLIP_ADX", "30"))   # ADX floor for a flip
BIAS_NEUTRAL = float(os.getenv("GURU_BIAS_NEUTRAL", "1.0"))    # |15m|% below this -> neutral
BIAS_NEUTRAL_HOLD = int(os.getenv("GURU_BIAS_NEUTRAL_HOLD", "3"))  # consecutive checks
INITIAL_LEGS = int(os.getenv("GURU_DYN_INITIAL_LEGS", "1"))    # initial leg on bias activation
# Initial-leg SIZING (capture fix A): fraction of the bot's notional capacity
# the bias entry takes (0.4 = 40%). A +10% mover then pays ~4% of capacity
# instead of one tiny ladder level. 0 = old behavior (one level qty).
INITIAL_CAP_RATIO = float(os.getenv("GURU_DYN_INITIAL_CAP_RATIO", "0.4"))
INITIAL_USD = float(os.getenv("GURU_DYN_INITIAL_USD", "0"))     # fixed notional override (0 = auto)
# ADX floor for opening the initial leg (2026-08-18): a +2.68% 15m pump with
# ADX=9 (thin dead-cat) cost a full -10% alloc equity stop on TUT. Weak
# trends now stay ladder-only — the market-entry leg needs real trend
# strength, mirroring the flip rule (ADX >= 30).
INITIAL_ADX_MIN = float(os.getenv("GURU_DYN_INITIAL_ADX_MIN", "30"))
# Anchors (XAUUSDT/BTCUSDT) are pure grid anchors — never open the trend
# initial leg (it is what kept tripping the tight -10% alloc stop on BTC).
INITIAL_ANCHOR_SKIP = os.getenv("GURU_DYN_INITIAL_ANCHOR_SKIP", "1") == "1"
# LLM override dwell (2026-08-18): the LLM may only CHANGE the bias if the
# previous bias change (any source) is at least this old. Kills the
# ACE-style 7-flips-in-2h whipsaw (LLM kept overriding the rule hysteresis
# into positions the market rejected). Rule-driven changes keep their own
# hysteresis and are not dwelled.
BIAS_MIN_DWELL_SEC = float(os.getenv("GURU_BIAS_MIN_DWELL_SEC", "600"))

# ── LLM layer ───────────────────────────────────────────────────────────────
# Provider: openrouter only in this build (single LLM path — the user's own
# OpenRouter key + model from Settings, or OPENROUTER_API_KEY / OPENROUTER_MODEL
# env). No other provider endpoints are called.
LLM_ENABLED = os.getenv("GURU_LLM", "1") == "1"
LLM_PROVIDER = "openrouter"
LLM_MODEL = os.getenv("GURU_LLM_MODEL", "")  # "" = Settings choice or provider default
LLM_CONF_MIN = float(os.getenv("GURU_LLM_CONF_MIN", "0.7"))
LLM_EFFORT = os.getenv("GURU_LLM_EFFORT", "low")  # reasoning effort (low/high/max)
# While bias == neutral the LLM used to be consulted every 60s scan — in
# choppy idle markets that's ~4 calls/min (≈5,760/day, ~$1.5-2/day) for
# nearly identical market data. Flip candidates (rule != bias) still
# consult INSTANTLY; only the neutral-idle polls are throttled.
LLM_NEUTRAL_COOLDOWN = float(os.getenv("GURU_LLM_NEUTRAL_COOLDOWN", "300"))
_LLM_STATE = {"fails": 0, "off_until": 0.0, "calls": 0}


def _llm_key() -> str:
    key = (os.getenv("OPENROUTER_LLM_KEY", "")
           or os.getenv("OPENROUTER_API_KEY", ""))
    if not key:
        try:
            import local_store
            key = str((local_store.get_setting("openrouter") or {}).get("api_key", "") or "")
        except Exception:
            pass
    return key


def _llm_model() -> str:
    if LLM_MODEL:
        return LLM_MODEL
    try:
        import local_store
        m = str((local_store.get_setting("openrouter") or {}).get("model", "") or "")
        if m:
            return m
    except Exception:
        pass
    return os.getenv("OPENROUTER_MODEL", "")


def _extract_llm_json(text: str) -> dict:
    """Find first { to last } and parse, repairing truncated strings like
    '{"bias":"neutral","reason":"unterminated ...' by closing the string/brackets."""
    if not text:
        raise ValueError("empty")
    lo = text.find("{")
    hi = text.rfind("}")
    raw = ""
    if lo >= 0 and hi > lo:
        raw = text[lo:hi+1]
        try:
            return json.loads(raw)
        except Exception:
            pass
    # truncated? take from first { to end and try to repair
    if lo >= 0:
        raw = text[lo:].strip()
        # count open braces/brackets and quotes
        # close unterminated string
        if raw.count('"') % 2 == 1:
            raw += '"'
        # close open objects/arrays
        opens = raw.count("{") - raw.count("}")
        if opens > 0:
            raw += "}" * opens
        # also handle missing commas vs trailing
        raw = raw.strip()
        # last attempt: if still not json, try to cut at last comma before truncation and close
        for _ in range(3):
            try:
                return json.loads(raw)
            except Exception as e:
                msg = str(e)
                # Unterminated string at column X -> trim last incomplete field
                if "Unterminated string" in msg or "Expecting" in msg:
                    # drop last incomplete '"reason"' tail
                    cut = raw.rfind('"reason"')
                    if cut > 10:
                        raw = raw[:cut].rstrip().rstrip(",") + "}"
                        continue
                # generic: trim trailing comma
                raw = raw.rstrip().rstrip(",")
                if not raw.endswith("}"):
                    raw += "}"
                # one more try
                try:
                    return json.loads(raw)
                except:
                    break
        # final fallback: return minimal neutral
        raise ValueError(f"unparseable json after repair: {raw[:200]}")
    raise ValueError(f"no json in: {text[:200]}")


def _llm_available() -> bool:
    return LLM_ENABLED and bool(_llm_key()) and time.time() > _LLM_STATE["off_until"]


def _llm_chat(prompt: str) -> str:
    """One chat call for the current provider. Returns the raw content text
    (reasoning included when the final answer is empty). Raises on error.
    Robust: larger max_tokens (reasoning eats budget), truncated JSON repair.
    OpenRouter only (single provider in this build)."""
    body = {
        "model": _llm_model(),
        "messages": [{"role": "user", "content": prompt}],
        "reasoning": {"enabled": True},
        "max_tokens": 900,
    }
    url = "https://openrouter.ai/api/v1/chat/completions"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_llm_key()}",
                 "Content-Type": "application/json",
                 "HTTP-Referer": "https://localhost:9100",
                 "X-Title": "Hybrid-GB"})
    with urllib.request.urlopen(req, timeout=18) as r:
        d = json.load(r)
    if "error" in d:
        raise RuntimeError(str(d["error"].get("message", "?"))[:180])
    # OpenRouter may return error inside choices as well
    choices = d.get("choices") or []
    if not choices:
        raise RuntimeError(f"no choices in response: {str(d)[:200]}")
    msg = choices[0].get("message") or {}
    content = msg.get("content") or ""
    if not content:
        if LLM_PROVIDER == "openrouter":
            rd = msg.get("reasoning_details", [])
            if rd:
                content = " ".join(t.get("text", "") for t in rd if isinstance(t, dict))[-1200:]
            else:
                content = msg.get("reasoning") or ""
        else:
            content = msg.get("reasoning_content") or ""
    if not content or not content.strip():
        # surface full payload for diagnosis (trimmed)
        raise RuntimeError(f"empty content, msg keys {list(msg.keys())}, raw {str(d)[:400]}")
    return content.strip()


# ── HERMES agent verdict bus (Phase 2: Hermes is the brain) ───────────────
# The engine consults the HERMES agent (control plane) via the Supabase
# decision bus instead of OpenRouter. Fast/safety decisions stay in code.
# If no fresh agent verdict exists, the engine files a request and falls
# back to its own rule-based bias (Phase 1 behavior) — never blocks.

_LLM_TTL_S = 1800          # agent verdict validity window
_LLM_REQ_COOLDOWN = 600.0  # file at most one request per symbol / 10 min
_LLM_LAST_REQ: dict = {}


def _llm_verdict(symbol: str, features: dict, rule_bias: str,
                    user_id: str = None, defaults: dict = None):
    """Look up the agent's latest verdict; file a request if stale.
    Returns a verdict dict like the old LLM one, or None (rules fallback)."""
    import openrouter_client
    v = None
    try:
        v = openrouter_client.llm_verdict(symbol, features or {}, rule_bias)
    except Exception as e:
        logger.debug("openrouter verdict fetch: %s" % e)
    if not v:
        return None
    d = defaults or {}
    bias = str(v.get("bias") or "").lower()
    if bias not in ("long", "short", "neutral"):
        return None
    def _pct(key_, default, lo, hi):
        try: x = float(v.get(key_) or default)
        except (TypeError, ValueError): x = default
        if x > 1: x /= 100.0
        return min(max(x, lo), hi)
    out = {
        "bias": bias,
        "confidence": float(v.get("confidence") or 0.0),
        "reason": (v.get("reason") or "HERMES")[:90],
        "entry_offset_pct": _pct("entry_offset_pct", d.get("entry_offset_pct", 0.06), 0.02, 0.20),
        "tp_vol_pct": _pct("tp_vol_pct", d.get("tp_vol_pct", 0.025), 0.008, 0.05),
        "sl_vol_pct": _pct("sl_vol_pct", d.get("sl_vol_pct", 0.015), 0.006, 0.02),
    }
    try:
        sp = float(v.get("spacing_mult") or 1.0)
        out["spacing_mult"] = min(max(sp, 0.7), 1.8)
    except (TypeError, ValueError):
        pass
    try:
        lev = int(float(v.get("leverage") or 10))
        out["leverage"] = max(3, min(lev, 15))
    except (TypeError, ValueError):
        pass
    logger.info("HERMES verdict: %s rules=%s -> %s (conf %.2f) — %s" %
                (symbol, rule_bias, out["bias"], out["confidence"], out["reason"]))
    return out


def llm_bias_binance(symbol: str, features: dict, rule_bias: str) -> dict:
    """HERMES agent verdict for the Binance desk (deep ladder rails).
    Rules fallback (Phase 1) whenever no fresh verdict exists. The old
    OpenRouter consult is retired — the agent IS the brain (2026-09-02)."""
    uid = features.get("user_id")
    v = _llm_verdict(symbol, features, rule_bias, user_id=uid,
                        defaults={"entry_offset_pct": 0.06,
                                  "tp_vol_pct": 0.025,
                                  "sl_vol_pct": 0.015})
    if v:
        return v
    return {"bias": rule_bias, "confidence": 0.0,
            "reason": "rules fallback (no fresh HERMES verdict)",
            "entry_offset_pct": 0.06, "tp_vol_pct": 0.025,
            "sl_vol_pct": 0.015, "spacing_mult": 1.0, "leverage": 10}


def llm_bias(symbol: str, features: dict, rule_bias: str) -> dict:
    """HERMES agent verdict (MT5/legacy rails). Rules fallback (Phase 1)
    whenever no fresh verdict exists."""
    uid = features.get("user_id")
    v = _llm_verdict(symbol, features, rule_bias, user_id=uid,
                        defaults={"entry_offset_pct": 0.04,
                                  "tp_vol_pct": 0.055,
                                  "sl_vol_pct": 0.005})
    if v:
        return v
    return {"bias": rule_bias, "confidence": 0.0,
            "reason": "rules fallback (no fresh HERMES verdict)",
            "entry_offset_pct": 0.04, "tp_vol_pct": 0.055,
            "sl_vol_pct": 0.005}


class DynamicGuruAIBot(GuruAIBot):
    """GuruAIBot with trend bias. All safety inherited unchanged."""

    def __init__(self, *args, bias: str = "neutral", **kwargs):
        super().__init__(*args, **kwargs)
        self.bias = bias                        # long | short | neutral
        self._bias_checked_at = 0.0
        self._bias_neutral_count = 0
        self._bias_source = "rules"
        self._bias_changed_at = 0.0             # last bias change (LLM dwell)
        self._llm_neutral_checked_at = 0.0      # neutral-idle LLM throttle
        self._tp_vol_pct = float(os.getenv("BINANCE_TP_VOL_PCT", "0.025") or 0.025)
        self._sl_vol_pct = float(os.getenv("BINANCE_SL_VOL_PCT", "0.015") or 0.015)
        self._entry_offset_pct = float(os.getenv("BINANCE_ENTRY_PCT", "0.06") or 0.06)
        # NY 0.40% base tight scalp; fallback swing 1.33× = 0.53% if AI unavailable, AI picks 0.7-1.8
        self._spacing_mult = float(os.getenv("BINANCE_SPACING_FALLBACK_MULT", "1.33") or 1.33)

    def start(self):
        plat = str(getattr(self.bridge, "platform", "") or "").lower()
        # AI PRIMARY for the Binance desk (OpenRouter): deep ladder compounding
        # at lev10 with fallback.
        if plat == "binance":
            try:
                self._size_qty()
            except Exception:
                pass
            f = self._bias_features()
            # branch to platform-specific LLM
            if plat == "binance":
                v = llm_bias_binance(self.symbol, f, self.bias or "neutral")
            else:
                v = llm_bias(self.symbol, f, self.bias or "neutral")
            # always apply rails (even if bias stays rule-based)
            self._apply_llm_order_plan(v or {
                "entry_offset_pct": self._entry_offset_pct,
                "tp_vol_pct": self._tp_vol_pct, "sl_vol_pct": self._sl_vol_pct})
            # Binance deep ladder hint: let AI tune spacing/leverage with robust fallback
            if plat == "binance" and v and v.get("spacing_mult"):
                try:
                    # spacing_mult scales the session target (0.7-1.8) — clamp already
                    self._spacing_mult = float(v["spacing_mult"])
                except: pass
                # leverage proposal — enforce 10× fallback on invalid
                try:
                    lev_p = int(v.get("leverage", 10))
                    lev_p = max(3, min(lev_p, 15))
                    self.leverage = lev_p
                    if hasattr(self.bridge, "_set_leverage"):
                        try: self.bridge._set_leverage(lev_p)
                        except: pass
                except: pass
            if v and v.get("bias") in ("long", "short", "neutral"):
                if v.get("confidence", 0) >= LLM_CONF_MIN:
                    self.bias = v["bias"]
                    self._bias_source = "llm"
            self.core.grid_levels = 1
            self._log(f"🤖 order plan bias={self.bias} entry={self._entry_offset_pct*100:.3f}% "
                      f"TP {self._tp_vol_pct*100:.2f}%-of-vol SL {self._sl_vol_pct*100:.2f}%-of-vol"
                      + (f" lev={getattr(self,'leverage',10)}× sp×{getattr(self,'_spacing_mult',1.0):.2f}" if plat=="binance" else ""))
        super().start()

    # ── bias features ──
    def _bias_features(self) -> dict:
        bars = self._bars("15m", limit=6) or []
        g15 = g1 = g4 = 0.0
        if len(bars) >= 2:
            g15 = (bars[-1]["close"] - bars[-2]["close"]) / bars[-2]["close"] * 100
        h1 = self._bars("1h", limit=4) or []
        if len(h1) >= 2:
            g1 = (h1[-1]["close"] - h1[-2]["close"]) / h1[-2]["close"] * 100
        h4 = self._bars("4h", limit=3) or []
        if len(h4) >= 2:
            g4 = (h4[-1]["close"] - h4[-2]["close"]) / h4[-2]["close"] * 100
        vol = 0.0
        if bars:
            vol = sum(b.get("quote_volume", 0) for b in bars)
        adx = self._adx_value()
        sess = "ASIA" if datetime.now(timezone.utc).hour < 8 else (
            "LONDON" if datetime.now(timezone.utc).hour < 16 else "NY")
        mark = self._price() or 0.0
        qty = float(getattr(self, "qty", 0) or 0)
        hx = 90.0
        try:
            hx = float(self.bridge._hedge_rate() or 90.0)
        except Exception:
            hx = 90.0
        notional_inr = qty * mark * hx if mark and qty else 0.0
        notional_usd = qty * mark if mark and qty else 0.0
        return {"g15": g15, "g1": g1, "g4": g4, "adx": adx, "vol": vol,
                "sess": sess, "mark": mark, "qty": qty,
                "notional_inr": notional_inr, "notional_usd": notional_usd,
                "free_margin": float(getattr(self, "free_margin", 0) or 0),
                "leverage": int(getattr(self, "leverage", 10) or 10),
                "n_levels": int(getattr(self, "grid_levels", 6) or 6),
                "spacing_pct": float(getattr(self.core, "spacing_pct", 0.0035) or 0.0035)}

    def _rule_bias(self, f: dict) -> str:
        g15, adx = f["g15"], f["adx"]
        if self.bias == "neutral":
            if g15 >= BIAS_ENTER:
                return "long"
            if g15 <= -BIAS_ENTER:
                return "short"
            return "neutral"
        if self.bias == "long":
            if g15 <= -BIAS_FLIP and adx >= BIAS_FLIP_ADX:
                return "short"
            if abs(g15) < BIAS_NEUTRAL:
                self._bias_neutral_count += 1
                if self._bias_neutral_count >= BIAS_NEUTRAL_HOLD:
                    self._bias_neutral_count = 0
                    return "neutral"
            else:
                self._bias_neutral_count = 0
            return "long"
        # short
        if g15 >= BIAS_FLIP and adx >= BIAS_FLIP_ADX:
            return "long"
        if abs(g15) < BIAS_NEUTRAL:
            self._bias_neutral_count += 1
            if self._bias_neutral_count >= BIAS_NEUTRAL_HOLD:
                self._bias_neutral_count = 0
                return "neutral"
        else:
            self._bias_neutral_count = 0
        return "short"

    def _maybe_update_bias(self):
        """Bias check every BIAS_SCAN_SEC (60s): rules first; LLM may
        confirm/override at flip candidates. LLM only touches bias."""
        now = time.time()
        if now - self._bias_checked_at < BIAS_SCAN_SEC:
            return
        self._bias_checked_at = now
        f = self._bias_features()
        rule = self._rule_bias(f)
        new_bias = rule
        # LLM consult: flip candidates (rule != bias) ALWAYS — that is the
        # event the LLM exists for. Neutral-idle polls are throttled to
        # every LLM_NEUTRAL_COOLDOWN (idle market data barely changes
        # minute-to-minute; the rule scan itself still runs every 60s).
        consult = rule != self.bias
        if not consult and rule == self.bias == "neutral":
            consult = now - self._llm_neutral_checked_at >= LLM_NEUTRAL_COOLDOWN
        if _llm_available() and consult:
            self._llm_neutral_checked_at = now
            # Binance primary LLM sizing: route accordingly, fallback 10× lev
            plat = str(getattr(self.bridge, "platform", "") or "").lower()
            if plat == "binance":
                v = llm_bias_binance(self.symbol, f, rule)
            else:
                v = llm_bias(self.symbol, f, rule)
            if v:
                self._apply_llm_order_plan(v)
                # Binance spacing/leverage advisory from LLM
                if plat == "binance":
                    if v.get("spacing_mult") is not None:
                        try: self._spacing_mult = float(v["spacing_mult"])
                        except: pass
                    if v.get("leverage") is not None:
                        try:
                            lev_p = max(3, min(int(v["leverage"]), 15))
                            self.leverage = lev_p
                            if hasattr(self.bridge, "_set_leverage"):
                                try: self.bridge._set_leverage(lev_p)
                                except: pass
                        except: pass
            if v and v["confidence"] >= LLM_CONF_MIN:
                if v["bias"] != self.bias and \
                        now - self._bias_changed_at < BIAS_MIN_DWELL_SEC:
                    logger.info(f"🕰 LLM override {v['bias']} held — dwell "
                                f"{BIAS_MIN_DWELL_SEC/60:.0f}m not elapsed "
                                f"({self.symbol}, conf {v['confidence']:.2f})")
                else:
                    new_bias = v["bias"]
                    self._bias_source = "llm"
        if new_bias != self.bias:
            self._bias_changed_at = now
            self._log(f"🧭 bias {self.bias} -> {new_bias} "
                      f"(15m={f['g15']:+.2f}% ADX={f['adx']:.0f} [{self._bias_source}])")
            prev_bias = self.bias
            self.bias = new_bias
            self._open_initial_leg()
            self._reanchor_on_bias_flip(prev_bias)

    def _apply_llm_order_plan(self, v: dict):
        """Copy LLM rails + entry tightness onto the bot/bridge (platform-aware)."""
        if not v:
            return
        if v.get("entry_offset_pct"):
            self._entry_offset_pct = float(v["entry_offset_pct"])
        if v.get("tp_vol_pct"):
            self._tp_vol_pct = float(v["tp_vol_pct"])
        if v.get("sl_vol_pct"):
            self._sl_vol_pct = float(v["sl_vol_pct"])
        br = getattr(self, "bridge", None)
        if br is not None:
            # per-platform defaults: Binance 1.5/0.6
            plat = str(getattr(br, "platform", "") or "").lower()
            d_tp, d_sl = (0.015, 0.006) if plat == "binance" else (0.055, 0.005)
            br.tp_vol_pct = getattr(self, "_tp_vol_pct", d_tp)
            br.sl_vol_pct = getattr(self, "_sl_vol_pct", d_sl)

    def _reanchor_on_bias_flip(self, prev_bias: str):
        """Bias-aware immediate re-anchor: when the bias flips while FLAT,
        re-center the ladder around current price instead of waiting out the
        stale timer — a bias flip means the old grid placement is obsolete.
        Never runs with an open leg/position (their exits stay protected;
        crash guard + trail lock manage them)."""
        if self._has_position() or any(self._pair_pending.values()):
            return
        price = self._price()
        if price <= 0 or not self.qty:
            return
        self._log(f"🧭 bias flip ({prev_bias} -> {self.bias}) -> re-anchoring "
                  f"ladder @ {price:.2f}")
        self._flatten()
        self.core.reset_grid(reset_regime=False)
        self.qty = 0.0
        self._size_qty()
        time.sleep(1)
        self._place_grid()

    def _open_initial_leg(self):
        """On bias activation while FLAT, open ONE market leg in the bias
        direction so the move is ridden immediately (not just laddered).
        Marked as level-1 pending so its exit rests at the ladder.

        Sizing (capture fix A): the leg takes INITIAL_CAP_RATIO (default 40%)
        of the bot's notional capacity, so a +10% mover pays ~4% of capacity
        instead of one tiny ladder level. Fixed anchors skip the leg entirely
        (XAU/BTC are pure grid anchors — the leg was what kept tripping the
        tight -10% alloc stop on BTC)."""
        if INITIAL_LEGS <= 0 or self.bias == "neutral":
            return
        if INITIAL_ANCHOR_SKIP and getattr(self, "is_anchor", False):
            return
        if INITIAL_ADX_MIN > 0:
            adxv = self._adx_value()
            if adxv < INITIAL_ADX_MIN:
                self._log(f"🧭 initial leg held: ADX {adxv:.0f} < "
                          f"{INITIAL_ADX_MIN:.0f} — weak trend, ladder only")
                return
        if self._has_position() or any(self._pair_pending.values()) or not self.qty:
            return
        side = "BUY" if self.bias == "long" else "SELL"
        qty = self._initial_qty()
        if qty <= 0:
            return
        try:
            self.bridge._client.futures_create_order(
                symbol=self.bridge.binance_symbol, side=side, type="MARKET",
                quantity=qty)
        except Exception as e:
            self._log(f"initial leg failed: {str(e)[:70]}")
            return
        self._pair_pending[1] = side
        self._pos_ref_price = 0.0   # crash guard re-anchors on the fresh exposure
        self._log(f"🧭 initial {self.bias} leg {qty} @market — riding the move")

    def _initial_qty(self) -> float:
        """Notional for the bias-entry leg. INITIAL_USD > 0 overrides with a
        fixed notional; otherwise INITIAL_CAP_RATIO x capacity. Capped at
        50% of capacity and 50% of account buying power."""
        si = getattr(self.bridge, "_symbol_info", {}) or {}
        min_qty = float(si.get("min_qty", 0.001) or 0.001)
        price = self._price() or 1.0
        if INITIAL_CAP_RATIO <= 0 and INITIAL_USD <= 0:
            return max(self.bridge._round_qty(self.qty), min_qty)  # old 1-level behavior
        cap = getattr(self, "capacity_usd", 0.0) or self.alloc_usd * self.leverage
        notional = INITIAL_USD if INITIAL_USD > 0 else cap * INITIAL_CAP_RATIO
        notional = min(notional, cap * 0.5)
        if self.free_margin > 0:
            notional = min(notional, self.free_margin * self.leverage * 0.5)
        if notional <= 0:
            return 0.0
        return max(self.bridge._round_qty(notional / price), min_qty)

    # ── asymmetric ladder ──
    def _desired_orders(self) -> list:
        """Bias-aware ladder:
          long   : BUYs rest at every level; a SELL rests only as the exit of
                   a level whose BUY leg filled (pending BUY).
          short  : mirror.
          neutral: base behavior (both sides rest)."""
        plat = str(getattr(self.bridge, "platform", "") or "").lower()
        if self.bias == "neutral":
            return super()._desired_orders()
        if self.core.center <= 0 or not self.qty:
            return []
        si = getattr(self.bridge, "_symbol_info", {}) or {}
        min_qty = float(si.get("min_qty", 0.001) or 0.001)
        qty = max(self.bridge._round_qty(self.qty), min_qty)
        entry_side = "BUY" if self.bias == "long" else "SELL"
        exit_side = "SELL" if self.bias == "long" else "BUY"
        out = []
        for idx, (side, px) in enumerate(
                self.core.level_pairs(self.core.center, self.core.active_levels)):
            level = idx // 2 + 1
            pending = self._pair_pending.get(level)
            if pending == side:
                continue                                # leg open — re-stock paused
            if side == exit_side and pending != entry_side:
                continue                                # no naked counter-side entries
            out.append((side, self.bridge._round_price(px), qty))
        return out

    # ── tick wrapper ──
    def _neutral_tick(self):
        self._maybe_update_bias()
        super()._neutral_tick()

    # ── direction-aware scale-in freeze ──
    def _escalate(self):
        # Base freeze guards the LONG side (price far below center). Short
        # bias needs the mirror: no new levels while price runs far ABOVE
        # center (selling into a parabolic pump = the wrong side again).
        if self.bias == "short" and self.core.center > 0:
            price = self._price()
            if price > 0 and (price - self.core.center) / self.core.center > SCALE_FREEZE_PCT:
                self._log(f"🧊 scale-in frozen — price "
                          f"{(price - self.core.center) / self.core.center * 100:+.1f}% "
                          f"above center")
                return
        super()._escalate()


# ── dynamic manager ─────────────────────────────────────────────────────────
class DynamicGuruAIManager(GuruAIManager):
    """Manager that spawns bias-aware bots. Everything else inherited
    (scan, rotation, dead-slot replacement, anchors, bank hooks)."""

    def _bot_cls(self):
        return DynamicGuruAIBot


def init_guru(orchestrator, env: str = "demo", user_id: str = None,
              group: str = "all", max_price: float = None,
              n_bots: int = None, platform: str = "binance") -> DynamicGuruAIManager:
    return DynamicGuruAIManager(orchestrator, env, user_id=user_id,
                                group=group, max_price=max_price, n_bots=n_bots,
                                platform=platform)
