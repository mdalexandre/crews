"""Brief contract v2 (crews-v2 spec, sections V2-1 to V2-7, plus the
V2-2a addendum): typed XML briefs, return modes, seat bodies, and the plan call dict. One test per behavior."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from crews import catalog, planner

CAT = catalog.load()
TASK = "build a payments ledger reconciliation module with idempotent posting"
MISSION = "Build the payments ledger reconciliation module with idempotent posting"


def produce_role(**overrides: Any) -> dict[str, Any]:
    role: dict[str, Any] = {
        "name": "ledger-reconciliation-builder",
        "kind": "produce",
        "mission": MISSION,
        "deliverable": "out.md",
        "scope": ["ledger/reconcile.py"],
        "acceptance": ["ledger totals reconcile"],
        "answers": {"need": "everyday_implementation", "difficulty": 1.0, "divisible": 0.1, "specific": 0.9},
    }
    role.update(overrides)
    return role


def research_role(**overrides: Any) -> dict[str, Any]:
    role: dict[str, Any] = {
        "name": "ledger-formula-locator",
        "kind": "research",
        "mission": "Locate every place the ledger posting formula is computed",
        "deliverable": "sites.md",
        "acceptance": ["every posting formula site is quoted with file and line"],
        "answers": {"need": "bounded_retrieval", "difficulty": 1.0, "divisible": 0.1, "specific": 0.9},
    }
    role.update(overrides)
    return role


def integrate_role(**overrides: Any) -> dict[str, Any]:
    role: dict[str, Any] = {
        "name": "ledger-design-merger",
        "kind": "integrate",
        "mission": "Merge the ledger posting design inputs into one reconciliation design",
        "deliverable": "merged.md",
        "scope": ["design/ledger.md"],
        "acceptance": ["the merged design carries every posting field"],
        "answers": {"need": "integration_architecture", "difficulty": 1.0, "divisible": 0.1, "specific": 0.9},
    }
    role.update(overrides)
    return role


def check_role(**overrides: Any) -> dict[str, Any]:
    role: dict[str, Any] = {
        "name": "ledger-reconciliation-checker",
        "kind": "check",
        "mission": "Check the payments ledger reconciliation module posted idempotently",
        "deliverable": "out.md",
        "criteria": ["ledger totals reconcile"],
        "answers": {"need": "everyday_implementation", "difficulty": 1.0, "divisible": 0.1, "specific": 0.9},
    }
    role.update(overrides)
    return role


def spec(*roles: dict[str, Any], task: str = TASK) -> dict[str, Any]:
    return {"task": task, "roles": list(roles)}


def answers() -> dict[str, Any]:
    return {"need": {"choice": "everyday_implementation", "confidence": 0.9,
                     "probabilities": {"everyday_implementation": 0.9}},
            "divisible": {"noul": 0.1}, "needs_verifier": {"noul": 0.8}, "difficulty": {"score": 1.0}}


def build(*roles: dict[str, Any], run_dir: str = "/tmp/run", cat: dict[str, Any] = CAT,
         task: str = TASK, task_answers: dict[str, Any] | None = None) -> dict[str, Any]:
    plan, _files = planner.build(cat, task_answers or answers(), spec(*roles, task=task), run_dir,
                                 allow=len(roles))
    return plan


def prompt_of(plan: dict[str, Any], role_name: str, kind: str) -> str:
    calls = [c for w in plan["waves"] if w["kind"] == kind for c in w["calls"] if c["role"] == role_name]
    assert len(calls) == 1
    result: str = calls[0]["agent_call"]["prompt"]
    return result


def call_of(plan: dict[str, Any], role_name: str) -> dict[str, Any]:
    calls = [c for w in plan["waves"] for c in w["calls"] if c["role"] == role_name]
    assert len(calls) == 1
    return calls[0]


# --- V2-1: role schema -------------------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["authority", "inputs", "acceptance", "execution", "blocked_when", "must_not",
                                   "read_scope"])
def test_v2_1_list_fields_refused_when_not_a_list_of_non_empty_strings(field: str) -> None:
    role = produce_role(**{field: ["", "ok"]})
    with pytest.raises(planner.PlanRefused, match=f"{field} must be a list of non empty strings"):
        planner.check_spec(CAT, spec(role), False)


def test_v2_1_finding_format_refused_when_not_a_non_empty_string() -> None:
    role = research_role(finding_format="")
    with pytest.raises(planner.PlanRefused, match="finding_format must be a non empty string"):
        planner.check_spec(CAT, spec(role), False)


def test_v2_1_acceptance_required_for_produce_research_integrate_when_threshold_true() -> None:
    assert CAT["thresholds"]["require_acceptance"] is True
    for factory, kind in ((produce_role, "produce"), (research_role, "research"), (integrate_role, "integrate")):
        role = factory(acceptance=None)
        role.pop("acceptance", None)
        with pytest.raises(planner.PlanRefused,
                           match=f"a {kind} role needs acceptance criteria \\(the observable conditions "
                                 "that define done\\)"):
            planner.check_spec(CAT, spec(role), False)


def test_v2_1_acceptance_not_required_when_threshold_false() -> None:
    cat = copy.deepcopy(CAT)
    cat["thresholds"]["require_acceptance"] = False
    role = produce_role()
    del role["acceptance"]
    roles = planner.check_spec(cat, spec(role), False)
    assert roles[0]["name"] == role["name"]


def test_v2_1_returns_defaults_per_kind() -> None:
    plan = build(produce_role(), research_role(), integrate_role(scope=["design/ledger.md"]),
                 check_role(name="ledger-checker"))
    assert call_of(plan, "ledger-reconciliation-builder")["returns"] == "artifact"
    assert call_of(plan, "ledger-formula-locator")["returns"] == "report"
    assert call_of(plan, "ledger-design-merger")["returns"] == "artifact"
    assert call_of(plan, "ledger-checker")["returns"] == "verdict"


def test_v2_1_returns_invalid_value_refused() -> None:
    role = produce_role(returns="pdf")
    with pytest.raises(planner.PlanRefused, match='returns must be "artifact" or "report"'):
        planner.check_spec(CAT, spec(role), False)


def test_v2_1_returns_refused_on_a_check_role() -> None:
    role = check_role(returns="artifact")
    with pytest.raises(planner.PlanRefused, match="a check role has no returns field; it returns a verdict"):
        planner.check_spec(CAT, spec(role), False)


def test_v2_1_returns_artifact_refused_on_a_research_role() -> None:
    role = research_role(returns="artifact")
    with pytest.raises(planner.PlanRefused,
                       match="a research role cannot return artifact; it holds no Write tool"):
        planner.check_spec(CAT, spec(role), False)


def test_v2_1_produce_role_with_returns_report_may_have_an_empty_scope() -> None:
    role = produce_role(returns="report")
    del role["scope"]
    plan = build(role, check_role())
    call = call_of(plan, role["name"])
    assert call["returns"] == "report"
    assert "WRITE: NONE" in call["agent_call"]["prompt"]


def test_v2_1_produce_role_with_returns_artifact_still_needs_a_write_scope() -> None:
    role = produce_role(returns="artifact")
    del role["scope"]
    with pytest.raises(planner.PlanRefused, match="a produce role needs a write scope"):
        planner.check_spec(CAT, spec(role), False)


# --- V2-2: brief rendering ----------------------------------------------------------------------------------

def test_v2_2_section_order() -> None:
    frame = {"profession": "payments reconciliation engineer", "standards": ["idempotent posting"],
             "method": ["trace every charge"], "watch_for": ["silently summed currencies"]}
    role = produce_role(professional_frame=frame, authority=["the ledger spec"], inputs=["ledger/spec.md"],
                        read_scope=["ledger/"], must_not=["touch the refund path"],
                        blocked_when=["the ledger spec is ambiguous"])
    prompt = prompt_of(build(role, check_role()), role["name"], "produce")
    tags = ["<objective>", "<context>", "<capability_cue>", "<authority>", "<inputs>", "<scope>", "<acceptance>",
           "<execution>", "<output_contract>", "<stop_conditions>"]
    positions = [prompt.index(t) for t in tags]
    assert positions == sorted(positions)
    assert prompt.startswith('<crew_task version="2" kind="produce" role="ledger-reconciliation-builder" '
                             'cell="ledger-reconciliation-builder">')
    assert prompt.rstrip().endswith("</crew_task>")


def test_v2_2_context_omitted_when_task_and_mission_are_equal_after_normalizing() -> None:
    role = produce_role(mission="  Build   the LEDGER  ")
    prompt = prompt_of(build(role, check_role(), task="build the ledger"), role["name"], "produce")
    assert "<context>" not in prompt


def test_v2_2_capability_cue_omitted_without_professional_frame() -> None:
    role = produce_role()
    prompt = prompt_of(build(role, check_role()), role["name"], "produce")
    assert "<capability_cue>" not in prompt


def test_v2_2_brief_never_contains_the_banned_phrases() -> None:
    role = produce_role(professional_frame={"profession": "payments reconciliation engineer"})
    plan = build(role, research_role(), check_role())
    for w in plan["waves"]:
        for c in w["calls"]:
            prompt = c["agent_call"]["prompt"]
            for banned in ("Deliverable: write your result to", "Task:", "Mission:", "Professional frame:"):
                assert banned not in prompt


def test_v2_2_check_brief_carries_only_its_own_mission_context_criteria_and_artifact_paths() -> None:
    producer = produce_role(mission="Build the ledger posting module")
    checker = check_role(criteria=["postings are idempotent"])
    plan = build(producer, checker)
    prompt = prompt_of(plan, checker["name"], "check")
    assert checker["mission"] in prompt
    assert producer["mission"] not in prompt
    assert "ledger totals reconcile" not in prompt  # producer's own acceptance text, unrelated to this checker


# --- V2-2a addendum: check inputs are write scopes only, never earlier record paths -----------------------

def test_v2_2a_check_artifact_under_check_holds_write_scopes_not_earlier_record_paths() -> None:
    researcher = research_role()
    producer = produce_role()
    checker = check_role()
    plan = build(researcher, producer, checker)
    record_paths = [call_of(plan, researcher["name"])["record_path"], call_of(plan, producer["name"])["record_path"]]
    check_prompt = prompt_of(plan, checker["name"], "check")
    assert "<artifact_under_check>" in check_prompt and "<inputs>" not in check_prompt
    for record_path in record_paths:
        assert record_path not in check_prompt
    for scope_path in producer["scope"]:
        assert scope_path in check_prompt
    produce_prompt = prompt_of(plan, producer["name"], "produce")
    assert "<inputs>" in produce_prompt


# --- V2-3: output contract text ------------------------------------------------------------------------------

def test_v2_3_artifact_mode_output_contract_text() -> None:
    role = produce_role()
    prompt = prompt_of(build(role, check_role()), role["name"], "produce")
    assert ("<output_contract>\nWrite artifacts inside WRITE only. Return exactly these lines and nothing else:\n"
           "STATUS: COMPLETE|PARTIAL|BLOCKED\nARTIFACTS: <paths you wrote, comma separated, or NONE>\n"
           "RESULT: <one sentence>\nVERIFY: <acceptance IDs met, and each check command with its one line "
           "result>\nDECISION: NONE|<the one decision you need from the orchestrator>\n"
           "Do not return command transcripts or narrate your steps.\n</output_contract>") in prompt


def test_v2_3_report_mode_default_finding_shape_differs_for_research_and_produce() -> None:
    research_prompt = prompt_of(build(research_role(), check_role()), "ledger-formula-locator", "research")
    assert "F: SUPPORTED|CONFLICT|UNKNOWN <finding> | EVIDENCE:" in research_prompt
    produce_report_role = produce_role(returns="report")
    del produce_report_role["scope"]
    produce_prompt = prompt_of(build(produce_report_role, check_role()), produce_report_role["name"], "produce")
    assert "F: <finding> | EVIDENCE:" in produce_prompt


def test_v2_3_report_mode_uses_finding_format_when_given() -> None:
    role = research_role(finding_format="L<line>: <what breaks>")
    prompt = prompt_of(build(role, check_role()), role["name"], "research")
    assert "F: L<line>: <what breaks> | EVIDENCE:" in prompt


def test_v2_3_check_verdict_mode_output_contract_text() -> None:
    checker = check_role()
    prompt = prompt_of(build(produce_role(), checker), checker["name"], "check")
    assert ("<output_contract>\nReturn exactly:\nVERDICT: PASS|FAIL|BLOCKED\n"
           "PASS: <criterion IDs, comma separated, or NONE>\n"
           "FAIL <ID>: <defect> | EVIDENCE: <path:line, or the probe and its result>\n"
           "UNVERIFIED: NONE|<ID: reason>\nDECISION: NONE|<the one decision you need from the orchestrator>\n"
           "Do not return command transcripts or narrate your steps.\n</output_contract>") in prompt


# --- V2-4: stop conditions -----------------------------------------------------------------------------------

def test_v2_4_produce_stop_conditions_text() -> None:
    role = produce_role()
    prompt = prompt_of(build(role, check_role()), role["name"], "produce")
    assert ("<stop_conditions>\nCOMPLETE only when every acceptance criterion is met and checked.\n"
           "PARTIAL when some are met; name the rest under DECISION.\n"
           "BLOCKED when an input or the authority is missing, or meeting a criterion needs a write outside "
           "WRITE. Never work around a block by inventing a requirement or widening scope.\n"
           "</stop_conditions>") in prompt


def test_v2_4_check_stop_conditions_text() -> None:
    checker = check_role()
    prompt = prompt_of(build(produce_role(), checker), checker["name"], "check")
    assert ("<stop_conditions>\nPASS only when your own evidence supports every criterion.\n"
           "FAIL when at least one criterion fails, reported after every criterion is evaluated.\n"
           "BLOCKED only when a missing artifact, authority or tool prevents a defensible verdict.\n"
           "</stop_conditions>") in prompt


def test_v2_4_blocked_when_appends_a_line_per_item() -> None:
    role = produce_role(blocked_when=["the ledger spec is missing a sign-off"])
    prompt = prompt_of(build(role, check_role()), role["name"], "produce")
    assert "BLOCKED also when: the ledger spec is missing a sign-off\n</stop_conditions>" in prompt


# --- V2-5: seat bodies ----------------------------------------------------------------------------------------

def test_v2_5_no_seat_body_carries_a_forbidden_phrase() -> None:
    for kind, row in CAT["kinds"].items():
        for phrase in ("heredoc", "Report the commands you ran", "at most 5 lines",
                       "full result belongs in the deliverable file"):
            assert phrase not in row["body"], f"kind {kind}: forbidden phrase {phrase!r}"


# --- V2-6: frame mapping and catalog --------------------------------------------------------------------------

def test_v2_6_catalog_version_and_thresholds() -> None:
    assert CAT["version"] == "2.1.0"
    assert CAT["thresholds"]["require_acceptance"] is True
    assert CAT["thresholds"]["require_frame"] is False


def test_v2_6_frame_mapping_profession_watch_for_standards_method() -> None:
    frame = {"profession": "payments reconciliation engineer", "standards": ["idempotent posting"],
             "method": ["trace every charge"], "watch_for": ["silently summed currencies"]}
    role = produce_role(professional_frame=frame)
    prompt = prompt_of(build(role, check_role()), role["name"], "produce")
    assert "<capability_cue>\npayments reconciliation engineer. Lenses: silently summed currencies.\n" in prompt
    assert "- idempotent posting\n" in prompt
    assert "- trace every charge\n" in prompt


# --- V2-7: plan output -----------------------------------------------------------------------------------------

def test_v2_7_call_dict_carries_returns_and_record_path_equal_to_deliverable() -> None:
    role = produce_role()
    call = call_of(build(role, check_role()), role["name"])
    assert call["returns"] == "artifact"
    assert call["record_path"] == call["deliverable"]


def test_v2_7_multi_wave_note_present_only_with_more_than_one_wave() -> None:
    no_check_answers = {**answers(), "needs_verifier": {"noul": 0.1}}
    single_wave = build(produce_role(), task_answers=no_check_answers)
    assert not any("record each hand-back a later wave reads with" in n for n in single_wave["notes"])
    multi_wave = build(produce_role(), check_role())
    assert any(n == "record each hand-back a later wave reads with: crew record --transcript "
              "<agent output file> --out <record_path>" for n in multi_wave["notes"])
