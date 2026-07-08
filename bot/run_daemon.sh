#!/bin/bash
# Intraday daemon launcher. Invoked by launchd near the open (09:25 ET).
# Gates like run.sh, waits for the open, then hands off to the long-running
# Python daemon which self-terminates near the close.
set -uo pipefail
ROOT=/Users/rickyhan/trading_agent
cd "$ROOT"
LOG=bot/logs/bot.log
mkdir -p bot/logs bot/runs

log() { echo "[$(date '+%F %T')] [daemon-launch] $*" >> "$LOG"; }

# env
set -a; source .env; set +a
export PATH="/Users/rickyhan/.local/bin:/usr/local/bin:$PATH"
PY=.venv/bin/python

# gates
[ -f bot/logs/ALERT ] && { log "ALERT present, not launching daemon"; exit 0; }
$PY bot/trading_calendar.py --check-today || { log "not a trading day"; exit 0; }
$PY bot/trading_calendar.py --too-late && { log "past cutoff, not launching"; exit 0; }

# wait for the regular-hours open, then run the daemon for the session
$PY bot/trading_calendar.py --sleep-until-open
log "launching intraday daemon"
exec $PY bot/intraday.py
