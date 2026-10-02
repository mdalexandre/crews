"""Phase 4: task specific roles and the plan."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from crews import planner

ROOT = Path(__file__).resolve().parent.parent
CREW = ROOT / "crew.py"
FIX = ROOT / "tests" / "fixtures"
TABLE = json.loads((FIX / "live_table.json").read_text(encoding="utf-8"))["rows"]
NAMES = ["rename_button", "find_mrr", "refactor_billing", "security_review", "storefront_hub", "caption_listings"]
CAT = json.loads((ROOT / "crews" / "catalog.json").read_text(encoding="utf-8"))
LADDER = CAT["effort_ladder"]

# OBSERVED 2026-09-22, in this environment: `claude --help`'s own stdout capture is occasionally
# truncated at roughly half its length (a flush race inside the installed `claude` binary itself,
# confirmed by direct repeated subprocess.run calls against the system claude with no test code
# involved), which non-deterministically dropped later-listed flags such as --model from help_text
# and made test_plan_surfaces flaky. A complete `claude --help` capture in this environment is a
# stable 16384 characters across repeated runs; retrying a short, bounded number of times against
# that minimum length works around the external binary's own non-determinism without loosening the
# assertion below once help_text is obtained, and without changing the existing "skip the check if
# claude is unavailable" behavior when every attempt still comes back short.
_CLAUDE_HELP_MIN_LEN = 16000
_CLAUDE_HELP_RETRIES = 5


def _claude_help_text() -> str | None:
    if not shutil.which("claude"):
        return None
    text = ""
    for _ in range(_CLAUDE_HELP_RETRIES):
        text = subprocess.run(["claude", "--help"], capture_output=True, text=True).stdout
        if len(text) >= _CLAUDE_HELP_MIN_LEN:
            return text
    return text or None


def spec(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIX / "roles" / f"{name}.json").read_text(encoding="utf-8"))
    return data


def answers(name: str) -> dict[str, Any]:
    row: dict[str, Any] = TABLE[NAMES.index(name)]["answers"]
    return row


def plan(tmp: Path, roles: dict[str, Any], task_answers: dict[str, Any] | None, *extra: str,
         run: str = "run") -> subprocess.CompletedProcess[str]:
    rf, af = tmp / "roles_in.json", tmp / "answers_in.json"
    rf.write_text(json.dumps(roles), encoding="utf-8")
    args = ["--roles", str(rf), "--run-dir", str(tmp / run)]
    if task_answers is None:
        args.append("--outage")
    else:
        af.write_text(json.dumps(task_answers), encoding="utf-8")
        args += ["--answers", str(af)]
    return subprocess.run([sys.executable, "-X", "utf8", str(CREW), "plan", *args, *extra],
                          capture_output=True, text=True, timeout=30)


def calls(view: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for w in view["waves"] for c in w["calls"]]


@pytest.mark.parametrize("name", NAMES)
def test_plan_live_fixtures_plan(name: str, tmp_path: Path) -> None:
    result = plan(tmp_path, spec(name), answers(name))
    assert result.returncode == 0, result.stderr
    for f in ("budget.json", "roles.json", "plan.json", "seats.json"):
        assert (tmp_path / "run" / f).is_file()


@pytest.mark.parametrize("bad_name", ["verifier", "researcher"])
def test_plan_generic_role_refused(bad_name: str, tmp_path: Path) -> None:
    s = spec("security_review")
    s["roles"][0]["name"] = bad_name
    result = plan(tmp_path, s, answers("security_review"))
    assert result.returncode == 2 and bad_name in result.stderr


def test_plan_off_task_mission_refused(tmp_path: Path) -> None:
    s = spec("security_review")
    s["roles"][1]["mission"] = "Do good work carefully"
    result = plan(tmp_path, s, answers("security_review"))
    assert result.returncode == 2 and "rls-policy-auditor" in result.stderr and "mission" in result.stderr


def test_plan_check_role_required(tmp_path: Path) -> None:
    s = spec("security_review")
    s["roles"] = [r for r in s["roles"] if r["kind"] != "check"]
    result = plan(tmp_path, s, answers("security_review"))
    assert result.returncode == 2 and "missing kind check" in result.stderr


def test_plan_check_role_holds_no_write_scope(tmp_path: Path) -> None:
    s = spec("security_review")
    s["roles"][3]["scope"] = ["reports/"]
    result = plan(tmp_path, s, answers("security_review"))
    assert result.returncode == 2 and "exploit-repro-checker" in result.stderr


def test_plan_check_prompt_is_blind(tmp_path: Path) -> None:
    """V2-2a: a check cell's <artifact_under_check> holds only the write scopes of writing roles, never an
    earlier wave's record path (a record carries a producer's own conclusion)."""
    sentinel = "PRODUCER-CONCLUSION-SENTINEL-7731"
    for role in ("rls-policy-auditor", "service-key-leak-hunter"):
        d = tmp_path / "run" / "roles" / role
        d.mkdir(parents=True)
        (d / "conclusion.md").write_text(sentinel, encoding="utf-8")
    s = spec("security_review")
    result = plan(tmp_path, s, answers("security_review"))
    assert result.returncode == 0, result.stderr
    view = json.loads(result.stdout)
    earlier = [c["record_path"] for c in calls(view) if c["kind"] != "check"]
    write_scopes = [p for r in s["roles"] if r["kind"] in ("produce", "integrate") for p in r.get("scope", [])]
    for c in (c for c in calls(view) if c["kind"] == "check"):
        prompt = c["agent_call"]["prompt"]
        assert sentinel not in prompt
        for r in s["roles"]:
            if r["kind"] != "check":
                assert r["mission"] not in prompt
        for path in earlier:
            assert path not in prompt
        for scope_path in write_scopes:
            assert scope_path in prompt
        for criterion in s["roles"][3]["criteria"]:
            assert criterion in prompt


def _expected_seat(role: dict[str, Any]) -> tuple[str, str | None]:
    column = CAT["kinds"][role["kind"]]["column"]
    seat = CAT["needs"][role["answers"]["need"]][column]
    effort = seat["effort"]
    if role["answers"]["difficulty"] >= 2.5 and effort is not None:
        effort = LADDER[min(LADDER.index(effort) + 1, 4)]
    return seat["model"], effort


def test_plan_multi_agent_effort_worker(tmp_path: Path) -> None:
    s = spec("security_review")
    result = plan(tmp_path, s, answers("security_review"))
    view = json.loads(result.stdout)
    cs = calls(view)
    assert len({c["model"] for c in cs}) >= 2
    assert len({c["effort"] for c in cs}) >= 2
    per_role: dict[str, int] = {}
    for c in cs:
        per_role[c["role"]] = per_role.get(c["role"], 0) + 1
    assert max(per_role.values()) >= 2
    roles = {r["name"]: r for r in s["roles"]}
    for c in cs:
        assert (c["model"], c["effort"]) == _expected_seat(roles[c["role"]])


def test_plan_budget_and_cap(tmp_path: Path) -> None:
    s = spec("security_review")
    extra = copy.deepcopy(s["roles"][2])
    extra.update(name="refund-log-redaction-hunter", mission="Find refund log lines that expose the service role key",
                 scope=["supabase/functions/refund_logs/"])
    s["roles"].append(extra)
    over = plan(tmp_path, s, answers("security_review"))
    assert over.returncode == 2 and "budget" in over.stderr
    ok = plan(tmp_path, s, answers("security_review"), "--allow", "6", run="allowed")
    assert ok.returncode == 0, ok.stderr
    big = spec("caption_listings")
    force = tmp_path / "force.json"
    force.write_text(json.dumps({"roles": {"listing-caption-writer": {"workers": 8}}}), encoding="utf-8")
    capped = plan(tmp_path, big, answers("caption_listings"), "--force", str(force), run="capped")
    assert capped.returncode == 2 and "cap" in capped.stderr


def test_plan_force_effort_sweep(tmp_path: Path) -> None:
    force = tmp_path / "force.json"
    force.write_text(json.dumps({"roles": {"listing-caption-writer": {"efforts": ["low", "medium", "high"]}}}),
                     encoding="utf-8")
    result = plan(tmp_path, spec("caption_listings"), answers("caption_listings"), "--force", str(force))
    assert result.returncode == 0, result.stderr
    cs = [c for c in calls(json.loads(result.stdout)) if c["role"] == "listing-caption-writer"]
    assert [c["effort"] for c in cs] == ["low", "medium", "high"]


def test_plan_force_cannot_drop_check(tmp_path: Path) -> None:
    s = spec("caption_listings")
    s["roles"] = [r for r in s["roles"] if r["kind"] != "check"]
    force = tmp_path / "force.json"
    force.write_text(json.dumps({"roles": {"listing-caption-writer": {"efforts": ["low"]}}}), encoding="utf-8")
    result = plan(tmp_path, s, answers("caption_listings"), "--force", str(force))
    assert result.returncode == 2 and "missing kind check" in result.stderr


def test_plan_force_fable_refused_outside_specialist(tmp_path: Path) -> None:
    force = tmp_path / "force.json"
    force.write_text(json.dumps({"roles": {"service-key-leak-hunter": {"model": "fable", "efforts": ["high"]}}}),
                     encoding="utf-8")
    result = plan(tmp_path, spec("security_review"), answers("security_review"), "--force", str(force))
    assert result.returncode == 0, result.stderr
    view = json.loads(result.stdout)
    assert not any(c["model"] == "fable" for c in calls(view))
    assert any(n.startswith("REFUSED") and "service-key-leak-hunter" in n for n in view["notes"])


def _manifest(d: Path) -> dict[str, str]:
    if not d.is_dir():
        return {}
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*")) if p.is_file()}


def test_plan_surfaces(tmp_path: Path) -> None:
    s = spec("security_review")
    result = plan(tmp_path, s, answers("security_review"))
    agents = tmp_path / "agents"
    subprocess.run([sys.executable, "-X", "utf8", str(CREW), "install", "--agents-dir", str(agents)],
                   capture_output=True, text=True, check=True)
    full = json.loads((tmp_path / "run" / "plan.json").read_text(encoding="utf-8"))
    roles = {r["name"]: r for r in s["roles"]}
    help_text = _claude_help_text()
    for w in full["waves"]:
        for c in w["calls"]:
            call = c["agent_call"]
            fm = (agents / f"{call['subagent_type']}.md").read_text(encoding="utf-8")
            assert f"model: {c['model']}\n" in fm
            assert (f"effort: {c['effort']}\n" in fm) if c["effort"] else ("effort:" not in fm)
            role = roles[c["role"]]
            assert role["name"] in call["description"]
            for piece in (role["name"], role["mission"], *role.get("scope", [])):
                assert piece in call["prompt"]
            assert "Deliverable: write your result to" not in call["prompt"]
            argv = c["headless_argv"]
            prefix = planner.memory_scope(CAT)
            assert prefix in ([], ["systemd-run", "--user", "--scope", "-p", "MemoryHigh=2G", "-p", "MemoryMax=3G",
                                   "--collect", "--"])
            assert argv[:len(prefix)] == prefix and argv[len(prefix)] == "claude"
            assert argv[argv.index("--model") + 1] == c["model"]
            assert ("--effort" in argv) == (c["effort"] is not None)
            assert role["name"] in json.loads(argv[argv.index("--agents") + 1])
            assert argv[argv.index("--agent") + 1] == role["name"]
            if help_text:
                for flag in (a for a in argv[argv.index("claude"):] if a.startswith("-") and len(a) < 12):
                    assert flag in help_text, flag
    view = json.loads(result.stdout)
    assert all("headless_argv" not in c for c in calls(view))


@pytest.mark.parametrize(("platform", "which"), [("darwin", "/usr/bin/systemd-run"), ("linux", None),
                                                 ("win32", None)])
def test_headless_argv_has_no_systemd_off_linux(monkeypatch: pytest.MonkeyPatch, platform: str,
                                                which: str | None) -> None:
    """OSS-POR-5: the memory scope prefix is emitted only on Linux with systemd-run on PATH."""
    monkeypatch.setattr(planner.sys, "platform", platform)
    monkeypatch.setattr(planner.shutil, "which", lambda _name: which)
    assert planner.memory_scope(CAT) == []
    argv = planner._headless(CAT, "role-x", "sonnet", "medium", ["Read"], "body", "prompt")
    assert argv[0] == "claude" and "systemd-run" not in argv


def test_headless_argv_scopes_memory_on_linux_with_systemd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(planner.sys, "platform", "linux")
    monkeypatch.setattr(planner.shutil, "which", lambda _name: "/usr/bin/systemd-run")
    b = CAT["bound"]
    assert planner.memory_scope(CAT) == ["systemd-run", "--user", "--scope", "-p", f"MemoryHigh={b['memory_high']}",
                                         "-p", f"MemoryMax={b['memory_max']}", "--collect", "--"]


def test_plan_haiku_cell_has_no_effort(tmp_path: Path) -> None:
    s = spec("refactor_billing")
    s["roles"][1]["answers"]["need"] = "mechanical_verification"
    result = plan(tmp_path, s, answers("refactor_billing"))
    assert result.returncode == 0, result.stderr
    full = json.loads((tmp_path / "run" / "plan.json").read_text(encoding="utf-8"))
    check = [c for w in full["waves"] for c in w["calls"] if c["kind"] == "check"][0]
    assert check["model"] == "haiku" and check["effort"] is None and "--effort" not in check["headless_argv"]


def test_plan_waves_handoff_determinism(tmp_path: Path) -> None:
    s = spec("storefront_hub")
    first = plan(tmp_path, s, answers("storefront_hub"))
    second = plan(tmp_path, s, answers("storefront_hub"))
    assert first.returncode == 0, first.stderr
    assert hashlib.sha256(first.stdout.encode()).hexdigest() == hashlib.sha256(second.stdout.encode()).hexdigest()
    view = json.loads(first.stdout)
    assert [w["kind"] for w in view["waves"]] == ["research", "produce", "integrate", "check"]
    run_roles = str(tmp_path / "run" / "roles")
    write_scopes = [p for r in s["roles"] if r["kind"] in ("produce", "integrate") for p in r.get("scope", [])]
    earlier: list[str] = []
    for w in view["waves"]:
        for c in w["calls"]:
            assert c["deliverable"] == c["record_path"] and c["deliverable"].startswith(run_roles)
            if c["kind"] == "check":
                for path in earlier:
                    assert path not in c["agent_call"]["prompt"]
                for scope_path in write_scopes:
                    assert scope_path in c["agent_call"]["prompt"]
            else:
                for path in earlier:
                    assert path in c["agent_call"]["prompt"]
        earlier += [c["record_path"] for c in w["calls"]]


def test_plan_overlapping_scopes_refused(tmp_path: Path) -> None:
    s = spec("security_review")
    s["roles"][1]["slices"] = []
    s["roles"][1]["scope"] = ["src/"]
    s["roles"][2]["scope"] = ["src/a.py"]
    result = plan(tmp_path, s, answers("security_review"))
    assert result.returncode == 2 and "overlap" in result.stderr
    assert not (tmp_path / "run").exists()
    s2 = spec("security_review")
    s2["roles"][1]["slices"] = [{"brief": "all", "scope": ["src/"]}, {"brief": "one", "scope": ["src/a.py"]}]
    result2 = plan(tmp_path, s2, answers("security_review"), run="run2")
    assert result2.returncode == 2 and "overlap" in result2.stderr
    assert not (tmp_path / "run2").exists()
