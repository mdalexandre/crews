"""Catalog: the single source of every crew configuration.

Interface
    load(path) -> dict            parse the catalog, refusing duplicate keys
    validate(cat) -> list[str]    every defect found, empty when the catalog is sound
    reachable_seats(cat) -> list  every (kind, model, effort) a plan can reach, plus one ladder step
    seat_name(kind, model, effort) -> str   `crew-<kind>-<model>-<effort>`,
        or `crew-<kind>-<model>` for models that take no effort

The seat set is computed from the registry, never typed. Kinds are configuration, never role names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parent / "catalog.json"
KINDS = ("research", "produce", "integrate", "check")
COLUMNS = ("worker", "lead", "verify")
FORBIDDEN_TOOLS = ("Agent", "Task")


class CatalogError(ValueError):
    """A catalog that cannot be used. The message names the defect."""


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise CatalogError(f"duplicate key {key!r}")
        seen[key] = value
    return seen


def load(path: Path | str | None = None) -> dict[str, Any]:
    target = Path(path) if path else DEFAULT_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicates)
    except json.JSONDecodeError as exc:
        raise CatalogError(f"not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CatalogError("catalog is not a JSON object")
    return data


def validate(cat: dict[str, Any]) -> list[str]:
    defects: list[str] = []
    ladder = cat.get("effort_ladder")
    capability = cat.get("capability")
    if ladder != ["low", "medium", "high", "xhigh", "max"]:
        defects.append("effort_ladder must be low, medium, high, xhigh, max")
    if not isinstance(capability, list) or not capability:
        defects.append("capability order is missing")
        capability = []
    no_effort = set(cat.get("no_effort_models") or [])
    fable_need = cat.get("fable_need")
    needs = cat.get("needs")
    if not isinstance(needs, dict) or not needs:
        defects.append("needs registry is missing")
        needs = {}
    for need, row in needs.items():
        if not isinstance(row, dict) or set(row) != set(COLUMNS):
            defects.append(f"need {need}: columns must be exactly worker, lead, verify")
            continue
        for column in COLUMNS:
            seat = row[column]
            model, effort = seat.get("model"), seat.get("effort")
            where = f"need {need} column {column}"
            if model not in capability:
                defects.append(f"{where}: model {model!r} is not in the capability order")
            if model in no_effort and effort is not None:
                defects.append(f"{where}: {model} seat carries effort {effort!r}; it takes none")
            if model not in no_effort and effort not in (ladder or []):
                defects.append(f"{where}: effort {effort!r} is outside low, medium, high, xhigh, max")
            if model == "fable" and need != fable_need:
                defects.append(f"{where}: fable appears outside the {fable_need} row")
    kinds = cat.get("kinds")
    if not isinstance(kinds, dict) or set(kinds) != set(KINDS):
        defects.append("kinds must be exactly research, produce, integrate, check")
        kinds = kinds if isinstance(kinds, dict) else {}
    for kind, spec in kinds.items():
        tools = spec.get("tools") or []
        bad = [t for t in tools if t in FORBIDDEN_TOOLS]
        if bad:
            defects.append(f"kind {kind}: tools include {', '.join(bad)}; no kind may dispatch")
        if spec.get("column") not in COLUMNS:
            defects.append(f"kind {kind}: column must be worker, lead, or verify")
    thresholds = cat.get("thresholds") or {}
    for name in ("need_floor", "divisible", "verifier", "ladder", "max_workers", "max_cells", "specific"):
        if not isinstance(thresholds.get(name), (int, float)):
            defects.append(f"threshold {name} is missing")
    if not cat.get("generic_names"):
        defects.append("generic_names is empty")
    if cat.get("wave_order") != list(KINDS):
        defects.append("wave_order must be research, produce, integrate, check")
    questions = cat.get("questions")
    if not isinstance(questions, dict) or set(questions) != {"need", "divisible", "needs_verifier", "difficulty"}:
        defects.append("questions must be need, divisible, needs_verifier, difficulty")
    defects += _validate_typesafe(cat.get("typesafe"))
    return defects


def _validate_typesafe(typesafe: Any) -> list[str]:
    """The optional `typesafe` block (SRD typesafe-integration-v1.0.md section 2.5, C-CLI). Absent is sound;
    present but malformed is not."""
    if typesafe is None:
        return []
    defects: list[str] = []
    if not isinstance(typesafe, dict):
        return ["typesafe must be an object"]
    skr = typesafe.get("skill_routing")
    if not isinstance(skr, dict):
        defects.append("typesafe.skill_routing must be an object")
    else:
        globs = skr.get("root_globs")
        if not isinstance(globs, list) or not globs or not all(isinstance(g, str) and g for g in globs):
            defects.append("typesafe.skill_routing.root_globs must be a non empty list of non empty strings")
        max_candidates = skr.get("max_candidates")
        if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or max_candidates <= 0:
            defects.append("typesafe.skill_routing.max_candidates must be a positive integer")
        thresholds = skr.get("thresholds")
        if not isinstance(thresholds, dict) or not all(
                isinstance(thresholds.get(k), (int, float)) and not isinstance(thresholds.get(k), bool)
                for k in ("fit", "gate")):
            defects.append("typesafe.skill_routing.thresholds must hold numeric fit and gate")
        if not isinstance(skr.get("calibrated"), bool):
            defects.append("typesafe.skill_routing.calibrated must be a boolean")
    for name, key in (("handback", "probe_threshold"), ("brief_leak", "leak_threshold")):
        block = typesafe.get(name)
        if not isinstance(block, dict):
            defects.append(f"typesafe.{name} must be an object")
            continue
        value = block.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            defects.append(f"typesafe.{name}.{key} must be a number from 0 to 1")
        if not isinstance(block.get("calibrated"), bool):
            defects.append(f"typesafe.{name}.calibrated must be a boolean")
    return defects


def bump(cat: dict[str, Any], effort: str | None, steps: int = 1) -> str | None:
    if effort is None:
        return None
    ladder: list[str] = cat["effort_ladder"]
    return ladder[min(ladder.index(effort) + steps, len(ladder) - 1)]


def seat_name(kind: str, model: str, effort: str | None) -> str:
    return f"crew-{kind}-{model}" if effort is None else f"crew-{kind}-{model}-{effort}"


def reachable_seats(cat: dict[str, Any]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for row in cat["needs"].values():
        for kind, spec in cat["kinds"].items():
            seat = row[spec["column"]]
            model, effort = seat["model"], seat["effort"]
            for eff in {effort, bump(cat, effort)}:
                name = seat_name(kind, model, eff)
                found[name] = {"agent": name, "kind": kind, "model": model, "effort": eff, "tools": list(spec["tools"])}
    return [found[k] for k in sorted(found)]
