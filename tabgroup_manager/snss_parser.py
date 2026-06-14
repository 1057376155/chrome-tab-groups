"""
Parse Chrome SNSS binary session files to recover tab groups and tabs.

Based on the Chrome Tab Group Recovery tool by holzerjm
(https://github.com/holzerjm/ChromeGroupTabRecovery), MIT licensed.
Adapted for the TabGroupManager project.
"""

import json
import re
import struct
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import CHROME_DIR, GROUP_COLORS


CHROME_EPOCH = datetime(1601, 1, 1)


def _chrome_ts_to_datetime(ts: int) -> Optional[datetime]:
    try:
        return CHROME_EPOCH + timedelta(microseconds=ts)
    except (OverflowError, OSError):
        return None


def _ts_from_filename(fname: str) -> Optional[datetime]:
    parts = fname.split("_")
    if len(parts) == 2 and parts[1].isdigit():
        return _chrome_ts_to_datetime(int(parts[1]))
    return None


def get_profiles() -> Dict[str, Dict[str, str]]:
    """Discover all Chrome profiles."""
    profiles: Dict[str, Dict[str, str]] = {}
    local_state = CHROME_DIR / "Local State"
    if local_state.exists():
        try:
            with open(local_state, "r", encoding="utf-8") as f:
                data = json.load(f)
            info_cache = data.get("profile", {}).get("info_cache", {})
            for pdir, info in info_cache.items():
                profiles[pdir] = {
                    "name": info.get("name", pdir),
                    "email": info.get("user_name", ""),
                    "gaia_name": info.get("gaia_name", ""),
                }
        except Exception as exc:  # pragma: no cover - defensive
            print(f"Warning: could not read Local State: {exc}")

    if CHROME_DIR.exists():
        for d in CHROME_DIR.iterdir():
            if d.is_dir() and (d / "Sessions").is_dir():
                dirname = d.name
                if dirname not in profiles:
                    profiles[dirname] = {
                        "name": dirname,
                        "email": "",
                        "gaia_name": "",
                    }
    return profiles


def parse_snss(filepath: Path) -> List[Tuple[int, bytes]]:
    """Parse an SNSS file and return a list of (command_id, payload) tuples."""
    commands: List[Tuple[int, bytes]] = []
    try:
        with open(filepath, "rb") as f:
            magic = f.read(4)
            if magic != b"SNSS":
                return commands
            f.read(4)  # version

            while True:
                size_data = f.read(2)
                if len(size_data) < 2:
                    break
                size = struct.unpack("<H", size_data)[0]
                if size == 0:
                    break
                if size == 0xFFFF:
                    size_data = f.read(4)
                    if len(size_data) < 4:
                        break
                    size = struct.unpack("<I", size_data)[0]
                payload = f.read(size)
                if len(payload) < size:
                    break
                if payload:
                    commands.append((payload[0], payload[1:]))
    except (IOError, OSError) as exc:
        print(f"  Warning: Could not read {filepath}: {exc}", file=__import__("sys").stderr)
    return commands


def _decode_group_metadata(data: bytes) -> Optional[Dict]:
    """Decode command 27 (SetTabGroupMetadata2)."""
    if len(data) < 28:
        return None

    token = data[4:20]
    title_len = struct.unpack("<I", data[20:24])[0]
    title = ""
    if title_len > 0 and 24 + title_len * 2 <= len(data):
        try:
            title = data[24 : 24 + title_len * 2].decode("utf-16-le")
        except UnicodeDecodeError:
            pass

    after_title = 24 + title_len * 2
    # Color/collapsed fields appear to be aligned to a 4-byte boundary.
    aligned = ((after_title + 3) // 4) * 4
    color_id = -1
    collapsed = False
    if aligned + 12 <= len(data):
        color_id = struct.unpack("<I", data[aligned : aligned + 4])[0]
        collapsed = struct.unpack("<I", data[aligned + 4 : aligned + 8])[0] != 0

    ascii_data = data.decode("latin-1", errors="ignore")
    uuid_match = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        ascii_data,
    )

    return {
        "token": token,
        "title": title.strip(),
        "color_name": GROUP_COLORS.get(color_id, f"id={color_id}"),
        "color_id": color_id,
        "collapsed": collapsed,
        "uuid": uuid_match.group(0) if uuid_match else "",
    }


def _extract_tab_windows(commands: List[Tuple[int, bytes]]) -> Dict[int, int]:
    """Map session tab_id -> Chrome window_id.

    cmd 14 records window definitions (first u32 = window_id). cmd 0 records
    tab creation with [window_id (u32), tab_id (u32)] in its payload. We build
    a window-id set from cmd 14 (so we only trust ids that Chrome actually
    registered as windows) and then map every tab via cmd 0. Field layout was
    verified empirically against real Session files.
    """
    window_ids: set = set()
    for cid, data in commands:
        if cid == 14 and len(data) >= 4:
            window_ids.add(struct.unpack("<I", data[0:4])[0])

    tab_windows: Dict[int, int] = {}
    for cid, data in commands:
        if cid == 0 and len(data) >= 8:
            wid = struct.unpack("<I", data[0:4])[0]
            tid = struct.unpack("<I", data[4:8])[0]
            if wid in window_ids:
                tab_windows[tid] = wid
    return tab_windows


def extract_session_data(
    session_path: Path,
) -> Tuple[Dict[str, Dict], Dict[int, Dict], Dict[int, int]]:
    """
    Extract tab groups, tab URLs and tab→window mapping from a Session file.
    Returns (groups_dict, tab_urls_dict, tab_windows_dict).

    ``tab_windows_dict`` maps session tab_id -> Chrome window_id (best effort;
    tabs whose window cannot be recovered are simply absent from the map).
    """
    commands = parse_snss(session_path)
    if not commands:
        return {}, {}, {}

    # cmd 27: group metadata
    groups: Dict[str, Dict] = {}
    for cmd_id, data in commands:
        if cmd_id == 27:
            meta = _decode_group_metadata(data)
            if meta:
                token_hex = meta["token"].hex()
                groups[token_hex] = {
                    "title": meta["title"],
                    "color_name": meta["color_name"],
                    "color_id": meta["color_id"],
                    "collapsed": meta["collapsed"],
                    "uuid": meta["uuid"],
                    "tab_ids": [],
                }

    # cmd 25: tab-to-group assignments
    for cmd_id, data in commands:
        if cmd_id == 25 and len(data) >= 28:
            tab_id = struct.unpack("<I", data[0:4])[0]
            token_hex = data[8:24].hex()
            has_group = struct.unpack("<I", data[24:28])[0]
            if has_group and token_hex in groups:
                groups[token_hex]["tab_ids"].append(tab_id)

    # cmd 6: tab navigation URLs
    tab_urls: Dict[int, Dict] = {}
    for cmd_id, data in commands:
        if cmd_id == 6 and len(data) >= 16:
            session_tab_id = struct.unpack("<I", data[4:8])[0]
            nav_index = struct.unpack("<I", data[8:12])[0]
            url_len = struct.unpack("<I", data[12:16])[0]

            if url_len > 0 and 16 + url_len <= len(data):
                url = data[16 : 16 + url_len].decode("latin-1", errors="replace")

                title = ""
                remaining = data[16 + url_len :]
                for i in range(0, min(len(remaining) - 4, 80)):
                    slen = struct.unpack("<H", remaining[i : i + 2])[0]
                    if 2 <= slen <= 300 and i + 2 + slen * 2 <= len(remaining):
                        try:
                            candidate = remaining[i + 2 : i + 2 + slen * 2].decode(
                                "utf-16-le"
                            )
                            if (
                                candidate.isprintable()
                                and candidate.strip()
                                and not candidate.startswith("http")
                            ):
                                title = candidate.strip()
                                break
                        except UnicodeDecodeError:
                            pass

                if session_tab_id not in tab_urls or nav_index >= tab_urls[
                    session_tab_id
                ].get("nav_index", -1):
                    tab_urls[session_tab_id] = {
                        "url": url,
                        "title": title,
                        "nav_index": nav_index,
                    }

    # cmd 0/14: tab -> window mapping (best effort)
    tab_windows = _extract_tab_windows(commands)

    return groups, tab_urls, tab_windows


def _group_window_id(tab_ids: List[int], tab_windows: Dict[int, int]) -> Optional[int]:
    """Pick the Chrome window_id a group belongs to.

    A group's tabs normally all live in the same window, so we return the most
    common mapped window. Returns None if none of the tabs could be mapped.
    """
    from collections import Counter

    counts = Counter(tab_windows.get(t) for t in tab_ids if t in tab_windows)
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def scan_profile(profile_dir: str, profile_info: Optional[Dict] = None) -> List[Dict]:
    """Scan a single Chrome profile's Sessions directory and return group data.

    Each group dict gains a ``window_id`` (Chrome-native window id, may be
    None). Ungrouped tabs are collected per window into a synthetic
    ``(未分组)`` group (uuid ``ungrouped-<windowId>``) so nothing is lost.
    """
    sessions_dir = CHROME_DIR / profile_dir / "Sessions"
    if not sessions_dir.exists():
        return []

    results: List[Dict] = []
    # Sort by the Chrome-epoch timestamp in the filename, newest first. File
    # size is misleading (older snapshots can be larger) and filesystem mtime
    # lags; the filename timestamp reflects when Chrome actually wrote it, so
    # the newest file holds the set of windows that are currently open.
    session_files = sorted(
        [f for f in sessions_dir.iterdir() if f.name.startswith("Session_")],
        key=lambda f: _ts_from_filename(f.name) or datetime.min,
        reverse=True,
    )

    for sf in session_files:
        file_date = _ts_from_filename(sf.name)
        file_mod = datetime.fromtimestamp(sf.stat().st_mtime)
        groups, tab_urls, tab_windows = extract_session_data(sf)

        if not groups and not tab_urls:
            continue

        grouped_tab_ids: set = set()
        for token_hex, group in groups.items():
            tabs_with_urls = []
            for tid in sorted(group["tab_ids"]):
                if tid in tab_urls:
                    info = tab_urls[tid]
                    if "chrome://saved-tab-groups-unsupported" in info["url"]:
                        continue
                    tabs_with_urls.append(
                        {
                            "url": info["url"],
                            "title": info["title"],
                            "tab_id": tid,
                        }
                    )
                    grouped_tab_ids.add(tid)
            group["tabs"] = tabs_with_urls
            group["window_id"] = _group_window_id(group["tab_ids"], tab_windows)

        # Collect ungrouped tabs (have a URL but belong to no group) and file
        # them under a per-window synthetic group so they survive the scan.
        ungrouped_by_window: Dict[Any, List[Dict]] = defaultdict(list)
        ungrouped_unknown: List[Dict] = []
        for tid, info in tab_urls.items():
            if tid in grouped_tab_ids:
                continue
            url = info.get("url", "")
            if not url or url.startswith("chrome") or url.startswith("about"):
                continue
            item = {"url": url, "title": info.get("title", ""), "tab_id": tid}
            wid = tab_windows.get(tid)
            if wid is not None:
                ungrouped_by_window[wid].append(item)
            else:
                ungrouped_unknown.append(item)

        for wid, items in ungrouped_by_window.items():
            token = f"ungrouped-{wid}"
            groups[token] = {
                "title": "(未分组)",
                "color_name": "grey",
                "color_id": 0,
                "collapsed": False,
                "uuid": token,
                "tab_ids": [i["tab_id"] for i in items],
                "tabs": items,
                "window_id": wid,
            }
        if ungrouped_unknown:
            groups["ungrouped-unknown"] = {
                "title": "(未分组)",
                "color_name": "grey",
                "color_id": 0,
                "collapsed": False,
                "uuid": "ungrouped-unknown",
                "tab_ids": [i["tab_id"] for i in ungrouped_unknown],
                "tabs": ungrouped_unknown,
                "window_id": None,
            }

        results.append(
            {
                "file": sf.name,
                "file_size": sf.stat().st_size,
                "file_modified": file_mod,
                "file_date": file_date,
                "groups": groups,
                "total_nav_entries": len(tab_urls),
            }
        )

    return results


def deduplicate_groups(results: List[Dict]) -> List[Dict]:
    """
    Deduplicate groups across session files.

    The primary key is the group UUID, which is stable across session files.
    When a UUID could not be extracted (empty string), the token_hex differs
    between session files for the same group, so we fall back to a composite
    of (title, color_name, window_id) — the human-visible identity of a group
    in a given window. In each bucket we keep the version with the most tabs.
    """
    def _key(group: Dict, token_hex: str) -> str:
        uuid = group.get("uuid", "")
        if uuid:
            return f"uuid:{uuid}"
        # Fallback: token_hex is unstable across sessions, so use the
        # human-visible identity instead, scoped to its window. Prefix avoids
        # collisions with UUIDs.
        title = (group.get("title") or "").strip().lower()
        color = (group.get("color_name") or "").strip().lower()
        wid = group.get("window_id")
        return f"name:{title}|{color}|{wid}"

    best: Dict[str, Tuple[Dict, Dict]] = {}
    for r in results:
        for token_hex, group in r["groups"].items():
            key = _key(group, token_hex)
            existing = best.get(key)
            if not existing or len(group.get("tabs", [])) > len(
                existing[0].get("tabs", [])
            ):
                best[key] = (group, r)

    # ``results`` is ordered newest-session-first (see scan_profile). The
    # newest file holds the windows that are currently open in Chrome; older
    # files keep records of windows that have since been closed. Collect the
    # set of window ids present in the newest file so we can drop closed
    # windows instead of surfacing stale ones.
    live_windows: set = set()
    if results:
        for g in results[0]["groups"].values():
            wid = g.get("window_id")
            if wid is not None:
                live_windows.add(wid)
    # If the newest file exposes no windows at all (older Chrome build, or the
    # cmd 0/14 layout we rely on changed), fall back to "keep everything"
    # rather than silently dropping all groups.
    use_live_filter = bool(live_windows)

    groups_out = []
    for group, r in best.values():
        wid = group.get("window_id")
        # Drop groups whose window is not in the newest session file — those
        # windows have been closed. Groups whose window could not be
        # determined (wid is None) are always kept.
        if use_live_filter and wid is not None and wid not in live_windows:
            continue
        groups_out.append(
            {
                "title": group["title"] or "(untitled)",
                "color_name": group["color_name"],
                "color_id": group["color_id"],
                "collapsed": group["collapsed"],
                "uuid": group["uuid"],
                "window_id": wid,
                "tabs": group.get("tabs", []),
                "session_file": r["file"],
                "file_modified": r["file_modified"],
            }
        )
    return groups_out
