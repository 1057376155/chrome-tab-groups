"""Open / focus URLs in Google Chrome.

On macOS this prefers *focusing* an already-open tab over opening a new one:
opening the same URL repeatedly used to pile up duplicate tabs. If a URL is
already open in some Chrome window, we activate that tab and bring Chrome to
the front; only URLs that aren't open anywhere get opened as new tabs.
"""

import subprocess
import sys
from typing import List, Optional, Tuple


def _applescape(url: str) -> str:
    """Escape a URL for embedding inside an AppleScript double-quoted string.

    shlex.quote() wraps the value in single quotes, which AppleScript does
    NOT accept as a string literal — it must be inside double quotes, with
    any embedded double quotes or backslashes escaped. Control characters
    (newline / carriage return / tab) are also illegal inside an AppleScript
    string literal, so they are converted to their escape sequences.
    """
    return (
        url.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def _find_tab(url: str) -> Optional[Tuple[int, int]]:
    """Return (windowIndex, tabIndex) of the first Chrome tab whose URL
    equals ``url`` exactly, or None if it is not open.

    Indices are 1-based, matching AppleScript's convention so the result can
    be fed straight back into ``set active tab index``.
    """
    if sys.platform != "darwin":
        return None
    safe = _applescape(url)
    # Return a single marker line "W<win>T<tab>" for the first match.
    # Walking in AppleScript and returning early keeps it fast even with
    # many tabs, because we stop at the first hit.
    script = f"""
set targetURL to "{safe}"
tell application "Google Chrome"
    set winCount to count of windows
    repeat with i from 1 to winCount
        set w to window i
        set tabCount to count of tabs of w
        repeat with j from 1 to tabCount
            if (URL of tab j of w) is targetURL then
                return ("W" & i & "T" & j)
            end if
        end repeat
    end repeat
end tell
return "NOTFOUND"
"""
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    out = r.stdout.strip()
    if not out or out == "NOTFOUND":
        return None
    # Parse "W2T13" → (2, 13)
    try:
        w_part, t_part = out[1:].split("T")
        return int(w_part), int(t_part)
    except (ValueError, IndexError):
        return None


def focus_tab(window_index: int, tab_index: int) -> bool:
    """Activate the given Chrome tab and bring its window to the front.

    Indices are 1-based. Returns True on success.
    """
    if sys.platform != "darwin":
        return False
    # set active tab index selects the tab within its window; setting the
    # window's index to 1 brings it in front of other Chrome windows, and
    # raising the Chrome process hands focus to the app itself.
    script = f"""
tell application "Google Chrome"
    set w to window {window_index}
    set active tab index of w to {tab_index}
    set index of w to 1
end tell
tell application "System Events"
    set frontmost of (first process whose name is "Google Chrome") to true
end tell
return "ok"
"""
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.returncode == 0


def _open_new_tab(url: str) -> None:
    """Open ``url`` as a new tab in Chrome's front window (or a new window)."""
    if sys.platform == "darwin":
        safe = _applescape(url)
        script = f'tell application "Google Chrome" to open location "{safe}"'
        subprocess.run(["osascript", "-e", script], capture_output=True)
    else:
        import webbrowser

        webbrowser.open(url)


def open_url(url: str) -> bool:
    """Focus the tab if ``url`` is already open, otherwise open a new tab.

    Returns True if an existing tab was focused, False if a new tab had to be
    opened (or on non-macOS where the concept doesn't apply).
    """
    if sys.platform == "darwin":
        found = _find_tab(url)
        if found is not None:
            focus_tab(*found)
            return True
    _open_new_tab(url)
    return False


def open_urls(urls: List[str]) -> None:
    """Open/focus each URL. Existing tabs are focused, the rest are opened.

    To avoid stealing focus back and forth, all "already open" tabs are
    focused first (the last one wins the foreground), then all missing URLs
    are opened as new tabs in a single batched AppleScript call.
    """
    if not urls:
        return
    if sys.platform != "darwin":
        for u in urls:
            open_url(u)
        return

    missing: List[str] = []
    last_focused = False
    for u in urls:
        found = _find_tab(u)
        if found is not None:
            focus_tab(*found)
            last_focused = True
        else:
            missing.append(u)

    if missing:
        # Open all missing URLs in the front window in one script.
        # The same "open location" sequence works whether Chrome has a window
        # or not: if there is no window, the first `open location` creates one
        # and the rest are added as tabs inside it. (A naive `repeat with u in
        # {...}` / `open location u` loop would open N separate windows when
        # Chrome has none.)
        locations = "\n".join(
            f'      open location "{_applescape(u)}"' for u in missing
        )
        script = f"""tell application "Google Chrome"
    if (count of windows) is 0 then
        open location "{_applescape(missing[0])}"
        tell front window
{chr(10).join('      open location "' + _applescape(u) + '"' for u in missing[1:])}
        end tell
    else
        tell front window
{locations}
        end tell
    end if
end tell
"""
        subprocess.run(["osascript", "-e", script], capture_output=True)

    if last_focused:
        # Make sure Chrome ends up in front.
        subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to set frontmost of '
             '(first process whose name is "Google Chrome") to true'],
            capture_output=True,
        )
