#!/bin/bash
# Install (or remove) the Vibe Trading launchd schedule on THIS machine.
#
# Renders the templates in bot/launchd/templates/ with your repo path and a
# label prefix, writes them to ~/Library/LaunchAgents/, and loads them.
#
# Usage:
#   bot/launchd/install_schedule.sh                 # install the ADVISORY DESK jobs (safe: never trades)
#   bot/launchd/install_schedule.sh --with-autotrader   # ALSO install the auto-trader jobs (places real
#                                                       # orders when PAPER_MODE=0 — read the README first!)
#   bot/launchd/install_schedule.sh --dry-run       # print what would be installed, change nothing
#   bot/launchd/install_schedule.sh --uninstall     # unload + remove ONLY jobs this script installed
#   bot/launchd/install_schedule.sh --prefix com.me # custom label prefix (default: com.$USER.vibetrading)
#
# NOTE: launchd fires on your machine's LOCAL clock. The shipped times assume
# US-Eastern (market time): desk preopen 08:00, monitor 09:25, wrap 16:30,
# weekly Sun 10:00, watchdog 09:15. If your machine isn't on ET, edit the
# StartCalendarInterval in the rendered plists (or the templates) accordingly.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
TEMPLATES="$HERE/templates"
DEST="$HOME/Library/LaunchAgents"
PREFIX="com.${USER}.vibetrading"
WITH_BOT=0
DRY=0
UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --with-autotrader) WITH_BOT=1 ;;
    --dry-run)         DRY=1 ;;
    --uninstall)       UNINSTALL=1 ;;
    --prefix)          PREFIX="$2"; shift ;;
    -h|--help)         grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $1 (see --help)"; exit 2 ;;
  esac
  shift
done

DESK_JOBS=(desk.preopen desk.monitor desk.wrap desk.weekly desk.watchdog)
BOT_JOBS=(tradingbot.daemon tradingbot.core tradingbot.reflect tradingbot.morning tradingbot.afternoon)
JOBS=("${DESK_JOBS[@]}")
[ "$WITH_BOT" = 1 ] && JOBS+=("${BOT_JOBS[@]}")

if [ "$UNINSTALL" = 1 ]; then
  # remove desk AND autotrader jobs under this prefix (only ours — nothing else)
  for j in "${DESK_JOBS[@]}" "${BOT_JOBS[@]}"; do
    p="$DEST/$PREFIX.$j.plist"
    if [ -f "$p" ]; then
      [ "$DRY" = 1 ] && { echo "would remove: $p"; continue; }
      launchctl unload "$p" 2>/dev/null || true
      rm -f "$p"
      echo "removed: $PREFIX.$j"
    fi
  done
  exit 0
fi

echo "repo:   $ROOT"
echo "prefix: $PREFIX"
[ "$WITH_BOT" = 1 ] && echo "WARNING: installing the AUTO-TRADER jobs too — it will place real orders when PAPER_MODE=0."

mkdir -p "$ROOT/bot/logs"
for j in "${JOBS[@]}"; do
  tmpl="$TEMPLATES/$j.plist.template"
  [ -f "$tmpl" ] || { echo "missing template: $tmpl"; exit 1; }
  out="$DEST/$PREFIX.$j.plist"
  if [ "$DRY" = 1 ]; then
    echo "--- would install: $out"
    sed -e "s|{{ROOT}}|$ROOT|g" -e "s|{{PREFIX}}|$PREFIX|g" "$tmpl" | grep -E "Label|run_|\.py|Hour|Minute" | head -6
    continue
  fi
  mkdir -p "$DEST"
  sed -e "s|{{ROOT}}|$ROOT|g" -e "s|{{PREFIX}}|$PREFIX|g" "$tmpl" > "$out"
  launchctl unload "$out" 2>/dev/null || true
  launchctl load -w "$out"
  echo "installed + loaded: $PREFIX.$j"
done

[ "$DRY" = 1 ] || echo "done. verify with: launchctl list | grep $PREFIX"
