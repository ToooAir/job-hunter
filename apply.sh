#!/usr/bin/env bash
# apply.sh — one-stop launcher for the Stage 2 submission session (host).
#
# The agent fills/submits through a real Chrome attached over CDP; this wrapper
# launches that Chrome with a dedicated debugging profile and runs the session
# subcommands so you don't have to remember the flags.
#
#   ./apply.sh chrome        launch the apply Chrome (debug port + own profile)
#   ./apply.sh prepare       fill forms, screenshot, leave tabs for you (default)
#   ./apply.sh submit        actually submit approved snapshots (3-5 stable runs first)
#   ./apply.sh watch         watch tabs and book human submissions automatically
#   ./apply.sh book <id>     manually book a submission watch couldn't attribute
#
# Progressive route: run `prepare` until it is boring, only then `submit`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"
PROFILE="${CHROME_APPLY_PROFILE:-$HOME/.chrome-apply-profile}"
PORT="${CHROME_DEBUG_PORT:-9222}"
CHROME="${CHROME_BIN:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"

usage() { awk 'NR>1 && /^#/{sub(/^# ?/,"");print;next} NR>1{exit}' "${BASH_SOURCE[0]}"; }

cmd="${1:-help}"
[ $# -gt 0 ] && shift || true

case "$cmd" in
  chrome)
    [ -x "$CHROME" ] || { echo "Chrome not found at: $CHROME (set CHROME_BIN)"; exit 1; }
    echo "啟動投遞 Chrome — port $PORT, profile $PROFILE"
    echo "(在這個視窗登入過的 ATS 帳號會持久保留;勿用日常瀏覽器)"
    exec "$CHROME" --remote-debugging-port="$PORT" --user-data-dir="$PROFILE"
    ;;
  prepare) exec "$PY" "$ROOT/apply_session.py" "$@" ;;
  submit)  exec "$PY" "$ROOT/apply_session.py" --submit "$@" ;;
  watch)   exec "$PY" "$ROOT/apply_session.py" --watch "$@" ;;
  book)
    [ $# -ge 1 ] || { echo "用法:./apply.sh book <SNAPSHOT_ID>"; exit 1; }
    exec "$PY" "$ROOT/apply_session.py" --book "$@" ;;
  help|-h|--help) usage ;;
  *) echo "未知指令:$cmd"; echo; usage; exit 1 ;;
esac
