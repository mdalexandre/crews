"""claude-crews command line entry point. Every verb lives in crews.cli; this file only fixes the import path."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from crews.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
