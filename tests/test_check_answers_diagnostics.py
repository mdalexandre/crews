"""check_answers must describe the input it actually received.

The task answers argument takes the nested TypeSafe shape. The per-role answers
block in roles[].answers is flat. Passing the flat shape to the task argument is
the easy mistake, so the defect list has to say so rather than report the key as
absent.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crews.budget import check_answers  # noqa: E402

CAT: dict[str, Any] = {"needs": {"everyday_implementation": {}, "mechanical_verification": {}}}

FLAT_ROLE_SHAPE: dict[str, Any] = {
    "need": "everyday_implementation", "difficulty": 1, "divisible": 0.6, "specific": 0.9,
}

VALID_TASK_SHAPE: dict[str, Any] = {
    "need": {"choice": "everyday_implementation", "confidence": 0.9, "probabilities": {}},
    "divisible": {"noul": 0.6},
    "needs_verifier": {"noul": 1.0},
    "difficulty": {"score": 1},
}


def test_present_but_wrong_typed_need_is_not_called_missing() -> None:
    joined = " ".join(check_answers(CAT, FLAT_ROLE_SHAPE))
    assert "need: missing" not in joined
    assert "need: must be an object" in joined


def test_wrong_typed_need_names_the_role_answers_confusion() -> None:
    joined = " ".join(check_answers(CAT, FLAT_ROLE_SHAPE))
    assert "roles[].answers" in joined


def test_genuinely_absent_need_is_still_reported_missing() -> None:
    assert "need: missing" in check_answers(CAT, {"divisible": {"noul": 0.5}})


def test_all_four_keys_are_reported_not_just_the_first() -> None:
    defects = check_answers(CAT, FLAT_ROLE_SHAPE)
    keys = {d.split(":")[0].split(".")[0] for d in defects}
    assert {"need", "divisible", "needs_verifier", "difficulty"} <= keys


def test_valid_task_answers_still_pass() -> None:
    assert check_answers(CAT, VALID_TASK_SHAPE) == []


def test_non_object_answers_still_rejected() -> None:
    assert check_answers(CAT, "everyday_implementation") == ["answers must be a JSON object"]
