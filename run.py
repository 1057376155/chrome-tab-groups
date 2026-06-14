#!/usr/bin/env python3
"""Launcher for the TabGroupManager GUI (uses the bundled venv)."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PYTHON = ROOT / "venv" / "bin" / "python3"

if not VENV_PYTHON.exists():
    print("Virtual environment not found. Please run:")
    print("  python3.12 -m venv venv")
    print("  ./venv/bin/pip install -r requirements.txt")
    sys.exit(1)

subprocess.run([str(VENV_PYTHON), "-m", "tabgroup_manager"], cwd=str(ROOT))
