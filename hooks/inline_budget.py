"""Opt-in inline execution budget.

Registered three times in hooks.json with a mode argument:
    prompt   UserPromptSubmit: reset this session's per-turn state
    post     PostToolUse: count execution calls, and note a crew_plan call or a crews seat dispatch
    pre      PreToolUse (Bash): deny the call (exit 2) once the count has reached the budget

It does nothing unless the plugin option inline_budget_enabled is true (CLAUDE_PLUGIN_OPTION_INLINE_BUDGET_ENABLED,
lowercase key form also accepted). The budget is the option inline_budget (default 5). Counted calls per user
turn: Read, Edit, Write, MultiEdit, NotebookEdit, Bash, Grep, Glob. A crew_plan tool call (tool name ending in
crew_plan) or a crew seat dispatch (subagent_type crew- or crews:crew-) lifts the budget for the rest of the turn.

State lives under CLAUDE_PLUGIN_DATA (fallback CREWS_HOME, then ~/.crews) in inline_budget/<session>.json.
Never acts inside a subagent. Fails open (exit 0) on malformed input, unreadable state or any exception.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

EXEC_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "Grep", "Glob"})
SEAT_PREFIX = re.compile(r"^(?:[A-Za-z0-9_.-]+:)?crew-")
DEFAULT_BUDGET = 5

DENY = (
    "crews inline budget: BLOCKED. {count} execution calls have already run inline this turn and the budget "
    "is {budget}. Either allocate the remaining work: call the crew_plan tool with task specific roles and "
    "dispatch the Agent calls it returns (that lifts the budget for the rest of this turn), or turn this "
    "guard off by setting the crews plugin option inline_budget_enabled to false."
)


def _option(name: str) -> str:
    for key in (f"CLAUDE_PLUGIN_OPTION_{name.upper()}", f"claude_plugin_option_{name.lower()}"):
        value = os.environ.get(key)
        if value is not None and value.strip():
            return value.strip()
    return ""


def _enabled() -> bool:
    return _option("inline_budget_enabled").lower() in ("true", "1", "yes", "on")


def _budget() -> int:
    try:
        return max(0, int(_option("inline_budget")))
    except ValueError:
        return DEFAULT_BUDGET


def _state_path(session_id: str) -> Path:
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or os.environ.get("CREWS_HOME") or str(Path.home() / ".crews")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:100] or "unknown"
    return Path(base) / "inline_budget" / f"{safe}.json"


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ib-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _run(mode: str, payload: dict[str, Any]) -> int:
    if not _enabled():
        return 0
    agent_id = payload.get("agent_id")
    if isinstance(agent_id, str) and agent_id:
        return 0
    session_id = payload.get("session_id")
    path = _state_path(session_id if isinstance(session_id, str) else "unknown")
    if mode == "prompt":
        _save(path, {"count": 0, "allocated": False})
        return 0
    state = _load(path)
    tool = str(payload.get("tool_name") or "")
    if mode == "post":
        if tool in EXEC_TOOLS:
            state["count"] = int(state.get("count", 0)) + 1
        elif tool.endswith("crew_plan"):
            state["allocated"] = True
        elif tool in ("Agent", "Task"):
            tool_input = payload.get("tool_input")
            seat = str(tool_input.get("subagent_type") or "") if isinstance(tool_input, dict) else ""
            if SEAT_PREFIX.match(seat):
                state["allocated"] = True
        else:
            return 0
        _save(path, state)
        return 0
    if mode == "pre" and tool == "Bash":
        count, budget = int(state.get("count", 0)), _budget()
        if count >= budget and not state.get("allocated"):
            print(DENY.format(count=count, budget=budget), file=sys.stderr)
            return 2
    return 0


def main() -> int:
    try:
        mode = sys.argv[1] if len(sys.argv) > 1 else ""
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict) or mode not in ("prompt", "post", "pre"):
            return 0
        return _run(mode, payload)
    except Exception:  # noqa: BLE001  a guard defect must never strand a session
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
