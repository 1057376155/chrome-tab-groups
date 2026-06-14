"""Build script for packaging the app as a macOS .app bundle.

Usage:
    ./venv/bin/python setup.py py2app

Produces dist/Chrome Tab Group Manager.app — a standalone macOS app with its
own Dock icon, process name, and menu-bar entry (no longer showing as
"python3"). The Chrome extension files are bundled under Resources/ so the
app stays self-contained.
"""

from setuptools import setup

APP = ["tabgroup_manager/__main__.py"]
APP_NAME = "Chrome Tab Group Manager"

OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": "com.cor.chrome-tab-groups",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleExecutable": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleIconFile": "app_icon.icns",
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "LSUIElement": False,  # show in Dock
    },
    "packages": ["tabgroup_manager"],
    "includes": ["PyQt6", "PyQt6.QtCore", "PyQt6.QtWidgets", "PyQt6.QtGui", "PyQt6.QtNetwork"],
    "resources": ["resources/app_icon.icns"],
    "iconfile": "resources/app_icon.icns",
}

setup(
    name=APP_NAME,
    app=APP,
    options={"py2app": OPTIONS},
)
