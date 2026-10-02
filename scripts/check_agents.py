"""OSS-PKG-3, PKG-4: agents/ must equal what `crew install` generates, byte for byte, and hold exactly the
seats the catalog can reach.

Interface
    python3 scripts/check_agents.py [--agents-dir DIR]   exit 0 when identical, 1 on any difference, 2 on a
    catalog or usage error. Regenerates into a temp dir, then diffs file names and bytes against agents/
    (default: <repo>/agents). Prints the seat count on success. Writes nothing outside the temp dir.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crews import catalog, installer  # noqa: E402


def differences(agents_dir: Path) -> tuple[list[str], int]:
    """(difference lines, number of regenerated seats)."""
    cat = catalog.load()
    with tempfile.TemporaryDirectory(prefix="crews-agents-") as tmp:
        fresh = Path(tmp)
        res = installer.install(cat, fresh)
        if res.problems:
            return [f"regeneration problem: {p}" for p in res.problems], 0
        want = {p.name: p.read_bytes() for p in fresh.iterdir() if p.is_file()}
    have = {p.name: p.read_bytes() for p in agents_dir.iterdir() if p.is_file()} if agents_dir.is_dir() else {}
    diffs: list[str] = []
    for name in sorted(set(want) | set(have)):
        if name not in have:
            diffs.append(f"{name}: missing from {agents_dir}")
        elif name not in want:
            diffs.append(f"{name}: not a reachable seat")
        elif have[name] != want[name]:
            diffs.append(f"{name}: differs from the regenerated file")
    return diffs, len(want)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agents-dir", default=str(ROOT / "agents"))
    ns = ap.parse_args(argv)
    try:
        diffs, count = differences(Path(ns.agents_dir))
    except (catalog.CatalogError, OSError) as exc:
        print(f"check_agents: {exc}", file=sys.stderr)
        return 2
    for line in diffs:
        print(f"check_agents: {line}", file=sys.stderr)
    if diffs:
        return 1
    print(f"check_agents: ok, {count} seats match the catalog")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
