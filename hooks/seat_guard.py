"""PreToolUse entry for the crews seat guard. Reads the hook JSON from stdin, asks crews.guard.decide, and
exits 2 with the reason on stderr to block, 0 to allow. Any malformed input or error fails open (exit 0).

Plugin option: seat_guard_mode (block or advise). Claude Code exposes a userConfig option to hooks as
CLAUDE_PLUGIN_OPTION_SEAT_GUARD_MODE (the lowercase key form is accepted too) and this entry maps it onto
CREWS_SEAT_GUARD_MODE, which crews.guard reads. An explicit CREWS_SEAT_GUARD_MODE in the environment wins.
Kill switches named in every block message: CREWS_SEAT_GUARD=0, the seat_guard_mode option, or disabling the
crews plugin.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_HINT = ("In the crews plugin, set the seat_guard_mode option to advise to warn instead of blocking, "
               "or disable the crews plugin.")


def _package_roots() -> list[str]:
    roots: list[str] = []
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        roots.append(plugin_root)
    roots.append(str(Path(__file__).resolve().parent.parent))
    return roots


def _map_option() -> None:
    if os.environ.get("CREWS_SEAT_GUARD_MODE"):
        return
    for key in ("CLAUDE_PLUGIN_OPTION_SEAT_GUARD_MODE", "claude_plugin_option_seat_guard_mode"):
        value = os.environ.get(key, "").strip().lower()
        if value:
            os.environ["CREWS_SEAT_GUARD_MODE"] = value
            return


def main() -> int:
    try:
        for root in reversed(_package_roots()):
            if root not in sys.path:
                sys.path.insert(0, root)
        _map_option()
        from crews import guard, paths  # noqa: PLC0415

        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            return 0
        decision = guard.decide(str(payload.get("tool_name") or payload.get("tool") or ""),
                                payload.get("tool_input"), paths.crews_home(), datetime.now(timezone.utc))
        if not decision.allow:
            print(f"{decision.reason}\n{PLUGIN_HINT}", file=sys.stderr)
            return 2
        if decision.warning:
            print(decision.warning, file=sys.stderr)
        return 0
    except Exception:  # noqa: BLE001  a guard defect must never strand a session
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
