"""Calibration data and measured bands (SRD typesafe-integration-v1.0 section 2.5, C-BND;
requirements TSI-CAL-01, TSI-CAL-03, TSI-CAL-04, TSI-CAL-05, TSI-CAL-06).

A band is a code computed threshold on Choice confidence, measured from owner labeled
observations, never a guessed default. Everything in this module is deterministic and reads no
network; the only inputs are what the caller passes and, for `bands_status`, one local file.

Interface
    record_observation(path, obs) -> bool
        appends obs to the observations file unless a record with the same case_id and
        request_fingerprint already exists there; returns whether it was appended.
    review_set(items, observed, *, target_field, sample_size, seed) -> dict
        {"disagreements", "agreement_sample", "agreements_total", "missing"}. No defaults for
        target_field, sample_size, or seed: calling without one of them raises TypeError, because
        which field to compare, how many to sample, and the sampling seed are owner policy, never
        an assumed value.
    wilson_lower(k, n, z=1.959963984540054) -> float
        the Wilson score interval's lower bound for k successes out of n trials; 0.0 when n is 0.
        z defaults to the two sided 95 percent value.
    derive_threshold(points, target, min_n) -> dict
        points are (confidence, correct) pairs. Scans distinct confidence values t in ascending
        order; at each t, the set is every point with confidence at least t. Returns the first t
        whose set has count at least min_n and Wilson lower bound at least target, as
        {"status": "MEASURED", "threshold", "n", "k", "precision", "wilson_lower"}. With no such
        t, returns {"status": "INSUFFICIENT", "n_total", "reason"}.
    derive_bands(points, config) -> dict
        config is {"bands": {"act": {"target": ...}, "confirm": {"target": ...}}, "min_n": ...},
        required in full. A missing key raises ValueError, because targets and the minimum count
        are owner policy, not defaults. Returns {"act": <derive_threshold result>, "confirm": <...>}.
    bands_status(path) -> dict
        the parsed bands file at path, or {"status": "UNCALIBRATED"} when the file is absent,
        unreadable, not valid JSON, or not a JSON object (INV-12: absence is never treated as
        calibrated).
    dataset_fingerprint(path) -> str
        sha256 hex digest of the observations file's raw bytes at path (JEV-16: the dataset
        fingerprint a band's provenance records is a hash of the observations content, never the
        path they happen to live at).

Data directory: env CREWS_CALIBRATION_DIR, else ~/.crews/calibration, holding
observations.jsonl and bands.json. calibration_dir() resolves it; callers (the CLI wiring, out
of this module's scope) pass the resulting paths into the functions above.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

DEFAULT_CALIBRATION_DIRNAME = ".crews/calibration"
Z_95 = 1.959963984540054


def calibration_dir() -> Path:
    """CREWS_CALIBRATION_DIR if set, else ~/.crews/calibration. Resolution only, no I/O."""
    env = os.environ.get("CREWS_CALIBRATION_DIR")
    if env:
        return Path(env)
    return Path.home() / DEFAULT_CALIBRATION_DIRNAME


def dataset_fingerprint(path: str | Path) -> str:
    """The sha256 hex digest of the observations file's raw bytes at path (JEV-16 dataset
    fingerprint). A path names a file that can change without the path itself changing, so a path
    alone can never prove what a band was actually measured on; the content hash is the value a
    later reader can check the observations file against.
    """
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def record_observation(path: str | Path, obs: dict[str, Any]) -> bool:
    """Appends obs as one JSON line unless a record sharing its case_id and request_fingerprint
    is already present. Returns True when appended, False when it was a duplicate.
    """
    p = Path(path)
    case_id = obs["case_id"]
    fingerprint = obs["request_fingerprint"]

    if p.is_file():
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                existing = json.loads(line)
                if existing.get("case_id") == case_id and existing.get("request_fingerprint") == fingerprint:
                    return False

    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obs, sort_keys=True, ensure_ascii=False) + "\n")
    return True


def review_set(
    items: list[dict[str, Any]],
    observed: dict[str, Any],
    *,
    target_field: str,
    sample_size: int,
    seed: Any,
) -> dict[str, Any]:
    """Compares each item's target_field to its observed label. An item with no entry in
    observed is missing, never counted as agreeing or disagreeing. The agreement sample is the
    first sample_size agreeing ids ordered by sha256(f"{seed}:{id}"), so the same inputs always
    produce the same sample (TSI-CAL-03 reproducibility) with no dependence on item order.
    """
    disagreements: list[str] = []
    agreements: list[str] = []
    missing: list[str] = []

    for item in items:
        item_id = item["id"]
        if item_id not in observed:
            missing.append(item_id)
            continue
        if observed[item_id] == item.get(target_field):
            agreements.append(item_id)
        else:
            disagreements.append(item_id)

    ordered_agreements = sorted(
        agreements, key=lambda item_id: hashlib.sha256(f"{seed}:{item_id}".encode("utf-8")).hexdigest()
    )

    return {
        "disagreements": disagreements,
        "agreement_sample": ordered_agreements[:sample_size],
        "agreements_total": len(agreements),
        "missing": missing,
    }


def wilson_lower(k: int, n: int, z: float = Z_95) -> float:
    """Wilson score interval lower bound for k successes out of n Bernoulli trials. 0.0 when n
    is 0, since no evidence supports any bound.
    """
    if n == 0:
        return 0.0
    p_hat = k / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    center = p_hat + z2 / (2 * n)
    spread = z * math.sqrt((p_hat * (1.0 - p_hat) / n) + (z2 / (4.0 * n * n)))
    return (center - spread) / denominator


def derive_threshold(points: list[tuple[float, bool]], target: float, min_n: int) -> dict[str, Any]:
    """Scans distinct confidence values ascending; at each candidate threshold t, the evaluated
    set is every point with confidence >= t. Returns the first (lowest, most inclusive) t whose
    set has count >= min_n and Wilson lower bound >= target. MEASURED carries the threshold, the
    set size n, the correct count k, the raw precision k/n, and the Wilson lower bound itself.
    INSUFFICIENT carries the total point count and a reason, never a guessed threshold.
    """
    candidates = sorted({confidence for confidence, _correct in points})

    for t in candidates:
        subset = [correct for confidence, correct in points if confidence >= t]
        n = len(subset)
        if n < min_n:
            continue
        k = sum(1 for correct in subset if correct)
        lower = wilson_lower(k, n)
        if lower >= target:
            return {
                "status": "MEASURED",
                "threshold": t,
                "n": n,
                "k": k,
                "precision": k / n,
                "wilson_lower": lower,
            }

    return {
        "status": "INSUFFICIENT",
        "n_total": len(points),
        "reason": f"no confidence threshold reached both min_n={min_n} and target={target}",
    }


def derive_bands(points: list[tuple[float, bool]], config: dict[str, Any]) -> dict[str, Any]:
    """config = {"bands": {"act": {"target"}, "confirm": {"target"}}, "min_n"}, required in
    full: targets and the minimum count are owner policy, so a missing key raises ValueError
    rather than falling back to a default.
    """
    if "bands" not in config:
        raise ValueError("derive_bands: config missing required key 'bands'")
    if "min_n" not in config:
        raise ValueError("derive_bands: config missing required key 'min_n'")

    bands_cfg = config["bands"]
    for name in ("act", "confirm"):
        if name not in bands_cfg:
            raise ValueError(f"derive_bands: config['bands'] missing required key '{name}'")
        if "target" not in bands_cfg[name]:
            raise ValueError(f"derive_bands: config['bands']['{name}'] missing required key 'target'")

    min_n = config["min_n"]
    return {name: derive_threshold(points, bands_cfg[name]["target"], min_n) for name in ("act", "confirm")}


def bands_status(path: str | Path) -> dict[str, Any]:
    """The parsed bands file, or {"status": "UNCALIBRATED"} when it is absent, unreadable, not
    valid JSON, or not a JSON object. Absence is never read as calibrated (INV-12).
    """
    p = Path(path)
    if not p.is_file():
        return {"status": "UNCALIBRATED"}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"status": "UNCALIBRATED"}
    if not isinstance(data, dict):
        return {"status": "UNCALIBRATED"}
    return data
