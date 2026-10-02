#!/usr/bin/env bash
# Runs INSIDE the clean-machine container as the non-root user (started by run.sh). Not meant to be run on a
# developer machine: it installs and uninstalls a Claude Code plugin in $HOME.
#   /in/head.tar         read-only `git archive` of the repo working tree (see run.sh)
#   /in/clean_machine/   read-only copy of scripts/clean_machine (this script and mcp_stdio_check.py)
# Every phase prints a PHASE line; every observation a CHECK or FACT line. The last line is RESULT_CLEAN_MACHINE.
set -u
FAIL=()
fail() { FAIL+=("$1"); echo "CHECK FAIL $1: $2"; }
pass() { echo "CHECK PASS $1: $2"; }
phase() { echo; echo "PHASE $*"; }

# crew-*.md anywhere under $HOME except the test's own checkout of the repo (the user's clone is not plugin state)
seat_files() { find "$HOME" -name 'crew-*.md' -not -path "$HOME/clone/*" "$@" 2>/dev/null; }

phase "0 environment"
echo "FACT claude $(claude --version 2>&1 | head -1)"
echo "FACT uv $(uv --version 2>&1 | head -1)"
echo "FACT node $(node --version)"
echo "FACT user $(id -un) uid=$(id -u) home=$HOME"
if command -v python3 >/dev/null 2>&1; then echo "FACT python3 on PATH: $(command -v python3)"; else echo "FACT python3 on PATH: none (only what uv fetches)"; fi
echo "FACT credential-looking env vars: $(env | cut -d= -f1 | grep -ciE 'api_key|token|secret|password|credential')"
echo "FACT claude login state: none (no credential of any kind in the container)"

phase "1 clone from the git archive"
CLONE=$HOME/clone
mkdir -p "$CLONE" && tar xf /in/head.tar -C "$CLONE" || { echo "RESULT_CLEAN_MACHINE FAIL archive"; exit 1; }
git -C "$CLONE" init -q && git -C "$CLONE" add -A \
  && git -C "$CLONE" -c user.name=clean -c user.email=clean@example.invalid commit -qm "git archive of HEAD" \
  && pass clone "$(git -C "$CLONE" ls-files | wc -l) tracked files, no gitignored caches (git archive)"
if [ -e "$CLONE/.venv" ] || [ -e "$CLONE/__pycache__" ]; then fail clone_has_no_local_caches "found .venv or __pycache__"; fi

phase "2 install from the clone"
claude plugin marketplace add "$CLONE" 2>&1 | tail -3
claude plugin install crews@crews 2>&1 | tail -3
LISTED=$(claude plugin list 2>&1)
echo "$LISTED"
if echo "$LISTED" | grep -q "crews@crews" && echo "$LISTED" | grep -q "enabled"; then
  pass plugin_installed "crews@crews enabled"
else
  fail plugin_installed "crews@crews not listed as enabled"
fi
ROOT=$(node -e 'const p=JSON.parse(require("fs").readFileSync(process.env.HOME+"/.claude/plugins/installed_plugins.json"));console.log(p.plugins["crews@crews"][0].installPath)')
echo "FACT installed plugin dir: $ROOT"
if [ -f "$ROOT/.mcp.json" ] && [ -f "$ROOT/hooks/seat_guard.py" ]; then
  pass installed_copy "found .mcp.json and hooks/seat_guard.py"
else
  fail installed_copy "installed copy lacks .mcp.json or hooks/seat_guard.py"
fi
DATA=$HOME/.claude/plugins/data/crews-crews
echo "FACT plugin data dir (plugin@marketplace id): $DATA"

phase "3 Claude Code launches the declared server itself (claude mcp list, no login)"
( cd "$HOME" && timeout 300 claude mcp list 2>&1 | tail -4 ) | tee /tmp/mcp_list.txt
if grep -q "Connected" /tmp/mcp_list.txt; then pass claude_mcp_list "server Connected"; else fail claude_mcp_list "server not Connected"; fi
if [ -d "$DATA" ]; then pass data_dir_created_by_claude "$(ls "$DATA" | tr '\n' ' ')"; else mkdir -p "$DATA"; fi

phase "4 stdio: crew_catalog, crew_plan, guard (installed copy, manifest command, uv only)"
UV_PROJECT_ENVIRONMENT="$DATA/venv" timeout 600 uv run --project "$ROOT" --frozen python /in/clean_machine/mcp_stdio_check.py \
  "$ROOT" "$DATA" "$ROOT/tests/fixtures/roles/caption_listings.json" "$ROOT/tests/fixtures/live_table.json" 2>&1 | tail -25
if [ "${PIPESTATUS[0]}" = 0 ]; then pass stdio_driver "all driver checks passed"; else fail stdio_driver "driver reported a failure"; fi

phase "5 uninstall"
claude plugin uninstall crews@crews 2>&1 | tail -2
claude plugin marketplace remove crews 2>&1 | tail -2
if claude plugin list 2>&1 | grep -q "crews@crews"; then fail uninstalled "crews@crews still listed"; else pass uninstalled "crews@crews not listed"; fi
if [ -e "$DATA" ]; then fail data_dir_removed "$DATA remains: $(ls "$DATA" 2>&1 | tr '\n' ' ')"; else pass data_dir_removed "$DATA is gone"; fi
if [ -e "$HOME/.crews" ]; then fail no_home_crews "~/.crews was created"; else pass no_home_crews "~/.crews never created"; fi
if ls "$HOME/.claude/agents" 2>/dev/null | grep -q '^crew'; then fail no_user_agents "seat files in ~/.claude/agents"; else pass no_user_agents "no seat files in ~/.claude/agents"; fi
echo "FACT crew-*.md count right after uninstall (outside the checkout): $(seat_files | wc -l)"
seat_files | head -2 | sed 's/^/FACT example leftover: /'
echo "FACT orphan markers: $(find "$HOME/.claude/plugins/cache" -maxdepth 4 -name .orphaned_at 2>/dev/null | tr '\n' ' ')"
LEFT=$(seat_files -not -path "$HOME/.claude/plugins/cache/crews/*" | wc -l)
if [ "$LEFT" = 0 ]; then
  pass no_seat_files_outside_orphaned_cache "0 outside the orphaned plugin cache"
else
  fail no_seat_files_outside_orphaned_cache "$LEFT found"
fi

phase "6 documented removal of the orphaned cache (OSS-DOC-6 instruction)"
rm -rf ~/.claude/plugins/cache/crews
FINAL=$(find "$HOME/.claude" -name 'crew-*.md' 2>/dev/null | wc -l)
if [ "$FINAL" = 0 ] && [ ! -e "$DATA" ]; then
  pass nothing_left "0 crew-*.md anywhere under ~/.claude, no data dir"
else
  fail nothing_left "$FINAL crew-*.md remain or data dir exists"
fi

echo
if [ "${#FAIL[@]}" = 0 ]; then echo "RESULT_CLEAN_MACHINE PASS"; else echo "RESULT_CLEAN_MACHINE FAIL ${FAIL[*]}"; fi
[ "${#FAIL[@]}" = 0 ]
