"""
shared/paths.py
===============
Single source of truth for the application's base directory.

Works in three environments:
  - PyInstaller exe  → directory containing the .exe
  - Development      → project root (two levels up from this file)
  - Tests            → same as development
"""

import sys
from pathlib import Path


def _resolve_base() -> Path:
    if getattr(sys, "frozen", False):
        # Running as PyInstaller bundle — sys.executable is the .exe path
        return Path(sys.executable).parent
    # Running as source — project root is two levels above shared/
    return Path(__file__).parent.parent


BASE_DIR    = _resolve_base()
CONFIG_FILE = BASE_DIR / "config.json"
AGENTS_DIR  = BASE_DIR / "agents"
LOGS_DIR    = BASE_DIR / "logs"
