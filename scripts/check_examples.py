"""Plan every example in examples/ through the command line and fail unless each one plans.

Usage: uv run python scripts/check_examples.py

For each examples/<name>.roles.json with a matching examples/<name>.answers.json this runs
`crew.py plan --judge off --skills off --brief-check off` with CREWS_HOME and CLAUDE_PLUGIN_DATA pointing at a
temporary directory (nothing is written to the user's own state), and requires exit 0 and at least one call in
the returned waves. Exits 1 on any failure, or when no example is found.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


def plan_example(roles: Path, answers: Path, tmp: Path) -> str | None:
    """None when the example plans with at least one call, else the reason it does not."""
    env = {**os.environ, "CREWS_HOME": str(tmp / "home"), "CLAUDE_PLUGIN_DATA": str(tmp / "data")}
    env.pop("CREWS_SEAT_PREFIX", None)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "crew.py"), "plan", "--roles", str(roles), "--answers", str(answers),
         "--judge", "off", "--skills", "off", "--brief-check", "off", "--run-dir", str(tmp / "run")],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=120, check=False)
    if proc.returncode != 0:
        return f"exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:400]}"
    try:
        waves = json.loads(proc.stdout)["waves"]
    except (ValueError, KeyError, TypeError) as exc:
        return f"unreadable plan output: {exc}"
    calls = sum(len(w.get("calls", [])) for w in waves)
    return None if calls >= 1 else "the plan holds no calls"


def main() -> int:
    roles_files = sorted(EXAMPLES.glob("*.roles.json"))
    if not roles_files:
        print("no examples found", file=sys.stderr)
        return 1
    failed = 0
    for roles in roles_files:
        name = roles.name[: -len(".roles.json")]
        answers = EXAMPLES / f"{name}.answers.json"
        if not answers.is_file():
            print(f"FAIL {name}: missing {answers.name}")
            failed += 1
            continue
        with tempfile.TemporaryDirectory() as tmp:
            problem = plan_example(roles, answers, Path(tmp))
        print(f"FAIL {name}: {problem}" if problem else f"ok   {name}")
        failed += problem is not None
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
