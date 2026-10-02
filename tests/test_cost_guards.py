"""Cost guards: B1 the planner note, B2 the brief contract v2 seat bodies (V2-5) hold no forbidden phrase."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
CREW = ROOT / "crew.py"
FIX = ROOT / "tests" / "fixtures"
TABLE = json.loads((FIX / "live_table.json").read_text(encoding="utf-8"))["rows"]
NAMES = ["rename_button", "find_mrr", "refactor_billing", "security_review", "storefront_hub", "caption_listings"]
CAT = json.loads((ROOT / "crews" / "catalog.json").read_text(encoding="utf-8"))
LADDER = CAT["effort_ladder"]

B1_NOTE = ("all produce cells seated on Opus or Fable: confirm each piece needs hard reasoning; "
           "execution usually belongs on a Sonnet row (repeatable_task, bounded_retrieval, "
           "everyday_implementation, long_running_agentic)")
FORBIDDEN_PHRASES = ("heredoc", "Report the commands you ran", "at most 5 lines",
                     "full result belongs in the deliverable file")
NO_NARRATION_LINE = "Return only what the output contract asks for: no narration, no command transcripts."


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


def _expected_seat(role: dict[str, Any]) -> tuple[str, str | None]:
    column = CAT["kinds"][role["kind"]]["column"]
    seat = CAT["needs"][role["answers"]["need"]][column]
    effort = seat["effort"]
    if role["answers"]["difficulty"] >= 2.5 and effort is not None:
        effort = LADDER[min(LADDER.index(effort) + 1, 4)]
    return seat["model"], effort


def test_b1_note_present_when_every_produce_cell_is_opus_or_fable(tmp_path: Path) -> None:
    s = spec("security_review")
    result = plan(tmp_path, s, answers("security_review"))
    assert result.returncode == 0, result.stderr
    view = json.loads(result.stdout)
    produce = [c for c in calls(view) if c["kind"] == "produce"]
    assert produce and all(c["model"] in ("opus", "fable") for c in produce)
    assert B1_NOTE in view["notes"]


def test_b1_note_absent_when_a_produce_cell_is_sonnet_or_haiku(tmp_path: Path) -> None:
    s = spec("refactor_billing")
    result = plan(tmp_path, s, answers("refactor_billing"))
    assert result.returncode == 0, result.stderr
    view = json.loads(result.stdout)
    produce = [c for c in calls(view) if c["kind"] == "produce"]
    assert produce and any(c["model"] not in ("opus", "fable") for c in produce)
    assert B1_NOTE not in view["notes"]


def test_b1_leaves_cells_and_seats_unchanged(tmp_path: Path) -> None:
    s = spec("security_review")
    result = plan(tmp_path, s, answers("security_review"))
    assert result.returncode == 0, result.stderr
    view = json.loads(result.stdout)
    cs = calls(view)
    roles = {r["name"]: r for r in s["roles"]}
    for c in cs:
        assert (c["model"], c["effort"]) == _expected_seat(roles[c["role"]])
    assert view["cells"] == len(cs)
    seats = json.loads((tmp_path / "run" / "seats.json").read_text(encoding="utf-8"))["seats"]
    assert len(seats) == len(cs)
    for row, c in zip(seats, cs, strict=False):
        assert row["model"] == c["model"]
    # the note list carries at most the one B1 note beyond whatever budget or force notes fired
    assert view["notes"].count(B1_NOTE) <= 1


def test_b1_note_never_appears_more_than_once(tmp_path: Path) -> None:
    s = spec("security_review")
    result = plan(tmp_path, s, answers("security_review"))
    assert result.returncode == 0, result.stderr
    view = json.loads(result.stdout)
    assert view["notes"].count(B1_NOTE) == 1


def test_b2_seat_body_holds_no_forbidden_phrase() -> None:
    for kind, row in CAT["kinds"].items():
        for phrase in FORBIDDEN_PHRASES:
            assert phrase not in row["body"], f"kind {kind} body carries the forbidden phrase {phrase!r}"
        assert row["body"].endswith("no narration, no command transcripts.")


def test_b2_catalog_version_bumped() -> None:
    assert CAT["version"] == "2.1.0"


def test_b2_catalog_still_validates() -> None:
    result = subprocess.run([sys.executable, "-X", "utf8", str(CREW), "validate"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", NAMES)
def test_b2_seat_body_reaches_the_installed_agent_body(name: str, tmp_path: Path) -> None:
    result = plan(tmp_path, spec(name), answers(name))
    assert result.returncode == 0, result.stderr
    agents = tmp_path / "agents"
    subprocess.run([sys.executable, "-X", "utf8", str(CREW), "install", "--agents-dir", str(agents)],
                   capture_output=True, text=True, check=True)
    full = json.loads((tmp_path / "run" / "plan.json").read_text(encoding="utf-8"))
    for w in full["waves"]:
        for c in w["calls"]:
            fm = (agents / f"{c['agent_call']['subagent_type']}.md").read_text(encoding="utf-8")
            assert CAT["kinds"][c["kind"]]["body"] in fm
            for phrase in FORBIDDEN_PHRASES:
                assert phrase not in fm
