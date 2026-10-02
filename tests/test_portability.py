"""P3 portability (OSS-POR-1, OSS-POR-2): one state root, nothing written into the package root."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from test_server import CREW, EIGHT, ROOT, answers, calls, roles  # noqa: E402

from crews import bands, egress, paths  # noqa: E402
from crews import cli as crews_cli

STATE_ENV = ("CREWS_HOME", "CLAUDE_PLUGIN_DATA", "CREWS_EGRESS_DIR", "CREWS_AUDIT_DIR", "CREWS_CALIBRATION_DIR")


def _tree(root: Path) -> set[Path]:
    return {p for p in root.rglob("*")} if root.exists() else set()


def _plan_args(**extra: Any) -> dict[str, Any]:
    return {"roles": roles("caption_listings"), "answers": answers("caption_listings"), "allow": 8,
            "force": EIGHT, **extra}


def test_resolver_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in STATE_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    assert paths.crews_home() == tmp_path / "h" / ".crews"
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin"))
    assert paths.crews_home() == tmp_path / "plugin"
    monkeypatch.setenv("CREWS_HOME", str(tmp_path / "own"))
    assert paths.crews_home() == tmp_path / "own"
    assert paths.resolve_state_path("obs/observations.jsonl") == tmp_path / "own" / "obs" / "observations.jsonl"
    assert paths.resolve_state_path(tmp_path / "abs") == tmp_path / "abs"


def test_every_write_lands_under_crews_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    state = tmp_path / "state"
    for var in STATE_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CREWS_HOME", str(state))
    assert Path.home() == fake_home

    # plan through the server with no run_dir: the run dir defaults under the state root
    env = calls([("crew_plan", _plan_args())], env={"HOME": str(fake_home), "CREWS_HOME": str(state)})[0]
    assert env["status"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS"), env
    assert (state / "runs").is_dir()

    # an egress ledger write
    body: dict[str, Any] = {"questions": {}}
    pkt = egress.packet(body, purpose="portability", requirement_ids=["OSS-POR-1"], data_category="test",
                        includes_worker_output=False, decline_behavior="fallback")
    egress.register(pkt)
    assert (state / "egress" / "pending.jsonl").is_file()

    # an audit write and a calibration directory resolution
    crews_cli.audit("q", "yes", "role", "policy", "none", "ref")
    assert (state / "audit" / "audit.jsonl").is_file()
    assert bands.calibration_dir() == state / "calibration"

    # a record write through the CLI
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "SubagentHandback", "input": {"message": "STATUS: COMPLETE"}}]}}) + "\n",
        encoding="utf-8")
    out = state / "runs" / "rec" / "record.txt"
    r = subprocess.run([sys.executable, "-X", "utf8", str(CREW), "record", "--transcript", str(transcript),
                        "--out", str(out)], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "HOME": str(fake_home), "CREWS_HOME": str(state)})
    assert r.returncode == 0, r.stderr
    assert out.is_file()

    created = _tree(state)
    assert created and all(p == state or state in p.parents for p in created)
    assert _tree(fake_home) == set(), "a write landed under Path.home()"


def _make_readonly(root: Path) -> None:
    for dp, dns, fns in os.walk(root):
        for n in [*dns, *fns]:
            p = Path(dp) / n
            if not p.is_symlink():
                p.chmod(p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    root.chmod(root.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _make_writable(root: Path) -> None:
    for dp, dns, fns in os.walk(root):
        Path(dp).chmod(0o755)
        for n in fns:
            p = Path(dp) / n
            if not p.is_symlink():
                p.chmod(0o644)


@pytest.fixture
def readonly_copy(tmp_path: Path) -> Iterator[Path]:
    dest = tmp_path / "pkgroot"
    shutil.copytree(ROOT, dest, ignore=shutil.ignore_patterns(".venv", ".git", "__pycache__", ".*_cache"))
    _make_readonly(dest)
    try:
        yield dest
    finally:
        _make_writable(dest)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions")
def test_read_only_package_root(readonly_copy: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(roles("caption_listings")), encoding="utf-8")
    ans = tmp_path / "answers.json"
    ans.write_text(json.dumps(answers("caption_listings")), encoding="utf-8")
    env = {**os.environ, "CREWS_HOME": str(state), "PYTHONDONTWRITEBYTECODE": "1"}
    for var in ("CLAUDE_PLUGIN_DATA", "CREWS_EGRESS_DIR", "CREWS_AUDIT_DIR", "CREWS_CALIBRATION_DIR"):
        env.pop(var, None)
    force = tmp_path / "force.json"
    force.write_text(json.dumps(EIGHT), encoding="utf-8")
    crew = readonly_copy / "crew.py"
    before = _tree(readonly_copy)

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-X", "utf8", str(crew), *args], capture_output=True, text=True,
                              timeout=120, env=env, cwd=str(readonly_copy))

    assert run("list").returncode == 0
    assert run("validate").returncode == 0
    r = run("budget", "--answers", str(ans), "--run-dir", str(state / "runs" / "b"))
    assert r.returncode == 0, r.stderr
    r = run("plan", "--roles", str(spec), "--answers", str(ans), "--run-dir", str(state / "runs" / "p"),
            "--allow", "8", "--force", str(force), "--judge", "off")
    assert r.returncode == 0, r.stderr
    assert _tree(readonly_copy) == before, "something was written into the package root"
    assert (state / "runs" / "p").is_dir()
