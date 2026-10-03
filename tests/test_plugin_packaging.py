"""P5 plugin packaging (OSS-PKG-1 to PKG-5, PKG-8, PKG-10). Synthetic data only, no network, no live key."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from test_p4_fixes import four_role_spec  # noqa: E402
from test_server import answers, calls  # noqa: E402

from crews import catalog, cli, guard, paths  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / ".claude-plugin" / "plugin.json"
MARKET = ROOT / ".claude-plugin" / "marketplace.json"
MCP = ROOT / ".mcp.json"
CHECK = ROOT / "scripts" / "check_agents.py"
JSON_FILES = [PLUGIN, MARKET, MCP, ROOT / "hooks" / "hooks.json"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("CREWS_SEAT_GUARD", "CREWS_SEAT_GUARD_MODE", "CLAUDE_PLUGIN_DATA", "CREWS_SEAT_PREFIX",
                "TYPESAFE_API_KEY", "TYPESAFE_API_KEY_FILE"):
        monkeypatch.delenv(var, raising=False)


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


# --- manifests ---------------------------------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("claude") is None, reason="the claude binary is absent")
def test_validate_strict_exits_zero() -> None:
    r = subprocess.run(["claude", "plugin", "validate", "--strict", str(ROOT)], capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


def test_manifest_contract() -> None:
    plugin, market = load(PLUGIN), load(MARKET)
    assert plugin["name"] == "crews" and not plugin["name"].startswith("claude-")
    assert plugin["version"] == "0.2.0" and plugin["license"] == "MIT"
    assert "@" not in json.dumps(plugin["author"]) and plugin["author"] == {"name": "mdalexandre"}
    assert "independent project for Claude Code" in plugin["description"]
    cfg = plugin["userConfig"]
    assert set(cfg) == {"typesafe_api_key", "inline_budget_enabled", "inline_budget", "seat_guard_mode"}
    assert (cfg["typesafe_api_key"]["type"], cfg["typesafe_api_key"]["sensitive"],
            cfg["typesafe_api_key"]["default"]) == ("string", True, "")
    assert (cfg["inline_budget_enabled"]["type"], cfg["inline_budget_enabled"]["default"]) == ("boolean", False)
    assert (cfg["inline_budget"]["type"], cfg["inline_budget"]["default"]) == ("number", 5)
    assert (cfg["seat_guard_mode"]["type"], cfg["seat_guard_mode"]["default"]) == ("string", "block")
    assert [(p["name"], p["source"]) for p in market["plugins"]] == [(plugin["name"], ".")]
    assert not (ROOT / "CLAUDE.md").exists()


def test_mcp_registration() -> None:
    server = load(MCP)["mcpServers"]["crews"]
    assert server["command"] == "uv"
    assert server["args"] == ["run", "--project", "${CLAUDE_PLUGIN_ROOT}", "--frozen", "python",
                              "${CLAUDE_PLUGIN_ROOT}/server.py"]
    assert server["env"] == {"UV_PROJECT_ENVIRONMENT": "${CLAUDE_PLUGIN_DATA}/venv",
                             "CREWS_HOME": "${CLAUDE_PLUGIN_DATA}", "CREWS_SEAT_PREFIX": "crews:",
                             "TYPESAFE_API_KEY": "${user_config.typesafe_api_key}"}


def strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        return [s for item in node for s in strings(item)]
    if isinstance(node, dict):
        return [s for item in node.values() for s in strings(item)]
    return []


def path_problems(text: str) -> list[str]:
    """Tokens that are an absolute path or climb out of the plugin tree. A token that opens with a ${...}
    placeholder is rooted by the host and allowed; a bare relative token such as '.' is allowed."""
    found: list[str] = []
    for token in text.split():
        if token.startswith("${"):
            rest = token.split("}", 1)[1]
        else:
            rest = token
            if rest.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[\\/]", rest):
                found.append(token)
        if ".." in rest.replace("\\", "/").split("/"):
            found.append(token)
    return found


@pytest.mark.parametrize("path", [p for p in JSON_FILES if p.exists()], ids=lambda p: p.name)
def test_no_path_outside_the_plugin_root(path: Path) -> None:
    bad = [(s, path_problems(s)) for s in strings(load(path)) if path_problems(s)]
    assert not bad, bad


def test_path_scan_catches_what_it_should() -> None:
    assert path_problems("/usr/bin/python") and path_problems("${CLAUDE_PLUGIN_ROOT}/../x")
    assert path_problems("~/x") and path_problems("a/../b")
    assert not path_problems("${CLAUDE_PLUGIN_ROOT}/server.py") and not path_problems(".")


# --- seat agents -------------------------------------------------------------------------------------------------

def test_agents_match_regeneration_and_reachable_seats() -> None:
    cat = catalog.load()
    names = sorted(p.name for p in (ROOT / "agents").iterdir())
    assert names == sorted(f"{s['agent']}.md" for s in catalog.reachable_seats(cat))
    r = subprocess.run([sys.executable, str(CHECK)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert f"{len(names)} seats" in r.stdout


def test_check_agents_negative_controls(tmp_path: Path) -> None:
    copy = tmp_path / "agents"
    shutil.copytree(ROOT / "agents", copy)

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(CHECK), "--agents-dir", str(copy)], capture_output=True,
                              text=True, timeout=60)

    assert run().returncode == 0
    victim = next(copy.glob("crew-*.md"))
    victim.write_text(victim.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")
    assert run().returncode == 1 and "differs" in run().stderr
    shutil.copy2(ROOT / "agents" / victim.name, victim)
    victim.unlink()
    assert run().returncode == 1 and "missing" in run().stderr
    shutil.copy2(ROOT / "agents" / victim.name, victim)
    (copy / "crew-extra-haiku.md").write_text("x", encoding="utf-8")
    assert run().returncode == 1 and "not a reachable seat" in run().stderr


# --- seat prefix (OSS-PKG-5) -------------------------------------------------------------------------------------

def plan_calls(tmp_path: Path) -> list[dict[str, Any]]:
    cat = cli._load(None)
    view = cli.core_plan(cat, spec=four_role_spec(), answers=answers("security_review"), outage=False,
                         judge_mode="off", run_dir=str(tmp_path / "run"), allow=8, force=None)
    return [c["agent_call"] for w in view["waves"] for c in w["calls"]]


def test_default_plan_has_no_prefix(tmp_path: Path) -> None:
    sent = plan_calls(tmp_path)
    assert sent and all(ac["subagent_type"].startswith("crew-") for ac in sent)


def test_prefixed_plan_is_admitted_and_generic_is_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREWS_SEAT_PREFIX", "crews:")
    sent = plan_calls(tmp_path)
    assert sent and all(ac["subagent_type"].startswith("crews:crew-") for ac in sent)
    home, now = paths.crews_home(), datetime.now(timezone.utc)
    for ac in sent:
        d = guard.decide("Agent", ac, home, now)
        assert d.allow and d.warning is None, (ac["subagent_type"], d.reason)
        bare = {**ac, "subagent_type": ac["subagent_type"].split(":", 1)[1]}
        assert not guard.decide("Agent", bare, home, now).allow  # the index holds the prefixed form only
        assert not guard.decide("Agent", {**ac, "prompt": ac["prompt"] + " edited"}, home, now).allow
        assert not guard.decide("Agent", ac, home, now + timedelta(hours=25)).allow
    # the hook may run without the variable set: the exact indexed form is still admitted
    monkeypatch.delenv("CREWS_SEAT_PREFIX")
    assert all(guard.decide("Agent", ac, home, now).allow for ac in sent)


@pytest.mark.parametrize("prefix", ["", "crews:"])
def test_generic_agents_are_blocked_with_and_without_a_prefix(prefix: str, monkeypatch: pytest.MonkeyPatch,
                                                              tmp_path: Path) -> None:
    if prefix:
        monkeypatch.setenv("CREWS_SEAT_PREFIX", prefix)
    now = datetime.now(timezone.utc)
    for name in ("general-purpose", "claude", "worker", "reviewer", "crews:general-purpose", "crews:worker", ""):
        d = guard.decide("Agent", {"subagent_type": name, "description": "d", "prompt": "p"}, tmp_path, now)
        assert not d.allow, name
    for name in ("Explore", "crews:something-named", "khec-orchestrator"):
        assert guard.decide("Agent", {"subagent_type": name, "description": "d", "prompt": "p"}, tmp_path, now).allow
    unplanned = {"subagent_type": f"{prefix}crew-produce-sonnet-high", "description": "d", "prompt": "p"}
    assert not guard.decide("Agent", unplanned, tmp_path, now).allow


# --- empty key (OSS-PKG-8) ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["", "   "])
def test_empty_typesafe_key_means_judge_off_with_one_warning(value: str, tmp_path: Path) -> None:
    from test_server import get_default_environment  # noqa: PLC0415

    env = {"TYPESAFE_API_KEY": value, "CREWS_HOME": str(tmp_path / "state"), "HOME": str(tmp_path),
           "PATH": get_default_environment().get("PATH", "")}
    out = calls([("crew_budget", {"answers": answers("rename_button"), "run_dir": str(tmp_path / "b")})], env=env)[0]
    assert out["status"] == "COMPLETED_WITH_WARNINGS"
    assert len(out["warnings"]) == 1 and "SRD Q9" in out["warnings"][0]


def test_non_empty_typesafe_key_counts_as_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import server  # noqa: PLC0415

    warnings: list[str] = []
    assert server._live_gate("live", warnings) == ("off", True) and len(warnings) == 1
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    warnings = []
    assert server._live_gate("live", warnings) == ("live", False) and warnings == []


def test_catalog_seat_set_is_what_agents_holds() -> None:
    assert len(catalog.reachable_seats(catalog.load())) == len(list((ROOT / "agents").glob("crew-*.md")))
