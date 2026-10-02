"""The single state-root resolver (OSS-POR-1, DR-OSS-4).

Every file this package writes lives under crews_home(): CREWS_HOME if set, else CLAUDE_PLUGIN_DATA if set,
else ~/.crews. Nothing is written into the package's own install directory. Resolution only, no I/O.
"""

from __future__ import annotations

import os
from pathlib import Path


def crews_home() -> Path:
    """CREWS_HOME, else CLAUDE_PLUGIN_DATA, else ~/.crews."""
    for var in ("CREWS_HOME", "CLAUDE_PLUGIN_DATA"):
        raw = os.environ.get(var)
        if raw:
            return Path(raw).expanduser()
    return Path.home() / ".crews"


def state_dir(name: str) -> Path:
    """A named state subdirectory of crews_home(). Resolution only, no I/O."""
    return crews_home() / name


def resolve_state_path(path: str | Path) -> Path:
    """A relative state path (for example a catalog observations_path) resolved against crews_home();
    an absolute path is returned unchanged."""
    p = Path(path).expanduser()
    return p if p.is_absolute() else crews_home() / p
