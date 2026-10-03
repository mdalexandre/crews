"""Bounded escalation and repair of one planned role (shared by `crew escalate` and the crew_escalate tool).

Interface
    next_seat(cat, kind, model, effort, need) -> NextSeat
        The next stronger seat on ALLOWLIST (sonnet low to xhigh, then opus medium to xhigh) whose model is no
        weaker than BASELINE_MODEL: effort first, then model. Fable and off allowlist efforts (max, opus low)
        are never targets. When no step exists the result carries a refusal naming the condition; a seat is
        never weakened and never substituted.
    escalate(cat, run_dir, role, reason, repair=False) -> dict
        repair False: re-seat the role one step up. repair True: same seat, with the failure evidence appended.
        Reads <run dir>/plan.json and roles.json, counts <run dir>/escalations.json, and caps a run at
        MAX_ESCALATIONS escalations and MAX_REPAIR_ROUNDS repair rounds. Returns {"status": "ok", "agent_call":
        {subagent_type, description, prompt}, "returns", "record_path", ...} or {"status": "blocked",
        "condition": ...}. On success the record is appended to escalations.json and the new call is registered
        in the plan index exactly as crew_plan registers its calls, so the seat guard admits it.

Boundaries: pure file reads and writes under the run dir plus the plan index; no Agent call, no process.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crews import catalog, paths, planindex

MAX_ESCALATIONS = 4
MAX_REPAIR_ROUNDS = 2
LEDGER = "escalations.json"


@dataclass(frozen=True)
class NextSeat:
    model: str | None
    effort: str | None
    refusal: str | None = None


# Escalation targets match the claude-crews route policy (crews/orchestrator/route.py): a target must be on
# ALLOWLIST and its model must be no weaker than BASELINE_MODEL. Fable is not on the allowlist.
ALLOWLIST: dict[str, list[str]] = {
    "sonnet": ["low", "medium", "high", "xhigh"],
    "opus": ["medium", "high", "xhigh"],
    "haiku": [""],
}
BASELINE_MODEL = "sonnet"


def _rank(cat: dict[str, Any], model: str, effort: str | None) -> tuple[int, int]:
    ladder: list[str] = cat["effort_ladder"]
    return cat["capability"].index(model), (ladder.index(effort) if effort else -1)


def next_seat(cat: dict[str, Any], kind: str, model: str, effort: str | None, need: str) -> NextSeat:
    ladder: list[str] = cat["effort_ladder"]
    capability: list[str] = cat["capability"]
    if model not in capability:
        return NextSeat(None, None, f"model {model!r} is not in the catalog capability order")
    if effort and effort not in ladder:
        return NextSeat(None, None, f"effort {effort!r} is not on the catalog effort ladder")
    here = _rank(cat, model, effort)
    floor = capability.index(BASELINE_MODEL)
    for m in capability:
        if m not in ALLOWLIST or capability.index(m) < floor:
            continue
        for e in ALLOWLIST[m]:
            if _rank(cat, m, e or None) > here:
                return NextSeat(m, e or None)
    top = f"{model} at {effort}" if effort else model
    return NextSeat(None, None, f"{kind} seat {top} is at or above the top of the allowlist; "
                                f"no stronger allowlisted seat exists")


def _blocked(condition: str) -> dict[str, Any]:
    return {"status": "blocked", "condition": condition}


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _find_role(spec: dict[str, Any], role: str) -> dict[str, Any] | None:
    for r in spec.get("roles") or []:
        if isinstance(r, dict) and r.get("name") == role:
            return r
    return None


def escalate(cat: dict[str, Any], run_dir: str, role: str, reason: str, repair: bool = False,
             home: Path | None = None) -> dict[str, Any]:
    run = Path(run_dir).expanduser()
    if not reason or not reason.strip():
        return _blocked("reason is empty: name the failure that justifies the re-seat")
    try:
        plan = _read(run / "plan.json")
        roles = _read(run / "roles.json")
    except (OSError, json.JSONDecodeError):
        return _blocked(f"no readable plan.json and roles.json in {run}: run crew_plan with this run_dir first")
    calls = [c for w in plan.get("waves") or [] for c in w.get("calls") or [] if c.get("role") == role]
    spec_role = _find_role(roles, role)
    if not calls or spec_role is None:
        return _blocked(f"unknown role {role!r}: the plan in {run} holds no such role")
    if len(calls) > 1:
        return _blocked(f"role {role!r} has {len(calls)} cells; escalation re-seats exactly one cell per role")
    call = calls[0]
    ledger_path = run / LEDGER
    try:
        records: list[dict[str, Any]] = _read(ledger_path)["escalations"] if ledger_path.is_file() else []
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return _blocked(f"{ledger_path} is unreadable; it is the cap counter and is never guessed")
    done = [r for r in records if r.get("repair") is repair]
    cap = MAX_REPAIR_ROUNDS if repair else MAX_ESCALATIONS
    label = "repair rounds" if repair else "escalations"
    if len(done) >= cap:
        return _blocked(f"cap: {len(done)} {label} already used in this run, the limit is {cap}")
    mine = [r for r in records if r.get("role") == role]
    kind, need = call["kind"], str((spec_role.get("answers") or {}).get("need", ""))
    cur_model, cur_effort = (mine[-1]["to_model"], mine[-1]["to_effort"]) if mine else (call["model"], call["effort"])
    if repair:
        model, effort = cur_model, cur_effort
    else:
        step = next_seat(cat, kind, cur_model, cur_effort, need)
        if step.refusal or step.model is None:
            return _blocked(step.refusal or "no stronger seat exists")
        model, effort = step.model, step.effort
        if _rank(cat, model, effort) <= _rank(cat, call["model"], call["effort"]):
            return _blocked("the next seat would not be stronger than the original seat; never downgraded")
    n = len(done) + 1
    seat_file = catalog.seat_name(kind, model, effort)
    word = "repair" if repair else "escalation"
    guidance = ("Same seat, re-dispatched. Fix the failure above and re-verify." if repair
                else "You are re-seated one step up. Do not repeat the failed approach.")
    section = (f"\n\n<escalation kind=\"{word}\" number=\"{n}\">\n"
               f"This is {word} {n} of {cap} for this role in this run. Reason: {reason.strip()}\n"
               f"{guidance}\n"
               f"</escalation>")
    agent_call = {"subagent_type": f"{paths.seat_prefix()}{seat_file}", "description": role,
                  "prompt": call["agent_call"]["prompt"] + section}
    record = {"n": n, "role": role, "repair": repair, "reason": reason.strip(),
              "from_model": cur_model, "from_effort": cur_effort, "to_model": model, "to_effort": effort,
              "seat_file": seat_file, "ts": datetime.now(timezone.utc).isoformat()}
    notes: list[str] = []
    if seat_file not in {s["agent"] for s in catalog.reachable_seats(cat)}:
        notes.append(f"seat {seat_file} is outside the installed seat set; the host cannot dispatch it")
    try:
        run.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(json.dumps({"escalations": [*records, record]}, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8")
        planindex.record({"waves": [{"calls": [{"agent_call": agent_call}]}]}, str(run), home=home)
    except OSError as exc:
        return _blocked(f"could not write the ledger or plan index ({type(exc).__name__}); nothing was issued")
    return {"status": "ok", "role": role, "repair": repair, "number": n, "cap": cap,
            "seat": {"model": model, "effort": effort, "seat_file": seat_file},
            "agent_call": agent_call, "returns": call.get("returns"), "record_path": call.get("record_path"),
            "notes": notes}
