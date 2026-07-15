#!/bin/bash
# Advisory desk runner — NEVER trades (no order tools anywhere in this path).
# Usage: run_desk.sh {preopen|wrap|weekly|monitor}
#   preopen  ~08:00 ET  full desk note + game plan
#   monitor  ~09:25 ET  sleeps to the open, then the intraday alert loop
#   wrap     ~16:30 ET  after-close note + accountability review
#   weekly   Sun        deeper pass (also researches top scouted ideas)
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
MODE="${1:-preopen}"
LOG=bot/logs/desk.log
mkdir -p bot/logs bot/reports

log() { echo "[$(date '+%F %T')] [desk $MODE] $*" >> "$LOG"; }

set -a; source .env 2>/dev/null; set +a
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"
PY=.venv/bin/python

# Refresh bot/desk/positions.json from the LIVE account via a read-only relay
# (no order tools — see bot/desk/snapshot.py). It rotates the prior snapshot to
# positions_prev.json so the desk can diff it and report any trades you made. If
# the relay fails, health.check_positions flags the stale file so the desk warns
# instead of analyzing the wrong book.
snapshot_positions() {
  $PY bot/desk/snapshot.py >> "$LOG" 2>&1 \
    && log "positions snapshot refreshed" \
    || log "positions snapshot FAILED — desk will warn on a stale book"
}

case "$MODE" in
  monitor)
    $PY bot/trading_calendar.py --check-today || { log "not a trading day"; exit 0; }
    $PY bot/trading_calendar.py --sleep-until-open
    snapshot_positions
    log "starting intraday monitor"
    exec $PY bot/desk/monitor.py --llm --strategist
    ;;
  preopen|wrap)
    $PY bot/trading_calendar.py --check-today || { log "not a trading day"; exit 0; }
    snapshot_positions
    log "generating $MODE note"
    $PY bot/desk/desk.py --mode "$MODE" --llm >> "$LOG" 2>&1 \
      || { log "$MODE failed"; tail -20 "$LOG" | $PY bot/notify.py "Desk $MODE failed"; exit 1; }
    log "$MODE complete"
    ;;
  weekly)
    # runs Sunday — intentionally NOT gated on trading day
    snapshot_positions
    log "generating weekly deep dive"
    $PY bot/desk/desk.py --mode weekly --top 8 --llm >> "$LOG" 2>&1 \
      || { log "weekly failed"; tail -20 "$LOG" | $PY bot/notify.py "Desk weekly failed"; exit 1; }
    log "weekly complete"
    ;;
  watchdog)
    # on a trading day, alert if today's preopen note never completed ok
    $PY bot/trading_calendar.py --check-today || { log "not a trading day"; exit 0; }
    $PY bot/desk/health.py --watchdog --modes preopen >> "$LOG" 2>&1 \
      && log "watchdog OK" || log "watchdog raised (alerted)"
    ;;
  *)
    log "unknown mode '$MODE'"; exit 2 ;;
esac
