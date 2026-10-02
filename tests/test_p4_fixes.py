"""P4 correctness fixes: OSS-FIX-1 (plan index and seat guard), OSS-FIX-2 (aggregated schema errors),
OSS-FIX-3 (compact crew_plan result), OSS-FIX-4 (judge degradation). Synthetic keys, no network."""

from __future__ import annotations

import copy
import json
import os
import site
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from test_server import ROOT, answers, calls, roles  # noqa: E402

from crews import catalog, cli, guard, paths, planindex, planner  # noqa: E402

HOOK = ROOT / "hooks" / "seat_guard.py"
SYNTHETIC_KEY = "synthetic-test-key-not-real"


def four_role_spec(pad: int = 0) -> dict[str, Any]:
    spec = roles("security_review")
    assert len(spec["roles"]) == 4
    if pad:
        filler = "reference line about the refund edge function policies and service keys. " * 9
        for role in spec["roles"]:
            role["inputs"] = [f"{filler}{i}" for i in range(pad)]
    return spec


def plan_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {"HOME": str(home), "PYTHONUSERBASE": site.getuserbase(), "CREWS_HOME": str(tmp_path / "state"),
            "CREWS_EGRESS_DIR": str(tmp_path / "egress"), "CREWS_AUDIT_DIR": str(tmp_path / "audit"),
            "CREWS_CALIBRATION_DIR": str(tmp_path / "calibration"), **extra}


def agent_calls(output: dict[str, Any]) -> list[dict[str, Any]]:
    return [c["agent_call"] for w in output["waves"] for c in w["calls"]]


# --- OSS-FIX-1: plan index and seat guard ----------------------------------------------------------------------

def test_g36_large_plan_is_admitted_from_the_index_alone(tmp_path: Path) -> None:
    env = plan_env(tmp_path)
    result = calls([("crew_plan", {"roles": four_role_spec(pad=45), "answers": answers("security_review"),
                                   "verbose": True, "run_dir": str(tmp_path / "run"), "allow": 8})], env=env)[0]
    assert result["status"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS"), result["errors"]
    assert len(json.dumps(result)) > 100_000
    sent = agent_calls(result["output"])
    assert len(sent) == 5
    now = datetime.now(timezone.utc)
    for ac in sent:
        d = guard.decide("Agent", ac, tmp_path / "state", now, transcript_text=None)
        assert d.allow and d.warning is None, (ac["subagent_type"], d.reason)


def planned(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """Plan through the CLI core (declared answers, judge off) and return (crews home, one agent call)."""
    home = paths.crews_home()
    cat = cli._load(None)
    view = cli.core_plan(cat, spec=four_role_spec(), answers=answers("security_review"), outage=False,
                         judge_mode="off", run_dir=str(tmp_path / "run"), allow=8, force=None)
    return home, copy.deepcopy(agent_calls(view)[0])


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("CREWS_SEAT_GUARD", "CREWS_SEAT_GUARD_MODE", "CLAUDE_PLUGIN_DATA"):
        monkeypatch.delenv(var, raising=False)


def test_index_line_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREWS_HOME", str(tmp_path / "state"))
    home, ac = planned(tmp_path)
    lines = [json.loads(x) for x in planindex.index_path(home).read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 5
    assert set(lines[0]) == {"ts", "subagent_type", "description", "prompt_sha256", "run_dir"}
    assert any(x["prompt_sha256"] == planindex.prompt_sha256(ac["prompt"]) for x in lines)
    assert datetime.fromisoformat(lines[0]["ts"]).utcoffset() == timedelta(0)


def test_guard_blocks_a_changed_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREWS_HOME", str(tmp_path / "state"))
    home, ac = planned(tmp_path)
    now = datetime.now(timezone.utc)
    assert guard.decide("Agent", ac, home, now).allow
    bad = {**ac, "prompt": ac["prompt"][:-1] + ("X" if ac["prompt"][-1] != "X" else "Y")}
    d = guard.decide("Agent", bad, home, now)
    assert not d.allow and "CREWS_SEAT_GUARD=0" in d.reason and "CREWS_SEAT_GUARD_MODE=advise" in d.reason
    other = {**ac, "description": ac["description"] + "-x"}
    assert not guard.decide("Agent", other, home, now).allow


def test_guard_blocks_an_entry_older_than_24_hours(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREWS_HOME", str(tmp_path / "state"))
    home, ac = planned(tmp_path)
    now = datetime.now(timezone.utc)
    assert guard.decide("Agent", ac, home, now + timedelta(hours=23)).allow
    assert not guard.decide("Agent", ac, home, now + timedelta(hours=25)).allow


def test_guard_plugin_prefixed_seat_form(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREWS_HOME", str(tmp_path / "state"))
    home, ac = planned(tmp_path)
    now = datetime.now(timezone.utc)
    prefixed = {**ac, "subagent_type": "crews:" + ac["subagent_type"]}
    assert not guard.decide("Agent", prefixed, home, now).allow
    assert guard.decide("Agent", prefixed, home, now, transcript_text=None).reason.startswith("crews-seat-guard")
    assert guard.SEAT_RE.match("crews:crew-produce-sonnet-high") and guard.SEAT_RE.match("crew-check-haiku")


def test_guard_generic_and_named_agents(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    for generic in ("general-purpose", "claude", "", "worker", "verifier"):
        d = guard.decide("Agent", {"subagent_type": generic, "description": "x", "prompt": "p"}, tmp_path, now)
        assert not d.allow, generic
        assert "CREWS_SEAT_GUARD=0" in d.reason and "CREWS_SEAT_GUARD_MODE=advise" in d.reason
    assert not guard.decide("Task", {"description": "no type", "prompt": "p"}, tmp_path, now).allow
    assert guard.decide("Agent", {"subagent_type": "Explore", "description": "x", "prompt": "p"}, tmp_path, now).allow
    assert guard.decide("Bash", {"command": "ls"}, tmp_path, now).allow


def test_guard_kill_switch_and_advise_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(timezone.utc)
    seat = {"subagent_type": "crew-produce-sonnet-high", "description": "unplanned", "prompt": "p"}
    generic = {"subagent_type": "general-purpose", "description": "x", "prompt": "p"}
    assert not guard.decide("Agent", seat, tmp_path, now).allow
    monkeypatch.setenv("CREWS_SEAT_GUARD_MODE", "advise")
    for call in (seat, generic):
        d = guard.decide("Agent", call, tmp_path, now)
        assert d.allow and d.warning and "would block" in d.warning
    monkeypatch.setenv("CREWS_SEAT_GUARD", "0")
    d = guard.decide("Agent", seat, tmp_path, now)
    assert d.allow and d.warning is None


def test_transcript_text_is_a_secondary_path(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    seat = {"subagent_type": "crew-produce-sonnet-high", "description": "unplanned", "prompt": "p"}
    text = '{"subagent_type": "crew-produce-sonnet-high", "description": "unplanned"}'
    assert guard.decide("Agent", seat, tmp_path, now, transcript_text=text).allow
    assert not guard.decide("Agent", seat, tmp_path, now, transcript_text="nothing here").allow


def run_hook(stdin: str, home: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(HOOK)], input=stdin, capture_output=True, text=True, timeout=60,
                          env={**os.environ, "CREWS_HOME": str(home), **env})


def test_hook_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREWS_HOME", str(tmp_path / "state"))
    home, ac = planned(tmp_path)
    allow = run_hook(json.dumps({"tool_name": "Agent", "tool_input": ac}), home)
    assert allow.returncode == 0, allow.stderr
    block_input = {**ac, "description": "never planned"}
    block = run_hook(json.dumps({"tool_name": "Agent", "tool_input": block_input}), home)
    assert block.returncode == 2 and "crews-seat-guard" in block.stderr
    generic = run_hook(json.dumps({"tool_name": "Agent", "tool_input": {"subagent_type": "general-purpose"}}), home)
    assert generic.returncode == 2
    advise = run_hook(json.dumps({"tool_name": "Agent", "tool_input": block_input}), home,
                      CREWS_SEAT_GUARD_MODE="advise")
    assert advise.returncode == 0 and "would block" in advise.stderr
    for garbage in ("{not json", "", "[1, 2]", "null", '{"tool_name": "Agent", "tool_input": 5}', "\x00\xff"):
        r = run_hook(garbage, home)
        assert r.returncode == 0, (garbage, r.stderr)


# --- OSS-FIX-2: every schema error in one refusal -----------------------------------------------------------------

def faulty_inputs() -> tuple[dict[str, Any], dict[str, Any]]:
    spec = four_role_spec()
    spec["roles"][0].pop("answers")                      # a role missing per role answers
    spec["roles"][0]["scope"] = "supabase/policies/"     # scope as a string
    spec["roles"][1]["authority"] = "the security review SRD"   # authority as a string
    spec["roles"][2]["deliverable"] = "out/findings.md"  # deliverable as a path
    spec["roles"][3]["returns"] = "report"               # a check role carrying returns
    flat = {"need": "integration_architecture", "divisible": "0.7", "needs_verifier": "yes", "difficulty": "2"}
    return spec, flat                                    # task answers as flat strings


def test_g37_one_refusal_lists_every_error(tmp_path: Path) -> None:
    spec, flat = faulty_inputs()
    with pytest.raises(planner.PlanRefused) as exc:
        cli.core_plan(cli._load(None), spec=spec, answers=flat, outage=False, judge_mode="off",
                      run_dir=str(tmp_path / "run"), allow=None, force=None)
    errors = exc.value.errors
    assert len(set(errors)) >= 6 and len(errors) == len(set(errors))
    text = "\n".join(errors)
    for needle in ("answers: need: must be an object", "answers: divisible.noul", "scope must be a list of paths",
                   "authority must be a list of non empty strings", "deliverable must be a plain file name",
                   "a check role has no returns field", "answers are missing"):
        assert needle in text, needle
    assert not (tmp_path / "run").exists()


def test_g37_over_mcp_and_cli(tmp_path: Path) -> None:
    spec, flat = faulty_inputs()
    env = calls([("crew_plan", {"roles": spec, "answers": flat, "run_dir": str(tmp_path / "run")})],
                env=plan_env(tmp_path))[0]
    assert env["status"] == "BLOCKED" and len(set(env["errors"])) >= 6
    assert all(e.startswith("PlanRefused: ") for e in env["errors"])
    (tmp_path / "roles.json").write_text(json.dumps(spec), encoding="utf-8")
    (tmp_path / "answers.json").write_text(json.dumps(flat), encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "crew.py"), "plan", "--roles", str(tmp_path / "roles.json"),
                        "--answers", str(tmp_path / "answers.json"), "--run-dir", str(tmp_path / "r2")],
                       capture_output=True, text=True, timeout=60, env={**os.environ, **plan_env(tmp_path)})
    assert r.returncode == 2 and "errors:" in r.stderr


def test_single_error_message_is_unchanged() -> None:
    assert str(planner.PlanRefused("one thing")) == "one thing"
    assert planner.PlanRefused(["a", "b"]).errors == ["a", "b"]


# --- OSS-FIX-3: compact inline result -------------------------------------------------------------------------------

def install_skills(home: Path, count: int) -> None:
    for i in range(count):
        d = home / ".claude" / "skills" / f"synthetic-skill-{i:04d}"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: synthetic-skill-{i:04d}\ndescription: Synthetic skill number {i} "
                                    "for the compact result test, covering refund policy review.\n---\n\nBody.\n",
                                    encoding="utf-8")


def test_compact_plan_result_with_400_skills(tmp_path: Path) -> None:
    key_file = tmp_path / "synthetic.key"
    key_file.write_text(SYNTHETIC_KEY, encoding="utf-8")
    env = plan_env(tmp_path, TYPESAFE_API_KEY_FILE=str(key_file))
    install_skills(Path(env["HOME"]), 400)
    base = {"roles": four_role_spec(), "answers": answers("security_review"), "skills": "live",
            "brief_check": "live", "judge": "off", "allow": 8}
    compact, full = calls([("crew_plan", {**base, "run_dir": str(tmp_path / "c")}),
                           ("crew_plan", {**base, "verbose": True, "run_dir": str(tmp_path / "v")})], env=env)
    assert compact["status"] == "COMPLETED_WITH_WARNINGS", compact["errors"]
    assert len(json.dumps(compact)) < 25_000
    out = compact["output"]
    assert out["compact"] is True and out["full_in"] == str(tmp_path / "c" / "plan.json")
    assert out["owner_gates"] and all("options" not in q for g in out["owner_gates"] for q in g["questions"].values())
    assert sum(q["option_count"] for g in out["owner_gates"] for q in g["questions"].values()) >= 1600
    assert all(set(c["agent_call"]) == {"subagent_type", "description", "prompt"}
               for w in out["waves"] for c in w["calls"])
    assert all(c["record_path"] for w in out["waves"] for c in w["calls"])
    saved = json.loads((tmp_path / "c" / "plan.json").read_text(encoding="utf-8"))
    assert len(saved["owner_gates"]) == len(out["owner_gates"])
    assert sum(len(q["options"]) for g in saved["owner_gates"] for q in g["questions"].values()) >= 1600
    assert "headless_argv" in json.dumps(saved)
    # verbose restores the full inline result
    vout = full["output"]
    assert len(json.dumps(full)) > 2 * len(json.dumps(compact)) and "compact" not in vout
    assert sum(len(q["options"]) for g in vout["owner_gates"] for q in g["questions"].values()) >= 1600
    assert all("deliverable" in c for w in vout["waves"] for c in w["calls"])


# --- OSS-FIX-4: judge live with declared answers degrades to a warning ----------------------------------------------

def test_judge_live_declared_answers_no_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY_FILE", raising=False)
    view = cli.core_plan(cli._load(None), spec=four_role_spec(), answers=answers("security_review"), outage=False,
                         judge_mode="live", run_dir=str(tmp_path / "run"), allow=8, force=None)
    assert view["cells"] == 5 and view["notes"][0] == cli.NO_KEY_NOTE
    assert "TYPESAFE_API_KEY_FILE" in view["notes"][0] and "typesafe" not in view
    res = calls([("crew_plan", {"roles": four_role_spec(), "answers": answers("security_review"), "judge": "live",
                                "skills": "off", "brief_check": "off", "allow": 8,
                                "run_dir": str(tmp_path / "srv")})], env=plan_env(tmp_path))[0]
    assert res["status"] == "COMPLETED_WITH_WARNINGS" and res["warnings"] and not res["errors"]
    assert res["output"]["cells"] == 5


def test_judge_live_declared_answers_with_key_skips_the_judge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", SYNTHETIC_KEY)
    monkeypatch.setenv("CREWS_EGRESS_DIR", str(tmp_path / "egress"))
    view = cli.core_plan(cli._load(None), spec=four_role_spec(), answers=answers("security_review"), outage=False,
                         judge_mode="live", run_dir=str(tmp_path / "run"), allow=8, force=None)
    assert view["cells"] == 5 and view["notes"][0] == cli.SKIPPED_NOTE
    assert "owner_gates" not in view and not (tmp_path / "egress" / "pending.jsonl").exists()
    key_file = tmp_path / "synthetic.key"
    key_file.write_text(SYNTHETIC_KEY, encoding="utf-8")
    res = calls([("crew_plan", {"roles": four_role_spec(), "answers": answers("security_review"), "judge": "live",
                                "skills": "off", "brief_check": "off", "allow": 8,
                                "run_dir": str(tmp_path / "srv")})],
                env=plan_env(tmp_path, TYPESAFE_API_KEY_FILE=str(key_file)))[0]
    assert res["status"] == "COMPLETED_WITH_WARNINGS" and not res["errors"]
    assert any("judge skipped" in w for w in res["warnings"])


def test_judge_live_budget_with_declared_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY_FILE", raising=False)
    cat = catalog.load()
    res = cli.core_budget(cat, answers=answers("security_review"), outage=False, judge_mode="live", task=None,
                          run_dir=str(tmp_path / "b"))
    assert res["notes"][0] == cli.NO_KEY_NOTE
    # the outage flag still conflicts with a live judge
    with pytest.raises(planner.PlanRefused):
        cli.core_budget(cat, answers=None, outage=True, judge_mode="live", task="t", run_dir=str(tmp_path / "b2"))
