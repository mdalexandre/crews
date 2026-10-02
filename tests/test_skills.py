"""Tests for crews/skills.py (C-SKL): TSI-SKL-01..04, TSI-VAL-01 (skill routing slice)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from crews import skills
from crews.judge import JudgeParseError

FIXTURES = Path(__file__).parent / "fixtures" / "skills"
FLAT_GLOB = str(FIXTURES / "flat" / "**" / "SKILL.md")
PLUGIN_GLOB = str(FIXTURES / "plugins" / "cache" / "**" / "SKILL.md")
ALL_GLOBS = [FLAT_GLOB, PLUGIN_GLOB]


# --- enumerate_skills ------------------------------------------------------------------------

def test_enumerate_order_and_ids() -> None:
    found = skills.enumerate_skills(ALL_GLOBS)
    ids = [s["id"] for s in found]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))
    # Every id is unique; the collision case produces two distinct ids for one shared name.
    assert {"alpha-widget-builder", "beta-report-generator", "delta-invoice-formatter",
            "shared-name-skill"}.issubset(set(ids))
    collided = [s for s in found if s["name"] == "shared-name-skill"]
    assert len(collided) == 2
    collided_ids = {s["id"] for s in collided}
    assert collided_ids == {"shared-name-skill", "shared-name-skill-other-tool"}
    # The path-first (lexicographically earlier) file keeps the plain id.
    plain = next(s for s in collided if s["id"] == "shared-name-skill")
    assert plain["path"].endswith("vendor-one/hashaaa/skills/shared-tool/SKILL.md")
    suffixed = next(s for s in collided if s["id"] == "shared-name-skill-other-tool")
    assert suffixed["path"].endswith("vendor-two/hashbbb/skills/other-tool/SKILL.md")


def test_enumerate_folded_description_is_read() -> None:
    found = {s["id"]: s for s in skills.enumerate_skills(ALL_GLOBS)}
    beta = found["beta-report-generator"]
    assert "financial reports" in beta["description"]
    assert "finance review" in beta["description"]


def test_enumerate_skips_incomplete_files() -> None:
    found = skills.enumerate_skills(ALL_GLOBS)
    assert "gamma-missing-description" not in {s["id"] for s in found}
    assert "gamma-missing-description" not in {s["name"] for s in found}


def test_enumerate_dedup_by_resolved_path() -> None:
    once = skills.enumerate_skills([FLAT_GLOB])
    twice = skills.enumerate_skills([FLAT_GLOB, FLAT_GLOB])
    assert [s["id"] for s in once] == [s["id"] for s in twice]


def test_enumerate_no_matches_is_empty() -> None:
    assert skills.enumerate_skills([str(FIXTURES / "nonexistent" / "**" / "SKILL.md")]) == []


def test_enumerate_real_default_globs_at_least_100_unique() -> None:
    """A4: read only, over the real machine's installed skills."""
    found = skills.enumerate_skills(skills.DEFAULT_GLOBS)
    ids = [s["id"] for s in found]
    assert len(ids) == len(set(ids))
    for s in found:
        assert s["name"]
        assert s["description"]
        assert Path(s["path"]).is_file()
    if len(ids) < 100:
        pytest.skip(f"only {len(ids)} skills are installed on this machine; the 100 skill scale check needs a "
                    "machine with at least 100")
    assert len(ids) >= 100


# --- prefilter ---------------------------------------------------------------------------------

INVOICE_ROLE: dict[str, Any] = {
    "name": "invoice-formatter-fixer",
    "mission": "Fix invoice formatting bugs and ensure totals render correctly for finance exports.",
    "scope": ["billing/invoice.py"],
    "execution": ["Adjust currency rounding logic"],
}


def test_prefilter_scores_and_orders_by_overlap() -> None:
    found = skills.enumerate_skills(ALL_GLOBS)
    top = skills.prefilter(INVOICE_ROLE, found, k=10)
    ids = [s["id"] for s in top]
    # alpha and the vendor-two duplicate share no four-letter-plus word with the role text.
    assert "alpha-widget-builder" not in ids
    assert "shared-name-skill-other-tool" not in ids
    # delta shares the most words (invoice, currency, rounding, render, totals), beta next.
    assert ids == ["delta-invoice-formatter", "beta-report-generator", "shared-name-skill"]


def test_prefilter_k_cap() -> None:
    found = skills.enumerate_skills(ALL_GLOBS)
    top = skills.prefilter(INVOICE_ROLE, found, k=2)
    assert len(top) == 2
    assert [s["id"] for s in top] == ["delta-invoice-formatter", "beta-report-generator"]


def test_prefilter_ties_break_by_id() -> None:
    # Names chosen so every skill contributes the same number of tokens at the same stem length
    # (none match a stripped suffix, none overlap the role's query terms): BM25's score depends only
    # on shared terms, document length, and corpus document frequency, all equal here, so the three
    # candidates score identically and the tie is broken by id alone.
    role = {"name": "widget-fixer", "mission": "fix widget", "scope": [], "execution": []}
    equal_skills = [
        {"id": "zeta", "name": "kappa", "description": "widget procedure", "path": "/tmp/zeta"},
        {"id": "alpha", "name": "gamma", "description": "widget procedure", "path": "/tmp/alpha"},
        {"id": "mid", "name": "delta", "description": "widget procedure", "path": "/tmp/mid"},
    ]
    out = skills.prefilter(role, equal_skills, k=10)
    assert [s["id"] for s in out] == ["alpha", "mid", "zeta"]


def test_prefilter_is_deterministic_across_calls() -> None:
    found = skills.enumerate_skills(ALL_GLOBS)
    first = [s["id"] for s in skills.prefilter(INVOICE_ROLE, found, k=10)]
    second = [s["id"] for s in skills.prefilter(INVOICE_ROLE, found, k=10)]
    assert first == second


def test_prefilter_bm25_weights_a_name_match_over_a_buried_description_mention() -> None:
    """A2 (the new ranking): a term matching the skill's *name* outweighs the same term appearing
    once, deep inside a longer, otherwise unrelated description. Plain word-overlap counting (the
    old scorer) cannot express this: both candidates share exactly one query word."""
    role = {"name": "role", "mission": "audit", "scope": [], "execution": []}
    cands = [
        {"id": "name-match", "name": "audit-tool",
         "description": "Miscellaneous helper with no other overlap here at all whatsoever period.",
         "path": "/tmp/a"},
        {"id": "desc-match", "name": "misc-tool",
         "description": "Provides an audit once as a single mention deep in unrelated helper text.",
         "path": "/tmp/b"},
    ]
    out = skills.prefilter(role, cands, k=10)
    assert [c["id"] for c in out] == ["name-match", "desc-match"]


def test_prefilter_quoted_trigger_phrase_bonus() -> None:
    """A2: a description that quotes a trigger phrase appearing verbatim in the role text scores
    above an otherwise identical description that only shares the same words unquoted."""
    role = {"name": "role", "mission": "Please run the quarterly compliance audit for this team.",
            "scope": [], "execution": []}
    cands = [
        {"id": "quoted", "name": "tool-a",
         "description": 'A helper skill. Fires on the phrase "quarterly compliance audit".',
         "path": "/tmp/a"},
        {"id": "unquoted", "name": "tool-b",
         "description": "A helper skill. Fires near quarterly compliance audit work sometimes.",
         "path": "/tmp/b"},
    ]
    out = skills.prefilter(role, cands, k=10)
    assert [c["id"] for c in out] == ["quoted", "unquoted"]


def test_prefilter_excludes_zero_score() -> None:
    role = {"name": "unrelated-role", "mission": "quantum harmonic oscillator wavefunction", "scope": [],
            "execution": []}
    found = skills.enumerate_skills(ALL_GLOBS)
    assert skills.prefilter(role, found, k=10) == []


# --- request / parse -----------------------------------------------------------------------

def _two_role_setup() -> tuple[list[dict[str, Any]], list[list[dict[str, str]]]]:
    roles: list[dict[str, Any]] = [
        {"name": "role-zero", "mission": "does the first thing"},
        {"name": "role-one", "mission": "does the second thing"},
    ]
    cands: list[list[dict[str, str]]] = [
        [{"id": "cand-a", "name": "cand-a", "description": "desc a", "path": "/tmp/a"},
         {"id": "cand-b", "name": "cand-b", "description": "desc b", "path": "/tmp/b"}],
        [],
    ]
    return roles, cands


def test_request_question_ids_and_options() -> None:
    roles, cands = _two_role_setup()
    body = skills.request("do the task", roles, cands)
    q = body["questions"]
    assert set(q) == {"skill.0", "fits.0.0", "fits.0.1", "procedure.0", "generalist.0",
                       "skill.1", "procedure.1", "generalist.1"}
    assert q["skill.0"]["type"] == "choice"
    # TSI-SKL-06: choice criteria is an object mapping every option id to a description string
    # (docs.typesafe.ai/primitives/choice.md); the API rejects a bare list with HTTP 422.
    assert q["skill.0"]["criteria"] == {"cand-a": "desc a", "cand-b": "desc b",
                                        skills.NONE_OPTION: "No listed skill genuinely fits this role's work."}
    assert q["skill.1"]["criteria"] == {skills.NONE_OPTION: "No listed skill genuinely fits this role's work."}
    assert q["fits.0.0"]["type"] == "noul"
    assert q["procedure.0"]["type"] == "noul"
    assert q["generalist.1"]["type"] == "noul"
    assert body["model"] == skills.MODEL
    assert body["state"]["task"] == "do the task"
    assert len(body["state"]["roles"]) == 2
    assert body["state"]["roles"][0]["candidates"][0]["id"] == "cand-a"


def test_request_criteria_is_object_with_every_candidate_id_and_none() -> None:
    """A1: criteria is an object containing every candidate id and none, each mapped to a
    non-empty string."""
    roles, cands = _two_role_setup()
    body = skills.request("do the task", roles, cands)
    criteria = body["questions"]["skill.0"]["criteria"]
    assert isinstance(criteria, dict)
    assert set(criteria) == {"cand-a", "cand-b", skills.NONE_OPTION}
    for value in criteria.values():
        assert isinstance(value, str)
        assert value


def test_request_role_candidate_length_mismatch_refused() -> None:
    roles, _ = _two_role_setup()
    with pytest.raises(ValueError):
        skills.request("t", roles, [[]])


def _valid_response() -> dict[str, Any]:
    return {"answers": {
        "skill.0": {"choice": "cand-a"},
        "fits.0.0": {"noul": 0.9},
        "fits.0.1": {"noul": 0.1},
        "procedure.0": {"noul": 0.8},
        "generalist.0": {"noul": 0.2},
        "skill.1": {"choice": skills.NONE_OPTION},
        "procedure.1": {"noul": 0.5},
        "generalist.1": {"noul": 0.5},
    }}


def test_parse_happy_path() -> None:
    roles, cands = _two_role_setup()
    parsed = skills.parse(_valid_response(), roles, cands)
    assert parsed[0]["choice"] == "cand-a"
    assert parsed[0]["fits"] == {"cand-a": 0.9, "cand-b": 0.1}
    assert parsed[0]["procedure"] == 0.8
    assert parsed[0]["generalist"] == 0.2
    assert parsed[1]["choice"] == skills.NONE_OPTION
    assert parsed[1]["fits"] == {}


def test_parse_refuses_unknown_option_key() -> None:
    roles, cands = _two_role_setup()
    bad = _valid_response()
    bad["answers"]["skill.0"] = {"choice": "not-a-real-candidate"}
    with pytest.raises(JudgeParseError, match="skill.0.choice"):
        skills.parse(bad, roles, cands)


def test_parse_refuses_missing_answers_key() -> None:
    roles, cands = _two_role_setup()
    with pytest.raises(JudgeParseError, match="answers"):
        skills.parse({}, roles, cands)


def test_parse_refuses_out_of_range_noul() -> None:
    roles, cands = _two_role_setup()
    bad = _valid_response()
    bad["answers"]["fits.0.0"] = {"noul": 1.5}
    with pytest.raises(JudgeParseError, match="fits.0.0"):
        skills.parse(bad, roles, cands)


def test_parse_refuses_missing_question() -> None:
    roles, cands = _two_role_setup()
    bad = _valid_response()
    del bad["answers"]["procedure.1"]
    with pytest.raises(JudgeParseError, match="procedure.1"):
        skills.parse(bad, roles, cands)


# --- decide ------------------------------------------------------------------------------------

THRESHOLDS = {"fit": 0.30, "gate": 0.30}


def test_decide_attaches_when_every_gate_passes() -> None:
    parsed = {"choice": "cand-a", "fits": {"cand-a": 0.9}, "procedure": 0.8, "generalist": 0.2}
    out = skills.decide(parsed, THRESHOLDS)
    assert out["attach"] == "cand-a"


def test_decide_blocks_on_none_choice_alone() -> None:
    parsed = {"choice": skills.NONE_OPTION, "fits": {}, "procedure": 0.9, "generalist": 0.1}
    out = skills.decide(parsed, THRESHOLDS)
    assert out["attach"] is None
    assert "none" in out["reason"]


def test_decide_blocks_on_low_fit_alone() -> None:
    parsed = {"choice": "cand-a", "fits": {"cand-a": 0.1}, "procedure": 0.9, "generalist": 0.1}
    out = skills.decide(parsed, THRESHOLDS)
    assert out["attach"] is None
    assert "fit" in out["reason"]


def test_decide_blocks_on_low_gate_mean_alone() -> None:
    # fit passes; mean(procedure, 1 - generalist) = mean(0.1, 0.1) = 0.1 < 0.30.
    parsed = {"choice": "cand-a", "fits": {"cand-a": 0.9}, "procedure": 0.1, "generalist": 0.9}
    out = skills.decide(parsed, THRESHOLDS)
    assert out["attach"] is None
    assert "gate" in out["reason"]


def test_decide_blocks_when_chosen_id_has_no_fit_recorded() -> None:
    parsed = {"choice": "cand-z", "fits": {"cand-a": 0.9}, "procedure": 0.9, "generalist": 0.1}
    out = skills.decide(parsed, THRESHOLDS)
    assert out["attach"] is None


# --- decide, calibrated=False (JEV-17): call and record, never act ----------------------------

def test_decide_calibrated_default_true_acted_true_when_it_would_attach() -> None:
    parsed = {"choice": "cand-a", "fits": {"cand-a": 0.9}, "procedure": 0.8, "generalist": 0.2}
    out = skills.decide(parsed, THRESHOLDS)
    assert out["attach"] == "cand-a"
    assert out["acted"] is True


def test_decide_uncalibrated_never_attaches_even_when_every_gate_would_pass() -> None:
    parsed = {"choice": "cand-a", "fits": {"cand-a": 0.9}, "procedure": 0.8, "generalist": 0.2}
    out = skills.decide(parsed, THRESHOLDS, calibrated=False)
    assert out["attach"] is None
    assert out["acted"] is False
    # The would-have-attached decision is still recorded for calibration, legibly, not silently dropped.
    assert out["observed_decision"]["attach"] == "cand-a"
    assert "uncalibrated" in out["reason"]


def test_decide_uncalibrated_records_would_have_blocked_decision_too() -> None:
    parsed = {"choice": "cand-a", "fits": {"cand-a": 0.1}, "procedure": 0.9, "generalist": 0.1}
    out = skills.decide(parsed, THRESHOLDS, calibrated=False)
    assert out["attach"] is None
    assert out["acted"] is False
    assert out["observed_decision"]["attach"] is None
    assert "fit" in out["observed_decision"]["reason"]


def test_decide_calibrated_true_is_unchanged_from_the_default() -> None:
    parsed = {"choice": "cand-a", "fits": {"cand-a": 0.1}, "procedure": 0.9, "generalist": 0.1}
    default_out = skills.decide(parsed, THRESHOLDS)
    explicit_out = skills.decide(parsed, THRESHOLDS, calibrated=True)
    assert default_out["attach"] == explicit_out["attach"] is None
    assert default_out["reason"] == explicit_out["reason"]
    assert explicit_out["acted"] is True


# --- attach ------------------------------------------------------------------------------------

def test_attach_adds_path_and_execution_line() -> None:
    found = skills.enumerate_skills(ALL_GLOBS)
    delta = next(s for s in found if s["id"] == "delta-invoice-formatter")
    role: dict[str, Any] = {"name": "invoice-formatter-fixer", "mission": "fix it", "execution": ["existing step"]}
    new_role = skills.attach(role, delta, found)
    assert new_role["inputs"] == [delta["path"]]
    assert new_role["execution"][0] == f"Read {delta['path']} before starting and apply its procedure to this work."
    assert new_role["execution"][1] == "existing step"
    # The original role dict is not mutated.
    assert "inputs" not in role
    assert role["execution"] == ["existing step"]


def test_attach_refuses_path_outside_index() -> None:
    found = skills.enumerate_skills(ALL_GLOBS)
    fake = {"id": "not-enumerated", "name": "not-enumerated", "description": "x", "path": "/etc/hostname"}
    role: dict[str, Any] = {"name": "some-role", "mission": "m"}
    with pytest.raises(skills.SkillRefused):
        skills.attach(role, fake, found)


def test_attach_refuses_path_that_is_not_a_file(tmp_path: Path) -> None:
    ghost = tmp_path / "ghost" / "SKILL.md"
    ghost.parent.mkdir()
    ghost.write_text("---\nname: ghost\ndescription: temp\n---\n", encoding="utf-8")
    index = skills.enumerate_skills([str(tmp_path / "**" / "SKILL.md")])
    entry = next(s for s in index if s["id"] == "ghost")
    ghost.unlink()
    role: dict[str, Any] = {"name": "some-role", "mission": "m"}
    with pytest.raises(skills.SkillRefused):
        skills.attach(role, entry, index)


# --- wide_request / wide_parse / merge_candidates -----------------------------------------------

def _big_index(n: int) -> list[dict[str, str]]:
    return [{"id": f"skill-{i:04d}", "name": f"skill-{i:04d}", "description": f"does thing {i}",
             "path": f"/tmp/skill-{i:04d}"} for i in range(n)]


def test_wide_request_chunks_never_exceed_255_options_and_cover_every_skill() -> None:
    index = _big_index(400)
    roles = [{"name": "role-zero", "mission": "m0"}, {"name": "role-one", "mission": "m1"}]
    body = skills.wide_request("do the task", roles, index)
    for i in range(len(roles)):
        wide_qids = sorted(k for k in body["questions"] if k.startswith(f"wide.{i}."))
        assert wide_qids == [f"wide.{i}.0", f"wide.{i}.1"]
        covered: set[str] = set()
        for qid in wide_qids:
            criteria = body["questions"][qid]["criteria"]
            assert skills.NONE_OPTION not in criteria  # the wide stage ranks, it does not decide
            assert len(criteria) <= 255
            covered.update(criteria)
        assert covered == {s["id"] for s in index}


def test_wide_request_small_index_is_one_chunk_per_role() -> None:
    index = _big_index(3)
    roles = [{"name": "role-zero", "mission": "m0"}]
    body = skills.wide_request("t", roles, index)
    assert set(k for k in body["questions"] if k.startswith("wide.")) == {"wide.0.0"}
    assert set(body["questions"]["wide.0.0"]["criteria"]) == {"skill-0000", "skill-0001", "skill-0002"}
    assert body["model"] == skills.MODEL
    assert body["state"]["task"] == "t"
    assert body["state"]["roles"] == [{"name": "role-zero", "mission": "m0"}]


def test_wide_request_desc_chars_truncates_criteria() -> None:
    index = [{"id": "s", "name": "s", "description": "x" * 500, "path": "/tmp/s"}]
    body = skills.wide_request("t", [{"name": "r", "mission": "m"}], index, desc_chars=10)
    assert len(body["questions"]["wide.0.0"]["criteria"]["s"]) == 10


def test_wide_parse_interleaves_chunk_rankings_by_probability() -> None:
    index = _big_index(4)
    roles = [{"name": "role-zero", "mission": "m0"}]
    body = skills.wide_request("t", roles, index)
    chunk_qids = sorted(k for k in body["questions"] if k.startswith("wide.0."))
    assert len(chunk_qids) == 1  # 4 skills, well under the 255-option cap: one chunk
    response = {"answers": {chunk_qids[0]: {
        "probabilities": {"skill-0002": 0.9, "skill-0000": 0.5, "skill-0003": 0.3, "skill-0001": 0.1}}}}
    ranked = skills.wide_parse(response, roles, index)
    assert ranked == [["skill-0002", "skill-0000", "skill-0003", "skill-0001"]]


def test_wide_parse_interleaves_across_multiple_chunks() -> None:
    index = _big_index(600)  # 3 chunks of 255/255/90
    roles = [{"name": "role-zero", "mission": "m0"}]
    body = skills.wide_request("t", roles, index)
    chunk_qids = sorted(k for k in body["questions"] if k.startswith("wide.0."))
    assert chunk_qids == ["wide.0.0", "wide.0.1", "wide.0.2"]
    response = {"answers": {
        "wide.0.0": {"probabilities": {"skill-0000": 0.9, "skill-0001": 0.1}},
        "wide.0.1": {"probabilities": {"skill-0255": 0.8, "skill-0256": 0.2}},
        "wide.0.2": {"probabilities": {"skill-0510": 0.7}},
    }}
    ranked = skills.wide_parse(response, roles, index)
    # round 0: each chunk's top pick, in chunk order; round 1: each chunk's next pick.
    assert ranked == [["skill-0000", "skill-0255", "skill-0510", "skill-0001", "skill-0256"]]


def test_wide_parse_refuses_missing_answer() -> None:
    index = _big_index(2)
    roles = [{"name": "r", "mission": "m"}]
    with pytest.raises(JudgeParseError, match="wide.0.0"):
        skills.wide_parse({"answers": {}}, roles, index)


def test_wide_parse_refuses_missing_probabilities() -> None:
    index = _big_index(2)
    roles = [{"name": "r", "mission": "m"}]
    with pytest.raises(JudgeParseError, match="probabilities"):
        skills.wide_parse({"answers": {"wide.0.0": {}}}, roles, index)


def test_wide_parse_refuses_out_of_range_probability() -> None:
    index = _big_index(2)
    roles = [{"name": "r", "mission": "m"}]
    bad = {"answers": {"wide.0.0": {"probabilities": {"skill-0000": 1.5}}}}
    with pytest.raises(JudgeParseError):
        skills.wide_parse(bad, roles, index)


def test_merge_candidates_appends_new_wide_picks_up_to_k() -> None:
    index = _big_index(10)
    bm25 = [index[0], index[1]]
    wide_ids = ["skill-0001", "skill-0005", "skill-0006", "skill-0007"]
    merged = skills.merge_candidates(bm25, wide_ids, index, wide_k=2)
    assert [s["id"] for s in merged] == ["skill-0000", "skill-0001", "skill-0005", "skill-0006"]


def test_merge_candidates_dedupes_and_skips_unknown_ids() -> None:
    index = _big_index(3)
    bm25 = [index[0]]
    merged = skills.merge_candidates(bm25, ["skill-0000", "skill-not-real", "skill-0001"], index, wide_k=5)
    assert [s["id"] for s in merged] == ["skill-0000", "skill-0001"]


def test_merge_candidates_no_wide_picks_returns_bm25_unchanged() -> None:
    index = _big_index(3)
    bm25 = [index[0], index[1]]
    merged = skills.merge_candidates(bm25, [], index, wide_k=5)
    assert merged == bm25


# --- routable ----------------------------------------------------------------------------------

def test_routable_drops_every_id_sharing_an_excluded_name_and_keeps_order() -> None:
    found = skills.enumerate_skills(ALL_GLOBS)
    ids_before = [s["id"] for s in found]
    assert {"shared-name-skill", "shared-name-skill-other-tool"}.issubset(set(ids_before))
    kept = skills.routable(found, ["shared-name-skill"])
    kept_ids = [s["id"] for s in kept]
    assert "shared-name-skill" not in kept_ids
    assert "shared-name-skill-other-tool" not in kept_ids
    # Order preserved: the surviving ids are exactly ids_before with the two excluded ids removed,
    # in the same relative order.
    assert kept_ids == [i for i in ids_before if i not in ("shared-name-skill", "shared-name-skill-other-tool")]


def test_routable_empty_exclude_list_returns_index_unchanged() -> None:
    found = skills.enumerate_skills(ALL_GLOBS)
    assert skills.routable(found, []) == found


def test_routable_excluded_name_not_present_is_a_no_op() -> None:
    found = skills.enumerate_skills(ALL_GLOBS)
    assert skills.routable(found, ["no-such-skill-name"]) == found
