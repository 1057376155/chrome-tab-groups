"""Standalone launcher for pyinstaller packaging.

Uses absolute imports so the frozen executable (which doesn't run inside a
package context) can import the module correctly. Run directly with
`python app.py` or freeze with pyinstaller pointing here.
"""

from tabgroup_manager.gui import run

if __name__ == "__main__":
    run()
