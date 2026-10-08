"""
activity.py — plain-language activity feed ("chatter") for the dashboard.

Every platform tab gets its own context ticker (binance / mt5 / system).
A background ingester diffs each bot's internal log ring buffer, translates
the NEW lines into NON-TECHNICAL user-facing chatter via a pattern map, and
pushes them to a bounded in-memory ring buffer.

Optional AI: summarize() batches recent items into a short plain-language
"what's happening" blurb using the same DeepSeek/OpenRouter endpoint as chat.
"""
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone

FEED_MAX = 300
_feed = {
    "binance": deque(maxlen=FEED_MAX),
    "mt5": deque(maxlen=FEED_MAX),
    "system": deque(maxlen=FEED_MAX),
}
_lock = threading.Lock()
_watermarks: dict = {}  # bot_id -> number of log lines already ingested
_last_text: dict = {}    # bot_id -> last pushed text (consecutive-dedupe)


def feed(platform: str, text: str, bot: str = "", symbol: str = ""):
    """Push one chatter item onto the platform's feed."""
    item = {
        "ts": time.time(),
        "t": datetime.now(timezone.utc).strftime("%H:%M"),
        "platform": platform,
        "bot": bot,
        "symbol": symbol,
        "text": text,
    }
    with _lock:
        _feed.setdefault(platform, deque(maxlen=FEED_MAX)).append(item)
    return item


def recent(platform: str = None, after: float = 0.0, limit: int = 60) -> list:
    """Feed items newer than `after` (up to `limit`), oldest first."""
    with _lock:
        if platform:
            items = list(_feed.get(platform, ()))
        else:
            items = [i for q in _feed.values() for i in q]
    items = [i for i in items if i["ts"] > after]
    return items[-limit:]


def bot_platform(bot) -> str:
    """Map a bot object to its feed platform (binance / mt5)."""
    br = getattr(bot, "bridge", None)
    if br is not None:
        return getattr(br, "platform", "binance") or "binance"
    return "binance"


# ── log-line → plain-language translation (first match wins) ────────────────

_PATTERNS = [
    (re.compile(r"✅ ladder order (BUY|SELL) ([\d.]+) @₹?([\d.]+)"),
     lambda m: f"Placed {m.group(1)} {m.group(2)} @ {m.group(3)}"),
    (re.compile(r"🧹 ghost cancelled (?:BUY|SELL)[^\n]*?(\d+) more to clear"),
     lambda m: "Cleaned up stray orders"),
    (re.compile(r"⏸ (?:BUY|SELL) [\d.]+ @[₹\d.]+ crosses (?:ask|bid|LTP) ₹?([\d.]+) — waiting for price to (fall|rise)"),
     lambda m: f"Waiting — price needs to {'fall into the buy zone' if m.group(2) == 'fall' else 'rise into the sell zone'}"),
    (re.compile(r"🔒 level (\d+): (BUY|SELL) leg open"),
     lambda m: f"Opened a {m.group(2)} leg at level {m.group(1)} — exit order is active"),
    (re.compile(r"🔓 level (\d+): round trip done"),
     lambda m: f"Level {m.group(1)} cycle complete — both sides active again"),
    (re.compile(r"⬆️ scaled in → (\d+) level"),
     lambda m: f"Grid expanded to {m.group(1)} levels per side"),
    (re.compile(r"🌙 (?:trending/dead|trending) market"),
     lambda m: "Market trending — grid paused, positions closed"),
    (re.compile(r"🚫 trending"),
     lambda m: "Trending market — standing by, no new entries"),
    (re.compile(r"🔄 drift ([+-][\d.]+)%"),
     lambda m: "Market moved — grid re-anchored to current price"),
    (re.compile(r"🛑 (?:Equity stop|equity stop|EQUITY STOP)"),
     lambda m: "Risk stop triggered — bot halted"),
    (re.compile(r"🗓 (\w+) session → spacing"),
     lambda m: f"{m.group(1)} session — grid spacing adjusted"),
    (re.compile(r"sized qty=([\d.]+) \(~([$₹])([\d.]+)/level"),
     lambda m: f"Level size {m.group(1)} (~{m.group(2)}{m.group(3)} per level)"),
    (re.compile(r"(BUY|SELL) filled @₹([\d.]+) \(cycle (\d+)\)"),
     lambda m: f"{m.group(1)} filled at ₹{m.group(2)}"),
    (re.compile(r"(BUY|SELL) filled @([\d.]+) \(cycle (\d+)\)"),
     lambda m: f"{m.group(1)} filled at {m.group(2)}"),
    (re.compile(r"🟢 (BUY|SELL) filled @₹?([\d.]+)"),
     lambda m: f"{m.group(1)} filled at {m.group(2)}"),
    (re.compile(r"🔴 (BUY|SELL) filled @₹?([\d.]+)"),
     lambda m: f"{m.group(1)} filled at {m.group(2)}"),
    (re.compile(r"📐 grid at center=([\d.]+)"),
     lambda m: f"Grid anchored at {m.group(1)}"),
    (re.compile(r"📐 grid placed: (\d+) orders @ ₹?([\d.]+)"),
     lambda m: f"Grid placed — {m.group(1)} orders around {m.group(2)}"),
    (re.compile(r"📐 Grid placed: (\d+) orders"),
     lambda m: f"Grid placed — {m.group(1)} orders"),
    (re.compile(r"🚀 Bot starting"),
     lambda m: "Bot started"),
    (re.compile(r"🟢 GuruAI grid started"),
     lambda m: "Bot started"),
    (re.compile(r"🛑 (?:Stopped|stopped)"),
     lambda m: "Bot stopped"),
    (re.compile(r"All positions closed"),
     lambda m: "All positions closed"),
    (re.compile(r"⚠️ (\d+) orders survived flatten"),
     lambda m: "Order cleanup retrying"),
    (re.compile(r"💰 Price: ₹?([\d.]+)"),
     lambda m: f"Price is {m.group(1)}"),
    (re.compile(r"🌙 DAILY RESET"),
     lambda m: "Daily reset — fresh grid placed"),
    (re.compile(r"(?:Loop error|❌ Fatal|reconcile fetch err|loop err)"),
     lambda m: "Minor hiccup — auto-recovering"),
]


def translate(line: str) -> str:
    """Map one engine log line to non-technical chatter, or None to skip."""
    line = re.sub(r"^\[\d{2}:\d{2}:\d{2}\]\s*", "", line).strip()
    if not line:
        return None
    for rx, fn in _PATTERNS:
        m = rx.search(line)
        if m:
            return fn(m)
    return None


def ingest(bot_snapshots: list):
    """Diff each bot's log ring buffer and push new chatter items."""
    for snap in bot_snapshots:
        bid = snap.get("id", "")
        logs = snap.get("logs") or []
        seen = _watermarks.get(bid, 0)
        if len(logs) <= seen:
            continue
        last = _last_text.get(bid)
        for line in logs[seen:]:
            text = translate(line)
            if text and text != last:
                feed(snap.get("platform", "binance"), text,
                     bot=bid, symbol=snap.get("symbol", ""))
                last = text
        _watermarks[bid] = len(logs)
        _last_text[bid] = last


def clear():
    with _lock:
        for q in _feed.values():
            q.clear()
        _watermarks.clear()
        _last_text.clear()


# ── optional AI summarizer (DeepSeek via OpenRouter — same as chat) ────────

def summarize(platform: str, minutes: int = 30) -> dict:
    """Plain-language summary of recent platform activity. Never raises."""
    items = recent(platform, after=time.time() - minutes * 60, limit=200)
    if not items:
        return {"summary": f"No activity on {platform} in the last {minutes} minutes.", "ai": False}
    key = os.getenv("DEEPSEEK_API_KEY", "")
    if not key:
        return {"summary": "AI summarizer not configured (DEEPSEEK_API_KEY missing).", "ai": False}
    lines = "\n".join(
        f"- {i['t']} {i['symbol'] or i['bot'] or i['platform']}: {i['text']}" for i in items)
    prompt = (
        "You summarize a crypto/derivatives grid-trading bot dashboard for its owner. "
        "Turn the recent activity events below into 2-4 short, friendly, NON-TECHNICAL "
        "sentences describing what the bots are doing (fills, grid moves, pauses, "
        "risk events, platform health). No jargon, no numbers overload. If everything "
        "is quiet, say so in one line.\n\nEVENTS:\n" + lines
    )
    try:
        import httpx
        r = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json={
                "model": "deepseek/deepseek-chat",
                "messages": [
                    {"role": "system", "content": "You write short, friendly trading-dashboard summaries."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.7,
                "max_tokens": 300,
            },
            headers={"Authorization": f"Bearer {key}",
                     "HTTP-Referer": "http://localhost:9100", "X-Title": "gb-api"},
            timeout=25,
        )
        r.raise_for_status()
        return {"summary": r.json()["choices"][0]["message"]["content"].strip(), "ai": True}
    except Exception as e:
        return {"summary": f"AI summary unavailable right now ({str(e)[:60]}).", "ai": False}