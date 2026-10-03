"""Bounded escalation and repair: the ladder, the caps, the shared CLI and tool path, and the seat guard."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from crews import catalog, cli, escalate, guard, paths

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
CAT = catalog.load()
TABLE = json.loads((FIX / "live_table.json").read_text(encoding="utf-8"))["rows"]
NAMES = ["rename_button", "find_mrr", "refactor_billing", "security_review", "storefront_hub", "caption_listings"]


def planned(tmp_path: Path, name: str = "rename_button") -> Path:
    spec = json.loads((FIX / "roles" / f"{name}.json").read_text(encoding="utf-8"))
    cli.core_plan(CAT, spec=spec, answers=TABLE[NAMES.index(name)]["answers"], outage=False, judge_mode="off",
                  run_dir=str(tmp_path / "run"), allow=None, force=None, skills_mode="off", brief_check_mode="off")
    return tmp_path / "run"


def role_of(run: Path, kind: str) -> str:
    plan = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    return next(c["role"] for w in plan["waves"] for c in w["calls"] if c["kind"] == kind)


def test_effort_steps_up_before_model() -> None:
    step = escalate.next_seat
    assert step(CAT, "produce", "sonnet", "low", "repeatable_task") == escalate.NextSeat("sonnet", "medium")
    assert step(CAT, "produce", "sonnet", "high", "repeatable_task") == escalate.NextSeat("sonnet", "xhigh")
    assert step(CAT, "produce", "opus", "medium", "x") == escalate.NextSeat("opus", "high")


def test_model_steps_up_at_lowest_allowlisted_effort() -> None:
    assert escalate.next_seat(CAT, "produce", "sonnet", "xhigh", "x") == escalate.NextSeat("opus", "medium")
    assert escalate.next_seat(CAT, "produce", "sonnet", "max", "x") == escalate.NextSeat("opus", "medium")
    assert escalate.next_seat(CAT, "produce", "opus", "low", "x") == escalate.NextSeat("opus", "medium")


def test_never_below_baseline_model() -> None:
    assert escalate.next_seat(CAT, "produce", "haiku", None, "x") == escalate.NextSeat("sonnet", "low")


def test_targets_stay_on_the_private_allowlist() -> None:
    for need in ("repeatable_task", "specialist"):
        for model in CAT["capability"]:
            for effort in (None,) if model == "haiku" else CAT["effort_ladder"]:
                step = escalate.next_seat(CAT, "produce", model, effort, need)
                if step.model is not None:
                    assert step.model != "fable" and step.model != "haiku"
                    assert (step.effort or "") in escalate.ALLOWLIST[step.model]


def test_top_of_allowlist_and_fable_are_refused() -> None:
    for model, effort in (("opus", "xhigh"), ("opus", "max"), ("fable", "low"), ("fable", "max")):
        step = escalate.next_seat(CAT, "produce", model, effort, "specialist")
        assert step.model is None and step.refusal


def test_never_weaker_than_the_current_seat() -> None:
    for model in CAT["capability"]:
        for effort in (None,) if model == "haiku" else CAT["effort_ladder"]:
            step = escalate.next_seat(CAT, "produce", model, effort, "specialist")
            if step.model is not None:
                assert escalate._rank(CAT, step.model, step.effort) > escalate._rank(CAT, model, effort)


def test_escalate_returns_a_stronger_call_and_records_it(tmp_path: Path) -> None:
    run = planned(tmp_path)
    role = role_of(run, "produce")
    res = escalate.escalate(CAT, str(run), role, "tests still red")
    assert res["status"] == "ok" and res["number"] == 1
    assert res["seat"] == {"model": "sonnet", "effort": "medium", "seat_file": "crew-produce-sonnet-medium"}
    call = res["agent_call"]
    assert call["description"] == role and call["subagent_type"].endswith("crew-produce-sonnet-medium")
    assert "escalation 1 of 4" in call["prompt"] and "tests still red" in call["prompt"]
    ledger = json.loads((run / "escalations.json").read_text(encoding="utf-8"))["escalations"]
    assert len(ledger) == 1 and ledger[0]["to_effort"] == "medium" and ledger[0]["repair"] is False
    second = escalate.escalate(CAT, str(run), role, "still red")
    assert second["seat"]["effort"] == "high"  # steps from the latest seat, not the original


def test_fifth_escalation_is_blocked(tmp_path: Path) -> None:
    run = planned(tmp_path)
    role = role_of(run, "produce")
    for _ in range(4):
        assert escalate.escalate(CAT, str(run), role, "r")["status"] == "ok"
    res = escalate.escalate(CAT, str(run), role, "r")
    assert res["status"] == "blocked" and "4 escalations" in res["condition"] and "limit is 4" in res["condition"]
    assert len(json.loads((run / "escalations.json").read_text(encoding="utf-8"))["escalations"]) == 4


def test_third_repair_round_is_blocked_and_keeps_the_seat(tmp_path: Path) -> None:
    run = planned(tmp_path)
    role = role_of(run, "produce")
    first = escalate.escalate(CAT, str(run), role, "defect A", repair=True)
    assert first["seat"]["seat_file"] == "crew-produce-sonnet-low" and "repair 1 of 2" in first["agent_call"]["prompt"]
    assert escalate.escalate(CAT, str(run), role, "defect B", repair=True)["status"] == "ok"
    res = escalate.escalate(CAT, str(run), role, "defect C", repair=True)
    assert res["status"] == "blocked" and "2 repair rounds" in res["condition"]
    assert escalate.escalate(CAT, str(run), role, "r")["status"] == "ok"  # repairs do not spend escalations


def test_unknown_role_missing_plan_and_empty_reason_are_blocked(tmp_path: Path) -> None:
    run = planned(tmp_path)
    assert "unknown role" in escalate.escalate(CAT, str(run), "no-such-role", "r")["condition"]
    assert "plan.json" in escalate.escalate(CAT, str(tmp_path / "empty"), "x", "r")["condition"]
    assert "reason" in escalate.escalate(CAT, str(run), role_of(run, "produce"), " ")["condition"]


def test_model_step_in_a_run(tmp_path: Path) -> None:
    run = planned(tmp_path)
    role = role_of(run, "produce")
    seats = [escalate.escalate(CAT, str(run), role, "r")["seat"]["seat_file"] for _ in range(4)]
    assert seats == ["crew-produce-sonnet-medium", "crew-produce-sonnet-high",
                     "crew-produce-sonnet-xhigh", "crew-produce-opus-medium"]


def test_guard_admits_the_returned_call_and_blocks_an_altered_prompt(tmp_path: Path) -> None:
    run = planned(tmp_path)
    res = escalate.escalate(CAT, str(run), role_of(run, "produce"), "r")
    call = dict(res["agent_call"])
    now = datetime.now(timezone.utc)
    assert guard.decide("Agent", call, paths.crews_home(), now).allow
    assert not guard.decide("Agent", {**call, "prompt": call["prompt"] + " extra"}, paths.crews_home(), now).allow


def test_cli_and_blocked_exit(tmp_path: Path) -> None:
    run = planned(tmp_path)
    role = role_of(run, "produce")
    base = [sys.executable, "-X", "utf8", str(ROOT / "crew.py"), "escalate", "--run-dir", str(run), "--role", role]
    ok = subprocess.run([*base, "--reason", "red"], capture_output=True, text=True, timeout=30)
    assert ok.returncode == 0 and json.loads(ok.stdout)["seat"]["effort"] == "medium"
    bad = subprocess.run([*base[:-1], "nope", "--reason", "red"], capture_output=True, text=True, timeout=30)
    assert bad.returncode == 2 and "unknown role" in bad.stderr


def test_mcp_tool_envelope(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    import server  # noqa: PLC0415

    run = planned(tmp_path)
    env: dict[str, Any] = server.crew_escalate(str(run), role_of(run, "produce"), "red")
    assert env["status"] == "COMPLETED" and env["output"]["agent_call"]["description"]
    blocked = server.crew_escalate(str(run), "nope", "red")
    assert blocked["status"] == "BLOCKED" and "unknown role" in blocked["errors"][0]


def test_multi_cell_role_is_blocked(tmp_path: Path) -> None:
    run = planned(tmp_path, "security_review")
    res = escalate.escalate(CAT, str(run), "rls-policy-auditor", "r")
    assert res["status"] == "blocked" and "2 cells" in res["condition"]
