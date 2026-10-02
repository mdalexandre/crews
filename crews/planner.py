"""Plan: task specific roles in, seated cells out.

Interface
    build(cat, task_answers | None, spec, run_dir, allow=None, force=None) -> (plan, files)
        plan   the full plan (plan.json): budget, waves of cells, each with its Agent call and headless argv
        files  {file name: payload} for the run dir: budget.json, roles.json, plan.json, seats.json
        Raises PlanRefused naming what is missing; nothing is written by this module.
    public_view(plan) -> dict   what the CLI prints and the MCP returns: the plan without headless argv

The base session writes the roles. Code only checks them and fits each one with a seat:
the role's need row and kind column fix model and effort, its difficulty may raise the effort one step,
its kind fixes the tools, and its slices set the worker count. Crew never inserts a role.

Brief contract v2 (spec V2-1 to V2-7): a role is a typed XML execution contract (_prompt), not free prose.
returns is "artifact" or "report" for produce, integrate and research (check always returns a verdict);
acceptance is required for produce, research and integrate roles when the catalog demands it; the brief
never tells the agent to write its own deliverable, because the deliverable now names the RECORD file the
orchestrator writes with `crew record`, not a file the agent produces.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import PurePosixPath
from typing import Any

from crews import budget as budget_mod
from crews import catalog

STOPWORDS = frozenset({"with", "that", "this", "from", "into", "each", "have", "will", "what", "when", "then", "than",
                       "them", "they", "your", "only", "also", "over", "must", "make", "sure", "task", "role", "work"})
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
FRAME_KEYS = frozenset({"profession", "standards", "method", "watch_for"})
FRAME_LIST_KEYS = ("standards", "method", "watch_for")
YEARS_RE = re.compile(r"\b\d+\+?\s*(years?|yrs)\b", re.IGNORECASE)
WS_RE = re.compile(r"\s+")

# V2-1: new optional list and string fields, each validated when present.
LIST_FIELDS = ("authority", "inputs", "acceptance", "execution", "blocked_when", "must_not", "read_scope")
STRING_FIELDS = ("finding_format",)
RETURNS_DEFAULT = {"produce": "artifact", "integrate": "artifact", "research": "report"}

# V2-2: the check kind's fixed inspection procedure, rendered as <check_protocol> in place of <execution>.
CHECK_PROTOCOL_LINES = [
    "- Establish the required behavior from the authority and the criteria first, then inspect and probe the "
    "artifacts.",
    "- Evaluate every criterion; a failure does not end the check.",
    "- Prefer executable evidence (run the tests, probe the behavior) over reading when it is available.",
    "- Treat every criterion as unverified until your own evidence supports it. Assume nothing about how the "
    "artifacts were produced or whether anyone tested them.",
]


class PlanRefused(ValueError):
    """The spec cannot be planned. The message names the role or the missing kind."""


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in STOPWORDS}


def _norm(path: str) -> PurePosixPath:
    return PurePosixPath(os.path.normpath(path.strip()))


def _overlap(a: str, b: str) -> bool:
    pa, pb = _norm(a), _norm(b)
    return pa == pb or pa in pb.parents or pb in pa.parents


def _norm_text(text: str) -> str:
    return WS_RE.sub(" ", text.strip().lower())


def _returns_of(role: dict[str, Any]) -> str | None:
    """The role's effective returns mode: "artifact" or "report", or None for a check role (it returns a verdict)."""
    kind = role["kind"]
    if kind == "check":
        return None
    declared = role.get("returns")
    return declared if declared else RETURNS_DEFAULT[kind]


def _role_answers(role: dict[str, Any], cat: dict[str, Any]) -> dict[str, Any]:
    ans = role.get("answers")
    name = role.get("name")
    if not isinstance(ans, dict):
        raise PlanRefused(f"role {name}: answers are missing (need, difficulty, divisible)")
    if ans.get("need") not in cat["needs"]:
        raise PlanRefused(f"role {name}: unknown need {ans.get('need')!r}")
    for key, hi in (("difficulty", 3), ("divisible", 1)):
        v = ans.get(key)
        if not isinstance(v, (int, float)) or not 0 <= v <= hi:
            raise PlanRefused(f"role {name}: answers.{key} must be a number from 0 to {hi}")
    spec = ans.get("specific")
    if spec is not None and (not isinstance(spec, (int, float)) or not 0 <= spec <= 1):
        raise PlanRefused(f"role {name}: answers.specific must be a number from 0 to 1")
    floor = cat["thresholds"]["specific"]
    if spec is not None and spec < floor:
        raise PlanRefused(f"role {name}: specificity {spec} below {floor}; name a concrete target in this task")
    return ans


def _frame_text(frame: dict[str, Any]) -> list[str]:
    texts = [str(frame.get("profession") or "")]
    for key in FRAME_LIST_KEYS:
        texts += [str(x) for x in (frame.get(key) or [])]
    return texts


def _check_frame(role: dict[str, Any], cat: dict[str, Any]) -> None:
    name = role.get("name")
    frame = role.get("professional_frame")
    if frame is None:
        if cat["thresholds"].get("require_frame") and role.get("kind") in ("produce", "check"):
            raise PlanRefused(f"role {name}: no professional_frame; state the expert this work needs "
                              "(profession, standards, method, watch_for)")
        return
    if not isinstance(frame, dict):
        raise PlanRefused(f"role {name}: professional_frame must be an object")
    unknown = sorted(set(frame) - FRAME_KEYS)
    if unknown:
        raise PlanRefused(f"role {name}: professional_frame has unknown key(s) {', '.join(unknown)}")
    profession = frame.get("profession")
    if not isinstance(profession, str) or not 3 <= len(profession) <= 200:
        raise PlanRefused(f"role {name}: professional_frame.profession must be 3 to 200 characters")
    for key in FRAME_LIST_KEYS:
        if key not in frame:
            continue
        items = frame[key]
        if not isinstance(items, list) or not all(isinstance(x, str) and x.strip() for x in items):
            raise PlanRefused(f"role {name}: professional_frame.{key} must be a list of non empty strings")
    for text in _frame_text(frame):
        if YEARS_RE.search(text):
            raise PlanRefused(f"role {name}: professional_frame: experience claims are decoration; "
                              "remove the years of experience phrase")


def _check_v2_fields(role: dict[str, Any], cat: dict[str, Any]) -> None:
    """V2-1: authority, inputs, acceptance, execution, returns, finding_format, blocked_when, must_not, read_scope."""
    name = role.get("name")
    kind = role["kind"]
    for field in LIST_FIELDS:
        if field in role and role[field] is not None:
            v = role[field]
            if not (isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)):
                raise PlanRefused(f"role {name}: {field} must be a list of non empty strings")
    for field in STRING_FIELDS:
        if field in role and role[field] is not None:
            v = role[field]
            if not (isinstance(v, str) and v.strip()):
                raise PlanRefused(f"role {name}: {field} must be a non empty string")
    returns_field = role.get("returns")
    if kind == "check":
        if returns_field is not None:
            raise PlanRefused(f"role {name}: a check role has no returns field; it returns a verdict")
    elif returns_field is not None:
        if returns_field not in ("artifact", "report"):
            raise PlanRefused(f"role {name}: returns must be \"artifact\" or \"report\"")
        if kind == "research" and returns_field == "artifact":
            raise PlanRefused(f"role {name}: a research role cannot return artifact; it holds no Write tool")
    if kind in ("produce", "research", "integrate") and cat["thresholds"].get("require_acceptance"):
        acc = role.get("acceptance")
        if not (isinstance(acc, list) and acc):
            raise PlanRefused(f"role {name}: a {kind} role needs acceptance criteria "
                              "(the observable conditions that define done)")


def check_spec(cat: dict[str, Any], spec: Any, check_required: bool) -> list[dict[str, Any]]:
    """Every refusal rule except the budget and the cap, which need the cell count."""
    if not isinstance(spec, dict) or not isinstance(spec.get("roles"), list) or not spec["roles"]:
        raise PlanRefused("roles: the spec must hold a non empty roles list")
    task = spec.get("task")
    if not isinstance(task, str) or not task.strip():
        raise PlanRefused("task: the spec must hold the task text")
    generic = {g.lower() for g in cat["generic_names"]}
    kinds = set(cat["kinds"])
    seen: set[str] = set()
    roles: list[dict[str, Any]] = spec["roles"]
    for role in roles:
        name = role.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise PlanRefused(f"role {name!r}: name must be kebab case, saying what it does to what")
        tokens = set(name.split("-"))
        if name in generic or tokens <= generic | kinds:
            raise PlanRefused(f"role {name}: generic name; name it after what it does to what in this task")
        if name in seen:
            raise PlanRefused(f"role {name}: duplicate name")
        seen.add(name)
        kind = role.get("kind")
        if kind not in kinds:
            raise PlanRefused(f"role {name}: kind must be one of {', '.join(cat['wave_order'])}")
        mission = role.get("mission")
        if not isinstance(mission, str) or not mission.strip():
            raise PlanRefused(f"role {name}: mission is missing")
        scope = role.get("scope") or []
        if not isinstance(scope, list) or not all(isinstance(s, str) and s.strip() for s in scope):
            raise PlanRefused(f"role {name}: scope must be a list of paths")
        anchor = _words(task) | {w for s in scope for w in _words(s.replace("/", " ").replace("_", " "))}
        if not _words(mission) & anchor:
            raise PlanRefused(f"role {name}: mission shares no word of four or more letters with the task or its scope")
        if kind == "check" and scope:
            raise PlanRefused(f"role {name}: a check role holds no write scope")
        _check_v2_fields(role, cat)
        writes = kind in ("produce", "integrate") and _returns_of(role) == "artifact"
        if writes and not scope:
            raise PlanRefused(f"role {name}: a {kind} role needs a write scope")
        if kind == "check" and not (isinstance(role.get("criteria"), list) and role["criteria"]):
            raise PlanRefused(f"role {name}: a check role needs its acceptance criteria")
        deliverable = role.get("deliverable")
        plain = isinstance(deliverable, str) and deliverable.strip() and "/" not in deliverable
        if not plain or str(deliverable).startswith("."):
            raise PlanRefused(f"role {name}: deliverable must be a plain file name")
        slices = role.get("slices") or []
        if not isinstance(slices, list):
            raise PlanRefused(f"role {name}: slices must be a list")
        for sl in slices:
            if not isinstance(sl, dict) or not isinstance(sl.get("brief"), str):
                raise PlanRefused(f"role {name}: every slice needs a brief")
            if writes and not sl.get("scope"):
                raise PlanRefused(f"role {name}: every slice of a {kind} role needs a scope")
        _check_frame(role, cat)
        _role_answers(role, cat)
    if check_required and not any(r["kind"] == "check" for r in roles):
        raise PlanRefused("missing kind check: the budget requires a check role; write one for this task")
    claims: list[tuple[str, str]] = []
    for role in roles:
        if role["kind"] not in ("produce", "integrate") or _returns_of(role) != "artifact":
            continue
        paths = [p for sl in (role.get("slices") or []) for p in sl["scope"]] or list(role["scope"])
        own = [p for sl in (role.get("slices") or []) for p in sl["scope"]]
        for i, a in enumerate(own):
            for b in own[i + 1:]:
                if _overlap(a, b):
                    raise PlanRefused(f"role {role['name']}: slices overlap on {a} and {b}")
        for p in paths:
            for other, q in claims:
                if other != role["name"] and _overlap(p, q):
                    raise PlanRefused(f"roles {other} and {role['name']}: write scopes overlap on {q} and {p}")
            claims.append((role["name"], p))
    return roles


def _seat(cat: dict[str, Any], role: dict[str, Any], notes: list[str]) -> tuple[str, str | None]:
    ans = role["answers"]
    column = cat["kinds"][role["kind"]]["column"]
    seat = cat["needs"][ans["need"]][column]
    model, effort = seat["model"], seat["effort"]
    if ans["difficulty"] >= cat["thresholds"]["ladder"] and effort is not None:
        effort = catalog.bump(cat, effort)
        notes.append(f"{role['name']}: difficulty {ans['difficulty']} at or above the ladder: "
                     f"effort raised to {effort}")
    return model, effort


def _worker_count(cat: dict[str, Any], role: dict[str, Any]) -> int:
    slices = role.get("slices") or []
    ans = role["answers"]
    if slices and ans["divisible"] >= cat["thresholds"]["divisible"]:
        return max(1, min(2 + round(ans["difficulty"]), cat["thresholds"]["max_workers"], len(slices)))
    return 1


def _cells_for(cat: dict[str, Any], role: dict[str, Any], force: dict[str, Any], notes: list[str]
               ) -> list[tuple[str, str | None]]:
    model, effort = _seat(cat, role, notes)
    count = _worker_count(cat, role)
    over = force.get(role["name"])
    if not over:
        return [(model, effort)] * count
    model = over.get("model", model)
    if model not in cat["capability"]:
        raise PlanRefused(f"role {role['name']}: forced model {model!r} is not in the capability order")
    efforts: list[str | None] = list(over.get("efforts") or [effort])
    for e in efforts:
        if e is not None and e not in cat["effort_ladder"]:
            raise PlanRefused(f"role {role['name']}: forced effort {e!r} is outside the ladder")
    if model in cat["no_effort_models"]:
        if any(e is not None for e in efforts):
            notes.append(f"{role['name']}: {model} takes no effort; forced efforts dropped")
        efforts = [None]
    if "workers" in over and not over.get("efforts"):
        count = int(over["workers"])
        cells = [(model, efforts[0])] * count
    else:
        cells = [(model, e) for e in efforts]
    if model == "fable" and role["answers"]["need"] != cat["fable_need"]:
        notes.append(f"REFUSED: {role['name']}: fable outside the {cat['fable_need']} need; "
                     "its fable cells are dropped")
        return []
    notes.append(f"{role['name']}: forced to {len(cells)} cell(s) on {model}")
    return cells


def _composition(role: dict[str, Any]) -> dict[str, Any]:
    return {"verdict": "SINGLE_TIER", "conditions": [
        {"condition": 1, "holds": False, "evidence": f"crew hands role {role['name']} one whole mission, not angles"},
        {"condition": 2, "holds": False, "evidence": "with one mission per role there are no two angles to place"},
        {"condition": 3, "holds": True, "evidence": f"role {role['name']} was written for this task in roles.json"},
        {"condition": 4, "holds": False, "evidence": "crew plans model seats only; no deterministic cell is composed"},
    ]}


def _block(tag: str, lines: list[str]) -> str:
    return f"<{tag}>\n" + "\n".join(lines) + f"\n</{tag}>"


def _output_contract_lines(role: dict[str, Any], kind: str, returns_val: str | None) -> list[str]:
    if kind == "check":
        lines = ["Return exactly:",
                 "VERDICT: PASS|FAIL|BLOCKED",
                 "PASS: <criterion IDs, comma separated, or NONE>",
                 "FAIL <ID>: <defect> | EVIDENCE: <path:line, or the probe and its result>",
                 "UNVERIFIED: NONE|<ID: reason>",
                 "DECISION: NONE|<the one decision you need from the orchestrator>"]
    elif returns_val == "artifact":
        lines = ["Write artifacts inside WRITE only. Return exactly these lines and nothing else:",
                 "STATUS: COMPLETE|PARTIAL|BLOCKED",
                 "ARTIFACTS: <paths you wrote, comma separated, or NONE>",
                 "RESULT: <one sentence>",
                 "VERIFY: <acceptance IDs met, and each check command with its one line result>",
                 "DECISION: NONE|<the one decision you need from the orchestrator>"]
    else:
        default_shape = "SUPPORTED|CONFLICT|UNKNOWN <finding>" if kind == "research" else "<finding>"
        shape = role.get("finding_format") or default_shape
        lines = ["Return the result as text; never write it to a file. Return exactly:",
                 "STATUS: COMPLETE|PARTIAL|BLOCKED",
                 f"F: {shape} | EVIDENCE: <minimal evidence: path:line, a probe and its result, or a source>",
                 "(one F: line per confirmed finding; no other prose)",
                 "COVERAGE: <what you assessed>",
                 "DECISION: NONE|<the one decision you need from the orchestrator>"]
    lines.append("Do not return command transcripts or narrate your steps.")
    return lines


def _stop_conditions_lines(role: dict[str, Any], kind: str) -> list[str]:
    if kind == "check":
        lines = ["PASS only when your own evidence supports every criterion.",
                 "FAIL when at least one criterion fails, reported after every criterion is evaluated.",
                 "BLOCKED only when a missing artifact, authority or tool prevents a defensible verdict."]
    else:
        lines = ["COMPLETE only when every acceptance criterion is met and checked.",
                 "PARTIAL when some are met; name the rest under DECISION.",
                 "BLOCKED when an input or the authority is missing, or meeting a criterion needs a write outside "
                 "WRITE. Never work around a block by inventing a requirement or widening scope."]
    lines += [f"BLOCKED also when: {b}" for b in role.get("blocked_when") or []]
    return lines


def _prompt(role: dict[str, Any], task: str, kind: str, write_scope: list[str], inputs: list[str],
            slice_: dict[str, Any] | None, cell_id: str, returns_val: str | None) -> str:
    """V2-2: the typed XML brief. Sections appear in exactly the order the spec fixes; some are omitted, some
    always render even when empty (authority always falls back to one line, scope and output_contract and
    stop_conditions always render)."""
    name = role["name"]
    frame = role.get("professional_frame") or {}
    sections: list[str] = []

    objective = [role["mission"]]
    if slice_:
        objective.append(f"This cell's slice: {slice_['brief']}")
    sections.append(_block("objective", objective))

    if _norm_text(task) != _norm_text(role["mission"]):
        sections.append(_block("context", [f"Crew task: {task}"]))

    if frame:
        cue = f"{frame['profession']}."
        if frame.get("watch_for"):
            cue += " Lenses: " + "; ".join(frame["watch_for"]) + "."
        sections.append(_block("capability_cue", [cue]))

    authority = [f"- {a}" for a in role.get("authority") or []] + [f"- {s}" for s in frame.get("standards") or []]
    if not authority:
        authority = ["- The objective above and the files under inputs."]
    authority.append("- Invent no requirement beyond these sources. If they are not enough to decide, stop BLOCKED "
                     "and name what is missing.")
    sections.append(_block("authority", authority))

    inputs_tag = "artifact_under_check" if kind == "check" else "inputs"
    inputs_lines = [f"- {i}" for i in role.get("inputs") or []] + [f"- {p}" for p in inputs]
    sections.append(_block(inputs_tag, inputs_lines or ["- NONE"]))

    read = ", ".join(role.get("read_scope") or []) or "whatever the objective needs"
    write = "NONE" if kind == "check" else (", ".join(write_scope) or "NONE")
    must_not = role.get("must_not") or []
    must_not_extra = ("; " + "; ".join(must_not)) if must_not else ""
    sections.append(_block("scope", [
        f"READ: {read}",
        f"WRITE: {write}",
        f"MUST_NOT: write outside WRITE; create a report, findings or summary file; start other agents"
        f"{must_not_extra}",
    ]))

    if kind == "check":
        sections.append(_block("criteria", [f"C{i}: {c}" for i, c in enumerate(role["criteria"], 1)]))
    else:
        sections.append(_block("acceptance", [f"A{i}: {c}" for i, c in enumerate(role.get("acceptance") or [], 1)]))

    if kind == "check":
        sections.append(_block("check_protocol", CHECK_PROTOCOL_LINES))
    else:
        exec_lines = [f"- {m}" for m in frame.get("method") or []] + [f"- {e}" for e in role.get("execution") or []]
        if exec_lines:
            sections.append(_block("execution", exec_lines))

    sections.append(_block("output_contract", _output_contract_lines(role, kind, returns_val)))
    sections.append(_block("stop_conditions", _stop_conditions_lines(role, kind)))

    header = f'<crew_task version="2" kind="{kind}" role="{name}" cell="{cell_id}">'
    return "\n".join([header, *sections, "</crew_task>"])


def memory_scope(cat: dict[str, Any]) -> list[str]:
    """Argv prefix that bounds a headless worker's memory: a systemd user scope with the catalog's
    MemoryHigh and MemoryMax, only on Linux where systemd-run is on PATH. Elsewhere an empty prefix, so
    the emitted argv is a plain `claude -p ...` that runs on any platform (OSS-POR-5)."""
    if not sys.platform.startswith("linux") or shutil.which("systemd-run") is None:
        return []
    b = cat["bound"]
    return ["systemd-run", "--user", "--scope", "-p", f"MemoryHigh={b['memory_high']}", "-p",
            f"MemoryMax={b['memory_max']}", "--collect", "--"]


def _headless(cat: dict[str, Any], name: str, model: str, effort: str | None, tools: list[str], body: str,
              prompt: str) -> list[str]:
    agent = {name: {"description": f"claude-crews role {name}", "prompt": body, "tools": tools, "model": model}}
    argv = [*memory_scope(cat), "claude", "-p", "--model", model]
    if effort is not None:
        argv += ["--effort", effort]
    argv += ["--agents", json.dumps(agent, sort_keys=True), "--agent", name, prompt]
    return argv


def build(cat: dict[str, Any], task_answers: dict[str, Any] | None, spec: Any, run_dir: str,
          allow: int | None = None, force: dict[str, Any] | None = None,
          answer_sources: dict[str, str] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """answer_sources (JEV-15) names, per answer the plan actually used, where it came from: one of
    "declared", "live_admitted", "live_confirmed", "fallback". The key is "task" for the answer
    budget.compute used, and a role's name for that role's answers. A caller passes only the entries it
    can prove (the live-routed ones); every answer this build actually used and every name not passed
    defaults to "declared", so a caller that never touches live routing gets every answer recorded as
    declared with no other change to its plan (JEV-15 acceptance: byte identical besides this field)."""
    bud = budget_mod.compute(cat, task_answers)
    roles = check_spec(cat, spec, bud["check_required"])
    task: str = spec["task"]
    notes: list[str] = list(bud["notes"])
    forced = (force or {}).get("roles") or {}
    unknown = sorted(set(forced) - {r["name"] for r in roles})
    if unknown:
        raise PlanRefused(f"force: no role named {', '.join(unknown)}")
    reachable = {s["agent"] for s in catalog.reachable_seats(cat)}
    per_role = {r["name"]: _cells_for(cat, r, forced, notes) for r in roles}
    if bud["check_required"] and not any(per_role[r["name"]] for r in roles if r["kind"] == "check"):
        raise PlanRefused("missing kind check: the budget requires a check role and none has a cell")
    total = sum(len(c) for c in per_role.values())
    cap = cat["thresholds"]["max_cells"]
    limit = bud["dispatched_budget"]
    if allow is None:
        if total > cap:
            raise PlanRefused(f"cap: {total} cells exceed the cap of {cap}; pass --allow {total} to raise it")
        if total > limit:
            raise PlanRefused(f"budget: {total} cells exceed the dispatched budget of {limit} (budget {bud['budget']}, "
                              f"lead cell is the base session); pass --allow {total} to accept")
    elif total > allow:
        raise PlanRefused(f"allow: {total} cells exceed --allow {allow}")
    root = PurePosixPath(run_dir)
    # V2-2a: a check cell's <artifact_under_check> holds only the write scopes of writing roles, never an
    # earlier wave's record path, because a record carries a producer's hand-back (its conclusion), and a
    # check must stay blind to it. Non-check kinds keep receiving earlier wave record paths under <inputs>.
    check_paths = [p for r in roles if r["kind"] in ("produce", "integrate") for p in (r.get("scope") or [])]
    waves: list[dict[str, Any]] = []
    seats: list[dict[str, Any]] = []
    earlier: list[str] = []
    for kind in cat["wave_order"]:
        calls: list[dict[str, Any]] = []
        produced: list[str] = []
        for role in (r for r in roles if r["kind"] == kind):
            cells = per_role[role["name"]]
            slices = role.get("slices") or []
            spec_kind = cat["kinds"][kind]
            returns_val = _returns_of(role)
            for i, (model, effort) in enumerate(cells):
                many = len(cells) > 1
                cell_id = f"{role['name']}-{i + 1}" if many else role["name"]
                record_path = str(root / "roles" / role["name"] / (f"cell-{i + 1}" if many else "") /
                                  role["deliverable"]).replace("//", "/")
                record_path = str(PurePosixPath(os.path.normpath(record_path)))
                my_slices = slices[i::len(cells)] if slices and len(cells) > 1 else slices
                slice_: dict[str, Any] | None = None
                if my_slices:
                    slice_ = {"brief": " ".join(s["brief"] for s in my_slices),
                              "scope": [p for s in my_slices for p in (s.get("scope") or [])]}
                write_scope: list[str] = (slice_["scope"] if slice_ and slice_.get("scope")
                                          else role.get("scope") or [])
                planner_paths = check_paths if kind == "check" else list(earlier)
                prompt = _prompt(role, task, kind, write_scope, planner_paths, slice_, cell_id, returns_val)
                seat_file = catalog.seat_name(kind, model, effort)
                installed = seat_file in reachable
                if not installed:
                    notes.append(f"{cell_id}: seat {seat_file} is outside the installed seat set; "
                                 "use the headless argv")
                desc = role["name"] if not many else f"{role['name']} ({i + 1} of {len(cells)})"
                calls.append({
                    "cell_id": cell_id, "role": role["name"], "kind": kind, "model": model, "effort": effort,
                    "seat_file": seat_file, "seat_installed": installed, "deliverable": record_path,
                    "record_path": record_path, "returns": returns_val if kind != "check" else "verdict",
                    "agent_call": {"subagent_type": seat_file, "description": desc, "prompt": prompt},
                    "headless_argv": _headless(cat, role["name"], model, effort, spec_kind["tools"],
                                               spec_kind["body"], prompt),
                })
                row: dict[str, Any] = {"work_item_id": f"WI-{cell_id}", "task_type": spec_kind["task_type"],
                                       "mode": "CHILD_AGENT", "model": model, "agent": seat_file}
                if kind == "check":
                    row["role"] = "verifier"
                else:
                    row["composition"] = _composition(role)
                seats.append(row)
                produced.append(record_path)
        if calls:
            waves.append({"kind": kind, "calls": calls})
        earlier += produced
    produce_calls = [c for w in waves for c in w["calls"] if c["kind"] == "produce"]
    if produce_calls and all(c["model"] in ("opus", "fable") for c in produce_calls):
        notes.append("all produce cells seated on Opus or Fable: confirm each piece needs hard reasoning; "
                     "execution usually belongs on a Sonnet row (repeatable_task, bounded_retrieval, "
                     "everyday_implementation, long_running_agentic)")
    if len(waves) > 1:
        notes.append("record each hand-back a later wave reads with: crew record --transcript "
                     "<agent output file> --out <record_path>")
    given_sources = answer_sources or {}
    sources: dict[str, str] = {}
    if task_answers is not None:
        sources["task"] = given_sources.get("task", "declared")
    for role in roles:
        sources[role["name"]] = given_sources.get(role["name"], "declared")
    plan = {"task": task, "run_dir": str(root), "budget": bud, "cells": total,
            "models": sorted({c["model"] for w in waves for c in w["calls"]}),
            "efforts": sorted({str(c["effort"]) for w in waves for c in w["calls"]}),
            "waves": waves, "notes": notes, "answer_sources": sources}
    files = {"budget.json": bud, "roles.json": spec, "plan.json": plan, "seats.json": {"seats": seats}}
    return plan, files


def public_view(plan: dict[str, Any]) -> dict[str, Any]:
    view = {k: v for k, v in plan.items() if k != "waves"}
    view["waves"] = [{"kind": w["kind"], "calls": [{k: v for k, v in c.items() if k != "headless_argv"}
                                                   for c in w["calls"]]} for w in plan["waves"]]
    view["headless_argv_in"] = str(PurePosixPath(plan["run_dir"]) / "plan.json")
    return view
