"""Phase 1: catalog validation, listing, standard library rule, and parity with the prototype."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
CREW = ROOT / "crew.py"
BAD = ROOT / "tests" / "fixtures" / "catalog_bad"

BAD_CASES = {
    "haiku_with_effort.json": "carries effort",
    "effort_outside_ladder.json": "outside low, medium, high, xhigh, max",
    "model_outside_order.json": "not in the capability order",
    "duplicate_need.json": "duplicate key 'repeatable_task'",
    "kind_with_agent_tool.json": "tools include Agent",
    "fable_outside_specialist.json": "fable appears outside the specialist row",
    "not_json.json": "not JSON",
}


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-X", "utf8", str(CREW), *args], capture_output=True, text=True, timeout=30)


def test_catalog_shipped_validates() -> None:
    result = run("validate")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name,needle", sorted(BAD_CASES.items()))
def test_catalog_bad_fixture_refused(name: str, needle: str) -> None:
    result = run("--catalog", str(BAD / name), "validate")
    assert result.returncode == 2
    assert needle in result.stderr


def test_catalog_bad_fixture_set_is_complete() -> None:
    assert sorted(p.name for p in BAD.glob("*.json")) == sorted(BAD_CASES)


def _independent_reachable(cat: dict[str, Any]) -> set[str]:
    ladder = ["low", "medium", "high", "xhigh", "max"]
    names: set[str] = set()
    for row in cat["needs"].values():
        for kind, spec in cat["kinds"].items():
            model, effort = row[spec["column"]]["model"], row[spec["column"]]["effort"]
            efforts = [effort] if effort is None else [effort, ladder[min(ladder.index(effort) + 1, 4)]]
            for e in efforts:
                names.add(f"crew-{kind}-{model}" + ("" if e is None else f"-{e}"))
    return names


def test_catalog_listing_is_complete() -> None:
    result = run("list", "--json")
    assert result.returncode == 0
    listed = json.loads(result.stdout)["seats"]
    cat = json.loads((ROOT / "crews" / "catalog.json").read_text(encoding="utf-8"))
    assert {s["agent"] for s in listed} == _independent_reachable(cat)
    assert len(listed) == len(_independent_reachable(cat))
    for s in listed:
        assert set(s) == {"agent", "kind", "model", "effort", "tools"}
        if s["model"] == "haiku":
            assert s["effort"] is None
        assert "Agent" not in s["tools"]


def test_catalog_core_is_standard_library_only() -> None:
    banned = ("requests", "httpx", "yaml", "typesafe_sdk")
    for py in (ROOT / "crews").glob("*.py"):
        for line in py.read_text(encoding="utf-8").splitlines():
            for mod in banned:
                assert not line.startswith((f"import {mod}", f"from {mod}")), f"{py.name}: {line}"


