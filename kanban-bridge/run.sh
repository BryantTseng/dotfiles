#!/usr/bin/env bash
# Runs the kanban bridge and keeps it running:
#   - bridge.py / providers/*.py changed -> finishes running jobs, exits 3, restarts
#   - bridge crashed                     -> restarts after 5 seconds
#   - Ctrl-C once                        -> running jobs finish, then stops cleanly
#   - Ctrl-C twice                       -> running jobs interrupted (marked on cards), then stops

set -a; . ~/.config/kanban-bridge/env; set +a
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")"

# The bridge handles Ctrl-C itself; this shell must survive it to read the exit code.
trap 'true' INT TERM

while true; do
  uv run bridge.py
  code=$?
  case $code in
    0) echo "bridge stopped."; exit 0 ;;
    3) echo "source changed: restarting…"; set -a; . ~/.config/kanban-bridge/env; set +a ;;
    *) echo "bridge exited with code $code; restarting in 5s (Ctrl-C to quit)…"
       sleep 5 || exit 0 ;;
  esac
done
