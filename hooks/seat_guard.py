"""PreToolUse entry for the crews seat guard. Reads the hook JSON from stdin, asks crews.guard.decide, and
exits 2 with the reason on stderr to block, 0 to allow. Any malformed input or error fails open (exit 0).
Not registered anywhere by this file; plugin wiring is a separate step."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    try:
        from crews import guard, paths  # noqa: PLC0415

        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            return 0
        decision = guard.decide(str(payload.get("tool_name") or payload.get("tool") or ""),
                                payload.get("tool_input"), paths.crews_home(), datetime.now(timezone.utc))
        if not decision.allow:
            print(decision.reason, file=sys.stderr)
            return 2
        if decision.warning:
            print(decision.warning, file=sys.stderr)
        return 0
    except Exception:  # noqa: BLE001  a guard defect must never strand a session
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
