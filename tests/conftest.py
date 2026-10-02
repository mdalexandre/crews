from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_crews_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with its own CREWS_HOME, so the plan index and run dirs a test writes never land in
    the real ~/.crews. A test that cares about the resolver deletes or sets the variable itself."""
    monkeypatch.setenv("CREWS_HOME", str(tmp_path_factory.mktemp("crews_home")))
