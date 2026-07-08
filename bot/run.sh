#!/bin/bash
# Trading bot pipeline: analyze (python) -> execute (headless claude) -> reconcile (python).
# Invoked by launchd with one arg: morning | afternoon
set -uo pipefail
ROOT=/Users/rickyhan/trading_agent
cd "$ROOT"
SLOT="${1:-morning}"
TS=$(date +%Y%m%d_%H%M)
RUN_DIR="bot/runs/$TS"
LOG=bot/logs/bot.log
mkdir -p bot/logs bot/runs

log() { echo "[$(date '+%F %T')] [$SLOT] $*" >> "$LOG"; }

# 1. overlap prevention (atomic mkdir lock; stale >3h auto-cleared)
if ! mkdir bot/.lock 2>/dev/null; then
  if find bot/.lock -maxdepth 0 -mmin +180 | grep -q .; then
    rm -rf bot/.lock && mkdir bot/.lock || { log "lock contention"; exit 0; }
  else
    log "another run in progress, exiting"; exit 0
  fi
fi
trap 'rm -rf bot/.lock' EXIT

# 2. env
set -a; source .env; set +a
export PATH="/Users/rickyhan/.local/bin:/usr/local/bin:$PATH"
PY=.venv/bin/python

# 3. gates
[ -f bot/logs/ALERT ] && { log "ALERT present, skipping run"; exit 0; }
$PY bot/trading_calendar.py --check-today || { log "not a trading day"; exit 0; }
$PY bot/trading_calendar.py --too-late && { log "past cutoff, skipping"; exit 0; }

# 4. analysis (no MCP, pure python)
mkdir -p "$RUN_DIR"
log "analysis starting -> $RUN_DIR"
$PY bot/analyze.py --slot "$SLOT" --out "$RUN_DIR/decisions.json" >> "$LOG" 2>&1 \
  || { log "analysis FAILED, no trading this run"; tail -30 "$LOG" | $PY bot/notify.py "Analysis run failed ($SLOT $TS) — no trading this run"; exit 1; }

# 5. wait for execution window (morning runs that finish before 9:32)
$PY bot/trading_calendar.py --sleep-until-open
$PY bot/trading_calendar.py --too-late && { log "finished analysis past cutoff"; exit 0; }

# 6. execution via headless claude (paper mode lacks order tools entirely)
RO_TOOLS='mcp__robinhood-trading__get_accounts,mcp__robinhood-trading__get_portfolio,mcp__robinhood-trading__get_equity_positions,mcp__robinhood-trading__get_equity_quotes,mcp__robinhood-trading__get_equity_orders,Read,Write,Bash(python3 bot/*),Bash(.venv/bin/python bot/*)'
TRADE_TOOLS="$RO_TOOLS,mcp__robinhood-trading__review_equity_order,mcp__robinhood-trading__place_equity_order"

if [ "${PAPER_MODE:-1}" = "1" ]; then
  PROMPT_FILE=bot/prompts/execute_paper.md; TOOLS="$RO_TOOLS"; log "executing (PAPER)"
else
  PROMPT_FILE=bot/prompts/execute_trades.md; TOOLS="$TRADE_TOOLS"; log "executing (LIVE)"
fi

claude -p "$(sed -e "s|{{RUN_DIR}}|$RUN_DIR|g" -e "s|{{ACCOUNT}}|${BOT_ACCOUNT:-}|g" "$PROMPT_FILE")" \
  --allowedTools "$TOOLS" \
  --output-format json \
  > "bot/logs/exec_$TS.json" 2>&1
CLAUDE_RC=$?
log "executor finished rc=$CLAUDE_RC"

# 7. reconcile + report
$PY bot/reconcile.py --run-dir "$RUN_DIR" --claude-rc "$CLAUDE_RC" >> "$LOG" 2>&1

# 8. alert email (deduped: only when ALERT is newer than last notification)
if [ -f bot/logs/ALERT ] && [ bot/logs/ALERT -nt bot/logs/.notified ]; then
  cat bot/logs/ALERT | $PY bot/notify.py "TRADING HALTED — ALERT raised, action needed" >> "$LOG" 2>&1
  touch bot/logs/.notified
fi
log "run complete"
