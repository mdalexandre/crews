"""Role answer gating (JEV-12) and answer source recording (JEV-15).

`uv run pytest tests/test_answer_source.py`

Covers:
  A1 a live role answer is gated by need_gate the same way the task need answer is; with no
     measured role band the decision is uncalibrated and the DECLARED answer is what the plan uses.
  A2 an admitted role answer is used outright; a confirmed one is used only with the same
     "need_gate confirm" marker the need path already carries, never silently.
  A3 every answer a plan used carries a source drawn from exactly declared, live_admitted,
     live_confirmed, fallback.
  A4 the source is recorded per answer, not per plan: a plan mixing sources records each distinctly.
  A5 plan.json alone (no transcript) says which answers acted and where they came from.
  A6 with no live answer involved, every answer is recorded declared and nothing else changes.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from crews import catalog, cli, planner

CAT = catalog.load()

# JEV-17: the installed catalog's typesafe.need_band now carries a real provenance.model (jev-1.13.0,
# from crews/catalog.json), and crews.need_gate.gate_need refuses to admit when a live response's own
# model differs from it. A stub response that instead reports a placeholder model string is not a
# neutral fixture value, it is a stub misrepresenting the model that answered it, so every stub here
# reports this real installed model rather than a literal "stub".
_INSTALLED_NEED_MODEL = ((CAT.get("typesafe") or {}).get("need_band") or {}).get("provenance", {}).get("model")

BASE_TASK_ANSWERS = {"need": {"choice": "repeatable_task", "confidence": 0.9,
                              "probabilities": {"repeatable_task": 0.9}},
                    "divisible": {"noul": 0.1}, "needs_verifier": {"noul": 0.2}, "difficulty": {"score": 0.5}}

ALLOWED_SOURCES = {"declared", "live_admitted", "live_confirmed", "fallback"}


def _role(name: str, target: str, declared_need: str = "repeatable_task") -> dict[str, Any]:
    return {
        "name": name, "kind": "produce",
        "mission": f"Rename the {target} label to a clearer word for the button task",
        "deliverable": f"{name}.md", "scope": [f"src/components/{target}.tsx"],
        "acceptance": [f"the {target} label is renamed", "no other label changed"],
        "answers": {"need": declared_need, "difficulty": 0.3, "divisible": 0.1, "specific": 0.9},
    }


def _spec(*roles: dict[str, Any]) -> dict[str, Any]:
    return {"task": "rename several button labels for the checkout button task", "roles": list(roles)}


def _need1_response(choice: str, confidence: float) -> dict[str, Any]:
    return {"model": _INSTALLED_NEED_MODEL, "usage": {}, "answers": {
        "need": {"choice": choice, "confidence": confidence, "probabilities": {choice: confidence}},
        "divisible": {"noul": 0.2}, "needs_verifier": {"noul": 0.2}, "difficulty": {"score": 1.0}}}


def _need2_response(role_answers: list[tuple[str, float]]) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    for i, (choice, confidence) in enumerate(role_answers):
        answers[f"need.{i}"] = {"choice": choice, "confidence": confidence, "probabilities": {choice: confidence}}
        answers[f"difficulty.{i}"] = {"score": 1.5}
        answers[f"divisible.{i}"] = {"noul": 0.6}
        answers[f"specific.{i}"] = {"noul": 0.95}
    return {"model": _INSTALLED_NEED_MODEL, "usage": {}, "answers": answers}


def _stub_gated_call(need1: dict[str, Any], need2: dict[str, Any]) -> Any:
    def _fn(body: dict[str, Any], *, purpose: str, **_kwargs: Any) -> dict[str, Any]:
        if purpose == cli._REQ1_PURPOSE:
            return {"state": "answered", "response": need1}
        if purpose == cli._REQ2_PURPOSE:
            return {"state": "answered", "response": need2}
        raise AssertionError(f"unexpected purpose {purpose!r}")
    return _fn


def _install_role_band(cat_dir: Path, act_threshold: float, confirm_threshold: float) -> None:
    cat_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "act": {"status": "MEASURED", "threshold": act_threshold, "n": 40, "k": 38,
               "precision": 0.95, "wilson_lower": 0.85},
        "confirm": {"status": "MEASURED", "threshold": confirm_threshold, "n": 40, "k": 30,
                   "precision": 0.75, "wilson_lower": 0.62},
        "provenance": {"criteria_hash": cli._need_criteria_hash(CAT), "catalog_version": CAT.get("version"),
                       "model": _INSTALLED_NEED_MODEL,
                       "n_observations": 40, "observations_path": "/tmp/does-not-matter/observations.jsonl",
                       "measured_at": "2026-09-22T19:00:00+00:00"},
    }
    (cat_dir / "bands_roles.json").write_text(json.dumps(payload), encoding="utf-8")


# --- A1: no measured role band leaves the declared per role answer standing ---------------------------


def test_a1_uncalibrated_role_band_leaves_declared_answer_standing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREWS_CALIBRATION_DIR", str(tmp_path / "calibration"))
    roles = [_role("checkout-button-label-renamer", "CheckoutButton"),
            _role("checkout-price-label-renamer", "CheckoutPrice")]
    spec = _spec(*roles)
    monkeypatch.setattr(cli.egress, "gated_call", _stub_gated_call(
        _need1_response("everyday_implementation", 0.95),
        _need2_response([("everyday_implementation", 0.97), ("everyday_implementation", 0.97)])))
    run_dir = tmp_path / "run"
    view = cli.core_plan(CAT, spec=spec, answers=None, outage=False, judge_mode="live", run_dir=str(run_dir),
                         allow=3, force=None)
    plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    roles_written = json.loads((run_dir / "roles.json").read_text(encoding="utf-8"))
    # JEV-34 (crew task blank-identity-normalization-closer): "fallback" alone does not say WHY;
    # this test's premise is specifically "no measured role band", so the reason must actually be
    # uncalibrated, never a threshold rejection that happened to land on the same declared answer.
    notes = " ".join(view.get("notes") or [])
    for role in roles:
        assert plan["answer_sources"][role["name"]] == "fallback"
        assert f"need_gate uncalibrated ({role['name']})" in notes, notes
        assert f"need_gate reject ({role['name']})" not in notes, notes
    for role in roles_written["roles"]:
        assert role["answers"]["need"] == "repeatable_task", "the declared need must stand, never the live one"


# --- A2: admit is used outright; confirm is used only with the marker ---------------------------------


def test_a2_admit_used_outright_and_confirm_used_only_with_its_marker(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREWS_CALIBRATION_DIR", str(tmp_path / "calibration"))
    _install_role_band(tmp_path / "calibration", act_threshold=0.85, confirm_threshold=0.55)
    admitted = _role("checkout-button-label-renamer", "CheckoutButton")
    confirmed = _role("checkout-price-label-renamer", "CheckoutPrice")
    spec = _spec(admitted, confirmed)
    monkeypatch.setattr(cli.egress, "gated_call", _stub_gated_call(
        _need1_response("everyday_implementation", 0.95),
        _need2_response([("bounded_retrieval", 0.97), ("everyday_implementation", 0.65)])))
    run_dir = tmp_path / "run"
    view = cli.core_plan(CAT, spec=spec, answers=None, outage=False, judge_mode="live", run_dir=str(run_dir),
                         allow=2, force=None)
    plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    roles_written = json.loads((run_dir / "roles.json").read_text(encoding="utf-8"))
    by_name = {r["name"]: r for r in roles_written["roles"]}

    assert plan["answer_sources"][admitted["name"]] == "live_admitted"
    assert by_name[admitted["name"]]["answers"]["need"] == "bounded_retrieval"

    assert plan["answer_sources"][confirmed["name"]] == "live_confirmed"
    assert by_name[confirmed["name"]]["answers"]["need"] == "everyday_implementation"
    assert any(f"need_gate confirm ({confirmed['name']})" in n for n in view.get("notes") or [])


# --- A3: every source a plan carries is drawn from exactly the four named values -----------------------


def test_a3_every_recorded_source_is_one_of_the_four_named_values(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREWS_CALIBRATION_DIR", str(tmp_path / "calibration"))
    _install_role_band(tmp_path / "calibration", act_threshold=0.85, confirm_threshold=0.55)
    roles = [_role("checkout-button-label-renamer", "CheckoutButton"),
            _role("checkout-price-label-renamer", "CheckoutPrice"),
            _role("checkout-footer-label-renamer", "CheckoutFooter")]
    spec = _spec(*roles)
    monkeypatch.setattr(cli.egress, "gated_call", _stub_gated_call(
        _need1_response("everyday_implementation", 0.95),
        _need2_response([("bounded_retrieval", 0.97), ("everyday_implementation", 0.65),
                         ("bounded_retrieval", 0.10)])))
    run_dir = tmp_path / "run"
    view = cli.core_plan(CAT, spec=spec, answers=None, outage=False, judge_mode="live", run_dir=str(run_dir),
                         allow=3, force=None)
    plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    sources = plan["answer_sources"]
    assert set(sources) == {"task", *(r["name"] for r in roles)}
    assert set(sources.values()) <= ALLOWED_SOURCES
    assert sources["task"] == "live_admitted"
    assert sources[roles[0]["name"]] == "live_admitted"
    assert sources[roles[1]["name"]] == "live_confirmed"
    assert sources[roles[2]["name"]] == "fallback"
    # JEV-34: a role band IS installed here (act 0.85, confirm 0.55), and role[2]'s confidence 0.10
    # is below both, so this "fallback" must be a genuine threshold rejection, never an uncalibrated
    # refusal that happened to fall back the same way; proving the reason keeps this assertion able
    # to fail if that changed.
    notes = " ".join(view.get("notes") or [])
    assert f"need_gate reject ({roles[2]['name']})" in notes, notes
    assert f"need_gate uncalibrated ({roles[2]['name']})" not in notes, notes


# --- A4: the source is per answer, not per plan ----------------------------------------------------------


def test_a4_mixed_sources_are_recorded_distinctly_per_answer(tmp_path: Path) -> None:
    """Unlike the other five negative-assertion sites this crew task closes (JEV-34), this test
    calls planner.build directly with a hand supplied answer_sources mapping: it proves the
    plumbing records whatever source label it is given, per answer, not a gate_need decision it
    derives itself. There is no "why" to disambiguate here (no need_gate call happens in this
    test), so JEV-34's threshold-vs-uncalibrated distinction does not apply to it; what a bare
    `== "fallback"` assertion COULD hide here instead is the mechanism silently ignoring its input
    and always recording the same label regardless of what was passed. Proved below by also
    building with a different label for the same role and confirming the plan tracks the change,
    so this assertion cannot pass "for the wrong reason" (a hardcoded expectation) either."""
    admitted_role = _role("checkout-button-label-renamer", "CheckoutButton")
    fallback_role = _role("checkout-price-label-renamer", "CheckoutPrice")
    spec = _spec(admitted_role, fallback_role)
    plan, files = planner.build(CAT, BASE_TASK_ANSWERS, spec, str(tmp_path / "run"), allow=2, answer_sources={
        "task": "declared", admitted_role["name"]: "live_admitted", fallback_role["name"]: "fallback"})
    assert plan["answer_sources"]["task"] == "declared"
    assert plan["answer_sources"][admitted_role["name"]] == "live_admitted"
    assert plan["answer_sources"][fallback_role["name"]] == "fallback"
    assert files["plan.json"]["answer_sources"] == plan["answer_sources"]

    # the proof that this assertion depends on its actual input rather than being hardcoded true:
    # the same role, given a different source label, must record THAT label, not "fallback" again.
    plan_confirmed, _ = planner.build(CAT, BASE_TASK_ANSWERS, spec, str(tmp_path / "run2"), allow=2,
                                      answer_sources={"task": "declared", admitted_role["name"]: "live_admitted",
                                                      fallback_role["name"]: "live_confirmed"})
    assert plan_confirmed["answer_sources"][fallback_role["name"]] == "live_confirmed"


def test_a4_core_plan_records_a_real_mix_of_task_and_role_sources(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same mix, reached through the actual live routing path rather than a direct planner.build call:
    the task level need admits while one role's need falls back (uncalibrated), inside one plan."""
    monkeypatch.setenv("CREWS_CALIBRATION_DIR", str(tmp_path / "calibration"))
    roles = [_role("checkout-button-label-renamer", "CheckoutButton")]
    spec = _spec(*roles)
    monkeypatch.setattr(cli.egress, "gated_call", _stub_gated_call(
        _need1_response("everyday_implementation", 0.95),
        _need2_response([("everyday_implementation", 0.97)])))
    run_dir = tmp_path / "run"
    view = cli.core_plan(CAT, spec=spec, answers=None, outage=False, judge_mode="live", run_dir=str(run_dir),
                         allow=3, force=None)
    plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    assert plan["answer_sources"]["task"] == "live_admitted"
    assert plan["answer_sources"][roles[0]["name"]] == "fallback", "no role band installed: declared stands"
    # JEV-34: no role band is installed in this test (no _install_role_band call), so this fallback
    # must be uncalibrated, never a threshold rejection the docstring above does not claim.
    notes = " ".join(view.get("notes") or [])
    assert f"need_gate uncalibrated ({roles[0]['name']})" in notes, notes
    assert f"need_gate reject ({roles[0]['name']})" not in notes, notes


# --- A5: plan.json alone says which answers acted and where they came from -----------------------------


def test_a5_plan_json_alone_shows_which_answers_acted_and_their_source(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREWS_CALIBRATION_DIR", str(tmp_path / "calibration"))
    _install_role_band(tmp_path / "calibration", act_threshold=0.85, confirm_threshold=0.55)
    admitted = _role("checkout-button-label-renamer", "CheckoutButton")
    fell_back = _role("checkout-price-label-renamer", "CheckoutPrice")
    spec = _spec(admitted, fell_back)
    monkeypatch.setattr(cli.egress, "gated_call", _stub_gated_call(
        _need1_response("everyday_implementation", 0.95),
        _need2_response([("bounded_retrieval", 0.97), ("bounded_retrieval", 0.10)])))
    run_dir = tmp_path / "run"
    cli.core_plan(CAT, spec=spec, answers=None, outage=False, judge_mode="live", run_dir=str(run_dir),
                 allow=3, force=None)

    # a probe: read only plan.json, as an independent reader would, with no transcript in hand.
    plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    sources = plan["answer_sources"]
    assert sources[admitted["name"]] == "live_admitted"
    assert sources[fell_back["name"]] == "fallback"
    # JEV-34: plan.json alone must be enough to tell WHY, not just that it fell back; its own
    # "notes" field (not a separate transcript) is what proves this "fallback" is a role band
    # confidence 0.10 falling below both the installed act (0.85) and confirm (0.55) thresholds, a
    # genuine rejection, never an uncalibrated refusal this test's fixture does not exercise.
    plan_notes = " ".join(plan.get("notes") or [])
    assert f"need_gate reject ({fell_back['name']})" in plan_notes, plan_notes
    assert f"need_gate uncalibrated ({fell_back['name']})" not in plan_notes, plan_notes
    # cells for the admitted role are seated on the live-judged need's row, provable from plan.json alone
    calls_by_role: dict[str, list[dict[str, Any]]] = {}
    for wave in plan["waves"]:
        for call in wave["calls"]:
            calls_by_role.setdefault(call["role"], []).append(call)
    admitted_seat = calls_by_role[admitted["name"]][0]["seat_file"]
    fallback_seat = calls_by_role[fell_back["name"]][0]["seat_file"]
    expected_admitted_seat = catalog.seat_name("produce", CAT["needs"]["bounded_retrieval"]["worker"]["model"],
                                               CAT["needs"]["bounded_retrieval"]["worker"]["effort"])
    expected_fallback_seat = catalog.seat_name("produce", CAT["needs"]["repeatable_task"]["worker"]["model"],
                                               CAT["needs"]["repeatable_task"]["worker"]["effort"])
    assert admitted_seat == expected_admitted_seat
    assert fallback_seat == expected_fallback_seat


# --- A6: with no live answer involved, every answer is recorded declared and nothing else changes -------


def test_a6_pure_declared_plan_records_every_answer_declared_and_is_otherwise_unchanged(tmp_path: Path) -> None:
    role_a = _role("checkout-button-label-renamer", "CheckoutButton")
    role_b = _role("checkout-price-label-renamer", "CheckoutPrice")
    spec = _spec(role_a, role_b)

    run_dir_a = tmp_path / "a"
    view = cli.core_plan(CAT, spec=copy.deepcopy(spec), answers=BASE_TASK_ANSWERS, outage=False,
                         judge_mode="off", run_dir=str(run_dir_a), allow=2, force=None)
    plan_a = json.loads((run_dir_a / "plan.json").read_text(encoding="utf-8"))
    assert plan_a["answer_sources"] == {"task": "declared", role_a["name"]: "declared", role_b["name"]: "declared"}
    assert "answer_sources" not in view or view["answer_sources"] == plan_a["answer_sources"]

    # the same declared plan built directly through planner.build, passing no answer_sources at all: this is
    # "today's output" before this change, and it must match plan_a in every field but the new one.
    plan_b, _ = planner.build(CAT, BASE_TASK_ANSWERS, copy.deepcopy(spec), str(run_dir_a), allow=2)
    for key in ("cells", "models", "efforts", "budget"):
        assert plan_a[key] == plan_b[key]
    assert plan_b["answer_sources"] == plan_a["answer_sources"]
