"""Phase 2: agent file installer against a temporary directory."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CREW = ROOT / "crew.py"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-X", "utf8", str(CREW), *args], capture_output=True, text=True, timeout=30)


def frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---"
    out: dict[str, str] = {}
    for line in lines[1:]:
        if line == "---":
            break
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seats() -> list[dict[str, object]]:
    result = run("list", "--json")
    assert result.returncode == 0
    listed: list[dict[str, object]] = json.loads(result.stdout)["seats"]
    return listed


def test_installer_one_file_per_seat(tmp_path: Path) -> None:
    assert run("install", "--agents-dir", str(tmp_path)).returncode == 0
    expected = seats()
    files = sorted(tmp_path.glob("*.md"))
    assert [f.stem for f in files] == sorted(str(s["agent"]) for s in expected)
    for seat in expected:
        fm = frontmatter(tmp_path / f"{seat['agent']}.md")
        assert fm["model"] == seat["model"]
        if seat["effort"] is None:
            assert "effort" not in fm
        else:
            assert fm["effort"] == seat["effort"]
        assert "Agent" not in fm["tools"]


def test_installer_foreign_file_untouched(tmp_path: Path) -> None:
    name = str(seats()[0]["agent"])
    foreign = tmp_path / f"{name}.md"
    foreign.write_text("---\nname: mine\nmodel: opus\n---\nhand authored\n", encoding="utf-8")
    before = sha(foreign)
    result = run("install", "--agents-dir", str(tmp_path))
    assert result.returncode == 1
    assert name in result.stderr
    assert sha(foreign) == before


def test_installer_drift_and_removal(tmp_path: Path) -> None:
    assert run("install", "--agents-dir", str(tmp_path)).returncode == 0
    assert run("check", "--agents-dir", str(tmp_path)).returncode == 0
    victim = next(p for p in sorted(tmp_path.glob("*.md")) if "effort:" in p.read_text(encoding="utf-8"))
    text = victim.read_text(encoding="utf-8")
    edited = "\n".join("effort: max" if ln.startswith("effort:") else ln for ln in text.splitlines()) + "\n"
    victim.write_text(edited, encoding="utf-8")
    drift = run("check", "--agents-dir", str(tmp_path))
    assert drift.returncode == 1 and victim.name in drift.stderr
    mine = tmp_path / "hand-authored.md"
    mine.write_text("---\nname: hand-authored\nmodel: sonnet\n---\nmine\n", encoding="utf-8")
    before = sha(mine)
    dry = run("uninstall", "--agents-dir", str(tmp_path), "--dry-run")
    assert dry.returncode == 0 and len(json.loads(dry.stdout)["removed"]) == len(seats())
    assert len(list(tmp_path.glob("crew-*.md"))) == len(seats())
    assert run("uninstall", "--agents-dir", str(tmp_path)).returncode == 0
    assert list(tmp_path.glob("crew-*.md")) == []
    assert sha(mine) == before


def test_installer_replacement_is_backed_up(tmp_path: Path) -> None:
    assert run("install", "--agents-dir", str(tmp_path)).returncode == 0
    target = sorted(tmp_path.glob("crew-*.md"))[0]
    target.write_text(target.read_text(encoding="utf-8") + "extra line\n", encoding="utf-8")
    assert run("install", "--agents-dir", str(tmp_path)).returncode == 0
    backups = list(tmp_path.glob(f"{target.name}.bak-crews-*"))
    assert len(backups) == 1 and "extra line" in backups[0].read_text(encoding="utf-8")


