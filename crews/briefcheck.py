"""C-BL, check brief leakage (SRD typesafe-integration-v1.0.md section 2.5, component C-BL).

Interface
    LEAK                 the seven leak alternatives below, joined with "|" and compiled
                          case insensitive.
    regex_hits(brief)     -> list[str]         the mechanical floor; runs first, needs no network
    request(brief)        -> dict[str, Any]    one "leak" Noul question over the brief text
    decide(hits, noul, threshold, calibrated=True) -> dict
                                                {"status": "LEAK"|"CLEAN"|"UNVERIFIED", "reason": str, ...}

A regex hit is always LEAK: TSI-BL-01, TSI-BL-02, and is decided by the mechanical floor alone; it never
consults `calibrated`, because it never trusted a TypeSafe threshold in the first place. A Noul result
never overrides a regex hit; it is consulted only once the mechanical floor is clean. `request` marks the
packet as carrying worker or generated text (`includes_worker_output`), because the brief text a check
role receives is itself generated from a base-session-authored role, per TSI-GATE-05.

This module makes no network call. Only crews.egress.gated_call may send `request`'s body to TypeSafe.
"""

from __future__ import annotations

import re
from typing import Any

MODEL = "jev-latest"
DATA_CATEGORY = "check_brief_leak"
INCLUDES_WORKER_OUTPUT = True

# CR-2, fixed join order for authored_text: only text a person wrote into the role, never the code owned
# rendered boilerplate (the output contract or the check protocol lines planner.py always appends).
_LIST_FIELDS = ("criteria", "authority", "inputs", "execution", "read_scope", "must_not", "blocked_when")

# The leak regex: self-contained in this package, with no import of or reference to any file outside it.
LEAK_ALTERNATIVES: list[str] = [
    r"\bconfirm (?:that |this |it )?(?:is|are|was|looks?) (?:correct|right|done|fixed|passing|good|fine)\b",
    r"\b(?:i|we) (?:already )?(?:verified|confirmed|checked|tested|validated) (?:that|this|it)\b",
    r"\bthe (?:answer|fix|change|edit|result|code|output) (?:is|are|was) (?:correct|right|done|complete|fixed)\b",
    r"\bplease just (?:confirm|sign[- ]?off|approve|rubber.?stamp)\b",
    r"\bmy (?:reasoning|analysis|conclusion|verdict) (?:was|is)\b",
    r"\bhere is (?:my|the author'?s) (?:reasoning|rationale|chain[- ]of[- ]thought|verdict)\b",
    r"\bverdict\s*[:=]\s*(?:pass|correct|approved|good)\b",
]
LEAK = re.compile("|".join(LEAK_ALTERNATIVES), re.I)


def authored_text(role: dict[str, Any], task: str) -> str:
    """CR-2: the text a person wrote into a check role, joined in a fixed order: task, mission, criteria,
    authority, inputs, execution, read_scope, must_not, blocked_when, then every string value inside
    professional_frame. Missing keys are skipped. Never includes rendered boilerplate (the output contract
    or the check protocol lines), because those are code owned and not authored, per CR-2 / TSI-BL-01."""
    parts: list[str] = [task]
    mission = role.get("mission")
    if isinstance(mission, str) and mission:
        parts.append(mission)
    for key in _LIST_FIELDS:
        value = role.get(key)
        if isinstance(value, list):
            parts.extend(str(v) for v in value if isinstance(v, str))
        elif isinstance(value, str) and value:
            parts.append(value)
    frame = role.get("professional_frame")
    if isinstance(frame, dict):
        for value in frame.values():
            if isinstance(value, str) and value:
                parts.append(value)
            elif isinstance(value, list):
                parts.extend(str(v) for v in value if isinstance(v, str))
    return "\n".join(parts)


def regex_hits(brief: str) -> list[str]:
    """The deterministic floor: every distinct matched phrase, or an empty list when the brief is clean."""
    return sorted({m.group(0).strip() for m in LEAK.finditer(brief)})


def request(brief: str) -> dict[str, Any]:
    """One 'leak' Noul question over the check brief text, marked as carrying generated brief content."""
    return {
        "model": MODEL,
        "state": {"brief": brief},
        "questions": {
            "leak": {
                "type": "noul",
                "instructions": (
                    "Does `brief` state, restate, or imply that its own conclusion, verdict, or finding is "
                    "already known, correct, or complete, rather than leaving that conclusion to be "
                    "established by the check itself?"
                ),
            }
        },
    }


def decide(hits: list[str], noul: float | None, threshold: float, *, calibrated: bool = True) -> dict[str, Any]:
    """TSI-BL-02: a Noul result never overrides a regex hit. With no hit, an unresolved Noul is UNVERIFIED,
    never CLEAN; a resolved Noul at or above `threshold` is LEAK, otherwise CLEAN.

    ``calibrated`` defaults to True so every existing caller keeps today's behavior byte for byte. JEV-17
    (mission SRD): an uncalibrated measurement must never produce an acting decision. A regex hit is
    mechanical, not a TypeSafe threshold decision, so it is always LEAK regardless of ``calibrated``, and
    a missing Noul with no hit is always UNVERIFIED regardless of ``calibrated`` (there is nothing to
    record). Only the case where a real Noul is compared against `threshold` to decide LEAK or CLEAN is
    gated: pass ``calibrated=False`` (the catalog's ``typesafe.brief_leak.calibrated`` flag) to record
    that comparison for later calibration without ever letting it set LEAK or CLEAN: ``status`` stays
    ``UNVERIFIED`` and ``acted`` is False, while ``observed_decision`` carries exactly what would have
    been decided had the threshold been trusted, so an operator reading the result can tell it only
    observed."""
    if hits:
        return {"status": "LEAK", "reason": f"regex floor hit: {'; '.join(hits[:5])}", "acted": True}
    if noul is None:
        return {"status": "UNVERIFIED", "reason": "no regex hit and no semantic leak judgment", "acted": True}
    if noul >= threshold:
        observed = {"status": "LEAK", "reason": f"semantic leak Noul {noul} at or above threshold {threshold}"}
    else:
        observed = {"status": "CLEAN",
                    "reason": f"no regex hit and semantic leak Noul {noul} below threshold {threshold}"}
    if not calibrated:
        return {"status": "UNVERIFIED", "acted": False,
                "reason": ("uncalibrated brief_leak leak_threshold (JEV-17): TypeSafe was called and the "
                           "verdict below was recorded for calibration, but it was never applied"),
                "observed_decision": observed}
    return {**observed, "acted": True}
