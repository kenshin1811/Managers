#!/usr/bin/env bash
# Run the real thing, from nothing, in one command.
#
#   ./demo.sh            the kitchen where cover has to be called in
#   ./demo.sh on_shift   the kitchen where somebody at work can take it
#   PORT=9000 ./demo.sh  somewhere other than 8000
#
# Starts the service, loads a demo kitchen timed to right now, sets the engine
# off, and opens the dashboard. Nothing leaves the process: DRY_RUN is the
# default, so the messages are logged rather than sent.

set -euo pipefail

VARIANT="${1:-call_in}"
PORT="${PORT:-8000}"
HOST="127.0.0.1"
URL="http://${HOST}:${PORT}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

say() { printf '\033[1m%s\033[0m %s\n' "→" "$*"; }
die() { printf '\033[31m%s\033[0m %s\n' "✗" "$*" >&2; exit 1; }

case "$VARIANT" in
  call_in|on_shift) ;;
  *) die "Unknown scenario '$VARIANT'. Use call_in or on_shift." ;;
esac

# --- python -----------------------------------------------------------------
PY=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$candidate"; break
  fi
done
[ -n "$PY" ] || die "Python 3.11 or newer is required; none found on PATH."

if [ ! -x .venv/bin/python ]; then
  say "Creating .venv with $($PY --version)"
  "$PY" -m venv .venv
fi
VENV=".venv/bin"

if [ ! -x "$VENV/uvicorn" ] || [ pyproject.toml -nt .venv/pyvenv.cfg ]; then
  say "Installing dependencies"
  "$VENV/pip" install --quiet --upgrade pip
  "$VENV/pip" install --quiet -e ".[dev]"
  touch .venv/pyvenv.cfg
fi

# --- a clean slate ----------------------------------------------------------
rm -f managers.db managers.db-wal managers.db-shm

# --- the service ------------------------------------------------------------
LOG="$(mktemp -t managers-demo.XXXXXX)"
say "Starting the service on $URL"
"$VENV/uvicorn" app.main:app --host "$HOST" --port "$PORT" >"$LOG" 2>&1 &
SERVER_PID=$!

cleanup() {
  printf '\n'
  say "Stopping"
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
  rm -f "$LOG"
}
trap cleanup EXIT INT TERM

for _ in $(seq 1 60); do
  if curl -fsS "$URL/health" >/dev/null 2>&1; then break; fi
  kill -0 "$SERVER_PID" 2>/dev/null || { cat "$LOG"; die "The service failed to start."; }
  sleep 0.5
done
curl -fsS "$URL/health" >/dev/null 2>&1 || { cat "$LOG"; die "The service never answered /health."; }

# --- something to look at ---------------------------------------------------
say "Loading the demo kitchen ($VARIANT), timed to right now"
curl -fsS -X POST "$URL/api/demo/reset?variant=$VARIANT" >/dev/null
say "Mai requests leave — the engine takes it from here"
curl -fsS -X POST "$URL/api/demo/leave" >/dev/null

if command -v open >/dev/null 2>&1; then open "$URL" >/dev/null 2>&1 || true
elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true
fi

cat <<BANNER

  Dashboard   $URL
  API docs    $URL/docs
  Messages    logged below (DRY_RUN is on, nothing is sent)

  Press Ctrl+C to stop.

BANNER

tail -f "$LOG" &
TAIL_PID=$!
trap 'kill "$TAIL_PID" 2>/dev/null || true; cleanup' EXIT INT TERM
wait "$SERVER_PID"
