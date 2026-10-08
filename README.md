# HybridGB Open

Open-source auto-grid trading system for **Binance** (USDT-M futures) & **MT5** — self-hosted bot with AI screening, prop-style risk governor, and built-in dashboard. Bring your own keys.

> Independent open-source software for **learning, experimentation, research, and educational purposes only**. Not financial advice. Start on testnet/demo.

## Disclaimer / Important Notice

- This project is provided for learning, experimentation, research, and educational purposes only.
- It is **not financial, investment, trading, or professional advice** of any kind.
- No profitability, performance, or financial return is guaranteed or implied.
- You are solely responsible for your own trading decisions, configuration, risk management, capital, and compliance with applicable laws, regulations, broker/exchange rules, and terms of service.
- This project does not provide managed trading, investment management, custody, or personalized investment recommendations.
- The software is **self-hosted and single-user per installation**: you clone or fork the MIT-licensed source and run your own instance on infrastructure you control.
- You connect **your own Binance account using your own API credentials**. Keys and funds stay under your control — never share them with the project author. The author does not receive, control, or custody user funds or credentials, and does not execute trades on anyone's behalf.
- Use appropriately restricted API keys. **Never enable withdrawal permissions** on keys used by any trading bot.
- Automated trading involves substantial risk, including possible loss of capital. Test with paper trading / testnet and understand the strategy before using real funds.
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

- **Auto-grids** on ETH, BTC, XAU (neutral quant grids: ladder in, take profit on reversion).
- **AI screening** via your own [OpenRouter](https://openrouter.ai/keys) key (entry veto, unwind votes — optional; math-only mode works without it).
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

MT5: attach `ea/HybridGB_EA.mq5` to a chart, generate a token in Settings ▸ MT5, paste token + server URL into the EA inputs, allow WebRequest to your server URL.

## Layout

| Path | What |
|---|---|
| `server.py` | All-in-one API + static dashboard |
| `guru_ai.py` | Live grid engine (Binance) |
| `mt5_guru.py` / `dynamic_guruai.py` | MT5 engine + AI hook |
| `neutral_grid.py` | Grid math core (frozen logic) |
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

## Security model

- Single operator, localhost-trusted. Bind `API_HOST=127.0.0.1` (default).
- Keys live in `data/hybrid_gb.db` or `.env` — both git-ignored, never committed.
- Set `ADMIN_PASSWORD` if you ever expose the port beyond localhost.
- No telemetry, no cloud calls except: Binance API, your MT5 terminal, OpenRouter (only when you configure a key).

## Validate before risking money

```bash
# dashboard → Backtest, or:
python -c "import backtest as b; r=b.run_backtest('ETHUSDT','1M','binance',per_level_usd=800.0,mode='scalp',compound=False,start_equity=10000.0,cost_bps=3.0,tp_arm_sec=150,session_rules=True); print(r['trades'],r['win_rate'],r['total'])"
```

Fills assume touch — live lands ~5–15% worse. Past replay ≠ future profit.

## License

MIT — see [LICENSE](LICENSE).
