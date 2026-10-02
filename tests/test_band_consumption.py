"""Band consumption: the live routing path prefers a band installed in the catalog under
typesafe.need_band over the calibration directory file, and treats a band that records no
criteria version as unverifiable rather than as permission to admit (crews/need_gate.py's own
principle, no admission from absence; round three protocol.md section 4).

`uv run pytest tests/test_band_consumption.py`

Covers:
  A1 with a band installed in the catalog, crews.cli._resolve_need_band (the function the live
     routing path calls) uses THAT band: a probe where the installed band and a differing
     calibration directory file would decide differently shows the installed one deciding.
  A2 the drift check applies to the installed band exactly as to a file band: a criterion changed
     after install yields "uncalibrated" with a drift reason, never "admit".
  A3 when the criteria hash in force is supplied and the band records no criteria version, the
     decision is "uncalibrated" with a reason distinguishable from drift, from no band, and from
     insufficient.
  A4 when no criteria hash is supplied, behaviour is unchanged: a legacy band with no provenance
     still admits, exactly as before this change.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from crews import bands, catalog, cli
from crews.cli import _live_task_answers, _need_criteria_hash, _resolve_need_band
from crews.need_gate import gate_need

NEED_CRITERIA_V1 = {
    "repeatable_task": "Clear, narrowly scoped, repeatable work with an obvious method",
    "bounded_retrieval": "Finding, reading, or scanning existing material; low ambiguity",
}

NEED_CRITERIA_V2 = {
    "repeatable_task": "Clear, narrowly scoped, repeatable work with an obvious method",
    "bounded_retrieval": "Finding, reading, or scanning existing material; low ambiguity",
    "specialist": "The task says a strong attempt already failed, so the hardest tier is warranted",
}


def _minimal_catalog(criteria: dict[str, str], version: str = "9.9.9",
                     need_band: dict[str, Any] | None = None) -> dict[str, Any]:
    cat: dict[str, Any] = {"version": version, "questions": {"need": {"type": "choice", "criteria": criteria}}}
    if need_band is not None:
        cat["typesafe"] = {"need_band": need_band}
    return cat


def _measured_band(threshold: float, n: int = 100, k: int = 90) -> dict[str, Any]:
    return {"status": "MEASURED", "threshold": threshold, "n": n, "k": k, "precision": k / n, "wilson_lower": 0.80}


# Both band fixtures name the model they were measured against, and any stubbed response routed
# through the live path names the same one. A band that records no model is non acting under JEV-32,
# so a fixture omitting it cannot prove anything about thresholds: the decision would be uncalibrated
# whatever the threshold said. Tests here that call gate_need directly pass no model keyword and are
# unaffected either way.
_FIXTURE_MODEL = "jev-1.13.0"


def _installed_band(criteria_hash: str, act_threshold: float) -> dict[str, Any]:
    return {
        "act": _measured_band(act_threshold),
        "confirm": _measured_band(act_threshold - 0.1),
        "provenance": {"criteria_hash": criteria_hash, "catalog_version": "9.9.9", "n_observations": 100,
                       "observations_path": "/tmp/does-not-matter/observations.jsonl",
                       "measured_at": "2026-09-22T19:00:00+00:00", "model": _FIXTURE_MODEL},
        "threshold_source": "crew calibrate-bands, test fixture",
        "calibrated": True,
    }


def _write_file_band(path: Path, criteria_hash: str, act_threshold: float) -> None:
    payload = {
        "act": _measured_band(act_threshold),
        "confirm": _measured_band(act_threshold - 0.1),
        "provenance": {"criteria_hash": criteria_hash, "catalog_version": "9.9.9", "n_observations": 50,
                       "observations_path": "/tmp/does-not-matter/observations.jsonl",
                       "measured_at": "2026-09-22T18:00:00+00:00", "model": _FIXTURE_MODEL},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# --- A1: the installed band decides, proven against a differing calibration directory file -----

def test_installed_band_wins_over_calibration_dir_file_and_the_decision_differs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREWS_CALIBRATION_DIR", str(tmp_path / "calibration"))
    cat_no_band = _minimal_catalog(NEED_CRITERIA_V1, version="1.0.0")
    criteria_hash = _need_criteria_hash(cat_no_band)

    # the calibration directory file alone would admit at confidence 0.60 (its act threshold 0.50)
    _write_file_band(bands.calibration_dir() / "bands.json", criteria_hash, act_threshold=0.50)
    file_only_status = _resolve_need_band(cat_no_band)
    file_only_decision = gate_need({"confidence": 0.60}, file_only_status, criteria_hash=criteria_hash)
    assert file_only_decision["decision"] == "admit"

    # the same catalog with a band installed at a stricter act threshold (0.90) decides differently
    # for the same confidence: the probe that shows precedence.
    cat_with_band = _minimal_catalog(NEED_CRITERIA_V1, version="1.0.0",
                                     need_band=_installed_band(criteria_hash, act_threshold=0.90))
    installed_status = _resolve_need_band(cat_with_band)
    assert installed_status is cat_with_band["typesafe"]["need_band"]
    installed_decision = gate_need({"confidence": 0.60}, installed_status, criteria_hash=criteria_hash)

    assert installed_decision["decision"] != file_only_decision["decision"]
    assert installed_decision["decision"] in ("confirm", "reject")
    assert installed_decision["threshold_act"] == 0.90


def test_installed_band_decides_through_the_live_routing_function_itself(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Precedence proven through _live_task_answers, the function routing actually calls.

    Added 2026-09-22 (ledger C-17) after a blind check observed that the precedence test above
    exercises _resolve_need_band, a helper, so it could pass while the live path read some other
    band. The transport is stubbed: no network, no approval ledger, no key.

    The stub response names its model because _live_task_answers always passes the answering model to
    the gate, and under JEV-32 a decision where neither side names a model is uncalibrated. Until
    2026-09-22 this fixture named none on either side, so both assertions below passed through the
    absence hole rather than through the thresholds they claim to test."""
    monkeypatch.setenv("CREWS_CALIBRATION_DIR", str(tmp_path / "calibration"))
    cat = _minimal_catalog(NEED_CRITERIA_V1, version="1.0.0")
    criteria_hash = _need_criteria_hash(cat)
    _write_file_band(bands.calibration_dir() / "bands.json", criteria_hash, act_threshold=0.50)

    monkeypatch.setattr(cli.egress, "gated_call",
                        lambda *a, **k: {"state": "answered",
                                         "response": {"stub": True, "model": _FIXTURE_MODEL}})
    monkeypatch.setattr(cli.judge, "parse1",
                        lambda *a, **k: {"need": {"choice": "everyday_implementation", "confidence": 0.60},
                                         "divisible": {"noul": 0.2}, "needs_verifier": {"noul": 0.5},
                                         "difficulty": {"score": 2.0}})

    answers_file_band, _ = _live_task_answers(cat, "some task text")
    assert answers_file_band is not None, "act threshold 0.50 admits confidence 0.60"

    cat_installed = _minimal_catalog(NEED_CRITERIA_V1, version="1.0.0",
                                     need_band=_installed_band(criteria_hash, act_threshold=0.90))
    answers_installed, info = _live_task_answers(cat_installed, "some task text")
    assert answers_installed is None, "the installed band at 0.90 must reject confidence 0.60"
    assert info is not None
    # JEV-34 (crew task blank-identity-normalization-closer): `answers_installed is None` is also
    # what an uncalibrated refusal looks like (a band model mismatch, a missing criteria version,
    # ...), so on its own this assertion cannot tell a genuine threshold rejection from a refusal
    # this test never intended to exercise; it would keep passing even if the model guard above
    # started rejecting this fixture instead of the threshold. Prove the reason: the decision must
    # be "reject" (never "uncalibrated"), and the reason text must actually cite the thresholds
    # confidence 0.60 fell below, not an unverifiable-band reason.
    ng = info["need_gate"]
    assert ng["decision"] == "reject", ng
    assert "below act threshold" in ng["reason"] and "confirm threshold" in ng["reason"], ng
    assert "uncalibrated" not in ng["reason"] and "model" not in ng["reason"] and "drift" not in ng["reason"], ng


def test_whitespace_only_model_identity_does_not_admit_at_the_production_call_site(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Crew task blank-identity-normalization-closer, JEV-33: reproduces DEFECT ONE at the real
    production call site, crews.cli.core_budget(judge_mode="live"), before this crew task's fix.
    A loopback stub reports an answering model of "   " (whitespace only); the installed catalog
    band's own provenance.model is also "   ". Before this fix, `crews/need_gate.py`'s caller side
    check (`isinstance(model, str) and model != ""`) read "   " as a real identity, so two blanks
    naming nobody compared equal and the call returned decision="admit" at confidence 0.995
    against an act threshold of 0.94. It must now refuse: normalize_identity reads both sides as
    naming no identity, so the decision is uncalibrated, and the live answer must never be used."""
    monkeypatch.setenv("CREWS_CALIBRATION_DIR", str(tmp_path / "calibration"))
    # The real shipped catalog and its real installed, provenanced act band (threshold 0.94), so
    # this reproduces the production shape exactly rather than a synthetic one; only the band's
    # own provenance.model is overridden to whitespace only, the shape the blind auditor found.
    cat = copy.deepcopy(catalog.load())
    cat["typesafe"]["need_band"]["provenance"]["model"] = "   "
    monkeypatch.setattr(cli.egress, "gated_call",
                        lambda *a, **k: {"state": "answered", "response": {"stub": True, "model": "   "}})
    monkeypatch.setattr(cli.judge, "parse1",
                        lambda *a, **k: {"need": {"choice": "everyday_implementation", "confidence": 0.995},
                                         "divisible": {"noul": 0.2}, "needs_verifier": {"noul": 0.5},
                                         "difficulty": {"score": 2.0}})

    result = cli.core_budget(cat, answers=None, outage=False, judge_mode="live",
                             task="some task text", run_dir=None)
    ng = result["judge"]["need_gate"]
    assert ng["decision"] == "uncalibrated", ng
    assert ng["threshold_act"] is None and ng["threshold_confirm"] is None, ng
    assert "confirm_need" not in result
    assert any("need_gate uncalibrated" in n for n in result["notes"])


def test_resolve_need_band_falls_back_to_calibration_dir_file_with_no_installed_band(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREWS_CALIBRATION_DIR", str(tmp_path / "calibration"))
    cat = _minimal_catalog(NEED_CRITERIA_V1, version="1.0.0")
    criteria_hash = _need_criteria_hash(cat)
    _write_file_band(bands.calibration_dir() / "bands.json", criteria_hash, act_threshold=0.50)

    status = _resolve_need_band(cat)
    assert status["act"]["threshold"] == 0.50
    assert status["provenance"]["criteria_hash"] == criteria_hash


def test_resolve_need_band_with_neither_source_is_uncalibrated(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREWS_CALIBRATION_DIR", str(tmp_path / "calibration"))
    cat = _minimal_catalog(NEED_CRITERIA_V1, version="1.0.0")
    status = _resolve_need_band(cat)
    assert status == {"status": "UNCALIBRATED"}


# --- A2: the drift check applies to the installed band exactly as to a file band ----------------

def test_installed_band_drift_after_criteria_changes_is_uncalibrated_never_admit(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREWS_CALIBRATION_DIR", str(tmp_path / "calibration"))
    cat_v1 = _minimal_catalog(NEED_CRITERIA_V1, version="1.0.0")
    v1_hash = _need_criteria_hash(cat_v1)
    installed = _installed_band(v1_hash, act_threshold=0.50)

    # the catalog's need criteria moved on to v2 (a role edited it) but still carries the band
    # installed against v1: the same shape a file band carries, and the same check must fire.
    cat_v2_with_stale_band = _minimal_catalog(NEED_CRITERIA_V2, version="1.0.0", need_band=installed)
    v2_hash = _need_criteria_hash(cat_v2_with_stale_band)
    assert v2_hash != v1_hash

    status = _resolve_need_band(cat_v2_with_stale_band)
    decision = gate_need({"confidence": 0.99}, status, criteria_hash=v2_hash)

    assert decision["decision"] == "uncalibrated"
    assert "drift" in decision["reason"]
    assert decision["threshold_act"] is None
    assert decision["threshold_confirm"] is None


def test_file_band_drift_still_applies_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREWS_CALIBRATION_DIR", str(tmp_path / "calibration"))
    cat_v1 = _minimal_catalog(NEED_CRITERIA_V1, version="1.0.0")
    v1_hash = _need_criteria_hash(cat_v1)
    _write_file_band(bands.calibration_dir() / "bands.json", v1_hash, act_threshold=0.50)

    cat_v2 = _minimal_catalog(NEED_CRITERIA_V2, version="1.0.0")
    v2_hash = _need_criteria_hash(cat_v2)
    status = _resolve_need_band(cat_v2)
    decision = gate_need({"confidence": 0.99}, status, criteria_hash=v2_hash)

    assert decision["decision"] == "uncalibrated"
    assert "drift" in decision["reason"]


# --- A3: a band recording no criteria version is uncalibrated with a distinguishable reason -----

def test_band_with_no_criteria_version_is_uncalibrated_when_hash_known() -> None:
    no_version_band = {"act": _measured_band(0.85), "confirm": _measured_band(0.70)}
    result = gate_need({"confidence": 0.95}, no_version_band, criteria_hash="deadbeef")
    assert result["decision"] == "uncalibrated"
    assert result["threshold_act"] is None
    assert result["threshold_confirm"] is None
    assert "drift" not in result["reason"]
    assert "no band installed" not in result["reason"]
    assert "INSUFFICIENT" not in result["reason"]


def test_no_criteria_version_reason_distinguishable_from_drift_no_band_and_insufficient() -> None:
    no_version = gate_need({"confidence": 0.95}, {"act": _measured_band(0.85), "confirm": _measured_band(0.70)},
                           criteria_hash="deadbeef")
    drifted = gate_need({"confidence": 0.95}, {"act": _measured_band(0.85), "confirm": _measured_band(0.70),
                                               "provenance": {"criteria_hash": "aaaa"}},
                        criteria_hash="bbbb")
    no_band = gate_need({"confidence": 0.95}, {"status": "UNCALIBRATED"}, criteria_hash="deadbeef")
    # the act band itself is not MEASURED (INSUFFICIENT): it never had a usable threshold, so
    # the no-criteria-version check never masks its own distinguishable reason.
    insufficient = gate_need({"confidence": 0.95}, {"act": {"status": "INSUFFICIENT", "n_total": 3},
                                                     "confirm": None}, criteria_hash="deadbeef")
    reasons = {no_version["reason"], drifted["reason"], no_band["reason"], insufficient["reason"]}
    assert len(reasons) == 4
    assert {no_version["decision"], drifted["decision"], no_band["decision"],
           insufficient["decision"]} == {"uncalibrated"}
    assert "INSUFFICIENT" in insufficient["reason"]
    assert "no band installed" in no_band["reason"]
    assert "drift" in drifted["reason"]
    assert "no provenance criteria hash" in no_version["reason"]


def test_installed_band_with_no_criteria_version_is_uncalibrated(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same rule reached through crews.cli._resolve_need_band: a catalog carrying an
    installed band with no provenance criteria hash (a hand edited or pre-provenance install)
    never admits once the caller knows the criteria in force."""
    monkeypatch.setenv("CREWS_CALIBRATION_DIR", str(tmp_path / "calibration"))
    no_version_band = {"act": _measured_band(0.85), "confirm": _measured_band(0.70), "calibrated": True}
    cat = _minimal_catalog(NEED_CRITERIA_V1, version="1.0.0", need_band=no_version_band)
    criteria_hash = _need_criteria_hash(cat)

    status = _resolve_need_band(cat)
    decision = gate_need({"confidence": 0.95}, status, criteria_hash=criteria_hash)
    assert decision["decision"] == "uncalibrated"
    assert "no band installed" not in decision["reason"]


# --- A4: with no criteria hash supplied, behaviour is unchanged ---------------------------------

def test_no_criteria_hash_supplied_legacy_band_still_admits_unchanged() -> None:
    legacy_bands = {"act": _measured_band(0.85), "confirm": _measured_band(0.70)}
    result = gate_need({"confidence": 0.95}, legacy_bands)
    assert result["decision"] == "admit"


def test_no_criteria_hash_supplied_legacy_fixtures_decide_exactly_as_before() -> None:
    """The legacy call shape, checked by behaviour rather than by existence.

    Strengthened 2026-09-22 (ledger C-17) after a blind check observed that this test asserted
    only `hasattr(legacy_suite, "MEASURED_BANDS")`, which proves nothing about how gate_need
    behaves and reads as coverage it does not provide. It now drives the pre-change fixtures
    through every decision branch with no criteria_hash argument.

    D-1 (mission gate defect repair, 2026-09-22): a static `import tests.test_need_gate` here made
    mypy see tests/test_need_gate.py under two module names at once ("test_need_gate" from pytest's
    own rootdir-relative collection, since no tests/__init__.py exists, and "tests.test_need_gate"
    from this dotted import), which mypy refuses outright and which then aborts checking of the
    entire repository, not just this file. Adding tests/__init__.py was rejected: it would change
    import semantics for every test module in the suite (test id, sys.path insertion, and any
    other file's bare `import test_foo`) to fix one file's cross-module reference, a much wider
    blast radius than this one call site. Loading the file at runtime through importlib instead
    means mypy never sees a dotted import statement for it, so it never double-names the module;
    the runtime behaviour (reading MEASURED_BANDS et al. from the pre-change fixture file) is
    unchanged."""
    import importlib.util

    need_gate_path = Path(__file__).resolve().parent / "test_need_gate.py"
    spec = importlib.util.spec_from_file_location("_legacy_test_need_gate", need_gate_path)
    assert spec is not None and spec.loader is not None
    legacy_suite = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy_suite)

    measured = legacy_suite.MEASURED_BANDS
    assert gate_need({"confidence": 0.95}, measured)["decision"] == "admit"
    assert gate_need({"confidence": 0.85}, measured)["decision"] == "admit"
    assert gate_need({"confidence": 0.75}, measured)["decision"] == "confirm"
    assert gate_need({"confidence": 0.10}, measured)["decision"] == "reject"
    assert gate_need(0.95, measured)["decision"] == "admit"
    assert gate_need({"confidence": 0.95}, legacy_suite.UNCALIBRATED_BANDS)["decision"] == "uncalibrated"
    assert gate_need({"confidence": 0.95}, legacy_suite.ACT_INSUFFICIENT_BANDS)["decision"] == "uncalibrated"


# --- sanity: crews.cli import surface used above stays importable -------------------------------

def test_cli_module_still_imports() -> None:
    assert callable(cli.main)
