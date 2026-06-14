"""Configuration and platform paths.

The data directory is kept under the user's home (~/Library/Application
Support on macOS, %APPDATA% on Windows, ~/.local/share on Linux) so that
development mode (`./run.py`) and the packaged .app read and write the SAME
database. Using Path(__file__) would break after pyinstaller freezing — the
frozen bundle's internal dir is read-only on macOS and different from the
source tree, so the app would silently start using an empty DB.
"""

import sys
from pathlib import Path

if sys.platform == "darwin":
    DATA_DIR = (
        Path.home() / "Library" / "Application Support" / "ChromeTabGroupManager"
    )
elif sys.platform == "win32":
    DATA_DIR = Path.home() / "AppData" / "Local" / "ChromeTabGroupManager"
else:
    DATA_DIR = Path.home() / ".local" / "share" / "ChromeTabGroupManager"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "tab_groups.db"

# Backwards-compat: if a previous dev-mode install left its DB next to the
# source tree (PROJECT_ROOT/data/tab_groups.db) and the new location is empty,
# copy it over so existing saved windows aren't lost.
try:
    _legacy_db = Path(__file__).resolve().parent.parent / "data" / "tab_groups.db"
    if _legacy_db.exists() and not DB_PATH.exists():
        import shutil
        shutil.copy2(_legacy_db, DB_PATH)
except Exception:
    pass  # never let migration crash the app

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
