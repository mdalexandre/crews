"""Clean-machine MCP check (OSS-TST-3, OSS-POR-3).

Starts the crews MCP server exactly as the installed plugin's .mcp.json declares it (command, args and env, with
${CLAUDE_PLUGIN_ROOT}, ${CLAUDE_PLUGIN_DATA} and ${user_config.*} substituted the way Claude Code substitutes them),
calls crew_catalog and crew_plan over stdio with the judge, skills and brief check off, then feeds a returned Agent
call and a general-purpose call to the installed hooks/seat_guard.py.

Interface
    mcp_stdio_check.py <plugin_root> <plugin_data> <roles.json> <table.json>
    Prints one CHECK line per observation. Exit 0 only when every check holds, 1 otherwise.
    Run it with a Python that has the `mcp` package (the plugin's own environment under uv).
No credential of any kind is read or passed: the TypeSafe key option is substituted as the empty string.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

FAILED: list[str] = []
EXPECTED_TOOLS = {"crew_catalog", "crew_plan"}


def check(name: str, ok: bool, detail: str) -> None:
    print(f"CHECK {'PASS' if ok else 'FAIL'} {name}: {detail}", flush=True)
    if not ok:
        FAILED.append(name)


def substitute(value: str, root: str, data: str) -> str:
    value = value.replace("${CLAUDE_PLUGIN_ROOT}", root).replace("${CLAUDE_PLUGIN_DATA}", data)
    return re.sub(r"\$\{user_config\.[A-Za-z0-9_]+\}", "", value)


def declared_server(root: str, data: str) -> StdioServerParameters:
    manifest = json.loads((Path(root) / ".mcp.json").read_text(encoding="utf-8"))
    spec = manifest["mcpServers"]["crews"]
    env = {k: substitute(v, root, data) for k, v in spec.get("env", {}).items()}
    return StdioServerParameters(command=spec["command"], args=[substitute(a, root, data) for a in spec["args"]],
                                 env={**get_default_environment(), **env}, cwd=str(Path.home()))


def parse(result: Any) -> dict[str, Any]:
    assert not result.isError, result
    envelope: dict[str, Any] = json.loads(result.content[0].text)
    return envelope


def declared_hook(root: str, script: str) -> list[str]:
    """The argv hooks.json declares for a hook script, with ${CLAUDE_PLUGIN_ROOT} expanded as Claude Code does."""
    cfg = json.loads((Path(root) / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    for groups in cfg["hooks"].values():
        for group in groups:
            for h in group["hooks"]:
                if h["args"][1] == script:
                    return [h["command"], *(a.replace("${CLAUDE_PLUGIN_ROOT}", root) for a in h["args"])]
    raise SystemExit(f"no hook declared for {script}")


def machine_path() -> str:
    """PATH as the machine has it: `uv run` prepends the project venv (which holds a python3), so drop it."""
    venv = os.environ.get("VIRTUAL_ENV", "")
    keep = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and not (venv and d.startswith(venv))]
    return os.pathsep.join(keep)


def run_hook(root: str, data: str, payload: dict[str, Any]) -> tuple[int, str]:
    """The seat guard exactly as hooks.json declares it (sh and the launcher), with this machine's PATH."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CREWS_", "CLAUDE_PLUGIN_"))}
    env.update({"CLAUDE_PLUGIN_ROOT": root, "CLAUDE_PLUGIN_DATA": data, "PATH": machine_path()})
    env.pop("VIRTUAL_ENV", None)
    proc = subprocess.run(declared_hook(root, "seat_guard.py"), input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=300)
    return proc.returncode, proc.stderr.strip()


async def main(root: str, data: str, roles_path: str, table_path: str) -> None:
    roles = json.loads(Path(roles_path).read_text(encoding="utf-8"))
    answers = json.loads(Path(table_path).read_text(encoding="utf-8"))["rows"][5]["answers"]
    found = shutil.which("python3", path=machine_path())
    check("no_python3_on_path", found is None, f"which python3 on the machine PATH = {found}")
    check("hook_declared_argv", True, " ".join(declared_hook(root, "seat_guard.py")))
    params = declared_server(root, data)
    check("server_command", True, f"{params.command} {' '.join(params.args)}")
    run_dir = Path(data) / "clean-machine-run"
    async with stdio_client(params, errlog=sys.stderr) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            check("tools_listed", EXPECTED_TOOLS.issubset(tools), ", ".join(sorted(tools)))
            cat = parse(await s.call_tool("crew_catalog", {}))
            check("crew_catalog", cat.get("status") in ("COMPLETED", "COMPLETED_WITH_WARNINGS"),
                  f"status={cat.get('status')} keys={sorted(cat.get('output', {}))[:6]}")
            plan = parse(await s.call_tool("crew_plan", {
                "roles": roles, "answers": answers, "judge": "off", "skills": "off", "brief_check": "off",
                "run_dir": str(run_dir), "allow": 8}))
            check("crew_plan", plan.get("status") in ("COMPLETED", "COMPLETED_WITH_WARNINGS"),
                  f"status={plan.get('status')} errors={plan.get('errors')}")
    waves = plan.get("output", {}).get("waves", [])
    calls = [c for w in waves for c in w.get("calls", [])]
    check("plan_returned_calls", bool(calls), f"{len(waves)} wave(s), {len(calls)} call(s)")
    call = calls[0]["agent_call"]
    tool_input: dict[str, str] = {k: call[k] for k in ("subagent_type", "description", "prompt")}
    payload: dict[str, Any] = {"tool_name": "Agent", "tool_input": tool_input}
    code, err = run_hook(root, data, payload)
    check("guard_admits_planned_call", code == 0,
          f"exit={code} subagent_type={call['subagent_type']} stderr={err[:120]!r}")
    code, err = run_hook(root, data, {"tool_name": "Agent", "tool_input": {
        "subagent_type": "general-purpose", "description": "do it", "prompt": "do the task"}})
    check("guard_blocks_general_purpose", code == 2, f"exit={code} stderr={err[:90]!r}")
    forged = dict(tool_input, prompt=tool_input["prompt"] + " (edited)")
    code, err = run_hook(root, data, {"tool_name": "Agent", "tool_input": forged})
    check("guard_blocks_edited_planned_call", code == 2, f"exit={code}")


if __name__ == "__main__":
    anyio.run(main, sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
    print(f"RESULT {'FAIL' if FAILED else 'PASS'} failed={FAILED}")
    sys.exit(1 if FAILED else 0)
