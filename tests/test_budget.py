"""Phase 3: cell budget from declared answers."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
CREW = ROOT / "crew.py"
TABLE = json.loads((ROOT / "tests" / "fixtures" / "live_table.json").read_text(encoding="utf-8"))
GENERIC = set(json.loads((ROOT / "crews" / "catalog.json").read_text(encoding="utf-8"))["generic_names"])


def budget(answers: dict[str, Any] | None, tmp: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    if answers is None:
        args = ["--outage"]
    else:
        f = tmp / "answers.json"
        f.write_text(json.dumps(answers), encoding="utf-8")
        args = ["--answers", str(f)]
    return subprocess.run([sys.executable, "-X", "utf8", str(CREW), "budget", *args, *extra],
                          capture_output=True, text=True, timeout=30)


def ans(need: str, conf: float, div: float, ver: float, diff: float, probs: dict[str, float] | None = None
        ) -> dict[str, Any]:
    return {"need": {"choice": need, "confidence": conf, "probabilities": probs or {need: conf}},
            "divisible": {"noul": div}, "needs_verifier": {"noul": ver}, "difficulty": {"score": diff}}


@pytest.mark.parametrize("row", TABLE["rows"], ids=[r["task"] for r in TABLE["rows"]])
def test_budget_live_table(row: dict[str, Any], tmp_path: Path) -> None:
    result = budget(row["answers"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["budget"] == row["cells"]


def test_budget_live_table_sequence(tmp_path: Path) -> None:
    got = [json.loads(budget(r["answers"], tmp_path).stdout)["budget"] for r in TABLE["rows"]]
    assert got == [1, 5, 2, 6, 6, 6]


def test_budget_outage(tmp_path: Path) -> None:
    out = json.loads(budget(None, tmp_path).stdout)
    assert out["budget"] == 2 and out["check_required"] is True and out["need"] == "everyday_implementation"
    assert any("judge unavailable" in n for n in out["notes"])


@pytest.mark.parametrize("difficulty,parallel", [(0.5, 2), (1.5, 4)])
def test_budget_half_to_even(difficulty: float, parallel: int, tmp_path: Path) -> None:
    out = json.loads(budget(ans("everyday_implementation", 0.9, 0.9, 0.1, difficulty), tmp_path).stdout)
    assert out["parallel_cells"] == parallel and out["budget"] == parallel + 1


@pytest.mark.parametrize("difficulty,effort", [(2.49, "medium"), (2.50, "high")])
def test_budget_ladder_edge(difficulty: float, effort: str, tmp_path: Path) -> None:
    out = json.loads(budget(ans("everyday_implementation", 0.9, 0.1, 0.1, difficulty), tmp_path).stdout)
    assert out["worker_seat"]["effort"] == effort


def test_budget_haiku_keeps_no_effort(tmp_path: Path) -> None:
    out = json.loads(budget(ans("mechanical_verification", 0.9, 0.1, 0.1, 2.9), tmp_path).stdout)
    assert out["worker_seat"] == {"model": "haiku", "effort": None}


def test_budget_confidence_floor(tmp_path: Path) -> None:
    probs = {"repeatable_task": 0.59, "everyday_implementation": 0.30, "bounded_retrieval": 0.11}
    low = json.loads(budget(ans("repeatable_task", 0.59, 0.1, 0.1, 1.0, probs), tmp_path).stdout)
    assert low["need"] == "everyday_implementation"
    assert low["row"] == json.loads((ROOT / "crews" / "catalog.json").read_text())["needs"]["everyday_implementation"]
    assert any("below 0.6" in n and "everyday_implementation" in n for n in low["notes"])
    probs["repeatable_task"] = 0.60
    at = json.loads(budget(ans("repeatable_task", 0.60, 0.1, 0.1, 1.0, probs), tmp_path).stdout)
    assert at["need"] == "repeatable_task" and at["notes"] == []


def _walk(node: Any) -> list[tuple[str | None, Any]]:
    out: list[tuple[str | None, Any]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            out.append((k, v))
            out += _walk(v)
    elif isinstance(node, list):
        for v in node:
            out.append((None, v))
            out += _walk(v)
    return out


@pytest.mark.parametrize("row", TABLE["rows"], ids=[r["task"] for r in TABLE["rows"]])
def test_budget_deterministic_and_role_free(row: dict[str, Any], tmp_path: Path) -> None:
    first = budget(row["answers"], tmp_path, "--run-dir", str(tmp_path / "a"))
    second = budget(row["answers"], tmp_path, "--run-dir", str(tmp_path / "b"))
    assert hashlib.sha256(first.stdout.encode()).hexdigest() == hashlib.sha256(second.stdout.encode()).hexdigest()
    written = json.loads((tmp_path / "a" / "budget.json").read_text(encoding="utf-8"))
    for key, value in _walk(written):
        assert key not in ("role", "roles")
        assert not (isinstance(value, str) and value in GENERIC)


def test_budget_bad_answers_refused(tmp_path: Path) -> None:
    bad = ans("not_a_need", 0.9, 0.1, 0.1, 1.0)
    result = budget(bad, tmp_path)
    assert result.returncode == 2 and "unknown need" in result.stderr
