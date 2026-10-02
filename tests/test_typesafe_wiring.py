"""C-CLI wiring: egress, bands, need_gate, skills, handback, and briefcheck into crews/cli.py, server.py,
and crews/catalog.json / crews/catalog.py (SRD typesafe-integration-v1.0.md section 2.5). Covers
TSI-GATE-07, TSI-CAL-02, TSI-SKL-05, TSI-BL-03..04, TSI-NEED-01..02, and TSI-AUD-01, plus the wiring
acceptance criteria A1..A8 of the integration brief. Every network call in this file goes to a loopback
stub; the loopback_only fixture (copied from tests/test_egress.py) refuses anything else."""

from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from crews import bands, briefcheck, budget, catalog, cli, egress, handback, judge, planner, skills

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
FIXTURE_ROLE_NAMES = ["rename_button", "find_mrr", "refactor_billing", "security_review", "storefront_hub",
                      "caption_listings"]
TABLE = json.loads((FIX / "live_table.json").read_text(encoding="utf-8"))["rows"]
CAT = catalog.load()


def _cat_without_installed_need_band() -> dict[str, Any]:
    """JEV-25: crews/catalog.json now ships an installed typesafe.need_band with real provenance
    (round three protocol.md section 4, adopted 2026-09-22), which crews.cli._resolve_need_band prefers
    over the calibration directory file (the source the A2 need_gate tests below write via
    _write_bands). Strip the installed band so those tests actually exercise the calibration directory
    fallback path they were written to cover, rather than always hitting the installed band regardless
    of what the fixture writes."""
    cat = copy.deepcopy(CAT)
    typesafe = cat.get("typesafe")
    if isinstance(typesafe, dict):
        typesafe.pop("need_band", None)
    return cat


CAT_NO_INSTALLED_NEED_BAND = _cat_without_installed_need_band()


def answers_for(name: str) -> dict[str, Any]:
    return dict(TABLE[FIXTURE_ROLE_NAMES.index(name)]["answers"])


# --- shared loopback fixtures (pattern copied from tests/test_egress.py) -------------------------------------

@pytest.fixture(autouse=True)
def loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise OSError(f"test guard: outbound connection to {host} refused")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture(autouse=True)
def egress_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREWS_EGRESS_DIR", str(tmp_path / "egress"))
    monkeypatch.setenv("CREWS_CALIBRATION_DIR", str(tmp_path / "calibration"))
    monkeypatch.setenv("CREWS_AUDIT_DIR", str(tmp_path / "audit"))


class Stub:
    """A scripted loopback HTTP server, recording every request it receives. Each script entry is
    (status, headers, body); the default with an empty script is one HTTP 500."""

    def __init__(self) -> None:
        self.script: list[tuple[int, dict[str, str], dict[str, Any]]] = []
        self.requests: list[dict[str, Any]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                stub.requests.append({"auth": self.headers.get("Authorization"),
                                      "body": json.loads(self.rfile.read(length))})
                status, headers, body = stub.script.pop(0) if stub.script else (500, {}, {})
                data = json.dumps(body).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/systemone"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Iterator[Stub]:
    s = Stub()
    monkeypatch.setenv("CREW_TYPESAFE_URL", s.url)
    monkeypatch.setenv("CREW_TYPESAFE_TIMEOUT", "1.0")
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    yield s
    s.stop()


def need_body(confidence: float) -> dict[str, Any]:
    return {"model": judge.MODEL, "usage": {},
            "answers": {"need": {"choice": "everyday_implementation", "confidence": confidence,
                                 "probabilities": {"everyday_implementation": confidence}},
                       "difficulty": {"score": 1.0}, "divisible": {"noul": 0.2},
                       "needs_verifier": {"noul": 0.8}}}


def approve_gate(result: dict[str, Any]) -> dict[str, Any]:
    pkt = result["owner_gates"][0]
    egress.approve(pkt["call_id"], pkt["fingerprint"])
    return pkt


# --- A1 / TSI-GATE-01: judge.call is reachable only through egress.py -----------------------------------------

def test_a1_only_egress_calls_judge_call() -> None:
    offenders = []
    for p in [*sorted((ROOT / "crews").rglob("*.py")), ROOT / "server.py"]:
        if p.name == "egress.py" or "__pycache__" in p.parts:
            continue
        if "judge.call(" in p.read_text(encoding="utf-8"):
            offenders.append(str(p))
    assert offenders == []


# --- A2 / TSI-NEED-01: core_budget live decisions -------------------------------------------------------------

def test_a2_owner_gate_never_touches_the_network(stub: Stub, tmp_path: Path) -> None:
    result = cli.core_budget(CAT, answers=None, outage=False, judge_mode="live", task="task A: owner gate",
                             run_dir=str(tmp_path / "r"))
    assert result["judge"]["status"] == "owner_gate"
    assert result["owner_gates"] and stub.requests == []
    assert result["need"] == budget.OUTAGE_NEED and result["confidence"] == 0.0


def test_a2_uncalibrated_gives_outage_budget_with_a_note(stub: Stub, tmp_path: Path) -> None:
    # JEV-25: use CAT_NO_INSTALLED_NEED_BAND so the calibration directory (empty here, no bands.json
    # written) is what actually gets read; the real shipped CAT would admit outright on its own
    # installed, provenanced band regardless of what this test's tmp_path holds.
    gated = cli.core_budget(CAT_NO_INSTALLED_NEED_BAND, answers=None, outage=False, judge_mode="live",
                            task="task B: uncalibrated", run_dir=str(tmp_path / "g"))
    approve_gate(gated)
    stub.script = [(200, {}, need_body(0.95))]
    result = cli.core_budget(CAT_NO_INSTALLED_NEED_BAND, answers=None, outage=False, judge_mode="live",
                             task="task B: uncalibrated", run_dir=str(tmp_path / "a"))
    assert result["judge"]["need_gate"]["decision"] == "uncalibrated"
    assert result["need"] == budget.OUTAGE_NEED
    assert any("need_gate uncalibrated" in n for n in result["notes"])


def _write_bands(tmp_path: Path) -> None:
    # JEV-25: carries real provenance (a criteria_hash matching the catalog these tests gate against,
    # crews.cli._need_criteria_hash) so this is a genuinely calibrated band, not one need_gate would
    # already reject as unverifiable before ever reading its threshold (crews/need_gate.py: a real band
    # with no recorded criteria hash is "uncalibrated" too, the same as no band at all).
    # JEV-17: also carries a real provenance.model, matching the model need_body() reports as having
    # answered (crews.judge.MODEL), so gate_need's model check (mirroring the criteria hash check
    # above) does not reject this band as unverifiable either.
    d = tmp_path / "calibration"
    d.mkdir(parents=True, exist_ok=True)
    (d / "bands.json").write_text(json.dumps({
        "act": {"status": "MEASURED", "threshold": 0.85, "n": 20, "k": 19, "precision": 0.95,
               "wilson_lower": 0.86},
        "confirm": {"status": "MEASURED", "threshold": 0.6, "n": 20, "k": 15, "precision": 0.75,
                   "wilson_lower": 0.62},
        "provenance": {"criteria_hash": cli._need_criteria_hash(CAT), "catalog_version": CAT.get("version"),
                       "model": judge.MODEL,
                       "n_observations": 20, "observations_path": "/tmp/does-not-matter/observations.jsonl",
                       "measured_at": "2026-09-22T19:00:00+00:00"},
    }), encoding="utf-8")


def test_a2_admit_keeps_the_budget(stub: Stub, tmp_path: Path) -> None:
    # JEV-25: use CAT_NO_INSTALLED_NEED_BAND so _write_bands' calibration directory band (threshold
    # 0.85) is the one routing actually reads, rather than the real shipped catalog's own installed
    # band (threshold 0.94), which would gate 0.9 confidence into "confirm" instead of "admit".
    _write_bands(tmp_path)
    gated = cli.core_budget(CAT_NO_INSTALLED_NEED_BAND, answers=None, outage=False, judge_mode="live",
                            task="task C: admit", run_dir=str(tmp_path / "g"))
    approve_gate(gated)
    stub.script = [(200, {}, need_body(0.9))]
    result = cli.core_budget(CAT_NO_INSTALLED_NEED_BAND, answers=None, outage=False, judge_mode="live",
                             task="task C: admit", run_dir=str(tmp_path / "a"))
    assert result["judge"]["need_gate"]["decision"] == "admit"
    assert result["need"] == "everyday_implementation" and "confirm_need" not in result


def test_a2_confirm_adds_confirm_need_and_a_note(stub: Stub, tmp_path: Path) -> None:
    _write_bands(tmp_path)
    gated = cli.core_budget(CAT_NO_INSTALLED_NEED_BAND, answers=None, outage=False, judge_mode="live",
                            task="task D: confirm", run_dir=str(tmp_path / "g"))
    approve_gate(gated)
    stub.script = [(200, {}, need_body(0.7))]
    result = cli.core_budget(CAT_NO_INSTALLED_NEED_BAND, answers=None, outage=False, judge_mode="live",
                             task="task D: confirm", run_dir=str(tmp_path / "a"))
    assert result["judge"]["need_gate"]["decision"] == "confirm"
    assert result["confirm_need"] is True and result["need"] == "everyday_implementation"
    assert any("need_gate confirm" in n for n in result["notes"])


def test_a2_reject_gives_outage_budget(stub: Stub, tmp_path: Path) -> None:
    _write_bands(tmp_path)
    gated = cli.core_budget(CAT_NO_INSTALLED_NEED_BAND, answers=None, outage=False, judge_mode="live",
                            task="task E: reject", run_dir=str(tmp_path / "g"))
    approve_gate(gated)
    stub.script = [(200, {}, need_body(0.3))]
    result = cli.core_budget(CAT_NO_INSTALLED_NEED_BAND, answers=None, outage=False, judge_mode="live",
                             task="task E: reject", run_dir=str(tmp_path / "a"))
    assert result["judge"]["need_gate"]["decision"] == "reject"
    assert result["need"] == budget.OUTAGE_NEED


def test_a2_failed_gives_outage_budget_with_a_note(stub: Stub, tmp_path: Path) -> None:
    gated = cli.core_budget(CAT, answers=None, outage=False, judge_mode="live", task="task F: failed",
                            run_dir=str(tmp_path / "g"))
    approve_gate(gated)
    stub.script = [(500, {}, {})]
    result = cli.core_budget(CAT, answers=None, outage=False, judge_mode="live", task="task F: failed",
                             run_dir=str(tmp_path / "a"))
    assert result["judge"]["status"] == "outage" and "owner_gates" not in result
    assert any("judge outage" in n for n in result["notes"])


# --- A3 / TSI-BL-01, TSI-SKL-05, TSI-BL-03..04 ------------------------------------------------------------

BASE_TASK_ANSWERS = {"need": {"choice": "repeatable_task", "confidence": 0.9,
                              "probabilities": {"repeatable_task": 0.9}},
                    "divisible": {"noul": 0.1}, "needs_verifier": {"noul": 0.2}, "difficulty": {"score": 0.5}}


def test_a3_regex_floor_refuses_a_leaking_check_role() -> None:
    spec = {"task": "review the checkout diff for correctness", "roles": [{
        "name": "checkout-diff-reviewer", "kind": "check",
        "mission": "Review the checkout diff. I already verified that the fix is correct.",
        "deliverable": "review.md", "criteria": ["the diff compiles"],
        "answers": {"need": "mechanical_verification", "difficulty": 0.1, "divisible": 0.0},
    }]}
    with pytest.raises(planner.PlanRefused) as exc:
        cli.core_plan(CAT, spec=spec, answers=BASE_TASK_ANSWERS, outage=False, judge_mode="off",
                      run_dir="/nonexistent-must-not-be-created", allow=None, force=None)
    assert "checkout-diff-reviewer" in str(exc.value)


PRODUCE_ROLE = {
    "name": "dashboard-widget-builder", "kind": "produce",
    "mission": "Build a reusable dashboard widget with accessible labels for the settings screen",
    "deliverable": "widget.md", "scope": ["src/widgets/SettingsWidget.tsx"],
    "acceptance": ["the widget renders with accessible labels"],
    "answers": {"need": "repeatable_task", "difficulty": 0.2, "divisible": 0.1, "specific": 0.9},
}
CHECK_ROLE = {
    "name": "dashboard-widget-checker", "kind": "check",
    "mission": "Check the dashboard widget against its acceptance criteria",
    "deliverable": "check.md", "criteria": ["the widget renders with accessible labels"],
    "answers": {"need": "mechanical_verification", "difficulty": 0.2, "divisible": 0.0},
}
SPEC = {"task": "build a dashboard widget for the settings screen", "roles": [PRODUCE_ROLE]}


def test_a3_options_off_adds_no_typesafe_field_and_touches_no_network(stub: Stub, tmp_path: Path) -> None:
    view = cli.core_plan(CAT, spec=copy.deepcopy(SPEC), answers=BASE_TASK_ANSWERS, outage=False, judge_mode="off",
                         run_dir=str(tmp_path / "r"), allow=None, force=None)
    assert "typesafe" not in view and "owner_gates" not in view and stub.requests == []


def _skills_catalog(*, calibrated: bool = True, wide_rank_enabled: bool = False) -> dict[str, Any]:
    """`wide_rank_enabled` defaults False here (the shipped catalog now defaults it True) so every
    caller in this file that does not itself exercise the wide ranking stage keeps sending exactly
    the one skill-routing gated call these tests were written against; the wide-rank-specific tests
    below pass `wide_rank_enabled=True` explicitly and approve both gates."""
    root_globs = [str(FIX / "skills" / "flat" / "**" / "SKILL.md"), str(FIX / "skills" / "plugins" / "**" / "SKILL.md")]
    wide_rank = {**CAT["typesafe"]["skill_routing"]["wide_rank"], "enabled": wide_rank_enabled}
    ts = {**CAT["typesafe"],
         "skill_routing": {**CAT["typesafe"]["skill_routing"], "root_globs": root_globs, "calibrated": calibrated,
                          "wide_rank": wide_rank}}
    return {**CAT, "typesafe": ts}


def test_a3_skill_routing_gated_then_applied(stub: Stub, tmp_path: Path) -> None:
    """A3 (calibrated-flag-wiring-completer, 2026-09-22): with `typesafe.skill_routing.calibrated` flipped
    true in this test catalog, skill routing gates through the owner approval ledger, then attaches
    exactly as it did before crews/cli.py started reading the flag. This is the gate proven, not removed."""
    cat = _skills_catalog()
    spec = copy.deepcopy(SPEC)
    gated = cli.core_plan(cat, spec=spec, answers=BASE_TASK_ANSWERS, outage=False, judge_mode="off",
                          run_dir=str(tmp_path / "g"), allow=None, force=None, skills_mode="live")
    assert "SKILL.md" not in gated["waves"][0]["calls"][0]["agent_call"]["prompt"]
    assert gated["typesafe"]["skills"]["status"] == "UNVERIFIED"
    assert stub.requests == []
    approve_gate(gated)

    stub.script = [(200, {}, {"answers": {
        "skill.0": {"choice": "alpha-widget-builder"}, "fits.0.0": {"noul": 0.9},
        "procedure.0": {"noul": 0.9}, "generalist.0": {"noul": 0.1}}})]
    applied = cli.core_plan(cat, spec=spec, answers=BASE_TASK_ANSWERS, outage=False, judge_mode="off",
                            run_dir=str(tmp_path / "a"), allow=None, force=None, skills_mode="live")
    assert applied["typesafe"]["skills"]["status"] == "applied"
    assert applied["typesafe"]["skills"]["attached"] == {"dashboard-widget-builder": "alpha-widget-builder"}
    prompt = applied["waves"][0]["calls"][0]["agent_call"]["prompt"]
    assert "alpha-widget-builder/SKILL.md" in prompt
    audit_log = tmp_path / "audit" / "audit.jsonl"
    recs = [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines()]
    assert any(r["question_id"] == "skill.0" and r["case_or_role"] == "dashboard-widget-builder" for r in recs)
    assert not any("synthetic-test-key" in json.dumps(r) for r in recs)


def test_excluded_skill_never_offered_in_wide_request_or_skill_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """typesafe.skill_routing.exclude_skills (owner approved 2026-09-23): a session level skill such
    as `alpha-widget-builder` here, standing in for memory-first/using-superpowers, must never appear
    as a criteria option in either the wide ranking request or the per role skill request, even though
    PRODUCE_ROLE's mission is worded to match it (the same match test_a3 above relies on to attach it
    when it is not excluded). Exercised directly against `cli._apply_skill_routing`, mirroring how the
    wide-rank-only tests above exercise `cli._apply_wide_rank` directly, since the point under test is
    what `routable()` removes before either request body is built, not the gating round trip."""
    cat = _skills_catalog(calibrated=True, wide_rank_enabled=True)
    cat["typesafe"]["skill_routing"]["exclude_skills"] = ["alpha-widget-builder"]
    seen_bodies: list[dict[str, Any]] = []

    def fake_gated_call(body: dict[str, Any], **kw: Any) -> dict[str, Any]:
        seen_bodies.append(body)
        response: dict[str, Any]
        if "wide.0.0" in body["questions"]:
            response = {"answers": {"wide.0.0": {"probabilities": {"beta-report-generator": 0.9}}}}
        else:
            response = {"answers": {"skill.0": {"choice": "none"}, "fits.0.0": {"noul": 0.1},
                                    "procedure.0": {"noul": 0.1}, "generalist.0": {"noul": 0.9}}}
        return {"state": "answered", "packet": None, "response": response, "reason": None}

    monkeypatch.setattr(cli.egress, "gated_call", fake_gated_call)
    spec = copy.deepcopy(SPEC)
    notes: list[str] = []
    owner_gates: list[dict[str, Any]] = []
    cli._apply_skill_routing(cat, spec, notes, owner_gates)
    assert len(seen_bodies) == 2
    wide_body, skill_body = seen_bodies
    assert "alpha-widget-builder" not in wide_body["questions"]["wide.0.0"]["criteria"]
    assert "alpha-widget-builder" not in skill_body["questions"]["skill.0"]["criteria"]


def test_jev17_calibrated_false_never_attaches_through_cli_apply_skill_routing(
        stub: Stub, tmp_path: Path) -> None:
    """A2 (crew task calibrated-flag-wiring-completer, 2026-09-22): closes the wiring gap the prior role
    (typesafe-surfaces-activator) documented and stopped short of. `_apply_skill_routing` now reads
    `typesafe.skill_routing.calibrated` from the catalog and threads it into `skills_mod.decide(...,
    calibrated=...)`. With the flag false (the shipped catalog's real value: test_a5_catalog_typesafe_
    block_present_and_calibrated_false), a stubbed response that WOULD have attached "alpha-widget-builder"
    (the exact same script test_a3_skill_routing_gated_then_applied uses to prove the calibrated=True gate
    acts) is called, answered, and recorded, but the plan's roles stay unchanged: no SKILL.md path is
    ever added to the produce role's prompt. Proven through cli.core_plan (the shared core `crew plan` and
    the MCP `crew_plan` tool both call), not by calling crews.skills.decide in isolation."""
    cat = _skills_catalog(calibrated=False)
    assert cat["typesafe"]["skill_routing"]["calibrated"] is False
    spec = copy.deepcopy(SPEC)
    gated = cli.core_plan(cat, spec=spec, answers=BASE_TASK_ANSWERS, outage=False, judge_mode="off",
                          run_dir=str(tmp_path / "jev17-g"), allow=None, force=None, skills_mode="live")
    approve_gate(gated)
    stub.script = [(200, {}, {"answers": {
        "skill.0": {"choice": "alpha-widget-builder"}, "fits.0.0": {"noul": 0.9},
        "procedure.0": {"noul": 0.9}, "generalist.0": {"noul": 0.1}}})]
    applied = cli.core_plan(cat, spec=spec, answers=BASE_TASK_ANSWERS, outage=False, judge_mode="off",
                            run_dir=str(tmp_path / "jev17-a"), allow=None, force=None, skills_mode="live")
    # The call was made and answered (stub.requests proves it reached the network); it never acted.
    assert stub.requests
    assert applied["typesafe"]["skills"]["attached"] == {}
    prompt = applied["waves"][0]["calls"][0]["agent_call"]["prompt"]
    assert "SKILL.md" not in prompt
    audit_log = tmp_path / "audit" / "audit.jsonl"
    recs = [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines()]
    skill_rec = next(r for r in recs if r["question_id"] == "skill.0")
    assert "uncalibrated" in skill_rec["consequence"] and "JEV-17" in skill_rec["consequence"]


# --- DEFECT ONE (skill-routing-legibility-and-default-repairer, 2026-09-22): the skill routing surface
# printed byte identical operator visible output for an observation (uncalibrated) and a decision
# (calibrated), and the note it printed on the uncalibrated path was affirmatively false. The 0.99 fit,
# 0.99 gate case named in the crew task, driven through the real `crew.py plan --skills live` subprocess
# so the proof reads actual stdout, never an in-process function return. -----------------------------

_HIGH_FIT_GATE_SCRIPT: list[tuple[int, dict[str, str], dict[str, Any]]] = [(200, {}, {"answers": {
    "skill.0": {"choice": "alpha-widget-builder"}, "fits.0.0": {"noul": 0.99},
    "procedure.0": {"noul": 0.99}, "generalist.0": {"noul": 0.01}}})]


def _plan_via_subprocess(catalog_path: Path, roles_file: Path, answers_file: Path, run_dir: Path) -> dict[str, Any]:
    r = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "crew.py"), "--catalog", str(catalog_path),
                       "plan", "--roles", str(roles_file), "--answers", str(answers_file),
                       "--run-dir", str(run_dir), "--skills", "live"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    result: dict[str, Any] = json.loads(r.stdout)
    return result


def test_a1_a2_a3_operator_stdout_distinguishes_observation_from_decision_at_099(
        stub: Stub, tmp_path: Path) -> None:
    """A1/A2/A3: with the shipped uncalibrated catalog and an answered call returning choice
    `alpha-widget-builder` at fit 0.99 and gate 0.99 (far above the 0.30 thresholds), the observation
    output and a genuinely calibrated decision output at the SAME script must read differently on real
    stdout, and neither may claim a role failed to meet a threshold it actually cleared."""
    roles_file = tmp_path / "spec.json"
    roles_file.write_text(json.dumps(SPEC), encoding="utf-8")
    answers_file = tmp_path / "answers.json"
    answers_file.write_text(json.dumps(BASE_TASK_ANSWERS), encoding="utf-8")

    # --- observation: calibrated false (the shipped catalog's real value) -----------------------------
    uncal_cat = _skills_catalog(calibrated=False)
    uncal_cat_path = tmp_path / "uncal_catalog.json"
    uncal_cat_path.write_text(json.dumps(uncal_cat), encoding="utf-8")
    gated = _plan_via_subprocess(uncal_cat_path, roles_file, answers_file, tmp_path / "uncal-g")
    approve_gate(gated)
    stub.script = list(_HIGH_FIT_GATE_SCRIPT)
    observation = _plan_via_subprocess(uncal_cat_path, roles_file, answers_file, tmp_path / "uncal-a")
    assert stub.requests

    # --- decision: calibrated true, the identical 0.99/0.99 script -------------------------------------
    cal_cat = _skills_catalog(calibrated=True)
    cal_cat_path = tmp_path / "cal_catalog.json"
    cal_cat_path.write_text(json.dumps(cal_cat), encoding="utf-8")
    gated2 = _plan_via_subprocess(cal_cat_path, roles_file, answers_file, tmp_path / "cal-g")
    approve_gate(gated2)
    stub.script = list(_HIGH_FIT_GATE_SCRIPT)
    decision = _plan_via_subprocess(cal_cat_path, roles_file, answers_file, tmp_path / "cal-a")

    obs_skills = observation["typesafe"]["skills"]
    dec_skills = decision["typesafe"]["skills"]
    obs_role = obs_skills["per_role"]["dashboard-widget-builder"]
    dec_role = dec_skills["per_role"]["dashboard-widget-builder"]

    # A1: the two outputs are not byte identical, and the field that differs is `acted` (plus the
    # note): the observation records `acted: false`, the decision `acted: true`.
    assert obs_role["acted"] is False
    assert dec_role["acted"] is True
    assert obs_skills["attached"] != dec_skills["attached"]
    assert obs_skills["attached"] == {}
    assert dec_skills["attached"] == {"dashboard-widget-builder": "alpha-widget-builder"}
    assert observation["notes"] != decision["notes"]

    # A2: the observation's note never claims the 0.99/0.99 role failed to meet the (0.30/0.30)
    # thresholds; the false note this defect shipped is gone from both paths.
    obs_note = next(n for n in observation["notes"] if n.startswith("skill routing:"))
    dec_note = next(n for n in decision["notes"] if n.startswith("skill routing:"))
    assert "no role met the attach thresholds" not in obs_note
    assert "uncalibrated" in obs_note and "JEV-17" in obs_note
    assert "attached 1 of 1 role" in dec_note

    # A3: the non acting shape matches its two siblings (crews/cli.py's brief_leak per_check entries
    # and handback's judged verdict), both of which carry `acted` and, when uncalibrated, an
    # `observed_decision` naming what would have been decided had the threshold been trusted.
    assert obs_role["observed_decision"]["attach"] == "alpha-widget-builder"
    assert "uncalibrated" in obs_role["reason"] and "JEV-17" in obs_role["reason"]


# --- A5: each `calibrated` validator line in crews/catalog.py, asserted specifically enough that
# deleting any one of the three breaks a test by name (not merely a generic `!= []` check). --------

def test_a5_catalog_refuses_skill_routing_calibrated_wrong_type() -> None:
    bad = copy.deepcopy(CAT)
    bad["typesafe"]["skill_routing"]["calibrated"] = "not-a-bool"
    assert "typesafe.skill_routing.calibrated must be a boolean" in catalog.validate(bad)


def test_a5_catalog_refuses_handback_calibrated_wrong_type() -> None:
    bad = copy.deepcopy(CAT)
    bad["typesafe"]["handback"]["calibrated"] = "not-a-bool"
    assert "typesafe.handback.calibrated must be a boolean" in catalog.validate(bad)


def test_a5_catalog_refuses_brief_leak_calibrated_wrong_type() -> None:
    bad = copy.deepcopy(CAT)
    bad["typesafe"]["brief_leak"]["calibrated"] = "not-a-bool"
    assert "typesafe.brief_leak.calibrated must be a boolean" in catalog.validate(bad)


# --- A4/DEFECT TWO: an absent `calibrated` key must yield non acting at runtime, constructed directly
# rather than through crews.catalog.validate (the validator that makes this state unreachable through
# the normal `_load` path used by `cmd_plan` and `cmd_judge_handback`). ---------------------------------

def _skills_catalog_calibrated_absent() -> dict[str, Any]:
    root_globs = [str(FIX / "skills" / "flat" / "**" / "SKILL.md"), str(FIX / "skills" / "plugins" / "**" / "SKILL.md")]
    sr = {k: v for k, v in CAT["typesafe"]["skill_routing"].items() if k != "calibrated"}
    sr["root_globs"] = root_globs
    sr["wide_rank"] = {**sr["wide_rank"], "enabled": False}
    assert "calibrated" not in sr
    ts = {**CAT["typesafe"], "skill_routing": sr}
    return {**CAT, "typesafe": ts}


def test_jev17_skill_routing_absent_calibrated_key_defaults_to_non_acting(stub: Stub, tmp_path: Path) -> None:
    """A4: `cat["typesafe"]["skill_routing"]` carries no `calibrated` key at all, constructed directly
    rather than loaded and validated through `crews.catalog.load`/`validate`. The runtime read in
    `crews.cli._apply_skill_routing` must still refuse to act: the exact stub response that attaches
    under calibrated=True never attaches here."""
    cat = _skills_catalog_calibrated_absent()
    spec = copy.deepcopy(SPEC)
    gated = cli.core_plan(cat, spec=spec, answers=BASE_TASK_ANSWERS, outage=False, judge_mode="off",
                          run_dir=str(tmp_path / "absent-g"), allow=None, force=None, skills_mode="live")
    approve_gate(gated)
    stub.script = list(_HIGH_FIT_GATE_SCRIPT)
    applied = cli.core_plan(cat, spec=spec, answers=BASE_TASK_ANSWERS, outage=False, judge_mode="off",
                            run_dir=str(tmp_path / "absent-a"), allow=None, force=None, skills_mode="live")
    assert stub.requests
    assert applied["typesafe"]["skills"]["attached"] == {}
    prompt = applied["waves"][0]["calls"][0]["agent_call"]["prompt"]
    assert "SKILL.md" not in prompt
    role_decision = applied["typesafe"]["skills"]["per_role"]["dashboard-widget-builder"]
    assert role_decision["acted"] is False


def _brief_leak_catalog_calibrated_absent() -> dict[str, Any]:
    bl = {k: v for k, v in CAT["typesafe"]["brief_leak"].items() if k != "calibrated"}
    assert "calibrated" not in bl
    ts = {**CAT["typesafe"], "brief_leak": bl}
    return {**CAT, "typesafe": ts}


def test_jev17_brief_leak_absent_calibrated_key_defaults_to_non_acting(stub: Stub, tmp_path: Path) -> None:
    """A4: `cat["typesafe"]["brief_leak"]` carries no `calibrated` key at all, constructed directly
    rather than through the validator. The runtime read in `crews.cli._apply_brief_check` must still
    refuse to mark LEAK: the exact stub Noul that marks LEAK under calibrated=True never does here."""
    cat = _brief_leak_catalog_calibrated_absent()
    spec = {"task": SPEC["task"], "roles": [PRODUCE_ROLE, CHECK_ROLE]}
    gated = cli.core_plan(cat, spec=copy.deepcopy(spec), answers=BASE_TASK_ANSWERS, outage=False,
                          judge_mode="off", run_dir=str(tmp_path / "absent-g"), allow=2, force=None,
                          brief_check_mode="live")
    approve_gate(gated)
    stub.script = [(200, {}, {"answers": {"leak": {"noul": 0.9}}})]
    applied = cli.core_plan(cat, spec=copy.deepcopy(spec), answers=BASE_TASK_ANSWERS, outage=False,
                            judge_mode="off", run_dir=str(tmp_path / "absent-a"), allow=2, force=None,
                            brief_check_mode="live")
    assert stub.requests
    assert applied["typesafe"]["brief_check"]["status"] == "UNVERIFIED"
    per_check = applied["typesafe"]["brief_check"]["per_check"]
    decision = next(iter(per_check.values()))
    assert decision["acted"] is False


def _handback_catalog_calibrated_absent() -> dict[str, Any]:
    hb = {k: v for k, v in CAT["typesafe"]["handback"].items() if k != "calibrated"}
    assert "calibrated" not in hb
    ts = {**CAT["typesafe"], "handback": hb}
    return {**CAT, "typesafe": ts}




def test_a3_brief_check_gated_owner_gate_is_unverified(stub: Stub, tmp_path: Path) -> None:
    spec = {"task": SPEC["task"], "roles": [PRODUCE_ROLE, CHECK_ROLE]}
    view = cli.core_plan(CAT, spec=spec, answers=BASE_TASK_ANSWERS, outage=False, judge_mode="off",
                         run_dir=str(tmp_path / "r"), allow=2, force=None, brief_check_mode="live")
    assert view["typesafe"]["brief_check"]["status"] == "UNVERIFIED"
    assert view["owner_gates"] and stub.requests == []


def _brief_leak_catalog(*, calibrated: bool) -> dict[str, Any]:
    ts = {**CAT["typesafe"], "brief_leak": {**CAT["typesafe"]["brief_leak"], "calibrated": calibrated}}
    return {**CAT, "typesafe": ts}


def test_a3_brief_check_calibrated_true_marks_leak(stub: Stub, tmp_path: Path) -> None:
    """A3 (calibrated-flag-wiring-completer, 2026-09-22): with typesafe.brief_leak.calibrated flipped true
    in this test catalog, a semantic Noul at or above leak_threshold marks the check role's rendered brief
    LEAK, exactly as before crews/cli.py started reading the flag. The gate is proven, not removed."""
    cat = _brief_leak_catalog(calibrated=True)
    spec = {"task": SPEC["task"], "roles": [PRODUCE_ROLE, CHECK_ROLE]}
    gated = cli.core_plan(cat, spec=copy.deepcopy(spec), answers=BASE_TASK_ANSWERS, outage=False,
                          judge_mode="off", run_dir=str(tmp_path / "g"), allow=2, force=None,
                          brief_check_mode="live")
    approve_gate(gated)
    stub.script = [(200, {}, {"answers": {"leak": {"noul": 0.9}}})]
    applied = cli.core_plan(cat, spec=copy.deepcopy(spec), answers=BASE_TASK_ANSWERS, outage=False,
                            judge_mode="off", run_dir=str(tmp_path / "a"), allow=2, force=None,
                            brief_check_mode="live")
    assert applied["typesafe"]["brief_check"]["status"] == "LEAK"


def test_jev17_brief_leak_calibrated_false_never_marks_leak(stub: Stub, tmp_path: Path) -> None:
    """A2 (calibrated-flag-wiring-completer, 2026-09-22): `_apply_brief_check` now reads
    `typesafe.brief_leak.calibrated` from the catalog and threads it into `briefcheck.decide(...,
    calibrated=...)`. With the flag false (the shipped catalog's real value), the exact stub Noul that
    marks LEAK under test_a3_brief_check_calibrated_true_marks_leak above is called, answered, and
    recorded, but the plan's brief_check status stays UNVERIFIED, never LEAK."""
    cat = _brief_leak_catalog(calibrated=False)
    assert cat["typesafe"]["brief_leak"]["calibrated"] is False
    spec = {"task": SPEC["task"], "roles": [PRODUCE_ROLE, CHECK_ROLE]}
    gated = cli.core_plan(cat, spec=copy.deepcopy(spec), answers=BASE_TASK_ANSWERS, outage=False,
                          judge_mode="off", run_dir=str(tmp_path / "g"), allow=2, force=None,
                          brief_check_mode="live")
    approve_gate(gated)
    stub.script = [(200, {}, {"answers": {"leak": {"noul": 0.9}}})]
    applied = cli.core_plan(cat, spec=copy.deepcopy(spec), answers=BASE_TASK_ANSWERS, outage=False,
                            judge_mode="off", run_dir=str(tmp_path / "a"), allow=2, force=None,
                            brief_check_mode="live")
    assert stub.requests
    assert applied["typesafe"]["brief_check"]["status"] == "UNVERIFIED"
    per_check = applied["typesafe"]["brief_check"]["per_check"]
    decision = next(iter(per_check.values()))
    assert decision["acted"] is False and "uncalibrated" in decision["reason"] and "JEV-17" in decision["reason"]
    assert decision["observed_decision"]["status"] == "LEAK"


# --- A2/A3/A5 (calibrated-flag-wiring-completer, 2026-09-22): the handback surface, reachable only through
# `crew judge-handback` (cmd_judge_handback), since server.py exposes no MCP tool for it. Driven through
# cli.main, the real CLI entry point (a tests/test_cli.py was considered for this coverage but collides on
# module basename with the pre-existing tests/detection_command/test_cli.py under pytest's default import
# mode, so it stays here alongside the skill_routing and brief_leak surfaces).

HANDBACK_TEXT = "VERDICT: PASS\nPASS: C1\nFAIL: NONE\nUNVERIFIED: NONE\nDECISION: NONE\n"


def _handback_catalog(*, calibrated: bool) -> dict[str, Any]:
    ts = {**CAT["typesafe"], "handback": {**CAT["typesafe"]["handback"], "calibrated": calibrated}}
    return {**CAT, "typesafe": ts}


def _write_handback_fixtures(tmp_path: Path) -> tuple[Path, Path]:
    hb = tmp_path / "handback.txt"
    hb.write_text(HANDBACK_TEXT, encoding="utf-8")
    criteria = tmp_path / "criteria.json"
    criteria.write_text(json.dumps({"C1": "the fix works"}), encoding="utf-8")
    return hb, criteria


def _judge_handback_cli(cat: dict[str, Any], hb: Path, criteria: Path, out: Path, tmp_path: Path) -> int:
    cat_path = tmp_path / "catalog.json"
    cat_path.write_text(json.dumps(cat), encoding="utf-8")
    return cli.main(["--catalog", str(cat_path), "judge-handback", "--handback", str(hb),
                     "--criteria", str(criteria), "--out", str(out)])


def _approve_all_pending() -> None:
    pending_path = Path(os.environ["CREWS_EGRESS_DIR"]) / "pending.jsonl"
    for line in pending_path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        egress.approve(rec["call_id"], rec["fingerprint"])






# --- A4: byte-equal Agent prompts against the pre-mission baseline, options off --------------------------------







# --- A5: catalog typesafe block ---------------------------------------------------------------------------

def test_a5_catalog_typesafe_block_present_and_calibrated_false() -> None:
    ts = CAT["typesafe"]
    assert ts["skill_routing"]["calibrated"] is False
    assert ts["handback"]["calibrated"] is False and ts["brief_leak"]["calibrated"] is False
    assert catalog.validate(CAT) == []


@pytest.mark.parametrize("mutate", [
    lambda c: c["typesafe"].__setitem__("skill_routing", {"root_globs": "not-a-list"}),
    lambda c: c["typesafe"]["handback"].__setitem__("probe_threshold", "not-a-number"),
    lambda c: c["typesafe"]["brief_leak"].__setitem__("calibrated", "not-a-bool"),
])
def test_a5_catalog_refuses_wrong_types(mutate: Any) -> None:
    bad = copy.deepcopy(CAT)
    mutate(bad)
    assert catalog.validate(bad) != []


# --- A6: CLI verbs ------------------------------------------------------------------------------------------



UNLABELED_TASKS = FIX / "calibration" / "tasks_unlabeled.json"








# --- A7: server passes the new options through, and approves nothing -------------------------------------------

def test_a7_server_exposes_no_approve_tool() -> None:
    pytest.importorskip("mcp")
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import get_default_environment, stdio_client

    async def main() -> set[str]:
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")],
                                       env=get_default_environment())
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = (await s.list_tools()).tools
                return {t.name for t in tools}

    names = anyio.run(main)
    assert not any("approve" in n for n in names)
    assert "crew_plan" in names and "crew_budget" in names


def test_a4_crew_plan_and_crew_budget_now_default_live() -> None:
    """A4 (crew task calibrated-flag-wiring-completer, 2026-09-22): the four MCP tool defaults directed by
    the 2026-09-22 owner directive are now "live", completing the flip the prior role
    (typesafe-surfaces-activator) documented and stopped short of at this exact boundary (see this test's
    own predecessor, previously named test_a7_crew_plan_accepts_skills_and_brief_check_and_stays_off_by_
    default, and server.py's module docstring "Calibration note")."""
    import inspect

    sys.path.insert(0, str(ROOT))
    import server as server_mod  # noqa: PLC0415
    budget_sig = inspect.signature(server_mod.crew_budget)
    plan_sig = inspect.signature(server_mod.crew_plan)
    assert {"judge", "skills", "brief_check"} <= set(plan_sig.parameters)
    assert budget_sig.parameters["judge"].default == "live"
    assert plan_sig.parameters["judge"].default == "live"
    assert plan_sig.parameters["skills"].default == "live" and plan_sig.parameters["brief_check"].default == "live"


def test_a4_crew_budget_live_default_with_no_key_file_falls_back_to_off_with_a_warning(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A4: on a stock machine (no TYPESAFE_API_KEY_FILE, matching the CI/dev environment this suite runs
    in), calling `crew_budget` with its new "live" default and declared `answers` (exactly the shape
    several tests/test_server.py calls exercise) now ends up "off" with a Q9 warning and the budget
    computed from the declared answers, COMPLETED_WITH_WARNINGS, not BLOCKED: the forced-outage-versus-
    declared-answers conflict check this test's predecessor proved (previously named test_a7_crew_budget_
    judge_live_with_no_key_file_and_declared_answers_would_refuse) is gone from server.py's crew_budget
    body; server._live_gate still forces the downgrade, it just no longer collides with declared answers."""
    import inspect

    sys.path.insert(0, str(ROOT))
    import server as server_mod  # noqa: PLC0415
    sig = inspect.signature(server_mod.crew_budget)
    assert sig.parameters["judge"].default == "live"

    monkeypatch.delenv("TYPESAFE_API_KEY_FILE", raising=False)
    answers = {"need": {"choice": "everyday_implementation", "confidence": 0.9,
                        "probabilities": {"everyday_implementation": 0.9}},
              "divisible": {"noul": 0.1}, "needs_verifier": {"noul": 1.0}, "difficulty": {"score": 1.0}}
    env = server_mod.crew_budget(answers=answers, run_dir=str(tmp_path / "default-live-no-key"))
    assert env["status"] == "COMPLETED_WITH_WARNINGS"
    assert any("SRD Q9" in w for w in env["warnings"])
    assert env["output"]["need"] == "everyday_implementation"


# --- A8: audit records carry no secret ---------------------------------------------------------------------

def test_a8_audit_records_hold_no_sentinel(stub: Stub, tmp_path: Path) -> None:
    gated = cli.core_budget(CAT, answers=None, outage=False, judge_mode="live", task="task G: audit sentinel",
                            run_dir=str(tmp_path / "g"))
    approve_gate(gated)
    stub.script = [(200, {}, need_body(0.9))]
    cli.core_budget(CAT, answers=None, outage=False, judge_mode="live", task="task G: audit sentinel",
                    run_dir=str(tmp_path / "a"))
    audit_log = tmp_path / "audit" / "audit.jsonl"
    assert audit_log.exists()
    text = audit_log.read_text(encoding="utf-8")
    assert "synthetic-test-key" not in text


# sanity: the producer modules import cleanly from this wiring layer's own imports -------------------------------

def test_producer_modules_importable() -> None:
    assert all([bands, briefcheck, egress, handback, skills])


# --- wide ranking stage (skill-routing-wide-rank-stage-builder, 2026-09-23): TSI-SKL-02 egress
# discipline over crews.cli._apply_wide_rank, exercised directly rather than through the full
# core_plan round trip above, since its fallback and merge behavior turn on `egress.gated_call`'s
# outcome, not on the ledger's approve/consume mechanics those tests already cover. -------------------

WIDE_ROLE = {"name": "dashboard-widget-builder", "mission": "Build a reusable dashboard widget"}
WIDE_INDEX = [
    {"id": "alpha-widget-builder", "name": "alpha-widget-builder", "description": "widgets", "path": "/tmp/a"},
    {"id": "beta-report-generator", "name": "beta-report-generator", "description": "reports", "path": "/tmp/b"},
]
WIDE_BM25 = [[{"id": "alpha-widget-builder", "name": "alpha-widget-builder", "description": "widgets",
              "path": "/tmp/a"}]]
WIDE_SR = {"wide_rank": {"enabled": True, "top_k": 5, "desc_chars": 160}}


def test_apply_wide_rank_disabled_returns_candidates_unchanged() -> None:
    notes: list[str] = []
    owner_gates: list[dict[str, Any]] = []
    out = cli._apply_wide_rank({"wide_rank": {"enabled": False}}, "t", [WIDE_ROLE], WIDE_INDEX, WIDE_BM25,
                               notes, owner_gates)
    assert out == WIDE_BM25
    assert notes == [] and owner_gates == []


def test_apply_wide_rank_owner_gate_falls_back_to_bm25(monkeypatch: pytest.MonkeyPatch) -> None:
    pkt = {"call_id": "ts-wide-1", "fingerprint": "fp"}
    monkeypatch.setattr(cli.egress, "gated_call",
                        lambda *a, **kw: {"state": "owner_gate", "packet": pkt, "response": None, "reason": None})
    notes: list[str] = []
    owner_gates: list[dict[str, Any]] = []
    out = cli._apply_wide_rank(WIDE_SR, "t", [WIDE_ROLE], WIDE_INDEX, WIDE_BM25, notes, owner_gates)
    assert out == WIDE_BM25
    assert owner_gates == [pkt]
    assert any(n.startswith("skill routing wide rank: owner_gate") for n in notes)


def test_apply_wide_rank_transport_failure_falls_back_to_bm25(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.egress, "gated_call", lambda *a, **kw: {
        "state": "failed", "packet": None, "response": None, "reason": "TypeSafe unavailable"})
    notes: list[str] = []
    owner_gates: list[dict[str, Any]] = []
    out = cli._apply_wide_rank(WIDE_SR, "t", [WIDE_ROLE], WIDE_INDEX, WIDE_BM25, notes, owner_gates)
    assert out == WIDE_BM25
    assert owner_gates == []
    assert any("TypeSafe unavailable" in n and "BM25 candidates only" in n for n in notes)


def test_apply_wide_rank_malformed_response_falls_back_to_bm25(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.egress, "gated_call", lambda *a, **kw: {
        "state": "answered", "packet": None, "response": {"answers": {}}, "reason": None})
    notes: list[str] = []
    owner_gates: list[dict[str, Any]] = []
    out = cli._apply_wide_rank(WIDE_SR, "t", [WIDE_ROLE], WIDE_INDEX, WIDE_BM25, notes, owner_gates)
    assert out == WIDE_BM25
    assert any("wide.0.0" in n and "BM25 candidates only" in n for n in notes)


def test_apply_wide_rank_merges_top_picks_onto_bm25_when_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    response = {"answers": {"wide.0.0": {"probabilities": {
        "beta-report-generator": 0.9, "alpha-widget-builder": 0.1}}}}
    seen_bodies: list[dict[str, Any]] = []

    def fake_gated_call(body: dict[str, Any], **kw: Any) -> dict[str, Any]:
        seen_bodies.append(body)
        return {"state": "answered", "packet": None, "response": response, "reason": None}

    monkeypatch.setattr(cli.egress, "gated_call", fake_gated_call)
    notes: list[str] = []
    owner_gates: list[dict[str, Any]] = []
    out = cli._apply_wide_rank(WIDE_SR, "t", [WIDE_ROLE], WIDE_INDEX, WIDE_BM25, notes, owner_gates)
    assert [s["id"] for s in out[0]] == ["alpha-widget-builder", "beta-report-generator"]
    assert owner_gates == []
    assert any(n.startswith("skill routing wide rank: merged") for n in notes)
    # the wide request itself carries no candidate list and no `none` option: it ranks the whole index.
    assert set(seen_bodies[0]["questions"]["wide.0.0"]["criteria"]) == {"alpha-widget-builder",
                                                                        "beta-report-generator"}


def test_apply_wide_rank_top_k_caps_how_many_wide_picks_are_added(monkeypatch: pytest.MonkeyPatch) -> None:
    index = WIDE_INDEX + [{"id": "gamma-tool", "name": "gamma-tool", "description": "g", "path": "/tmp/g"}]
    response = {"answers": {"wide.0.0": {"probabilities": {
        "beta-report-generator": 0.9, "gamma-tool": 0.8, "alpha-widget-builder": 0.1}}}}
    monkeypatch.setattr(cli.egress, "gated_call", lambda *a, **kw: {
        "state": "answered", "packet": None, "response": response, "reason": None})
    notes: list[str] = []
    owner_gates: list[dict[str, Any]] = []
    sr = {"wide_rank": {"enabled": True, "top_k": 1, "desc_chars": 160}}
    out = cli._apply_wide_rank(sr, "t", [WIDE_ROLE], index, WIDE_BM25, notes, owner_gates)
    assert [s["id"] for s in out[0]] == ["alpha-widget-builder", "beta-report-generator"]
