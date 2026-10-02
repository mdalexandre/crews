"""crews.handback (C-HB): mechanical hand back checks, probe/verify request builders, and the judged
verdict rule. TSI-HB-01 to TSI-HB-03, TSI-GATE-05 (typesafe-integration-v1.0.md section 2.5 C-HB, section
3). Fixture hand backs are synthetic: no real crew check seat or credential ever appears in them."""

from __future__ import annotations

from pathlib import Path

import pytest

from crews import handback
from crews.verdict import Verdict

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "handback"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- module surface for the integrator (A1) --------------------------------------------------------------

def test_data_category_and_includes_worker_output_exposed() -> None:
    assert isinstance(handback.DATA_CATEGORY, str) and handback.DATA_CATEGORY
    assert handback.INCLUDES_WORKER_OUTPUT is True


# --- check_mechanical: MALFORMED before any request is built (TSI-HB-01) ---------------------------------

def test_check_mechanical_well_formed_genuine_probe() -> None:
    mech = handback.check_mechanical(_read("genuine_probe.txt"), ["A1", "A2"])
    assert mech.status == "OK"
    assert isinstance(mech.verdict, Verdict)
    assert mech.verdict.passed_ids == frozenset({"A1", "A2"})


def test_check_mechanical_malformed_no_verdict_line() -> None:
    mech = handback.check_mechanical(_read("malformed_no_verdict_line.txt"), ["A1", "A2"])
    assert mech.status == "MALFORMED"
    assert mech.verdict is None
    assert "VERDICT" in (mech.reason or "")


def test_check_mechanical_malformed_criterion_listed_twice() -> None:
    mech = handback.check_mechanical(_read("malformed_criterion_listed_twice.txt"), ["A1", "A2"])
    assert mech.status == "MALFORMED"
    assert mech.verdict is None
    assert "A1" in (mech.reason or "") and "more than once" in (mech.reason or "")


def test_check_mechanical_malformed_fail_without_evidence() -> None:
    mech = handback.check_mechanical(_read("malformed_fail_without_evidence.txt"), ["A1", "A2"])
    assert mech.status == "MALFORMED"
    assert mech.verdict is None


def test_check_mechanical_malformed_criterion_missing_from_block() -> None:
    # PASS: A1 only, but the criteria set names A1 and A2: A2 is accounted for nowhere.
    mech = handback.check_mechanical(_read("pure_restatement.txt"), ["A1", "A2"])
    assert mech.status == "MALFORMED"
    assert "A2" in (mech.reason or "")


def test_check_mechanical_accepts_the_fail_none_variant() -> None:
    mech = handback.check_mechanical(_read("paraphrased_producer_conclusion.txt"), ["A1", "A2"])
    assert mech.status == "OK"
    assert mech.verdict is not None
    assert mech.verdict.passed_ids == frozenset({"A1", "A2"})
    assert mech.verdict.failed == {}


# --- request_check: state carries the hand back text and the criteria (A3) --------------------------------

def test_request_check_builds_one_probe_noul_per_pass_id_and_carries_state() -> None:
    text = _read("genuine_probe.txt")
    criteria = {"A1": "Descriptive stats fixtures pass.", "A2": "Sales tax fixtures pass."}
    mech = handback.check_mechanical(text, ["A1", "A2"])
    assert mech.status == "OK" and mech.verdict is not None
    body = handback.request_check(text, criteria, mech.verdict.passed_ids)
    assert body["model"] == handback.MODEL
    assert body["state"]["handback"] == text
    assert body["state"]["criteria"] == criteria
    assert set(body["questions"]) == {"probe.A1", "probe.A2"}
    for q in body["questions"].values():
        assert q["type"] == "noul"
        assert isinstance(q["instructions"], str) and q["instructions"]


def test_request_check_only_asks_about_pass_ids_not_fail_or_unverified() -> None:
    text = _read("mixed_pass_and_fail.txt")
    mech = handback.check_mechanical(text, ["A1", "A2"])
    assert mech.status == "OK" and mech.verdict is not None
    body = handback.request_check(text, {"A1": "x", "A2": "y"}, mech.verdict.passed_ids)
    assert set(body["questions"]) == {"probe.A1"}


# --- parse_probe_response validates against the question (TSI-VAL-01) -------------------------------------

def test_parse_probe_response_valid() -> None:
    response = {"answers": {"probe.A1": {"noul": 0.87}, "probe.A2": {"noul": 0.12}}}
    out = handback.parse_probe_response(response, ["A1", "A2"])
    assert out == {"A1": 0.87, "A2": 0.12}


@pytest.mark.parametrize("response", [
    {},
    {"answers": {}},
    {"answers": {"probe.A1": {"noul": 1.5}}},
    {"answers": {"probe.A1": {"noul": "high"}}},
    {"answers": {"probe.A1": {}}},
])
def test_parse_probe_response_invalid_raises(response: dict) -> None:
    with pytest.raises(handback.HandbackParseError):
        handback.parse_probe_response(response, ["A1"])


# --- apply: restatement, paraphrase, claimed test with no output all become UNVERIFIED (TSI-HB-02) --------

def test_apply_genuine_probe_evidence_stays_pass() -> None:
    mech = handback.check_mechanical(_read("genuine_probe.txt"), ["A1", "A2"])
    assert mech.verdict is not None
    result = handback.apply(mech.verdict, {"A1": 0.9, "A2": 0.81}, threshold=0.5)
    assert result["per_criterion"] == {"A1": "PASS", "A2": "PASS"}
    assert result["verdict"] == "PASS"
    assert result["reasons"] == {}


def test_apply_pure_restatement_becomes_unverified() -> None:
    mech = handback.check_mechanical(_read("pure_restatement.txt"), ["A1"])
    assert mech.verdict is not None
    result = handback.apply(mech.verdict, {"A1": 0.1}, threshold=0.5)
    assert result["per_criterion"] == {"A1": "UNVERIFIED"}
    assert result["reasons"] == {"A1": "restatement"}
    assert result["verdict"] == "UNVERIFIED"


def test_apply_paraphrased_producer_conclusion_becomes_unverified() -> None:
    mech = handback.check_mechanical(_read("paraphrased_producer_conclusion.txt"), ["A1", "A2"])
    assert mech.verdict is not None
    result = handback.apply(mech.verdict, {"A1": 0.9, "A2": 0.05}, threshold=0.5)
    assert result["per_criterion"]["A1"] == "PASS"
    assert result["per_criterion"]["A2"] == "UNVERIFIED"
    assert result["reasons"] == {"A2": "restatement"}
    assert result["verdict"] == "UNVERIFIED"


def test_apply_claimed_test_result_with_no_output_becomes_unverified() -> None:
    mech = handback.check_mechanical(_read("claimed_test_no_output.txt"), ["A1"])
    assert mech.verdict is not None
    result = handback.apply(mech.verdict, {"A1": 0.2}, threshold=0.5)
    assert result["per_criterion"] == {"A1": "UNVERIFIED"}
    assert result["reasons"] == {"A1": "restatement"}


def test_apply_exactly_at_threshold_is_not_below_and_stays_pass() -> None:
    mech = handback.check_mechanical(_read("pure_restatement.txt"), ["A1"])
    assert mech.verdict is not None
    result = handback.apply(mech.verdict, {"A1": 0.5}, threshold=0.5)
    assert result["per_criterion"] == {"A1": "PASS"}


# --- apply: nouls=None gives UNJUDGED, never PASS (TSI-HB-03) ----------------------------------------------

def test_apply_no_judgment_gives_unjudged_never_pass() -> None:
    mech = handback.check_mechanical(_read("genuine_probe.txt"), ["A1", "A2"])
    assert mech.verdict is not None
    result = handback.apply(mech.verdict, None, threshold=0.5)
    assert result["verdict"] == "UNJUDGED"
    assert result["per_criterion"] == {"A1": "UNJUDGED", "A2": "UNJUDGED"}
    assert "PASS" not in result["per_criterion"].values()


# --- the judged verdict rule: PASS only if every criterion is PASS, FAIL if any is FAIL, else UNVERIFIED ---

def test_judged_verdict_rule_fail_when_any_criterion_fails_regardless_of_noul() -> None:
    mech = handback.check_mechanical(_read("mixed_pass_and_fail.txt"), ["A1", "A2"])
    assert mech.verdict is not None
    result = handback.apply(mech.verdict, {"A1": 0.99}, threshold=0.5)
    assert result["per_criterion"] == {"A1": "PASS", "A2": "FAIL"}
    assert result["verdict"] == "FAIL"


def test_judged_verdict_rule_pass_only_when_every_criterion_passes() -> None:
    mech = handback.check_mechanical(_read("genuine_probe.txt"), ["A1", "A2"])
    assert mech.verdict is not None
    below = handback.apply(mech.verdict, {"A1": 0.9, "A2": 0.1}, threshold=0.5)
    assert below["verdict"] == "UNVERIFIED"
    above = handback.apply(mech.verdict, {"A1": 0.9, "A2": 0.9}, threshold=0.5)
    assert above["verdict"] == "PASS"


def test_judged_verdict_rule_declared_unverified_criterion_keeps_hand_back_from_pass() -> None:
    v = Verdict(overall="BLOCKED", passed_ids=frozenset({"A1"}), failed={},
                unverified_raw="A2: artifact missing", decision="owner must supply the missing file")
    result = handback.apply(v, {"A1": 0.9}, threshold=0.5)
    assert result["per_criterion"] == {"A1": "PASS", "A2": "UNVERIFIED"}
    assert result["verdict"] == "UNVERIFIED"


# --- apply, calibrated=False (JEV-17): the probe is called and recorded, never applied -------------------

def test_apply_calibrated_default_true_is_unchanged_and_marks_acted() -> None:
    mech = handback.check_mechanical(_read("pure_restatement.txt"), ["A1"])
    assert mech.verdict is not None
    default_result = handback.apply(mech.verdict, {"A1": 0.1}, threshold=0.5)
    explicit_result = handback.apply(mech.verdict, {"A1": 0.1}, threshold=0.5, calibrated=True)
    assert default_result["per_criterion"] == explicit_result["per_criterion"] == {"A1": "UNVERIFIED"}
    assert explicit_result["acted"] is True


def test_apply_uncalibrated_restatement_stays_claimed_pass_not_downgraded() -> None:
    # Below threshold: calibrated=True would downgrade this to UNVERIFIED (proven above); uncalibrated
    # must never apply that downgrade, so the claimed PASS stands.
    mech = handback.check_mechanical(_read("pure_restatement.txt"), ["A1"])
    assert mech.verdict is not None
    result = handback.apply(mech.verdict, {"A1": 0.1}, threshold=0.5, calibrated=False)
    assert result["per_criterion"] == {"A1": "PASS"}
    assert result["verdict"] == "PASS"
    assert result["acted"] is False
    # The call and its answer are recorded, legibly, so a reader can tell it observed rather than decided.
    assert result["observed_nouls"] == {"A1": 0.1}
    assert result["observed_decision"]["per_criterion"] == {"A1": "UNVERIFIED"}
    assert "uncalibrated" in result["note"]


def test_apply_uncalibrated_fail_claims_are_unaffected_by_calibration() -> None:
    # FAIL is a producer claim from the fixed VERDICT block, never a Noul judgment; calibration must not
    # touch it either way.
    mech = handback.check_mechanical(_read("mixed_pass_and_fail.txt"), ["A1", "A2"])
    assert mech.verdict is not None
    result = handback.apply(mech.verdict, {"A1": 0.99}, threshold=0.5, calibrated=False)
    assert result["per_criterion"] == {"A1": "PASS", "A2": "FAIL"}
    assert result["verdict"] == "FAIL"
    assert result["acted"] is False


def test_apply_uncalibrated_no_judgment_still_gives_unjudged_never_pass() -> None:
    # TSI-HB-03 holds regardless of calibration: with no answer there is nothing to record or apply.
    mech = handback.check_mechanical(_read("genuine_probe.txt"), ["A1", "A2"])
    assert mech.verdict is not None
    result = handback.apply(mech.verdict, None, threshold=0.5, calibrated=False)
    assert result["verdict"] == "UNJUDGED"
    assert result["per_criterion"] == {"A1": "UNJUDGED", "A2": "UNJUDGED"}
    assert result["acted"] is False


# --- artifact mode (A2, A3) ----------------------------------------------------------------------------

def test_artifact_mechanical_well_formed() -> None:
    mech = handback.artifact_mechanical(_read("artifact_mode.txt"), ["A1", "A2"])
    assert mech.status == "OK"
    assert mech.parsed is not None
    assert mech.parsed.top_status == "COMPLETE"
    assert mech.parsed.verify_ids == frozenset({"A1", "A2"})
    assert mech.parsed.unverified_ids == frozenset()


def test_artifact_mechanical_malformed_no_status_line() -> None:
    text = "ARTIFACTS: none\nRESULT: nothing\nVERIFY: A1 met\nDECISION: NONE\n"
    mech = handback.artifact_mechanical(text, ["A1"])
    assert mech.status == "MALFORMED"
    assert mech.parsed is None


def test_artifact_mechanical_malformed_no_verify_line() -> None:
    text = "STATUS: COMPLETE\nARTIFACTS: none\nRESULT: nothing\nDECISION: NONE\n"
    mech = handback.artifact_mechanical(text, ["A1"])
    assert mech.status == "MALFORMED"
    assert mech.parsed is None


def test_request_artifact_builds_one_verify_noul_per_acceptance_id_and_carries_state() -> None:
    text = _read("artifact_mode.txt")
    acceptance = {"A1": "mypy passes", "A2": "pytest passes"}
    body = handback.request_artifact(text, acceptance, ["A1", "A2"])
    assert body["state"]["handback"] == text
    assert body["state"]["acceptance"] == acceptance
    assert set(body["questions"]) == {"verify.A1", "verify.A2"}
    for q in body["questions"].values():
        assert q["type"] == "noul"


def test_parse_verify_response_valid() -> None:
    response = {"answers": {"verify.A1": {"noul": 0.95}}}
    assert handback.parse_verify_response(response, ["A1"]) == {"A1": 0.95}


def test_apply_artifact_mode_unclaimed_id_is_unverified_not_pass() -> None:
    mech = handback.artifact_mechanical(
        "STATUS: PARTIAL\nARTIFACTS: none\nRESULT: only A1 done\nVERIFY: A1 met by pytest (3 passed).\n"
        "DECISION: NONE\n", ["A1", "A2"])
    assert mech.status == "OK" and mech.parsed is not None
    assert mech.parsed.verify_ids == frozenset({"A1"})
    assert mech.parsed.unverified_ids == frozenset({"A2"})
    result = handback.apply(mech.parsed, {"A1": 0.9}, threshold=0.5)
    assert result["per_criterion"] == {"A1": "PASS", "A2": "UNVERIFIED"}
    assert result["verdict"] == "UNVERIFIED"


def test_apply_artifact_mode_uncalibrated_below_threshold_stays_claimed_pass() -> None:
    mech = handback.artifact_mechanical(_read("artifact_mode.txt"), ["A1", "A2"])
    assert mech.status == "OK" and mech.parsed is not None
    calibrated = handback.apply(mech.parsed, {"A1": 0.1, "A2": 0.1}, threshold=0.5)
    assert calibrated["per_criterion"] == {"A1": "UNVERIFIED", "A2": "UNVERIFIED"}
    result = handback.apply(mech.parsed, {"A1": 0.1, "A2": 0.1}, threshold=0.5, calibrated=False)
    assert result["per_criterion"] == {"A1": "PASS", "A2": "PASS"}
    assert result["verdict"] == "PASS"
    assert result["acted"] is False
    assert result["observed_nouls"] == {"A1": 0.1, "A2": 0.1}
