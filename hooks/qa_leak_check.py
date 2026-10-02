"""PreToolUse (Agent|Task) advisory: warn when a crews check seat dispatch carries an author verdict.

A check seat must be blind: its prompt carries the artifact paths and criteria, never the producer's
conclusion. When the dispatched subagent_type is a check seat and the prompt matches the brief leak regex of
crews.briefcheck, print a warning to stderr. Advisory only: this hook never exits 2 and fails open on any
malformed input or error.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

CHECK_SEAT_RE = re.compile(r"^(?:[A-Za-z0-9_.-]+:)?crew-check-")


def main() -> int:
    try:
        roots = [os.environ.get("CLAUDE_PLUGIN_ROOT") or "", str(Path(__file__).resolve().parent.parent)]
        for root in reversed([r for r in roots if r]):
            if root not in sys.path:
                sys.path.insert(0, root)
        from crews import briefcheck  # noqa: PLC0415

        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict) or payload.get("tool_name") not in ("Agent", "Task"):
            return 0
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return 0
        if not CHECK_SEAT_RE.match(str(tool_input.get("subagent_type") or "")):
            return 0
        hits = briefcheck.regex_hits(str(tool_input.get("prompt") or ""))
        if hits:
            print("crews-qa-leak-check (advisory): this check seat prompt appears to carry the author's "
                  f"conclusion ({'; '.join(hits[:3])}). A check must be blind: pass only the artifact paths "
                  "and the criteria, then re-plan with crew_plan if the brief leaked.", file=sys.stderr)
        return 0
    except Exception:  # noqa: BLE001
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
