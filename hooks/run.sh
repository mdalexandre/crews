#!/bin/sh
# Hook launcher. Usage: sh run.sh <script.py> [args...]  (script is resolved next to this file).
# Runs the hook with python3 (3.9 or newer) when present, else with `uv run --no-project`, else exits 0
# without doing anything (fail open: a missing interpreter must never block the user's tool call).
# Stdin and the hook's exit code pass through unchanged.
dir=$(cd "$(dirname "$0")" && pwd) || exit 0
script=$1
[ -n "$script" ] || exit 0
shift
if command -v python3 >/dev/null 2>&1 \
   && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' </dev/null >/dev/null 2>&1; then
  exec python3 "$dir/$script" "$@"
fi
if command -v uv >/dev/null 2>&1; then
  exec uv run --no-project --quiet python "$dir/$script" "$@"
fi
exit 0
