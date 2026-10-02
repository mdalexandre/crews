"""Opus reseat: the lead column runs on Sonnet everywhere, and the verify column runs on Sonnet
except the two hard-judgment rows (difficult_review, integration_architecture), which stay on Opus.
Worker seats and every non-model field (effort ladder, thresholds, bound, kinds, wave_order) are untouched."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CAT: dict[str, Any] = json.loads((ROOT / "crews" / "catalog.json").read_text(encoding="utf-8"))

OPUS_VERIFY_NEEDS = ("difficult_review", "integration_architecture")
SONNET_VERIFY_HIGH_NEEDS = ("everyday_implementation", "long_running_agentic", "specialist")

# worker seats and their efforts as of the reseat (2026-09-19); this table must never change under this test file.
EXPECTED_WORKERS = {
    "repeatable_task": ("sonnet", "low"),
    "bounded_retrieval": ("sonnet", "medium"),
    "mechanical_verification": ("haiku", None),
    "everyday_implementation": ("sonnet", "medium"),
    "difficult_review": ("opus", "high"),
    "integration_architecture": ("opus", "high"),
    "long_running_agentic": ("sonnet", "medium"),
    "specialist": ("fable", "high"),
}

EXPECTED_LEAD_EFFORT = {
    "repeatable_task": "medium",
    "bounded_retrieval": "high",
    "mechanical_verification": "medium",
    "everyday_implementation": "high",
    "difficult_review": "high",
    "integration_architecture": "high",
    "long_running_agentic": "xhigh",
    "specialist": "high",
}


def test_catalog_version_bumped_for_the_reseat() -> None:
    assert CAT["version"] == "2.1.0"


def test_every_need_row_lead_seat_is_sonnet_with_its_effort_preserved() -> None:
    for need, row in CAT["needs"].items():
        lead = row["lead"]
        assert lead["model"] == "sonnet", f"{need}: lead model is {lead['model']!r}, expected sonnet"
        assert lead["effort"] == EXPECTED_LEAD_EFFORT[need], (
            f"{need}: lead effort is {lead['effort']!r}, expected {EXPECTED_LEAD_EFFORT[need]!r}")


def test_verify_seat_is_sonnet_high_on_the_three_reseated_rows() -> None:
    for need in SONNET_VERIFY_HIGH_NEEDS:
        verify = CAT["needs"][need]["verify"]
        assert verify == {"model": "sonnet", "effort": "high"}, f"{need}: verify is {verify}"


def test_verify_seat_stays_opus_high_on_the_two_hard_judgment_rows() -> None:
    for need in OPUS_VERIFY_NEEDS:
        verify = CAT["needs"][need]["verify"]
        assert verify == {"model": "opus", "effort": "high"}, f"{need}: verify is {verify}"


def test_verify_seat_unchanged_on_the_rows_this_reseat_does_not_touch() -> None:
    assert CAT["needs"]["repeatable_task"]["verify"] == {"model": "sonnet", "effort": "medium"}
    assert CAT["needs"]["bounded_retrieval"]["verify"] == {"model": "sonnet", "effort": "high"}
    assert CAT["needs"]["mechanical_verification"]["verify"] == {"model": "haiku", "effort": None}


def test_worker_seats_are_unchanged_by_the_reseat() -> None:
    for need, (model, effort) in EXPECTED_WORKERS.items():
        worker = CAT["needs"][need]["worker"]
        assert (worker["model"], worker["effort"]) == (model, effort), f"{need}: worker is {worker}"


def test_reseat_touches_only_needs_and_version() -> None:
    """The effort ladder, thresholds, bound, kinds and wave_order must be byte-identical to the pre-reseat catalog."""
    assert CAT["effort_ladder"] == ["low", "medium", "high", "xhigh", "max"]
    assert CAT["capability"] == ["haiku", "sonnet", "opus", "fable"]
    assert CAT["no_effort_models"] == ["haiku"]
    assert CAT["fable_need"] == "specialist"
    assert CAT["thresholds"] == {
        "need_floor": 0.6, "divisible": 0.6, "verifier": 0.6, "ladder": 2.5, "max_workers": 4,
        "max_cells": 8, "specific": 0.5, "require_frame": False, "require_acceptance": True,
    }
    assert CAT["bound"] == {"mem_per_cell_gb": 1.5, "memory_high": "2G", "memory_max": "3G"}
    assert CAT["wave_order"] == ["research", "produce", "integrate", "check"]
    assert set(CAT["kinds"]) == {"research", "produce", "integrate", "check"}
    for kind, spec in CAT["kinds"].items():
        assert set(spec) == {"column", "task_type", "writes", "tools", "body"}
