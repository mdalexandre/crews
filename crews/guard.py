"""The seat guard decision (OSS-FIX-1): a pure function, no I/O beyond reading the plan index.

Interface
    decide(tool_name, tool_input, crews_home, now, transcript_text=None) -> Decision
    Decision(allow, reason, warning)   allow False means block; reason names the cause and both switches

Rules, ported from the owner guard:
    a  a crew seat (crew-<kind>-<model>[-<effort>], also '<plugin>:crew-...') is admitted only when the plan
       index holds the same subagent_type, description and prompt digest recorded within 24 hours; a plan
       found in transcript_text is a secondary path, the index alone suffices
    b  a generic agent (general-purpose, claude, no subagent_type, or a catalog generic_names entry) is blocked
    c  any other named agent is allowed
Switches: CREWS_SEAT_GUARD=0 allows everything; CREWS_SEAT_GUARD_MODE=advise turns a block into an allow
carrying a warning.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from crews import planindex

SEAT_RE = re.compile(r"^(?:[A-Za-z0-9_.-]+:)?crew-[a-z]+-[a-z]+(?:-[a-z]+)?$")
BUILTIN_GENERIC = frozenset({"", "general-purpose", "claude"})
SWITCHES = ("set CREWS_SEAT_GUARD=0 to disable this guard, or CREWS_SEAT_GUARD_MODE=advise to warn "
            "instead of blocking")


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str = ""
    warning: str | None = None


def _generic_names() -> frozenset[str]:
    try:
        from crews import catalog  # noqa: PLC0415

        return frozenset(str(g).lower() for g in catalog.load()["generic_names"])
    except Exception:  # noqa: BLE001  an unreadable catalog leaves the built in generic names
        return frozenset()


def _block(reason: str) -> Decision:
    if os.environ.get("CREWS_SEAT_GUARD_MODE", "").strip().lower() == "advise":
        return Decision(True, "", f"crews-seat-guard (advise mode, would block): {reason}")
    return Decision(False, reason)


def decide(tool_name: str, tool_input: Any, crews_home: Path, now: datetime,
           transcript_text: str | None = None) -> Decision:
    if os.environ.get("CREWS_SEAT_GUARD") == "0":
        return Decision(True)
    if tool_name not in ("Agent", "Task") or not isinstance(tool_input, dict):
        return Decision(True)
    seat = str(tool_input.get("subagent_type") or "")
    desc = str(tool_input.get("description") or "")
    bare = seat.rsplit(":", 1)[-1]
    if seat.lower() in BUILTIN_GENERIC or bare.lower() in BUILTIN_GENERIC or \
            seat.lower() in _generic_names() or bare.lower() in _generic_names():
        name = seat or "an Agent call with no subagent_type (general-purpose)"
        return _block(f"crews-seat-guard: {name} is a generic agent. Delegation goes through claude-crews: "
                      f"call crew_plan with task specific roles and dispatch exactly the Agent calls it "
                      f"returns, or use a named non generic agent. To override, {SWITCHES}.")
    if not SEAT_RE.match(seat):
        return Decision(True)
    digest = planindex.prompt_sha256(str(tool_input.get("prompt") or ""))
    # OSS-PKG-5: both forms (crew-... and <prefix>crew-...) are seats; either is admitted only when the index
    # holds that exact subagent_type, so a bare dispatch of a prefixed plan (or the reverse) stays blocked
    if planindex.admitted(crews_home, seat, desc, digest, now):
        return Decision(True)
    if transcript_text and f'"subagent_type": "{seat}"' in transcript_text and \
            f'"description": {json.dumps(desc)}' in transcript_text:
        return Decision(True)
    return _block(f"crews-seat-guard: {seat} is a claude-crews seat. It may be dispatched only with the exact "
                  f"subagent_type, description and prompt a crew plan returned within the last 24 hours, and "
                  f"the plan index holds no such call for description {json.dumps(desc)}. Call crew_plan with "
                  f"task specific roles first and issue the calls it returns. To override, {SWITCHES}.")
