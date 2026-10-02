"""crew command line. Exit codes: 0 ok, 1 drift or a found problem, 2 refused input or a bad catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crews import (
    bands,
    briefcheck,
    budget,
    catalog,
    egress,
    installer,
    judge,
    need_gate,
    planner,
    record,
)
from crews import skills as skills_mod

# request1/request2 egress packet metadata (C-CLI wiring; TSI-GATE-01, TSI-NEED-01).
_REQ1_PURPOSE = "task need classification (TypeSafe request 1)"
_REQ1_REQS = ["TSI-NEED-01"]
_REQ1_DATA_CATEGORY = "task_text"
_REQ2_PURPOSE = "per role need classification (TypeSafe request 2)"
_REQ2_REQS = ["TSI-VAL-01"]
_REQ2_DATA_CATEGORY = "role_definitions"

# JEV-16: the provenance fields crew install-band requires present, each refused by name when
# missing (cmd_install_band). JEV-16 names nine identity fields in total; the other four are
# covered without a dedicated required key here: "criteria fingerprint" is criteria_hash, already
# required above this list existed; "sample counts" is n_observations, which every calibrate-bands
# and round three output already always carries; "thresholds" and "Wilson bounds" live on the
# act/confirm entries themselves (threshold, wilson_lower), guaranteed present by the "act band
# status is MEASURED" check that runs right after this one, since derive_threshold's MEASURED
# shape always carries both. The ninth field, schema_version, is never required here: no round
# three artifact (manifest_r3.json, bands_r3.json, protocol.md, the observations) records one, and
# this module fabricates no provenance value that no artifact states. A --bands file that does
# carry a schema_version is still passed through unchanged.
_REQUIRED_PROVENANCE_FIELDS = ("model", "request_type", "criteria_hash", "dataset_fingerprint", "run_id")


def _emit(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _audit_dir() -> Path:
    raw = os.environ.get("CREWS_AUDIT_DIR")
    d = Path(raw).expanduser() if raw else Path.home() / ".crews" / "audit"
    d.mkdir(parents=True, exist_ok=True)
    return d


def audit(question_id: str, typed_answer: Any, case_or_role: Any, policy: str, consequence: Any,
          approval_ref: str) -> None:
    """TSI-AUD-01: one record per consequential semantic decision (need_gate, skill attach, hand back
    judging, brief leak). Only a typed answer, the case or role it applies to, the policy it was checked
    against, the consequence applied, and the approval reference (a payload fingerprint) are recorded: never
    the TypeSafe key, never raw response bytes."""
    rec = {"at": datetime.now(timezone.utc).isoformat(), "question_id": question_id, "typed_answer": typed_answer,
           "case_or_role": case_or_role, "policy": policy, "consequence": consequence,
           "approval_ref": approval_ref}
    with (_audit_dir() / "audit.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")


def _read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _load(path: str | None) -> dict[str, Any]:
    cat = catalog.load(path)
    defects = catalog.validate(cat)
    if defects:
        raise catalog.CatalogError("; ".join(defects))
    return cat


def _need_criteria_hash(cat: dict[str, Any]) -> str:
    """The sha256 hex digest of the catalog's need question criteria (JEV-16/JEV-17, round three
    protocol.md section 4: a band is only valid for the criteria version it was measured on).
    Serialization: json.dumps(criteria, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    over cat["questions"]["need"]["criteria"], so key order and incidental whitespace never
    change the hash and only the criteria text itself does."""
    criteria = ((cat.get("questions") or {}).get("need") or {}).get("criteria") or {}
    canonical = json.dumps(criteria, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cmd_validate(ns: argparse.Namespace) -> int:
    try:
        cat = catalog.load(ns.catalog)
    except (catalog.CatalogError, OSError) as exc:
        print(f"catalog refused: {exc}", file=sys.stderr)
        return 2
    defects = catalog.validate(cat)
    if defects:
        for d in defects:
            print(f"catalog refused: {d}", file=sys.stderr)
        return 2
    print(f"catalog ok: version {cat['version']}, {len(cat['needs'])} needs, {len(catalog.reachable_seats(cat))} seats")
    return 0


def core_list(cat: dict[str, Any]) -> dict[str, Any]:
    """The seat list, shared by `list --json` and the crew_catalog tool."""
    return {"version": cat["version"], "seats": catalog.reachable_seats(cat)}


def cmd_list(ns: argparse.Namespace) -> int:
    try:
        cat = _load(ns.catalog)
    except (catalog.CatalogError, OSError) as exc:
        print(f"catalog refused: {exc}", file=sys.stderr)
        return 2
    seats = catalog.reachable_seats(cat)
    if ns.json:
        _emit(core_list(cat))
    else:
        for s in seats:
            print(f"{s['agent']:40} {s['model']:7} {s['effort'] or '-':7} {','.join(s['tools'])}")
    return 0


def _agents_dir(ns: argparse.Namespace) -> Path:
    return Path(ns.agents_dir).expanduser()


def cmd_install(ns: argparse.Namespace) -> int:
    try:
        cat = _load(ns.catalog)
    except (catalog.CatalogError, OSError) as exc:
        print(f"catalog refused: {exc}", file=sys.stderr)
        return 2
    res = installer.install(cat, _agents_dir(ns))
    _emit(res.as_dict())
    for problem in res.problems:
        print(f"install: {problem}", file=sys.stderr)
    return 1 if res.problems else 0


def cmd_check(ns: argparse.Namespace) -> int:
    try:
        cat = _load(ns.catalog)
    except (catalog.CatalogError, OSError) as exc:
        print(f"catalog refused: {exc}", file=sys.stderr)
        return 2
    res = installer.check(cat, _agents_dir(ns))
    _emit(res.as_dict())
    for problem in res.problems:
        print(f"check: {problem}", file=sys.stderr)
    return 1 if res.problems else 0


def cmd_uninstall(ns: argparse.Namespace) -> int:
    try:
        cat = _load(ns.catalog)
    except (catalog.CatalogError, OSError) as exc:
        print(f"catalog refused: {exc}", file=sys.stderr)
        return 2
    _emit(installer.uninstall(cat, _agents_dir(ns), dry_run=ns.dry_run).as_dict())
    return 0


def read_json_arg(value: str) -> Any:
    """A JSON argument: a file path, or `-` for stdin."""
    text = sys.stdin.read() if value == "-" else Path(value).read_text(encoding="utf-8")
    return json.loads(text)


def write_run_file(run_dir: str | None, name: str, payload: Any) -> None:
    if run_dir:
        target = Path(run_dir).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _resolve_need_band(cat: dict[str, Any]) -> dict[str, Any]:
    """The band status live routing actually reads (JEV-16/JEV-17). Precedence, decided once
    here: a band installed in the catalog under typesafe.need_band (crew install-band's shape,
    {"act", "confirm", "provenance", ...}) wins over the calibration directory file, because the
    catalog is the artifact that ships and the installed band is what an operator deliberately
    put there; a calibration directory file is left over from the development workflow that
    produced it and is a fallback, kept so that workflow still works with no installed band. The
    drift check in crews.need_gate.gate_need applies identically to either source, since both are
    passed through unchanged."""
    installed = (cat.get("typesafe") or {}).get("need_band")
    if isinstance(installed, dict) and "act" in installed:
        return installed
    return bands.bands_status(bands.calibration_dir() / "bands.json")


def _resolve_role_band(cat: dict[str, Any]) -> dict[str, Any]:
    """The band status the live per role routing reads (JEV-12), mirroring _resolve_need_band above: a
    band installed in the catalog under typesafe.role_band wins over the calibration directory file
    crew calibrate-bands-roles writes by default (bands_roles.json), kept separate from bands.json so
    request 1 and request 2 observations never pool into one measurement (calibration observations
    carry kind "need" versus "need_role"). With neither source the status is UNCALIBRATED
    (bands.bands_status's own no admission from absence rule), which is the case today: no role band has
    been measured or installed yet, so gate_need on a role answer always returns "uncalibrated"."""
    installed = (cat.get("typesafe") or {}).get("role_band")
    if isinstance(installed, dict) and "act" in installed:
        return installed
    return bands.bands_status(bands.calibration_dir() / "bands_roles.json")


def _live_task_answers(cat: dict[str, Any], task: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Request 1, gated through the owner approval ledger (egress.gated_call, TSI-GATE-01) and, once
    answered, routed through the confidence gated need decision (crews.need_gate, TSI-NEED-01). An
    owner_gate or a failed call gives None (the outage budget); a reject or uncalibrated need_gate decision
    gives None as well, each with a reason that holds no secret."""
    body = judge.request1(cat, task)
    outcome = egress.gated_call(body, purpose=_REQ1_PURPOSE, requirement_ids=_REQ1_REQS,
                                data_category=_REQ1_DATA_CATEGORY, includes_worker_output=False,
                                decline_behavior="the outage budget stands; task not classified")
    if outcome["state"] == "owner_gate":
        return None, {"status": "owner_gate", "reason": "awaiting owner approval",
                     "owner_gates": [outcome["packet"]]}
    if outcome["state"] == "failed":
        return None, {"status": "outage", "reason": outcome["reason"]}
    try:
        ans = judge.parse1(cat, outcome["response"])
    except judge.JudgeParseError as exc:
        return None, {"status": "outage", "reason": str(exc)}
    info: dict[str, Any] = {"status": "ok", **judge.meta(outcome["response"])}
    band_status = _resolve_need_band(cat)
    # JEV-16: the model that actually answered this call (info["model"], from judge.meta) is known
    # only now, after the response is parsed, so it is passed to gate_need at this same call site
    # rather than earlier: a band measured against a different model must never admit or confirm.
    decision = need_gate.gate_need(ans["need"], band_status, criteria_hash=_need_criteria_hash(cat),
                                   model=info.get("model"))
    info["need_gate"] = decision
    audit("need", ans["need"], "budget",
         f"need_gate threshold_act={decision['threshold_act']} threshold_confirm={decision['threshold_confirm']}",
         decision["decision"], egress.fingerprint(body))
    if decision["decision"] == "admit":
        return ans, info
    if decision["decision"] == "confirm":
        info["confirm_need"] = True
        return ans, info
    return None, info


def _live_role_answers(cat: dict[str, Any], task: str, roles: list[dict[str, Any]]
                       ) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """Request 2, gated through the owner approval ledger. Any refusal gives None, so the declared per role
    answers stand (a whole batch "fallback", the caller's concern). On success this reads the response
    through judge.parse2_confidence rather than parse2, so each role's need answer carries the confidence
    a role level band gate (JEV-12, crews.need_gate.gate_need) needs; the caller gates each role's need
    answer itself and flattens it back to parse2's plain choice shape once gated. info["fingerprint"]
    (present only on a "status": "ok" outcome) is this call's body fingerprint, so the caller's per role
    need_gate audit records carry the approval reference the request itself was authorized under."""
    body = judge.request2(cat, task, roles)
    outcome = egress.gated_call(body, purpose=_REQ2_PURPOSE, requirement_ids=_REQ2_REQS,
                                data_category=_REQ2_DATA_CATEGORY, includes_worker_output=False,
                                decline_behavior="declared per role answers stand")
    if outcome["state"] == "owner_gate":
        return None, {"status": "owner_gate", "reason": "awaiting owner approval",
                     "owner_gates": [outcome["packet"]]}
    if outcome["state"] == "failed":
        return None, {"status": "outage", "reason": outcome["reason"]}
    try:
        judged = judge.parse2_confidence(cat, outcome["response"], len(roles))
    except judge.JudgeParseError as exc:
        return None, {"status": "outage", "reason": str(exc)}
    return judged, {"status": "ok", "fingerprint": egress.fingerprint(body), **judge.meta(outcome["response"])}


def resolve_task_answers(cat: dict[str, Any], judge_mode: str, answers: Any, outage: bool, task: str | None
                         ) -> tuple[Any, dict[str, Any] | None]:
    """Declared answers, the outage flag, or the live judge. Only the live branch touches the environment,
    and only through egress.gated_call."""
    if judge_mode == "live":
        if answers is not None or outage:
            raise planner.PlanRefused("judge live asks the judge; drop the declared answers and the outage flag")
        if not task:
            raise planner.PlanRefused("judge live needs the task text")
        return _live_task_answers(cat, task)
    if outage:
        return None, None
    if answers is None:
        raise planner.PlanRefused("give declared answers, the outage flag, or judge live")
    defects = budget.check_answers(cat, answers)
    if defects:
        raise planner.PlanRefused("answers: " + "; ".join(defects))
    return answers, None


def _judge_notes(info: dict[str, Any] | None, notes: list[str], owner_gates: list[dict[str, Any]]) -> None:
    """The common handling of a resolve_task_answers() live info dict: outage, owner_gate, and need_gate
    confirm/reject/uncalibrated notes, plus owner_gates collection. Shared by core_budget and core_plan.
    A no-op when info is None (the declared or outage path was used, never the live judge)."""
    if info is None:
        return
    if info["status"] == "owner_gate":
        notes.append("judge owner_gate: awaiting owner approval; the outage budget stands")
        owner_gates.extend(info.get("owner_gates") or [])
    elif info["status"] != "ok":
        notes.append(f"judge outage: {info['reason']}")
    ng = info.get("need_gate")
    if info.get("confirm_need"):
        notes.append(f"need_gate confirm: {ng['reason']}" if ng else "need_gate confirm")
    elif ng and ng["decision"] in ("reject", "uncalibrated"):
        notes.append(f"need_gate {ng['decision']}: {ng['reason']}")


def core_budget(cat: dict[str, Any], *, answers: Any, outage: bool, judge_mode: str, task: str | None,
                run_dir: str | None) -> dict[str, Any]:
    """The budget, shared by the budget verb and the crew_budget tool. Raises PlanRefused on refused input."""
    ans, info = resolve_task_answers(cat, judge_mode, answers, outage, task)
    result = budget.compute(cat, ans)
    owner_gates: list[dict[str, Any]] = []
    _judge_notes(info, result["notes"], owner_gates)
    if info is not None:
        result["judge"] = info
        if info.get("confirm_need"):
            result["confirm_need"] = True
    if owner_gates:
        result["owner_gates"] = owner_gates
    write_run_file(run_dir, "budget.json", result)
    return result


def cmd_budget(ns: argparse.Namespace) -> int:
    try:
        cat = _load(ns.catalog)
        answers = read_json_arg(ns.answers) if ns.answers else None
        result = core_budget(cat, answers=answers, outage=ns.outage, judge_mode=ns.judge, task=ns.task,
                             run_dir=ns.run_dir)
    except (catalog.CatalogError, OSError, json.JSONDecodeError, planner.PlanRefused) as exc:
        print(f"budget refused: {exc}", file=sys.stderr)
        return 2
    _emit(result)
    return 0


def cmd_parse(ns: argparse.Namespace) -> int:
    try:
        cat = _load(ns.catalog)
        response = read_json_arg(ns.response)
        parsed: Any = judge.parse2(cat, response, ns.roles) if ns.roles else judge.parse1(cat, response)
    except (catalog.CatalogError, OSError, json.JSONDecodeError) as exc:
        print(f"parse refused: {exc}", file=sys.stderr)
        return 2
    except judge.JudgeParseError as exc:
        print(f"parse refused: {exc}", file=sys.stderr)
        return 2
    _emit(parsed)
    return 0


def run_plan(cat: dict[str, Any], answers: Any, spec: Any, run_dir: str, allow: int | None,
             force: Any, notes: list[str] | None = None,
             answer_sources: dict[str, str] | None = None) -> dict[str, Any]:
    """Plan, then write the run files. Raises PlanRefused before any file is written."""
    if answers is not None:
        defects = budget.check_answers(cat, answers)
        if defects:
            raise planner.PlanRefused("answers: " + "; ".join(defects))
    plan, files = planner.build(cat, answers, spec, str(Path(run_dir).expanduser()), allow=allow, force=force,
                                answer_sources=answer_sources)
    if notes:
        plan["notes"] = notes + plan["notes"]
    for name, payload in files.items():
        write_run_file(run_dir, name, payload)
    return planner.public_view(plan)


def _check_brief_leak_floor(spec: Any, task: str | None) -> None:
    """CR-2, TSI-BL-01: the regex floor over every check role's authored text, always on, no network. A hit
    refuses the plan naming the role; a clean scan is silent."""
    if not (isinstance(spec, dict) and isinstance(spec.get("roles"), list)):
        return
    for role in spec["roles"]:
        if isinstance(role, dict) and role.get("kind") == "check":
            hits = briefcheck.regex_hits(briefcheck.authored_text(role, str(task or "")))
            if hits:
                raise planner.PlanRefused(f"role {role.get('name')}: brief leak regex floor hit: "
                                          f"{'; '.join(hits[:5])}")


def _apply_wide_rank(sr: dict[str, Any], task: str, roles: list[dict[str, Any]], index: list[dict[str, str]],
                     candidates: list[list[dict[str, str]]], notes: list[str],
                     owner_gates: list[dict[str, Any]]) -> list[list[dict[str, str]]]:
    """TypeSafe cookbook skill_suggestion.md wide ranking stage: one gated request over the whole
    installed skill index whose per-role top picks are merged onto the BM25 candidates already
    computed for that role. Gated through the same owner-approval ledger as the main skill routing
    request, on its own fingerprint and its own purpose string, per TSI-SKL egress discipline; never
    bypassed. Falls back to the BM25 candidates unchanged whenever the wide call is not answered
    (owner_gate, transport failure, or a malformed response), each case leaving one note behind so an
    operator reading stdout can tell a fallback happened and why."""
    wide_cfg = sr.get("wide_rank") or {}
    if not wide_cfg.get("enabled"):
        return candidates
    wide_k = int(wide_cfg.get("top_k", 5))
    desc_chars = int(wide_cfg.get("desc_chars", 160))
    body = skills_mod.wide_request(task, roles, index, desc_chars=desc_chars)
    outcome = egress.gated_call(body, purpose="per role skill routing wide rank (TypeSafe request)",
                                requirement_ids=["TSI-SKL-02"], data_category=skills_mod.DATA_CATEGORY,
                                includes_worker_output=skills_mod.INCLUDES_WORKER_OUTPUT,
                                decline_behavior="skill routing wide rank skipped; BM25 candidates only")
    if outcome["state"] == "owner_gate":
        owner_gates.append(outcome["packet"])
        notes.append("skill routing wide rank: owner_gate; BM25 candidates only")
        return candidates
    if outcome["state"] == "failed":
        notes.append(f"skill routing wide rank outage: {outcome['reason']}; BM25 candidates only")
        return candidates
    try:
        wide_ids = skills_mod.wide_parse(outcome["response"], roles, index)
    except judge.JudgeParseError as exc:
        notes.append(f"skill routing wide rank outage: {exc}; BM25 candidates only")
        return candidates
    notes.append(f"skill routing wide rank: merged up to {wide_k} wide pick(s) per role onto BM25 candidates")
    return [skills_mod.merge_candidates(cands, ids, index, wide_k) for cands, ids in zip(candidates, wide_ids)]


def _apply_skill_routing(cat: dict[str, Any], spec: dict[str, Any], notes: list[str],
                         owner_gates: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """TSI-SKL-01..05: gated per role skill routing. With no approval or no answer, the spec is returned
    unchanged and the status is UNVERIFIED (never a silent attach)."""
    sr = (cat.get("typesafe") or {}).get("skill_routing") or {}
    globs = sr.get("root_globs") or list(skills_mod.DEFAULT_GLOBS)
    max_candidates = int(sr.get("max_candidates", 6))
    thresholds = sr.get("thresholds") or {"fit": 0.30, "gate": 0.30}
    # JEV-17/DEFECT TWO (skill-routing-legibility-and-default-repairer, 2026-09-22): fail closed on an
    # absent calibrated key. crews.catalog.validate requires every typesafe.*.calibrated key to be a
    # boolean, so a catalog that reached this point through crews.cli._load already carries a real
    # value; but core_plan (the shared function the MCP crew_plan tool and this test suite both call
    # directly) accepts a catalog dict with no such validation ever having run, so this default is the
    # actual last line of defense, not a value the validator makes unreachable. Absence must never grant
    # acting authority, the same principle the eight prior JEV closures on normalize_identity and its
    # callers established for an absent or unverifiable identity: default to False (non acting,
    # recorded-only), never True.
    calibrated = sr.get("calibrated", False)
    roles: list[dict[str, Any]] = spec["roles"]
    task = str(spec.get("task") or "")
    index = skills_mod.enumerate_skills(globs)
    # Session level skills (already run for every session through hooks, e.g. memory-first,
    # using-superpowers) must never reach a candidate list or a wide ranking question; dropped
    # once here, before prefilter and before the wide request, per typesafe.skill_routing.
    # exclude_skills in crews/catalog.json (owner approved 2026-09-23).
    index = skills_mod.routable(index, list(sr.get("exclude_skills") or []))
    candidates = [skills_mod.prefilter(role, index, max_candidates) for role in roles]
    candidates = _apply_wide_rank(sr, task, roles, index, candidates, notes, owner_gates)
    if not any(candidates):
        return spec, {"status": "off", "reason": "no candidate skill shares a word with any role"}
    body = skills_mod.request(task, roles, candidates)
    outcome = egress.gated_call(body, purpose="per role skill routing (TypeSafe request)",
                                requirement_ids=["TSI-SKL-02"], data_category=skills_mod.DATA_CATEGORY,
                                includes_worker_output=skills_mod.INCLUDES_WORKER_OUTPUT,
                                decline_behavior="skill routing skipped; roles unchanged")
    if outcome["state"] == "owner_gate":
        owner_gates.append(outcome["packet"])
        notes.append("skill routing: owner_gate; roles unchanged")
        return spec, {"status": "UNVERIFIED", "reason": "awaiting owner approval"}
    if outcome["state"] == "failed":
        notes.append(f"skill routing outage: {outcome['reason']}")
        return spec, {"status": "UNVERIFIED", "reason": outcome["reason"]}
    try:
        parsed = skills_mod.parse(outcome["response"], roles, candidates)
    except judge.JudgeParseError as exc:
        notes.append(f"skill routing outage: {exc}")
        return spec, {"status": "UNVERIFIED", "reason": str(exc)}
    new_roles = list(roles)
    attached: dict[str, str] = {}
    # DEFECT ONE (skill-routing-legibility-and-default-repairer, 2026-09-22): `decision`, the per role
    # dict `skills_mod.decide` already returns with `acted`, `reason`, and (when uncalibrated)
    # `observed_decision`, is now carried all the way to the view rather than discarded once
    # `decision["attach"]` has been read, mirroring `_apply_brief_check`'s `per_check` and
    # `cmd_judge_handback`'s `observed_decision`/`observed_nouls`/`note`: an operator reading stdout
    # must be able to tell an uncalibrated observation apart from a calibrated decision, and must never
    # be told a role failed to meet a threshold it actually cleared.
    per_role: dict[str, Any] = {}
    fp = egress.fingerprint(body)
    for i, (role, cands, parsed_role) in enumerate(zip(roles, candidates, parsed)):
        decision = skills_mod.decide(parsed_role, thresholds, calibrated=calibrated)
        audit(f"skill.{i}", parsed_role, role.get("name"), f"skill_routing thresholds {thresholds}",
             decision["reason"], fp)
        per_role[str(role.get("name"))] = decision
        if decision["attach"] is None:
            continue
        chosen = next((c for c in cands if c["id"] == decision["attach"]), None)
        if chosen is None:
            continue
        new_roles[i] = skills_mod.attach(role, chosen, index)
        attached[str(role.get("name"))] = chosen["id"]
    if not calibrated:
        notes.append("skill routing: uncalibrated skill_routing thresholds (JEV-17): TypeSafe was "
                    "called and each role's verdict was recorded, but none were applied; see "
                    "typesafe.skills.per_role for what was observed")
    elif attached:
        notes.append(f"skill routing: attached {len(attached)} of {len(roles)} role(s)")
    else:
        notes.append("skill routing: no role met the attach thresholds")
    return {**spec, "roles": new_roles}, {"status": "applied", "attached": attached, "per_role": per_role}


def _apply_brief_check(cat: dict[str, Any], view: dict[str, Any], notes: list[str],
                       owner_gates: list[dict[str, Any]]) -> dict[str, Any]:
    """TSI-BL-03: the semantic leak Noul over every check role's rendered brief, off by default. CR-2: the
    regex floor never runs on the rendered brief (it already ran on every check role's authored text in
    `_check_brief_leak_floor`, before the plan was built); the fixed output contract line every rendered
    check brief carries (`VERDICT: PASS|FAIL|BLOCKED`, planner.py) matches the LEAK regex by construction, so
    re-running it here would refuse every plan. Only the semantic Noul reads the rendered text."""
    brief_leak_cfg = (cat.get("typesafe") or {}).get("brief_leak", {})
    threshold = brief_leak_cfg.get("leak_threshold", 0.5)
    # JEV-17/DEFECT TWO: fail closed on an absent calibrated key (see the matching comment in
    # _apply_skill_routing above); default False, never True.
    calibrated = brief_leak_cfg.get("calibrated", False)
    check_calls = [c for w in view.get("waves", []) for c in w.get("calls", []) if c.get("kind") == "check"]
    results: dict[str, Any] = {}
    for call in check_calls:
        brief = call["agent_call"]["prompt"]
        body = briefcheck.request(brief)
        outcome = egress.gated_call(body, purpose="check brief leak, semantic Noul", requirement_ids=["TSI-BL-03"],
                                    data_category=briefcheck.DATA_CATEGORY,
                                    includes_worker_output=briefcheck.INCLUDES_WORKER_OUTPUT,
                                    decline_behavior="brief leak status stays UNVERIFIED")
        if outcome["state"] == "owner_gate":
            owner_gates.append(outcome["packet"])
            results[call["cell_id"]] = briefcheck.decide([], None, threshold, calibrated=calibrated)
            continue
        if outcome["state"] == "failed":
            notes.append(f"brief leak check outage on {call['cell_id']}: {outcome['reason']}")
            results[call["cell_id"]] = briefcheck.decide([], None, threshold, calibrated=calibrated)
            continue
        response = outcome["response"]
        ans = response.get("answers") if isinstance(response, dict) else None
        leak_ans = ans.get("leak") if isinstance(ans, dict) else None
        noul = leak_ans.get("noul") if isinstance(leak_ans, dict) else None
        decision = briefcheck.decide([], noul, threshold, calibrated=calibrated)
        audit("leak", noul, call["cell_id"], f"brief_leak threshold {threshold}", decision["status"],
             egress.fingerprint(body))
        results[call["cell_id"]] = decision
    if not results:
        return {"status": "off", "reason": "no check role in this plan"}
    if any(r["status"] == "LEAK" for r in results.values()):
        overall = "LEAK"
    elif any(r["status"] == "UNVERIFIED" for r in results.values()):
        overall = "UNVERIFIED"
    else:
        overall = "CLEAN"
    return {"status": overall, "per_check": results}


def core_plan(cat: dict[str, Any], *, spec: Any, answers: Any, outage: bool, judge_mode: str, run_dir: str,
              allow: int | None, force: Any, skills_mode: str = "off", brief_check_mode: str = "off"
             ) -> dict[str, Any]:
    """The plan, shared by the plan verb and the crew_plan tool. Raises PlanRefused before any file is written.
    skills_mode and brief_check_mode are each "off" (default) or "live"; off leaves the plan byte identical
    to the plan without this mission's wiring (TSI-SKL-05, TSI-BL-04)."""
    task = spec.get("task") if isinstance(spec, dict) else None
    _check_brief_leak_floor(spec, task)
    task_answers, info = resolve_task_answers(cat, judge_mode, answers, outage, task)
    notes: list[str] = []
    owner_gates: list[dict[str, Any]] = []
    _judge_notes(info, notes, owner_gates)
    # Phase 9 step 8 / token accounting: request 1's answering model and usage (judge.meta, carried
    # in info by _live_task_answers) reach core_budget's budget.json today (result["judge"] = info)
    # but never a live crew_plan / `crew plan --judge live` run, so a production plan call's spend was
    # unrecoverable after the process exited. Persist it the same way JEV-14 already persists request
    # 2's usage below: a file under the run dir, named on the info dict as usage_file, so the usage
    # survives past the single process that printed the view. Guarded on status == "ok" because that
    # is the only state judge.meta ever actually answered from (owner_gate, outage, and the declared/
    # outage non-live paths all carry no real response to report usage for).
    if info is not None and info.get("status") == "ok":
        usage_file = "request1_usage.json"
        write_run_file(run_dir, usage_file, {"answering_model": info.get("model"), "usage": info.get("usage")})
        info["usage_file"] = usage_file
    # JEV-15: the source of every answer this plan actually used. "task" is present only when a task
    # answer was actually used to compute the budget (resolve_task_answers gives None on reject,
    # uncalibrated, outage, or owner_gate, so there is nothing to attribute a source to then). A role
    # name is added only on the live path below; planner.build defaults every other role to "declared".
    answer_sources: dict[str, str] = {}
    if task_answers is not None:
        ng = info.get("need_gate") if info is not None else None
        if info is None:
            answer_sources["task"] = "declared"
        elif info.get("confirm_need"):
            answer_sources["task"] = "live_confirmed"
        elif ng is not None and ng.get("decision") == "admit":
            answer_sources["task"] = "live_admitted"
        else:
            answer_sources["task"] = "declared"
    typesafe_info: dict[str, Any] = {}
    if info is not None:
        # Mirrors role_judge below for request 1: the view is not the only surface that must carry
        # usage (the file does that), but a reader of the view should not have to open a second file
        # to see which model answered and whether this call even reached the network.
        typesafe_info["task_judge"] = info
    if judge_mode == "live" and isinstance(spec, dict) and isinstance(spec.get("roles"), list):
        judged, rinfo = _live_role_answers(cat, str(task), spec["roles"])
        # JEV-14: the request 2 answering model and token usage (judge.meta, carried in rinfo) are kept
        # on the view rather than discarded once judged answers are applied, and also persisted to a file
        # under the run dir so the usage survives past the single process that printed the view.
        if rinfo.get("status") == "ok":
            usage_file = "request2_usage.json"
            write_run_file(run_dir, usage_file,
                           {"answering_model": rinfo.get("model"), "usage": rinfo.get("usage")})
            rinfo["usage_file"] = usage_file
        typesafe_info["role_judge"] = rinfo
        if judged is not None:
            # JEV-12: each role's live judged need answer is gated the same way the task level need
            # answer is (crews.need_gate.gate_need), against a role level band kept separate from the
            # task band (_resolve_role_band, bands_roles.json, never pooled with need's bands.json).
            # With no measured role band the decision is always "uncalibrated" and that role's own
            # declared answer stands unchanged, never the live one.
            role_band = _resolve_role_band(cat)
            criteria_hash = _need_criteria_hash(cat)
            # JEV-16: the request 2 answering model (rinfo["model"], from judge.meta) is known once
            # the batched role response returns, and is the same model every role's need answer in
            # this response carries, so it is passed once here rather than per role.
            answering_model = rinfo.get("model")
            fp = rinfo.get("fingerprint", "")
            new_roles: list[dict[str, Any]] = []
            for role, jr in zip(spec["roles"], judged):
                name = str(role.get("name"))
                need_ans = jr["need"]
                decision = need_gate.gate_need(need_ans, role_band, criteria_hash=criteria_hash,
                                               model=answering_model)
                audit(f"need_role:{name}", need_ans, name,
                     f"need_gate threshold_act={decision['threshold_act']} "
                     f"threshold_confirm={decision['threshold_confirm']}", decision["decision"], fp)
                if decision["decision"] in ("admit", "confirm"):
                    flat = {"need": need_ans["choice"], "difficulty": jr["difficulty"],
                            "divisible": jr["divisible"], "specific": jr["specific"]}
                    new_roles.append({**role, "answers": flat})
                    if decision["decision"] == "admit":
                        answer_sources[name] = "live_admitted"
                    else:
                        answer_sources[name] = "live_confirmed"
                        notes.append(f"need_gate confirm ({name}): {decision['reason']}")
                else:
                    new_roles.append(role)
                    answer_sources[name] = "fallback"
                    notes.append(f"need_gate {decision['decision']} ({name}): {decision['reason']}")
            spec = {**spec, "roles": new_roles}
        else:
            notes.append(f"role judge outage: {rinfo['reason']}; declared per role answers stand")
            if rinfo["status"] == "owner_gate":
                owner_gates.extend(rinfo.get("owner_gates") or [])
            if isinstance(spec, dict) and isinstance(spec.get("roles"), list):
                for role in spec["roles"]:
                    answer_sources[str(role.get("name"))] = "fallback"
    if skills_mode == "live":
        if isinstance(spec, dict) and isinstance(spec.get("roles"), list) and spec["roles"]:
            spec, typesafe_info["skills"] = _apply_skill_routing(cat, spec, notes, owner_gates)
        else:
            typesafe_info["skills"] = {"status": "off", "reason": "spec carries no roles"}
    view = run_plan(cat, task_answers, spec, run_dir, allow, force, notes, answer_sources=answer_sources)
    if brief_check_mode == "live":
        after_build: list[str] = []
        typesafe_info["brief_check"] = _apply_brief_check(cat, view, after_build, owner_gates)
        if after_build:
            view["notes"] = list(view.get("notes") or []) + after_build
    if owner_gates:
        view["owner_gates"] = owner_gates
    if typesafe_info:
        view["typesafe"] = typesafe_info
    return view


def cmd_plan(ns: argparse.Namespace) -> int:
    try:
        cat = _load(ns.catalog)
        spec = read_json_arg(ns.roles)
        answers = read_json_arg(ns.answers) if ns.answers else None
        force = read_json_arg(ns.force) if ns.force else None
        view = core_plan(cat, spec=spec, answers=answers, outage=ns.outage, judge_mode=ns.judge, run_dir=ns.run_dir,
                         allow=ns.allow, force=force, skills_mode=ns.skills, brief_check_mode=ns.brief_check)
    except (catalog.CatalogError, OSError, json.JSONDecodeError, planner.PlanRefused) as exc:
        print(f"plan refused: {exc}", file=sys.stderr)
        return 2
    _emit(view)
    return 0




def cmd_record(ns: argparse.Namespace) -> int:
    try:
        text, source = record.extract(Path(ns.transcript).expanduser())
        out = Path(ns.out).expanduser()
        written = record.write(text, out, ns.force)
    except record.RecordError as exc:
        print(f"record refused: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"record refused: {exc}", file=sys.stderr)
        return 1
    _emit({"out": str(out), "bytes": written, "source": source})
    return 0












def cmd_approve(ns: argparse.Namespace) -> int:
    try:
        egress.approve(ns.call_id, ns.fingerprint)
    except ValueError as exc:
        print(f"approve refused: {exc}", file=sys.stderr)
        return 2
    _emit({"approved": ns.call_id, "fingerprint": ns.fingerprint})
    return 0


def cmd_approve_batch(ns: argparse.Namespace) -> int:
    try:
        egress.approve_batch(ns.manifest, ns.sha256)
    except (ValueError, OSError) as exc:
        print(f"approve-batch refused: {exc}", file=sys.stderr)
        return 2
    _emit({"approved_manifest": ns.manifest, "sha256": ns.sha256})
    return 0




def cmd_gates(ns: argparse.Namespace) -> int:
    """List every pending TypeSafe call awaiting owner approval, each marked whether it already holds one."""
    d = egress._dir()  # noqa: SLF001  the ledger directory resolver carries no public alias (C-EGR)
    pending = _read_ledger(d / "pending.jsonl")
    approved_ids = {r["call_id"] for r in _read_ledger(d / "approvals.jsonl")}
    consumed_ids = {r["call_id"] for r in _read_ledger(d / "consumed.jsonl")}
    open_gates = [{**p, "approved": p["call_id"] in approved_ids} for p in pending
                 if p["call_id"] not in consumed_ids]
    _emit({"gates": open_gates})
    return 0
























def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="crew", description="Deterministic Claude crew configurations")
    ap.add_argument("--catalog", default=None, help="catalog path (default: the shipped catalog)")
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("validate", help="check the catalog").set_defaults(fn=cmd_validate)
    p = sub.add_parser("list", help="list every reachable seat")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_list)
    for verb, fn, text in (("install", cmd_install, "write the seat agent files"),
                           ("check", cmd_check, "report drift between the agent files and the catalog"),
                           ("uninstall", cmd_uninstall, "remove generated seat files only")):
        p = sub.add_parser(verb, help=text)
        p.add_argument("--agents-dir", required=True)
        if verb == "uninstall":
            p.add_argument("--dry-run", action="store_true")
        p.set_defaults(fn=fn)
    p = sub.add_parser("budget", help="cell budget from four typed task answers")
    p.add_argument("--answers", help="answers JSON file, or - for stdin")
    p.add_argument("--outage", action="store_true", help="the judge was unavailable")
    p.add_argument("--task", default=None, help="task text for --judge live")
    p.add_argument("--judge", choices=["off", "live"], default="off")
    p.add_argument("--run-dir", default=None)
    p.set_defaults(fn=cmd_budget)
    p = sub.add_parser("parse", help="parse a recorded TypeSafe response against its questions")
    p.add_argument("--response", required=True, help="response JSON file, or - for stdin")
    p.add_argument("--roles", type=int, default=0, help="parse a request 2 response for N roles")
    p.set_defaults(fn=cmd_parse)
    p = sub.add_parser("plan", help="plan task specific roles into seated cells")
    p.add_argument("--roles", required=True, help="roles JSON file, or - for stdin")
    p.add_argument("--answers", help="task answers JSON file")
    p.add_argument("--outage", action="store_true", help="the judge was unavailable")
    p.add_argument("--judge", choices=["off", "live"], default="off")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--allow", type=int, default=None, help="accept up to N cells past the budget or the cap")
    p.add_argument("--force", default=None, help="force JSON: {roles: {name: {model, efforts, workers}}}")
    p.add_argument("--skills", choices=["off", "live"], default="off", help="gated per role skill routing")
    p.add_argument("--brief-check", dest="brief_check", choices=["off", "live"], default="off",
                   help="gated semantic check brief leak Noul, on top of the always-on regex floor")
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("record", help="extract a subagent hand-back from a transcript and write it as a record")
    p.add_argument("--transcript", required=True, help="the subagent JSONL transcript file")
    p.add_argument("--out", required=True, help="the record file to write")
    p.add_argument("--force", action="store_true", help="overwrite an existing record file")
    p.set_defaults(fn=cmd_record)
    p = sub.add_parser("approve", help="approve one pending TypeSafe call")
    p.add_argument("call_id")
    p.add_argument("--fingerprint", required=True)
    p.set_defaults(fn=cmd_approve)
    p = sub.add_parser("approve-batch", help="approve every call of an enumerated calibration manifest")
    p.add_argument("--manifest", required=True)
    p.add_argument("--sha256", required=True)
    p.set_defaults(fn=cmd_approve_batch)
    p = sub.add_parser("gates", help="list every pending TypeSafe call awaiting owner approval")
    p.set_defaults(fn=cmd_gates)
    return ap


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    result: int = ns.fn(ns)
    return result
