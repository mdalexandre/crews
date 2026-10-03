"""claude-crews MCP server: four tools over the same core the crew CLI calls (SRD 3.7).

Interface
    A stdio MCP server named claude-crews. Tools and their CLI twins:
        crew_catalog  list --json    read only
        crew_check    check          read only
        crew_budget   budget         writes <run dir>/budget.json
        crew_plan     plan           writes the plan files in <run dir>/
        crew_escalate escalate       appends <run dir>/escalations.json and the plan index
    Every tool returns an execution envelope (execution_id, holon_id, contract_version, status, input_reference,
    output, evidence, warnings, errors, started_at, completed_at) and never raises. Status is one of COMPLETED,
    COMPLETED_WITH_WARNINGS, BLOCKED, FAILED; a refusal the CLI reports at exit 2 comes back BLOCKED.

Boundaries
    The server plans; the session dispatches. It issues no Agent call, starts no claude process and no shell.
    A writing tool called without run_dir writes under
    <crews home>/runs/<execution_id>/ and nowhere else. stdout carries only the protocol.
    The live judge runs only when the server's own environment names a key: TYPESAFE_API_KEY (the plugin
    userConfig value) or TYPESAFE_API_KEY_FILE (SRD Q9). An empty or blank TYPESAFE_API_KEY is no key.

Calibration note (JEV-17, owner directive 2026-09-22, crew task calibrated-flag-wiring-completer): the
four `judge`/`skills`/`brief_check` parameters below now default "live", completing the flip a prior role
stopped short of at this exact boundary. The prior role's own note explained why: several
tests/test_server.py calls omit these parameters and assume the "off" default, so a live default with no
TYPESAFE_API_KEY_FILE forces an outage that used to collide with a declared `answers` argument and raise
PlanRefused where COMPLETED was expected. That collision is now removed: `crew_budget` and `crew_plan`
below no longer special case a forced outage against declared `answers`/`outage`; once `_live_gate`
downgrades a requested "live" mode to "off" for want of a key file, the call proceeds exactly as an
explicit "off" call always has (declared answers stand, a bare live-with-nothing-declared call still
refuses through `crews/cli.py`'s own `resolve_task_answers`), and only a warning is added. `skills` and
`brief_check` are downgraded through the same `_live_gate` before reaching `crews/cli.py`'s
`core_plan`, so a machine with no key file never opens a pending owner-gate intent for either surface by
mere default; explicitly passing "live" from a machine that does carry a key file still reaches
`crews/egress.py`'s owner approval ledger exactly as before.

The second reason the prior role withheld the flip is now also closed: `crews/cli.py`'s
`_apply_skill_routing`, `_apply_brief_check`, and `cmd_judge_handback` read `typesafe.skill_routing.
calibrated` / `.brief_leak.calibrated` / `.handback.calibrated` from the catalog and thread it into
`crews.skills.decide`, `crews.briefcheck.decide` (now carries the same `calibrated` keyword as the other
two), and `crews.handback.apply`. With the shipped catalog's three flags still `false`, an answered,
approved call at any of these surfaces is recorded (`observed_decision` / `observed_nouls`) but never
attaches a skill, never marks a brief LEAK from the semantic Noul alone, and never turns a claimed PASS
into a judged one: JEV-17 holds at the real call site, not only in the functions it calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from crews import catalog, cli, escalate, installer, judge, paths, planner  # noqa: E402

# OSS-PKG-8: the plugin registration passes the userConfig key as TYPESAFE_API_KEY. An empty or blank value
# (the key was left unset) is no key at all, so the variable is removed and the judge degrades to off.
if not os.environ.get("TYPESAFE_API_KEY", "").strip():
    os.environ.pop("TYPESAFE_API_KEY", None)

HOLON_ID = "claude-crews"
Q9_OFF = ("live judge is off for this server: its registration names no TYPESAFE_API_KEY or "
          "TYPESAFE_API_KEY_FILE (SRD Q9); the outage budget stands")

mcp = FastMCP("claude-crews", log_level="WARNING")


@dataclass
class Outcome:
    output: Any
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _files(root: Path | None) -> set[str]:
    if root is None or not root.is_dir():
        return set()
    return {str(p) for p in root.rglob("*") if p.is_file()}


def _run_dir(given: str | None, execution_id: str) -> Path:
    return Path(given).expanduser() if given else paths.state_dir("runs") / execution_id


def _envelope(tool: str, inputs: dict[str, Any], run: Path | None,
              body: Callable[[dict[str, Any]], Outcome], execution_id: str) -> dict[str, Any]:
    canonical = json.dumps({"tool": tool, **inputs}, sort_keys=True, separators=(",", ":"), default=str)
    env: dict[str, Any] = {
        "execution_id": execution_id, "holon_id": HOLON_ID, "contract_version": "unknown", "status": "FAILED",
        "input_reference": hashlib.sha256(canonical.encode("utf-8")).hexdigest(), "output": None,
        "evidence": [], "warnings": [], "errors": [], "started_at": _now(), "completed_at": ""}
    before = _files(run)
    try:
        cat = cli._load(None)
        env["contract_version"] = cat["version"]
        out = body(cat)
        env["output"], env["warnings"], env["errors"] = out.output, out.warnings, out.errors
        env["status"] = "BLOCKED" if out.errors else ("COMPLETED_WITH_WARNINGS" if out.warnings else "COMPLETED")
    except planner.PlanRefused as exc:
        # OSS-FIX-2: a refusal lists every error it found, one entry each
        env["status"], env["errors"] = "BLOCKED", [f"PlanRefused: {e}" for e in exc.errors]
    except (catalog.CatalogError, judge.JudgeParseError, json.JSONDecodeError) as exc:
        env["status"], env["errors"] = "BLOCKED", [f"{type(exc).__name__}: {exc}"]
    except Exception as exc:  # noqa: BLE001  a tool never raises, by contract (DR-20)
        env["status"], env["errors"] = "FAILED", [f"{type(exc).__name__}: {exc}"]
    written = sorted(_files(run) - before)
    if run is not None:
        env["evidence"] = [{"run_dir": str(run)}, *({"written": w} for w in written)]
    env["completed_at"] = _now()
    return env


def _live_gate(judge_mode: str, warnings: list[str]) -> tuple[str, bool]:
    """(mode to use, outage flag). Live without a registered key file becomes "off" with a warning. Callers
    may gate several parameters (judge, skills, brief_check) through the same `warnings` list in one body;
    the Q9 warning is appended at most once per call even so, since it names the same missing key file
    regardless of which of the four surfaces asked for live."""
    has_key = bool(os.environ.get("TYPESAFE_API_KEY") or os.environ.get("TYPESAFE_API_KEY_FILE"))
    if judge_mode == "live" and not has_key:
        if Q9_OFF not in warnings:
            warnings.append(Q9_OFF)
        return "off", True
    return judge_mode, False


_NOTE_MARKERS = ("judge outage", "judge owner_gate", "judge skipped", "judge live was asked", "need_gate",
                 "skill routing", "brief leak", "plan index not written")


def _judge_warnings(result: dict[str, Any], warnings: list[str]) -> None:
    for note in result.get("notes", []):
        if any(marker in note for marker in _NOTE_MARKERS):
            warnings.append(note)
    if result.get("owner_gates"):
        warnings.append(f"{len(result['owner_gates'])} TypeSafe call(s) await owner approval via `crew approve`")


@mcp.tool(structured_output=False)
def crew_catalog() -> dict[str, Any]:
    """Every reachable seat (kind, model, effort, tools) from the shipped catalog. Twin of `crew list --json`.
    Read only."""
    return _envelope("crew_catalog", {}, None, lambda cat: Outcome(cli.core_list(cat)), uuid.uuid4().hex)


@mcp.tool(structured_output=False)
def crew_check(agents_dir: str = "~/.claude/agents") -> dict[str, Any]:
    """Drift between the installed seat files and the catalog: missing, foreign, drifted, stale. Twin of
    `crew check`. Read only; problems come back as warnings."""
    def body(cat: dict[str, Any]) -> Outcome:
        res = installer.check(cat, Path(agents_dir).expanduser())
        return Outcome(res.as_dict(), warnings=list(res.problems))
    return _envelope("crew_check", {"agents_dir": agents_dir}, None, body, uuid.uuid4().hex)


@mcp.tool(structured_output=False)
def crew_budget(answers: dict[str, Any] | None = None, outage: bool = False,
                judge: Literal["off", "live"] = "live", task: str | None = None,
                run_dir: str | None = None) -> dict[str, Any]:
    """Cell budget from four typed task answers, from the outage flag, or from the live judge when the
    registration allows it. Twin of `crew budget`. Writes <run dir>/budget.json; run_dir defaults to
    <crews home>/runs/<execution_id>/.

    answers takes the NESTED task shape, which is not the flat roles[].answers shape:
        {"need": {"choice": <need>, "confidence": 0..1, "probabilities": {<need>: 0..1}},
         "divisible": {"noul": 0..1}, "needs_verifier": {"noul": 0..1},
         "difficulty": {"score": 0..3}}
    """
    eid = uuid.uuid4().hex
    run = _run_dir(run_dir, eid)

    def body(cat: dict[str, Any]) -> Outcome:
        warnings: list[str] = []
        mode, forced_outage = _live_gate(judge, warnings)
        # A forced downgrade to "off" is never an error (A4): declared answers stand when given (the
        # ordinary case a default "live" call with no key file falls into); with no declared answers, the
        # call falls back to the outage-default budget, the same "off with a warning" result a caller
        # explicitly passing judge="off"/outage=True always got. A genuine "live" mode (key file present)
        # keeps its own conflict checks in resolve_task_answers ("judge live asks the judge...", "judge
        # live needs the task text"); this body only ever forces outage once the mode is already downgraded.
        effective_outage = outage or (forced_outage and answers is None)
        result = cli.core_budget(cat, answers=answers, outage=effective_outage, judge_mode=mode, task=task,
                                 run_dir=str(run))
        _judge_warnings(result, warnings)
        return Outcome(result, warnings=warnings)
    inputs = {"answers": answers, "outage": outage, "judge": judge, "task": task, "run_dir": run_dir}
    return _envelope("crew_budget", inputs, run, body, eid)


@mcp.tool(structured_output=False)
def crew_plan(roles: dict[str, Any] | list[Any] | str, answers: dict[str, Any] | None = None, outage: bool = False,
              judge: Literal["off", "live"] = "live", run_dir: str | None = None, allow: int | None = None,
              force: dict[str, Any] | None = None, skills: Literal["off", "live"] = "live",
              brief_check: Literal["off", "live"] = "live", verbose: bool = False) -> dict[str, Any]:
    """Plan task specific roles into seated cells. `roles` is {task, roles: [{name, kind, mission, deliverable,
    scope, slices, criteria, answers, professional_frame, authority, inputs, acceptance, execution, returns,
    finding_format, blocked_when, must_not, read_scope}]} (brief contract v2, SRD spec V2-1). Returns the Agent
    calls to issue, grouped by wave (subagent_type = seat file, description = role name, prompt); each call also
    carries returns ("artifact", "report", or "verdict") and record_path (the file `crew record` writes from the
    agent's hand-back; the agent never writes it itself). The headless argv stay in <run dir>/plan.json. Twin of
    `crew plan`. The server dispatches nothing: the session issues the calls. run_dir defaults to
    <crews home>/runs/<execution_id>/.

    Every check role's authored text is always scanned by the mechanical brief leak regex floor (no option,
    no network); a hit refuses the plan. `skills` and `brief_check` are each "live" (default) or "off": each
    live surface gates one further TypeSafe call through the owner approval ledger (`crew
    approve`/`crew gates`); with no approval yet, the plan is unchanged, the pending packet comes back
    under `owner_gates`, and the matching status is UNVERIFIED. On a machine with no TYPESAFE_API_KEY_FILE
    registered, `judge`, `skills`, and `brief_check` each downgrade to "off" (a warning, never an owner-gate
    packet, never an error) before any of the three reaches `crews/cli.py`. This tool never approves a call
    itself; only the CLI `approve` and `approve-batch` verbs do (TSI-GATE-07). Even an approved, answered
    call at `skills` or `brief_check` never acts while the shipped catalog's matching `calibrated` flag
    stays false (JEV-17): the TypeSafe verdict is recorded for calibration and the plan is unchanged.

    The inline result is compact (OSS-FIX-3): the calls plus summary fields. Owner gate packets and the
    TypeSafe detail are in <run dir>/plan.json (named by `full_in`); verbose=true returns everything inline.
    A refusal lists every input error at once (OSS-FIX-2). With judge "live" and declared `answers`, the
    declared answers stand and the plan carries a warning (OSS-FIX-4), never a refusal."""
    eid = uuid.uuid4().hex
    run = _run_dir(run_dir, eid)

    def body(cat: dict[str, Any]) -> Outcome:
        spec = json.loads(roles) if isinstance(roles, str) else roles
        warnings: list[str] = []
        mode, forced_outage = _live_gate(judge, warnings)
        skills_mode, _ = _live_gate(skills, warnings)
        brief_check_mode, _ = _live_gate(brief_check, warnings)
        # See crew_budget's body: a forced downgrade to "off" is never an error here either. Declared
        # answers stand when given; with no declared answers, the task level judge falls back to the
        # outage default, the same "off with a warning" result a caller explicitly passing judge="off"/
        # outage=True always got. A genuine "live" judge mode (key file present) keeps its own conflict
        # checks in crews.cli.resolve_task_answers; skills_mode/brief_check_mode "live" reach
        # crews.cli._apply_skill_routing/_apply_brief_check, which already fail open to a note plus
        # UNVERIFIED/owner_gate on any outage, never an exception.
        effective_outage = outage or (forced_outage and answers is None)
        view = cli.core_plan(cat, spec=spec, answers=answers, outage=effective_outage, judge_mode=mode,
                             run_dir=str(run), allow=allow, force=force, skills_mode=skills_mode,
                             brief_check_mode=brief_check_mode)
        _judge_warnings(view, warnings)
        return Outcome(view if verbose else cli.compact_view(view), warnings=warnings)
    inputs = {"roles": roles, "answers": answers, "outage": outage, "judge": judge, "run_dir": run_dir,
              "allow": allow, "force": force, "skills": skills, "brief_check": brief_check, "verbose": verbose}
    return _envelope("crew_plan", inputs, run, body, eid)


@mcp.tool(structured_output=False)
def crew_escalate(run_dir: str, role: str, reason: str, repair: bool = False) -> dict[str, Any]:
    """Re-seat one planned role one step up (effort first, then model; never below its original seat; Fable only
    for need specialist), or with repair=true re-dispatch the same seat with the failure evidence appended.
    Twin of `crew escalate`. Caps per run dir: 4 escalations and 2 repair rounds; beyond a cap the result is
    BLOCKED naming the condition. Returns the Agent call to issue (subagent_type, description = role name,
    prompt), returns and record_path; the new call is registered in the plan index so the seat guard admits
    exactly it. Appends to <run dir>/escalations.json. The server dispatches nothing."""
    def body(cat: dict[str, Any]) -> Outcome:
        res = escalate.escalate(cat, run_dir, role, reason, repair=repair)
        if res["status"] != "ok":
            return Outcome(res, errors=[f"escalation blocked: {res['condition']}"])
        return Outcome(res, warnings=list(res.get("notes") or []))
    inputs = {"run_dir": run_dir, "role": role, "reason": reason, "repair": repair}
    return _envelope("crew_escalate", inputs, Path(run_dir).expanduser(), body, uuid.uuid4().hex)


if __name__ == "__main__":
    mcp.run()
