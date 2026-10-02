"""SessionStart hook: print one line telling the session that crews plans subagent crews.

Reads and discards the hook payload. Prints one self-contained line to stdout, which Claude Code adds to the
session context. Fails open: always exits 0.
"""

from __future__ import annotations

import sys

LINE = ("[crews] crews plans subagent crews: for work worth delegating, load the crew skill, call the "
        "crew_plan tool with task specific roles, and dispatch exactly the Agent calls it returns.")


def main() -> int:
    try:
        sys.stdin.read()
        print(LINE)
    except Exception:  # noqa: BLE001
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
