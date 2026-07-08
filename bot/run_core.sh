#!/bin/bash
# Weekly Tier-3 CORE run: multi-agent research on the core watchlist -> core
# intents -> place during market hours. Runs once (not a daemon). Holds the
# shared bot/.lock so the intraday daemon defers its own placements meanwhile.
set -uo pipefail
ROOT=/Users/rickyhan/trading_agent
cd "$ROOT"
TS=$(date +%Y%m%d_%H%M)
RUN_DIR="bot/runs/core_$TS"
LOG=bot/logs/bot.log
mkdir -p bot/logs bot/runs

log() { echo "[$(date '+%F %T')] [core] $*" >> "$LOG"; }

# overlap lock (shared with run.sh; daemon checks this and defers)
if ! mkdir bot/.lock 2>/dev/null; then
  if find bot/.lock -maxdepth 0 -mmin +180 | grep -q .; then
    rm -rf bot/.lock && mkdir bot/.lock || { log "lock contention"; exit 0; }
  else
    log "another run in progress, exiting"; exit 0
  fi
fi
trap 'rm -rf bot/.lock' EXIT

set -a; source .env; set +a
export PATH="/Users/rickyhan/.local/bin:/usr/local/bin:$PATH"
PY=.venv/bin/python

# gates
[ -f bot/logs/ALERT ] && { log "ALERT present, skipping core run"; exit 0; }
$PY bot/trading_calendar.py --check-today || { log "not a trading day"; exit 0; }
$PY bot/trading_calendar.py --too-late && { log "past cutoff, skipping"; exit 0; }

# research (slow: multi-agent over the core watchlist)
mkdir -p "$RUN_DIR"
log "core research starting -> $RUN_DIR"
$PY bot/analyze.py --out "$RUN_DIR/decisions.json" >> "$LOG" 2>&1 \
  || { log "core research FAILED"; tail -30 "$LOG" | $PY bot/notify.py "Core research failed ($TS)"; exit 1; }

# must still be within the execution window to place
$PY bot/trading_calendar.py --too-late && { log "research finished past cutoff, not placing"; exit 0; }

# execution via the same relay run.sh uses
RO_TOOLS='mcp__robinhood-trading__get_accounts,mcp__robinhood-trading__get_portfolio,mcp__robinhood-trading__get_equity_positions,mcp__robinhood-trading__get_equity_quotes,mcp__robinhood-trading__get_equity_orders,Read,Write,Bash(python3 bot/*),Bash(.venv/bin/python bot/*)'
TRADE_TOOLS="$RO_TOOLS,mcp__robinhood-trading__review_equity_order,mcp__robinhood-trading__place_equity_order"
if [ "${PAPER_MODE:-1}" = "1" ]; then
  PROMPT_FILE=bot/prompts/execute_paper.md; TOOLS="$RO_TOOLS"; log "executing core (PAPER)"
else
  PROMPT_FILE=bot/prompts/execute_trades.md; TOOLS="$TRADE_TOOLS"; log "executing core (LIVE)"
fi

claude -p "$(sed -e "s|{{RUN_DIR}}|$RUN_DIR|g" -e "s|{{ACCOUNT}}|${BOT_ACCOUNT:-}|g" "$PROMPT_FILE")" \
  --allowedTools "$TOOLS" --output-format json \
  > "bot/logs/exec_core_$TS.json" 2>&1
CLAUDE_RC=$?
log "core executor finished rc=$CLAUDE_RC"

$PY bot/reconcile.py --run-dir "$RUN_DIR" --claude-rc "$CLAUDE_RC" >> "$LOG" 2>&1
log "core run complete"
