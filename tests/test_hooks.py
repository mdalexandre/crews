"""Hook scripts run as subprocesses with synthetic JSON stdin and a temp state directory."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "hooks"
SCRIPTS = ["seat_guard.py", "session_start.py", "inline_budget.py", "qa_leak_check.py"]
SCRUB = ("CREWS_SEAT_GUARD", "CREWS_SEAT_GUARD_MODE", "CLAUDE_PLUGIN_OPTION_SEAT_GUARD_MODE",
         "claude_plugin_option_seat_guard_mode", "CLAUDE_PLUGIN_OPTION_INLINE_BUDGET_ENABLED",
         "claude_plugin_option_inline_budget_enabled", "CLAUDE_PLUGIN_OPTION_INLINE_BUDGET",
         "claude_plugin_option_inline_budget", "CLAUDE_PLUGIN_ROOT")


def run(script: str, payload: Any, data: Path, *args: str, raw: str | None = None,
        **env: str) -> subprocess.CompletedProcess[str]:
    full = {k: v for k, v in os.environ.items() if k not in SCRUB}
    full.update({"CLAUDE_PLUGIN_DATA": str(data), "CREWS_HOME": str(data)})
    full.update(env)
    stdin = raw if raw is not None else json.dumps(payload)
    return subprocess.run([sys.executable, str(HOOKS / script), *args], input=stdin, capture_output=True,
                          text=True, env=full, timeout=60, check=False)


def agent(seat: str, prompt: str = "do the work") -> dict[str, Any]:
    return {"tool_name": "Agent", "tool_input": {"subagent_type": seat, "description": "role", "prompt": prompt}}


# seat guard

def test_seat_guard_blocks_generic_agent(tmp_path: Path) -> None:
    res = run("seat_guard.py", agent("general-purpose"), tmp_path)
    assert res.returncode == 2
    assert "CREWS_SEAT_GUARD=0" in res.stderr and "seat_guard_mode" in res.stderr


def test_seat_guard_blocks_unplanned_seat_and_admits_planned(tmp_path: Path) -> None:
    seat = "crews:crew-produce-sonnet-medium"
    assert run("seat_guard.py", agent(seat), tmp_path).returncode == 2
    sys.path.insert(0, str(ROOT))
    from crews import planindex

    planindex.record({"waves": [{"calls": [{"agent_call": {"subagent_type": seat, "description": "role",
                                                           "prompt": "do the work"}}]}]}, "run", home=tmp_path)
    assert run("seat_guard.py", agent(seat), tmp_path).returncode == 0


def test_seat_guard_allows_named_specialist(tmp_path: Path) -> None:
    assert run("seat_guard.py", agent("Explore"), tmp_path).returncode == 0


@pytest.mark.parametrize("key", ["CLAUDE_PLUGIN_OPTION_SEAT_GUARD_MODE", "claude_plugin_option_seat_guard_mode"])
def test_seat_guard_advise_option(tmp_path: Path, key: str) -> None:
    res = run("seat_guard.py", agent("general-purpose"), tmp_path, **{key: "advise"})
    assert res.returncode == 0
    assert "advise mode" in res.stderr


def test_seat_guard_block_option_and_plugin_root(tmp_path: Path) -> None:
    res = run("seat_guard.py", agent("claude"), tmp_path, CLAUDE_PLUGIN_OPTION_SEAT_GUARD_MODE="block",
              CLAUDE_PLUGIN_ROOT=str(ROOT))
    assert res.returncode == 2


# inline budget

BASH = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "true"}}
ENABLED = {"CLAUDE_PLUGIN_OPTION_INLINE_BUDGET_ENABLED": "true", "CLAUDE_PLUGIN_OPTION_INLINE_BUDGET": "3"}


def count(data: Path, n: int, **env: str) -> None:
    for _ in range(n):
        assert run("inline_budget.py", {**BASH, "hook_event_name": "PostToolUse"}, data, "post", **env).returncode == 0


def pre(data: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return run("inline_budget.py", {**BASH, "hook_event_name": "PreToolUse"}, data, "pre", **env)


def test_inline_budget_off_by_default(tmp_path: Path) -> None:
    count(tmp_path, 12)
    assert pre(tmp_path).returncode == 0
    assert not (tmp_path / "inline_budget").exists()


def test_inline_budget_denies_past_budget(tmp_path: Path) -> None:
    run("inline_budget.py", {"session_id": "s1"}, tmp_path, "prompt", **ENABLED)
    count(tmp_path, 3, **ENABLED)
    res = pre(tmp_path, **ENABLED)
    assert res.returncode == 2
    assert "inline_budget_enabled" in res.stderr


def test_inline_budget_allows_up_to_budget(tmp_path: Path) -> None:
    count(tmp_path, 2, **ENABLED)
    assert pre(tmp_path, **ENABLED).returncode == 0


def test_inline_budget_default_is_five(tmp_path: Path) -> None:
    env = {"CLAUDE_PLUGIN_OPTION_INLINE_BUDGET_ENABLED": "true"}
    count(tmp_path, 4, **env)
    assert pre(tmp_path, **env).returncode == 0
    count(tmp_path, 1, **env)
    assert pre(tmp_path, **env).returncode == 2


def test_inline_budget_lifted_by_crew_plan(tmp_path: Path) -> None:
    count(tmp_path, 3, **ENABLED)
    assert pre(tmp_path, **ENABLED).returncode == 2
    plan = {"session_id": "s1", "tool_name": "mcp__plugin_crews_crews__crew_plan", "tool_input": {}}
    assert run("inline_budget.py", plan, tmp_path, "post", **ENABLED).returncode == 0
    assert pre(tmp_path, **ENABLED).returncode == 0


def test_inline_budget_lifted_by_seat_dispatch(tmp_path: Path) -> None:
    count(tmp_path, 3, **ENABLED)
    dispatch = {"session_id": "s1", **agent("crews:crew-produce-sonnet-medium")}
    run("inline_budget.py", dispatch, tmp_path, "post", **ENABLED)
    assert pre(tmp_path, **ENABLED).returncode == 0


def test_inline_budget_resets_on_new_prompt(tmp_path: Path) -> None:
    count(tmp_path, 3, **ENABLED)
    assert pre(tmp_path, **ENABLED).returncode == 2
    run("inline_budget.py", {"session_id": "s1"}, tmp_path, "prompt", **ENABLED)
    assert pre(tmp_path, **ENABLED).returncode == 0


def test_inline_budget_ignores_subagents_and_non_bash(tmp_path: Path) -> None:
    count(tmp_path, 3, **ENABLED)
    assert run("inline_budget.py", {**BASH, "agent_id": "a1"}, tmp_path, "pre", **ENABLED).returncode == 0
    read = {"session_id": "s1", "tool_name": "Read", "tool_input": {}}
    assert run("inline_budget.py", read, tmp_path, "pre", **ENABLED).returncode == 0


# qa leak check

def test_qa_leak_warns_but_exits_zero(tmp_path: Path) -> None:
    res = run("qa_leak_check.py", agent("crews:crew-check-haiku", "I verified that the fix is correct. Confirm it."),
              tmp_path)
    assert res.returncode == 0
    assert "qa-leak-check" in res.stderr


def test_qa_leak_clean_and_non_check_silent(tmp_path: Path) -> None:
    assert run("qa_leak_check.py", agent("crews:crew-check-haiku", "Check C1 against src/a.py."),
               tmp_path).stderr == ""
    assert run("qa_leak_check.py", agent("crews:crew-produce-sonnet-medium", "I verified that it works."),
               tmp_path).stderr == ""


# session start

def test_session_start_prints_one_line(tmp_path: Path) -> None:
    res = run("session_start.py", {}, tmp_path)
    assert res.returncode == 0
    lines = res.stdout.strip().splitlines()
    assert len(lines) == 1 and "crew" in lines[0] and "crew_plan" in lines[0]


# every hook fails open

@pytest.mark.parametrize("script", SCRIPTS)
@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", "null", '{"tool_name": 5, "tool_input": "x"}'])
def test_hooks_exit_zero_on_garbage(tmp_path: Path, script: str, raw: str) -> None:
    for args in ([], ["pre"], ["post"], ["prompt"]) if script == "inline_budget.py" else ([],):
        env = ENABLED if script == "inline_budget.py" else {}
        assert run(script, None, tmp_path, *args, raw=raw, **env).returncode == 0


# registration and compile

def test_hooks_json_args_form() -> None:
    hooks = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    seen: set[str] = set()
    for groups in hooks.values():
        for group in groups:
            for h in group["hooks"]:
                assert h["command"] == "python3" and h["args"][0].startswith("${CLAUDE_PLUGIN_ROOT}/hooks/")
                assert (ROOT / h["args"][0].replace("${CLAUDE_PLUGIN_ROOT}/", "")).is_file()
                seen.add(Path(h["args"][0]).stem)
    assert seen == {"seat_guard", "session_start", "inline_budget", "qa_leak_check"}
    assert {g["matcher"] for g in hooks["PreToolUse"]} == {"Agent|Task", "Bash"}


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
@pytest.mark.parametrize("script", SCRIPTS)
def test_hooks_compile_on_python39(tmp_path: Path, script: str) -> None:
    res = subprocess.run(["uv", "run", "--no-project", "--python", "3.9", "python", "-m", "py_compile",
                          str(HOOKS / script)], capture_output=True, text=True, timeout=600, check=False,
                         env={**os.environ, "PYTHONPYCACHEPREFIX": str(tmp_path)})
    assert res.returncode == 0, res.stderr
