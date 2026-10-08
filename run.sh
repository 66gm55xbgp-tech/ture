#!/usr/bin/env bash
# HybridGB Open — all-in-one server (backend + dashboard).
# Usage: ./run.sh  (then open http://127.0.0.1:9100)
set -uo pipefail
cd "$(dirname "$0")"
PORT="${API_PORT:-9100}"
if [ ! -d ".venv" ]; then
  echo "creating virtualenv…"
  python3 -m venv .venv || { echo "need python3 with venv"; exit 1; }
fi
. .venv/bin/activate
pip install -q -r requirements.txt
[ -f .env ] && set -a && . ./.env && set +a
echo "→ dashboard: http://127.0.0.1:$PORT"
echo "  1) open Settings, save Binance DEMO keys (testnet first!)"
echo "  2) optional: save your OpenRouter key + model for AI screening"
echo "  3) spawn a bot (scalp, comp OFF is closest to the validated baseline)"
exec python server.py
