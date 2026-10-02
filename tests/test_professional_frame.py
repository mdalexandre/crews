"""Piece F: professional_frame on a role, its rendering, its refusals, require_frame, and judge request2."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from crews import catalog, judge, planner

CAT = catalog.load()
TASK = "build a payments ledger reconciliation module with idempotent posting"


def task_answers(need: str = "everyday_implementation", verifier: float = 0.1) -> dict[str, Any]:
    return {"need": {"choice": need, "confidence": 0.9, "probabilities": {need: 0.9}},
            "divisible": {"noul": 0.1}, "needs_verifier": {"noul": verifier}, "difficulty": {"score": 1.0}}


def produce_role(frame: dict[str, Any] | None = None, name: str = "ledger-reconciliation-builder") -> dict[str, Any]:
    role: dict[str, Any] = {
        "name": name,
        "kind": "produce",
        "mission": "Build the payments ledger reconciliation module with idempotent posting",
        "deliverable": "out.md",
        "scope": ["ledger/reconcile.py"],
        "acceptance": ["ledger totals reconcile"],
        "answers": {"need": "everyday_implementation", "difficulty": 1.0, "divisible": 0.1, "specific": 0.9},
    }
    if frame is not None:
        role["professional_frame"] = frame
    return role


def check_role(frame: dict[str, Any] | None = None, name: str = "ledger-reconciliation-checker") -> dict[str, Any]:
    role: dict[str, Any] = {
        "name": name,
        "kind": "check",
        "mission": "Check the payments ledger reconciliation module posted idempotently",
        "deliverable": "out.md",
        "criteria": ["ledger totals reconcile"],
        "answers": {"need": "everyday_implementation", "difficulty": 1.0, "divisible": 0.1, "specific": 0.9},
    }
    if frame is not None:
        role["professional_frame"] = frame
    return role


def spec(*roles: dict[str, Any]) -> dict[str, Any]:
    return {"task": TASK, "roles": list(roles)}


def prompt_for(cat: dict[str, Any], role: dict[str, Any], run_dir: str = "/tmp/run") -> str:
    plan, _files = planner.build(cat, task_answers(), spec(role), run_dir)
    calls = [c for w in plan["waves"] for c in w["calls"]]
    assert len(calls) == 1
    result: str = calls[0]["agent_call"]["prompt"]
    return result


FULL_FRAME = {
    "profession": "payments reconciliation engineer",
    "standards": ["idempotent posting", "no float in money math"],
    "method": ["trace every charge to its ledger row before touching a refund"],
    "watch_for": ["silently summed currencies", "banker's rounding where the spec says half up"],
}


def test_render_with_all_lists() -> None:
    role = produce_role(frame=FULL_FRAME)
    prompt = prompt_for(CAT, role)
    assert prompt.startswith(f'<crew_task version="2" kind="produce" role="{role["name"]}" cell="{role["name"]}">\n'
                             f"<objective>\n{role['mission']}\n</objective>\n"
                             f"<context>\nCrew task: {TASK}\n</context>\n")
    assert ("<capability_cue>\npayments reconciliation engineer. Lenses: silently summed currencies; "
           "banker's rounding where the spec says half up.\n</capability_cue>") in prompt
    assert ("<authority>\n- idempotent posting\n- no float in money math\n"
           "- Invent no requirement beyond these sources. If they are not enough to decide, stop BLOCKED "
           "and name what is missing.\n</authority>") in prompt
    assert ("<execution>\n- trace every charge to its ledger row before touching a refund\n</execution>") in prompt


def test_render_profession_only() -> None:
    role = produce_role(frame={"profession": "payments reconciliation engineer"})
    prompt = prompt_for(CAT, role)
    assert "<capability_cue>\npayments reconciliation engineer.\n</capability_cue>" in prompt
    assert "Lenses:" not in prompt
    assert ("<authority>\n- The objective above and the files under inputs.\n"
           "- Invent no requirement beyond these sources.") in prompt


def test_byte_identical_prompt_with_no_frame() -> None:
    role = produce_role(frame=None)
    prompt = prompt_for(CAT, role)
    expected = "\n".join([
        f'<crew_task version="2" kind="produce" role="{role["name"]}" cell="{role["name"]}">',
        "<objective>", role["mission"], "</objective>",
        "<context>", f"Crew task: {TASK}", "</context>",
        "<authority>", "- The objective above and the files under inputs.",
        "- Invent no requirement beyond these sources. If they are not enough to decide, stop BLOCKED and name "
        "what is missing.", "</authority>",
        "<inputs>", "- NONE", "</inputs>",
        "<scope>", "READ: whatever the objective needs", "WRITE: ledger/reconcile.py",
        "MUST_NOT: write outside WRITE; create a report, findings or summary file; start other agents", "</scope>",
        "<acceptance>", "A1: ledger totals reconcile", "</acceptance>",
        "<output_contract>", "Write artifacts inside WRITE only. Return exactly these lines and nothing else:",
        "STATUS: COMPLETE|PARTIAL|BLOCKED", "ARTIFACTS: <paths you wrote, comma separated, or NONE>",
        "RESULT: <one sentence>",
        "VERIFY: <acceptance IDs met, and each check command with its one line result>",
        "DECISION: NONE|<the one decision you need from the orchestrator>",
        "Do not return command transcripts or narrate your steps.", "</output_contract>",
        "<stop_conditions>", "COMPLETE only when every acceptance criterion is met and checked.",
        "PARTIAL when some are met; name the rest under DECISION.",
        "BLOCKED when an input or the authority is missing, or meeting a criterion needs a write outside WRITE. "
        "Never work around a block by inventing a requirement or widening scope.",
        "</stop_conditions>", "</crew_task>",
    ])
    assert prompt == expected
    assert "Professional frame" not in prompt
    assert "Deliverable: write your result to" not in prompt
    assert "Task:" not in prompt
    assert "Mission:" not in prompt


def test_refuses_unknown_key() -> None:
    role = produce_role(frame={"profession": "payments reconciliation engineer", "expertise": "money"})
    with pytest.raises(planner.PlanRefused, match="unknown key"):
        planner.check_spec(CAT, spec(role), False)


def test_refuses_empty_profession() -> None:
    role = produce_role(frame={"profession": ""})
    with pytest.raises(planner.PlanRefused, match="3 to 200 characters"):
        planner.check_spec(CAT, spec(role), False)


def test_refuses_years_of_experience_phrase() -> None:
    role = produce_role(frame={"profession": "payments reconciliation engineer with 10+ years experience"})
    with pytest.raises(planner.PlanRefused, match="decoration"):
        planner.check_spec(CAT, spec(role), False)


def test_require_frame_true_refuses_frameless_produce_role() -> None:
    cat = copy.deepcopy(CAT)
    cat["thresholds"]["require_frame"] = True
    role = produce_role(frame=None)
    with pytest.raises(planner.PlanRefused, match="no professional_frame"):
        planner.check_spec(cat, spec(role), False)


def test_require_frame_false_accepts_frameless_produce_role() -> None:
    assert CAT["thresholds"].get("require_frame") is False
    role = produce_role(frame=None)
    roles = planner.check_spec(CAT, spec(role), False)
    assert roles[0]["name"] == role["name"]


def test_judge_request2_carries_the_frame() -> None:
    framed = produce_role(frame=FULL_FRAME, name="framed-ledger-builder")
    unframed = check_role(frame=None, name="unframed-ledger-checker")
    body = judge.request2(CAT, TASK, [framed, unframed])
    roles_state = body["state"]["roles"]
    assert roles_state[0]["professional_frame"] == FULL_FRAME
    assert "professional_frame" not in roles_state[1]
