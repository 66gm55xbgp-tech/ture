# Ture

> An open-source experimental trading engine for learning, research, and experimentation.

Ture is a self-hosted trading tool you run on your own machine. It connects to your own exchange account, manages orders according to configurable strategy modules, and exposes everything through a local dashboard. What exactly it can do is best discovered by reading the code and running it on testnet.

## Disclaimer / Important Notice

- This project is provided for learning, experimentation, research, and educational purposes only.
- It is **not financial, investment, trading, or professional advice** of any kind.
- No profitability, performance, or financial return is guaranteed or implied.
- You are solely responsible for your own trading decisions, configuration, risk management, capital, and compliance with applicable laws, regulations, broker/exchange rules, and terms of service.
- This project does not provide managed trading, investment management, custody, or personalized investment recommendations.
- The software is **self-hosted and single-user per installation**: you clone or fork the MIT-licensed source and run your own instance on infrastructure you control.
- You connect **your own Binance account using your own API credentials**. Keys and funds stay under your control — never share them with the project author or any third-party server. The author does not receive, control, or custody user funds or credentials, and does not execute trades on anyone's behalf.
- Use appropriately restricted API keys. **Never enable withdrawal permissions** on keys used by any trading bot.
- Automated trading involves substantial risk, including possible loss of capital. Test with paper trading / testnet and understand the strategy modules before using real funds.
- The MIT license governs the software itself; it is not financial authorization, investment advice, or a guarantee of regulatory compliance in any jurisdiction.
- Any optional **Buy Me a Coffee** support is voluntary support for the open-source project — not payment for investment management, trading profits, signals, or guaranteed returns.

### How it works

```text
GitHub
  ↓  MIT-licensed source code
Anyone can download / fork / run it
  ↓
User runs their OWN instance on their OWN server or machine
  ↓
User connects THEIR Binance account using THEIR own API keys
  ↓
User makes THEIR own trading decisions and assumes THEIR own risk
  ↓
Optional Buy Me a Coffee support (voluntary, no returns promised)
```

## What it does

- **Automated order management** on Binance (USDT-M futures) & MT5 — including adaptive grid-style positioning modules on ETH, BTC, XAU (ladders in, take profit on reversion).
- **Optional AI screening** via your own [OpenRouter](https://openrouter.ai/keys) key (entry veto, unwind votes — works without it in math-only mode).
- **Risk governor**: drawdown bands, floating-heat limits, session/daily sit-outs, weekend-flat, 150 s minimum hold, funds-based leg cap.
- **Backtester** with realistic costs, per-bar heat curves, profit factor / expectancy stats.
- **One server**: backend + dashboard at `http://127.0.0.1:9100`, local SQLite storage — no cloud accounts.

## Quick start

```bash
./run.sh
# → open http://127.0.0.1:9100
```

1. **Settings** → save Binance **DEMO** (testnet) keys. Testnet first, always.
2. Optional: save your OpenRouter key + model (e.g. `anthropic/claude-3.5-haiku`) for AI screening.
3. Spawn a bot per symbol (mode `scalp`, comp OFF ≈ validated baseline).
4. Tick `prop` for evaluation-style discipline (session/daily sit-outs, 0:00 UTC reset, DD governor). Weekend-flat applies either way.

MT5: attach `ea/Ture_Bridge_EA.mq5` to a chart, generate a token in Settings ▸ MT5, paste token + server URL into the EA inputs, allow WebRequest to your server URL.

## Layout

| Path | What |
|---|---|
| `server.py` | All-in-one API + static dashboard |
| `guru_ai.py` | Live engine (Binance) |
| `mt5_guru.py` / `dynamic_guruai.py` | MT5 engine + AI hook |
| `neutral_grid.py` | Grid math core |
| `strategy_core.py` | Frozen v1.0 parameters + signal type |
| `risk_engine.py` / `prod_guard.py` | Portfolio governor, state machine |
| `openrouter_client.py` | LLM screening (your key, your model) |
| `local_store.py` / `storage.py` / `session_book.py` | Local SQLite stores |
| `execution/` | Venue adapters (Binance, MT5) |
| `bridge.py` / `bridge_mt5.py` | Exchange/broker bridges |
| `backtest.py` | Offline replay (`cost_bps=3` default) |
| `dashboard-v2/` | Web UI (served by server.py) |
| `ea/` | MT5 Expert Advisor |
| `config/` | Risk profiles (YAML, tune freely) |
| `data/` | Local runtime state (git-ignored) |

## Architecture

```text
STRATEGY CORE (strategy_core.py — parameters + normalized signals)
      |
      v
PORTFOLIO RISK ENGINE (risk_engine.py — per-tick governor verdict)
      |
      v
BROKER ADAPTER (execution/ — translates signals, confirms fills, reconciles)
      +---- Binance (bridge.py)
      +---- MT5     (bridge_mt5.py + EA)
```

The strategy layer emits normalized signals; adapters handle venue specifics (lots, sessions, spreads). Supported venue: Binance USDT-M futures (primary, backtested) and MT5 via the EA bridge (execution behavior differs — spreads, swaps, sessions, lot steps; validate separately).

## Configuration

Copy `.env.example` to `.env`, or use the dashboard Settings UI (stored in local SQLite). Keys: Binance demo/live, OpenRouter key/model, MT5 EA token, optional `ADMIN_PASSWORD` for non-localhost access. Tuning knobs: grid mode, leverage, vol/wallet fractions, session behavior, hold time, prop thresholds (`config/prop_maven_10k.yaml`).

## Risk controls

Drawdown bands (warn / stop-new-entries / emergency flatten), portfolio floating-heat limits with a separate ETH+BTC heat watch, per-book equity stops, session bank/loss sit-outs, daily-loss sit-out and 0:00 UTC reset (prop mode), weekend-flat (always), TP-arm / minimum-hold on exits, funds-based leg cap, duplicate-order protection, startup reconciliation. Risk code is deliberately separate from strategy code.

## Backtesting / paper trading

```bash
# dashboard → Backtest, or:
python -c "import backtest as b; r=b.run_backtest('ETHUSDT','1M','binance',per_level_usd=800.0,mode='scalp',compound=False,start_equity=10000.0,cost_bps=3.0,tp_arm_sec=150,session_rules=True); print(r['trades'],r['win_rate'],r['total'])"
```

Fills assume touch — live lands ~5–15% worse. 15 m bars can't resolve the 150 s hold rule; verify holds on live fills. Past replay ≠ future results.

## API keys

- Create Binance API keys with **trading + futures enabled, withdrawals DISABLED**, IP-restricted where possible.
- Prefer testnet (demo) keys while learning.
- Keys live in `data/hybrid_gb.db` or `.env` — both git-ignored, never committed, never transmitted anywhere except Binance's own API.

## Limitations

- Backtest granularity (15 m) and touch-fill assumption overstate precision.
- Binance and MT5 executions differ materially; results don't transfer 1:1.
- AI screening depends on your OpenRouter key/model and degrades to math-only on outage or 429s.
- Free Cloudflare-style tunnels (if you expose the dashboard) rotate and can drop.
- Single-user design: one operator per installation; no multi-tenancy, no hosted service.

## Security model

- Single operator, localhost-trusted. Bind `API_HOST=127.0.0.1` (default).
- Set `ADMIN_PASSWORD` if you ever expose the port beyond localhost.
- No telemetry, no cloud calls except: Binance API, your MT5 terminal, OpenRouter (only when you configure a key).

## License

MIT — see [LICENSE](LICENSE).

## ☕ Support the Project

If you find this project useful for learning, experimentation, or research, you can optionally support its continued development.

[![Buy Me a Coffee](https://img.buymeacoffee.com/button-api/?text=Buy%20Me%20a%20Coffee&emoji=%E2%98%95&slug=gurumetta2q&button_colour=FFDD00&font_colour=000000&font_family=Cookie&outline_colour=000000&coffee_colour=ffffff)](https://buymeacoffee.com/gurumetta2q)

Your support helps fund development, testing, documentation, and future open-source improvements.
