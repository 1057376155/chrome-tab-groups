"""Configuration and platform paths."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "tab_groups.db"

PLATFORM = sys.platform

if PLATFORM == "darwin":
    CHROME_DIR = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
elif PLATFORM == "win32":
    CHROME_DIR = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
else:
    CHROME_DIR = Path.home() / ".config" / "google-chrome"

# Local HTTP bridge used by the Chrome extension
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 8765
BRIDGE_URL = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}"

# Colors used by Chrome tab groups
GROUP_COLORS = {
    0: "grey",
    1: "blue",
    2: "red",
    3: "yellow",
    4: "green",
    5: "pink",
    6: "purple",
    7: "cyan",
    8: "orange",
}
