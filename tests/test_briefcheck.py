"""C-BL, check brief leakage (SRD typesafe-integration-v1.0.md section 2.5, TSI-BL-01, TSI-BL-02).

One test per behavior: parity with the hook's LEAK regex, no false positive on the current fixture check
briefs, an injected leak is a hit, and decide's three outcomes (LEAK on any hit, CLEAN below threshold
with no hits, UNVERIFIED when the Noul is None and there are no hits).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crews import briefcheck, catalog, planner  # noqa: E402

FIXTURES_DIR = ROOT / "tests" / "fixtures" / "roles"
CAT = catalog.load()






def _fixture_paths() -> list[Path]:
    return sorted(p for p in FIXTURES_DIR.glob("*.json") if ".bak-" not in p.name)


def _check_roles(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in spec["roles"] if r.get("kind") == "check"]


@pytest.mark.parametrize("fixture_path", _fixture_paths(), ids=lambda p: p.stem)
def test_no_false_positive_on_current_check_roles_authored_text(fixture_path: Path) -> None:
    """R2, CR-2: the floor scans authored_text, not the rendered brief, so it must have no hit on any
    current fixture's check role."""
    spec: dict[str, Any] = json.loads(fixture_path.read_text())
    checks = _check_roles(spec)
    for role in checks:
        hits = briefcheck.regex_hits(briefcheck.authored_text(role, spec["task"]))
        assert hits == [], f"{fixture_path.name}: false positive on {role['name']}'s authored text"


def test_rendered_check_brief_still_hits_on_the_fixed_verdict_line(tmp_path: Path) -> None:
    """Documents the observed CR-2 fact: scanning the whole rendered brief (not authored_text) still hits
    on planner.py's fixed output contract line 'VERDICT: PASS|FAIL|BLOCKED', which is why the floor must
    run on authored_text rather than on the rendered brief."""
    fixture_path = FIXTURES_DIR / "storefront_hub.json"
    spec: dict[str, Any] = json.loads(fixture_path.read_text())
    task_answers = {
        "need": {"choice": "everyday_implementation", "confidence": 0.9,
                 "probabilities": {"everyday_implementation": 0.9}},
        "divisible": {"noul": 0.1},
        "needs_verifier": {"noul": 1.0},
        "difficulty": {"score": 1.0},
    }
    plan, _files = planner.build(CAT, task_answers, spec, str(tmp_path / "storefront_hub"), allow=999)
    check_prompts = [c["agent_call"]["prompt"] for w in plan["waves"] if w["kind"] == "check" for c in w["calls"]]
    assert check_prompts
    hits = {h for prompt in check_prompts for h in briefcheck.regex_hits(prompt)}
    assert "VERDICT: PASS" in hits


def test_injected_leak_phrase_is_a_hit() -> None:
    brief = "<crew_task>...<criteria>C1: the fix works</criteria>The answer is correct.</crew_task>"
    hits = briefcheck.regex_hits(brief)
    assert hits == ["The answer is correct"]


def test_check_role_mission_with_leak_phrase_hits_through_authored_text() -> None:
    role = {
        "name": "checkout-fix-checker",
        "kind": "check",
        "mission": "I already verified that the fix is correct; confirm the checkout tests pass",
        "criteria": ["the checkout flow completes without error"],
    }
    hits = briefcheck.regex_hits(briefcheck.authored_text(role, "fix the checkout flow"))
    assert hits


def test_decide_leak_on_any_regex_hit_even_with_noul_zero() -> None:
    result = briefcheck.decide(["the answer is correct"], 0.0, 0.5)
    assert result["status"] == "LEAK"


def test_decide_clean_below_threshold_with_no_hits() -> None:
    result = briefcheck.decide([], 0.2, 0.5)
    assert result["status"] == "CLEAN"


def test_decide_unverified_when_noul_none_and_no_hits() -> None:
    result = briefcheck.decide([], None, 0.5)
    assert result["status"] == "UNVERIFIED"


def test_decide_leak_when_noul_at_or_above_threshold_with_no_hits() -> None:
    result = briefcheck.decide([], 0.5, 0.5)
    assert result["status"] == "LEAK"


def test_data_category_and_includes_worker_output_exposed() -> None:
    assert isinstance(briefcheck.DATA_CATEGORY, str) and briefcheck.DATA_CATEGORY
    assert briefcheck.INCLUDES_WORKER_OUTPUT is True


def test_request_shape() -> None:
    body = briefcheck.request("some check brief text")
    assert body["state"] == {"brief": "some check brief text"}
    assert set(body["questions"]) == {"leak"}
    assert body["questions"]["leak"]["type"] == "noul"


# --- A5/A6: the mechanical regex floor still refuses a leaking brief regardless of the TypeSafe surface's
# calibration state, because decide() consults hits before it ever looks at a semantic Noul (TSI-BL-02).
# crews/briefcheck.py's decide() now also carries a `calibrated` keyword (default True, matching
# crews/skills.decide and crews/handback.apply), but the floor still needs none of it for a regex hit: a
# hit is mechanical, never a TypeSafe threshold decision, so it stays LEAK under every `calibrated` value.

def test_regex_floor_refuses_regardless_of_what_a_typesafe_call_would_have_answered() -> None:
    brief = "<crew_task>...<criteria>C1: the fix works</criteria>The answer is correct.</crew_task>"
    hits = briefcheck.regex_hits(brief)
    assert hits
    # Every possible Noul, including one that would otherwise read as confidently CLEAN, still yields LEAK,
    # under both calibrated states.
    for noul in (0.0, 0.1, 0.49, None):
        for calibrated in (True, False):
            result = briefcheck.decide(hits, noul, threshold=0.5, calibrated=calibrated)
            assert result["status"] == "LEAK"
            assert result["reason"].startswith("regex floor hit")


def test_decide_uncalibrated_records_verdict_but_stays_unverified_and_does_not_act() -> None:
    """JEV-17: with no hit, an uncalibrated semantic Noul that would otherwise read LEAK must never set
    LEAK; it is recorded under `observed_decision` and the returned status stays UNVERIFIED, acted False."""
    result = briefcheck.decide([], 0.9, threshold=0.5, calibrated=False)
    assert result["status"] == "UNVERIFIED" and result["acted"] is False
    assert result["observed_decision"]["status"] == "LEAK"


def test_decide_calibrated_true_matches_unchanged_default_behavior() -> None:
    calibrated_result = briefcheck.decide([], 0.9, threshold=0.5, calibrated=True)
    default_result = briefcheck.decide([], 0.9, threshold=0.5)
    assert calibrated_result == default_result
    assert calibrated_result["status"] == "LEAK" and calibrated_result["acted"] is True
