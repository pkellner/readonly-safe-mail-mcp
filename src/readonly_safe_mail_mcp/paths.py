"""Platform-specific locations for user config and the local index.

Overridable with RSMM_CONFIG_DIR / RSMM_DATA_DIR (used by tests and by people
who keep config somewhere unusual).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "readonly-safe-mail-mcp"


def config_dir() -> Path:
    if p := os.environ.get("RSMM_CONFIG_DIR"):
        return Path(p).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / APP_NAME
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_NAME


def data_dir() -> Path:
    if p := os.environ.get("RSMM_DATA_DIR"):
        return Path(p).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / APP_NAME
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / APP_NAME


# Folder names that commonly sync to cloud storage. The index must not live in one.
_SYNCED_MARKERS = (
    "Dropbox",
    "iCloud Drive",
    "Mobile Documents",
    "OneDrive",
    "Google Drive",
    "Desktop",
    "Documents",
)


def looks_synced(path: Path) -> bool:
    parts = {p for p in path.resolve().parts}
    return any(marker in parts for marker in _SYNCED_MARKERS)
