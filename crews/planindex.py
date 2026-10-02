"""The plan index (OSS-FIX-1): every planned agent call, recorded on disk under crews_home().

Interface
    record(view, run_dir, now=None, home=None) -> int   append one JSON line per agent call of a plan view
    admitted(home, subagent_type, description, prompt_sha256, now) -> bool   was this exact call planned lately
    prompt_sha256(prompt) -> str   sha256 hex of the exact prompt string
    index_path(home=None) -> Path   <crews home>/plans/index.jsonl

A line holds ts (UTC ISO), subagent_type, description, prompt_sha256 and run_dir. The seat guard admits a
dispatch from this index alone, so a plan whose tool result a host spilled to a file (G-36) is still admitted.
Nothing here writes under the package root; the file lives under crews_home() (paths.py).
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from crews import paths

WINDOW = timedelta(hours=24)
SKEW = timedelta(minutes=5)


def index_path(home: Path | None = None) -> Path:
    return (home if home is not None else paths.crews_home()) / "plans" / "index.jsonl"


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    from crews import egress  # noqa: PLC0415  lazy: the guard imports this module and needs no egress

    with open(path.with_name("index.lock"), "a+", encoding="utf-8") as fh:
        egress._flock(fh.fileno(), exclusive=True)  # noqa: SLF001  the package's one flock helper
        try:
            yield
        finally:
            egress._flock(fh.fileno(), exclusive=False)  # noqa: SLF001


def record(view: dict[str, Any], run_dir: str, now: datetime | None = None, home: Path | None = None) -> int:
    """Append one line per agent call in the plan view; returns the number of lines written."""
    ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    lines: list[str] = []
    for wave in view.get("waves") or []:
        for call in wave.get("calls") or []:
            ac = call["agent_call"]
            lines.append(json.dumps({"ts": ts, "subagent_type": ac["subagent_type"],
                                     "description": ac["description"],
                                     "prompt_sha256": prompt_sha256(ac["prompt"]), "run_dir": str(run_dir)},
                                    sort_keys=True))
    if not lines:
        return 0
    path = index_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path):
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, ("\n".join(lines) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    return len(lines)


def admitted(home: Path, subagent_type: str, description: str, digest: str, now: datetime) -> bool:
    """True when the index holds a line with this subagent_type, description and prompt digest, recorded
    within the last 24 hours. A missing, unreadable or malformed index admits nothing."""
    path = index_path(home)
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return False
    with fh:
        for line in fh:
            if digest not in line:
                continue
            try:
                entry = json.loads(line)
                ts = datetime.fromisoformat(str(entry["ts"]))
            except (ValueError, KeyError, TypeError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if (entry.get("subagent_type") == subagent_type and entry.get("description") == description
                    and entry.get("prompt_sha256") == digest and -SKEW <= now - ts <= WINDOW):
                return True
    return False
