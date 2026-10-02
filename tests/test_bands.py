"""crews/bands.py: calibration data and measured bands (SRD typesafe-integration-v1.0 section
2.5, C-BND; TSI-CAL-01, TSI-CAL-03, TSI-CAL-04, TSI-CAL-05, TSI-CAL-06).

`uv run pytest tests/test_bands.py`
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crews.bands import bands_status, derive_bands, derive_threshold, record_observation, review_set, wilson_lower

ROOT = Path(__file__).resolve().parent.parent
POINTS_FIXTURE = ROOT / "tests" / "fixtures" / "bands" / "points.json"
WITNESS_EXPORT = ROOT / "witness" / "bands_witness_export.json"


def _load_points() -> list[tuple[float, bool]]:
    data = json.loads(POINTS_FIXTURE.read_text(encoding="utf-8"))
    return [(p["confidence"], bool(p["correct"])) for p in data["points"]]


# -- A2: fixture shape -------------------------------------------------------------------------


def test_fixture_has_at_least_60_points_with_repeated_confidence() -> None:
    points = _load_points()
    assert len(points) >= 60
    confidences = [c for c, _ in points]
    assert len(set(confidences)) < len(confidences), "fixture must repeat at least one confidence value"


# -- wilson_lower -------------------------------------------------------------------------------


def test_wilson_lower_zero_n_is_zero() -> None:
    assert wilson_lower(0, 0) == 0.0


def test_wilson_lower_monotonic_in_k() -> None:
    low = wilson_lower(5, 20)
    high = wilson_lower(15, 20)
    assert low < high


def test_wilson_lower_known_value() -> None:
    # 10 successes out of 10 trials, two sided 95%: standard reference value ~0.7224672.
    assert wilson_lower(10, 10) == pytest.approx(0.7224672001371106, abs=1e-12)


# -- derive_threshold ----------------------------------------------------------------------------


def test_derive_threshold_measured_at_lowest_qualifying_confidence() -> None:
    points = _load_points()
    result = derive_threshold(points, target=0.70, min_n=10)
    assert result["status"] == "MEASURED"
    assert result["wilson_lower"] >= 0.70
    assert result["n"] >= 10
    assert result["precision"] == pytest.approx(result["k"] / result["n"])


def test_derive_threshold_insufficient_when_target_unreachable() -> None:
    points = _load_points()
    result = derive_threshold(points, target=0.85, min_n=10)
    assert result["status"] == "INSUFFICIENT"
    assert result["n_total"] == len(points)
    assert "reason" in result


def test_derive_threshold_insufficient_on_too_little_data() -> None:
    tiny = [(0.9, True), (0.9, True), (0.9, False)]
    result = derive_threshold(tiny, target=0.5, min_n=10)
    assert result["status"] == "INSUFFICIENT"
    assert result["n_total"] == 3


def test_derive_threshold_selects_lowest_qualifying_threshold() -> None:
    # Every point qualifies at the lowest confidence already; a higher threshold would also
    # satisfy target and min_n, but the lowest (most inclusive) one must be selected.
    points = [(0.5, True)] * 20 + [(0.9, True)] * 20
    result = derive_threshold(points, target=0.9, min_n=10)
    assert result["status"] == "MEASURED"
    assert result["threshold"] == 0.5


# -- derive_bands ----------------------------------------------------------------------------


def test_derive_bands_returns_act_and_confirm() -> None:
    points = _load_points()
    config = {"bands": {"act": {"target": 0.85}, "confirm": {"target": 0.70}}, "min_n": 10}
    bands = derive_bands(points, config)
    assert set(bands) == {"act", "confirm"}
    assert bands["act"]["status"] == "INSUFFICIENT"
    assert bands["confirm"]["status"] == "MEASURED"


@pytest.mark.parametrize(
    "config",
    [
        {"min_n": 10},
        {"bands": {"act": {"target": 0.85}, "confirm": {"target": 0.70}}},
        {"bands": {"confirm": {"target": 0.70}}, "min_n": 10},
        {"bands": {"act": {"target": 0.85}}, "min_n": 10},
        {"bands": {"act": {}, "confirm": {"target": 0.70}}, "min_n": 10},
    ],
)
def test_derive_bands_raises_valueerror_on_missing_config_key(config: dict) -> None:
    with pytest.raises(ValueError):
        derive_bands([(0.9, True)] * 20, config)


# -- bands_status ----------------------------------------------------------------------------


def test_bands_status_uncalibrated_when_file_absent(tmp_path: Path) -> None:
    result = bands_status(tmp_path / "does-not-exist.json")
    assert result == {"status": "UNCALIBRATED"}


def test_bands_status_uncalibrated_on_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "bands.json"
    p.write_text("not valid json {{{", encoding="utf-8")
    assert bands_status(p) == {"status": "UNCALIBRATED"}


def test_bands_status_uncalibrated_when_not_an_object(tmp_path: Path) -> None:
    p = tmp_path / "bands.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    assert bands_status(p) == {"status": "UNCALIBRATED"}


def test_bands_status_returns_parsed_file(tmp_path: Path) -> None:
    p = tmp_path / "bands.json"
    payload = {"act": {"status": "MEASURED", "threshold": 0.8}, "confirm": {"status": "INSUFFICIENT"}}
    p.write_text(json.dumps(payload), encoding="utf-8")
    assert bands_status(p) == payload


# -- record_observation ----------------------------------------------------------------------------


def _obs(case_id: str, fingerprint: str) -> dict:
    return {
        "case_id": case_id,
        "kind": "task",
        "call_id": f"ts-{fingerprint[:16]}-1",
        "request_fingerprint": fingerprint,
        "approval_ref": "approved",
        "answering_model": "jev-latest",
        "typed_result": {"choice": "everyday_implementation", "confidence": 0.8},
        "transport_outcome": "answered",
        "validation_outcome": "valid",
        "observed_at": "2026-09-21T00:00:00Z",
    }


def test_record_observation_appends_new_record(tmp_path: Path) -> None:
    p = tmp_path / "observations.jsonl"
    appended = record_observation(p, _obs("case-1", "fp-1"))
    assert appended is True
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["case_id"] == "case-1"


def test_record_observation_dedupes_by_case_id_and_fingerprint(tmp_path: Path) -> None:
    p = tmp_path / "observations.jsonl"
    record_observation(p, _obs("case-1", "fp-1"))
    appended_again = record_observation(p, _obs("case-1", "fp-1"))
    assert appended_again is False
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_record_observation_same_case_different_fingerprint_is_new_record(tmp_path: Path) -> None:
    p = tmp_path / "observations.jsonl"
    record_observation(p, _obs("case-1", "fp-1"))
    appended = record_observation(p, _obs("case-1", "fp-2"))
    assert appended is True
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


# -- review_set ----------------------------------------------------------------------------


_ITEMS = [
    {"id": "t1", "proposed_need": "everyday_implementation"},
    {"id": "t2", "proposed_need": "difficult_review"},
    {"id": "t3", "proposed_need": "everyday_implementation"},
    {"id": "t4", "proposed_need": "bounded_retrieval"},
    {"id": "t5", "proposed_need": "everyday_implementation"},
]


def test_review_set_reproducible_for_same_inputs() -> None:
    observed = {
        "t1": "everyday_implementation",
        "t2": "everyday_implementation",  # disagreement
        "t3": "everyday_implementation",
        "t4": "bounded_retrieval",
        # t5 has no observation -> missing
    }
    first = review_set(_ITEMS, observed, target_field="proposed_need", sample_size=2, seed="s1")
    second = review_set(_ITEMS, observed, target_field="proposed_need", sample_size=2, seed="s1")
    assert first == second
    assert first["disagreements"] == ["t2"]
    assert set(first["agreement_sample"]).issubset({"t1", "t3", "t4"})
    assert first["agreements_total"] == 3
    assert first["missing"] == ["t5"]


def test_review_set_different_seed_can_change_sample_order() -> None:
    observed = {"t1": "everyday_implementation", "t3": "everyday_implementation", "t4": "bounded_retrieval"}
    a = review_set(_ITEMS, observed, target_field="proposed_need", sample_size=3, seed="seed-a")
    b = review_set(_ITEMS, observed, target_field="proposed_need", sample_size=3, seed="seed-b")
    # Same membership either way (all three agree), ordering is the part seed controls.
    assert set(a["agreement_sample"]) == set(b["agreement_sample"]) == {"t1", "t3", "t4"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sample_size": 2, "seed": "s1"},
        {"target_field": "proposed_need", "seed": "s1"},
        {"target_field": "proposed_need", "sample_size": 2},
    ],
)
def test_review_set_refuses_missing_keyword_argument(kwargs: dict) -> None:
    with pytest.raises(TypeError):
        review_set(_ITEMS, {}, **kwargs)


# -- TSI-CAL-06: witness agreement --------------------------------------------------------------


