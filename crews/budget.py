"""Cell budget: four typed task answers in, a cell count and the mandatory kinds out. Never names a role.

Interface
    compute(cat, answers | None) -> dict   answers None means the judge was unavailable (outage budget)
    check_answers(cat, answers) -> list[str]   every defect in an answers object

Answers schema (the TypeSafe response shape of the prototype):
    {"need": {"choice": str, "confidence": float, "probabilities": {need: float}},
     "divisible": {"noul": float}, "needs_verifier": {"noul": float}, "difficulty": {"score": float}}
"""

from __future__ import annotations

from typing import Any

from crews import catalog

OUTAGE_NEED = "everyday_implementation"


def check_answers(cat: dict[str, Any], answers: Any) -> list[str]:
    if not isinstance(answers, dict):
        return ["answers must be a JSON object"]
    defects: list[str] = []
    need = answers.get("need")
    if need is None:
        defects.append("need: missing")
    elif not isinstance(need, dict):
        defects.append(
            'need: must be an object {"choice": str, "confidence": 0..1}, got '
            f"{type(need).__name__}; the flat shape belongs in roles[].answers, "
            "not in the task answers argument")
    else:
        if need.get("choice") not in cat["needs"]:
            defects.append(f"need.choice: unknown need {need.get('choice')!r}")
        conf = need.get("confidence")
        if not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
            defects.append("need.confidence: must be a number from 0 to 1")
        probs = need.get("probabilities", {})
        if not isinstance(probs, dict):
            defects.append("need.probabilities: must be an object")
            probs = {}
        for key, value in probs.items():
            if key not in cat["needs"]:
                defects.append(f"need.probabilities: unknown need {key!r}")
            if not isinstance(value, (int, float)) or not 0 <= value <= 1:
                defects.append(f"need.probabilities.{key}: must be a number from 0 to 1")
    for key in ("divisible", "needs_verifier"):
        v = (answers.get(key) or {}).get("noul") if isinstance(answers.get(key), dict) else None
        if not isinstance(v, (int, float)) or not 0 <= v <= 1:
            defects.append(f"{key}.noul: must be a number from 0 to 1")
    d = answers.get("difficulty")
    score = d.get("score") if isinstance(d, dict) else None
    if not isinstance(score, (int, float)) or not 0 <= score <= 3:
        defects.append("difficulty.score: must be a number from 0 to 3")
    return defects


def capability(cat: dict[str, Any], need: str) -> tuple[int, int]:
    """Worker seat strength: model first, then effort. A no effort model ranks its effort at -1."""
    seat = cat["needs"][need]["worker"]
    effort = seat["effort"]
    return (cat["capability"].index(seat["model"]), -1 if effort is None else cat["effort_ladder"].index(effort))


def resolve_need(cat: dict[str, Any], answers: dict[str, Any], notes: list[str]) -> str:
    need: str = answers["need"]["choice"]
    conf = answers["need"]["confidence"]
    floor = cat["thresholds"]["need_floor"]
    if conf >= floor:
        return need
    probs = dict(answers["need"].get("probabilities") or {})
    probs.setdefault(need, conf)
    ranked = [n for n, _ in sorted(probs.items(), key=lambda kv: -kv[1])[:2]]
    safer = max(ranked, key=lambda n: capability(cat, n))
    if safer != need:
        notes.append(f"confidence {conf} below {floor}: {need} -> {safer}, the more capable of the top two")
    return safer


def compute(cat: dict[str, Any], answers: dict[str, Any] | None) -> dict[str, Any]:
    t = cat["thresholds"]
    notes: list[str] = []
    if answers is None:
        need, conf = OUTAGE_NEED, 0.0
        divisible, verifier, difficulty = 0.0, 1.0, 1.0
        notes.append("judge unavailable: outage budget on the everyday_implementation row, with a check required "
                     "because nothing classified the risk")
    else:
        need = resolve_need(cat, answers, notes)
        conf = float(answers["need"]["confidence"])
        divisible = float(answers["divisible"]["noul"])
        verifier = float(answers["needs_verifier"]["noul"])
        difficulty = float(answers["difficulty"]["score"])
    row = cat["needs"][need]
    worker = dict(row["worker"])
    if difficulty >= t["ladder"] and worker["effort"] is not None:
        worker["effort"] = catalog.bump(cat, worker["effort"])
        notes.append(f"difficulty {difficulty} at or above {t['ladder']}: worker effort raised one step")
    if need == "mechanical_verification":
        notes.append("try a deterministic tool first; this row spends the least when none is available")
    fan_out = divisible >= t["divisible"]
    parallel = min(2 + round(difficulty), t["max_workers"]) if fan_out else 1
    check_required = verifier >= t["verifier"]
    cells = (1 if fan_out else 0) + parallel + (1 if check_required else 0)
    return {
        "need": need,
        "confidence": conf,
        "row": row,
        "worker_seat": worker,
        "fan_out": fan_out,
        "parallel_cells": parallel,
        "lead_cell": fan_out,
        "check_required": check_required,
        "budget": cells,
        "dispatched_budget": cells - 1 if fan_out else cells,
        "dials": {"divisible": divisible, "needs_verifier": verifier, "difficulty": difficulty},
        "notes": notes,
    }
