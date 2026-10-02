#!/usr/bin/env bash
# Clean-machine test (OSS-TST-3, OSS-POR-3, OSS-DOC-6). Builds an image with only Node, a pinned Claude Code, git
# and a pinned uv, then runs inside.sh in it as a non-root user with no credential, a 4 GB memory cap and a
# read-only `git archive` of the working tree (never a directory copy: gitignored caches carry local paths).
#   scripts/clean_machine/run.sh          writes scripts/clean_machine/last_run.log, exit 0 only on PASS
#   KEEP_IMAGE=1                          keep the image afterwards (default: remove it)
# The archive is the working tree, so uncommitted work is tested: a temporary index (add -A, which honours
# .gitignore and includes untracked files) is written to a tree object and archived; `git stash create` would
# miss untracked files. When the tree is clean this is HEAD's tree. No commit, ref or stash is created.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(dirname "$(dirname "$HERE")")
LOG=$HERE/last_run.log

# Heavy job gate: do not launch under memory pressure.
swap_total=$(awk '/SwapTotal/{print $2}' /proc/meminfo)
swap_free=$(awk '/SwapFree/{print $2}' /proc/meminfo)
full10=$(awk '/^full/{sub("avg10=","",$2); print $2}' /proc/pressure/memory)
if [ "$swap_total" -gt 0 ] && [ $((swap_free * 100 / swap_total)) -lt 25 ]; then
  echo "swap free under 25%: not launching" >&2
  exit 3
fi
if awk "BEGIN{exit !($full10 > 10)}"; then
  echo "memory full avg10 $full10 above 10: not launching" >&2
  exit 3
fi

WORK=$(mktemp -d)
TAG=crews-clean-machine:$(git -C "$REPO" rev-parse --short HEAD)
cleanup() {
  rm -r "$WORK"
  if [ "${KEEP_IMAGE:-0}" != 1 ]; then docker rmi -f "$TAG" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT
TMP_INDEX=$WORK/index
GIT_INDEX_FILE=$TMP_INDEX git -C "$REPO" read-tree HEAD
GIT_INDEX_FILE=$TMP_INDEX git -C "$REPO" add -A
TREE=$(GIT_INDEX_FILE=$TMP_INDEX git -C "$REPO" write-tree)
git -C "$REPO" archive --format=tar -o "$WORK/head.tar" "$TREE"

# Drop any line that could carry a secret before it reaches the log.
redact() {
  sed -E '/(sk-[A-Za-z0-9_-]{8,}|bearer |api[_-]?key|token|secret|passw(or)?d|authorization)/I s/.*/[line removed: possible secret pattern]/'
}

{
  echo "crews clean-machine run"
  echo "date_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "repo_head: $(git -C "$REPO" rev-parse HEAD) plus working tree $TREE ($(git -C "$REPO" status --short | wc -l) changed paths)"
  echo "host: $(uname -sr), $(docker --version | cut -d, -f1)"
  echo "container: --memory 4g, non-root user, no environment variables passed, no credential mounted"
  echo "----"
  docker build -q -t "$TAG" "$HERE" >/dev/null && echo "image built: $TAG"
  set +e
  timeout 1500 docker run --rm --memory 4g --memory-swap 4g --pids-limit 512 --cap-drop ALL \
    --security-opt no-new-privileges \
    -v "$WORK/head.tar:/in/head.tar:ro" -v "$HERE:/in/clean_machine:ro" \
    "$TAG" bash /in/clean_machine/inside.sh 2>&1
  echo "container_exit: $?"
} 2>&1 | redact | tee "$LOG"
grep -q '^RESULT_CLEAN_MACHINE PASS' "$LOG"
